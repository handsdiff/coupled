#!/usr/bin/env python3
"""Offline-only new-pipeline 50-train/50-future LR pilot; no provider dispatch.

Re-render exact frozen semantic inputs with Qwen3.6's native tokenizer and loss
contract. No new context selection, truncation, target correction, or main-run
authorization is inferred. The evaluation block is never trained.
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
from pathlib import Path
import resource
import statistics
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent))
spec=importlib.util.spec_from_file_location('model_preflight',Path(__file__).with_name('run-phase1-qwen-model-preflights.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
from phase1_qwen38_execution import NativeRows, prior_order

VERSION='phase1-qwen36-lr-pilot-preparation-v1'
RATES=(5e-5,1e-4,2e-4)
STORAGE_RESERVE=1.00


def select(cohort, blocks):
    m.require(len(blocks)>=2 and len(cohort)>=100,'Two full blocks required')
    train=[e['exampleID'] for e in cohort[:50]]
    test=[e['exampleID'] for e in cohort[50:100]]
    m.require(blocks[0]['exampleIDs']==train and blocks[1]['exampleIDs']==test,'Cohort differs from frozen chronological blocks')
    m.require(len(set(train+test))==100 and not set(train)&set(test),'Overlapping training/evaluation IDs')
    m.require(max(e['targetAvailableAt'] for e in cohort[:50]) < min(e['targetBeganAt'] for e in cohort[50:100]),
        'A training target was unavailable when evaluation began')
    return train,test


def budget(rows, train, test, prices):
    def total(kind,ids):return sum(m.original.charge(kind,rows[e],prices) for e in ids)
    training=total('train',train)
    score_gen=total('generation',test)
    score_nll=total('nll',test)
    # One frozen baseline plus three trained arms; not three copies of baseline.
    token_total=3*training+4*(score_gen+score_nll)
    recovery=training+max(m.original.charge('generation',rows[e],prices)+m.original.charge('nll',rows[e],prices) for e in test)
    return {'perRateTrainingUSD':training,'allThreeTrainingUSD':3*training,
        'oneArmGenerationMaximumUSD':score_gen,'oneArmTargetNLLUSD':score_nll,
        'allFourScoringUSD':4*(score_gen+score_nll),'scheduledTokenMaximumUSD':token_total,
        'optionalOneWholeTrainingArmRetryPlusOneScoreUSD':recovery,
        'checkpointStorageReserveUSD':STORAGE_RESERVE,
        'proposedAuthorizationUSD':token_total+recovery+STORAGE_RESERVE,
        'pricingMeaning':'Uncached prefill, maximum 512 output tokens, exact shifted train positions; not invoice cost. Recheck before execution.'}


def prepare(a):
    m.require(not a.output.exists(),'Use a fresh immutable directory')
    report=json.loads(a.reference.read_text())
    reference_path=next(Path(p) for p in report['sourceBindingsSHA256'] if p.endswith('/preparation.json'))
    reference=json.loads(reference_path.read_text())
    plan_path=Path(reference['executionPlan']);source_plan=json.loads(plan_path.read_text())
    m.require(m.fingerprint(source_plan)==reference['planSHA256'],'Source plan changed')
    arm=source_plan['arms']['new'];pack=Path(arm['packDirectory'])
    m.require(m.file_hash(pack/'native-rows.jsonl')==arm['nativeRowsSHA256'],'Source rows changed')
    m.require(m.file_hash(pack/'blocks.json')==arm['blocksSHA256'],'Source schedule changed')
    for path,digest in report['sourceBindingsSHA256'].items():m.require(m.file_hash(path)==digest,'Preflight source changed')
    # Retain only the first hundred small cohort records, and one native row at a time.
    cohort=[]
    with (pack/'cohort.jsonl').open() as f:
        for line in f:
            cohort.append(json.loads(line))
            if len(cohort)==100:break
    blocks=json.loads((pack/'blocks.json').read_text())
    train,test=select(cohort,blocks)
    source_tok,_=m.original.local_renderer()
    tok,renderer=m.local_model('qwen36_hybrid')
    native=m.prep.native_runtime_from_tokenizer('qwen36_hybrid',tok)
    source=NativeRows(pack/'native-rows.jsonl')
    model=report['models']['qwen36_hybrid']
    m.require(m.fingerprint(tok.get_vocab())==model['tokenizerVocabularySHA256'],'Native vocabulary changed')
    a.output.mkdir(parents=True)
    compact={}
    import tinker
    with (a.output/'native-rows.jsonl').open('x') as output, (a.output/'cohort.jsonl').open('x') as co:
        for e in cohort:
            old=source[e['exampleID']]
            semantic,target=m.prep.source_text(old,source_tok)
            m.require(e['targetText']==target,'Frozen target changed')
            row=m.prep.supervised_row(e,semantic,native)
            m.native_datum(row,tinker,model['nativeStopTokenIDs'][0])
            m.require(row['promptTokenCount']+512<=65536,'Generation exceeds native context')
            m.require(row['modelInputSHA256']==old['modelInputSHA256'] and row['targetSHA256']==old['targetSHA256'],'Task changed while rendering')
            row.update(pipeline='new',model=model['model'],sourceFullSequenceTokenSHA256=old['fullSequenceTokenSHA256'])
            output.write(json.dumps(row,ensure_ascii=False,separators=(',',':'))+'\n')
            co.write(json.dumps(e,ensure_ascii=False,separators=(',',':'))+'\n')
            compact[e['exampleID']]={k:v for k,v in row.items() if k not in ('promptTokenIDs','completionTokenIDs')}
    model_contract=copy.deepcopy(model['trainingContract'])
    # Remove unrelated ten-example memorization protocol fields explicitly.
    for name in ('overfitMaximumEpochs','overfitEvaluationEpochs','overfitOrder'):model_contract.pop(name,None)
    model_contract.update(startingPoint='fresh base adapter per learning rate; no prior trained checkpoint',
        checkpointTTLSeconds=604800,epochsPerNewBlockUpdate=1)
    model_contract['generation']={**source_plan['contract']['generation'],'stopTokenIDs':model['nativeStopTokenIDs']}
    order=prior_order(train,1,17)
    contracts=[]
    for rate in RATES:
        contract=copy.deepcopy(model_contract);contract['optimizer']['learningRate']=rate
        contracts.append({'name':f'lr-{rate:g}','contract':contract,'trainingOrder':order})
    costs=budget(compact,train,test,m.prep.PRICES['qwen36_hybrid'])
    p={
        'version':VERSION,'status':'offline_prepared_NEEDS_BUDGET_AND_EXECUTOR_APPROVAL','providerCalls':0,
        'projectID':m.PROJECT,'model':'Qwen/Qwen3.6-35B-A3B','reasoning':'off','pipeline':'new',
        'purpose':'Directional future-write learning and LR choice; not the full old/new comparison.',
        'referencePlan':str(plan_path),'referencePlanFingerprint':reference['planSHA256'],
        'sourcePack':str(pack),'sourcePipelineEligibleExamples':arm['counts']['examples'],
        'sourceBindingsSHA256':{str(p.resolve()):m.file_hash(p) for p in (a.reference,reference_path,plan_path,
            pack/'native-rows.jsonl',pack/'cohort.jsonl',pack/'blocks.json')},
        'codeSHA256':{str(p.resolve()):m.file_hash(p) for p in (Path(__file__),m.ROOT/'scripts/phase1_qwen35_native.py',
            m.ROOT/'scripts/prepare-phase1-qwen-model-preflights.py',m.ROOT/'scripts/run-phase1-qwen-model-preflights.py',m.ROOT/'scripts/phase1_qwen38_execution.py')},
        'nativeRowsSHA256':m.file_hash(a.output/'native-rows.jsonl'),'cohortSHA256':m.file_hash(a.output/'cohort.jsonl'),
        'tokenizer':{k:model[k] for k in ('localTokenizerRevision','tokenizerVocabularySHA256','tokenizerFilesSHA256','nativeStopTokenIDs','pasteMarkerIDs')},
        'trainIDs':train,'evaluationIDs':test,'arms':contracts,
        'counts':{'uniqueTrainingExamples':50,'uniqueFutureExamples':50,'trainingPresentations':150,
            'frozenGenerations':50,'personalizedGenerations':150,'targetNLLCalls':200,'optimizerSteps':150},
        'tokens':{'trainingPositionsPerRate':sum(compact[e]['trainingDatumPositions'] for e in train),
            'lossBearingPresentationsPerRate':sum(compact[e]['lossBearingTokenCount'] for e in train),
            'evaluationTargetTokens':sum(compact[e]['lossBearingTokenCount'] for e in test),
            'promptMinimum':min(x['promptTokenCount'] for x in compact.values()),
            'promptMaximum':max(x['promptTokenCount'] for x in compact.values()),
            'promptMedian':statistics.median(x['promptTokenCount'] for x in compact.values()),
            'targetsExceeding512Tokens':[e for e in test if compact[e]['lossBearingTokenCount']>512]},
        'timeline':{'trainingStart':cohort[0]['targetBeganAt'],'trainingAvailableThrough':max(e['targetAvailableAt'] for e in cohort[:50]),
            'evaluationBegins':cohort[50]['targetBeganAt'],'evaluationEnds':cohort[-1]['targetAvailableAt']},
        'historyPolicy':'Exact frozen semantic inputs; later evaluation queries may contain earlier observed human writes, but no evaluation target receives training loss.',
        'evaluationPolicy':{'primary':'Blinded intended-thought pass using the agreed human-calibrated rubric.',
            'secondary':'Paired future-target NLL/BPB versus the single frozen baseline; exact/prefix metrics supplementary.',
            'failure':'Invalid or empty generations remain failures, never silently excluded.',
            'report':'All responses, returned token IDs, native termination, per-query timing, token-rate costs, training losses and checkpoint paths.',
            'selectionCaveat':'One 50-case development block, one seed, and measured cross-client variability; not a definitive optimum or untouched final validation set.'},
        'prices':m.prep.PRICES['qwen36_hybrid'],'budget':costs,
        'gates':['Resolve/report fixed-checkpoint scoring control','Explicit sufficient spending authorization',
            'Frozen resumable executor and no-network failure tests','Remote tokenizer and current-pricing checks before data transmission'],
        'mainRunAuthorized':False,'memoryPeakMiB':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024**2}
    m.original.save(a.output/'preparation.json',p)
    print(json.dumps({'status':p['status'],'counts':p['counts'],'tokens':p['tokens'],'budget':costs,'memoryPeakMiB':p['memoryPeakMiB']},indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--reference',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    prepare(parser.parse_args())

#!/usr/bin/env python3
"""Select/audit the exact Qwen3.8 native first100 rows; no provider calls.

One fresh 2e-4 reasoning-off adapter, first50 train once and next50 score.
The original full pack is already Qwen3.8-native: do not invent another wrapper.
"""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import statistics

spec = importlib.util.spec_from_file_location('lr', Path(__file__).with_name('run-phase1-qwen-lr-pilot.py'))
r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)
m, p = r.m, r.p
VERSION = 'phase1-qwen38-future-preparation-v1'
MODEL = 'Qwen/Qwen3.8-27B'
PRICES = {'prefill': 1.86, 'sample': 5.595, 'train': 4.103}


def contracts(reference):
    arm = copy.deepcopy(next(a for a in reference['arms'] if a['contract']['optimizer']['learningRate'] == 2e-4))
    arm['contract'].update(model=MODEL, reasoning=False)
    return [arm]


def budget(rows, train, test):
    total = lambda kind, ids: sum(m.original.charge(kind, rows[e], PRICES) for e in ids)
    t, g, n = total('train', train), total('generation', test), total('nll', test)
    retry = t + sum(max(m.original.charge(k, rows[e], PRICES) for e in test) for k in ('generation','nll'))
    return {'perRateTrainingUSD':t, 'oneArmGenerationMaximumUSD':g, 'oneArmTargetNLLUSD':n,
            'scheduledTokenMaximumUSD':t+2*(g+n), 'optionalRecoveryMaximumUSD':retry,
            'checkpointStorageReserveUSD':r.QWEN38_STORAGE_RESERVE,
            'proposedAuthorizationUSD':t+2*(g+n)+retry+r.QWEN38_STORAGE_RESERVE,
            'pricingMeaning':'Exact shifted Qwen3.8-native rows, uncached input and 512 output ceiling; not invoice cost.'}


def validate_row(row, example, tok, renderer):
    import tinker
    from tinker_cookbook.renderers import TrainOnWhat, get_text_content
    semantic, target = m.prep.source_text(row, tok)
    assert target == example['targetText']
    assert row['completionTokenIDs'] == tok.encode(target, add_special_tokens=False)+[248046]
    messages = [{'role':'user','content':semantic}]
    prompt = renderer.build_generation_prompt(messages).to_ints()
    assert prompt == row['promptTokenIDs'] == m.prep.hf_prompt(tok, messages, enable_thinking=False)
    full, weights = renderer.build_supervised_example(messages+[{'role':'assistant','content':target}],
                                                      train_on_what=TrainOnWhat.LAST_ASSISTANT_MESSAGE)
    assert full.to_ints() == prompt+row['completionTokenIDs']
    assert weights.tolist() == [0.]*len(prompt)+[1.]*row['lossBearingTokenCount']
    datum = m.native_datum(row, tinker, 248046)
    assert datum.loss_fn_inputs['weights'].to_numpy().tolist() == weights.tolist()[1:]
    assert len(prompt)+512 <= 65536
    parsed, _ = renderer.parse_response(row['completionTokenIDs'])
    assert get_text_content(parsed) == target
    try: m.native_datum(row, tinker, 248044)
    except m.ContractError: pass
    else: raise AssertionError('Base EOS accepted for Qwen3.8')


def tokenizer_metadata(tok):
    directory = m.original.PREP/'tokenizer'
    prior = json.loads((m.original.PREP/'native-format-checks.json').read_text())
    files = {f.name:m.file_hash(f) for f in sorted(directory.iterdir()) if f.is_file()}
    assert files == prior['tokenizerFiles']
    return {'localTokenizerRevision':prior['tokenizerRevision'],
            'tokenizerVocabularySHA256':m.fingerprint(tok.get_vocab()), 'tokenizerFilesSHA256':files,
            'nativeStopTokenIDs':[248046], 'pasteMarkerIDs':tok.encode('<|paste|>',add_special_tokens=False)}


def prepare(a):
    reference, previous = r.load_prepared(a.reference)
    m.require(reference['model']=='Qwen/Qwen3.6-35B-A3B', 'Expected original matched pilot')
    m.require(not a.output.exists(), 'Use a fresh immutable directory')
    cohort = [json.loads(l) for l in (a.reference/'cohort.jsonl').open()]
    source = p.NativeRows(Path(reference['sourcePack'])/'native-rows.jsonl')
    tok, renderer = r.local_renderer('qwen38_off')
    metadata = tokenizer_metadata(tok)
    a.output.mkdir(parents=True)
    compact = {}
    with (a.output/'native-rows.jsonl').open('x') as out:
        for e in cohort:
            row = source[e['exampleID']]
            validate_row(row,e,tok,renderer)
            assert row['modelInputSHA256']==previous[e['exampleID']]['modelInputSHA256']
            assert row['targetSHA256']==previous[e['exampleID']]['targetSHA256']
            out.write(json.dumps(row,ensure_ascii=False,separators=(',',':'))+'\n')
            compact[e['exampleID']]={k:v for k,v in row.items() if not k.endswith('TokenIDs')}
    shutil.copyfile(a.reference/'cohort.jsonl',a.output/'cohort.jsonl')
    report = copy.deepcopy(reference); report.pop('memoryPeakMiB',None)
    report.update(version=VERSION,status='offline_prepared_requires_frozen_execution',model=MODEL,reasoning='off',
        referencePilotDirectory=str(a.reference.resolve()), prices=PRICES, tokenizer=metadata, arms=contracts(reference),
        purpose='Matched Qwen3.8/Base/Qwen3.6 future-write comparison; one 2e-4 rate, not a sweep.',
        nativeFormat='Original Qwen3.8 HF-matched non-thinking prompt including masked empty think block; exact content plus loss-bearing im_end 248046.',
        nativeRowsSHA256=m.file_hash(a.output/'native-rows.jsonl'),cohortSHA256=m.file_hash(a.output/'cohort.jsonl'),
        counts={'uniqueTrainingExamples':50,'uniqueFutureExamples':50,'trainingPresentations':50,
                'frozenGenerations':50,'personalizedGenerations':50,'targetNLLCalls':100,'optimizerSteps':50},
        budget=budget(compact,reference['trainIDs'],reference['evaluationIDs']))
    report['tokens']={'trainingPositionsPerRate':sum(compact[e]['trainingDatumPositions'] for e in report['trainIDs']),
        'lossBearingPresentationsPerRate':sum(compact[e]['lossBearingTokenCount'] for e in report['trainIDs']),
        'evaluationTargetTokens':sum(compact[e]['lossBearingTokenCount'] for e in report['evaluationIDs']),
        'promptMinimum':min(x['promptTokenCount'] for x in compact.values()),
        'promptMaximum':max(x['promptTokenCount'] for x in compact.values()),
        'promptMedian':statistics.median(x['promptTokenCount'] for x in compact.values()),
        'targetsExceeding512Tokens':[e for e in report['evaluationIDs'] if compact[e]['lossBearingTokenCount']>512]}
    paths = [a.reference/n for n in ('preparation.json','native-rows.jsonl','cohort.jsonl','audit.json')]
    paths += [m.original.PREP/'native-format-checks.json',m.original.PREP/'check-native-format.py']
    paths += list((m.original.PREP/'tokenizer').glob('*'))
    report['sourceBindingsSHA256'].update({str(f.resolve()):m.file_hash(f) for f in paths if f.is_file()})
    report['codeSHA256'][str(Path(__file__).resolve())]=m.file_hash(Path(__file__))
    m.require(report['budget']['proposedAuthorizationUSD']<=20, 'Quoted pilot exceeds $20')
    r.native_spec(report)
    m.original.save(a.output/'preparation.json',report)
    print(json.dumps({'status':'offline_prepared','counts':report['counts'],'tokens':report['tokens'],'budget':report['budget']},indent=2))


def audit(directory):
    report=json.loads((directory/'preparation.json').read_text())
    for path,digest in {**report['sourceBindingsSHA256'],**report['codeSHA256']}.items():
        m.require(m.file_hash(path)==digest,'Bound source/code changed: '+path)
    reference, previous = r.load_prepared(Path(report['referencePilotDirectory']))
    cohort=[json.loads(l) for l in (directory/'cohort.jsonl').open()]
    blocks=json.loads((Path(report['sourcePack'])/'blocks.json').read_text())
    assert p.select(cohort,blocks)==(reference['trainIDs'],reference['evaluationIDs'])
    assert report['arms']==contracts(reference) and report['prices']==PRICES
    assert m.file_hash(directory/'cohort.jsonl')==reference['cohortSHA256']==report['cohortSHA256']
    assert m.file_hash(directory/'native-rows.jsonl')==report['nativeRowsSHA256']
    tok,renderer=r.local_renderer('qwen38_off'); assert tokenizer_metadata(tok)==report['tokenizer']
    rows=p.NativeRows(directory/'native-rows.jsonl'); source=p.NativeRows(Path(report['sourcePack'])/'native-rows.jsonl')
    for e in cohort:
        row=rows[e['exampleID']]; assert row==source[e['exampleID']]
        validate_row(row,e,tok,renderer)
        assert row['modelInputSHA256']==previous[e['exampleID']]['modelInputSHA256']
        assert row['targetSHA256']==previous[e['exampleID']]['targetSHA256']
    assert budget(rows,report['trainIDs'],report['evaluationIDs'])==report['budget']
    result={'status':'audit_passed_OFFLINE_ONLY','examples':100,'trainingExamples':50,'futureExamples':50,
        'exactSameSemanticInputsTargetsAndOrderAsQwen36':True,'originalQwen38RowsUnchanged':True,
        'everyShiftedLossPositionVerified':True,'hfNonthinkingPromptMatches':True,'nativeEOS248046Verified':True,
        'baseEOSRejected':True,'providerCalls':0,
        'filesSHA256':{n:m.file_hash(directory/n) for n in ('preparation.json','native-rows.jsonl','cohort.jsonl')},
        'auditCodeSHA256':m.file_hash(Path(__file__))}
    m.original.save(directory/'audit.json',result);print(json.dumps(result,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--reference',type=Path);parser.add_argument('--output',type=Path);parser.add_argument('--audit',type=Path)
    a=parser.parse_args();audit(a.audit) if a.audit else prepare(a)

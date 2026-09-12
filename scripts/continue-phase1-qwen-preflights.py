#!/usr/bin/env python3
"""Finish the authorized model checks; retain failed attempts and their costs.

The MoE diagnostic measures forward variability WITHOUT an optimizer update.
It does not turn a failed exact-continuation test into a claimed exact restore.
The overfit test always uses a fresh adapter, independently of that diagnostic.
"""
from __future__ import annotations

import argparse
import copy
import itertools
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import importlib.util
_spec = importlib.util.spec_from_file_location('multi', Path(__file__).with_name('run-phase1-qwen-model-preflights.py'))
m = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(m)
VERSION = 'phase1-qwen-model-preflight-recovery-v1'
ORDER = ['qwen36_hybrid', 'qwen35_base', 'qwen38_reasoning_low']


def prior_binding(paths, prepared_sha):
    bindings, cost, cached = {}, 0., {}
    for path in paths:
        state = json.loads((path / 'run.json').read_text())
        plan = json.loads((path / 'execution.json').read_text())
        m.require(state['status'] == 'stopped_requires_review' and state['projectID'] == m.PROJECT,
                  'Only reviewed stopped attempts may be carried')
        m.require(plan['preparedSHA256'] == prepared_sha and plan['hardCeilingUSD'] == 20., 'Prior approval/data changed')
        m.require(m.file_hash(path/'execution.json') == state['executionSHA256'], 'Prior plan changed')
        records = [json.loads(line) for line in (path/'operations.jsonl').open()]
        starts = {r['key']: r for r in records if r['kind'] == 'operation_begin'}
        ends = {r['key']: r for r in records if r['kind'] == 'operation_result'}
        m.require(len(starts) == sum(r['kind']=='operation_begin' for r in records), 'Duplicate starts')
        m.require(len(ends) == sum(r['kind']=='operation_result' for r in records), 'Duplicate results')
        m.require(starts.keys() == ends.keys(), 'Uncertain prior operations require explicit recovery')
        local_cost = sum(r['maximumUSD'] for r in starts.values())
        m.require(math.isclose(local_cost, state['reservedTokenCostUSD'], abs_tol=1e-9), 'Prior local charge changed')
        cost += local_cost  # Never add an attempt's already-carried cost twice.
        for key, result in ends.items():
            if key.startswith('qwen36_hybrid/old/base/'):
                m.require(result['operation'] in ('generation', 'nll'), 'Cannot reuse mutable operation')
                m.require(key not in cached, 'Duplicate baseline cache')
                cached[key] = {'record': result, 'source': str(path.resolve())}
        for name in ('run.json','execution.json','operations.jsonl'):
            bindings[str((path/name).resolve())] = m.file_hash(path/name)
        for prior_name, digest in (plan.get('priorReasoningGate') or {}).get('filesSHA256', {}).items():
            m.require(prior_name in bindings and bindings[prior_name] == digest,
                      'Supply all prior attempts in chronological order')
    m.require(len(paths) == 2 and len(cached) == 10, 'Expected reasoning gate and 35B checkpoint attempt')
    return {'filesSHA256': bindings, 'reservedTokenCostUSD': cost}, cached


def runtime():
    r = m.runtime_binding()
    r['filesSHA256'][str(Path(__file__).resolve())] = m.file_hash(Path(__file__))
    return r


def low_rows(rows, tok):
    renderer = m.prep.ExactReasoningRenderer(tok, reasoning_effort='low')
    result = {}
    for eid, source in rows['qwen36_hybrid']['new'].items():
        semantic, target = m.prep.source_text(source, tok)
        prompt = renderer.build_generation_prompt([{'role':'user', 'content':semantic}]).to_ints()
        m.require(prompt == m.prep.hf_prompt(tok, [{'role':'user','content':semantic}],
                    enable_thinking=True, reasoning_effort='low'), 'Low reasoning differs from native template')
        previous = rows['qwen38_reasoning']['new'][eid]
        m.require(previous['targetSHA256'] == m.prep.text_hash(target) and
                  previous['modelInputSHA256'] == m.prep.text_hash(semantic), 'Reasoning changed the task')
        result[eid] = {**previous, 'promptTokenIDs':prompt, 'promptTokenCount':len(prompt),
                       'reasoningEffort':'low', 'generationOnly':True, 'trainingDatum':None}
    return renderer, result


def difference(a, b):
    x, y = a['targetLogprobs'], b['targetLogprobs']
    m.require(len(x) == len(y) and x, 'Likelihood shape mismatch')
    return {'maxTokenLogprobDelta':max(abs(u-v) for u,v in zip(x,y)),
            'meanAbsoluteTokenLogprobDelta':sum(abs(u-v) for u,v in zip(x,y))/len(x),
            'meanNLLDelta':abs(a['meanNLL']-b['meanNLL'])}


def classify_restore(before, after, repeated, continuous, restored):
    initial = difference(before, after)
    m.require(initial['maxTokenLogprobDelta'] <= .005, 'Restored weights differ before update')
    pairs = [difference(a,b) for a,b in itertools.combinations(repeated,2)]
    delta = difference(continuous, restored)
    variable = max(p['maxTokenLogprobDelta'] for p in pairs) > .005
    exact = delta['maxTokenLogprobDelta'] <= .005
    # Never raise the original tolerance or assert optimizer equality from noise.
    status = ('exact_continuation_pass' if exact else
              'inconclusive_training_forward_varies_without_update' if variable else
              'failed_continuation_without_measured_forward_variability')
    return {'status':status, 'weightReload':initial, 'continuation':delta,
            'sameUnchangedTrainerForwardComparisons':pairs,
            'exactOptimizerContinuationVerified':exact,
            'mainRunGatePassed':exact,
            'interpretation':'Fresh-adapter capability/overfit may finish; non-exact restoration remains a separate main-run gate'}


class DiagnosticBridge(m.NativeBridge):
    def forward_only(self, trainer, row):
        start = time.monotonic()
        output = trainer.forward([m.native_datum(row,self.sdk,self.terminator)], 'cross_entropy').result()
        values = output.loss_fn_outputs[0]['logprobs'].tolist()
        m.require(len(values) == row['trainingDatumPositions'], 'Forward length mismatch')
        result = m.nll_result(values[row['promptTokenCount']-1:])
        metrics = m.plain(output.metrics)
        m.require(math.isclose(metrics['loss:sum'],result['weightedNLLSum'],rel_tol=2e-5,abs_tol=1e-4), 'Forward mask/loss mismatch')
        return {**result,'forwardMetrics':metrics,'latencySeconds':time.monotonic()-start,
                'inputTokens':row['trainingDatumPositions'],'optimizerUpdatePerformed':False}


class RecoveryCalls(m.SharedCalls):
    def __init__(self, *args, cached=None, **kwargs):
        super().__init__(*args, **kwargs); self.cached = cached or {}
    def call(self,key,kind,row,fn):
        full = self.key+'/'+key
        if full in self.cached:
            old = self.cached[full]
            m.require(old['record']['operation']==kind and kind in ('generation','nll'), 'Invalid cached operation')
            m.require(not any(r.get('key')==full for r in self.journal.records), 'Cached result already consumed')
            # Frozen row/model/recipe is bound by both prior and current plans.
            self.journal.append({'kind':'reused_prior_result','key':full,'source':old['source'],
                                 'sourceResultSHA256':m.fingerprint(old['record']),'at':m.utc()})
            return old['record']['value']
        return super().call(key,kind,row,fn)


def phases(bridge,calls,rows,spec,arm):
    probes, ids = spec['probeIDs'], spec['trainIDs']; scores = {}; base = bridge.sampler()
    def op(label,kind,e,fn): return calls.call(arm+'/'+label,kind,rows[e],fn)
    for eid in probes:
        scores[eid] = {'base':{kind:op('base/'+eid+'/'+kind,kind,eid,
            lambda kind=kind,eid=eid:(bridge.generate if kind=='generation' else bridge.nll)(base,rows[eid]))
            for kind in ('generation','nll')}}
    clients = m.original.JournaledBridge(bridge)
    t = op('fresh_adapter','admin',ids[0],lambda:clients.trainer(arm))
    for i,eid in enumerate(ids[:2]): op('train/'+str(i),'train',eid,lambda eid=eid:clients.train(t,rows[eid]))
    a = op('checkpoint-A','admin',ids[0],lambda:clients.checkpoint(t,arm+'-A'))
    before = op('before-restore','nll',ids[0],lambda:bridge.nll(bridge.sampler(a['samplerCheckpointPath']),rows[ids[0]]))
    restored = op('restore-optimizer','admin',ids[0],lambda:clients.trainer(arm,a))
    ar = op('checkpoint-A-restored','admin',ids[0],lambda:clients.checkpoint(restored,arm+'-A-restored'))
    after = op('after-restore','nll',ids[0],lambda:bridge.nll(bridge.sampler(ar['samplerCheckpointPath']),rows[ids[0]]))
    m.original.compare_probabilities(before,after,.005)
    # Three forwards, no gradients and no optimizer changes, on ONE trainer.
    # Charge at full training price: conservative even if forward is cheaper.
    repeats = [op('diagnostic-forward/'+str(i),'train',ids[2],
        lambda:bridge.forward_only(clients.clients[t['clientHandle']],rows[ids[2]])) for i in range(3)]
    for name,client in [('continuous',t),('restored',restored)]:
        op('train-third-'+name,'train',ids[2],lambda client=client:clients.train(client,rows[ids[2]]))
    b = op('checkpoint-B','admin',ids[2],lambda:clients.checkpoint(t,arm+'-B'))
    br = op('checkpoint-B-restored','admin',ids[2],lambda:clients.checkpoint(restored,arm+'-B-restored'))
    left = op('continuous-third-NLL','nll',ids[0],lambda:bridge.nll(bridge.sampler(b['samplerCheckpointPath']),rows[ids[0]]))
    right = op('restored-third-NLL','nll',ids[0],lambda:bridge.nll(bridge.sampler(br['samplerCheckpointPath']),rows[ids[0]]))
    restoration = classify_restore(before,after,repeats,left,right)
    for eid in probes:
        scores[eid]['after']={kind:op('after/'+eid+'/'+kind,kind,eid,
            lambda kind=kind,eid=eid:(bridge.generate if kind=='generation' else bridge.nll)(bridge.sampler(br['samplerCheckpointPath']),rows[eid]))
            for kind in ('generation','nll')}
    return {'scores':scores,'restorationDiagnostic':restoration,'trainedExamples':3,
            'optimizerStepsIncludingRestorationFork':4,'noUpdateForwardCalls':3,'checkpoint':br}


def prepare(a):
    m.require(not a.output.exists(),'Fresh output required')
    report,reference,rows=m.load_inputs(a.prepared)
    prior,cached=prior_binding(a.prior,m.file_hash(a.prepared/'preparation.json'))
    tok,_=m.local_model('qwen38_reasoning'); _,low=low_rows(rows,tok)
    # Both full MoE suites, six diagnostic forwards/model, the full 80 low
    # reasoning outputs, all prior attempts, and shared storage allowance.
    maximum=prior['reservedTokenCostUSD']+.5
    for key in ('qwen36_hybrid','qwen35_base'):
        maximum+=report['models'][key]['budget']['maximumTokenCostUSD']
        for arm in ('old','new'):
            r=rows[key][arm][reference['arms'][arm]['trainIDs'][2]]
            maximum+=3*r['trainingDatumPositions']*m.prep.PRICES[key]['train']/1e6
    maximum+=sum(4*(low[e]['promptTokenCount']*1.86+8192*5.595)/1e6 for e in reference['additionalTests']['probeIDs'])
    m.require(maximum<=20.,'Recovery maximum exceeds shared authorization')
    a.output.mkdir(parents=True)
    with (a.output/'reasoning-low-rows.jsonl').open('x') as f:
        for eid in sorted(low): f.write(json.dumps(low[eid],separators=(',',':'),ensure_ascii=False)+'\n')
    m.original.save(a.output/'execution.json',{'version':VERSION,'preparedDirectory':str(a.prepared.resolve()),
        'preparedSHA256':m.file_hash(a.prepared/'preparation.json'),'runtime':runtime(),'projectID':m.PROJECT,
        'hardCeilingUSD':20.,'maximumIncludingStorageUSD':maximum,'priorAttempts':prior,
        'priorDirectories':[str(p.resolve()) for p in a.prior], 'modelOrder':ORDER,
        'reasoningLowRowsSHA256':m.file_hash(a.output/'reasoning-low-rows.jsonl'),
        'recipe':'Unchanged original native MoE training; fresh disposable adapters. Reasoning low is frozen inference only.',
        'diagnostic':'Exact weight reload, repeated no-update forwards, measured next-update divergence. Non-exact remains a main-run gate.',
        'reasoningOutputPolicy':'Complete all 80 low-effort samples; unfinished reasoning is an explicit failed prediction, never a fabricated final answer.',
        'recoveryPolicy':'Reuse only the 10 bound baseline results. Restart the expired disposable 3-update check; count both attempts. No uncertain replay.',
        'authorization':'User requested continued fixes and completion on 2026-09-12 under the existing additional $20; no main run'})
    print(json.dumps({'status':'prepared','maximumIncludingStorageUSD':maximum,'reusedBaselineCalls':len(cached)}))


def run(a):
    plan=json.loads((a.output/'execution.json').read_text())
    m.require(a.confirm_transfer and plan['version']==VERSION,'Transfer/code version gate')
    m.require(not (a.output/'operations.jsonl').exists(),'Do not replay existing execution')
    m.require(not subprocess.check_output(['git','status','--porcelain'],cwd=m.ROOT,text=True).strip(),'Commit before execution')
    m.require(plan['runtime']==runtime(),'Runtime changed')
    m.require(m.file_hash(a.prepared/'preparation.json')==plan['preparedSHA256'],'Data changed')
    prior,cached=prior_binding([Path(p) for p in plan['priorDirectories']],plan['preparedSHA256'])
    m.require(prior==plan['priorAttempts'],'Prior results changed')
    m.require(m.file_hash(a.output/'reasoning-low-rows.jsonl')==plan['reasoningLowRowsSHA256'],'Low rows changed')
    report,reference,rows=m.load_inputs(a.prepared)
    low={r['exampleID']:r for r in map(json.loads,(a.output/'reasoning-low-rows.jsonl').open())}
    prices=json.loads(subprocess.check_output(['curl','--fail','--silent','--show-error','--max-time','30',m.original.PRICING_URL],text=True))
    for key,spec in report['models'].items():
        price=next(p for p in prices if p['tinker_id']==spec['model'])
        m.require({k:float(price[k].lstrip('$')) for k in ('prefill','sample','train')}==m.prep.PRICES[key],'Prices changed')
    m.original.save(a.output/'pricing.json',{'checkedAt':m.utc(),'source':m.original.PRICING_URL,'models':m.prep.PRICES})
    import tinker
    os.environ['TINKER_API_KEY']=m.original.api_key(a.env_file)
    os.environ.pop('HF_HUB_OFFLINE',None);os.environ.pop('TRANSFORMERS_OFFLINE',None)
    service=tinker.ServiceClient(project_id=m.PROJECT,user_metadata={'purpose':VERSION},max_retries=0)
    proxy=m.original.NoAutomaticResampling(service)
    state={'version':VERSION,'status':'running','startedAt':m.utc(),'sessionID':service.holder.get_session_id(),
           'executionSHA256':m.file_hash(a.output/'execution.json'),'projectID':m.PROJECT,'mainRunStarted':False,
           'implementationCommit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
           'priorReservedTokenCostUSD':prior['reservedTokenCostUSD'],'models':{}}
    journal=m.Journal(a.output,{'executionSHA256':state['executionSHA256']})
    def save():
        state['reservedTokenCostUSD']=sum(r.get('maximumUSD',0) for r in journal.records if r['kind']=='operation_begin')
        state['totalAuthorizationReservedTokenCostUSD']=prior['reservedTokenCostUSD']+state['reservedTokenCostUSD']
        m.original.save(a.output/'run.json',state)
    save()
    try:
        with journal.exclusive():
            for key in ORDER:
                source='qwen38_reasoning' if key=='qwen38_reasoning_low' else key
                spec=report['models'][source];reasoning=source=='qwen38_reasoning'
                tok,renderer=m.local_model(source)
                if reasoning:renderer=m.prep.ExactReasoningRenderer(tok,reasoning_effort='low')
                data={'new':low} if reasoning else rows[key]
                model_state={'model':spec['model'],'status':'tokenizer_preflight','serverModelRevision':'unverified'}
                state['models'][key]=model_state;save()
                base=proxy.create_sampling_client(base_model=spec['model']);remote=base.get_tokenizer()
                m.require(m.fingerprint(remote.get_vocab())==spec['tokenizerVocabularySHA256']==m.fingerprint(tok.get_vocab()),'Vocabulary differs')
                for arm in data.values():
                    for r in arm.values():
                        ids=r['promptTokenIDs']+r.get('completionTokenIDs',[])
                        m.require(remote.decode(ids,clean_up_tokenization_spaces=False)==tok.decode(ids,clean_up_tokenization_spaces=False),'Remote decode differs')
                        if not reasoning:m.native_datum(r,tinker,spec['nativeStopTokenIDs'][0])
                model_state['tokenizerVerified']=True
                contract=copy.deepcopy(spec['trainingContract'] or {})
                contract.update(model=spec['model'],reasoning=reasoning,
                    generation={**spec['generation'],'seed':17,'samplesPerExample':1})
                bridge=DiagnosticBridge(proxy,tinker,tok,renderer,contract)
                calls=RecoveryCalls(journal,key,m.prep.PRICES[source],spec['generation']['maximumTokens'],
                    carried_usd=prior['reservedTokenCostUSD'],cached=cached)
                if not reasoning:
                    model_state['originalChecks']={}
                    for arm in ('old','new'):
                        model_state['status']='original_'+arm;save()
                        model_state['originalChecks'][arm]=phases(bridge,calls,data[arm],reference['arms'][arm],arm);save()
                extra=reference['additionalTests']; scores={}
                model_state['capability']={'status':'sampling','scores':scores};model_state['status']='capability';save()
                for eid in extra['probeIDs']:
                    scores[eid]=[]
                    for seed in extra['generationSeeds']:
                        seeded=DiagnosticBridge(proxy,tinker,tok,renderer,
                            {**contract,'generation':{**contract['generation'],'seed':seed}})
                        value=calls.call(f'capability/{eid}/seed-{seed}','generation',data['new'][eid],
                            lambda eid=eid:seeded.generate(base,data['new'][eid]))
                        scores[eid].append({'seed':seed,**value});save()
                model_state['capability']['status']='complete_pending_intent_review'
                if not reasoning:
                    model_state['status']='overfit';save()
                    def progress(value):
                        journal.append({'kind':'overfit_progress','modelKey':key,'at':m.utc(),**value})
                        model_state['latestOverfitProgress']=value;save()
                    model_state['overfit']=m.original.overfit_phase(m.original.JournaledBridge(bridge),calls,data['new'],extra,progress)
                model_state['status']='complete_pending_review';save()
            state['status']='complete_pending_review'
    except BaseException as e:
        state.update(status='stopped_requires_review',errorType=type(e).__name__)
        raise
    finally:
        state['endedAt']=m.utc();save()
    print(json.dumps({'status':state['status'],'totalAuthorizationReservedTokenCostUSD':state['totalAuthorizationReservedTokenCostUSD']}))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--prepared',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--prior',type=Path,nargs='+');p.add_argument('--env-file',type=Path,default=m.ROOT/'.env')
    p.add_argument('--prepare',action='store_true');p.add_argument('--execute',action='store_true')
    p.add_argument('--confirm-transfer',action='store_true');a=p.parse_args()
    m.require(a.prepare!=a.execute,'Choose prepare or execute')
    prepare(a) if a.prepare else run(a)

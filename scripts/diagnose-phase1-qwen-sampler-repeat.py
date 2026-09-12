#!/usr/bin/env python3
"""Separate fixed-checkpoint scoring variability from repeated exports; no updates."""
import argparse
import importlib.util
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
spec = importlib.util.spec_from_file_location('client_control', Path(__file__).with_name('diagnose-phase1-qwen-client-control.py'))
c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c)
r, m = c.r, c.m
VERSION = 'phase1-qwen-fixed-checkpoint-scoring-v1'
CAP = .20


def runtime():
    value = c.runtime()
    value['filesSHA256'][str(Path(__file__).resolve())] = m.file_hash(Path(__file__))
    return value


def summarize(values):
    return {'pairs': [r.difference(a,b) for a,b in itertools.combinations(values,2)],
        'maximumTargetLogprobDelta': max(r.difference(a,b)['maxTokenLogprobDelta'] for a,b in itertools.combinations(values,2)),
        'distinctFullSequenceLogprobHashes': len({v['fullLogprobsSHA256'] for v in values})}


def phases(bridge, checkpoint, row, op):
    fixed = bridge.sampler(checkpoint['samplerCheckpointPath'])
    first = [op(f'fixed/same-client/{i}', 'nll', lambda: bridge.nll(fixed,row)) for i in range(3)]
    other = [op(f'fixed/new-client/{i}', 'nll',
        lambda: bridge.nll(bridge.sampler(checkpoint['samplerCheckpointPath']),row)) for i in range(3)]
    result = {'fixedCheckpoint': {'sameClient':summarize(first), 'independentClients':summarize(other),
        'allSix':summarize(first+other)}, 'optimizerUpdates':0, 'generations':0}
    if result['fixedCheckpoint']['allSix']['maximumTargetLogprobDelta'] > c.TOLERANCE:
        return {**result,'finding':'same_saved_checkpoint_scoring_varies','exportsSkipped':True,
            'reason':'Scoring variability is already demonstrated without saving or restoring anything.'}
    holder = {}
    def restore():
        holder['trainer'] = bridge.trainer('unchanged-export-control', checkpoint)
        return {'info':m.plain(holder['trainer'].get_info()), 'parent':checkpoint}
    op('export-control/restore', 'admin', restore)
    exports = []
    for i in range(2):
        exports.append(op(f'export-control/save/{i}', 'admin', lambda i=i: m.plain(
            holder['trainer'].save_weights_for_sampler(f'unchanged-export-{i}',ttl_seconds=3600).result())))
    scores = []
    for i, export in enumerate(exports):
        sample = bridge.sampler(export['path'])
        scores.append([op(f'export-control/{i}/score/{j}', 'nll',lambda:bridge.nll(sample,row)) for j in range(2)])
    delta = summarize(scores[0]+scores[1])
    return {**result,'exportsSkipped':False, 'exports':exports, 'repeatedExportScores':delta,
        'withinExport':[summarize(x) for x in scores],
        'finding':'export_or_scoring_difference_after_stable_fixed_probe' if delta['maximumTargetLogprobDelta'] > c.TOLERANCE
            else 'no_material_difference_in_fixed_checkpoint_or_repeated_exports',
        'limitation':'Finite probes; matching results do not prove all exports or scoring calls are deterministic.'}


def prior(source):
    plan = json.loads((source/'plan.json').read_text())
    audit = json.loads((source/'audit.json').read_text())
    m.require(audit['status']=='audit_passed' and audit['pendingOperations']==0,'Control incomplete')
    m.require(c.bind_prior(Path(plan['source'])) == plan['prior'],'Earlier evidence changed')
    files = {str(source/name):digest for name,digest in audit['lineageSHA256'].items()}
    files[str(source/'audit.json')] = m.file_hash(source/'audit.json')
    for path,digest in files.items(): m.require(m.file_hash(path)==digest,'Source changed')
    return {'filesSHA256':files, 'tokenBoundUSD':plan['prior']['tokenBoundUSD']+audit['reservedTokenCostUSD'],
        'storageReserveUSD':plan['prior']['storageReserveUSD']}


def prepare(a):
    m.require(not a.output.exists(),'Fresh output required')
    source = a.source.resolve(); source_plan = json.loads((source/'plan.json').read_text())
    case = next(x for x in source_plan['cases'] if x['modelKey']=='qwen36_hybrid')
    _,_,rows = m.load_inputs(Path(source_plan['preparedDirectory']))
    row = rows['qwen36_hybrid']['old'][case['exampleIDs'][2]]
    records = {v['key']:v['value'] for v in map(json.loads,(source/'operations.jsonl').open()) if v['kind']=='operation_result'}
    cp = records['qwen36_hybrid/post/A/checkpoint']
    cost = 10*m.original.charge('nll',row,case['prices'])
    budget = prior(source)
    m.require(cost <= CAP and budget['tokenBoundUSD']+budget['storageReserveUSD']+cost <= 20.,'Budget exceeded')
    a.output.mkdir(parents=True)
    m.original.save(a.output/'plan.json', {'version':VERSION,'source':str(source),'projectID':m.PROJECT,
        'runtime':runtime(),'implementationCommit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=m.ROOT,text=True).strip(),
        'preparedDirectory':source_plan['preparedDirectory'],'case':case,'checkpoint':cp,'prior':budget,
        'rowSHA256':m.fingerprint(row),'maximumTokenCostUSD':cost,'subBudgetUSD':CAP,
        'maximumIncludingStorageUSD':budget['tokenBoundUSD']+budget['storageReserveUSD']+cost,
        'recipe':'Six identical fixed-checkpoint long-input scores, then only if stable four scores across two unchanged-trainer exports.',
        'failurePolicy':'No automatic replay or fresh training if a checkpoint has expired.',
        'mainRunAuthorized':False})
    print(json.dumps({'status':'prepared','maximumTokenCostUSD':cost,'tokensPerScore':row['promptTokenCount']+row['lossBearingTokenCount']}))


def execute(a):
    plan = json.loads((a.output/'plan.json').read_text())
    m.require(not (a.output/'operations.jsonl').exists(),'Never replay existing execution')
    m.require(plan['version']==VERSION and plan['projectID']==m.PROJECT,'Wrong plan')
    m.require(not subprocess.check_output(['git','status','--porcelain'],cwd=m.ROOT,text=True).strip(),'Commit before execution')
    m.require(subprocess.check_output(['git','rev-parse','HEAD'],cwd=m.ROOT,text=True).strip()==plan['implementationCommit'],'Commit changed')
    m.require(runtime()==plan['runtime'] and prior(Path(plan['source']))==plan['prior'],'Bound inputs changed')
    _,_,rows = m.load_inputs(Path(plan['preparedDirectory']))
    row = rows['qwen36_hybrid']['old'][plan['case']['exampleIDs'][2]]
    m.require(m.fingerprint(row)==plan['rowSHA256'],'Exact long input changed')
    prices = json.loads(subprocess.check_output(['curl','--fail','--silent','--show-error','--max-time','30',m.original.PRICING_URL],text=True))
    price = next(x for x in prices if x['tinker_id']==plan['case']['contract']['model'])
    m.require({k:float(price[k].lstrip('$')) for k in ('train','prefill','sample')}==plan['case']['prices'],'Price changed')
    m.original.save(a.output/'pricing.json',{'at':m.utc(),'source':m.original.PRICING_URL,'prices':plan['case']['prices']})
    import tinker
    os.environ['TINKER_API_KEY']=m.original.api_key(a.env_file)
    service=tinker.ServiceClient(project_id=m.PROJECT,max_retries=0,user_metadata={'purpose':VERSION})
    proxy=m.original.NoAutomaticResampling(service)
    tok,renderer=m.local_model('qwen36_hybrid')
    bridge=r.DiagnosticBridge(proxy,tinker,tok,renderer,plan['case']['contract'])
    m.require(m.fingerprint(bridge.sampler().get_tokenizer().get_vocab())==m.fingerprint(tok.get_vocab())==plan['case']['vocabularySHA256'],'Tokenizer changed')
    journal=m.Journal(a.output,{'planSHA256':m.file_hash(a.output/'plan.json')})
    calls=m.SharedCalls(journal,'scoring',plan['case']['prices'],512,carried_usd=plan['prior']['tokenBoundUSD'])
    state={'status':'running','sessionID':service.holder.get_session_id(),'startedAt':m.utc(),
        'planSHA256':m.file_hash(a.output/'plan.json'),'mainRunStarted':False}
    def save():
        state['reservedTokenCostUSD']=sum(x.get('maximumUSD',0) for x in journal.records if x['kind']=='operation_begin')
        state['combinedIncludingStorageUSD']=plan['prior']['tokenBoundUSD']+plan['prior']['storageReserveUSD']+state['reservedTokenCostUSD']
        m.original.save(a.output/'result.json',state)
    def op(key,kind,fn):
        m.require(kind in ('admin','nll'),'No training or generations authorized')
        spent=sum(x.get('maximumUSD',0) for x in journal.records if x['kind']=='operation_begin')
        m.require(spent+m.original.charge(kind,row,plan['case']['prices']) <= plan['maximumTokenCostUSD']+1e-9,'Sub-budget exceeded')
        value=calls.call(key,kind,row,fn);save();return value
    save()
    try:
        with journal.exclusive():
            state['result']=phases(bridge,plan['checkpoint'],row,op)
            state.update(status='complete',finishedAt=m.utc())
    except BaseException as error:
        state.update(status='stopped_requires_review',errorType=type(error).__name__);raise
    finally:save()
    print(json.dumps(state,indent=2))


def audit(a):
    plan=json.loads((a.output/'plan.json').read_text())
    state=json.loads((a.output/'result.json').read_text())
    m.require(state['status']=='complete' and state['planSHA256']==m.file_hash(a.output/'plan.json'),'Incomplete or changed result')
    m.require(prior(Path(plan['source']))==plan['prior'],'Prior evidence changed')
    records=[json.loads(line) for line in (a.output/'operations.jsonl').open()]
    starts=[x for x in records if x['kind']=='operation_begin']
    results=[x for x in records if x['kind']=='operation_result']
    m.require(len(starts)==len(results) and len({x['key'] for x in starts})==len(starts),'Missing/duplicate dispatch')
    m.require([x['key'] for x in starts]==[x['key'] for x in results],'Result order changed')
    _,_,rows=m.load_inputs(Path(plan['preparedDirectory']))
    row=rows['qwen36_hybrid']['old'][plan['case']['exampleIDs'][2]]
    for begin,end in zip(starts,results):
        m.require(begin['operation']==end['operation'] and begin['operation'] in ('admin','nll'),'Unexpected operation')
        m.require(abs(begin['maximumUSD']-m.original.charge(begin['operation'],row,plan['case']['prices']))<1e-12,'Wrong charge')
    iterator=iter(results)
    class Replay:
        def sampler(self,path):return None
    def op(key,kind,fn):
        saved=next(iterator)
        m.require(saved['key']=='scoring/'+key and saved['operation']==kind,'Schedule changed')
        return saved['value']
    reconstructed=phases(Replay(),plan['checkpoint'],row,op)
    m.require(next(iterator,None) is None and reconstructed==state['result'],'Results changed')
    cost=sum(x['maximumUSD'] for x in starts)
    m.require(abs(cost-state['reservedTokenCostUSD'])<1e-9 and cost<=plan['maximumTokenCostUSD']+1e-9,'Cost changed')
    output={'status':'audit_passed','operations':len(starts),'pending':0,'reservedTokenCostUSD':cost,
        'result':reconstructed,'sourceSHA256':{n:m.file_hash(a.output/n) for n in ('plan.json','result.json','operations.jsonl','pricing.json')}}
    m.original.save(a.output/'audit.json',output)
    print(json.dumps({'status':'audit_passed','operations':len(starts),'finding':reconstructed['finding']}))


def main():
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    mode=p.add_mutually_exclusive_group();mode.add_argument('--execute',action='store_true');mode.add_argument('--audit',action='store_true')
    p.add_argument('--env-file',type=Path,default=m.ROOT/'.env')
    a=p.parse_args();(execute if a.execute else audit if a.audit else prepare)(a)


if __name__=='__main__':main()

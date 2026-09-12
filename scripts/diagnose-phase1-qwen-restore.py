#!/usr/bin/env python3
"""Bounded seeded-restore control; never modifies the main preflight clients."""
import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent))
s=importlib.util.spec_from_file_location('recovery',Path(__file__).with_name('continue-phase1-qwen-preflights.py'))
r=importlib.util.module_from_spec(s);s.loader.exec_module(r)
m=r.m


def main():
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--execute',action='store_true')
    p.add_argument('--env-file',type=Path,default=m.ROOT/'.env');a=p.parse_args()
    if not a.execute:
        m.require(not a.output.exists(),'Fresh diagnostic output required')
        source=json.loads((a.source/'execution.json').read_text())
        evidence={v['key']:v for v in map(json.loads,(a.source/'operations.jsonl').open()) if v['kind']=='operation_result'}
        keys=['qwen36_hybrid/old/'+x for x in ['checkpoint-A','diagnostic-forward/0','continuous-third-NLL','restored-third-NLL']]
        m.require(all(k in evidence for k in keys),'Wait for bound source observations')
        runtime=r.runtime();runtime['filesSHA256'][str(Path(__file__).resolve())]=m.file_hash(Path(__file__))
        m.require(source['maximumIncludingStorageUSD']+.35<=20.,'Combined cap exceeds authorization')
        a.output.mkdir(parents=True)
        m.original.save(a.output/'plan.json',{'purpose':'Separate inherited SDK restoration defaults from full-state restoration',
            'sourceDirectory':str(a.source.resolve()),'sourcePlanSHA256':m.file_hash(a.source/'execution.json'),
            'sourceEvidence':{k:evidence[k] for k in keys},'runtime':runtime,
            'projectID':m.PROJECT,'maximumTokenUSD':.35,
            'sourceMaximumIncludingStorageUSD':source['maximumIncludingStorageUSD'],
            'combinedMaximumIncludingStorageUSD':source['maximumIncludingStorageUSD']+.35,
            'recipe':'Explicit same model/rank/train scopes/seed=17; load_state_with_optimizer; forward and third update; no main run'})
        print(json.dumps({'status':'prepared','maximumAdditionalTokenUSD':.35}));return
    plan=json.loads((a.output/'plan.json').read_text())
    m.require(not (a.output/'operations.jsonl').exists(),'Do not replay diagnostic')
    m.require(not subprocess.check_output(['git','status','--porcelain'],cwd=m.ROOT,text=True).strip(),'Commit before paid operation')
    for path,digest in plan['runtime']['filesSHA256'].items():m.require(m.file_hash(path)==digest,'Runtime changed')
    m.require(m.file_hash(a.source/'execution.json')==plan['sourcePlanSHA256'],'Source plan changed')
    source=json.loads((a.source/'execution.json').read_text())
    evidence={v['key']:v for v in map(json.loads,(a.source/'operations.jsonl').open()) if v['kind']=='operation_result'}
    m.require(all(evidence[k]==v for k,v in plan['sourceEvidence'].items()),'Source observations changed')
    report,reference,rows=m.load_inputs(Path(source['preparedDirectory']))
    spec=report['models']['qwen36_hybrid'];tok,renderer=m.local_model('qwen36_hybrid')
    c=copy.deepcopy(spec['trainingContract']);c['generation']={**spec['generation'],'seed':17}
    import tinker
    os.environ['TINKER_API_KEY']=m.original.api_key(a.env_file)
    svc=tinker.ServiceClient(project_id=m.PROJECT,max_retries=0,user_metadata={'purpose':'seeded-full-optimizer-restore-control'})
    bridge=r.DiagnosticBridge(m.original.NoAutomaticResampling(svc),tinker,tok,renderer,c)
    journal=m.Journal(a.output,{'planSHA256':m.file_hash(a.output/'plan.json')})
    calls=m.SharedCalls(journal,'seeded_restore',m.prep.PRICES['qwen36_hybrid'],512,
                        carried_usd=plan['sourceMaximumIncludingStorageUSD']-.5)
    # The source reserves its FULL remaining cost, not just current usage.
    # This diagnostic is additionally capped at 35 cents before every dispatch.
    ids=reference['arms']['old']['trainIDs'];data=rows['qwen36_hybrid']['old'];holder={}
    def op(key,kind,e,fn):
        spent=sum(x.get('maximumUSD',0) for x in journal.records if x['kind']=='operation_begin')
        estimate=m.original.charge(kind,data[e],m.prep.PRICES['qwen36_hybrid'])
        m.require(spent+estimate<=.35,'Diagnostic sub-budget reached')
        return calls.call(key,kind,data[e],fn)
    state={'status':'running','sessionID':svc.holder.get_session_id(),'planSHA256':m.file_hash(a.output/'plan.json')}
    try:
        with journal.exclusive():
            a_cp=plan['sourceEvidence']['qwen36_hybrid/old/checkpoint-A']['value']
            def create():
                t=bridge.trainer('seeded-control')
                response=t.load_state_with_optimizer(a_cp['optimizerStatePath']).result()
                holder['trainer']=t
                return {'info':m.plain(t.get_info()),'loadResponse':m.plain(response),'seed':17,'parent':a_cp}
            state['restored']=op('create-load','admin',ids[0],create)
            t=holder['trainer']
            f=op('forward-before-update','train',ids[2],lambda:bridge.forward_only(t,data[ids[2]]))
            state['forwardVsContinuous']=r.difference(plan['sourceEvidence']['qwen36_hybrid/old/diagnostic-forward/0']['value'],f)
            state['update']=op('third-update','train',ids[2],lambda:bridge.train(t,data[ids[2]]))
            cp=op('checkpoint','admin',ids[0],lambda:bridge.checkpoint(t,'seeded-third'))
            result=op('after-NLL','nll',ids[0],lambda:bridge.nll(bridge.sampler(cp['samplerCheckpointPath']),data[ids[0]]))
            state['continuationVsContinuous']=r.difference(plan['sourceEvidence']['qwen36_hybrid/old/continuous-third-NLL']['value'],result)
            state['continuationVsDefaultRestore']=r.difference(plan['sourceEvidence']['qwen36_hybrid/old/restored-third-NLL']['value'],result)
            state['status']='complete'
    except BaseException as e:
        state.update(status='stopped_requires_review',errorType=type(e).__name__);raise
    finally:
        state['reservedTokenCostUSD']=sum(x.get('maximumUSD',0) for x in journal.records if x['kind']=='operation_begin')
        m.original.save(a.output/'result.json',state)
    print(json.dumps(state,indent=2))


if __name__=='__main__':main()

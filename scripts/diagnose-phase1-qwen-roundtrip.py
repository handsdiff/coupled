#!/usr/bin/env python3
"""Same-client save/load control: isolate checkpoint round-trip from new clients."""
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
r=importlib.util.module_from_spec(s);s.loader.exec_module(r);m=r.m


def main():
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True)
    p.add_argument('--prior-diagnostic',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--execute',action='store_true');p.add_argument('--env-file',type=Path,default=m.ROOT/'.env');a=p.parse_args()
    if not a.execute:
        m.require(not a.output.exists(),'Fresh directory required')
        source=json.loads((a.source/'execution.json').read_text());prior=json.loads((a.prior_diagnostic/'plan.json').read_text())
        prior_result=json.loads((a.prior_diagnostic/'result.json').read_text())
        m.require(prior_result['status']=='complete','Prior control must finish first')
        maximum=source['maximumIncludingStorageUSD']+prior['maximumTokenUSD']+.35
        m.require(maximum<=20.,'Concurrent maxima exceed shared ceiling')
        runtime=r.runtime();runtime['filesSHA256'][str(Path(__file__).resolve())]=m.file_hash(Path(__file__))
        a.output.mkdir(parents=True)
        m.original.save(a.output/'plan.json',{'version':'qwen-same-client-roundtrip-v1','source':str(a.source.resolve()),
            'sourceSHA256':m.file_hash(a.source/'execution.json'),'runtime':runtime,
            'priorDiagnostic':str(a.prior_diagnostic.resolve()),
            'priorSHA256':{name:m.file_hash(a.prior_diagnostic/name) for name in ('plan.json','result.json','operations.jsonl')},
            'maximumAdditionalTokenUSD':.35,'reservedOtherWorkIncludingStorageUSD':maximum-.35,
            'combinedMaximumIncludingStorageUSD':maximum,'projectID':m.PROJECT,
            'recipe':'Fresh seed-17 adapter, identical first two examples, same-client forward/save/full-optimizer-load/forward; no generation or main run'})
        print(json.dumps({'status':'prepared','combinedMaximumIncludingStorageUSD':maximum}));return
    plan=json.loads((a.output/'plan.json').read_text())
    m.require(not (a.output/'operations.jsonl').exists(),'Do not replay')
    m.require(not subprocess.check_output(['git','status','--porcelain'],cwd=m.ROOT,text=True).strip(),'Commit first')
    for path,digest in plan['runtime']['filesSHA256'].items():m.require(m.file_hash(path)==digest,'Runtime changed')
    m.require(m.file_hash(a.source/'execution.json')==plan['sourceSHA256'],'Source plan changed')
    for name,digest in plan['priorSHA256'].items():m.require(m.file_hash(a.prior_diagnostic/name)==digest,'Prior diagnostic changed')
    source=json.loads((a.source/'execution.json').read_text());report,ref,rows=m.load_inputs(Path(source['preparedDirectory']))
    spec=report['models']['qwen36_hybrid'];tok,renderer=m.local_model('qwen36_hybrid')
    c=copy.deepcopy(spec['trainingContract']);c['generation']={**spec['generation'],'seed':17}
    import tinker
    os.environ['TINKER_API_KEY']=m.original.api_key(a.env_file)
    svc=tinker.ServiceClient(project_id=m.PROJECT,max_retries=0,user_metadata={'purpose':'same-client-full-state-roundtrip'})
    bridge=r.DiagnosticBridge(m.original.NoAutomaticResampling(svc),tinker,tok,renderer,c)
    journal=m.Journal(a.output,{'planSHA256':m.file_hash(a.output/'plan.json')})
    calls=m.SharedCalls(journal,'same_client',m.prep.PRICES['qwen36_hybrid'],512,
                        carried_usd=plan['reservedOtherWorkIncludingStorageUSD']-.5)
    data=rows['qwen36_hybrid']['old'];ids=ref['arms']['old']['trainIDs'];holder={}
    def op(key,kind,e,fn):
        spent=sum(x.get('maximumUSD',0) for x in journal.records if x['kind']=='operation_begin')
        m.require(spent+m.original.charge(kind,data[e],m.prep.PRICES['qwen36_hybrid'])<=.35,'Diagnostic maximum')
        return calls.call(key,kind,data[e],fn)
    state={'status':'running','sessionID':svc.holder.get_session_id(),'planSHA256':m.file_hash(a.output/'plan.json')}
    try:
        with journal.exclusive():
            def create():
                holder['trainer']=bridge.trainer('same-client-control');return m.plain(holder['trainer'].get_info())
            state['trainer']=op('create','admin',ids[0],create);t=holder['trainer']
            for i,e in enumerate(ids[:2]):op('train-'+str(i),'train',e,lambda e=e:bridge.train(t,data[e]))
            before=op('forward-before-save','train',ids[2],lambda:bridge.forward_only(t,data[ids[2]]))
            cp=op('checkpoint','admin',ids[0],lambda:bridge.checkpoint(t,'same-client-parent'))
            state['loadResponse']=op('load-same-client','admin',ids[0],lambda:m.plain(t.load_state_with_optimizer(cp['optimizerStatePath']).result()))
            after=op('forward-after-load','train',ids[2],lambda:bridge.forward_only(t,data[ids[2]]))
            state['sameClientRoundtrip']=r.difference(before,after)
            state['status']='complete'
    except BaseException as e:state.update(status='stopped_requires_review',errorType=type(e).__name__);raise
    finally:
        state['reservedTokenCostUSD']=sum(x.get('maximumUSD',0) for x in journal.records if x['kind']=='operation_begin')
        m.original.save(a.output/'result.json',state)
    print(json.dumps(state,indent=2))


if __name__=='__main__':main()

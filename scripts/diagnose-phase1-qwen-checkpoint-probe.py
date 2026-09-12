#!/usr/bin/env python3
"""Compare inference on the long probe before/after restore, without training."""
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
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--diagnostics',type=Path,nargs=2,required=True);p.add_argument('--execute',action='store_true')
    p.add_argument('--env-file',type=Path,default=m.ROOT/'.env');a=p.parse_args()
    if not a.execute:
        m.require(not a.output.exists(),'Fresh directory required')
        source=json.loads((a.source/'execution.json').read_text())
        prior_cost=0.;bindings={}
        for d in a.diagnostics:
            result=json.loads((d/'result.json').read_text())
            m.require(result['status'] in ('complete','stopped_requires_review'),'Prior control still live')
            prior_cost+=result['reservedTokenCostUSD']
            for name in ('plan.json','result.json','operations.jsonl'):bindings[str((d/name).resolve())]=m.file_hash(d/name)
        max_other=source['maximumIncludingStorageUSD']+prior_cost
        m.require(max_other+.1<=20.,'Combined budget exceeds authorization')
        records={v['key']:v for v in map(json.loads,(a.source/'operations.jsonl').open()) if v['kind']=='operation_result'}
        keys=['qwen36_hybrid/old/checkpoint-A','qwen36_hybrid/old/checkpoint-A-restored']
        runtime=r.runtime();runtime['filesSHA256'][str(Path(__file__).resolve())]=m.file_hash(Path(__file__))
        a.output.mkdir(parents=True)
        m.original.save(a.output/'plan.json',{'runtime':runtime,'projectID':m.PROJECT,'sourceSHA256':m.file_hash(a.source/'execution.json'),
            'sourceEvidence':{k:records[k] for k in keys},'completedDiagnosticBindings':bindings,
            'maximumAdditionalTokenUSD':.1,'reservedOtherWorkIncludingStorageUSD':max_other,
            'combinedMaximumIncludingStorageUSD':max_other+.1,'recipe':'Two long-probe sampler likelihood calls; no new training'})
        print(json.dumps({'status':'prepared','combinedMaximumIncludingStorageUSD':max_other+.1}));return
    plan=json.loads((a.output/'plan.json').read_text())
    m.require(not (a.output/'operations.jsonl').exists(),'Do not replay')
    m.require(not subprocess.check_output(['git','status','--porcelain'],cwd=m.ROOT,text=True).strip(),'Commit first')
    for path,digest in {**plan['runtime']['filesSHA256'],**plan['completedDiagnosticBindings']}.items():m.require(m.file_hash(path)==digest,'Bound evidence/code changed')
    m.require(m.file_hash(a.source/'execution.json')==plan['sourceSHA256'],'Source plan changed')
    source=json.loads((a.source/'execution.json').read_text());report,ref,rows=m.load_inputs(Path(source['preparedDirectory']))
    spec=report['models']['qwen36_hybrid'];tok,renderer=m.local_model('qwen36_hybrid')
    c=copy.deepcopy(spec['trainingContract']);c['generation']={**spec['generation'],'seed':17}
    import tinker
    os.environ['TINKER_API_KEY']=m.original.api_key(a.env_file)
    svc=tinker.ServiceClient(project_id=m.PROJECT,max_retries=0,user_metadata={'purpose':'long-probe-checkpoint-likelihood'})
    bridge=r.DiagnosticBridge(m.original.NoAutomaticResampling(svc),tinker,tok,renderer,c)
    journal=m.Journal(a.output,{'planSHA256':m.file_hash(a.output/'plan.json')})
    calls=m.SharedCalls(journal,'long_probe',m.prep.PRICES['qwen36_hybrid'],512,
                        carried_usd=plan['reservedOtherWorkIncludingStorageUSD']-.5)
    row=rows['qwen36_hybrid']['old'][ref['arms']['old']['trainIDs'][2]]
    m.require(2*m.original.charge('nll',row,m.prep.PRICES['qwen36_hybrid'])<=.1,'Sub-budget')
    state={'status':'running','sessionID':svc.holder.get_session_id(),'planSHA256':m.file_hash(a.output/'plan.json')}
    try:
        with journal.exclusive():
            outputs=[]
            for label,key in [('original','qwen36_hybrid/old/checkpoint-A'),('restored','qwen36_hybrid/old/checkpoint-A-restored')]:
                path=plan['sourceEvidence'][key]['value']['samplerCheckpointPath']
                outputs.append(calls.call(label,'nll',row,lambda path=path:bridge.nll(bridge.sampler(path),row)))
            state['longProbeSamplerDifference']=r.difference(*outputs);state['status']='complete'
    except BaseException as e:state.update(status='stopped_requires_review',errorType=type(e).__name__);raise
    finally:
        state['reservedTokenCostUSD']=sum(x.get('maximumUSD',0) for x in journal.records if x['kind']=='operation_begin')
        m.original.save(a.output/'result.json',state)
    print(json.dumps(state,indent=2))


if __name__=='__main__':main()

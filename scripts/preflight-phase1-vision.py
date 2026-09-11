#!/usr/bin/env python3
"""Two bounded synthetic image requests; never opens personal pilot screenshots."""
import argparse
import base64
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from phase1_read_model_comparison import canonical, file_hash, fingerprint
from phase1_subscription_output import interpret


def runner():
    spec=importlib.util.spec_from_file_location('vision_transport',Path(__file__).with_name('run-phase1-read-model-comparison.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def prepare(folder,project,reuse_single=None):
    r=runner();r.require(not folder.exists(),'Use a fresh canary directory')
    folder.mkdir(parents=True,mode=0o700);images=folder/'images';images.mkdir()
    source=Path(__file__).with_name('create-phase1-vision-canary.m')
    with tempfile.TemporaryDirectory(prefix='coupled-image-canary-') as temporary:
        binary=Path(temporary)/'fixture'
        subprocess.run(['xcrun','clang','-fobjc-arc','-framework','AppKit',str(source),'-o',str(binary)],capture_output=True,check=True)
        subprocess.run([str(binary),str(images)],check=True)
    bodies=[]
    for count in (1,8):
        text='For each image, transcribe its validation code in the lower right. Return only the codes in image order, separated by commas. Do not include labels or explanations.'
        content=[{'type':'input_text','text':text}]
        for index in range(1,count+1):
            content.append({'type':'input_image','detail':'original',
                'image_url':'data:image/png;base64,'+base64.b64encode((images/f'fixture-{index}.png').read_bytes()).decode()})
        body={'model':'chatgpt/gpt-6-astra','input':[{'role':'user','content':content}],
              'reasoning':{'effort':'xhigh'},'tools':[],'stream':True}
        r.atomic_json(folder/f'request-{count}.json',body)
        bodies.append({'imageCount':count,'path':f'request-{count}.json','sha256':file_hash(folder/f'request-{count}.json'),
            'expectedCodes':[f'CEDAR-{4000+i*137:04d}' for i in range(1,count+1)],'patchGrid32px':48*42*count})
    proxy={**r.PROXY,'model_list':[{'model_name':'chatgpt/gpt-6-astra','litellm_params':{'model':'chatgpt/gpt-6-astra'}}]}
    r.atomic_json(folder/'proxy.json',proxy)
    imported=None
    if reuse_single:
        reuse_single=reuse_single.resolve()
        original=r.read_json(reuse_single/'plan.json')
        r.require(original['personalData'] is False,'Only synthetic preflight evidence may be imported')
        r.require(file_hash(reuse_single/'request-1.json')==bodies[0]['sha256'],'Imported synthetic request differs')
        attempt=reuse_single/'attempt-1'
        timing=r.read_json(attempt/'transport.json')
        r.require(timing['bodySHA256']==file_hash(attempt/'response.body'),'Imported response changed')
        parsed=interpret((attempt/'response.body').read_bytes(),'chatgpt/gpt-6-astra')
        r.require(parsed['validCompletion'] and parsed['prediction'].strip()=='CEDAR-4137','Imported image was not verified')
        imported={'path':str(attempt),'artifactSHA256':{name:file_hash(attempt/name) for name in ('response.body','transport.json')},
                  'requestSHA256':bodies[0]['sha256'],'reason':'Reinterpret durable completed stream with the established subscription-output decoder; do not retransmit.'}
    names=['preflight-phase1-vision.py','create-phase1-vision-canary.m','run-phase1-read-model-comparison.py','phase1_read_model_comparison.py','phase1_subscription_output.py']
    code=folder/'code';code.mkdir()
    for name in names:shutil.copy2(Path(__file__).with_name(name),code/name)
    plan={'version':'phase1-synthetic-vision-preflight-v2','project':str(project),'personalData':False,'providerCallsAtPreparation':0,
        'importSingleFrameAttempt':imported,
        'requests':bodies,'model':'chatgpt/gpt-6-astra','imageDetail':'original','requestTimeoutSeconds':240,
        'codeSHA256':{name:file_hash(code/name) for name in names},'runtimeSHA256':r.runtime_hashes(project),
        'proxySHA256':file_hash(folder/'proxy.json'),'apiKeyFallback':False,'automaticRetries':0,
        'measurement':'Single and eight synthetic windows; measures image route and token usage, not personal-task latency.',
        'pricesUSDPerMillion':{'input':10,'cachedInput':1,'output':50}}
    r.atomic_json(folder/'plan.json',plan)
    print(json.dumps({'status':'prepared','planSHA256':file_hash(folder/'plan.json'),'requests':2,'personalData':False,'providerCalls':0}))


def execute(folder,authorization):
    r=runner();plan=r.read_json(folder/'plan.json')
    r.require(file_hash(folder/'plan.json')==authorization,'Authorize the exact synthetic preflight plan')
    r.verify_files(folder/'code',plan['codeSHA256']);r.verify_files(Path('/'),plan['runtimeSHA256'])
    r.require(file_hash(folder/'proxy.json')==plan['proxySHA256'],'Proxy changed')
    # Must execute the isolated snapshot, not a mutable working-tree copy.
    r.require(Path(__file__).resolve()==folder/'code'/Path(__file__).name,'Run the frozen code copy')
    results=[]
    with r.owned_proxy(plan,folder):
        for request in plan['requests']:
            r.require(file_hash(folder/request['path'])==request['sha256'],'Synthetic input changed')
            attempt=folder/f"attempt-{request['imageCount']}"
            if (attempt/'result.json').exists():
                results.append(r.read_json(attempt/'result.json'));continue
            r.require(not attempt.exists(),'Uncertain canary call: inspect evidence, never replay automatically')
            attempt.mkdir()
            imported=plan['importSingleFrameAttempt'] if request['imageCount']==1 else None
            if imported:
                r.verify_files(Path(imported['path']),imported['artifactSHA256'])
                for name in imported['artifactSHA256']:shutil.copy2(Path(imported['path'])/name,attempt/name)
                r.atomic_json(attempt/'import.json',imported)
            else:
                r.atomic_json(attempt/'inflight.json',{'planSHA256':authorization,'request':request,'startedAt':r.now()})
                body=r.read_json(folder/request['path'])
                r.transport(body,attempt,plan['requestTimeoutSeconds'])
            result=r.derive_result({'model':plan['model'],'variant':'synthetic_images','imageCount':request['imageCount']},attempt,{plan['model']:plan['pricesUSDPerMillion']})
            result.update(interpret((attempt/'response.body').read_bytes(),plan['model']))
            result['importedCompletedResponse']=bool(imported)
            result['expectedCodes']=request['expectedCodes'];result['imageReadPassed']=result['validCompletion'] and [x.strip() for x in result['prediction'].split(',')]==request['expectedCodes']
            result['patchGrid32px']=request['patchGrid32px']
            r.atomic_json(attempt/'result.json',result);results.append(result)
            print(json.dumps({'images':result['imageCount'],'imageReadPassed':result['imageReadPassed'],'usage':result['usage'],'latencySeconds':result['timing']['dispatchToCompletionSeconds'],'apiEquivalentCostUSD':result['apiEquivalentCostUSD']}),flush=True)
            r.require(result['imageReadPassed'],'Image canary failed: do not launch personal pilot')
    r.atomic_json(folder/'completion.json',{'status':'passed','personalData':False,'newProviderCalls':sum(not x.get('importedCompletedResponse') for x in results),'measuredResponses':len(results),'results':results,
        'planSHA256':authorization,'limitation':'No personal-context latency measured; image token subtotal may not be exposed. Large-image extrapolation is provisional.'})


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('command',choices=['prepare','run'])
    parser.add_argument('--output',required=True,type=Path);parser.add_argument('--authorize-plan-sha256')
    parser.add_argument('--reuse-single-frame',type=Path)
    args=parser.parse_args();folder=args.output.resolve()
    if args.command=='prepare':prepare(folder,Path.cwd(),args.reuse_single_frame)
    else:execute(folder,args.authorize_plan_sha256)

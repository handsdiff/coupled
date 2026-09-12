#!/usr/bin/env python3
"""Bounded live gate for frozen independent pipeline packs; never launches the run.

Uses the production TinkerBridge without changing data, loss, or optimization.
All dispatched operations are journaled first. An incomplete run is fail-closed;
the explicit branch comparison tests optimizer restoration, not a crash retry.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PREP = ROOT / 'coupled-data/sep02-10-training-prep-20260910'
sys.path.insert(0, str(PREP / 'runtime'))
from phase1_qwen38_execution import (ContractError, Journal, NativeRows, TinkerBridge,
    canonical, datum_from_row, file_hash, fingerprint, plain, require, utc,
    verify_execution_binding)

VERSION = 'phase1-qwen38-live-preflight-v1'
PRICING_URL = 'https://tinker-docs.thinkingmachines.ai/tinker/models.json'
PACKAGES = ('tinker', 'tinker-cookbook', 'transformers', 'tokenizers', 'torch')


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w') as f:
        f.write(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + '\n')
        f.flush(); os.fsync(f.fileno())
    os.replace(temporary, path)


def runtime():
    import tinker
    import tinker_cookbook
    paths = [Path(__file__), ROOT/'scripts/phase1_qwen38_execution.py', PREP/'check-native-format.py']
    for package in (tinker, tinker_cookbook):
        paths.extend(sorted(Path(package.__file__).parent.rglob('*.py')))
    return {'packages': {p: importlib.metadata.version(p) for p in PACKAGES},
            'filesSHA256': {str(p.resolve()): file_hash(p) for p in paths}}


def local_renderer():
    from transformers import AutoTokenizer
    spec = importlib.util.spec_from_file_location('qwen38_exact', PREP/'check-native-format.py')
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    tok = AutoTokenizer.from_pretrained(PREP/'tokenizer', local_files_only=True, trust_remote_code=False)
    return tok, mod.ExactContentRenderer(tok)


def charge(kind, row, prices):
    if kind == 'generation':
        return (row['promptTokenCount']*prices['prefill'] + 512*prices['sample'])/1e6
    if kind == 'nll':
        return ((row['promptTokenCount']+row['lossBearingTokenCount'])*prices['prefill']+prices['sample'])/1e6
    if kind == 'train': return row['trainingDatumPositions']*prices['train']/1e6
    return 0.


def selection(pack):
    cohort = [json.loads(l) for l in (pack/'cohort.jsonl').open()]
    rows = NativeRows(pack/'native-rows.jsonl')
    warmup = cohort[:50]
    # Three substantive training rows, one per major destination, all from B1.
    train = [next(e for e in warmup if e['modelFacingDestination']['application']==app
                  and len(e['targetText']) >= threshold)
             for app,threshold in [('Visual Studio Code',100),('Obsidian',150),('ChatGPT',300)]]
    train.sort(key=lambda e:e['targetBeganAt'])
    chrome = next(e for e in cohort if e['modelFacingDestination']['application'] in ('Chrome','Google Chrome'))
    paste = next(e for e in cohort if '<|paste|>' in e['targetText'])
    probes = train+[paste,chrome]
    require(len({e['exampleID'] for e in probes}) == 5, 'Probe coverage is not distinct')
    require(max(e['targetAvailableAt'] for e in train)<min(e['targetBeganAt'] for e in probes[3:]),
            'Diagnostic future probes overlap training')
    return {'trainIDs':[e['exampleID'] for e in train], 'probeIDs':[e['exampleID'] for e in probes],
        'cases':[{'exampleID':e['exampleID'],'application':e['modelFacingDestination']['application'],
                  'targetText':e['targetText'],'promptTokenCount':rows[e['exampleID']]['promptTokenCount'],
                  'fullSequenceTokenSHA256':rows[e['exampleID']]['fullSequenceTokenSHA256']}
                 for e in probes]}, rows


def prepare(args):
    plan = json.loads(args.execution_plan.read_text()); verify_execution_binding(plan)
    require(not args.output.exists(), 'Use a new preflight directory')
    arms={};prices=plan['pricesPerMillionTokens'];total=0.
    for arm in ('old','new'):
        arms[arm],rows=selection(Path(plan['arms'][arm]['packDirectory']))
        tr=arms[arm]['trainIDs'];probes=arms[arm]['probeIDs']
        # 5 base + 5 final generations/NLL; four anchor NLL comparisons;
        # first two updates, then the third once on each fork. No other updates.
        cost=sum(2*(charge('generation',rows[e],prices)+charge('nll',rows[e],prices)) for e in probes)
        cost+=4*charge('nll',rows[tr[0]],prices)
        cost+=sum(charge('train',rows[e],prices) for e in tr)+charge('train',rows[tr[2]],prices)
        arms[arm]['maximumTokenCostUSD']=cost;total+=cost
    result={'version':VERSION,'executionPlan':str(args.execution_plan.resolve()),'planSHA256':fingerprint(plan),
        'runtime':runtime(),'arms':arms,'pricesPerMillionTokens':prices,'maximumTokenCostUSD':total,
        'storageReserveUSD':.25,'hardCeilingUSD':5.,'projectID':args.project_id,
        'checkpointTTLSeconds':3600,'maximumRepeatedBranchLogprobDifference':.005,
        'sourceOfProjectPrivacy':'Previously user-confirmed dedicated private project; SDK does not expose grants',
        'purpose':'Live format, masked updates, finite NLL/BPB, optimizer restoration, latency and cost—not efficacy',
        'maximumOperations':{'generation':20,'nll':28,'train':8},
        'retryPolicy':'No automatic full sampling retries, no automatic replay of uncertain paid operations',
        'mainExperimentAuthorized':False,'preparedAt':utc()}
    require(total+.25 <= 5.,'Preflight exceeds its bounded ceiling')
    args.output.mkdir(parents=True)
    save(args.output/'preparation.json',result)
    print(json.dumps({'status':'prepared','maximumTokenCostUSD':total,'hardCeilingUSD':5.,
                      'uniqueExampleOccurrences':10,'providerCalls':0},indent=2))


class NoAutomaticResampling:
    """Disable SDK retries that would create a new billable sample request."""
    def __init__(self, service): self.service=service
    def __getattr__(self,name): return getattr(self.service,name)
    def create_sampling_client(self,**kwargs):
        from tinker.lib.retry_handler import RetryConfig
        return self.service.create_sampling_client(**kwargs,retry_config=RetryConfig(enable_retry_logic=False))


class Calls:
    def __init__(self,journal,prices,ceiling,storage=.25):
        self.journal,self.prices,self.ceiling,self.storage=journal,prices,ceiling,storage
    def call(self,key,kind,row,fn):
        require(not any(r.get('key')==key for r in self.journal.records),'Preflight operation already attempted')
        maximum=charge(kind,row,self.prices)
        spent=sum(r.get('maximumUSD',0.) for r in self.journal.records if r['kind']=='operation_begin')
        require(spent+maximum+self.storage<=self.ceiling,'Preflight budget exhausted before dispatch')
        self.journal.append({'kind':'operation_begin','key':key,'operation':kind,
            'exampleID':row.get('exampleID'),'maximumUSD':maximum,'at':utc()})
        start=time.monotonic();value=fn()
        self.journal.append({'kind':'operation_result','key':key,'operation':kind,
            'value':value,'wallSeconds':time.monotonic()-start,'at':utc()})
        print(json.dumps({'completed':key,'seconds':round(time.monotonic()-start,2),
                          'reservedTokenCostUSD':round(spent+maximum,4)}),flush=True)
        return value


def compare_probabilities(left,right,tolerance):
    a,b=left['targetLogprobs'],right['targetLogprobs']
    require(len(a)==len(b),'Restored likelihood shape differs')
    delta=max(abs(x-y) for x,y in zip(a,b))
    require(delta<=tolerance,'Optimizer/weight restoration diverged: '+str(delta))
    return delta


def phases(bridge,calls,rows,spec,arm):
    probes=spec['probeIDs'];train=spec['trainIDs'];base=bridge.sampler();scores={}
    def op(label,kind,e,fn):return calls.call(arm+'/'+label,kind,rows[e],fn)
    for e in probes:
        scores[e]={kind:op('base/'+e+'/'+kind,kind,e,lambda k=kind,e=e:
                         (bridge.generate if k=='generation' else bridge.nll)(base,rows[e]))
                   for kind in ('generation','nll')}
    trainer=op('fresh_adapter','admin',train[0],lambda:bridge.trainer(arm))
    # Live objects cannot be journaled; supplied wrappers store only metadata.
    for i,e in enumerate(train[:2]):op('train/'+str(i),'train',e,lambda e=e:bridge.train(trainer,rows[e]))
    first=op('checkpoint-A','admin',train[0],lambda:bridge.checkpoint(trainer,'preflight-'+arm+'-A'))
    anchor=train[0];sa=bridge.sampler(first['samplerCheckpointPath'])
    before=op('before-restore','nll',anchor,lambda:bridge.nll(sa,rows[anchor]))
    restored=op('restore-optimizer','admin',anchor,lambda:bridge.trainer(arm,first))
    copy=op('checkpoint-A-restored','admin',anchor,lambda:bridge.checkpoint(restored,'preflight-'+arm+'-A-restored'))
    sr=bridge.sampler(copy['samplerCheckpointPath'])
    after=op('after-restore','nll',anchor,lambda:bridge.nll(sr,rows[anchor]))
    weight_delta=compare_probabilities(before,after,.005)
    e=train[2]
    for label,client in [('continuous',trainer),('restored',restored)]:
        op('train-third-'+label,'train',e,lambda client=client:bridge.train(client,rows[e]))
    sb=op('checkpoint-B','admin',e,lambda:bridge.checkpoint(trainer,'preflight-'+arm+'-B'))
    sc=op('checkpoint-B-restored','admin',e,lambda:bridge.checkpoint(restored,'preflight-'+arm+'-B-restored'))
    original=bridge.sampler(sb['samplerCheckpointPath']); resumed=bridge.sampler(sc['samplerCheckpointPath'])
    l=op('continuous-third-NLL','nll',anchor,lambda:bridge.nll(original,rows[anchor]))
    r=op('restored-third-NLL','nll',anchor,lambda:bridge.nll(resumed,rows[anchor]))
    optimizer_delta=compare_probabilities(l,r,.005)
    for e in probes:
        scores[e]['after']={kind:op('after/'+e+'/'+kind,kind,e,lambda k=kind,e=e:
                                  (bridge.generate if k=='generation' else bridge.nll)(resumed,rows[e]))
                           for kind in ('generation','nll')}
    return {'scores':scores,'weightRestoreMaxLogprobDelta':weight_delta,
            'optimizerContinuationMaxLogprobDelta':optimizer_delta,'trainedExamples':3,
            'optimizerStepsIncludingRestorationFork':4,'checkpoint':sc}


class JournaledBridge:
    """Persist creation metadata rather than SDK live objects in the operation log."""
    def __init__(self,bridge): self.bridge=bridge;self.clients={};self.next_client=0
    def __getattr__(self,name):return getattr(self.bridge,name)
    def trainer(self,arm,parent=None):
        client=self.bridge.trainer(arm,parent);key=str(self.next_client);self.next_client+=1
        self.clients[key]=client
        return {'clientHandle':key,'info':plain(client.get_info()),'parent':parent}
    def train(self,handle,row):return self.bridge.train(self.clients[handle['clientHandle']],row)
    def checkpoint(self,handle,name):return self.bridge.checkpoint(self.clients[handle['clientHandle']],name)


def run(args):
    preparation=json.loads((args.output/'preparation.json').read_text())
    require(preparation['version']==VERSION and preparation['projectID']==args.project_id,'Wrong preflight binding')
    require(not (args.output/'operations.jsonl').exists(),'Prior attempt exists; review instead of replaying spend')
    require(args.confirm_transfer and args.execute,'Explicit bounded execution/transfer required')
    require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip(),'Commit before paid execution')
    require(preparation['runtime']==runtime(),'Preflight implementation/dependencies changed')
    plan=json.loads(Path(preparation['executionPlan']).read_text());verify_execution_binding(plan)
    require(fingerprint(plan)==preparation['planSHA256'],'Frozen plan differs')
    # Public machine-readable price check before constructing an authenticated client.
    with urllib.request.urlopen(PRICING_URL,timeout=30) as response: prices_doc=json.load(response)
    current=next(v for v in prices_doc if v['tinker_id']==plan['contract']['model'])
    prices={k:float(current[k].lstrip('$')) for k in ('train','prefill','sample')}
    require(prices==preparation['pricesPerMillionTokens'],'Prices changed; regenerate bounded approval')
    save(args.output/'pricing.json',{'checkedAt':utc(),'source':PRICING_URL,'model':current})
    import tinker
    tok,renderer=local_renderer()
    rows={arm:NativeRows(Path(plan['arms'][arm]['packDirectory'])/'native-rows.jsonl') for arm in ('old','new')}
    for arm in rows:
        for case in preparation['arms'][arm]['cases']:
            row=rows[arm][case['exampleID']];datum_from_row(row,tinker)
            require(row['fullSequenceTokenSHA256']==case['fullSequenceTokenSHA256'],'Probe changed')
    # Read only the explicitly scoped credential; never echo or put it in artifacts.
    for line in args.env_file.read_text().splitlines():
        text=line.strip().removeprefix('export ')
        if text.startswith('TINKER_API_KEY='):
            os.environ['TINKER_API_KEY']=text.split('=',1)[1].strip().strip('\"\'');break
    require(bool(os.environ.get('TINKER_API_KEY')),'Missing Tinker key')
    metadata={'purpose':'phase1-qwen38-bounded-preflight','plan':preparation['planSHA256']}
    service=tinker.ServiceClient(project_id=args.project_id,user_metadata=metadata,max_retries=0)
    capability=next((v for v in service.get_server_capabilities().supported_models if v.model_name==plan['contract']['model']),None)
    require(capability and capability.max_context_length>=65536,'Model unavailable or context changed')
    session_id=service.holder.get_session_id()
    manifest={'version':VERSION,'status':'running','startedAt':utc(),'planSHA256':fingerprint(plan),
        'implementationCommit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'preparationSHA256':file_hash(args.output/'preparation.json'),'sessionID':session_id,
        'projectID':args.project_id,'model':plain(capability),'modelRevision':'unverified: provider exposes model name only',
        'maximumUSD':5.,'mainRunStarted':False,'arms':{}}
    save(args.output/'preflight.json',manifest)
    contract={**plan['contract'],'checkpointTTLSeconds':preparation['checkpointTTLSeconds']}
    service_proxy=NoAutomaticResampling(service)
    raw_bridge=TinkerBridge(service_proxy,tinker,tok,renderer,contract)
    # The SDK loads HF metadata for its served model; compare complete ID maps,
    # then native tokenization/decoding for every actually transmitted probe.
    remote=raw_bridge.sampler().get_tokenizer()
    require(remote.get_vocab()==tok.get_vocab(),'Served-model tokenizer vocabulary differs')
    for arm in rows:
        for e in preparation['arms'][arm]['probeIDs']:
            row=rows[arm][e]
            for field in ('promptTokenIDs','completionTokenIDs'):
                require(remote.decode(row[field],clean_up_tokenization_spaces=False)==tok.decode(row[field],clean_up_tokenization_spaces=False),'Tokenizer decode differs')
    manifest['tokenizer']={'fullVocabularySHA256':fingerprint(tok.get_vocab()),'pasteIDs':tok.encode('<|paste|>',add_special_tokens=False),
        'nativeTerminatorID':248046,'scope':'SDK tokenizer for served model; complete vocabulary and probe decodes match'}
    save(args.output/'preflight.json',manifest)
    journal=Journal(args.output,{'preparationSHA256':manifest['preparationSHA256'],'commit':manifest['implementationCommit']})
    calls=Calls(journal,prices,5.)
    try:
        with journal.exclusive():
            for arm in ('old','new'):
                manifest['arms'][arm]=phases(JournaledBridge(raw_bridge),calls,rows[arm],preparation['arms'][arm],arm)
                save(args.output/'preflight.json',manifest)
        values=[r['value'] for r in journal.records if r['kind']=='operation_result' and r['operation']=='generation']
        manifest['generationSummary']={'count':len(values),'empty':sum(not v['prediction'].strip() for v in values),
            'lengthStopped':sum(v['stopReason']=='length' for v in values),
            'medianLatencySeconds':statistics.median(v['latencySeconds'] for v in values),
            'meanLatencySeconds':statistics.mean(v['latencySeconds'] for v in values)}
        manifest['status']='mechanical_checks_passed_pending_output_review'
    except BaseException as error:
        manifest['status']='stopped_requires_review';manifest['errorType']=type(error).__name__
        # Do not log provider exception bodies: those may contain personal requests.
        save(args.output/'preflight.json',manifest);raise
    finally:
        manifest['endedAt']=utc();manifest['reservedTokenCostUSD']=sum(r.get('maximumUSD',0.) for r in journal.records if r['kind']=='operation_begin')
        save(args.output/'preflight.json',manifest)
    # Billing can lag hours. Save only this session, not other organization usage.
    rest=service.create_rest_client()
    start=dt.datetime.fromisoformat(manifest['startedAt']).replace(minute=0,second=0,microsecond=0)
    end=dt.datetime.now(dt.timezone.utc).replace(minute=0,second=0,microsecond=0)+dt.timedelta(hours=1)
    try:
        billing=plain(rest.get_billing_usage(start,end).result())
        save(args.output/'billing.json',{'retrievedAt':utc(),'status':'may_lag_hours','sessionID':session_id,
             'data':{k:[r for r in v if r.get('session_id')==session_id] for k,v in billing.items() if isinstance(v,list)}})
    except Exception as error:save(args.output/'billing.json',{'status':'unavailable','errorType':type(error).__name__})
    print(json.dumps({'status':manifest['status'],'reservedTokenCostUSD':manifest['reservedTokenCostUSD'],
                      'generationSummary':manifest.get('generationSummary'),'mainRunStarted':False},indent=2))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--execution-plan',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--project-id',required=True);p.add_argument('--env-file',type=Path,default=ROOT/'.env')
    p.add_argument('--prepare',action='store_true');p.add_argument('--execute',action='store_true')
    p.add_argument('--confirm-transfer',action='store_true');a=p.parse_args()
    require(a.prepare != a.execute,'Choose prepare or execute')
    prepare(a) if a.prepare else run(a)


if __name__=='__main__':main()

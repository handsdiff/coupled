#!/usr/bin/env python3
"""Supervise an authorized frozen subscription run, then audit and combine.

The frozen runner never retries. A separately authorized supervision policy
may retry one explicit, output-free 5xx error per request; a second pauses.
Uncertain dispatch, auth/quota errors and provider fallback remain prohibited.
"""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time


def sha(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()


def load(p):return json.loads(p.read_text())


def stamp():return datetime.now(timezone.utc).isoformat()


def save(p,v):
    temp=p.with_suffix('.pending')
    with temp.open('w') as f:
        os.chmod(temp,0o600);json.dump(v,f,indent=2,sort_keys=True);f.write('\n');f.flush();os.fsync(f.fileno())
    os.replace(temp,p)
    fd=os.open(p.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)


def server_error(attempt):
    """Only positive evidence of an output-free server failure permits retry."""
    timing_file=attempt/'transport.json';wire_file=attempt/'response.body'
    if not timing_file.exists() or not wire_file.exists():return None
    timing=load(timing_file);wire=wire_file.read_bytes()
    assert hashlib.sha256(wire).hexdigest()==timing['bodySHA256']
    records=[]
    for line in wire.decode('utf-8',errors='strict').splitlines():
        if line.startswith('data: '):
            if line[6:].strip()=='[DONE]':continue
            try:records.append(json.loads(line[6:]))
            except json.JSONDecodeError:return None
    if not records:
        try:records=[json.loads(wire)]
        except (json.JSONDecodeError,UnicodeDecodeError):return None
    errors=[]
    for r in records:
        if not isinstance(r,dict):return None
        kind=r.get('type') or ''
        # Never resample a completion, partial generated text or reasoning.
        if kind.startswith(('response.output_','response.content_part','response.reasoning')):return None
        if kind in ('response.completed','response.incomplete'):return None
        response=r.get('response') or {}
        if not isinstance(response,dict):return None
        if response.get('output'):return None
        if r.get('error'):errors.append(r['error'])
        if response.get('error'):errors.append(response['error'])
    for e in errors:
        if not isinstance(e,dict):continue
        try:code=int(e.get('code') or timing['httpStatus'])
        except (ValueError,TypeError):continue
        if 500<=code<=599:
            return {'code':code,'message':e.get('message'),'transport':timing,
                    'usage':'not returned; do not assume zero cost'}
    return None


def verify_retry_archives(root,plan):
    """Recover the local archive/progress transaction, never a provider call."""
    for decision in sorted((root/'retry-attempts').glob('*/decision.json')):
        r=load(decision);archive=decision.parent/'failed-attempt-1'
        assert r['originalBinding']['executorPlanSHA256']==plan['executorPlanSHA256']
        if not archive.exists():continue
        for name,digest in r['sourceSHA256'].items():assert sha(archive/name)==digest
        current=root/'results'/f"{r['requestOrdinal']:04d}"
        if not current.exists():
            saved=sorted((root/'results').glob('[0-9][0-9][0-9][0-9]'))
            assert len(saved)==r['requestOrdinal']
            progress=load(root/'results/progress.json')
            assert progress['completed'] in (r['requestOrdinal'],r['requestOrdinal']+1)
            progress.update(completed=r['requestOrdinal'],lastRequestOrdinal=r['requestOrdinal']-1)
            save(root/'results/progress.json',progress)


def wait_for_proxy_port(root,state,probe=None,timeout=180):
    """Wait out a stopped proxy's socket lifetime; never kill another listener."""
    def bindable():
        try:
            with socket.socket() as sock:sock.bind(('127.0.0.1',4000))
            return True
        except OSError:return False
    probe=probe or bindable
    deadline=time.monotonic()+timeout
    while not probe():
        if time.monotonic()>=deadline:
            raise RuntimeError('Proxy port 4000 remained unavailable; no provider call or listener replacement')
        state.update(status='retry_wait',retryDisposition='waiting for proxy port release; no provider call',checkedAt=stamp())
        save(root/'supervisor.json',state)
        time.sleep(10)


def retry_server_failure(root,plan,state):
    policy=plan.get('serverErrorRetryPolicy')
    if not policy:return False
    assert policy['pauseAfterConsecutiveServerErrors']==2 and policy['maxRetriesPerRequest']==1
    saved=sorted((root/'results').glob('[0-9][0-9][0-9][0-9]'))
    if not saved:return False
    attempt=saved[-1];ordinal=int(attempt.name)
    if (attempt/'result.json').exists():
        result=load(attempt/'result.json')
        if not result.get('providerError') and result.get('responseStatus')!='failed':return False
    error=server_error(attempt)
    if error is None:return False
    marker=load(attempt/'inflight.json')
    assert marker['binding']['executorPlanSHA256']==plan['executorPlanSHA256']
    assert marker['binding']['request']['requestOrdinal']==ordinal
    retry_dir=root/'retry-attempts'/attempt.name
    decision=retry_dir/'decision.json';archive=retry_dir/'failed-attempt-1'
    if decision.exists():
        prior=load(decision)
        assert prior['sourceSHA256']
        if archive.exists():
            state['consecutiveServerErrors']=2
            state['retryDisposition']='paused after second consecutive explicit server error'
            return False
        # Complete an interrupted archive operation, without granting a second retry.
        assert {p.name:sha(p) for p in attempt.iterdir() if p.is_file()}==prior['sourceSHA256']
    else:
        retry_dir.mkdir(parents=True,mode=0o700)
        save(decision,{'at':stamp(),'authorizationPlanSHA256':state['supervisionPlanSHA256'],
            'requestOrdinal':ordinal,'originalBinding':marker['binding'],'error':error,
            'sourceSHA256':{p.name:sha(p) for p in attempt.iterdir() if p.is_file()},
            'priorProgress':load(root/'results/progress.json'),
            'disposition':'one authorized retry after explicit output-free server error; preserve failed attempt'})
    os.rename(attempt,archive)
    for name,digest in load(decision)['sourceSHA256'].items():assert sha(archive/name)==digest
    progress=load(root/'results/progress.json')
    assert progress['completed'] in (ordinal,ordinal+1)
    progress.update(completed=ordinal,lastRequestOrdinal=ordinal-1)
    save(root/'results/progress.json',progress)
    state.update(status='retry_wait',retryRequestOrdinal=ordinal,consecutiveServerErrors=1,
        authorizedServerRetries=len(list((root/'retry-attempts').glob('*/decision.json'))))
    save(root/'supervisor.json',state)
    time.sleep(policy['delaySeconds'])
    return True


def memory(pid):
    table={}
    for line in subprocess.check_output(['ps','-axo','pid=,ppid=,rss='],text=True).splitlines():
        child,parent,rss=map(int,line.split());table[child]=(parent,rss)
    descendants={pid}
    while True:
        more=descendants|{p for p,(parent,_) in table.items() if parent in descendants}
        if more==descendants:break
        descendants=more
    return sum(table.get(p,(0,0))[1] for p in descendants)/1024


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--directory',required=True,type=Path)
    p.add_argument('--authorize-plan-sha256',required=True)
    p.add_argument('--supervision-plan',default='supervision-plan.json')
    p.add_argument('--authorization',default='authorization.json')
    a=p.parse_args();root=a.directory.resolve()
    supervision=load(root/a.supervision_plan)
    assert sha(root/a.supervision_plan)==a.authorize_plan_sha256
    for name,digest in supervision['codeSHA256'].items():assert sha(Path(name))==digest
    for name,digest in supervision['gateSHA256'].items():assert sha(Path(name))==digest
    gate=load(Path(supervision['independentAudit']))
    assert gate['status']=='passed' and gate['allPromptsRepackedIdentically']
    assert gate['plannedRequests']==supervision['totalPredictions']
    assert sha(root/'executor-plan.json')==supervision['executorPlanSHA256']
    authorization=load(root/a.authorization)
    assert authorization['supervisionPlanSHA256']==a.authorize_plan_sha256
    assert authorization['newRequestsAuthorized']==supervision['newPredictions']
    assert authorization.get('serverErrorRetryPolicy')==supervision.get('serverErrorRetryPolicy')
    # A previous live supervisor must not be duplicated.
    import fcntl
    lock=(root/'.supervisor.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    sys.path.insert(0,str(root/'postprocess'))
    from phase1_subscription_output import interpret
    runner=root/'code/run-phase1-read-model-comparison.py'
    state={'status':'starting','startedAt':stamp(),'supervisorPID':os.getpid(),
        'plannedNewRequests':supervision['newPredictions'],'reusedRequests':supervision['reusedPredictions'],
        'supervisionPlanSHA256':a.authorize_plan_sha256,'executorPlanSHA256':supervision['executorPlanSHA256'],
        'maxProcessTreeMiB':4096,'peakObservedProcessTreeMiB':0,'executorAutomaticRetries':0,
        'authorizedServerRetries':len(list((root/'retry-attempts').glob('*/decision.json'))),
        'serverErrorRetryPolicy':supervision.get('serverErrorRetryPolicy')}
    tag=time.time_ns();child=None;checked=set()
    def run_stage(name,command):
        nonlocal child
        with (root/f'{name}-{tag}.log').open('x') as log,(root/f'{name}-resources-{tag}.jsonl').open('x') as metrics:
            os.chmod(log.name,0o600);os.chmod(metrics.name,0o600)
            child=subprocess.Popen(command,cwd=root,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
            state.update(status='running',stage=name,runnerPID=child.pid);save(root/'supervisor.json',state)
            while True:
                finished=child.poll() is not None
                rss=memory(child.pid)
                state['peakObservedProcessTreeMiB']=max(state['peakObservedProcessTreeMiB'],rss)
                progress=root/'results/progress.json'
                if progress.exists():state['completedNewRequests']=load(progress)['completed']
                if state.get('completedNewRequests',0)>state.get('retryRequestOrdinal',10**9):
                    state['consecutiveServerErrors']=0
                for result_file in sorted((root/'results').glob('[0-9]*/result.json')):
                    if result_file.parent.name in checked:continue
                    result=load(result_file);wire=(result_file.parent/'response.body').read_bytes()
                    assert hashlib.sha256(wire).hexdigest()==result['timing']['bodySHA256']
                    interpret(wire,result['model']);checked.add(result_file.parent.name)
                state.update(checkedAt=stamp(),interpretedResponsesChecked=len(checked))
                metrics.write(json.dumps({'at':state['checkedAt'],'processTreeMiB':rss,'completed':state.get('completedNewRequests',0)})+'\n');metrics.flush()
                save(root/'supervisor.json',state)
                if rss>4096:raise RuntimeError('4-GiB process-tree memory guard reached')
                if finished:break
                time.sleep(10)
            if child.returncode:raise RuntimeError(f'{name} paused/failed; inspect retained log; no request replay')
        child=None
        return root/f'{name}-{tag}.log'
    try:
        while True:
            # Inspect a prior interruption before starting any provider process.
            verify_retry_archives(root,supervision)
            retry_server_failure(root,supervision,state)
            if state.get('consecutiveServerErrors')==2:
                raise RuntimeError('Two consecutive explicit server errors; manual review required')
            wait_for_proxy_port(root,state)
            try:
                tag=time.time_ns()
                run_stage('run',[sys.executable,str(runner),'run','--directory',str(root),'--authorize-plan-sha256',supervision['executorPlanSHA256']])
                break
            except RuntimeError as error:
                if not str(error).startswith('run paused/failed'):raise
                if state.get('consecutiveServerErrors')==2 or not retry_server_failure(root,supervision,state):raise
                child=None
        audit_log=run_stage('audit',[sys.executable,str(runner),'audit','--directory',str(root)])
        audit=load(audit_log);assert audit['status']=='complete' and audit['recorded']==supervision['newPredictions']
        save(root/'final-audit.json',audit)
        if not (root/'final-output-v2').exists():
            run_stage('projection',[sys.executable,str(root/'postprocess/project-phase1-read-model-results.py'),'--execution',str(root),'--output',str(root/'final-output-v2')])
        projection=load(root/'final-output-v2/projection.json')
        assert projection['recorded']==supervision['newPredictions']
        combined=Path(supervision['combinedOutput'])
        if not combined.exists():
            run_stage('combine',[sys.executable,str(root/'postprocess/extend-phase1-read-model-comparison.py'),'combine','--expansion',supervision['expansionDirectory'],'--execution',str(root),'--output',str(combined)])
        report=load(combined/'projection.json')
        assert report['recorded']==supervision['totalPredictions']
        assert sha(combined/'predictions.jsonl')==report['predictionsSHA256']
        retry_files={str(p.resolve()):sha(p) for p in sorted((root/'retry-attempts').rglob('*')) if p.is_file()}
        save(combined/'retry-evidence.json',{'version':'phase1-explicit-server-retries-v1',
            'supervisionPlanSHA256':a.authorize_plan_sha256,'sourceSHA256':retry_files,
            'retriedRequests':len(list((root/'retry-attempts').glob('*/decision.json'))),
            'policy':supervision.get('serverErrorRetryPolicy'),
            'accounting':'Prediction latency/cost describes successful attempts; separately include these failed attempts. Missing provider usage is unknown, not zero.'})
        state.update(status='complete',completedNewRequests=supervision['newPredictions'],combinedPredictions=report['recorded'],finalAudit='passed',holisticScoring='pending blinded review; not fabricated by supervisor')
    except BaseException as error:
        if child is not None and child.poll() is None:
            os.killpg(child.pid,signal.SIGTERM)
            try:child.wait(timeout=10)
            except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait()
        state.update(status='paused_needs_review',failure=str(error))
    finally:
        state['finishedAt']=stamp();save(root/'supervisor.json',state)
        save(root/f'supervisor-attempt-{tag}.json',state)


if __name__=='__main__':main()

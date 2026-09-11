#!/usr/bin/env python3
"""Local process watcher: estimate once early, then hand off on completion.

Periodic checks read local files only. They do not invoke an LLM or provider.
After a successful run, prepare blinded review and resume the authorized thread
once to grade/analyze. A busy writer lock may be retried without a model call.
"""
import argparse
from collections import Counter,defaultdict
from datetime import datetime,timedelta,timezone
import fcntl
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time


def load(p):return json.loads(p.read_text())
def save(p,value):
    temp=p.with_suffix('.pending')
    with temp.open('w') as f:os.chmod(temp,0o600);json.dump(value,f,indent=2,sort_keys=True);f.write('\n');f.flush();os.fsync(f.fileno())
    os.replace(temp,p)
def now():return datetime.now(timezone.utc)


def estimate(requests,results,at):
    samples=defaultdict(list)
    for r in results:
        if not r.get('reusedFrom'):samples[r['variant']].append(r['timing']['dispatchToCompletionSeconds'])
    counts=Counter(r['variant'] for r in requests if not r.get('reuse'))
    if not samples or any(len(samples[v])<3 for v in counts):return None
    remaining=sum((n-len(samples[v]))*statistics.mean(samples[v]) for v,n in counts.items())*1.08+60
    return {'estimatedAt':at.isoformat(),'completed':len(results),'planned':len(requests),
        'meansSeconds':{v:statistics.mean(x) for v,x in samples.items()},
        'sampleCounts':{v:len(x) for v,x in samples.items()},'remainingSeconds':remaining,
        'expectedCompletionAt':(at+timedelta(seconds=remaining)).isoformat(),
        'conservativeCheckAt':(at+timedelta(seconds=remaining*1.25)).isoformat(),
        'method':'Per-arm early mean transport latency × remaining requests, plus 8% local overhead and 60 seconds audit. Not a guarantee.',
        'monitoring':'Local process wait/file checks only; no LLM calls until completion.'}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run',type=Path,required=True);p.add_argument('--plan-sha256',required=True);p.add_argument('--thread',required=True);p.add_argument('--handoff',type=Path,required=True);p.add_argument('--authorize-interrupted-case',type=int,action='append',default=[]);a=p.parse_args()
    root=a.run.resolve();project=Path.cwd();script=root/'code/run-phase1-vision-pilot.py'
    assert not (root/'superseded.json').exists(), 'Superseded experiment must not restart'
    import hashlib
    assert hashlib.sha256((root/'plan.json').read_bytes()).hexdigest()==a.plan_sha256
    requests=[json.loads(l) for l in (root/'requests.jsonl').open()]
    with (root/'.watcher.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        state={'status':'starting','pid':os.getpid(),'startedAt':now().isoformat(),'threadID':a.thread,'planSHA256':a.plan_sha256,'localOnlyChecks':True,'automaticScoringHandoff':True}
        save(root/'watcher.json',state)
        with (root/'runner.log').open('ab') as log:
            os.chmod(log.name,0o600)
            command=[sys.executable,str(script),'run','--directory',str(root),'--authorize-plan-sha256',a.plan_sha256]
            for case in a.authorize_interrupted_case:command+=['--authorize-interrupted-case',str(case)]
            child=subprocess.Popen(command,cwd=project,stdin=subprocess.DEVNULL,stdout=log,stderr=log)
            state.update(status='running',runnerPID=child.pid);save(root/'watcher.json',state)
            early=False;half=False
            while True:
                try:code=child.wait(timeout=30);break
                except subprocess.TimeoutExpired:pass
                progress=load(root/'progress.json') if (root/'progress.json').exists() else {}
                completed=progress.get('completed',0)
                if (not early and completed>=9) or (early and not half and completed>=len(requests)//2):
                    results=[load(f) for f in sorted((root/'results').glob('*/result.json'))]
                    e=estimate(requests,results,now())
                    if e:
                        if early:half=True
                        early=True;save(root/'eta.json',e)
        state.update(runnerExitCode=code,endedAt=now().isoformat())
        audit=load(root/'audit.json') if (root/'audit.json').exists() else {}
        state['status']='generation_complete' if code==0 and audit.get('status')=='complete' else 'needs_attention'
        save(root/'watcher.json',state)
        if state['status']=='generation_complete':
            analysis_root=root
            if load(root/'data/plan.json')['version']=='phase1-vision-budget-v2':
                analysis_root=root/'comparison-v2'
                if not analysis_root.exists():
                    with (root/'comparison-assemble.log').open('xb') as log:
                        subprocess.run([sys.executable,str(root/'code/finalize-phase1-vision-budget.py'),
                            '--run',str(root),'--output',str(analysis_root)],cwd=project,stdout=log,stderr=log,check=True)
            review=analysis_root/'review-v1'
            if not review.exists():
                with (root/'review-prepare.log').open('xb') as log:
                    os.chmod(log.name,0o600)
                    analyzer=root/'code/analyze-phase1-vision.py'
                    if not analyzer.exists():analyzer=project/'scripts/analyze-phase1-vision.py'
                    subprocess.run([sys.executable,str(analyzer),'prepare','--run',str(analysis_root),'--review',str(review)],cwd=project,stdout=log,stderr=log,check=True)
            state['status']='ready_for_scoring';save(root/'watcher.json',state)
        # Both completion and a real operational failure deserve one informed
        # follow-up. Do not automatically grant retries for uncertain dispatch.
        for attempt in range(1,4):
            output=root/f'analysis-handoff-{attempt}.jsonl'
            with a.handoff.open('rb') as prompt,output.open('xb') as log:
                os.chmod(output,0o600)
                call=subprocess.run(['codex','exec','--sandbox','workspace-write','resume',a.thread,'-','--json','-o',str(root/f'analysis-response-{attempt}.md')],cwd=project,stdin=prompt,stdout=log,stderr=log)
            state['handoffAttempt']=attempt;state['handoffExitCode']=call.returncode
            if call.returncode==0:
                state['status']='analysis_handoff_completed';save(root/'watcher.json',state);break
            msg=output.read_text(errors='replace')
            if 'active writer' not in msg:
                state['status']='handoff_failed';save(root/'watcher.json',state);break
            state['status']='waiting_for_thread_writer';save(root/'watcher.json',state)
            if attempt<3:time.sleep(120)
        else:
            state['status']='handoff_pending_thread_busy';save(root/'watcher.json',state)


if __name__=='__main__':main()

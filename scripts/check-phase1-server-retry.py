#!/usr/bin/env python3
"""Sanitized, no-network retry-policy tests; production runner stays frozen."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile

p=Path(__file__).with_name('supervise-phase1-read-model-expansion.py')
spec=importlib.util.spec_from_file_location('supervisor',p)
s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)
s.time.sleep=lambda _:None
POLICY={'pauseAfterConsecutiveServerErrors':2,'maxRetriesPerRequest':1,'delaySeconds':30}
PLAN={'executorPlanSHA256':'frozen','serverErrorRetryPolicy':POLICY}
ERROR={'error':{'code':'500','message':'servers overloaded'}}


def attempt(root,events,status=200):
    p=root/'results/0000';p.mkdir(parents=True)
    wire=''.join('data: '+json.dumps(e)+'\n\n' for e in events).encode()
    (p/'response.body').write_bytes(wire)
    s.save(p/'transport.json',{'httpStatus':status,'bodySHA256':hashlib.sha256(wire).hexdigest(),
        'dispatchToCompletionSeconds':1})
    s.save(p/'inflight.json',{'binding':{'executorPlanSHA256':'frozen','request':{'requestOrdinal':0}}})
    s.save(root/'results/progress.json',{'completed':0,'lastRequestOrdinal':-1})
    return p


def main():
    with tempfile.TemporaryDirectory(prefix='phase1-retry-test-') as temp:
        root=Path(temp);state={'supervisionPlanSHA256':'authorized'}
        p=attempt(root,[{'type':'response.created'},ERROR])
        original={f.name:s.sha(f) for f in p.iterdir()}
        assert s.retry_server_failure(root,PLAN,state)
        assert not p.exists()
        archive=root/'retry-attempts/0000/failed-attempt-1'
        assert {f.name:s.sha(f) for f in archive.iterdir()}==original
        probes=iter([False,False,True])
        s.wait_for_proxy_port(root,state,probe=lambda:next(probes))
        try:s.wait_for_proxy_port(root,state,probe=lambda:False,timeout=0)
        except RuntimeError:pass
        else:raise AssertionError('busy listener was ignored')
        s.verify_retry_archives(root,PLAN)
        p=attempt(root,[ERROR])
        assert not s.retry_server_failure(root,PLAN,state)
        assert state['consecutiveServerErrors']==2 and p.exists()
        assert {f.name:s.sha(f) for f in archive.iterdir()}==original
        # Successful output, quota/auth, uncertainty, or partial output is not retryable.
        for events,status in [([{'type':'response.completed'}],200),
            ([{'error':{'code':'429','message':'quota'}}],200),
            ([{'error':{'code':'401','message':'auth'}}],401),
            ([{'type':'response.in_progress'}],200),
            ([{'type':'response.output_text.delta','delta':'partial'},ERROR],200),
            ([{'type':'response.reasoning_text.delta','delta':'partial'},ERROR],200)]:
            wire=''.join('data: '+json.dumps(e)+'\n\n' for e in events).encode()
            (p/'response.body').write_bytes(wire)
            s.save(p/'transport.json',{'httpStatus':status,'bodySHA256':hashlib.sha256(wire).hexdigest()})
            assert s.server_error(p) is None
        # Never silently accept altered archived evidence.
        (archive/'response.body').write_bytes(b'tampered')
        try:s.verify_retry_archives(root,PLAN)
        except AssertionError:pass
        else:raise AssertionError('tampered archive accepted')
    print('PASS: one explicit 5xx retry, second pauses, archive preserved, quota/auth/uncertain/partial outputs rejected, tampering rejected, proxy socket release waited for without replacing listener; no network')


if __name__=='__main__':main()

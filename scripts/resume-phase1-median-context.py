#!/usr/bin/env python3
"""One explicitly authorized replacement, then resume the unchanged runner.

Preserve the interrupted transport outside the runner's new attempt namespace.
All completed results and the frozen experiment plan remain unchanged.
"""
import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time


def module(root):
    sys.path.insert(0, str(root / 'code'))
    spec = importlib.util.spec_from_file_location('frozen_median', root / 'code/run-phase1-median-context.py')
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def prepare(root, m, case, digest):
    assert m.file_hash(root / 'plan.json') == digest
    m.verify(root)
    reqs = list(m.rows(root / 'requests.jsonl'))
    req = next(r for r in reqs if r['case'] == case and r['arm'] == 'time_images')
    folder = root / 'results' / f"{req['requestOrdinal']:04d}"
    assert not (folder / 'result.json').exists()
    assert [p.name for p in folder.glob('attempt-*')] == ['attempt-1']
    old = folder / 'attempt-1'
    binding = m.load(old / 'inflight.json')['binding']
    assert binding['request'] == req and binding['planSHA256'] == digest
    transport = m.load(old / 'transport.json')
    assert m.file_hash(old / 'response.body') == transport['bodySHA256']
    archive = root / 'authorized-replacement-20260910'
    assert not archive.exists(), 'Authorization already prepared; use replace or audit'
    archive.mkdir(mode=0o700)
    before = {str(p.relative_to(root)): m.file_hash(p) for p in root.glob('results/*/result.json')}
    decision = {'version': 'phase1-explicit-replacement-v1', 'at': m.ex.now(),
        'authorization': 'User: instead of trying to time it, just keep this thread live, polling every few minutes for completion. but yes continue',
        'runPlanSHA256': digest, 'request': req, 'maximumReplacementDispatches': 1,
        'scope': 'One replacement of interrupted case 148/time_images; then unchanged sampling of unsent requests.',
        'scriptSHA256': m.file_hash(Path(__file__)), 'completedResultsSHA256': before,
        'interruptedAttemptSHA256': {p.name: m.file_hash(p) for p in old.iterdir() if p.is_file()},
        'interruptedUsage': 'Not returned; do not assume zero usage or cost.',
        'archive': str(archive / 'interrupted-attempt-1')}
    m.ex.atomic_json(archive / 'authorization.json', decision)
    os.rename(old, archive / 'interrupted-attempt-1')
    if (root / 'pause.json').exists(): os.rename(root / 'pause.json', archive / 'original-pause.json')
    print(m.canonical({'status': 'prepared', 'retainedCompleted': len(before), 'requestOrdinal': req['requestOrdinal']}))


def verify(root, m):
    archive = root / 'authorized-replacement-20260910'
    a = m.load(archive / 'authorization.json')
    assert m.file_hash(root / 'plan.json') == a['runPlanSHA256']
    assert m.file_hash(Path(__file__)) == a['scriptSHA256']
    m.verify(root)
    for name, digest in a['completedResultsSHA256'].items(): assert m.file_hash(root / name) == digest
    for name, digest in a['interruptedAttemptSHA256'].items():
        assert m.file_hash(archive / 'interrupted-attempt-1' / name) == digest
    return archive, a


def replace(root, m):
    archive, a = verify(root, m)
    req = a['request']; reqs = list(m.rows(root / 'requests.jsonl')); plan = m.verify(root)
    sent = 0
    def once(body, attempt, timeout):
        nonlocal sent
        assert sent == 0 and not (archive / 'replacement-dispatch.json').exists(), 'No second replacement dispatch'
        m.ex.atomic_json(archive / 'replacement-dispatch.json', {'at': m.ex.now(), 'attempt': str(attempt),
            'authorizationSHA256': m.file_hash(archive / 'authorization.json')})
        sent += 1
        return m.ex.transport(body, attempt, timeout)
    with (root / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stop = threading.Event()
        def memory_guard():
            while not stop.wait(5):
                rss = m.legacy.failure.memory(os.getpid())
                m.ex.atomic_json(archive / 'resources.json', {'at': m.ex.now(), 'processTreeMiB': rss, 'limitMiB': 4096})
                if rss > 4096: os.kill(os.getpid(), signal.SIGTERM)
        threading.Thread(target=memory_guard, daemon=True).start()
        try:
            with m.ex.owned_proxy(plan, root):
                result = m.execute_one(root, req, a['runPlanSHA256'], sender=once)
            m.publish(root, reqs, plan, full=True)
            m.ex.atomic_json(archive / 'replacement-complete.json', {'at': m.ex.now(),
                'sampleID': result['sampleID'], 'providerCallsThisInvocation': sent,
                'resultSHA256': m.file_hash(root / 'results' / f"{req['requestOrdinal']:04d}" / 'result.json')})
            print(m.canonical({'status': 'replacement_complete', 'case': req['case'], 'providerCallsThisInvocation': sent}))
        finally:
            stop.set()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('prepare', 'replace', 'audit'))
    p.add_argument('--run', type=Path, required=True); p.add_argument('--plan-sha256'); p.add_argument('--case', type=int, default=148)
    a = p.parse_args(); root = a.run.resolve(); m = module(root)
    if a.action == 'prepare':
        with (root / '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            prepare(root, m, a.case, a.plan_sha256)
    elif a.action == 'replace': replace(root, m)
    else:
        _, auth = verify(root, m)
        print(m.canonical({'status': 'passed', 'priorResultsPreserved': len(auth['completedResultsSHA256']), 'providerCalls': 0}))


if __name__ == '__main__': main()

#!/usr/bin/env python3
"""Attach to an existing run and resume its own thread once for analysis.

Local JSON/lock checks only until completion or a real operational stop. Never
launch, stop, retry, or modify the sampling runner. Never start a new thread.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def load(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    pending = path.with_suffix('.pending')
    with pending.open('w') as f:
        os.chmod(pending, 0o600)
        json.dump(value, f, indent=2, sort_keys=True)
        f.write('\n'); f.flush(); os.fsync(f.fileno())
    os.replace(pending, path)


def runner_active(root):
    # The runner holds this exact lock throughout dispatch and its final audit.
    with (root / '.lock').open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False


def verify_complete(root, digest):
    """Verify the runner's final audit without loading any images/prompt bodies."""
    assert sha(root / 'plan.json') == digest, 'Run plan changed'
    plan = load(root / 'plan.json')
    audit = load(root / 'audit.json')
    assert audit['status'] == 'complete' and audit['planSHA256'] == digest
    assert audit['predictionsSHA256'] == sha(root / 'predictions.jsonl')
    with (root / 'requests.jsonl').open() as f:
        requests = [json.loads(line) for line in f if line.strip()]
    with (root / 'predictions.jsonl').open() as f:
        predictions = [json.loads(line) for line in f if line.strip()]
    eligible = [r for r in requests if not r['limits']]
    blocked = [{'case': r['case'], 'arm': r['arm']} for r in requests if r['limits']]
    assert len(requests) == plan['plannedConditions'] == audit['plannedConditions']
    assert len(predictions) == len(eligible) == audit['completed'] == audit['plannedPredictions']
    assert len({r['sampleID'] for r in predictions}) == len(predictions)
    assert {r['sampleID'] for r in predictions} == {r['sampleID'] for r in eligible}
    assert Counter(r['arm'] for r in predictions) == Counter(r['arm'] for r in eligible)
    assert audit['capacityBlocked'] == blocked
    assert audit['newCompleted'] == audit['newPlanned'] == plan['newProviderRequests']
    return audit


def inspect(root, digest):
    assert sha(root / 'plan.json') == digest, 'Run plan changed'
    if runner_active(root):
        return 'waiting_for_sampling'
    if (root / 'audit.json').exists() and load(root / 'audit.json').get('status') == 'complete':
        verify_complete(root, digest)
        return 'ready_for_scoring'
    return 'sampling_needs_attention'


def busy_before_model_started(path):
    """Only retry a rejected writer-lock acquisition, never an uncertain turn."""
    busy = False
    with path.open(errors='replace') as f:
        for line in f:
            busy |= 'active writer' in line.lower()
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('type') in ('turn.started', 'turn.completed', 'item.started', 'item.completed'):
                return False
    return busy


def handoff(folder, plan, condition, call=subprocess.run, sleep=time.sleep):
    marker = folder / 'handoff.json'
    if marker.exists():
        prior = load(marker)
        if prior['status'] == 'scoring_turn_finished':
            return prior
        assert prior['status'] == 'waiting_for_thread_writer', 'Uncertain prior handoff; do not send twice'
        first = prior['attempt'] + 1
    else:
        first = 1
    prompt = folder / 'handoff.md'
    assert sha(prompt) == plan['handoffSHA256'], 'Handoff instructions changed'
    for attempt in range(first, 1441):
        state = {'status': 'handoff_inflight', 'attempt': attempt,
                 'condition': condition, 'at': now(), 'threadID': plan['threadID']}
        save(marker, state)  # Before potentially starting a model turn.
        output = folder / f'handoff-{attempt:04d}.jsonl'
        with prompt.open('rb') as body, output.open('xb') as log:
            os.chmod(output, 0o600)
            command = [plan['codex'], 'exec', '--sandbox', 'workspace-write', 'resume',
                       plan['threadID'], '-', '--json', '-o', str(folder / f'answer-{attempt:04d}.md')]
            result = call(command, cwd=plan['project'], stdin=body, stdout=log, stderr=log)
        state['exitCode'] = result.returncode
        if result.returncode == 0:
            state['status'] = 'scoring_turn_finished'
        elif busy_before_model_started(output):
            state['status'] = 'waiting_for_thread_writer'
        else:
            state['status'] = 'handoff_needs_attention'
        state['at'] = now(); save(marker, state)
        if state['status'] != 'waiting_for_thread_writer':
            return state
        sleep(30)  # A busy-lock rejection makes no model call.
    return state


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('prepare', 'watch'))
    p.add_argument('--run', type=Path, required=True)
    p.add_argument('--plan-sha256')
    p.add_argument('--thread')
    p.add_argument('--handoff', type=Path)
    a = p.parse_args(); root = a.run.resolve(); folder = root / 'analysis-followup'
    if a.action == 'prepare':
        assert sha(root / 'plan.json') == a.plan_sha256
        assert a.thread and a.handoff.is_file()
        assert not folder.exists(), 'Follow-up already prepared'
        folder.mkdir(mode=0o700)
        shutil.copyfile(a.handoff, folder / 'handoff.md')
        shutil.copyfile(__file__, folder / Path(__file__).name)
        codex = shutil.which('codex'); assert codex
        plan = {'version': 'phase1-median-context-followup-v1', 'run': str(root),
                'runPlanSHA256': a.plan_sha256, 'threadID': a.thread,
                'project': str(Path.cwd()), 'codex': codex, 'preparedAt': now(),
                'handoffSHA256': sha(folder / 'handoff.md'),
                'watcherSHA256': sha(folder / Path(__file__).name),
                'pollSeconds': 30, 'samplingProviderCalls': 0,
                'policy': 'Attach only; same-thread scoring handoff. No experiment resends or collector changes.'}
        save(folder / 'plan.json', plan)
        print(json.dumps(plan)); return
    plan = load(folder / 'plan.json')
    assert plan['run'] == str(root)
    assert sha(__file__) == plan['watcherSHA256']
    assert sha(folder / 'handoff.md') == plan['handoffSHA256']
    with (folder / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            try:
                condition = inspect(root, plan['runPlanSHA256'])
            except Exception as e:
                condition = 'integrity_needs_attention'
                save(folder / 'error.json', {'at': now(), 'error': str(e)})
            save(folder / 'status.json', {'status': condition, 'at': now(), 'pid': os.getpid(),
                                         'threadID': plan['threadID'], 'localOnlyChecks': True})
            if condition != 'waiting_for_sampling':
                break
            time.sleep(plan['pollSeconds'])
        result = handoff(folder, plan, condition)
        save(folder / 'status.json', result | {'pid': os.getpid()})


if __name__ == '__main__':
    main()

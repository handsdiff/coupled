#!/usr/bin/env python3
"""No-network checks of completion gates and the same-thread handoff."""
import fcntl
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location('followup', Path(__file__).with_name('watch-phase1-median-context.py'))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)


def reject(fn):
    try:
        fn()
    except AssertionError:
        return
    raise AssertionError('Expected rejection')


def main():
    with TemporaryDirectory(prefix='coupled-followup-check-') as td:
        root = Path(td)
        m.save(root / 'plan.json', {'plannedConditions': 3, 'newProviderRequests': 1})
        digest = m.sha(root / 'plan.json')
        requests = [{'sampleID': 'a', 'case': 1, 'arm': 'time_cleaned', 'limits': []},
                    {'sampleID': 'b', 'case': 1, 'arm': 'time_ocr', 'limits': []},
                    {'sampleID': 'c', 'case': 41, 'arm': 'time_images', 'limits': ['capacity']}]
        (root / 'requests.jsonl').write_text('\n'.join(json.dumps(r) for r in requests) + '\n')
        (root / 'predictions.jsonl').write_text('\n'.join(json.dumps(r) for r in requests[:2]) + '\n')
        assert m.inspect(root, digest) == 'sampling_needs_attention'
        with (root / '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert m.inspect(root, digest) == 'waiting_for_sampling'
        audit = {'status': 'complete', 'planSHA256': digest, 'predictionsSHA256': m.sha(root / 'predictions.jsonl'),
                 'plannedConditions': 3, 'completed': 2, 'plannedPredictions': 2,
                 'capacityBlocked': [{'case': 41, 'arm': 'time_images'}], 'newCompleted': 1, 'newPlanned': 1}
        m.save(root / 'audit.json', audit)
        assert m.inspect(root, digest) == 'ready_for_scoring'
        m.save(root / 'audit.json', audit | {'capacityBlocked': []})
        reject(lambda: m.inspect(root, digest))
        m.save(root / 'audit.json', audit)
        with (root / 'predictions.jsonl').open('a') as f:
            f.write('{}\n')
        reject(lambda: m.inspect(root, digest))
        folder = root / 'followup'; folder.mkdir()
        (folder / 'handoff.md').write_text('Score the completed frozen run, no resampling.\n')
        plan = {'threadID': 'existing-thread', 'project': str(root), 'codex': 'codex',
                'handoffSHA256': m.sha(folder / 'handoff.md')}
        calls = []
        def fake(command, **kw):
            calls.append(command)
            assert command[1:5] == ['exec', '--sandbox', 'workspace-write', 'resume']
            assert command[5] == 'existing-thread'
            assert m.load(folder / 'handoff.json')['status'] == 'handoff_inflight'
            if len(calls) == 1:
                kw['stdout'].write(b'error: already has an active writer\n')
                return SimpleNamespace(returncode=1)
            return SimpleNamespace(returncode=0)
        assert m.handoff(folder, plan, 'ready_for_scoring', fake, lambda _: None)['status'] == 'scoring_turn_finished'
        assert len(calls) == 2
        m.handoff(folder, plan, 'ready_for_scoring', fake, lambda _: None)
        assert len(calls) == 2, 'Completed handoff must not repeat'
        m.save(folder / 'handoff.json', {'status': 'handoff_inflight', 'attempt': 3})
        reject(lambda: m.handoff(folder, plan, 'ready_for_scoring', fake))
        evidence = root / 'uncertain.jsonl'
        evidence.write_text('{"type":"turn.started"}\nerror: active writer\n')
        assert not m.busy_before_model_started(evidence)
        evidence.write_text('network connection failed\n')
        assert not m.busy_before_model_started(evidence)
    print(json.dumps({'status': 'passed', 'providerCalls': 0, 'checks': [
        'attach_without_restarting_sampling', 'final_audit_and_hash_gate', 'capacity_is_not_a_failure',
        'same_thread_only', 'persist_before_handoff', 'writer_busy_retry_only',
        'completed_handoff_idempotent', 'uncertain_handoff_not_repeated']}))


if __name__ == '__main__':
    main()

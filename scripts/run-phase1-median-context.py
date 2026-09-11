#!/usr/bin/env python3
"""Frozen four-condition subscription execution; reuse the original 32K baseline.

One prompt/wire body in memory at a time. No training, provider fallback, input
shortening, or semantic-failure resampling. Preparation and audit are offline.
"""
import argparse
from collections import Counter
import copy
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import random
import shutil
import signal
import statistics
import sys
import threading
import time

from phase1_read_model_comparison import canonical, file_hash, fingerprint, rows


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


legacy = module('median_transport', 'run-phase1-vision-pilot.py')
ex = legacy.ex
VERSION = 'phase1-median-context-executor-v2'
ARMS = ('time_cleaned', 'time_ocr', 'time_images', 'budget_cleaned', 'budget_ocr')
CODE = tuple(dict.fromkeys((Path(__file__).name, 'check-phase1-median-context-executor.py',
                           *legacy.CODE)))


def load(path):
    return json.loads(Path(path).read_text())


def bind_reuse(ref, request):
    """Resolve explicit historical reuse to its original immutable transport."""
    source = Path(ref['run'])
    assert file_hash(source / 'predictions.jsonl') == ref['predictionsSHA256']
    original = next(r for r in rows(source / 'requests.jsonl') if r['sampleID'] == ref['sampleID'])
    assert all(original[k] == request[k] for k in ('case', 'exampleID', 'partsSHA256'))
    for _ in range(8):
        if not original.get('reuse'):
            break
        link = original['reuse']
        source = Path(link['sourceRun'])
        assert file_hash(source / 'plan.json') == link['sourcePlanSHA256']
        original = next(r for r in rows(source / 'requests.jsonl')
                        if r['requestOrdinal'] == link['sourceRequestOrdinal'])
        assert all(original[k] == request[k] for k in ('case', 'exampleID', 'partsSHA256'))
    else:
        raise AssertionError('Cyclic/overlong reuse chain')
    folder = source / 'results' / f"{original['requestOrdinal']:04d}"
    return {'sourceRun': str(source), 'sourceRequestOrdinal': original['requestOrdinal'],
            'sourcePlanSHA256': file_hash(source / 'plan.json'),
            'sourceResultSHA256': file_hash(folder / 'result.json'),
            'sourceVariant': original['variant'], 'preparedReference': ref}


def reused_result(request):
    ref = request['reuse']
    assert file_hash(Path(ref['preparedReference']['run']) / 'predictions.jsonl') == ref['preparedReference']['predictionsSHA256']
    value = legacy.reused_result(dict(request, variant=ref['sourceVariant']), legacy.PRICES)
    return {**value, **request, 'providerCallsForThisCondition': 0}


def prepare(source, root, validation, rejected_run=None):
    assert not root.exists(), 'Use a new immutable execution directory'
    source = source.resolve()
    data = load(source / 'plan.json')
    assert data['version'] == 'phase1-median-context-controls-v1'
    assert data['fourConditions'] == list(ARMS[1:]) and data['baseline'] == ARMS[0]
    digest = file_hash(source / 'plan.json')
    report = load(validation)
    assert report['status'] == 'passed' and report['networkCalls'] == 0
    assert report['preparedPlanSHA256'] == digest
    assert report['codeSHA256'] == {n: file_hash(Path(__file__).with_name(n)) for n in CODE}
    assert report['largestRealRequest']['completedTransportResumedWithoutResend']
    assert report['largestRealRequest']['imageCount'] > 60
    rejected = None
    if rejected_run:
        prior = rejected_run.resolve()
        attempts = list(prior.glob('results/*/attempt-*/transport.json'))
        assert len(attempts) == 1
        t = load(attempts[0]); body = attempts[0].parent/'response.body'
        assert t['httpStatus'] == 400 and file_hash(body) == t['bodySHA256']
        assert 'Unsupported parameter: truncation' in load(body)['error']['message']
        rejected = {'run': str(prior), 'planSHA256': file_hash(prior/'plan.json'),
                    'transport': str(attempts[0]), 'transportSHA256': file_hash(attempts[0]),
                    'bodySHA256': file_hash(body), 'reason': 'Unsupported API parameter; no prediction returned',
                    'usage': 'not returned; do not infer billed usage'}
    audit = load(source / 'audit.json')
    assert audit['status'] == 'passed' and audit['planSHA256'] == digest
    hashes = load(source / 'artifact-hashes.json')
    ex.verify_files(source, hashes)
    requests = []
    for row in load(source / 'cases.json'):
        req = {k: row[k] for k in ('case', 'arm', 'exampleID', 'partsSHA256', 'payload',
                                  'estimatedInputTokens', 'wirePayloadBytes', 'status', 'limits')}
        req.update(model=legacy.MODEL, variant=row['arm'], replicate=1)
        p = load(source / row['payload'])
        assert p['partsSHA256'] == fingerprint(p['parts'])
        req['imageCount'] = p['imageCount']
        if p['reuse']:
            req['reuse'] = bind_reuse(p['reuse'], req)
            reused_result(req)
        requests.append(req)
    assert len(requests) == 345 and Counter(r['arm'] for r in requests) == {a: 69 for a in ARMS}
    assert [(r['case'], r['arm']) for r in requests if r['limits']] == [(41, 'time_images')]
    assert sum(bool(r.get('reuse')) for r in requests) == 82
    new = [r for r in requests if not r.get('reuse') and not r['limits']]
    assert len(new) == 262
    # Select by input size, never by model output/target quality. Both retained.
    probes = [max((r for r in new if r['arm'] == a), key=lambda r: r['wirePayloadBytes'])
              for a in ('time_images', 'budget_cleaned')]
    rest = [r for r in new if r not in probes]
    random.Random(17).shuffle(rest)
    requests = [r for r in requests if r.get('reuse') or r['limits']] + probes + rest
    for i, r in enumerate(requests, 1):
        r['requestOrdinal'] = i
        r['sampleID'] = fingerprint([digest, r['case'], r['arm'], r['partsSHA256']])
    root.mkdir(parents=True, mode=0o700)
    (root / 'code').mkdir(mode=0o700)
    (root / 'data').mkdir(mode=0o700)
    for name in CODE:
        shutil.copyfile(Path(__file__).with_name(name), root / 'code' / name)
    for name in (*hashes, 'artifact-hashes.json', 'audit.json'):
        target = root / 'data' / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(source / name, target)
    shutil.copyfile(validation, root / 'executor-validation.json')
    legacy.save_rows(root / 'requests.jsonl', requests)
    proxy = copy.deepcopy(ex.PROXY)
    proxy['model_list'] = [{'model_name': legacy.MODEL, 'litellm_params': {'model': legacy.MODEL}}]
    proxy['litellm_settings']['cache'] = False
    ex.atomic_json(root / 'proxy.json', proxy)
    artifacts = [*('data/' + n for n in hashes), 'data/artifact-hashes.json', 'data/audit.json',
                 'executor-validation.json', 'requests.jsonl', 'proxy.json']
    plan = {'version': VERSION, 'preparedPlanSHA256': digest,
            'project': str(Path.cwd().resolve()), 'source': str(source),
            'pythonVersion': sys.version, 'sharedBudgetInputTokens': data['sharedBudgetInputTokens'],
            'codeSHA256': {n: file_hash(root / 'code' / n) for n in CODE},
            'runtimeSHA256': ex.runtime_hashes(Path.cwd()),
            'artifactsSHA256': {n: file_hash(root / n) for n in artifacts},
            'provider': {'model': legacy.MODEL, 'reasoningEffort': 'xhigh', 'temperature': 'omitted',
                         'tools': [], 'stream': True,
                         'truncation': 'omitted: subscription rejects this API parameter; no client/proxy shortening or compaction configured',
                         'endpoint': ex.ENDPOINT, 'apiKeyFallback': False, 'localResponseCache': False},
            'plannedConditions': 345, 'newProviderRequests': 262, 'reuses': 82,
            'blocked': [{'case': 41, 'arm': 'time_images', 'reason': 'estimated_context_over_limit'}],
            'preflightSampleIDs': [p['sampleID'] for p in probes],
            'preflightAcceptance': 'Completed transport and usage within max(4096, 5%) of frozen input estimate; no grading gate.',
            'requestTimeoutSeconds': 1800, 'maximumProcessTreeMiB': 4096,
            'concurrency': 1, 'seedForRequestOrder': 17,
            'failurePolicy': 'One retry only for explicit output-free 5xx; pause on two consecutive such errors or uncertain dispatch. Retain invalid completions.',
            'authorization': 'User: please execute on that; corrected four-condition subscription run with capacity-blocked case 41.',
            'pricesUSDPerMillion': legacy.PRICES, 'pricingBasis': 'API-equivalent, not subscription billing; long-input multipliers and cache usage retained.',
            'scoringContract': data['scoringContract'], 'training': False,
            'modelRevision': 'Requested and returned model checked; remote weights revision unverified',
            'supersededRejectedAttempt': rejected,
            'artifactPolicy': 'Source inputs unchanged; isolated runtime/source hashes required on every resume.'}
    assert plan['codeSHA256'] == report['codeSHA256']
    ex.atomic_json(root / 'plan.json', plan)
    for p in root.rglob('*'):
        if p.is_file(): os.chmod(p, 0o600)
    print(canonical({'status': 'frozen', 'planSHA256': file_hash(root / 'plan.json'), 'providerCalls': 0}))


def verify(root):
    plan = load(root / 'plan.json')
    assert plan['version'] == VERSION and plan['pythonVersion'] == sys.version
    assert Path(__file__).resolve().parent == root / 'code', 'Use the isolated frozen runner'
    ex.verify_files(root, plan['artifactsSHA256'])
    ex.verify_files(root / 'code', plan['codeSHA256'])
    ex.verify_files(Path('/'), plan['runtimeSHA256'])
    assert file_hash(root / 'data/plan.json') == plan['preparedPlanSHA256']
    return plan


def prompt(root, req):
    path = root / 'data' / req['payload']
    p = load(path)
    assert p['partsSHA256'] == req['partsSHA256'] == fingerprint(p['parts'])
    assert p['case'] == req['case'] and p['arm'] == req['arm']
    assert not p['limits'], 'Blocked input cannot dispatch'
    return p


def execute_one(root, req, digest, sender=ex.transport, sleep=time.sleep):
    folder = root / 'results' / f"{req['requestOrdinal']:04d}"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if req['limits']:
        value = {'status': 'capacity_blocked', 'planSHA256': digest, 'request': req,
                 'prediction': None, 'score': None, 'providerCalls': 0}
        if (folder / 'blocked.json').exists(): assert load(folder / 'blocked.json') == value
        else: ex.atomic_json(folder / 'blocked.json', value)
        return None
    if req.get('reuse'):
        value = reused_result(req)
        binding = {'planSHA256': digest, 'request': req}
        if (folder / 'reuse.json').exists(): assert load(folder / 'reuse.json') == binding
        else: ex.atomic_json(folder / 'reuse.json', binding)
        if (folder / 'result.json').exists(): assert load(folder / 'result.json') == value
        else: ex.atomic_json(folder / 'result.json', value)
        return value
    if (folder / 'result.json').exists():
        found, audit = legacy.audit(root, [req], digest, legacy.PRICES)
        assert audit['status'] == 'complete'
        return found[0]
    p = prompt(root, req)
    return legacy.run_requests([req], {req['partsSHA256']: p}, root, digest,
                               sender=sender, sleep=sleep)[0]


def preflight_report(root, requests, plan):
    results = []
    for ident in plan['preflightSampleIDs']:
        req = next(r for r in requests if r['sampleID'] == ident)
        f = root / 'results' / f"{req['requestOrdinal']:04d}" / 'result.json'
        if not f.exists(): return {'status': 'incomplete'}
        r = load(f)
        actual = r['usage']['input_tokens']; estimate = req['estimatedInputTokens']
        results.append({'case': req['case'], 'arm': req['arm'], 'sampleID': ident,
                        'resultSHA256': file_hash(f), 'estimatedInputTokens': estimate,
                        'actualInputTokens': actual, 'relativeError': (actual - estimate) / estimate,
                        'passed': r['responseStatus'] == 'completed' and abs(actual - estimate) <= max(4096, estimate * .05),
                        'validCompletion': r['validCompletion'], 'imageCount': req['imageCount'],
                        'scope': 'Measured execution feasibility/accounting, not semantic prediction quality'})
    return {'status': 'passed' if all(r['passed'] for r in results) else 'failed',
            'planSHA256': file_hash(root / 'plan.json'), 'results': results,
            'budgetRemainsFrozen': plan['sharedBudgetInputTokens'], 'noPreflightResampling': True}


def audit_run(root, requests, plan, *, full=True):
    digest = file_hash(root / 'plan.json'); found = []; blocked = []; identities = {}
    for req in requests:
        folder = root / 'results' / f"{req['requestOrdinal']:04d}"
        if req['limits']:
            if (folder / 'blocked.json').exists():
                assert load(folder / 'blocked.json')['request'] == req
                blocked.append({'case': req['case'], 'arm': req['arm']})
            continue
        if not (folder / 'result.json').exists(): continue
        stored = load(folder / 'result.json')
        if full:
            if req.get('reuse'):
                assert stored == reused_result(req)
                assert load(folder / 'reuse.json') == {'planSHA256': digest, 'request': req}
            else:
                actual, _ = legacy.audit(root, [req], digest, legacy.PRICES)
                assert actual == [stored]
        key = stored['responseID']; binding = (req['case'], req['partsSHA256'])
        if key in identities:
            assert req.get('reuse') and identities[key] == binding, 'Unexpected repeated response identity'
        identities[key] = binding; found.append(stored)
    fresh = [r for r in found if not r.get('reuse')]
    report = {'status': 'complete' if len(found) + len(blocked) == len(requests) else 'partial',
              'completed': len(found), 'plannedPredictions': len(requests) - 1,
              'plannedConditions': len(requests), 'capacityBlocked': blocked,
              'newCompleted': len(fresh), 'newPlanned': plan['newProviderRequests'],
              'reusedCompleted': len(found) - len(fresh),
              'byArm': dict(Counter(r['arm'] for r in found)),
              'providerCallsDuringAudit': 0, 'checkedAt': ex.now(), 'planSHA256': digest}
    if fresh:
        durations = [r['timing']['dispatchToCompletionSeconds'] for r in fresh]
        if durations:
            report['medianLatencySeconds'] = statistics.median(durations)
            report['meanLatencySeconds'] = statistics.mean(durations)
        report['actualInputTokens'] = sum(r['usage']['input_tokens'] for r in fresh)
        report['apiEquivalentCostUSD'] = sum(r['apiEquivalentCostUSD'] or 0 for r in fresh)
        report['unknownCostResults'] = sum(r['apiEquivalentCostUSD'] is None for r in fresh)
    return found, report


def publish(root, requests, plan, *, full=False):
    found, report = audit_run(root, requests, plan, full=full)
    # Snapshot, not append: recovery cannot create duplicate rows.
    pending = root / 'predictions.pending'
    with pending.open('w') as f:
        os.chmod(pending, 0o600)
        for r in found: f.write(canonical(r) + '\n')
        f.flush(); os.fsync(f.fileno())
    os.replace(pending, root / 'predictions.jsonl'); ex.sync_dir(root)
    report['predictionsSHA256'] = file_hash(root / 'predictions.jsonl')
    ex.atomic_json(root / 'progress.json', report)
    if full: ex.atomic_json(root / 'audit.json', report)
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('prepare', 'preflight', 'run', 'audit'))
    p.add_argument('--source', type=Path)
    p.add_argument('--validation-report', type=Path)
    p.add_argument('--directory', type=Path, required=True)
    p.add_argument('--authorize-plan-sha256')
    p.add_argument('--rejected-run', type=Path, help='Preserve evidence of rejected unsupported-parameter preflight')
    a = p.parse_args(); root = a.directory.resolve()
    if a.action == 'prepare':
        prepare(a.source, root, a.validation_report, a.rejected_run); return
    plan = verify(root); digest = file_hash(root / 'plan.json')
    requests = list(rows(root / 'requests.jsonl'))
    if a.action == 'audit':
        print(canonical(publish(root, requests, plan, full=True))); return
    assert a.authorize_plan_sha256 == digest, 'Exact frozen-plan authorization required'
    with (root / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ex.atomic_json(root / 'process.json', {'pid': os.getpid(), 'startedAt': ex.now(), 'planSHA256': digest})
        stop = threading.Event()
        def interrupted(signum, frame): raise RuntimeError('Interrupted; keep uncertain dispatch evidence')
        signal.signal(signal.SIGTERM, interrupted)
        def monitor():
            peak = 0
            while not stop.wait(5):
                try:
                    rss = legacy.failure.memory(os.getpid()); peak = max(peak, rss)
                    ex.atomic_json(root / 'resources.json', {'checkedAt': ex.now(), 'processTreeMiB': rss,
                        'peakObservedProcessTreeMiB': peak, 'limitMiB': plan['maximumProcessTreeMiB']})
                    assert rss <= plan['maximumProcessTreeMiB'], 'Process-tree memory limit reached'
                except Exception as e:
                    ex.atomic_json(root / 'resource-guard.json', {'at': ex.now(), 'error': str(e)})
                    os.kill(os.getpid(), signal.SIGTERM); return
        threading.Thread(target=monitor, daemon=True).start()
        try:
            # Local reuses/dispositions precede every live dispatch.
            for req in requests:
                if req.get('reuse') or req['limits']: execute_one(root, req, digest)
            publish(root, requests, plan)
            if a.action == 'run':
                report = preflight_report(root, requests, plan)
                assert report['status'] == 'passed', 'Complete and inspect the bound preflight before batch execution'
            wanted = set(plan['preflightSampleIDs']) if a.action == 'preflight' else None
            with ex.owned_proxy(plan, root):
                for req in requests:
                    if req.get('reuse') or req['limits'] or (wanted is not None and req['sampleID'] not in wanted): continue
                    execute_one(root, req, digest)
                    report = publish(root, requests, plan)
                    print(canonical({'completed': report['newCompleted'], 'planned': report['newPlanned'],
                                     'case': req['case'], 'arm': req['arm']}), flush=True)
            if a.action == 'preflight':
                report = preflight_report(root, requests, plan)
                ex.atomic_json(root / 'preflight.json', report)
                assert report['status'] == 'passed', 'Preflight failed; preserve outputs and do not run batch'
            report = publish(root, requests, plan, full=True)
            ex.atomic_json(root / 'execution-status.json', {'status': 'complete_pending_holistic_scoring' if report['status'] == 'complete' else 'preflight_complete', 'at': ex.now(), 'planSHA256': digest})
        except BaseException as e:
            ex.atomic_json(root / 'pause.json', {'status': 'paused', 'reason': str(e), 'at': ex.now(), 'planSHA256': digest})
            raise
        finally:
            stop.set()


if __name__ == '__main__':
    main()

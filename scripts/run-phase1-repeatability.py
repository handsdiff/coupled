#!/usr/bin/env python3
"""Selected-case repeatability using frozen inputs and the existing subscription transport.

Local prepare/audit; explicit reviewed-plan authorization for run. No training.
Original outputs and judgments are immutable. Prefix caching is not output reuse.
"""
import argparse
from collections import Counter, defaultdict
import copy
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import random
import shutil
import sys
import time

from phase1_read_model_comparison import canonical, file_hash, fingerprint, rows
from phase1_subscription_output import interpret


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


ex = module('repeat_transport', 'run-phase1-read-model-comparison.py')
supervisor = module('repeat_failure', 'supervise-phase1-read-model-expansion.py')
VERSION = 'phase1-selected-repeatability-v1'
CODE = ('run-phase1-repeatability.py', 'run-phase1-read-model-comparison.py',
        'phase1_read_model_comparison.py', 'phase1_subscription_output.py',
        'supervise-phase1-read-model-expansion.py')


def load(path):
    return json.loads(Path(path).read_text())


def save_rows(path, data):
    with path.open('x') as f:
        os.chmod(path, 0o600)
        for row in data:
            f.write(canonical(row) + '\n')
        f.flush()
        os.fsync(f.fileno())


def schedule(original, selected_ids, seed):
    selected = [r for r in original if r['exampleID'] in selected_ids]
    assert len(selected) == 4 * len(selected_ids)
    assert len({(r['exampleID'], r['model'], r['variant']) for r in selected}) == len(selected)
    result = []
    for replicate in (2, 3):
        for row in selected:
            result.append({**row, 'replicate': replicate, 'originalRequestOrdinal': row['requestOrdinal']})
    random.Random(seed).shuffle(result)
    for i, r in enumerate(result):
        r['requestOrdinal'] = i
        r['sampleID'] = fingerprint([VERSION, r['exampleID'], r['model'], r['variant'], r['replicate']])
    return result


def prepare(source, output, selection_path, evidence_path, seed):
    assert not output.exists(), 'Use a fresh run directory'
    source = source.resolve()
    selection, evidence = load(selection_path), load(evidence_path)
    assert evidence['approved'] is True and evidence['reviewerThreadID']
    assert evidence['selectionSHA256'] == file_hash(selection_path), 'Reviewer approved a different selection'
    assert evidence['messages'], 'Actual reviewer evidence required'
    selected = selection['cases']
    assert len({r['case'] for r in selected}) == len(selected)
    assert 60 <= len(selected) <= 80, 'Requested approximately 70 target cases'
    assert all(r['reason'].strip() and r['category'] in ('prior_success', 'near_success') for r in selected)
    pointer = load(source / 'current-scoring.json')
    review = source / pointer['reviewDirectory']
    assert file_hash(review / 'scoring-revision.json') == pointer['revisionSHA256']
    ex.verify_files(source, load(source / 'completion.json')['artifactsSHA256'])
    ex.verify_files(review, load(review / 'scoring-revision.json')['artifactsSHA256'])
    frozen = source / 'frozen'
    ex.verify_files(frozen, load(frozen / 'artifact-hashes.json'))
    classes = {r['case']: r for r in rows(review / 'substantiveness.jsonl')}
    targets = {r['case']: r for r in rows(review / 'targets.jsonl')}
    selected_cases = {r['case'] for r in selected}
    assert selected_cases <= {c for c, r in classes.items() if r['substantive']}
    grades = list(rows(review / 'scored/judgments.unblinded.jsonl'))
    pass_cases = {r['case'] for r in grades if r['semanticPass']}
    assert pass_cases <= selected_cases, 'All originally successful cases must be included'
    for r in selected:
        assert (r['category'] == 'prior_success') == (r['case'] in pass_cases)
    ids = {targets[c]['exampleID'] for c in selected_cases}
    original = list(rows(frozen / 'requests.planned.jsonl'))
    requests = schedule(original, ids, seed)
    baseline = [r for r in grades if r['case'] in selected_cases]
    prompts = [r for r in rows(frozen / 'prompts.jsonl') if r['exampleID'] in ids]
    assert len(prompts) == 2 * len(ids)
    lookup = {p['promptID']: p for p in prompts}
    for r in requests:
        ex.payload(r, lookup[r['promptID']])  # exact original request body
    plan0 = load(frozen / 'plan.json')
    provider = {p: h for p, h in plan0['sourceHashes'].items() if '/llms/chatgpt/' in p}
    assert len(provider) >= 2
    ex.verify_files(Path('/'), provider)
    output.mkdir(parents=True, mode=0o700)
    (output / 'code').mkdir()
    for name in CODE:
        shutil.copy2(Path(__file__).with_name(name), output / 'code' / name)
    shutil.copyfile(selection_path, output / 'selection.json')
    shutil.copyfile(evidence_path, output / 'reviewer-selection-evidence.json')
    save_rows(output / 'requests.jsonl', requests)
    save_rows(output / 'prompts.jsonl', prompts)
    save_rows(output / 'baseline.jsonl', baseline)
    save_rows(output / 'targets.jsonl', [targets[c] for c in sorted(selected_cases)])
    proxy = copy.deepcopy(ex.PROXY)
    proxy['litellm_settings']['cache'] = False
    ex.atomic_json(output / 'proxy.json', proxy)
    sources = [source / 'current-scoring.json', source / 'completion.json',
               frozen / 'artifact-hashes.json', frozen / 'plan.json',
               frozen / 'prompts.jsonl', frozen / 'requests.planned.jsonl',
               review / 'scoring-revision.json', review / 'scored/judgments.unblinded.jsonl']
    bound = ('selection.json', 'reviewer-selection-evidence.json', 'requests.jsonl',
             'prompts.jsonl', 'baseline.jsonl', 'targets.jsonl', 'proxy.json')
    plan = {'version': VERSION, 'source': str(source), 'cases': len(ids), 'freshRequests': len(requests),
            'originalPredictions': len(baseline), 'scheduleSeed': seed, 'models': list(ex.MODELS),
            'project': str(Path.cwd().resolve()), 'pythonVersion': sys.version,
            'sourceSHA256': {str(p): file_hash(p) for p in sources},
            'artifactsSHA256': {n: file_hash(output / n) for n in bound},
            'codeSHA256': {n: file_hash(output / 'code' / n) for n in CODE},
            'runtimeSHA256': ex.runtime_hashes(Path.cwd()),
            'measurementContract': plan0['measurementContract'],
            'metrics': {'primaryDisplay': 'per-case old versus new success counts out of three; original and fresh answers separated',
                        'singleAnswerPassRate': 'passing answers / all answers',
                        'anyOfThreeSuccessRate': 'cases with at least one passing answer / cases',
                        'allThreeSuccessRate': 'cases with all three answers passing / cases',
                        'scope': 'selected-subset repeatability, not full-dataset accuracy',
                        'selectionBias': 'original successes selected; any-of-three is already true for those original arm successes'},
            'cachePolicy': {'localResponseCache': False, 'semanticInputUnchanged': True,
                            'randomizedSchedule': True, 'randomModelVisibleText': False,
                            'providerPrefixCaching': 'allowed; fresh response generation, IDs and usage audited',
                            'reference': 'https://developers.openai.com/api/docs/guides/prompt-caching'},
            'failurePolicy': {'pauseAfterConsecutiveServerErrors': 2, 'retryDelaySeconds': 10,
                              'retry': 'explicit output-free server errors only',
                              'uncertainDispatch': 'preserve evidence, no automatic replay',
                              'semanticFailure': 'retain; never retry to improve grade'},
            'requestTimeoutSeconds': 1800, 'maximumProcessTreeMiB': 4096,
            'authorization': 'User authorized two fresh attempts across the reviewer-aligned approximately 70-case common cohort.',
            'training': False, 'apiKeyFallback': False,
            'serverModelRevision': 'unverified; returned model alias and effort must match'}
    ex.atomic_json(output / 'plan.json', plan)
    print(json.dumps({'status': 'prepared', 'cases': len(ids), 'freshRequests': len(requests),
                      'planSHA256': file_hash(output / 'plan.json'), 'providerCalls': 0}))


def verify(root):
    plan = load(root / 'plan.json')
    assert plan['version'] == VERSION and sys.version == plan['pythonVersion']
    ex.verify_files(Path('/'), plan['sourceSHA256'])
    ex.verify_files(root, plan['artifactsSHA256'])
    ex.verify_files(Path(__file__).parent, plan['codeSHA256'])
    ex.verify_files(Path('/'), plan['runtimeSHA256'])
    assert load(root / 'proxy.json')['litellm_settings']['cache'] is False
    return plan


def derive(request, attempt, prices):
    result = ex.derive_result(request, attempt, prices)
    parsed = interpret((attempt / 'response.body').read_bytes(), request['model'])
    result.update(parsed)
    assert result['responseID'], 'Missing provider response identity'
    return result


def run_requests(requests, prompts, output, plan_hash, prices, timeout,
                 sender=ex.transport, sleep=time.sleep, heartbeat=lambda: None):
    """Exact sample identity; retry only proven empty server failures, once.

    The second consecutive failure persists a pause. Resume cannot reset it.
    A durable completed response is recovered without calling the provider.
    """
    output.mkdir(exist_ok=True)
    results, response_ids = [], set()
    for request in requests:
        heartbeat()
        folder = output / f"{request['requestOrdinal']:04d}"
        folder.mkdir(exist_ok=True)
        binding = {'planSHA256': plan_hash, 'request': request}
        body = ex.payload(request, prompts[request['promptID']])
        for number in (1, 2):
            attempt = folder / f'attempt-{number}'
            if attempt.exists():
                assert load(attempt / 'inflight.json')['binding'] == binding
                assert (attempt / 'transport.json').exists(), 'Uncertain dispatch; never auto-replay'
            else:
                attempt.mkdir()
                ex.atomic_json(attempt / 'inflight.json', {'binding': binding, 'beganAt': ex.now()})
                try:
                    sender(body, attempt, timeout)
                except BaseException as error:
                    ex.atomic_json(attempt / 'interruption.json', {'at': ex.now(), 'exception': type(error).__name__,
                                                               'disposition': 'uncertain; do not replay'})
                    raise
            error = supervisor.server_error(attempt)
            if error is not None:
                disposition = {'status': 'server_failure', 'consecutiveFailures': number,
                               'error': error, 'attempt': number, 'sampleID': request['sampleID']}
                if (attempt / 'disposition.json').exists():
                    assert load(attempt / 'disposition.json') == disposition
                else:
                    ex.atomic_json(attempt / 'disposition.json', disposition)
                if number == 2:
                    raise RuntimeError('Two consecutive explicit server failures; retained, paused for review')
                sleep(10)
                continue
            result = derive(request, attempt, prices)
            assert not result['providerError'] and result['responseStatus'] != 'failed', 'Non-retryable provider failure'
            assert result['responseID'] not in response_ids, 'Provider response ID reused across independent requests'
            response_ids.add(result['responseID'])
            if (folder / 'result.json').exists():
                assert load(folder / 'result.json') == result, 'Stored result differs from wire'
            else:
                ex.atomic_json(folder / 'result.json', result)
            results.append(result)
            ex.atomic_json(output.parent / 'progress.json', {'status': 'running', 'completed': len(results),
                'planned': len(requests), 'lastSampleID': request['sampleID'], 'checkedAt': ex.now(),
                'consecutiveServerFailures': 0, 'planSHA256': plan_hash})
            print(f'Recorded {len(results)}/{len(requests)}: attempt {request["replicate"]}, {request["model"]}, {request["variant"]}', flush=True)
            break
    return results


def aggregate(graded):
    groups = defaultdict(list)
    for row in graded:
        assert type(row['semanticPass']) is bool
        groups[(row['case'], row['model'], row['variant'])].append(row)
    arms = defaultdict(list)
    for (case, model, variant), triplet in groups.items():
        assert sorted(r['replicate'] for r in triplet) == [1, 2, 3], 'Exactly three distinct attempts required'
        arms[(model, variant)].append((case, sum(r['semanticPass'] for r in triplet),
                                      sum(r['semanticPass'] for r in triplet if r['replicate'] > 1)))
    assert set(arms) == {(m, v) for m in ex.MODELS for v in ('old', 'new')}, 'All four arms required'
    assert len({frozenset(c for c, _, _ in vals) for vals in arms.values()}) == 1, 'Unpaired cohort'
    return {model + ' / ' + variant: {'cases': len(vals),
        'singleAnswerPassRate': sum(n for _, n, _ in vals) / (3 * len(vals)),
        'allThreeSuccessRate': sum(n == 3 for _, n, _ in vals) / len(vals),
        'anyOfThreeSuccessRate': sum(n > 0 for _, n, _ in vals) / len(vals),
        'countAllThree': sum(n == 3 for _, n, _ in vals), 'countAnyOne': sum(n > 0 for _, n, _ in vals),
        'successCountDistribution': dict(sorted(Counter(n for _, n, _ in vals).items())),
        'freshSingleAnswerPassRate': sum(f for _, _, f in vals) / (2 * len(vals))}
        for (model, variant), vals in sorted(arms.items())}


def audit(root, plan, requests):
    found, used, failures = [], set(), []
    prices = plan['measurementContract']['pricesUSDPerMillion']
    for request in requests:
        folder = root / 'results' / f"{request['requestOrdinal']:04d}"
        if not (folder / 'result.json').exists():
            continue
        stored = load(folder / 'result.json')
        attempts = sorted(folder.glob('attempt-*'))
        assert len(attempts) in (1, 2)
        for a in attempts:
            assert load(a / 'inflight.json')['binding'] == {'planSHA256': file_hash(root / 'plan.json'), 'request': request}
        if len(attempts) == 2:
            assert supervisor.server_error(attempts[0]) is not None
            failures.append({'sampleID': request['sampleID'], 'evidence': str(attempts[0]), 'cost': 'unknown unless returned'})
        actual = derive(request, attempts[-1], prices)
        assert actual == stored and actual['responseID'] not in used
        used.add(actual['responseID']); found.append(actual)
    original_ids = {r['responseID'] for r in rows(Path(plan['source']) / 'execution-v1-final/final-output-v2/predictions.jsonl')}
    assert used.isdisjoint(original_ids), 'Original response replayed as fresh result'
    return found, {'status': 'complete' if len(found) == len(requests) else 'partial',
                   'recorded': len(found), 'planned': len(requests), 'freshResponseIDs': True,
                   'failedAttempts': failures, 'providerCallsDuringAudit': 0}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['prepare', 'run', 'audit'])
    p.add_argument('--source', type=Path); p.add_argument('--directory', required=True, type=Path)
    p.add_argument('--selection', type=Path); p.add_argument('--reviewer-evidence', type=Path)
    p.add_argument('--seed', type=int, default=20260908); p.add_argument('--authorize-plan-sha256')
    a = p.parse_args(); root = a.directory.resolve()
    if a.action == 'prepare':
        prepare(a.source, root, a.selection, a.reviewer_evidence, a.seed); return
    plan = verify(root); requests = list(rows(root / 'requests.jsonl'))
    if a.action == 'audit':
        _, report = audit(root, plan, requests); print(json.dumps(report)); return
    digest = file_hash(root / 'plan.json')
    assert a.authorize_plan_sha256 == digest, 'Exact prepared-plan authorization required'
    with (root / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        prompts = {r['promptID']: r for r in rows(root / 'prompts.jsonl')}
        try:
            with ex.owned_proxy(plan, root):
                def heartbeat():
                    rss = supervisor.memory(os.getpid())
                    assert rss < plan['maximumProcessTreeMiB'], 'Memory guard reached'
                run_requests(requests, prompts, root / 'results', digest,
                    plan['measurementContract']['pricesUSDPerMillion'], plan['requestTimeoutSeconds'], heartbeat=heartbeat)
            found, report = audit(root, plan, requests)
            assert report['status'] == 'complete'
            if not (root / 'predictions.jsonl').exists(): save_rows(root / 'predictions.jsonl', found)
            else: assert list(rows(root / 'predictions.jsonl')) == found
            report['predictionsSHA256'] = file_hash(root / 'predictions.jsonl')
            ex.atomic_json(root / 'audit.json', report)
            ex.atomic_json(root / 'progress.json', {**report, 'status': 'complete_pending_semantic_review', 'finishedAt': ex.now()})
        except BaseException as e:
            pause = {'status': 'paused', 'reason': str(e), 'at': ex.now()}
            ex.atomic_json(root / 'pause.json', pause)
            prior = load(root / 'progress.json') if (root / 'progress.json').exists() else {}
            ex.atomic_json(root / 'progress.json', {**prior, **pause})
            raise


if __name__ == '__main__':
    main()

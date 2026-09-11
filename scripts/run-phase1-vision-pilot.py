#!/usr/bin/env python3
"""Frozen, sequential subscription executor for the approved recent-READ pilot.

Prepare/audit are local. Run requires the exact frozen execution-plan hash.
Requests are persisted before dispatch; completed transports resume offline.
No model/API fallback, response cache, training, or semantic-failure resampling.
"""
import argparse
import copy
import fcntl
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import threading
import time

from phase1_read_model_comparison import canonical, file_hash, fingerprint, rows
from phase1_subscription_output import interpret


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


ex = module('vision_transport', 'run-phase1-read-model-comparison.py')
failure = module('vision_failure', 'supervise-phase1-read-model-expansion.py')
prep = module('vision_preparation', 'prepare-phase1-vision-pilot.py')
VERSION = 'phase1-vision-executor-v3'
APPROVED_DATA = '8300d0175cf371009f5167bd9de52cc699aec1b93add7980e2e3f5233c817501'
MODEL = 'chatgpt/gpt-6-astra'
CODE = ('run-phase1-vision-pilot.py', 'run-phase1-read-model-comparison.py',
        'phase1_subscription_output.py', 'phase1_read_model_comparison.py',
        'supervise-phase1-read-model-expansion.py', 'prepare-phase1-vision-pilot.py',
        'phase1_repeatability_review.py', 'check-phase1-vision-executor.py',
        'prepare-phase1-vision-budget.py', 'check-phase1-vision-budget.py',
        'prepare-phase1-vision-budget-v2.py', 'phase1_vision_raw_history.py',
        'check-phase1-vision-budget-v2.py', 'finalize-phase1-vision-budget.py',
        'watch-phase1-vision-budget.py', 'analyze-phase1-vision.py',
        'check-phase1-vision-analysis.py', 'check-phase1-vision-budget-e2e.py')
PRICES = {MODEL: {'input': 10, 'cachedInput': 1, 'cacheWriteInput': 12.5, 'output': 50}}


def load(path):
    return json.loads(Path(path).read_text())


def save_rows(path, values):
    with path.open('x') as out:
        os.chmod(path, 0o600)
        for value in values:
            out.write(canonical(value) + '\n')
        out.flush()
        os.fsync(out.fileno())
    ex.sync_dir(path.parent)


def validate_data(source, expected_plan_sha=APPROVED_DATA):
    assert file_hash(source / 'plan.json') == expected_plan_sha, 'Unapproved preparation'
    data_plan = load(source/'plan.json')
    assert data_plan['version'] in ('phase1-recent-read-vision-pilot-v3','phase1-vision-budget-v1','phase1-vision-budget-v2')
    corrected = data_plan['version'] == 'phase1-vision-budget-v2'
    variants = ('cleaned_recent','raw_ocr_recent') if corrected else ('cleaned_recent','raw_ocr_recent','screenshots_recent')
    ex.verify_files(source, load(source / 'artifact-hashes.json'))
    requests = list(rows(source / 'requests.proposed.jsonl'))
    prompts = {p['partsSHA256']: p for p in rows(source / 'prompts.local.jsonl')}
    assert len(requests) == len(prompts) == 69*len(variants)
    cases = {r['case'] for r in requests}
    assert len(cases) == 69 and cases.isdisjoint({118, 126, 127, 353, 354})
    assert [r['requestOrdinal'] for r in requests] == list(range(1, len(requests)+1))
    assert {(r['case'], r['variant']) for r in requests} == {
        (c, v) for c in cases for v in variants}
    for r in requests:
        p = prompts[r['partsSHA256']]
        assert r['model'] == MODEL and r['replicate'] == 1
        assert all(r[k] == p[k] for k in ('case', 'variant', 'exampleID', 'partsSHA256'))
        assert fingerprint(p['parts']) == p['partsSHA256']
        if corrected:
            assert p['imageCount'] == 0 and all(t['type']=='input_text' for t in p['parts'])
        if r.get('reuse'):
            assert corrected and r['variant']=='cleaned_recent'
            reused_result(r, PRICES)
    return requests


def prepare(source, root, validation_path, prepared_hash=APPROVED_DATA):
    assert not root.exists(), 'Use a fresh execution directory'
    requests = validate_data(source, prepared_hash)
    validation = load(validation_path)
    assert validation['status'] == 'passed' and validation['networkCalls'] == 0
    assert validation['preparedPlanSHA256'] == prepared_hash
    assert validation['largestRealRequest']['completedTransportResumedWithoutResend'] is True
    corrected = load(source/'plan.json')['version']=='phase1-vision-budget-v2'
    assert validation['largestRealRequest']['imageCount'] == (0 if corrected else 60)
    assert validation['codeSHA256'] == {n: file_hash(Path(__file__).with_name(n)) for n in CODE}
    root.mkdir(parents=True, mode=0o700)
    (root / 'code').mkdir(mode=0o700)
    (root / 'data').mkdir(mode=0o700)
    for name in CODE:
        shutil.copyfile(Path(__file__).with_name(name), root / 'code' / name)
    for name in load(source / 'artifact-hashes.json'):
        shutil.copyfile(source / name, root / 'data' / name)
    shutil.copyfile(source / 'artifact-hashes.json', root / 'data/artifact-hashes.json')
    shutil.copyfile(validation_path, root / 'executor-validation.json')
    save_rows(root / 'requests.jsonl', [{**r, 'sampleID': fingerprint([prepared_hash, r])} for r in requests])
    proxy = copy.deepcopy(ex.PROXY)
    proxy['model_list'] = [{'model_name': MODEL, 'litellm_params': {'model': MODEL}}]
    proxy['litellm_settings']['cache'] = False
    ex.atomic_json(root / 'proxy.json', proxy)
    artifacts = ['requests.jsonl', 'proxy.json', 'data/artifact-hashes.json', 'executor-validation.json']
    artifacts += ['data/' + n for n in load(source / 'artifact-hashes.json')]
    interrupted_prior=[]
    if corrected:
        prior=Path(load(source/'plan.json')['supersededHybridRun'])
        wanted={r['partsSHA256'] for r in requests if not r.get('reuse')}
        for r in rows(prior/'requests.jsonl'):
            folder=prior/'results'/f"{r['requestOrdinal']:04d}"
            if r['partsSHA256'] in wanted and not (folder/'result.json').exists() and list(folder.glob('attempt-*/inflight.json')):
                interrupted_prior.append({'case':r['case'],'variant':r['variant'],'partsSHA256':r['partsSHA256'],
                    'sourceRun':str(prior),'sourceRequestOrdinal':r['requestOrdinal'],
                    'sourcePlanSHA256':file_hash(prior/'plan.json'),'reason':'User pause interrupted prior transport; separate one-resend authorization required'})
    plan = {
        'version': VERSION, 'project': str(Path.cwd().resolve()), 'source': str(source),
        'preparedPlanSHA256': prepared_hash, 'cases': 69, 'planned': len(requests),
        'newProviderRequests':sum(not r.get('reuse') for r in requests),
        'reusedResults':sum(bool(r.get('reuse')) for r in requests),
        'interruptedPriorRequests':interrupted_prior,
        'artifactsSHA256': {n: file_hash(root / n) for n in artifacts},
        'codeSHA256': {n: file_hash(root / 'code' / n) for n in CODE},
        'runtimeSHA256': ex.runtime_hashes(Path.cwd()), 'pythonVersion': sys.version,
        'provider': {'model': MODEL, 'reasoningEffort': 'xhigh', 'temperature': 'omitted',
                     'endpoint': ex.ENDPOINT, 'tools': [], 'stream': True, 'detail': 'original',
                     'apiKeyFallback': False, 'localResponseCache': False},
        'authorization': ('User approved correcting the comparison to expanded cleaned text and expanded raw OCR, reusing unchanged completed clean results and the original screenshot baseline. No screenshot retransmission.' if corrected else 'Approved original three-arm subscription comparison.'),
        'excludedCases': [118, 126, 127], 'training': False,
        'failurePolicy': {'consecutiveServerFailuresBeforePause': 2, 'retryDelaySeconds': 10,
                          'retry': 'only explicit output-free 5xx, once',
                          'uncertainDispatch': 'pause; never automatically replay',
                          'invalidGeneration': 'retain for scoring, continue; never resample'},
        'requestTimeoutSeconds': 1800, 'maximumProcessTreeMiB': 4096,
        'pricesUSDPerMillion': PRICES,
        'pricing': {'source': 'https://developers.openai.com/api/docs/models/gpt-6-astra',
                    'verifiedOn': '2026-09-09', 'longInputThresholdTokens': 272000,
                    'longInputMultiplier': 2, 'longOutputMultiplier': 1.5,
                    'basis': 'API-equivalent only, not subscription billing; images included in returned input usage'},
        'scope': load(source / 'plan.json')['hypothesisScope'],
        'scoringContract': load(source / 'plan.json')['scoringContract'],
    }
    assert plan['codeSHA256'] == validation['codeSHA256'], 'Code changed during freeze'
    ex.atomic_json(root / 'plan.json', plan)
    for p in root.rglob('*'):
        if p.is_file():
            os.chmod(p, 0o600)
    print(canonical({'status': 'frozen', 'planSHA256': file_hash(root / 'plan.json'), 'providerCalls': 0}))


def verify(root):
    plan = load(root / 'plan.json')
    assert plan['version'] == VERSION and plan['pythonVersion'] == sys.version
    assert Path(__file__).resolve().parent == root / 'code', 'Run the isolated frozen executor'
    ex.verify_files(root, plan['artifactsSHA256'])
    ex.verify_files(root / 'code', plan['codeSHA256'])
    ex.verify_files(Path('/'), plan['runtimeSHA256'])
    validate_data(root / 'data', plan['preparedPlanSHA256'])
    return plan


def persist_request(folder, prompt):
    """At most one real-sized base64 request in memory; deterministic gzip spool."""
    body = prep.wire_payload(prompt)
    encoded = canonical(body).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    path, pending = folder / 'request.json.gz', folder / 'request.pending'
    if not path.exists():
        # No dispatch can have occurred before this spool is committed.
        assert not list(folder.glob('attempt-*')), 'Missing request evidence after dispatch'
        with pending.open('wb') as out:
            os.chmod(pending, 0o600)
            with gzip.GzipFile(filename='', mode='wb', fileobj=out, mtime=0, compresslevel=1) as zipped:
                zipped.write(encoded)
            out.flush()
            os.fsync(out.fileno())
        os.replace(pending, path)
        ex.sync_dir(folder)
    else:
        # Compare bounded chunks without allocating a second full request.
        h = hashlib.sha256()
        with gzip.open(path, 'rb') as inp:
            for chunk in iter(lambda: inp.read(1024 * 1024), b''):
                h.update(chunk)
        assert h.hexdigest() == digest, 'Saved wire request differs from frozen prompt'
    metadata = {'bodySHA256': digest, 'bodyBytes': len(encoded), 'spoolSHA256': file_hash(path),
                'imageCount': prompt['imageCount'], 'partsSHA256': prompt['partsSHA256']}
    if (folder / 'request.json').exists():
        assert load(folder / 'request.json') == metadata
    else:
        ex.atomic_json(folder / 'request.json', metadata)
    return body, metadata


def derive(request, attempt, prices):
    result = ex.derive_result(request, attempt, prices)
    result.update(interpret((attempt / 'response.body').read_bytes(), request['model']))
    assert result['responseID'], 'Missing response identity'
    u, rate = result['usage'], prices[request['model']]
    cached = u.get('input_tokens_details', {}).get('cached_tokens')
    written = u.get('input_tokens_details', {}).get('cache_write_tokens', 0)
    assert type(written) is int and 0 <= written <= u['input_tokens']
    assert cached is None or cached + written <= u['input_tokens']
    long = u['input_tokens'] > 272000
    im, om = (2, 1.5) if long else (1, 1)
    result['apiEquivalentCostUSD'] = None if cached is None else (
        ((u['input_tokens'] - cached - written) * rate['input'] + cached * rate['cachedInput']
         + written * rate['cacheWriteInput']) * im + u['output_tokens'] * rate['output'] * om) / 1e6
    result['apiEquivalentBaseRateUncachedEstimateUSD'] = (
        u['input_tokens'] * rate['input'] + u['output_tokens'] * rate['output']) / 1e6
    result['costBasis'] = 'frozen API-equivalent rates; images included in input usage, reasoning included in output; not subscription billing'
    result['costCaveat'] = 'Cached input usage absent' if cached is None else None
    result['longContextPricingApplied'] = long
    result['nonemptyPrediction'] = bool(result['prediction'].strip())
    return result


def reused_result(request, prices):
    """Reuse immutable transport evidence, never an unverified prediction copy."""
    ref=request['reuse'];source=Path(ref['sourceRun'])
    assert file_hash(source/'plan.json')==ref['sourcePlanSHA256']
    plan=load(source/'plan.json')
    assert plan['provider']['model']==request['model'] and plan['provider']['reasoningEffort']=='xhigh'
    assert plan['provider']['temperature']=='omitted' and plan['provider']['tools']==[]
    source_requests=list(rows(source/'requests.jsonl'))
    original=next(r for r in source_requests if r['requestOrdinal']==ref['sourceRequestOrdinal'])
    assert not original.get('reuse'), 'No recursive reuse chain'
    assert all(original[k]==request[k] for k in ('case','exampleID','variant','partsSHA256','model','replicate'))
    folder=source/'results'/f"{original['requestOrdinal']:04d}"
    assert file_hash(folder/'result.json')==ref['sourceResultSHA256']
    assert file_hash(source/'requests.jsonl')==plan['artifactsSHA256']['requests.jsonl']
    assert file_hash(source/'data/prompts.local.jsonl')==plan['artifactsSHA256']['data/prompts.local.jsonl']
    prompt=next(p for p in rows(source/'data/prompts.local.jsonl') if p['partsSHA256']==request['partsSHA256'])
    assert fingerprint(prompt['parts'])==request['partsSHA256']
    found,report=audit(source,[original],ref['sourcePlanSHA256'],prices)
    assert report['status']=='complete' and len(found)==1
    return {**found[0],**request,'reusedFrom':{**ref,'sourceSampleID':original['sampleID'],
            'providerCallsForReuse':0}}


def run_requests(requests, prompts, root, plan_hash, prices=PRICES, timeout=1800,
                 sender=ex.transport, sleep=time.sleep, heartbeat=lambda: None, limit=None):
    output = root / 'results'
    output.mkdir(exist_ok=True, mode=0o700)
    results, ids = [], set()
    for request in requests:
        heartbeat()
        folder = output / f"{request['requestOrdinal']:04d}"
        folder.mkdir(exist_ok=True, mode=0o700)
        if request.get('reuse'):
            result=reused_result(request,prices)
            binding={'planSHA256':plan_hash,'request':request}
            if (folder/'reuse.json').exists(): assert load(folder/'reuse.json')==binding
            else: ex.atomic_json(folder/'reuse.json',binding)
            if (folder/'result.json').exists(): assert load(folder/'result.json')==result
            else: ex.atomic_json(folder/'result.json',result)
            assert result['responseID'] not in ids
            ids.add(result['responseID']);results.append(result)
            ex.atomic_json(root/'progress.json',{'status':'running','completed':len(results),'planned':len(requests),
                'lastSampleID':request['sampleID'],'lastVariant':request['variant'],'checkedAt':ex.now(),'planSHA256':plan_hash})
            print(f"Reused {len(results)}/{len(requests)}: case {request['case']}, {request['variant']}",flush=True)
            if limit is not None and len(results)>=limit: break
            continue
        body, metadata = persist_request(folder, prompts[request['partsSHA256']])
        binding = {'planSHA256': plan_hash, 'request': request, 'wire': metadata}
        for number in (1, 2):
            attempt = folder / f'attempt-{number}'
            if attempt.exists():
                assert load(attempt / 'inflight.json')['binding'] == binding
                assert (attempt / 'transport.json').exists(), 'Uncertain dispatch; never auto-replay'
            else:
                attempt.mkdir(mode=0o700)
                ex.atomic_json(attempt / 'inflight.json', {'binding': binding, 'beganAt': ex.now()})
                try:
                    sender(body, attempt, timeout)
                except BaseException as error:
                    ex.atomic_json(attempt / 'interruption.json', {'at': ex.now(), 'exception': type(error).__name__,
                                                                 'disposition': 'uncertain; do not replay'})
                    raise
            error = failure.server_error(attempt)
            if error is not None:
                disposition = {'status': 'server_failure', 'consecutiveFailures': number, 'error': error}
                if (attempt / 'disposition.json').exists():
                    assert load(attempt / 'disposition.json') == disposition
                else:
                    ex.atomic_json(attempt / 'disposition.json', disposition)
                if number == 2:
                    raise RuntimeError('Two consecutive explicit server failures; paused with evidence')
                sleep(10)
                continue
            result = {**derive(request, attempt, prices), 'requestEvidence': metadata, 'selectedAttempt': number}
            assert not result['providerError'] and result['responseStatus'] != 'failed', 'Non-retryable provider error'
            assert result['responseID'] not in ids, 'Provider response ID reused'
            ids.add(result['responseID'])
            if (folder / 'result.json').exists():
                assert load(folder / 'result.json') == result, 'Stored output differs from wire'
            else:
                ex.atomic_json(folder / 'result.json', result)
            results.append(result)
            ex.atomic_json(root / 'progress.json', {'status': 'running', 'completed': len(results),
                'planned': len(requests), 'lastSampleID': request['sampleID'], 'lastVariant': request['variant'],
                'checkedAt': ex.now(), 'planSHA256': plan_hash})
            print(f"Recorded {len(results)}/{len(requests)}: case {request['case']}, {request['variant']}", flush=True)
            break
        del body
        if limit is not None and len(results) >= limit:
            break
    return results


def audit(root, requests, plan_hash, prices):
    results, ids, failed = [], set(), []
    for request in requests:
        folder = root / 'results' / f"{request['requestOrdinal']:04d}"
        if not (folder / 'result.json').exists():
            continue
        stored = load(folder / 'result.json')
        if request.get('reuse'):
            assert load(folder/'reuse.json')=={'planSHA256':plan_hash,'request':request}
            actual=reused_result(request,prices)
            assert stored==actual and actual['responseID'] not in ids
            ids.add(actual['responseID']);results.append(actual)
            continue
        metadata = load(folder / 'request.json')
        assert file_hash(folder / 'request.json.gz') == metadata['spoolSHA256']
        assert stored['requestEvidence'] == metadata
        number = stored['selectedAttempt']
        assert number in (1, 2)
        assert len(list(folder.glob('attempt-*'))) == number
        for n in range(1, number + 1):
            attempt = folder / f'attempt-{n}'
            assert load(attempt / 'inflight.json')['binding'] == {
                'planSHA256': plan_hash, 'request': request, 'wire': metadata}
            if n < number:
                assert failure.server_error(attempt) is not None
                failed.append(str(attempt))
        actual = {**derive(request, attempt, prices), 'requestEvidence': metadata, 'selectedAttempt': number}
        assert actual == stored and actual['responseID'] not in ids
        ids.add(actual['responseID'])
        results.append(actual)
    return results, {'status': 'complete' if len(results) == len(requests) else 'partial',
                     'completed': len(results), 'planned': len(requests),
                     'failedAttempts': failed, 'failedAttemptCost': 'unknown unless usage returned',
                     'responseIDsUnique': True, 'providerCallsDuringAudit': 0}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('prepare', 'run', 'audit'))
    p.add_argument('--source', type=Path)
    p.add_argument('--validation-report', type=Path)
    p.add_argument('--directory', required=True, type=Path)
    p.add_argument('--authorize-plan-sha256')
    p.add_argument('--prepared-plan-sha256',default=APPROVED_DATA)
    p.add_argument('--limit', type=int)
    p.add_argument('--authorize-interrupted-case',type=int,action='append',default=[])
    a = p.parse_args()
    root = a.directory.resolve()
    if a.action == 'prepare':
        assert a.validation_report, 'Bound offline executor-validation report required'
        prepare(a.source.resolve(), root, a.validation_report.resolve(),a.prepared_plan_sha256)
        return
    plan = verify(root)
    digest = file_hash(root / 'plan.json')
    requests = list(rows(root / 'requests.jsonl'))
    if a.action == 'audit':
        _, report = audit(root, requests, digest, plan['pricesUSDPerMillion'])
        print(canonical(report))
        return
    assert a.authorize_plan_sha256 == digest, 'Exact frozen-plan authorization required'
    required={r['case'] for r in plan.get('interruptedPriorRequests',[])}
    auth_path=root/'prior-interruption-authorization.json'
    authorization={'planSHA256':digest,'cases':sorted(required),'scope':'One replacement request per listed prior interrupted case; no silent retries'}
    if auth_path.exists():assert load(auth_path)==authorization
    elif required:
        assert required<=set(a.authorize_interrupted_case), 'Explicit user authorization needed for prior interrupted case(s): '+str(sorted(required))
        ex.atomic_json(auth_path,authorization)
    stop = threading.Event()
    def interrupted(signum, frame):
        raise RuntimeError(f'Interrupted by signal {signum}; preserve uncertain dispatch')
    signal.signal(signal.SIGTERM, interrupted)
    with (root / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ex.atomic_json(root / 'process.json', {'pid': os.getpid(), 'startedAt': ex.now(), 'planSHA256': digest})
        def monitor():
            peak = 0
            while not stop.wait(5):
                try:
                    rss = failure.memory(os.getpid())
                    peak = max(peak, rss)
                    ex.atomic_json(root / 'resources.json', {'checkedAt': ex.now(), 'processTreeMiB': rss,
                        'peakObservedProcessTreeMiB': peak, 'limitMiB': plan['maximumProcessTreeMiB']})
                    if rss > plan['maximumProcessTreeMiB']:
                        raise RuntimeError('Process-tree memory guard reached')
                except Exception as error:
                    ex.atomic_json(root / 'resource-guard.json', {'at': ex.now(), 'error': str(error)})
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
        threading.Thread(target=monitor, daemon=True).start()
        try:
            prompts = {p['partsSHA256']: p for p in rows(root / 'data/prompts.local.jsonl')}
            with ex.owned_proxy(plan, root):
                run_requests(requests, prompts, root, digest, plan['pricesUSDPerMillion'],
                             plan['requestTimeoutSeconds'], limit=a.limit)
            found, report = audit(root, requests, digest, plan['pricesUSDPerMillion'])
            if report['status'] == 'complete':
                if (root / 'predictions.jsonl').exists():
                    assert list(rows(root / 'predictions.jsonl')) == found
                else:
                    save_rows(root / 'predictions.jsonl', found)
                report['predictionsSHA256'] = file_hash(root / 'predictions.jsonl')
            ex.atomic_json(root / 'audit.json', report)
            ex.atomic_json(root / 'progress.json', {**report, 'planSHA256': digest, 'checkedAt': ex.now(),
                'status': 'complete_pending_semantic_review' if report['status'] == 'complete' else 'controlled_checkpoint'})
        except BaseException as error:
            pause = {'status': 'paused', 'reason': str(error), 'at': ex.now()}
            ex.atomic_json(root / 'pause.json', pause)
            prior = load(root / 'progress.json') if (root / 'progress.json').exists() else {}
            ex.atomic_json(root / 'progress.json', {**prior, **pause})
            raise
        finally:
            stop.set()


if __name__ == '__main__':
    main()

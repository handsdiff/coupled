#!/usr/bin/env python3
"""Offline executor regression gates, optionally including the largest real request.

Never launches a provider or reads credentials. Personal input stays local.
"""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import resource
import shutil
import tempfile
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('vision', Path(__file__).with_name('run-phase1-vision-pilot.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def rejected(fn):
    try:
        fn()
    except (AssertionError, RuntimeError, ValueError, OSError, KeyError):
        return
    raise AssertionError('Unsafe operation accepted')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--real-data', type=Path)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--prepared-plan-sha256',default=m.APPROVED_DATA)
    args = parser.parse_args()
    prompt = {'parts': [{'type': 'input_text', 'text': 'Synthetic local fixture'}], 'imageCount': 0}
    prompt['partsSHA256'] = m.fingerprint(prompt['parts'])
    request = {'partsSHA256': prompt['partsSHA256'], 'model': m.MODEL, 'case': 1,
               'requestOrdinal': 1, 'sampleID': 'fixture1', 'exampleID': 'fixture',
               'replicate': 1, 'variant': 'cleaned_recent'}
    count = 0
    def persist(attempt, wire, status=200):
        with (attempt / 'response.body').open('xb') as f:
            f.write(wire)
        m.ex.atomic_json(attempt / 'transport.json', {'httpStatus': status,
            'bodySHA256': m.file_hash(attempt / 'response.body'), 'dispatchToCompletionSeconds': 1.25})
    def success(body, attempt, timeout, invalid=False):
        nonlocal count
        count += 1
        binding = m.load(attempt / 'inflight.json')['binding']
        assert m.fingerprint(body) == binding['wire']['bodySHA256']
        assert m.file_hash(attempt.parent / 'request.json.gz') == binding['wire']['spoolSHA256']
        response = {'id': f'response-{count}', 'model': body['model'], 'reasoning': {'effort': 'xhigh'},
            'status': 'completed', 'output': [],
            'usage': {'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110,
                      'input_tokens_details': {'cached_tokens': 20},
                      'output_tokens_details': {'reasoning_tokens': 5}}}
        content = {'type': 'refusal', 'refusal': 'synthetic refusal'} if invalid else {
            'type': 'output_text', 'text': 'Decoded from complete output-item, not empty wrapper'}
        # Real observed LiteLLM shape: completed item then empty terminal wrapper.
        events = [{'type': 'response.output_item.done', 'output_index': 0,
                   'item': {'type': 'message', 'id': 'msg1', 'content': [content]}},
                  {'type': 'response.completed', 'response': response}]
        wire = b''.join(b'data: ' + json.dumps(e).encode() + b'\n\n' for e in events)
        persist(attempt, wire)
    def server_error(body, attempt, timeout):
        nonlocal count
        count += 1
        persist(attempt, b'{"error":{"code":500,"message":"synthetic empty failure"}}', 500)
    def uncertain(body, attempt, timeout):
        nonlocal count
        count += 1
        raise OSError('Synthetic connection lost')
    def forbidden(*args, **kwargs):
        raise AssertionError('Should recover locally, not send again')
    with tempfile.TemporaryDirectory(prefix='coupled-vision-executor-') as temp, \
            patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('Network forbidden')), \
            patch('subprocess.Popen', side_effect=AssertionError('Child processes forbidden')):
        root = Path(temp)
        def run(name, sender=success, req=request, p=prompt):
            path = root / name
            path.mkdir(exist_ok=True)
            return m.run_requests([req], {p['partsSHA256']: p}, path, 'frozen-hash',
                                  sender=sender, sleep=lambda _: None)
        first = run('complete')
        assert first[0]['prediction'].startswith('Decoded from complete output-item')
        assert first[0]['responseInterpretation']['source'] == 'sse_output_item_done'
        calls = count
        assert run('complete', forbidden) == first and count == calls
        (root / 'complete/results/0001/result.json').unlink()
        assert run('complete', forbidden) == first and count == calls
        found, audit = m.audit(root / 'complete', [request], 'frozen-hash', m.PRICES)
        assert found == first and audit['status'] == 'complete'
        # Corrected comparison reuses saved clean transports under exact prompt,
        # model and evidence bindings; it must never dispatch them again.
        source=root/'reuse-source';shutil.copytree(root/'complete',source)
        (source/'data').mkdir()
        m.save_rows(source/'requests.jsonl',[request])
        m.save_rows(source/'data/prompts.local.jsonl',[prompt])
        source_plan={'provider':{'model':m.MODEL,'reasoningEffort':'xhigh','temperature':'omitted','tools':[]},
            'artifactsSHA256':{n:m.file_hash(source/n) for n in ('requests.jsonl','data/prompts.local.jsonl')}}
        m.ex.atomic_json(source/'plan.json',source_plan)
        source_hash=m.file_hash(source/'plan.json')
        marker=source/'results/0001/attempt-1/inflight.json'
        value=m.load(marker);value['binding']['planSHA256']=source_hash;m.ex.atomic_json(marker,value)
        reuse={**request,'sampleID':'reused-new-logical-id','reuse':{'sourceRun':str(source),
            'sourceRequestOrdinal':1,'sourceResultSHA256':m.file_hash(source/'results/0001/result.json'),
            'sourcePlanSHA256':source_hash}}
        calls=count
        reused=run('reuse-destination',forbidden,reuse,prompt)
        assert count==calls and reused[0]['responseID']==first[0]['responseID']
        assert reused[0]['reusedFrom']['providerCallsForReuse']==0
        assert run('reuse-destination',forbidden,reuse,prompt)==reused
        assert m.audit(root/'reuse-destination',[reuse],'frozen-hash',m.PRICES)[0]==reused
        wrong=copy.deepcopy(reuse);wrong['partsSHA256']='different-prompt'
        rejected(lambda:m.reused_result(wrong,m.PRICES))
        wrong=copy.deepcopy(reuse);wrong['reuse']['sourceResultSHA256']='tampered'
        rejected(lambda:m.reused_result(wrong,m.PRICES))
        # Price high-resolution image requests using actual returned tokens, with
        # cache writes and long-context multipliers rather than an image estimate.
        first_result = copy.deepcopy(first[0])
        first_result['usage'] = {'input_tokens': 300000, 'output_tokens': 1000,
            'total_tokens': 301000, 'input_tokens_details': {'cached_tokens': 10000, 'cache_write_tokens': 1000}}
        attempt = root / 'complete/results/0001/attempt-1'
        with patch.object(m.ex, 'derive_result', return_value=first_result):
            priced = m.derive(request, attempt, m.PRICES)
            expected = ((289000 * 10 + 10000 + 1000 * 12.5) * 2 + 1000 * 50 * 1.5) / 1e6
            assert priced['apiEquivalentCostUSD'] == expected and priced['longContextPricingApplied']
        def isolated(body, attempt, timeout):
            if attempt.name == 'attempt-1':
                server_error(body, attempt, timeout)
            else:
                success(body, attempt, timeout)
        assert run('isolated', isolated)[0]['selectedAttempt'] == 2
        calls = count
        rejected(lambda: run('consecutive', server_error))
        assert count == calls + 2
        calls = count
        rejected(lambda: run('consecutive', forbidden))
        assert count == calls, 'Resume must not reset consecutive-error gate'
        rejected(lambda: run('uncertain', uncertain))
        calls = count
        rejected(lambda: run('uncertain', forbidden))
        assert count == calls
        path = root / 'invalid'; path.mkdir()
        second_request = {**request, 'requestOrdinal': 2, 'sampleID': 'fixture2'}
        results = m.run_requests([request, second_request], {prompt['partsSHA256']: prompt}, path,
            'frozen-hash', sender=lambda b, a, t: success(b, a, t, invalid=a.parent.name == '0001'))
        assert len(results) == 2 and not results[0]['validCompletion'] and results[1]['validCompletion']
        run('tamper')
        meta = root / 'tamper/results/0001/request.json'
        value = m.load(meta); value['bodySHA256'] = 'tampered'
        m.ex.atomic_json(meta, value)
        rejected(lambda: run('tamper', forbidden))
        run('wire-tamper')
        wire = root / 'wire-tamper/results/0001/attempt-1/response.body'
        wire.write_bytes(b'corrupt')
        rejected(lambda: run('wire-tamper', forbidden))
        # Long-input and cache-write pricing retained, not silently base-priced.
        rates = m.PRICES[m.MODEL]
        assert rates == {'input': 10, 'cachedInput': 1, 'cacheWriteInput': 12.5, 'output': 50}
        report = {'status': 'passed', 'networkCalls': 0, 'mockDispatches': count,
                  'gates': ['complete-stream decoding', 'save before dispatch', 'completed resume without resend',
                            'crash after transport recovered', 'single server error retried',
                            'consecutive server errors persist pause', 'uncertain dispatch never repeated',
                            'invalid generation retained without blocking next request', 'evidence tampering rejected',
                            'unchanged completed clean result reused without dispatch', 'reuse prompt and source tampering rejected']}
        if args.real_data:
            m.validate_data(args.real_data,args.prepared_plan_sha256)
            prompts = list(m.rows(args.real_data / 'prompts.local.jsonl'))
            # Match actual encoded size; text and PNG base64 are the only varying payload parts.
            def size(p):
                return sum(len(x.get('text', '').encode()) if x['type'] == 'input_text' else
                           ((Path(x['path']).stat().st_size + 2) // 3) * 4 for x in p['parts'])
            largest = max(prompts, key=size)
            r = next(r for r in m.rows(args.real_data / 'requests.proposed.jsonl')
                     if r['partsSHA256'] == largest['partsSHA256'])
            r = {**r, 'sampleID': 'real-sized-local-fixture'}
            real = run('real-sized', req=r, p=largest)
            folder = root / f"real-sized/results/{r['requestOrdinal']:04d}"
            metadata = m.load(folder / 'request.json')
            (folder / 'result.json').unlink()
            calls = count
            assert run('real-sized', forbidden, r, largest) == real and count == calls
            found, audit = m.audit(root / 'real-sized', [r], 'frozen-hash', m.PRICES)
            assert found == real and audit['status'] == 'complete'
            report['largestRealRequest'] = {'case': r['case'], **metadata, 'providerCalls': 0,
                'completedTransportResumedWithoutResend': True}
        report['peakProcessMiB'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)
        assert report['peakProcessMiB'] < 1024, 'Offline executor exceeds 1 GiB'
        report['codeSHA256'] = {n: m.file_hash(Path(__file__).with_name(n)) for n in m.CODE}
        report['preparedPlanSHA256'] = m.file_hash(args.real_data / 'plan.json') if args.real_data else None
        if args.report:
            m.ex.atomic_json(args.report, report)
        print(m.canonical(report))


if __name__ == '__main__':
    main()

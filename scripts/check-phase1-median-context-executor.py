#!/usr/bin/env python3
"""No-network regression and full-sized save/recovery test for corrected inputs."""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import resource
import tempfile
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('median_runner', Path(__file__).with_name('run-phase1-median-context.py'))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)


def rejected(fn):
    try: fn()
    except (AssertionError, RuntimeError, ValueError, OSError, KeyError): return
    raise AssertionError('Unsafe operation accepted')


def check(data=None):
    count = 0
    def success(body, attempt, timeout, invalid=False):
        nonlocal count
        count += 1
        assert 'truncation' not in body and 'context_management' not in body and not body['tools']
        assert 'max_output_tokens' not in body and 'temperature' not in body
        wire = {'id': f'resp-{count}', 'model': m.legacy.MODEL, 'status': 'completed',
                'reasoning': {'effort': 'xhigh'}, 'output': [],
                'usage': {'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110,
                          'input_tokens_details': {'cached_tokens': 0},
                          'output_tokens_details': {'reasoning_tokens': 5}}}
        content = {'type': 'refusal', 'refusal': 'synthetic'} if invalid else {'type': 'output_text', 'text': 'recovered output'}
        stream = [{'type': 'response.output_item.done', 'output_index': 0,
                   'item': {'type': 'message', 'id': 'msg', 'content': [content]}},
                  {'type': 'response.completed', 'response': wire}]
        encoded = b''.join(b'data: ' + json.dumps(x).encode() + b'\n\n' for x in stream)
        (attempt / 'response.body').write_bytes(encoded)
        m.ex.atomic_json(attempt / 'transport.json', {'httpStatus': 200,
            'bodySHA256': m.file_hash(attempt / 'response.body'), 'dispatchToCompletionSeconds': 1.25})
    def forbidden(*args, **kwargs): raise AssertionError('Unexpected network/resend')
    p = {'case': 1, 'arm': 'time_ocr', 'imageCount': 0, 'limits': [],
         'parts': [{'type': 'input_text', 'text': 'synthetic'}]}
    p['partsSHA256'] = m.fingerprint(p['parts'])
    r = {'case': 1, 'arm': 'time_ocr', 'variant': 'time_ocr', 'model': m.legacy.MODEL,
         'exampleID': 'example', 'requestOrdinal': 1, 'sampleID': 'sample', 'replicate': 1,
         'partsSHA256': p['partsSHA256'], 'payload': 'prompt.json', 'limits': []}
    with tempfile.TemporaryDirectory(prefix='coupled-median-executor-') as td, \
            patch('socket.socket.connect', side_effect=forbidden), \
            patch('urllib.request.OpenerDirector.open', side_effect=forbidden), \
            patch('subprocess.Popen', side_effect=forbidden):
        root = Path(td); (root / 'data').mkdir(); m.ex.atomic_json(root / 'data/prompt.json', p)
        first = m.execute_one(root, r, 'digest', sender=success)
        assert first['prediction'] == 'recovered output'
        assert m.execute_one(root, r, 'digest', sender=forbidden) == first
        folder = root / 'results/0001'; (folder / 'result.json').unlink()
        assert m.execute_one(root, r, 'digest', sender=forbidden) == first
        assert count == 1
        # Invalid model completion does not terminate the sequence.
        r2 = dict(r, requestOrdinal=2, sampleID='second')
        assert not m.execute_one(root, r2, 'digest', sender=lambda *a: success(*a, invalid=True))['validCompletion']
        r3 = dict(r, requestOrdinal=3, sampleID='third')
        assert m.execute_one(root, r3, 'digest', sender=success)['validCompletion']
        # Capacity-blocked input never loads a prompt or creates a prediction.
        blocked = dict(r, case=41, arm='time_images', requestOrdinal=4, limits=['estimated_context_over_limit'])
        assert m.execute_one(root, blocked, 'digest', sender=forbidden) is None
        assert (root / 'results/0004/blocked.json').exists() and not (root / 'results/0004/result.json').exists()
        # Half-size usage cannot pass the large-input feasibility gate.
        m.ex.atomic_json(root / 'plan.json', {})
        plan = {'preflightSampleIDs': ['sample'], 'sharedBudgetInputTokens': 413836}
        r.update(estimatedInputTokens=400000, imageCount=0)
        assert m.preflight_report(root, [r], plan)['status'] == 'failed'
        # Evidence tampering cannot be recovered as a successful resume.
        path = folder / 'attempt-1/response.body'; path.write_bytes(b'tampered')
        rejected(lambda: m.execute_one(root, r, 'digest', sender=forbidden))
        report = {'status': 'passed', 'networkCalls': 0,
                  'gates': ['complete stream decoding', 'no unsupported truncation or compaction parameters', 'completed resume without resend',
                            'crash after transport recovered', 'invalid generation retained',
                            'capacity block never dispatched or scored', 'shortened usage rejected', 'tampering rejected']}
        if data:
            rows = m.load(data / 'cases.json')
            row = max((r for r in rows if r['arm'] == 'time_images' and r['status'] == 'prepared_not_sampled'), key=lambda r: r['wirePayloadBytes'])
            payload = m.load(data / row['payload'])
            large_root = root / 'large'; (large_root / 'data').mkdir(parents=True)
            m.ex.atomic_json(large_root / 'data/prompt.json', payload)
            req = {k: row[k] for k in ('case', 'arm', 'exampleID', 'partsSHA256', 'limits')}
            req.update(requestOrdinal=1, sampleID='large', variant=row['arm'], model=m.legacy.MODEL, replicate=1, payload='prompt.json')
            value = m.execute_one(large_root, req, 'large-digest', sender=success)
            (large_root / 'results/0001/result.json').unlink()
            assert m.execute_one(large_root, req, 'large-digest', sender=forbidden) == value
            found, audit = m.legacy.audit(large_root, [req], 'large-digest', m.legacy.PRICES)
            assert audit['status'] == 'complete' and found == [value]
            report['largestRealRequest'] = {**value['requestEvidence'], 'case': row['case'],
                                           'completedTransportResumedWithoutResend': True, 'providerCalls': 0}
            # Every intended reuse must resolve to verified exact transport evidence.
            reuse_count = 0
            for row in rows:
                if not row.get('reuse'): continue
                req = dict(row, model=m.legacy.MODEL, variant=row['arm'], replicate=1)
                req['reuse'] = m.bind_reuse(row['reuse'], req)
                result = m.reused_result(req)
                assert result['partsSHA256'] == row['partsSHA256'] and result['arm'] == row['arm']
                assert result['providerCallsForThisCondition'] == 0
                reuse_count += 1
            assert reuse_count == 82
            report['exactReusesValidated'] = reuse_count
            report['preparedPlanSHA256'] = m.file_hash(data / 'plan.json')
        report['peakProcessMiB'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024**2
        assert report['peakProcessMiB'] < 2048, 'Local single-request executor exceeded 2 GiB'
        report['codeSHA256'] = {n: m.file_hash(Path(__file__).with_name(n)) for n in m.CODE}
        return report


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data', type=Path); p.add_argument('--report', type=Path)
    a = p.parse_args(); report = check(a.data)
    if a.report: m.ex.atomic_json(a.report, report)
    print(m.canonical(report))

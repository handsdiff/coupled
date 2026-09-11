#!/usr/bin/env python3
"""No-network tests for paired repeatability, all/any success and failure recovery."""
import copy
import importlib.util
import itertools
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from phase1_read_model_comparison import request_plan
from phase1_repeatability_review import combine, scored_directory

s = importlib.util.spec_from_file_location('repeat', Path(__file__).with_name('run-phase1-repeatability.py'))
m = importlib.util.module_from_spec(s); s.loader.exec_module(m)


def rejected(fn):
    try: fn()
    except (AssertionError, RuntimeError, ValueError, OSError, KeyError): return
    raise AssertionError('Invalid operation was accepted')


def main():
    cases = [{'exampleID': 'fixture-' + str(i)} for i in range(3)]
    prompts = [{'exampleID': e['exampleID'], 'variant': v, 'promptID': e['exampleID'] + v,
                'modelInput': 'Synthetic unchanged context: ' + e['exampleID'] + v}
               for e in cases for v in ('old', 'new')]
    originals = request_plan(prompts, cases, 17)
    selected = {e['exampleID'] for e in cases}
    requests = m.schedule(originals, selected, 123)
    assert requests == m.schedule(originals, selected, 123)
    assert requests != m.schedule(originals, selected, 456)
    assert len(requests) == 24 and len({r['sampleID'] for r in requests}) == 24
    for r in requests:
        original = originals[r['originalRequestOrdinal']]
        assert all(r[k] == original[k] for k in ('exampleID', 'model', 'variant', 'promptID', 'requestSHA256'))
    lookup = {p['promptID']: p for p in prompts}
    calls = []
    def persist(attempt, response, code=200):
        wire = json.dumps(response).encode()
        (attempt / 'response.body').write_bytes(wire)
        m.ex.atomic_json(attempt / 'transport.json', {'httpStatus': code,
            'bodySHA256': m.file_hash(attempt / 'response.body'), 'dispatchToCompletionSeconds': 1.0})
    def ok(body, attempt, timeout):
        assert (attempt / 'inflight.json').exists()
        assert set(body) == {'model', 'input', 'reasoning', 'tools', 'stream'}
        calls.append(body)
        persist(attempt, {'id': 'response-' + str(len(calls)), 'model': body['model'],
            'reasoning': {'effort': 'xhigh'}, 'status': 'completed', 'output': [
                {'type': 'message', 'content': [{'type': 'output_text', 'text': 'same answer allowed'}]}],
            'usage': {'input_tokens': 20, 'output_tokens': 5, 'total_tokens': 25,
                      'input_tokens_details': {'cached_tokens': 10}}})
    def failure(body, attempt, timeout):
        calls.append(body); persist(attempt, {'error': {'code': 500, 'message': 'synthetic'}}, 500)
    def quota(body, attempt, timeout):
        calls.append(body); persist(attempt, {'error': {'code': 429, 'message': 'synthetic'}}, 429)
    def crash(body, attempt, timeout):
        calls.append(body); raise OSError('Interrupted dispatch')
    prices = {name: {'input': 10, 'cachedInput': 1, 'output': 50} for name in m.ex.MODELS}
    with tempfile.TemporaryDirectory(prefix='coupled-repeatability-check-') as tmp, \
            patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('Network forbidden')), \
            patch('subprocess.Popen', side_effect=AssertionError('Process launch forbidden')):
        root = Path(tmp)
        def run(name, sender=ok, req=requests):
            return m.run_requests(req, lookup, root / name, 'plan-hash', prices, 10, sender=sender, sleep=lambda _: None)
        result = run('complete')
        assert len(result) == 24
        count = len(calls); assert run('complete') == result and len(calls) == count
        assert len({r['prediction'] for r in result}) == 1, 'Identical text must not be rejected as cached'
        # Final transport survives interruption before result serialization.
        (root / 'complete/0003/result.json').unlink()
        assert run('complete') == result and len(calls) == count
        def isolated(body, attempt, timeout):
            if attempt.name == 'attempt-1': failure(body, attempt, timeout)
            else: ok(body, attempt, timeout)
        result = run('isolated', isolated, requests[:3])
        assert len(result) == 3 and len(calls) == count + 6
        assert len(list((root / 'isolated').glob('*/attempt-1/disposition.json'))) == 3
        count = len(calls)
        rejected(lambda: run('twice', failure, requests[:2]))
        assert len(calls) == count + 2, 'Must pause on two consecutive failures'
        count = len(calls)
        rejected(lambda: run('twice', ok, requests[:2]))
        assert len(calls) == count, 'Resume must not reset failure authorization'
        rejected(lambda: run('uncertain', crash, requests[:1]))
        count = len(calls); rejected(lambda: run('uncertain', ok, requests[:1])); assert len(calls) == count
        rejected(lambda: run('quota', quota, requests[:1]))
        count = len(calls); rejected(lambda: run('quota', ok, requests[:1])); assert len(calls) == count
        (root / 'complete/0000/attempt-1/response.body').write_bytes(b'corrupt')
        rejected(lambda: run('complete'))
    graded = []
    for case, pattern in enumerate(itertools.product((False, True), repeat=3)):
        for model in m.ex.MODELS:
            for variant in ('old', 'new'):
                for replicate, passed in enumerate(pattern, 1):
                    graded.append({'case': case, 'model': model, 'variant': variant,
                                   'replicate': replicate, 'semanticPass': passed})
    scores = m.aggregate(graded)
    assert len(scores) == 4
    for arm in scores.values():
        assert arm['cases'] == 8 and arm['allThreeSuccessRate'] == 1/8
        assert arm['anyOfThreeSuccessRate'] == 7/8
        assert arm['singleAnswerPassRate'] == .5
        assert arm['successCountDistribution'] == {0: 1, 1: 3, 2: 3, 3: 1}
        assert arm['freshSingleAnswerPassRate'] == .5
    rejected(lambda: m.aggregate(graded[1:]))
    rejected(lambda: m.aggregate(graded + [graded[0]]))
    rejected(lambda: m.aggregate([]))
    rejected(lambda: m.aggregate([r for r in graded if r['variant'] == 'old']))
    rejected(lambda: m.aggregate([r for r in graded if not (r['case'] == 0 and r['variant'] == 'old')]))
    wrong = copy.deepcopy(graded); wrong[0]['semanticPass'] = 'true'
    rejected(lambda: m.aggregate(wrong))
    baseline, fresh, judgments = [], [], []
    for r in graded:
        if r['replicate'] == 1:
            baseline.append(r)
        else:
            ident = m.fingerprint([r['case'], r['model'], r['variant'], r['replicate']])
            fresh.append({**{k: v for k, v in r.items() if k != 'semanticPass'},
                          'reviewID': ident, 'prediction': 'answer', 'validCompletion': True})
            judgments.append({'reviewID': ident, 'predictionSHA256': m.fingerprint('answer'),
                              'decision': 'pass' if r['semanticPass'] else 'fail', 'reason': 'Synthetic rubric judgment'})
    combined, per_case = combine(baseline, fresh, judgments)
    assert m.aggregate(combined) == scores and len(per_case) == 8
    for row in per_case:
        for model in m.ex.MODELS:
            for variant in ('old', 'new'):
                v = row['models'][model][variant]
                assert v['successes'] == sum(r['semanticPass'] for r in v['answers'])
                assert v['freshSuccesses'] == sum(r['semanticPass'] for r in v['answers'][1:])
    rejected(lambda: combine(baseline, fresh, judgments[:-1]))
    rejected(lambda: combine(baseline, fresh, judgments + [judgments[0]]))
    altered = copy.deepcopy(judgments); altered[0]['predictionSHA256'] = 'tampered'
    rejected(lambda: combine(baseline, fresh, altered))
    invalid = copy.deepcopy(fresh)
    passed = next(i for i, j in enumerate(judgments) if j['decision'] == 'pass')
    invalid[passed]['validCompletion'] = False
    rejected(lambda: combine(baseline, invalid, judgments))
    # Human decisions bind exact answers, including originals, without mutating evidence.
    human_base = [{**r, 'prediction': 'original', 'validCompletion': True} for r in baseline]
    before = copy.deepcopy((human_base, fresh, judgments))
    decisions = []
    for r in (human_base[0], fresh[0]):
        decisions.append({**{k: r.get(k, 1) for k in ('case', 'model', 'variant', 'replicate')},
            'predictionSHA256': m.fingerprint(r['prediction']), 'decision': 'pass',
            'reason': 'Explicit human judgment', 'source': 'synthetic-user-message'})
    adjusted, adjusted_cases = combine(human_base, fresh, judgments, decisions)
    assert before == (human_base, fresh, judgments), 'Frozen evidence was mutated'
    assert sum(r['reviewOrigin'] == 'human_adjudication' for r in adjusted) == 2
    assert sum(r['semanticPass'] for r in adjusted) == sum(r['semanticPass'] for r in combined) + 2
    assert adjusted_cases[0]['models'][decisions[0]['model']][decisions[0]['variant']]['successes'] == 2
    rejected(lambda: combine(human_base, fresh, judgments, decisions + [decisions[0]]))
    bad = copy.deepcopy(decisions); bad[0]['predictionSHA256'] = 'wrong answer'
    rejected(lambda: combine(human_base, fresh, judgments, bad))
    # Calibration is not misrepresented as the user's own judgment, and cannot
    # override an explicit user decision for the same exact answer.
    calibration = [{**d, 'source': 'synthetic-assistant-calibration'} for d in decisions]
    calibrated, _ = combine(human_base, fresh, judgments, calibrations=calibration)
    assert sum(r['reviewOrigin'] == 'assistant_calibration' for r in calibrated) == 2
    assert not any(r['reviewOrigin'] == 'human_adjudication' for r in calibrated)
    human_fail = [{**decisions[0], 'decision': 'fail'}]
    priority, _ = combine(human_base, fresh, judgments, human_fail, calibration)
    exact = next(r for r in priority if all(r[k] == human_fail[0][k] for k in ('case', 'model', 'variant', 'replicate')))
    assert exact['semanticPass'] is False and exact['reviewOrigin'] == 'human_adjudication'
    assert before == (human_base, fresh, judgments)
    rejected(lambda: combine(human_base, fresh, judgments, calibrations=calibration + [calibration[0]]))
    bad_calibration = copy.deepcopy(calibration); bad_calibration[0]['predictionSHA256'] = 'stale'
    rejected(lambda: combine(human_base, fresh, judgments, calibrations=bad_calibration))
    bad = copy.deepcopy(decisions); bad[0]['replicate'] = 4
    rejected(lambda: combine(human_base, fresh, judgments, bad))
    with tempfile.TemporaryDirectory(prefix='coupled-review-pointer-') as tmp:
        review = Path(tmp)
        assert scored_directory(review) == review / 'scored'
        publication = review / 'scored-v3'; publication.mkdir()
        m.ex.atomic_json(publication / 'completion.json', {'status': 'synthetic'})
        pointer = {'directory': publication.name, 'completionSHA256': m.file_hash(publication / 'completion.json')}
        m.ex.atomic_json(review / 'current-scoring.json', pointer)
        assert scored_directory(review) == publication
        m.ex.atomic_json(review / 'current-scoring.json', {**pointer, 'directory': '../outside'})
        rejected(lambda: scored_directory(review))
        m.ex.atomic_json(review / 'current-scoring.json', {**pointer, 'completionSHA256': 'stale'})
        rejected(lambda: scored_directory(review))
    print(json.dumps({'status': 'passed', 'syntheticRequests': 24, 'providerCalls': 0,
        'gates': ['unchanged request fingerprints', 'two shuffled fresh replicas', 'resume without replay',
                  'same text allowed but distinct response IDs required', 'isolated failures continue',
                  'two consecutive failures pause durably', 'quota/uncertain no replay', 'wire integrity',
                  'all eight success patterns', 'distinct single-answer/all-three/any-one definitions', 'paired four-arm cohort']}))


if __name__ == '__main__': main()

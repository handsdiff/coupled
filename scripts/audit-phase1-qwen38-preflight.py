#!/usr/bin/env python3
"""Local audit/report of the completed bounded preflight and human-intent grades."""
import argparse
from collections import Counter
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import statistics


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args(); p = args.directory
    prep = json.loads((p / 'preparation.json').read_text())
    run = json.loads((p / 'preflight.json').read_text())
    records = [json.loads(l) for l in (p / 'operations.jsonl').open()]
    assert run['status'] == 'mechanical_checks_passed_pending_output_review'
    assert sha(p / 'preparation.json') == run['preparationSHA256']
    for path, digest in prep['runtime']['filesSHA256'].items():
        assert sha(Path(path)) == digest, f'Runtime changed: {path}'
    beginnings = {}; results = {}; cost = 0.
    for r in records:
        if r['kind'] == 'operation_begin':
            assert r['key'] not in beginnings
            beginnings[r['key']] = r; cost += r['maximumUSD']
            assert cost + prep['storageReserveUSD'] <= run['maximumUSD']
        elif r['kind'] == 'operation_result':
            assert r['key'] in beginnings and r['key'] not in results
            assert r['operation'] == beginnings[r['key']]['operation']
            results[r['key']] = r
    assert beginnings.keys() == results.keys(), 'Unfinished operation'
    assert math.isclose(cost, run['reservedTokenCostUSD'], abs_tol=1e-9)
    counts = Counter(r['operation'] for r in results.values())
    epochs = run['overfit']['epochsCompleted']
    evaluations = len(run['overfit']['snapshots'])
    assert counts['generation'] == 100 + 10 * evaluations
    assert counts['nll'] == 38 + 10 * evaluations
    assert counts['train'] == 8 + 10 * epochs
    for arm in ('old', 'new'):
        assert run['arms'][arm]['weightRestoreMaxLogprobDelta'] == 0
        assert run['arms'][arm]['optimizerContinuationMaxLogprobDelta'] == 0
    for r in results.values():
        v = r['value']
        if r['operation'] in ('nll', 'train'):
            assert len(v['targetLogprobs']) == v['lossBearingTokens']
            assert all(math.isfinite(x) and x <= 0 for x in v['targetLogprobs'])
            assert math.isclose(-sum(v['targetLogprobs']), v['weightedNLLSum'], abs_tol=1e-7)
            if r['operation'] == 'train':
                assert math.isclose(v['forwardMetrics']['loss:sum'], v['weightedNLLSum'], rel_tol=2e-5, abs_tol=1e-4)
                assert all(math.isfinite(x) for x in v['optimizerMetrics'].values())
    review = json.loads((p / 'capability-review.json').read_text())
    by_number = {c['cohortNumber']: c for c in prep['additionalTests']['cases']}
    assert len(by_number) == len(review['cases']) == 20
    assert {g['cohortNumber'] for g in review['cases']} == by_number.keys()
    grades = []
    for case in review['cases']:
        c = by_number[case['cohortNumber']]; eid = c['exampleID']
        outputs = run['capability']['scores'][eid]
        assert [g['seed'] for g in outputs] == prep['additionalTests']['generationSeeds']
        assert len(case['passBySeed']) == len(outputs) == 4
        for good, gen in zip(case['passBySeed'], outputs):
            assert type(good) is bool
            assert gen == {'seed': gen['seed'], **results[f"capability/{eid}/seed-{gen['seed']}"]['value']}
            grades.append({'exampleID': eid, 'cohortNumber': c['cohortNumber'], 'seed': gen['seed'],
                           'pass': good, 'reason': case['reason'],
                           'predictionSHA256': hashlib.sha256(gen['prediction'].encode()).hexdigest()})
    capability = {'answers': len(grades), 'passedAnswers': sum(g['pass'] for g in grades),
                  'cases': 20, 'anyOfFour': sum(any(g['passBySeed']) for g in review['cases']),
                  'allOfFour': sum(all(g['passBySeed']) for g in review['cases']),
                  'reviewStatus': 'implementer judgment; not independent adjudication or random-corpus estimate'}
    timing = {}
    groups = {
        'frozenCapability': [r['value'] for k, r in results.items() if k.startswith('capability/')],
        'overfitGeneration': [r['value'] for k, r in results.items() if k.startswith('overfit/eval-') and r['operation'] == 'generation'],
        'optimizerUpdates': [r['value'] for r in results.values() if r['operation'] == 'train'],
    }
    for name, values in groups.items():
        timing[name] = {'count': len(values), 'medianSeconds': statistics.median(v['latencySeconds'] for v in values),
                        'meanSeconds': statistics.mean(v['latencySeconds'] for v in values)}
    cost_by_test = {}
    for name, prefix in [('originalOld', 'old/'), ('originalNew', 'new/'), ('capability', 'capability/'), ('overfit', 'overfit/')]:
        cost_by_test[name] = sum(r['maximumUSD'] for r in beginnings.values() if r['key'].startswith(prefix))
    last = run['overfit']['snapshots'][-1]
    audit = {'status': 'passed_mechanics_NOT_full_run_approval', 'operationCounts': dict(counts),
        'unfinishedOperations': 0, 'maximumTokenCostUSD': cost, 'storageReserveUSD': prep['storageReserveUSD'],
        'actualInvoiceCostUSD': None, 'billingNote': 'Immediate provider billing has not populated; token bounds are not an invoice',
        'maximumTokenCostByTestUSD': cost_by_test, 'capability': capability, 'latency': timing,
        'overfit': {k: last[k] for k in ('epoch', 'baselineMeanNLL', 'meanNLL', 'normalizedExactMatches', 'memorizationGatePassed')},
        'epochTrainingNLL': [{'epoch': r['epoch'], 'trainingNLL': r['trainingNLL']} for r in records if r['kind'] == 'overfit_progress' and 'epoch' in r],
        'wallMinutes': (dt.datetime.fromisoformat(run['endedAt']) - dt.datetime.fromisoformat(run['startedAt'])).total_seconds() / 60,
        'lineageSHA256': {n: sha(p / n) for n in ('preparation.json', 'preflight.json', 'operations.jsonl', 'capability-review.json')},
        'scriptSHA256': sha(Path(__file__)), 'boundCapabilityGrades': grades}
    with (p / 'audit.json').open('x') as f:
        f.write(json.dumps(audit, indent=2, sort_keys=True) + '\n')
    print(json.dumps({k: audit[k] for k in ('status', 'operationCounts', 'maximumTokenCostUSD', 'capability', 'overfit', 'latency', 'wallMinutes')}, indent=2))


if __name__ == '__main__':
    main()

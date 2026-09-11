#!/usr/bin/env python3
"""Synthetic, no-network regression of versioned scoring reconciliation."""
import importlib.util
import json
from pathlib import Path
import tempfile


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


r = module('reconcile', 'reconcile-phase1-holistic-review.py')
s = module('score', 'score-phase1-read-model-comparison.py')


def rejected(fn):
    try:
        fn()
    except (AssertionError, ValueError):
        return
    raise AssertionError('Invalid revision was accepted')


with tempfile.TemporaryDirectory(prefix='coupled-scoring-check-') as tmp:
    root = Path(tmp)
    base = root / 'holistic-v2'
    base.mkdir()
    pred_dir = root / 'execution-v1-final/final-output-v2'
    pred_dir.mkdir(parents=True)
    predictions, blind, keys, grades, targets, classes = [], [], [], [], [], []
    for case in (1, 2):
        candidates, judgments = [], {}
        for i, (model, variant) in enumerate((m, v) for m in ('chatgpt/gpt-5.6-sol', 'chatgpt/gpt-6-astra') for v in ('old', 'new')):
            ordinal, label = (case - 1) * 4 + i + 1, 'ABCD'[i]
            predictions.append({'requestOrdinal': ordinal, 'exampleID': f'e{case}', 'model': model, 'variant': variant,
                'prediction': 'a meaningful thought', 'validCompletion': True,
                'timing': {'dispatchToCompletionSeconds': 1.25}, 'apiEquivalentCostUSD': 0.01})
            candidates.append({'label': label, 'prediction': 'a meaningful thought', 'validCompletion': True})
            keys.append({'case': case, 'label': label, 'exampleID': f'e{case}', 'requestOrdinal': ordinal,
                         'model': model, 'variant': variant})
            judgments[label] = {'decision': 'pass' if case == 1 else 'excluded_non_substantive', 'reason': 'Baseline fixture.'}
        targets.append({'case': case, 'exampleID': f'e{case}', 'target': 'a meaningful thought'})
        blind.append({**targets[-1], 'candidates': candidates})
        grades.append({'case': case, 'judgments': judgments})
        classes.append({'case': case, 'substantive': case == 1, 'reason': 'Baseline fixture.'})
    r.save(pred_dir / 'predictions.jsonl', predictions, True)
    for name, value in [('targets.jsonl', targets), ('blinded.jsonl', blind), ('blinding-key.jsonl', keys),
                        ('judgments.jsonl', grades), ('substantiveness.jsonl', classes)]:
        r.save(base / name, value, True)
    r.save(base / 'review-plan.json', {'version': 'fixture-v2', 'authority': 'test', 'passBar': 'Fixture',
        'source': str(root), 'cases': 2, 'predictions': 8,
        'sourceSHA256': {str(pred_dir / 'predictions.jsonl'): r.sha(pred_dir / 'predictions.jsonl')},
        'blindArtifactSHA256': {n: r.sha(base / n) for n in ('targets.jsonl', 'blinded.jsonl', 'blinding-key.jsonl')}})
    s.finish(base)
    r.save(base / 'scored/supplement.json', {'failedProviderAttempts': 0})
    baseline_hashes = {p: r.sha(p) for p in base.rglob('*') if p.is_file()}
    prediction_hash = r.sha(pred_dir / 'predictions.jsonl')
    policy = root / 'policy.json'
    r.save(policy, {'version': 'fixture-v3', 'passBar': 'Contextual bar', 'eligibilityBar': 'Specific intent',
        'classificationChanges': [{'case': 2, 'substantive': True, 'reason': 'Meaningful contextual question.'}]})
    output = root / 'holistic-v3'
    r.prepare(root, output, policy)
    rejected(lambda: r.prepare(root, output, policy))
    incomplete = root / 'incomplete.json'
    r.save(incomplete, {'judgments': [{'case': 2, 'label': 'A', 'decision': 'pass', 'reason': 'Explicit.'}]})
    rejected(lambda: r.finish(root, output, incomplete))
    assert not (output / 'judgments.jsonl').exists()
    duplicates = root / 'duplicate.json'
    r.save(duplicates, {'judgments': r.read(incomplete)['judgments'] * 2})
    rejected(lambda: r.finish(root, output, duplicates))
    # Mutating a target/query/blinded artifact invalidates the eligibility lock.
    target_file = output / 'targets.jsonl'
    original_bytes = target_file.read_bytes()
    target_file.write_bytes(original_bytes + b'\n')
    rejected(lambda: r.finish(root, output, incomplete))
    target_file.write_bytes(original_bytes)
    complete = root / 'complete.json'
    r.save(complete, {'judgments': [{'case': 2, 'label': label, 'decision': 'pass', 'reason': 'Explicit reviewed match.'} for label in 'ABCD']})
    r.finish(root, output, complete)
    summary = r.read(output / 'scored/summary.json')
    assert summary['version'] == 'fixture-v3' and summary['passBar'] == 'Contextual bar'
    assert summary['substantiveExamples'] == 2
    assert all(a['semanticPasses'] == 2 for a in summary['models'].values())
    assert all(r.sha(path) == digest for path, digest in baseline_hashes.items())
    assert r.sha(pred_dir / 'predictions.jsonl') == prediction_hash
    assert r.read(output / 'scored/final-audit.json')['allTargetsPredictionsTimingCostsAndDeterministicMetricsUnchanged']
    assert len(r.read(output / 'changes.json')['cases']) == 1
    rejected(lambda: r.finish(root, output, complete))
    bad_acceptance = root / 'unaccepted.json'
    r.save(bad_acceptance, {'reviewerAcceptedApplication': False})
    rejected(lambda: r.finalize(root, output, bad_acceptance))
    stale_acceptance = root / 'stale-acceptance.json'
    r.save(stale_acceptance, {'reviewerAcceptedApplication': True, 'adjudicationSHA256': '0'*64})
    rejected(lambda: r.finalize(root, output, stale_acceptance))
    acceptance = root / 'accepted.json'
    r.save(acceptance, {'reviewerAcceptedApplication': True, 'adjudicationSHA256': r.sha(output / 'adjudication.json'),
                       'reviewerThreadID': 'synthetic-test', 'messages': ['Synthetic fixture, not a real reviewer approval.']})
    r.finalize(root, output, acceptance)
    assert r.read(output / 'scoring-revision.json')['status'] == 'reviewed_and_audited'
    assert (output / 'scored/report.md').exists()
    rejected(lambda: r.finalize(root, output, acceptance))
print('PASS: new denominator, explicit four-arm judgments, versioned bar, immutable prior results, missing/duplicate/tampered annotations and repeat-write rejection; no network')

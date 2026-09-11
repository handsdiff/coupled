#!/usr/bin/env python3
"""Apply explicit review annotations to a fresh scoring version, never predictions.

This is an annotation validator/aggregator, not an automatic semantic judge.
Preparation locks the target-only eligibility revision. Finish requires explicit
A–D decisions for every newly included case. No network or provider operations.
"""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil


def read(path):
    return json.loads(path.read_text())


def rows(path):
    return [json.loads(line) for line in path.open() if line.strip()]


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1048576), b''):
            h.update(chunk)
    return h.hexdigest()


def save(path, value, lines=False):
    with path.open('x') as f:
        if lines:
            for row in value:
                f.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + '\n')
        else:
            f.write(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + '\n')


def prepare(root, output, policy_path):
    assert not output.exists(), 'Use a fresh review directory'
    baseline = root / 'holistic-v2'
    policy = read(policy_path)
    original = read(baseline / 'review-plan.json')
    for path, digest in original['sourceSHA256'].items():
        assert sha(Path(path)) == digest
    for name, digest in original['blindArtifactSHA256'].items():
        assert sha(baseline / name) == digest
    classes = {r['case']: r for r in rows(baseline / 'substantiveness.jsonl')}
    changes = policy['classificationChanges']
    assert len({r['case'] for r in changes}) == len(changes)
    for r in changes:
        assert r['case'] in classes and type(r['substantive']) is bool and r['reason'].strip()
        assert r['substantive'] != classes[r['case']]['substantive'], 'Redundant classification change'
        classes[r['case']] = {'case': r['case'], 'substantive': r['substantive'], 'reason': r['reason']}
    output.mkdir()
    for name in ('targets.jsonl', 'blinded.jsonl', 'blinding-key.jsonl'):
        shutil.copyfile(baseline / name, output / name)
    shutil.copyfile(policy_path, output / 'policy.json')
    save(output / 'substantiveness.jsonl', sorted(classes.values(), key=lambda r: r['case']), True)
    inputs = ('targets.jsonl', 'blinded.jsonl', 'blinding-key.jsonl', 'substantiveness.jsonl', 'policy.json')
    save(output / 'classification-lock.json', {
        'policy': 'Target/query-based review, including previously imported cases. Prior outputs were seen; this is not an independent blind evaluation.',
        'artifactsSHA256': {n: sha(output / n) for n in inputs},
        'cases': len(classes), 'substantive': sum(r['substantive'] for r in classes.values()),
    })
    plan = {**original, 'version': policy['version'], 'passBar': policy['passBar'],
            'substantiveness': policy['eligibilityBar'], 'priorLabelPolicy': 'Reconciled across the entire cohort; v2 is preserved, not overwritten.',
            'targetReviewPlanSHA256': None, 'classificationLockSHA256': sha(output / 'classification-lock.json'),
            'blinding': 'A–D review with prior exposure; model/pipeline identities are omitted from adjudication files, not a new independently blinded experiment.',
            'priorReview': str(baseline.resolve())}
    for n in ('review-plan.json', 'substantiveness.jsonl', 'judgments.jsonl', 'scored/summary.json'):
        plan['sourceSHA256'][str((baseline / n).resolve())] = sha(baseline / n)
    save(output / 'review-plan.json', plan)
    print(json.dumps({'status': 'eligibility_locked', 'cases': len(classes),
                      'substantive': sum(r['substantive'] for r in classes.values())}))


def finish(root, output, annotations_path):
    assert not (output / 'judgments.jsonl').exists(), 'Annotations already finalized'
    lock = read(output / 'classification-lock.json')
    for n, digest in lock['artifactsSHA256'].items():
        assert sha(output / n) == digest
    assert sha(output / 'classification-lock.json') == read(output / 'review-plan.json')['classificationLockSHA256']
    baseline = root / 'holistic-v2'
    classes = {r['case']: r for r in rows(output / 'substantiveness.jsonl')}
    old_classes = {r['case']: r for r in rows(baseline / 'substantiveness.jsonl')}
    original = {r['case']: r for r in rows(baseline / 'judgments.jsonl')}
    grades = copy.deepcopy(original)
    annotation = read(annotations_path)
    edits = annotation['judgments']
    assert len({(r['case'], r['label']) for r in edits}) == len(edits), 'Duplicate judgment'
    for r in edits:
        assert r['case'] in grades and r['label'] in 'ABCD' and len(r['label']) == 1
        assert r['decision'] in ('pass', 'fail') and r['reason'].strip()
        assert classes[r['case']]['substantive'], 'Cannot grade an excluded target'
        grades[r['case']]['judgments'][r['label']] = {k: r[k] for k in ('decision', 'reason')}
    for case, row in grades.items():
        for label, g in row['judgments'].items():
            if not classes[case]['substantive']:
                row['judgments'][label] = {'decision': 'excluded_non_substantive', 'reason': classes[case]['reason']}
            else:
                assert g['decision'] in ('pass', 'fail'), f'Missing explicit newly eligible judgment: {case}/{label}'
    save(output / 'judgments.jsonl', sorted(grades.values(), key=lambda r: r['case']), True)
    shutil.copyfile(annotations_path, output / 'adjudication.json')
    mapping = {(r['case'], r['label']): ('astra' if 'astra' in r['model'] else 'sol') + '_' + r['variant']
               for r in rows(output / 'blinding-key.jsonl')}
    changes = []
    for case, row in sorted(grades.items()):
        c = {'case': case, 'judgments': []}
        if classes[case]['substantive'] != old_classes[case]['substantive']:
            c['classification'] = {'before': old_classes[case]['substantive'],
                                   'after': classes[case]['substantive'], 'reason': classes[case]['reason']}
        for label, g in row['judgments'].items():
            before = original[case]['judgments'][label]
            if g['decision'] != before['decision']:
                c['judgments'].append({'arm': mapping[(case, label)], 'label': label,
                                      'before': before['decision'], 'after': g['decision'], 'reason': g['reason']})
        if c['judgments'] or 'classification' in c:
            changes.append(c)
    save(output / 'changes.json', {'baseline': 'holistic-v2', 'cases': changes})
    source = Path(__file__).with_name('score-phase1-read-model-comparison.py')
    spec = importlib.util.spec_from_file_location('scorer', source)
    scorer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scorer)
    scorer.finish(output)
    new = read(output / 'scored/summary.json')
    old = read(baseline / 'scored/summary.json')
    old_rows = {r['requestOrdinal']: r for r in rows(baseline / 'scored/judgments.unblinded.jsonl')}
    for r in rows(output / 'scored/judgments.unblinded.jsonl'):
        for key in ('prediction', 'target', 'exampleID', 'model', 'variant', 'generationLatencySeconds', 'apiEquivalentCostUSD', 'deterministicMetrics'):
            assert r[key] == old_rows[r['requestOrdinal']][key], f'Non-scoring change: {key}'
    for arm, s in new['models'].items():
        for key in ('medianLatencySecondsAllExamples', 'meanLatencySecondsAllExamples', 'apiEquivalentCostUSDAllExamples', 'operationalMeasurementExamples'):
            assert s[key] == old['models'][arm][key]
    supplement = read(baseline / 'scored/supplement.json')
    save(output / 'scored/supplement.json', {k: v for k, v in supplement.items() if k not in ('arms', 'sourceSummarySHA256', 'noNewOrChangedJudgments')})
    save(output / 'scored/final-audit.json', {
        'status': 'passed', 'cases': new['cases'], 'predictions': new['predictions'],
        'substantiveCases': new['substantiveExamples'], 'excludedCases': new['excludedExamples'],
        'allTargetsPredictionsTimingCostsAndDeterministicMetricsUnchanged': True,
        'newlyIncludedCasesExplicitlyGradedInAllFourArms': True,
        'baselineScoringHashBoundAndPreserved': True, 'providerCalls': 0,
        'artifactsSHA256': {n: sha(output / n) for n in ('policy.json', 'adjudication.json', 'changes.json', 'substantiveness.jsonl', 'judgments.jsonl', 'scored/summary.json', 'scored/judgments.unblinded.jsonl')},
        'implementationSHA256': {str(p.resolve()): sha(p) for p in (Path(__file__), source)},
    })


def finalize(root, output, evidence_path):
    """Promote only the exact adjudication accepted in the reviewer evidence."""
    assert not (output / 'scoring-revision.json').exists(), 'Revision already finalized'
    evidence = read(evidence_path)
    assert evidence['reviewerAcceptedApplication'] is True
    assert evidence['adjudicationSHA256'] == sha(output / 'adjudication.json')
    assert evidence['reviewerThreadID'] and evidence['messages']
    audit = read(output / 'scored/final-audit.json')
    assert audit['status'] == 'passed'
    for name, digest in audit['artifactsSHA256'].items():
        assert sha(output / name) == digest
    plan = read(output / 'review-plan.json')
    for path, digest in plan['sourceSHA256'].items():
        assert sha(Path(path)) == digest
    summary = read(output / 'scored/summary.json')
    old = read(root / 'holistic-v2/scored/summary.json')
    changes = read(output / 'changes.json')['cases']
    classifications = [c for c in changes if 'classification' in c]
    corrections = [g for c in changes if 'classification' not in c for g in c['judgments']]
    explanation = (f"Scoring v3: {len(classifications)} meaningful targets reinstated; "
                   f"{len(corrections)} prior prediction judgments corrected after contextual review with the reviewer. "
                   "The same rules apply to the original sample and added examples. All predictions, prompts, timing and costs are unchanged; scoring v2 is preserved.")
    lines = ['# Sol / Astra · reconciled semantic scoring v3', '', explanation, '',
             f"{summary['cases']} targets and {summary['predictions']} saved predictions. "
             f"Score denominator: **{summary['substantiveExamples']}**; {summary['excludedExamples']} remain inspectable but unscored.", '',
             'This is a contextual assistant/reviewer adjudication, subject to the user’s judgment—not independent blinded human ground truth. It is semantic-only; latency does not affect passes.', '',
             '## Consistent bar', '', plan['passBar'], '', plan['substantiveness'], '',
             '## Results', '', '| Model | READ pipeline | Passes | Rate | Median latency | Mean latency | API-equivalent / query |',
             '|---|---|---:|---:|---:|---:|---:|']
    for model in ('chatgpt/gpt-6-astra', 'chatgpt/gpt-5.6-sol'):
        for variant in ('old', 'new'):
            a = summary['models'][model + ' / ' + variant]
            lines.append(f"| {model.removeprefix('chatgpt/')} | {variant} | {a['semanticPasses']}/{a['substantiveExamples']} | {100*a['semanticPassRate']:.2f}% | {a['medianLatencySecondsAllExamples']:.2f}s | {a['meanLatencySecondsAllExamples']:.2f}s | ${a['apiEquivalentCostUSDAllExamples']/a['operationalMeasurementExamples']:.4f} |")
    lines += ['', 'Latency and cost use all 377 successful queries per arm. Dollar figures are frozen API-equivalent estimates, not subscription charges. No new inference or training occurred.', '',
              '## Old/new pipeline overlap', '']
    for model in ('chatgpt/gpt-6-astra', 'chatgpt/gpt-5.6-sol'):
        a = set(summary['models'][model + ' / old']['passCases'])
        b = set(summary['models'][model + ' / new']['passCases'])
        lines.append(f"- {model.removeprefix('chatgpt/')}: both pass {len(a & b)}; old only {len(a-b)}; new only {len(b-a)}; either {len(a|b)}.")
    lines += ['', '## What changed from scoring v2', '',
              f"The denominator changed from {old['substantiveExamples']} to {summary['substantiveExamples']}. "
              'Detailed workflow intent and context-resolved continuations count; generic controls and quotation wrappers do not. Predictions within an existing draft are not evidence of predicting the whole draft from scratch.', '',
              'The UI exposes per-case changes; `changes.json` preserves the exact old/new decisions. Prior v2 reports remain unchanged. Do not mix the two denominators.', '',
              'This correction does not by itself establish a pipeline improvement: subjective judgments, correlated work and modest pass counts remain important limitations.', '']
    with (output / 'scored/report.md').open('x') as f:
        f.write('\n'.join(lines))
    shutil.copyfile(evidence_path, output / 'reviewer-evidence.json')
    names = ('targets.jsonl', 'blinded.jsonl', 'blinding-key.jsonl', 'policy.json', 'classification-lock.json',
             'review-plan.json', 'substantiveness.jsonl', 'judgments.jsonl', 'adjudication.json', 'changes.json',
             'reviewer-evidence.json', 'scored/summary.json', 'scored/judgments.unblinded.jsonl',
             'scored/supplement.json', 'scored/final-audit.json', 'scored/report.md')
    save(output / 'scoring-revision.json', {
        'status': 'reviewed_and_audited', 'version': plan['version'], 'explanation': explanation,
        'reviewerThreadID': evidence['reviewerThreadID'], 'classificationChanges': len(classifications),
        'priorPredictionJudgmentCorrections': len(corrections), 'priorScoring': 'holistic-v2',
        'noNewInference': True, 'allFrozenInferenceArtifactsUnchanged': True,
        'artifactsSHA256': {n: sha(output / n) for n in names},
    })
    print(json.dumps({'status': 'reviewed_and_audited', 'substantive': summary['substantiveExamples']}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'finish', 'finalize'))
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--annotations', type=Path, required=True)
    args = parser.parse_args()
    {'prepare': prepare, 'finish': finish, 'finalize': finalize}[args.action](args.run, args.output, args.annotations)

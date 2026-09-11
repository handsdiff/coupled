#!/usr/bin/env python3
"""Local blinded review and reviewer-gated publication of repeated predictions."""
import argparse
from collections import defaultdict
import importlib.util
import json
from pathlib import Path
import re

from phase1_read_model_comparison import canonical, file_hash, fingerprint, rows


def load(path):
    return json.loads(path.read_text())


def scored_directory(review):
    pointer = review / 'current-scoring.json'
    if not pointer.exists():
        return review / 'scored'
    current = load(pointer)
    assert re.fullmatch(r'scored(?:-v[0-9]+)?', current['directory']), 'Invalid scoring directory'
    folder = review / current['directory']
    assert folder.resolve().parent == review.resolve(), 'Scoring directory escapes review'
    assert file_hash(folder / 'completion.json') == current['completionSHA256'], 'Scoring pointer hash mismatch'
    return folder


def runner(root):
    spec = importlib.util.spec_from_file_location('frozen_repeatability', root / 'code/run-phase1-repeatability.py')
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def blinded_rows(predictions, targets):
    blinded, key = [], []
    for r in predictions:
        ident = fingerprint(['repeatability-review-v1', r['sampleID']])
        target = targets[r['exampleID']]
        context_id = fingerprint(['blind-context-v1', r['promptID']])
        blinded.append({'reviewID': ident, 'case': target['case'], 'exampleID': r['exampleID'],
            'target': target['target'], 'query': target['query'], 'prediction': r['prediction'],
            'validCompletion': r['validCompletion'], 'contextID': context_id,
            'predictionSHA256': fingerprint(r['prediction'])})
        key.append({'reviewID': ident, 'sampleID': r['sampleID'], 'case': target['case'],
            'exampleID': r['exampleID'], 'model': r['model'], 'variant': r['variant'],
            'replicate': r['replicate'], 'requestOrdinal': r['requestOrdinal']})
    return sorted(blinded, key=lambda r: r['reviewID']), sorted(key, key=lambda r: r['reviewID'])


def stage(root, output, drafts, limit):
    """Inspect audited completed replies while generation continues; never publish."""
    m = runner(root); plan = m.verify(root)
    predictions, audit = m.audit(root, plan, list(rows(root / 'requests.jsonl')))
    targets = {r['exampleID']: r for r in rows(root / 'targets.jsonl')}
    blinded, _ = blinded_rows(predictions, targets)
    reviewed = {p.stem for p in drafts.glob('*.json')} if drafts else set()
    batch = [r for r in blinded if r['reviewID'] not in reviewed][:limit]
    assert batch and not output.exists()
    output.mkdir(parents=True, mode=0o700)
    m.save_rows(output / 'blinded.jsonl', batch)
    m.ex.atomic_json(output / 'stage.json', {'status': 'draft_review_only', 'runPlanSHA256': file_hash(root / 'plan.json'),
        'auditedCompleted': audit['recorded'], 'staged': len(batch),
        'blindedSHA256': file_hash(output / 'blinded.jsonl'), 'providerCalls': 0})
    print(json.dumps({'status': 'draft_review_only', 'auditedCompleted': audit['recorded'], 'staged': len(batch)}))


def prepare(root, output):
    m = runner(root); plan = m.verify(root)
    predictions, audit = m.audit(root, plan, list(rows(root / 'requests.jsonl')))
    assert audit['status'] == 'complete' and len(predictions) == plan['freshRequests']
    assert list(rows(root / 'predictions.jsonl')) == predictions
    assert file_hash(root / 'predictions.jsonl') == load(root / 'audit.json')['predictionsSHA256']
    assert not output.exists(), 'Use a new review directory'
    policy_path = Path(plan['source']) / 'holistic-v3/policy.json'
    policy = load(policy_path)
    targets = {r['exampleID']: r for r in rows(root / 'targets.jsonl')}
    prompts = {r['promptID']: r for r in rows(root / 'prompts.jsonl')}
    blinded, key = blinded_rows(predictions, targets)
    output.mkdir(parents=True, mode=0o700)
    m.save_rows(output / 'blinded.jsonl', blinded)
    m.save_rows(output / 'blinding-key.jsonl', key)
    m.save_rows(output / 'contexts.jsonl', [
        {'contextID': fingerprint(['blind-context-v1', p['promptID']]), 'modelInput': p['modelInput'],
         'modelInputSHA256': p['modelInputSHA256']}
        for p in sorted(prompts.values(), key=lambda r: r['promptID'])])
    m.ex.atomic_json(output / 'policy.json', policy)
    m.ex.atomic_json(output / 'review-plan.json', {'version': 'phase1-repeatability-review-v1',
        'run': str(root.resolve()), 'predictions': len(blinded), 'cases': len(targets),
        'runPlanSHA256': file_hash(root / 'plan.json'),
        'predictionsSHA256': file_hash(root / 'predictions.jsonl'),
        'baselineSHA256': file_hash(root / 'baseline.jsonl'),
        'policySourceSHA256': file_hash(policy_path),
        'blinding': 'Fresh outputs shuffled independently; model, variant, latency, sibling answers and original grade hidden. Context available by opaque ID. Assistant review, not independent human ground truth.',
        'grading': 'Judge each whole completion independently under holistic-v3. Do not reward similarity to sibling predictions or lower the threshold for near cases. Invalid/empty outputs fail. Original grades are immutable.',
        'artifactsSHA256': {n: file_hash(output / n) for n in ('blinded.jsonl', 'blinding-key.jsonl', 'contexts.jsonl', 'policy.json')}})
    print(json.dumps({'status': 'ready_for_blinded_review', 'freshAnswers': len(blinded), 'providerCalls': 0}))


def combine(baseline, fresh, judgments, adjudications=(), calibrations=()):
    """Exact-once grades; no missing-answer denominator shrinkage."""
    by_id = {r['reviewID']: r for r in judgments}
    assert len(by_id) == len(judgments) == len(fresh), 'Missing/duplicate judgments'
    assert set(by_id) == {r['reviewID'] for r in fresh}
    combined = [{**r, 'replicate': 1, 'reviewOrigin': 'preserved_holistic_v3'} for r in baseline]
    for r in fresh:
        grade = by_id[r['reviewID']]
        assert grade['decision'] in ('pass', 'fail') and grade['reason'].strip()
        assert grade['predictionSHA256'] == fingerprint(r['prediction'])
        passed = grade['decision'] == 'pass'
        assert not passed or (r['validCompletion'] and r['prediction'].strip()), 'Invalid output cannot pass'
        combined.append({**r, 'semanticPass': passed, 'decision': grade['decision'],
            'reason': grade['reason'], 'reviewOrigin': 'fresh_independent_review'})
    # Human review is a versioned overlay, never a rewrite of baseline evidence.
    lookup = {(r['case'], r['model'], r['variant'], r['replicate']): r for r in combined}
    assert len(lookup) == len(combined), 'Duplicate answer identity'
    seen = set()
    # Explicit human decisions take precedence over assistant calibration.
    human_keys = {tuple(d[k] for k in ('case', 'model', 'variant', 'replicate')) for d in adjudications}
    calibration_keys = [tuple(d[k] for k in ('case', 'model', 'variant', 'replicate')) for d in calibrations]
    assert len(set(calibration_keys)) == len(calibration_keys), 'Duplicate assistant calibration'
    decisions = [(d, 'assistant_calibration') for d in calibrations
                 if tuple(d[k] for k in ('case', 'model', 'variant', 'replicate')) not in human_keys]
    decisions.extend((d, 'human_adjudication') for d in adjudications)
    for decision, origin in decisions:
        key = tuple(decision[k] for k in ('case', 'model', 'variant', 'replicate'))
        assert key not in seen, 'Duplicate adjudication'
        seen.add(key)
        answer = lookup[key]
        assert decision['predictionSHA256'] == fingerprint(answer['prediction']), 'Adjudicated answer binding changed'
        assert decision['decision'] in ('pass', 'fail') and decision['reason'].strip()
        passed = decision['decision'] == 'pass'
        assert not passed or (answer.get('validCompletion', True) and answer['prediction'].strip())
        answer['previousScoring'] = {k: answer.get(k) for k in ('decision', 'semanticPass', 'reason', 'reviewOrigin')}
        answer.update(semanticPass=passed, decision=decision['decision'], reason=decision['reason'],
                      reviewOrigin=origin, adjudicationSource=decision['source'])
    by_case = defaultdict(lambda: defaultdict(list))
    for r in combined:
        assert type(r['semanticPass']) is bool
        by_case[r['case']][(r['model'], r['variant'])].append(r)
    cases = []
    for case, arms in sorted(by_case.items()):
        assert len(arms) == 4
        models = {}
        for model in ('chatgpt/gpt-6-astra', 'chatgpt/gpt-5.6-sol'):
            variants = {}
            for variant in ('old', 'new'):
                answers = sorted(arms[(model, variant)], key=lambda r: r['replicate'])
                assert [r['replicate'] for r in answers] == [1, 2, 3]
                variants[variant] = {'successes': sum(r['semanticPass'] for r in answers),
                    'freshSuccesses': sum(r['semanticPass'] for r in answers[1:]),
                    'originalPass': answers[0]['semanticPass'], 'answers': answers}
            original_delta = int(variants['new']['originalPass']) - int(variants['old']['originalPass'])
            fresh_delta = variants['new']['freshSuccesses'] - variants['old']['freshSuccesses']
            models[model] = {**variants, 'originalDifference': original_delta,
                'freshDifference': fresh_delta,
                'differenceOfThree': variants['new']['successes'] - variants['old']['successes']}
        cases.append({'case': case, 'models': models})
    return combined, cases


def reconcile(root, review, evidence, human_evidence, output=None, calibration_evidence=None):
    """Publish agreed reviewer corrections plus explicit, hash-bound user decisions.

    Retains the draft and frozen originals. The publication does not claim that
    the reviewer independently approved the user's subsequent adjudications.
    """
    m = runner(root); m.verify(root)
    rp = load(review / 'review-plan.json')
    assert rp['run'] == str(root.resolve()) and rp['runPlanSHA256'] == file_hash(root / 'plan.json')
    assert rp['predictionsSHA256'] == file_hash(root / 'predictions.jsonl')
    assert rp['baselineSHA256'] == file_hash(root / 'baseline.jsonl')
    m.ex.verify_files(review, rp['artifactsSHA256'])
    report = load(evidence); human = load(human_evidence)
    draft_hash = file_hash(review / 'judgments.jsonl')
    assert report['sourceJudgmentsSHA256'] == draft_hash
    assert report['sourcePredictionsSHA256'] == rp['predictionsSHA256']
    assert human['predictionsSHA256'] == rp['predictionsSHA256'] and human['baselineSHA256'] == rp['baselineSHA256']
    assert human['authority'] == 'user' and human['message'].strip()
    if human.get('previousScoringSHA256'):
        assert file_hash(scored_directory(review) / 'completion.json') == human['previousScoringSHA256'], 'A newer scoring revision already exists'
    assert human['answerLabels'] == {'original': 1, 'freshAnswer1': 2, 'freshAnswer2': 3}
    assert calibration_evidence or not (scored_directory(review) / 'assistant-calibrations.json').exists(), 'Carry forward existing assistant calibrations explicitly; do not silently discard them'
    calibration = load(calibration_evidence) if calibration_evidence else None
    if calibration:
        assert calibration['authority'] == 'assistant_calibration'
        assert calibration['predictionsSHA256'] == rp['predictionsSHA256']
        assert calibration['baselineSHA256'] == rp['baselineSHA256']
        assert calibration['previousScoringSHA256'] == file_hash(scored_directory(review) / 'completion.json')
        assert calibration['principle'].strip()
    predictions = {r['sampleID']: r for r in rows(root / 'predictions.jsonl')}
    blinded = {r['reviewID']: r for r in rows(review / 'blinded.jsonl')}
    fresh = []
    for key in rows(review / 'blinding-key.jsonl'):
        r = predictions[key['sampleID']]
        assert all(r[k] == key[k] for k in ('exampleID', 'model', 'variant', 'replicate', 'requestOrdinal'))
        assert blinded[key['reviewID']]['prediction'] == r['prediction']
        fresh.append({**r, 'case': key['case'], 'reviewID': key['reviewID'],
                      'generationLatencySeconds': r['timing']['dispatchToCompletionSeconds']})
    judgments = list(rows(review / 'judgments.jsonl'))
    by_id = {r['reviewID']: r for r in judgments}
    seen = set()
    for change in report['overrides']:
        ident = change['reviewID']; assert ident not in seen
        seen.add(ident)
        old = by_id[ident]
        assert old['case'] == change['case'] and old['decision'] == change['originalDecision']
        assert blinded[ident]['prediction'] == change['prediction']
        old.update(decision=change['proposedDecision'], reason=change['reason'],
                   authority='reviewer_reconciled', previousDraftDecision=change['originalDecision'])
    coverage = report['reviewCoverage']
    assert len(seen) == coverage['proposedChanges']
    assert coverage['agreedDecisions'] + len(seen) == coverage['freshAnswersIndependentlyAssessed'] == len(judgments)
    baseline = list(rows(root / 'baseline.jsonl'))
    reviewer_answers, _ = combine(baseline, fresh, judgments)
    combined, cases = combine(baseline, fresh, judgments, human['decisions'],
                              calibration['decisions'] if calibration else ())
    changed = [{k: r[k] for k in ('case', 'model', 'variant', 'replicate', 'decision')}
               for r in combined if r.get('previousScoring', {}).get('semanticPass', r['semanticPass']) != r['semanticPass']]
    # Compare exact repeated answers without silently overruling specific human decisions.
    identical = defaultdict(list)
    for r in combined:
        identical[(r['case'], r['prediction'])].append(r)
    conflicts = [[{k: r[k] for k in ('case', 'model', 'variant', 'replicate', 'decision', 'reviewOrigin')}
                  for r in group] for group in identical.values() if len({r['semanticPass'] for r in group}) > 1]
    assert not conflicts, f'Identical-answer scoring needs reconciliation: {conflicts}'
    out = output or review / 'scored'
    assert out.resolve().parent == review.resolve() and re.fullmatch(r'scored(?:-v[0-9]+)?', out.name), 'Invalid scoring output'
    assert not out.exists(), 'Published review is immutable'
    out.mkdir()
    m.save_rows(out / 'reviewer-judgments.jsonl', judgments)
    m.save_rows(out / 'answers.jsonl', combined); m.save_rows(out / 'cases.jsonl', cases)
    m.ex.atomic_json(out / 'reviewer-evidence.json', report)
    m.ex.atomic_json(out / 'human-adjudications.json', human)
    if calibration:
        m.ex.atomic_json(out / 'assistant-calibrations.json', calibration)
    m.ex.atomic_json(out / 'summary.json', {'arms': m.aggregate(combined), 'cases': len(cases), 'freshAnswers': len(fresh),
        'primary': 'Per-case old/new success counts out of three, with original and fresh answers separated.',
        'scope': 'Selected promising cases; repeatability diagnostic, not full-corpus accuracy.',
        'passBar': load(review / 'policy.json')['passBar'],
        'passBarClarifications': human.get('calibrationClarifications', []),
        'humanAdjudications': len(human['decisions']),
        'humanDecisionChanges': [d for d in changed if any(all(d[k] == h[k] for k in ('case', 'model', 'variant', 'replicate')) for h in human['decisions'])],
        'assistantCalibrationCount': sum(r['reviewOrigin'] == 'assistant_calibration' for r in combined),
        'reviewerChanges': len(seen),
        'reviewerFreshPassCount': sum(r['semanticPass'] for r in reviewer_answers if r['replicate'] > 1),
        'identicalAnswerConflicts': conflicts})
    m.ex.atomic_json(out / 'completion.json', {'version': 'phase1-repeatability-scoring-v2',
        'status': 'reviewer_reconciled_with_human_adjudications',
        'reviewPlanSHA256': file_hash(review / 'review-plan.json'), 'judgmentsSHA256': draft_hash,
        'reviewerSourceSHA256': file_hash(evidence), 'humanSourceSHA256': file_hash(human_evidence),
        'implementationSHA256': file_hash(Path(__file__)),
        'authority': 'Reviewer recommendations accepted by implementer; separately attributed assistant calibrations where provided; explicit user judgments take precedence. Original artifacts are unchanged.',
        'artifactsSHA256': {n: file_hash(out / n) for n in ('answers.jsonl', 'cases.jsonl', 'summary.json',
             'reviewer-judgments.jsonl', 'reviewer-evidence.json', 'human-adjudications.json') +
             (('assistant-calibrations.json',) if calibration else ())}})
    if output:
        if human.get('previousScoringSHA256'):
            assert file_hash(scored_directory(review) / 'completion.json') == human['previousScoringSHA256'], 'Scoring changed during publication'
        m.ex.atomic_json(review / 'current-scoring.json', {'directory': out.name,
            'completionSHA256': file_hash(out / 'completion.json')})
    print(json.dumps({'status': 'reviewer_reconciled_with_human_adjudications', 'answers': len(combined),
        'reviewerChanges': len(seen), 'humanAdjudications': len(human['decisions']),
        'humanChanges': len(changed), 'providerCalls': 0}))


def finish(root, review, evidence):
    m = runner(root); plan = m.verify(root); rp = load(review / 'review-plan.json')
    assert rp['run'] == str(root.resolve()) and rp['runPlanSHA256'] == file_hash(root / 'plan.json')
    assert rp['predictionsSHA256'] == file_hash(root / 'predictions.jsonl')
    assert rp['baselineSHA256'] == file_hash(root / 'baseline.jsonl')
    m.ex.verify_files(review, rp['artifactsSHA256'])
    approval = load(evidence)
    assert approval['approved'] is True and approval['messages'] and approval['reviewerThreadID']
    assert approval['judgmentsSHA256'] == file_hash(review / 'judgments.jsonl'), 'Reviewer approval does not bind these grades'
    predictions = {r['sampleID']: r for r in rows(root / 'predictions.jsonl')}
    blinded = {r['reviewID']: r for r in rows(review / 'blinded.jsonl')}
    fresh = []
    for key in rows(review / 'blinding-key.jsonl'):
        r = predictions[key['sampleID']]
        assert all(r[k] == key[k] for k in ('exampleID', 'model', 'variant', 'replicate', 'requestOrdinal'))
        assert blinded[key['reviewID']]['prediction'] == r['prediction']
        fresh.append({**r, 'case': key['case'], 'reviewID': key['reviewID'],
            'generationLatencySeconds': r['timing']['dispatchToCompletionSeconds']})
    combined, cases = combine(list(rows(root / 'baseline.jsonl')), fresh, list(rows(review / 'judgments.jsonl')))
    summary = m.aggregate(combined)
    out = review / 'scored'; assert not out.exists(), 'Published review is immutable'
    out.mkdir()
    m.save_rows(out / 'answers.jsonl', combined); m.save_rows(out / 'cases.jsonl', cases)
    m.ex.atomic_json(out / 'summary.json', {'arms': summary, 'cases': len(cases), 'freshAnswers': len(fresh),
        'primary': 'Per-case old/new success counts out of three, with original and fresh answers separated.',
        'scope': 'Selected promising cases; repeatability diagnostic, not full-corpus accuracy or causal proof of a pipeline effect.',
        'passBar': load(review / 'policy.json')['passBar']})
    m.ex.atomic_json(out / 'reviewer-evidence.json', approval)
    m.ex.atomic_json(out / 'completion.json', {'status': 'reviewer_agreed_and_audited',
        'reviewPlanSHA256': file_hash(review / 'review-plan.json'),
        'judgmentsSHA256': file_hash(review / 'judgments.jsonl'),
        'artifactsSHA256': {n: file_hash(out / n) for n in ('answers.jsonl', 'cases.jsonl', 'summary.json', 'reviewer-evidence.json')}})
    print(json.dumps({'status': 'reviewer_agreed_and_audited', 'cases': len(cases), 'answers': len(combined), 'providerCalls': 0}))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('stage', 'prepare', 'finish', 'reconcile'))
    p.add_argument('--run', required=True, type=Path); p.add_argument('--review', required=True, type=Path)
    p.add_argument('--reviewer-evidence', type=Path)
    p.add_argument('--human-adjudications', type=Path)
    p.add_argument('--assistant-calibrations', type=Path, help='Hash-bound, separately attributed assistant grading corrections; explicit user decisions take precedence')
    p.add_argument('--output', type=Path, help='New immutable scored-vN directory; atomically selects it for the UI')
    p.add_argument('--drafts', type=Path); p.add_argument('--limit', type=int, default=25)
    a = p.parse_args()
    if a.action == 'stage': stage(a.run.resolve(), a.review.resolve(), a.drafts, a.limit)
    elif a.action == 'prepare': prepare(a.run.resolve(), a.review.resolve())
    elif a.action == 'reconcile': reconcile(a.run.resolve(), a.review.resolve(), a.reviewer_evidence, a.human_adjudications, a.output, a.assistant_calibrations)
    else: finish(a.run.resolve(), a.review.resolve(), a.reviewer_evidence)


if __name__ == '__main__': main()

#!/usr/bin/env python3
"""Blinded explicit grading and paired analysis of the five-condition study.

Offline only. Capacity-blocked inputs are not predictions. Exact historical
reuse carries its verified grade; new judgments require human/assistant review.
"""
import argparse
from collections import Counter, defaultdict
import itertools
import json
import os
from pathlib import Path
import random
import statistics

from phase1_read_model_comparison import canonical, file_hash, fingerprint, rows

VERSION = 'phase1-median-context-holistic-v1'
ARMS = ('time_cleaned', 'time_ocr', 'time_images', 'budget_cleaned', 'budget_ocr')
NAMES = dict(zip(ARMS, ('Original 32K cleaned', 'Full-interval OCR', 'Full-interval screenshots',
                       'Expanded cleaned (shared median budget)', 'Expanded OCR (shared median budget)')))


def load(p): return json.loads(p.read_text())


def save(p, v):
    with p.open('x') as f:
        os.chmod(p, 0o600); f.write(json.dumps(v, indent=2, ensure_ascii=False, sort_keys=True) + '\n')


def save_rows(p, rs):
    with p.open('x') as f:
        os.chmod(p, 0o600)
        for r in rs: f.write(canonical(r) + '\n')


def verify_run(root):
    plan = load(root / 'plan.json'); audit = load(root / 'audit.json')
    assert plan['version'] == 'phase1-median-context-executor-v2'
    assert audit['status'] == 'complete' and audit['planSHA256'] == file_hash(root / 'plan.json')
    assert audit['predictionsSHA256'] == file_hash(root / 'predictions.jsonl')
    for n, h in plan['artifactsSHA256'].items(): assert file_hash(root / n) == h
    for n, h in plan['codeSHA256'].items(): assert file_hash(root / 'code' / n) == h
    rs = list(rows(root / 'predictions.jsonl')); reqs = list(rows(root / 'requests.jsonl'))
    wanted = {r['sampleID']: r for r in reqs if not r['limits']}
    assert len(rs) == len({r['sampleID'] for r in rs}) == len(wanted) == audit['completed'] == 344
    assert {r['sampleID'] for r in rs} == set(wanted)
    assert Counter(r['arm'] for r in rs) == Counter({a: (68 if a == 'time_images' else 69) for a in ARMS})
    assert audit['capacityBlocked'] == [{'case': 41, 'arm': 'time_images'}]
    for r in rs:
        q = wanted[r['sampleID']]
        assert all(r[k] == q[k] for k in ('case', 'arm', 'exampleID', 'partsSHA256'))
        assert load(root / 'results' / f"{q['requestOrdinal']:04d}" / 'result.json') == r
    cases = {}
    for c in load(root / 'data/cases.json'):
        if c['case'] in cases:
            assert all(c[k] == cases[c['case']][k] for k in ('exampleID', 'query', 'target'))
        cases[c['case']] = c
    assert len(cases) == 69 and set(cases).isdisjoint({118, 126, 127})
    # Explicit recovery must retain the failed attempt and every prior result.
    recovery = root / 'authorized-replacement-20260910'
    if recovery.exists():
        a = load(recovery / 'authorization.json')
        assert a['runPlanSHA256'] == file_hash(root / 'plan.json')
        for n, h in a['completedResultsSHA256'].items(): assert file_hash(root / n) == h
        for n, h in a['interruptedAttemptSHA256'].items(): assert file_hash(recovery / 'interrupted-attempt-1' / n) == h
        done = load(recovery / 'replacement-complete.json')
        r = wanted[done['sampleID']]
        assert file_hash(root / 'results' / f"{r['requestOrdinal']:04d}" / 'result.json') == done['resultSHA256']
    return rs, cases


def prior_grades(paths):
    found = {}
    for path in paths:
        completion = load(path / 'completion.json')
        for n, h in completion['artifactsSHA256'].items(): assert file_hash(path / n) == h
        for r in rows(path / 'judgments.jsonl'):
            key = (r['exampleID'], r['partsSHA256'], fingerprint(r['prediction']))
            if key in found: assert found[key]['decision'] == r['decision']
            found[key] = {'decision': r['decision'], 'reason': r['reason'], 'target': r['target'],
                          'source': str(path / 'judgments.jsonl'), 'sourceSHA256': file_hash(path / 'judgments.jsonl')}
    return found


def draft(root, output):
    """Read-only snapshot for parallel review; no final scores from partial data.

    Blind indices derive from ALL frozen planned predictions, not arrival order,
    and must match the later complete prepare exactly.
    """
    assert not output.exists()
    plan = load(root/'plan.json')
    assert file_hash(root/'requests.jsonl') == plan['artifactsSHA256']['requests.jsonl']
    reqs = [r for r in rows(root/'requests.jsonl') if not r['limits']]
    assert len(reqs) == 344
    random.Random(20260910344).shuffle(reqs)
    cs = {c['case']:c for c in load(root/'data/cases.json')}
    available=[];missing=[];bound={}
    for i,q in enumerate(reqs,1):
        if q.get('reuse'):continue
        path=root/'results'/f"{q['requestOrdinal']:04d}"/'result.json'
        if not path.exists():missing.append(i);continue
        r=load(path);c=cs[r['case']]
        assert all(q[k]==r[k] for k in ('sampleID','exampleID','case','arm','partsSHA256'))
        available.append({'blindIndex':i,'reviewID':fingerprint([VERSION,r['sampleID']]),'case':r['case'],
            'exampleID':r['exampleID'],'target':c['target'],'query':c['query'],'prediction':r['prediction'],
            'predictionSHA256':fingerprint(r['prediction']), 'validCompletion':bool(r['validCompletion'] and r['prediction'].strip())})
        bound[str(path)]=file_hash(path)
    output.mkdir(mode=0o700)
    save_rows(output/'blinded.jsonl',available)
    save(output/'draft-plan.json',{'version':VERSION,'status':'partial_review_only_not_publishable',
        'runPlanSHA256':file_hash(root/'plan.json'),'availableNewAnswers':len(available),'pendingBlindIndices':missing,
        'resultSHA256':bound,'blindedSHA256':file_hash(output/'blinded.jsonl'),
        'policy':'No aggregate scoring before complete audited coverage. Later full review must match these exact blind records.', 'providerCalls':0})
    save(output/'policy.json',plan['scoringContract'])
    print(canonical({'status':'draft_ready','available':len(available),'pending':len(missing)}))


def prepare(root, review, prior):
    assert not review.exists(), 'Fresh review directory required'
    rs, cases = verify_run(root); previous = prior_grades(prior)
    random.Random(20260910344).shuffle(rs)
    blind, key, fixed = [], [], []
    for i, r in enumerate(rs, 1):
        c = cases[r['case']]
        b = {'blindIndex': i, 'reviewID': fingerprint([VERSION, r['sampleID']]), 'case': r['case'],
             'exampleID': r['exampleID'], 'target': c['target'], 'query': c['query'], 'prediction': r['prediction'],
             'predictionSHA256': fingerprint(r['prediction']), 'validCompletion': bool(r['validCompletion'] and r['prediction'].strip())}
        key.append({'blindIndex': i, 'sampleID': r['sampleID'], 'case': r['case'], 'arm': r['arm'], 'reviewID': b['reviewID']})
        if r.get('reuse'):
            old = previous[(r['exampleID'], r['partsSHA256'], fingerprint(r['prediction']))]
            assert old['target'] == c['target']
            fixed.append({**b, **old, 'authority': 'exact_reuse_of_previously_reconciled_answer'})
        else:
            blind.append(b)
    assert len(fixed) == 82 and len(blind) == 262
    review.mkdir(mode=0o700)
    save_rows(review / 'blinded.jsonl', blind); save_rows(review / 'blinding-key.jsonl', key)
    save_rows(review / 'fixed-grades.jsonl', fixed)
    save(review / 'policy.json', load(root / 'plan.json')['scoringContract'])
    save(review / 'review-plan.json', {'version': VERSION, 'run': str(root), 'runPlanSHA256': file_hash(root / 'plan.json'),
        'predictionsSHA256': file_hash(root / 'predictions.jsonl'), 'newJudgments': 262, 'fixedJudgments': 82,
        'cases': 69, 'capacityException': {'case': 41, 'arm': 'time_images', 'score': None},
        'authority': 'Explicit assistant judgments plus independent reconciliation; user judgments supersede.',
        'blinding': 'Shuffled individual answers: no arm, usage, latency, sibling grades or prior grades in blinded.jsonl.',
        'contextReference': str(root / 'data/case-NNNN/time_cleaned.json'),
        'policy': 'Same intended thought including useful compatible elaboration; construction audit separate. Never exclude after seeing results.',
        'artifactsSHA256': {n: file_hash(review / n) for n in ('blinded.jsonl', 'blinding-key.jsonl', 'fixed-grades.jsonl', 'policy.json')},
        'priorScoring': {str(p / 'completion.json'): file_hash(p / 'completion.json') for p in prior}, 'providerCalls': 0})
    print(canonical({'status': 'ready_for_blinded_scoring', 'new': len(blind), 'fixed': len(fixed)}))


def annotations(path, blind):
    rs = list(rows(path)); lookup = {r['blindIndex']: r for r in rs}
    assert len(rs) == len(lookup) == len(blind) and set(lookup) == set(blind), 'Missing or duplicate judgments'
    for i, r in lookup.items():
        assert r['decision'] in ('pass', 'fail') and r['reason'].strip()
        assert r['decision'] != 'pass' or blind[i]['validCompletion']
    return lookup


def stats(values):
    values = sorted(v for v in values if v is not None)
    if not values: return {'count': 0}
    return {'count': len(values), 'mean': statistics.mean(values), 'median': statistics.median(values),
            'p90': values[min(len(values)-1, int(len(values)*.9))], 'total': sum(values)}


def pair(left, right):
    common = sorted(set(left) & set(right)); groups = defaultdict(list); differences = []
    for c in common:
        a, b = left[c]['semanticPass'], right[c]['semanticPass']
        groups['bothPass' if a and b else 'bothFail' if not a and not b else 'gains' if b else 'losses'].append(c)
        differences.append(int(b)-int(a))
    rng = random.Random(20260910)
    boot = sorted(sum(rng.choices(differences, k=len(common)))/len(common) for _ in range(10000))
    return {'n': len(common), **{k: groups[k] for k in ('bothPass', 'bothFail', 'gains', 'losses')},
            'netPassDifference': sum(differences), 'netPercentagePoints': 100*sum(differences)/len(common),
            'pairedBootstrap95PercentIntervalPercentagePoints': [100*boot[249], 100*boot[9749]],
            'interpretation': 'Selected diagnostic cohort, one sample per condition; descriptive, not population accuracy.'}


def publish(root, review, primary, independent, resolutions, approval, output):
    assert not output.exists(), 'Versioned output required'
    rs, cases = verify_run(root); plan = load(review / 'review-plan.json')
    assert plan['predictionsSHA256'] == file_hash(root / 'predictions.jsonl')
    for n, h in plan['artifactsSHA256'].items(): assert file_hash(review / n) == h
    blind = {r['blindIndex']: r for r in rows(review / 'blinded.jsonl')}
    a, b = annotations(primary, blind), annotations(independent, blind)
    resolve_rows = list(rows(resolutions)); resolved = {r['blindIndex']: r for r in resolve_rows}
    assert len(resolved) == len(resolve_rows) and set(resolved) <= set(blind)
    disagreements = {i for i in blind if a[i]['decision'] != b[i]['decision']}
    assert disagreements <= set(resolved), 'Unreconciled disagreement'
    for i, d in resolved.items():
        assert d['decision'] in ('pass', 'fail') and d['reason'].strip()
        if i not in disagreements: assert d['kind'] == 'joint_consistency_correction'
    assert approval.is_file(), 'Independent reconciliation record required'
    fixed = {r['blindIndex']: r for r in rows(review / 'fixed-grades.jsonl')}
    keys = {r['blindIndex']: r for r in rows(review / 'blinding-key.jsonl')}
    predictions = {r['sampleID']: r for r in rs}; graded = []; exact = {}
    for i, k in sorted(keys.items()):
        r = predictions[k['sampleID']]; c = cases[r['case']]
        decision = fixed[i] if i in fixed else resolved.get(i, a[i])
        assert decision['decision'] != 'pass' or r['validCompletion'] and r['prediction'].strip()
        identical = fingerprint([c['query'], c['target'], r['prediction']])
        if identical in exact: assert exact[identical] == decision['decision'], 'Identical answer received inconsistent grade'
        exact[identical] = decision['decision']
        graded.append({**r, 'target': c['target'], 'query': c['query'], 'semanticPass': decision['decision']=='pass',
            'decision': decision['decision'], 'reason': decision['reason'], 'blindIndex': i,
            'initialJudgment': a.get(i), 'independentJudgment': b.get(i),
            'reviewAuthority': 'fixed_prior_reconciled_grade' if i in fixed else 'explicit_reconciliation' if i in resolved else 'two_reviewers_agree'})
    by_arm = {arm: {r['case']: r for r in graded if r['arm'] == arm} for arm in ARMS}
    summary = {'version': VERSION, 'cases': 69, 'predictions': 344, 'newPredictions': 262, 'reusedPredictions': 82,
        'sharedBudgetInputTokens': 413836, 'capacityBlocked': [{'case': 41, 'arm': 'time_images'}],
        'authority': 'Assistant holistic judgments with independent reconciliation, not independent human ground truth.',
        'comparisonScope': 'Full original 32K historical interval across representations, plus two text histories expanded to the median screenshot budget.',
        'budgetCaveat': 'One shared median budget is not equal per-case tokens; underfilled histories are retained without padding.',
        'costBasis': 'API-equivalent value, not subscription charges. Reused responses incur no new sampling usage.',
        'unknownFailedAttemptCost': 'Interrupted original case 148 and rejected v1 preflight returned no usage; not counted as zero cost.',
        'reviewerDisagreements': len(disagreements), 'arms': {}, 'paired': {}}
    for arm, index in by_arm.items():
        vals = list(index.values()); fresh = [r for r in vals if not r.get('reuse')]
        meta = [c for c in load(root / 'data/cases.json') if c['arm'] == arm and not c['limits']]
        summary['arms'][arm] = {'label': NAMES[arm], 'n': len(vals), 'passes': sum(r['semanticPass'] for r in vals),
            'passRate': sum(r['semanticPass'] for r in vals)/len(vals),
            'latencySeconds': stats([r['timing']['dispatchToCompletionSeconds'] for r in vals]),
            'inputTokens': stats([r['usage']['input_tokens'] for r in vals]),
            'outputTokens': stats([r['usage']['output_tokens'] for r in vals]),
            'apiEquivalentUSD': stats([r['apiEquivalentCostUSD'] for r in vals]),
            'newSamplingAPIEquivalentUSD': stats([r['apiEquivalentCostUSD'] for r in fresh]),
            'reused': len(vals)-len(fresh), 'historySpanMinutes': stats([c['historySpanMinutes'] for c in meta])}
    for left, right in itertools.combinations(ARMS, 2): summary['paired'][left+'__'+right] = pair(by_arm[left], by_arm[right])
    output.mkdir(mode=0o700)
    save_rows(output / 'judgments.jsonl', sorted(graded, key=lambda r: (r['case'], r['arm'])))
    save(output / 'summary.json', summary)
    save(output / 'completion.json', {'version': VERSION, 'status': 'scored_and_reconciled',
        'predictionsSHA256': file_hash(root / 'predictions.jsonl'), 'runPlanSHA256': file_hash(root / 'plan.json'),
        'evidenceSHA256': {str(p): file_hash(p) for p in (primary, independent, resolutions, approval, review / 'review-plan.json')},
        'artifactsSHA256': {n: file_hash(output/n) for n in ('judgments.jsonl', 'summary.json')}, 'providerCalls': 0})
    print(canonical(summary))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('draft', 'prepare', 'publish')); p.add_argument('--run', type=Path, required=True)
    p.add_argument('--review', type=Path, required=True); p.add_argument('--prior-scoring', type=Path, action='append', default=[])
    for n in ('primary', 'independent', 'resolutions', 'approval', 'output'): p.add_argument('--'+n, type=Path)
    a = p.parse_args()
    if a.action == 'draft': draft(a.run.resolve(),a.review.resolve())
    elif a.action == 'prepare': prepare(a.run.resolve(), a.review.resolve(), [p.resolve() for p in a.prior_scoring])
    else: publish(a.run.resolve(), a.review.resolve(), a.primary, a.independent, a.resolutions, a.approval, a.output)


if __name__ == '__main__': main()

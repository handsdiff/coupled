#!/usr/bin/env python3
"""Local, label-blinded holistic review and paired analysis of the vision pilot.

No provider calls or automatic semantic grader. Explicit judgments are required.
Frozen predictions and preparation are never overwritten.
"""
import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import random
import statistics

from phase1_read_model_comparison import canonical, file_hash, fingerprint, rows

VARIANTS = ('cleaned_recent', 'raw_ocr_recent', 'screenshots_recent')
NAMES = dict(zip(VARIANTS, ('Cleaned READs', 'Full-window OCR', 'Screenshots')))
VERSION = 'phase1-vision-holistic-v1'


def load(p):
    return json.loads(p.read_text())


def save(p, value):
    with p.open('x') as f:
        f.write(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + '\n')


def save_rows(p, data):
    with p.open('x') as f:
        for row in data:
            f.write(canonical(row) + '\n')


def verify_run(root):
    completion = load(root / 'audit.json')
    assert completion['status'] == 'complete' and completion['completed'] == completion['planned'] == 207
    assert file_hash(root / 'predictions.jsonl') == completion['predictionsSHA256']
    for name, digest in load(root / 'plan.json')['artifactsSHA256'].items():
        assert file_hash(root / name) == digest
    rs = list(rows(root / 'predictions.jsonl'))
    assert len(rs) == len({r['sampleID'] for r in rs}) == len({r['responseID'] for r in rs}) == 207
    assert Counter(r['variant'] for r in rs) == Counter({v: 69 for v in VARIANTS})
    assert not {r['case'] for r in rs}.intersection({118, 126, 127})
    return rs


def prepare(root, review):
    assert not review.exists(), 'Fresh review required'
    rs = verify_run(root)
    cases = {c['exampleID']: c for c in rows(root / 'data/cases.jsonl')}
    random.Random(20260909207).shuffle(rs)
    blinded, key = [], []
    for i, r in enumerate(rs, 1):
        c = cases[r['exampleID']]
        b = {'blindIndex': i, 'reviewID': fingerprint([VERSION, r['sampleID']]), 'case': r['case'],
             'exampleID': r['exampleID'], 'target': c['target'], 'query': c['query'],
             'prediction': r['prediction'], 'predictionSHA256': fingerprint(r['prediction']),
             'validCompletion': r['validCompletion'] and r['nonemptyPrediction']}
        blinded.append(b)
        key.append({'reviewID': b['reviewID'], 'blindIndex': i, 'sampleID': r['sampleID'],
                    'case': r['case'], 'variant': r['variant']})
    review.mkdir(mode=0o700)
    save_rows(review / 'blinded.jsonl', blinded)
    save_rows(review / 'blinding-key.jsonl', key)
    fixed_files=[]
    if (root/'baseline-grades.jsonl').exists():
        baseline={r['sampleID']:r for r in rows(root/'baseline-grades.jsonl')}
        fixed=[{'blindIndex':k['blindIndex'],'sampleID':k['sampleID'],
                'decision':baseline[k['sampleID']]['decision'],'reason':baseline[k['sampleID']]['reason']}
               for k in key if k['sampleID'] in baseline]
        assert len(fixed)==69
        save_rows(review/'fixed-baseline-grades.jsonl',fixed)
        fixed_files=['fixed-baseline-grades.jsonl']
    # A common context reference is available per case; no answer labels, usage,
    # sibling grades or variant identity are exposed in blinded.jsonl.
    save_rows(review / 'case-contexts.jsonl', [{'case': p['case'], 'parts': p['parts'],
        'partsSHA256': p['partsSHA256']} for p in rows(root / 'data/prompts.local.jsonl')
        if p['variant'] == 'cleaned_recent'])
    policy = load(root / 'data/plan.json')['scoringContract']
    save(review / 'policy.json', policy)
    save(review / 'review-plan.json', {'version': VERSION, 'run': str(root.resolve()),
        'runPlanSHA256': file_hash(root / 'plan.json'), 'predictionsSHA256': file_hash(root / 'predictions.jsonl'),
        'cases': 69, 'predictions': 207, 'denominatorFrozen': True,
        'authority': 'assistant judgments, reviewer-reconciled; subject to user adjudication, not independent human ground truth',
        'blinding': 'Individually shuffled outputs. Arm, latency, cost, sibling outputs and prior grades hidden during initial review. Same case target/query and optional shared context reference.',
        'constructionAudit': 'Separate from semantic grades; no post-result case exclusions',
        'fixedHistoricalBaselineGrades':len(fixed) if fixed_files else 0,
        'artifactsSHA256': {n: file_hash(review / n) for n in ['blinded.jsonl', 'blinding-key.jsonl', 'case-contexts.jsonl', 'policy.json']+fixed_files}})
    print(canonical({'review': str(review), 'blindedAnswers': len(blinded)}))


def judgments(review, annotation):
    """Convert explicitly authored blindIndex<TAB>decision<TAB>reason annotations."""
    blind = {r['blindIndex']: r for r in rows(review / 'blinded.jsonl')}
    result = []
    for line in annotation.read_text().splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        index, decision, reason = line.split('\t', 2)
        b = blind[int(index)]
        assert decision in ('pass', 'fail') and reason.strip()
        assert decision != 'pass' or b['validCompletion']
        result.append({k: b[k] for k in ('blindIndex', 'reviewID', 'case', 'predictionSHA256')} |
                      {'decision': decision, 'reason': reason})
    assert len(result) == len({r['blindIndex'] for r in result}) == 207
    assert {r['blindIndex'] for r in result} == set(blind)
    if (review/'fixed-baseline-grades.jsonl').exists():
        chosen={r['blindIndex']:r for r in result}
        for r in rows(review/'fixed-baseline-grades.jsonl'):
            assert chosen[r['blindIndex']]['decision']==r['decision'], 'Existing screenshot baseline grade changed'
    return sorted(result, key=lambda r: r['blindIndex'])


def stats(vals):
    a = sorted(vals)
    return {'mean': statistics.mean(a), 'median': statistics.median(a),
            'p90': a[min(len(a)-1, int(0.9*len(a)))], 'total': sum(a)}


def paired_uncertainty(differences):
    """Descriptive paired resampling; not a population-generalization claim."""
    rng = random.Random(20260909)
    n = len(differences)
    draws = sorted(sum(rng.choices(differences,k=n))/n for _ in range(20000))
    gains, losses = differences.count(1), differences.count(-1)
    discordant = gains + losses
    exact_p = min(1.0, 2*sum(math.comb(discordant,k) for k in range(min(gains,losses)+1))/2**discordant) if discordant else 1.0
    return {'netPassDifference':sum(differences),'netRateDifference':sum(differences)/n,
            'pairedBootstrap95PercentInterval':[draws[500],draws[19499]],
            'exactTwoSidedDiscordantPairP':exact_p,
            'interpretation':'Selected cohort, single sample per arm; descriptive only. No multiple-comparison correction.'}


def publish(root, review, draft, reviewer_path, resolutions_path, out, approval_path=None):
    assert not out.exists(), 'Versioned output required'
    rs = verify_run(root)
    rp = load(review / 'review-plan.json')
    assert rp['predictionsSHA256'] == file_hash(root / 'predictions.jsonl')
    for n, h in rp['artifactsSHA256'].items():
        assert file_hash(review / n) == h
    a, b = judgments(review, draft), judgments(review, reviewer_path)
    resolution_rows = load(resolutions_path)
    resolutions = {r['blindIndex']: r for r in resolution_rows}
    assert len(resolutions) == len(resolution_rows), 'Duplicate reconciliation'
    disagreements = {x['blindIndex'] for x, y in zip(a, b) if x['decision'] != y['decision']}
    assert disagreements <= set(resolutions), 'Every disagreement must be explicitly reconciled'
    assert set(resolutions) <= {r['blindIndex'] for r in a}
    # A joint second look can correct an initially agreed grade. Keep this
    # explicit rather than rewriting either blinded initial assessment.
    for i in set(resolutions) - disagreements:
        assert resolutions[i].get('kind') == 'joint_consistency_correction'
    if (review/'fixed-baseline-grades.jsonl').exists():
        for r in rows(review/'fixed-baseline-grades.jsonl'):
            if r['blindIndex'] in resolutions:
                assert resolutions[r['blindIndex']]['decision']==r['decision'], 'Cannot retroactively regrade fixed screenshot baseline'
    approval = approval_path or review / 'reviewer-reconciliation.md'
    assert approval.is_file(), 'Persist independent reconciliation before publication'
    keys = {k['blindIndex']: k for k in rows(review / 'blinding-key.jsonl')}
    predictions = {r['sampleID']: r for r in rs}
    targets = {c['case']: c for c in rows(root / 'data/cases.jsonl')}
    graded = []
    for x, y in zip(a, b):
        assert all(x[k] == y[k] for k in ('blindIndex', 'reviewID', 'case', 'predictionSHA256'))
        decision = resolutions.get(x['blindIndex'], x)
        assert decision['decision'] in ('pass', 'fail') and decision['reason'].strip()
        r = predictions[keys[x['blindIndex']]['sampleID']]
        assert fingerprint(r['prediction']) == x['predictionSHA256']
        assert decision['decision'] != 'pass' or r['validCompletion'] and r['nonemptyPrediction']
        graded.append({**r, 'target': targets[r['case']]['target'], 'reviewID': x['reviewID'],
            'semanticPass': decision['decision'] == 'pass', 'decision': decision['decision'],
            'reason': decision['reason'], 'initialJudgment': x, 'reviewerJudgment': y,
            'reviewAuthority': 'explicit_reconciliation' if x['blindIndex'] in resolutions else 'two_reviewers_agree'})
    by_case = defaultdict(dict)
    for r in graded:
        by_case[r['case']][r['variant']] = r
    assert len(by_case) == 69 and all(set(v) == set(VARIANTS) for v in by_case.values())
    summary = {'cases': 69, 'predictions': 207, 'arms': {}, 'paired': {},
        'reviewerDisagreements': len(disagreements),
        'jointConsistencyCorrections': len(set(resolutions) - disagreements),
        'providerErrors': 0,
        'scope': load(root / 'plan.json')['scope'],
        'denominator': 'Selected diagnostic cohort; not full-corpus accuracy. One fresh answer per arm/case.',
        'costBasis': 'API-equivalent usage pricing, not subscription billing; unequal input token budgets.',
        'authority': 'Two assistant reviewers, initially blinded to representation, with explicit reconciliation; not independent human ground truth.',
        'patterns': dict(Counter(''.join('1' if d[v]['semanticPass'] else '0' for v in VARIANTS) for d in by_case.values()))}
    for v in VARIANTS:
        arm = [r for r in graded if r['variant'] == v]
        summary['arms'][v] = {'label': NAMES[v], 'passes': sum(r['semanticPass'] for r in arm),
            'passRate': sum(r['semanticPass'] for r in arm)/69,
            'latencySeconds': stats([r['timing']['dispatchToCompletionSeconds'] for r in arm]),
            'inputTokens': stats([r['usage']['input_tokens'] for r in arm]),
            'outputTokens': stats([r['usage']['output_tokens'] for r in arm]),
            'apiEquivalentUSD': stats([r['apiEquivalentCostUSD'] for r in arm])}
    if (root/'baseline-grades.jsonl').exists():
        summary['denominator']='Same selected 69-case diagnostic cohort. 138 expanded text predictions versus 69 reused original screenshot predictions; no new screenshot sampling.'
        summary['costBasis']='API-equivalent usage pricing, not subscription billing. Per-case original screenshot token budgets; underfill and actual usage retained. Baseline reuse incurs zero new usage.'
        summary['executionLineage']=load(root/'lineage.json')
    for before, after in ((VARIANTS[0], VARIANTS[1]), (VARIANTS[0], VARIANTS[2]), (VARIANTS[1], VARIANTS[2])):
        groups = defaultdict(list)
        for c, d in sorted(by_case.items()):
            p, q = d[before]['semanticPass'], d[after]['semanticPass']
            groups['bothPass' if p and q else 'bothFail' if not p and not q else 'gains' if q else 'losses'].append(c)
        summary['paired'][before+'__'+after] = {k: groups[k] for k in ('gains', 'losses', 'bothPass', 'bothFail')}
        summary['paired'][before+'__'+after]['uncertainty'] = paired_uncertainty([
            int(d[after]['semanticPass'])-int(d[before]['semanticPass']) for _,d in sorted(by_case.items())])
    out.mkdir(mode=0o700)
    save_rows(out / 'judgments.jsonl', sorted(graded, key=lambda r:(r['case'], r['variant'])))
    save(out / 'summary.json', summary)
    save(out / 'completion.json', {'version': VERSION, 'status': 'scored_and_reconciled',
        'predictionsSHA256': file_hash(root / 'predictions.jsonl'),
        'evidenceSHA256': {str(p.resolve()): file_hash(p) for p in (draft, reviewer_path, resolutions_path, approval, review/'review-plan.json')},
        'artifactsSHA256': {n: file_hash(out/n) for n in ('judgments.jsonl','summary.json')}})
    print(canonical(summary))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('prepare','publish','check-annotations'))
    p.add_argument('--run', type=Path, required=True)
    p.add_argument('--review', type=Path, required=True)
    p.add_argument('--draft', type=Path)
    p.add_argument('--reviewer', type=Path)
    p.add_argument('--resolutions', type=Path)
    p.add_argument('--output', type=Path)
    p.add_argument('--approval', type=Path)
    a = p.parse_args()
    if a.action == 'prepare': prepare(a.run, a.review)
    elif a.action == 'check-annotations': print(canonical({'annotations':len(judgments(a.review,a.draft)), 'SHA256':file_hash(a.draft)}))
    else: publish(a.run,a.review,a.draft,a.reviewer,a.resolutions,a.output,a.approval)


if __name__ == '__main__': main()

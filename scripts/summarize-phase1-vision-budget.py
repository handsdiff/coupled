#!/usr/bin/env python3
"""Local paired budget analysis from immutable predictions and explicit grades."""
import argparse
import importlib.util
import json
from pathlib import Path
import statistics

spec = importlib.util.spec_from_file_location('vision_analysis', Path(__file__).with_name('analyze-phase1-vision.py'))
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for k in ('run', 'scored', 'previous-scored', 'output'):
        p.add_argument('--' + k, type=Path, required=True)
    a = p.parse_args()
    assert not a.output.exists(), 'Use a new versioned output directory'
    analysis.verify_run(a.run)
    current = {(x['case'], x['variant']): x for x in analysis.rows(a.scored/'judgments.jsonl')}
    previous = {(x['case'], x['variant']): x for x in analysis.rows(a.previous_scored/'judgments.jsonl')}
    lineage = analysis.load(a.run/'lineage.json')
    original_root = Path(lineage['baselineRun'])
    assert analysis.file_hash(a.previous_scored/'completion.json') == lineage['baselineScoringCompletionSHA256']
    for d in (a.scored, a.previous_scored):
        completion = analysis.load(d/'completion.json')
        for n, h in completion['artifactsSHA256'].items():
            assert analysis.file_hash(d/n) == h
    prompts = {(x['case'], x['variant']): x for x in analysis.rows(a.run/'data/prompts.local.jsonl')}
    cs = sorted({k[0] for k in current})
    assert len(cs) == 69 and set(previous) == set(current)
    for c in cs:
        for v in analysis.VARIANTS:
            assert current[c, v]['target'] == previous[c, v]['target']
        assert current[c, 'screenshots_recent']['sampleID'] == previous[c, 'screenshots_recent']['sampleID']
        assert current[c, 'screenshots_recent']['semanticPass'] == previous[c, 'screenshots_recent']['semanticPass']
    result = {'version': 'phase1-vision-budget-analysis-v1', 'cases': 69,
              'pairedExpandedVersusOriginal': {}, 'actualBudgets': {}, 'execution': lineage,
              'scoring': 'Intention-level paired assistant judgments, reconciled; not independent human ground truth.',
              'scope': 'Different chronological history coverage at per-case screenshot input budgets; not a pure modality experiment. The original raw-OCR arm retained older cleaned background; the expanded raw-OCR arm does not.',
              'pricing': 'Frozen usage-derived API equivalents, not subscription charges. No paid API fallback or newly dispatched screenshot requests.'}
    pair_rows = []
    for v in analysis.VARIANTS[:2]:
        groups = {k: [] for k in ('gains', 'losses', 'bothPass', 'bothFail')}
        for c in cs:
            old, new = previous[c, v], current[c, v]
            x, y = old['semanticPass'], new['semanticPass']
            group = 'bothPass' if x and y else 'bothFail' if not x and not y else 'gains' if y else 'losses'
            groups[group].append(c)
            if x != y:
                pair_rows.append({'case': c, 'variant': v, 'change': group, 'target': new['target'],
                                  'originalAnswer': old['prediction'], 'expandedAnswer': new['prediction'],
                                  'originalReason': old['reason'], 'expandedReason': new['reason']})
        result['pairedExpandedVersusOriginal'][v] = dict(groups,
            originalPasses=sum(previous[c, v]['semanticPass'] for c in cs),
            expandedPasses=sum(current[c, v]['semanticPass'] for c in cs),
            uncertainty=analysis.paired_uncertainty([int(current[c, v]['semanticPass'])-int(previous[c, v]['semanticPass']) for c in cs]))
        packing = [prompts[c, v]['budgetPacking'] for c in cs]
        inputs = [current[c, v]['usage']['input_tokens'] for c in cs]
        over = [c for c, p, n in zip(cs, packing, inputs) if n > p['assignedInputTokens']]
        result['actualBudgets'][v] = {
            'inputTokens': analysis.stats(inputs), 'casesOverBudget': over,
            'actualMinusEstimatedTokens': analysis.stats([n-p['estimatedInputTokens'] for p, n in zip(packing, inputs)]),
            'casesAtLeast95PercentOfBudget': sum(n >= .95*p['assignedInputTokens'] for p, n in zip(packing, inputs)),
            'under95PercentCases': [c for c, p, n in zip(cs, packing, inputs) if n < .95*p['assignedInputTokens']],
            'historySpanMinutes': analysis.stats([p['expandedHistorySpanSeconds']/60 for p in packing]),
            'additionalEarlierMinutes': analysis.stats([p['additionalEarlierSeconds']/60 for p in packing]),
            'timeCaveat': 'Wall-clock span includes idle/overnight gaps; not active attention duration.'}
        assert not over
    text = list(analysis.rows(Path(lineage['textRun'])/'predictions.jsonl'))
    new = [r for r in text if not r.get('reusedFrom')]
    assert len(new) == 130 and len(text)-len(new) == 8
    result['newlyDispatchedTextUsage'] = {
        'requests': len(new), 'invalidAnswers': sum(not x['validCompletion'] or not x['nonemptyPrediction'] for x in new),
        'inputTokens': sum(x['usage']['input_tokens'] for x in new),
        'outputTokens': sum(x['usage']['output_tokens'] for x in new),
        'apiEquivalentUSD': sum(x['apiEquivalentCostUSD'] for x in new),
        'latencySeconds': analysis.stats([x['timing']['dispatchToCompletionSeconds'] for x in new])}
    result['expandedArms'] = analysis.load(a.scored/'summary.json')['arms']
    result['originalArms'] = analysis.load(a.previous_scored/'summary.json')['arms']
    result['originalPredictionsSHA256'] = analysis.file_hash(original_root/'predictions.jsonl')
    a.output.mkdir(mode=0o700)
    analysis.save(a.output/'analysis.json', result)
    analysis.save_rows(a.output/'changed-cases.jsonl', sorted(pair_rows, key=lambda x: (x['case'], x['variant'])))
    analysis.save(a.output/'completion.json', {'version': result['version'], 'status': 'complete',
        'sourceDigests': {str(d.resolve()): analysis.file_hash(d) for d in [a.scored/'completion.json', a.previous_scored/'completion.json', a.run/'lineage.json', Path(__file__)]},
        'artifactsSHA256': {n: analysis.file_hash(a.output/n) for n in ('analysis.json', 'changed-cases.jsonl')}})
    print(json.dumps({'output': str(a.output), 'pairedChanges': len(pair_rows), 'newRequests': len(new), 'overBudget': 0}))


if __name__ == '__main__':
    main()

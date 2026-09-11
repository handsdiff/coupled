#!/usr/bin/env python3
"""Audit saved pane-first OCR requests and bind independent screenshot judgments.

No network calls and no edits to semantic events or training artifacts.
"""
import argparse
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import statistics


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def main(root, surfaces=None):
    spec = importlib.util.spec_from_file_location('probe_review', Path(__file__).with_name('review-phase1-ocr-probe.py'))
    probe = importlib.util.module_from_spec(spec); spec.loader.exec_module(probe)
    pilot = root/'pilot'
    plan, rows = probe.audit_run(pilot)
    assert plan['models'] == ['chatgpt/gpt-6-astra'] and plan['reasoning'] == 'low'
    assert len(rows) == plan['plannedRequests']
    manual_path = root/'analysis/manual-review.json'
    manual = json.loads(manual_path.read_text())
    assert manual['planSHA256'] == sha(pilot/'plan.json')
    reviews = {r['requestIndex']: r for r in manual['reviews']}
    assert len(reviews) == len(manual['reviews']) == len(rows)
    local = {r['sourceRecordID']: r for r in json.loads((root/'local-review-evidence.json').read_text())}
    prepared = {d['documentID']: d for d in json.loads((root/'documents.json').read_text())}
    assert prepared == {d['documentID']: d for d in json.loads((pilot/'documents.json').read_text())}
    final_sources = {}
    if surfaces:
        wanted = set(local)
        wanted.update(rid for item in local.values() for rid in item['previousSourceRecordIDs'])
        found = {}
        for day in (2, 3, 4, 7):
            directory = surfaces/f'sep{day}-surfaces'
            manifest = json.loads((directory/'read-surface-evidence.json').read_text())
            file = directory/'read-surfaces.jsonl'
            assert sha(file) == manifest['artifacts']['digestsSHA256'][file.name]
            final_sources[str(file)] = sha(file)
            with file.open() as f:
                for line in f:
                    r = json.loads(line)
                    if r['sourceRecordID'] in wanted:
                        found[r['sourceRecordID']] = {'capturedAt': r['capturedAt'], 'ocrText': r['content']}
        assert wanted == set(found)
        for rid, doc in prepared.items():
            assert found[rid] == {'capturedAt': doc['capturedAt'], 'ocrText': doc['text']}
            assert [found[k] for k in local[rid]['previousSourceRecordIDs']] == doc['previousObservations']
    timings, results, review_rows = [], [], []
    for i, doc, result in rows:
        assert result['validCompletion'] and result['editContractValid']
        assert all(r['capturedAt'] < doc['capturedAt'] for r in doc['previousObservations'])
        assert len(doc['previousObservations']) <= 3
        evidence = local[doc['documentID']]
        assert sha(evidence['screenshot']) == evidence['screenshotSHA256']
        review = reviews[i]
        assert review['sourceRecordID'] == doc['documentID']
        review_rows.append(dict(review, cases=evidence['cases'], candidateLine=evidence['candidateLine'],
            screenshot=evidence['screenshot'], screenshotSHA256=evidence['screenshotSHA256'],
            resultSHA256=sha(pilot/f'results/{i:04d}/result.json'), appliedEdits=len(result['edits'])))
        timings.append(result['timing']['dispatchToCompletionSeconds']); results.append(result)
    summary = {
        'version': 'pane-first-ocr-review-v1', 'authority': 'shadow_only',
        'planSHA256': sha(pilot/'plan.json'), 'manualReviewSHA256': sha(manual_path),
        'preparationSHA256': sha(root/'preparation.json'), 'analysisSHA256': sha(__file__),
        'requestsCompleted': len(results), 'validEditContracts': len(results),
        'appliedEdits': sum(len(r['edits']) for r in results),
        'manualAssessment': dict(Counter(r['assessment'] for r in review_rows)),
        'medianLatencySeconds': statistics.median(timings),
        'meanLatencySeconds': statistics.mean(timings), 'sumGenerationSeconds': sum(timings),
        'inputTokens': sum(r['usage']['input_tokens'] for r in results),
        'outputTokens': sum(r['usage']['output_tokens'] for r in results),
        'reasoningTokens': sum(r['usage'].get('output_tokens_details', {}).get('reasoning_tokens', 0) for r in results),
        'apiEquivalentUncachedUSD': sum(r['apiEquivalentUncachedUSD'] for r in results),
        'apiEquivalentWithReportedCacheUSD': sum(r['apiEquivalentWithReportedCacheUSD'] for r in results),
        'costPolicy': 'Subscription transport; API-equivalent estimate is not an actual charge.',
        'qualityCaveat': 'Purpose-selected observations, not corpus accuracy. Screenshot review is assistant judgment, not human ground truth. Valid patches do not prove complete OCR repair.',
        'noFutureObservations': True, 'screenshotsSent': False, 'writeTargetsSent': False,
        'productionArtifactsChanged': False, 'reviews': review_rows,
        'finalPaneInputsByteIdenticalToTestedInputs': True if surfaces else None,
        'finalPaneSourceSHA256': final_sources,
    }
    (root/'analysis/summary.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True)+'\n')
    page = ['# Pane-first Astra-low OCR pilot', '',
        'Shadow test only. No collector, raw journal, semantic-event, episode or training-target changes were made by this model stage.', '',
        f"All {len(results)} requests completed and replayed exactly from saved responses; {summary['appliedEdits']} exact edits. "
        f"Median latency {summary['medianLatencySeconds']:.2f}s; mean {summary['meanLatencySeconds']:.2f}s.", '',
        f"{summary['inputTokens']:,} input tokens and {summary['outputTokens']:,} output tokens. "
        f"Uncached API-equivalent ${summary['apiEquivalentUncachedUSD']:.4f}; used the subscription, not API billing.", '',
        '## What this establishes', '',
        '- Correct the selected image region first, then give the model its OCR and up to three strictly earlier same-surface observations.',
        '- Exact edits preserve untouched text and make each alteration auditable. No screenshot or future WRITE target was transmitted.',
        '- Astra corrected recognizable terms, some identifiers and some reading order. It did not reliably repair tables, severely damaged text or every identifier.',
        '- The three genuine-new-information controls retain their new content. No automatic episode merge or READ removal follows from this report.',
        '- This is not a complete production OCR resolver. Keep the preserved OCR and explicit unresolved cases; do not call syntactically valid patches accuracy.', '',
        '## Screenshot review', '',
        'The following are assistant judgments against the actual screenshot, not user adjudications. Case numbers refer to the frozen 687-example cohort. A useful correction can still leave errors.', '',
        '| Request | Cases | Assessment | Specific result and limitation |', '|---|---|---|---|']
    for r in review_rows:
        cases = ', '.join(str(n) for n in r['cases'] if n is not None)
        page.append(f"| {r['requestIndex'] + 1} | {cases} | {r['assessment'].replace('_', ' ')} | {r['details'].replace('|', '/')} |")
    page += ['', '## Reproduce without provider calls', '', '```sh',
        f'python3 -B scripts/report-phase1-pane-ocr-pilot.py {root.relative_to(Path.cwd()) if root.is_relative_to(Path.cwd()) else root}'
        + (f' --surfaces {surfaces}' if surfaces else ''),
        '```', '', 'Saved requests, raw response bodies, decoded edits, corrected text, timings, usage, screenshot references and their hashes remain beside this report.', '']
    (root/'analysis/REPORT.md').write_text('\n'.join(page))
    print(json.dumps({k: v for k, v in summary.items() if k != 'reviews'}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--surfaces', type=Path, help='Verify saved request texts also match a finalized pane candidate')
    args = parser.parse_args()
    main(args.directory.resolve(), args.surfaces.resolve() if args.surfaces else None)

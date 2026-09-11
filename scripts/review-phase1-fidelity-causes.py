#!/usr/bin/env python3
"""Render a source-bound, diagnostic-only root-cause review. No inference or fixes.

The two line-order counterfactuals are experiments over already retained text,
not a proposed universal reading-order algorithm. Annotation is explicitly
separate from measured evidence and never consumed by training construction.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import resource
import sys

from phase1_read_boundary import app_name, explain, residual_runs, tokens

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text())


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def save(path, data):
    with path.open('x') as f:
        json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write('\n')


def ordered_lines(content, pane):
    """Return only a permutation of existing lines; reject ambiguous geometry."""
    boxes = {}
    for line in pane['lines']:
        boxes.setdefault(line['text'], []).append(line['boundingBox'])
    lines = content.splitlines()
    if any(len(boxes.get(line, [])) != 1 for line in lines):
        raise ValueError('Semantic line lacks a unique retained bounding box')
    def key(line):
        b = boxes[line][0]
        return (-b['y'] - b['height'] / 2, b['x'])
    ordered = sorted(lines, key=key)
    assert Counter(ordered) == Counter(lines)
    return '\n'.join(ordered)


def residuals(text, prior, draft, field):
    observed = tokens(text)
    dc = explain(tokens(draft), observed)
    fc = explain(tokens(field), observed) if len(dc) >= 3 else set()
    return residual_runs(observed, explain(tokens(prior), observed) | dc | fc)


def ordering_probe(data, line):
    boundary = next(b for b in data['boundaries'] if b['candidateLine'] == line)
    candidate = data['candidates'][boundary['leftCandidateID']]
    assert len(boundary['reads']) == 1
    read_row = boundary['reads'][0]
    event = read_row['event']
    value = json.loads(event['serialized'])
    assert app_name(value['source']['application']) == app_name(boundary['destination']['application'])
    content = value['content']
    evidence = read_row['v10Assessment']['boundaryEvidence']
    assert evidence['observationID'] == event['sourceEventID']
    prior = read_row['prior']
    assert prior['capturedAt'] < boundary['beganAt']
    field = candidate['members'][0]['beforeLogicalValue']
    args = (prior['content'], boundary['draft'], field)
    before = residuals(content, *args)
    assert before == evidence['unexplainedRuns'], 'Must reproduce frozen baseline before perturbation'
    assert len(read_row['paneRecordIDs']) == 1
    pane = data['panes'][read_row['paneRecordIDs'][0]]
    after_content = ordered_lines(content, pane)
    after = residuals(after_content, *args)
    assert sum(x['end'] - x['start'] for x in before) == 2 and after == []
    return {
        'candidateLine': line, 'cases': [boundary['leftCase'], boundary['rightCase']],
        'eventID': event['sourceEventID'], 'paneEvidenceID': pane['evidenceID'],
        'priorObservationID': evidence['priorObservationID'],
        'priorCapturedAt': prior['capturedAt'], 'onset': boundary['beganAt'],
        'beforeUnexplained': before, 'afterUnexplained': after,
        'beforeText': content, 'afterText': after_content,
        'sameLineMultiset': True, 'charactersEdited': 0,
        'limitation': 'Selected single-column cases only. No new OCR, target rewrite, general table-order solution, or production merge.'}


def verify_island(case):
    assert len(case['islandEvidence']) == 1
    source = case['islandEvidence'][0]
    value = source['before']
    provenance = [True] * len(value)
    for edit in source['localEdits']:
        at, removed, inserted = edit['offset'], edit['removed'], edit['inserted']
        assert value[at:at + len(removed)] == removed, 'Local edit does not reconstruct source'
        value = value[:at] + inserted + value[at + len(removed):]
        provenance = provenance[:at] + [False] * len(inserted) + provenance[at + len(removed):]
    assert value == source['after']
    before = source['before']
    start = 0
    while start < min(len(before), len(value)) and before[start] == value[start]:
        start += 1
    tail = 0
    while tail < min(len(before), len(value)) - start and before[-tail - 1] == value[-tail - 1]:
        tail += 1
    end = len(value) - tail if tail else len(value)
    assert value[start:end] == source['netInserted']
    for run in source['runs']:
        offset = start + run['offsetInNetEdit']
        assert value[offset:offset + run['length']] == run['content']
        assert all(provenance[offset:offset + run['length']]), 'Reported island was newly inserted'
    return {'case': case['case'], 'localEditsReplayExactly': True,
            'islandCharacters': sum(r['length'] for r in source['runs']),
            'islandCount': len(source['runs']), 'rawPath': source['rawPath'],
            'rawLines': [e['rawLine'] for e in source['localEdits']],
            'interpretation': 'Unchanged initial-field spans inside the minimal net insertion; not permission to delete these spans from a final thought.'}


def link(path, label=None, line=None):
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    return f'[{label or p.name}](<{p}{":" + str(line) if line else ""}>)'


def source_links(data, boundary):
    result = []
    seen = set()
    for r in boundary['reads']:
        for rid in r['rawRecordIDs']:
            raw = data['raw'][rid]
            result.append(link(raw['auditSourcePath'], f"raw {raw['auditSourceLine']}", raw['auditSourceLine']))
            image = raw.get('screenshotRelativePath')
            if image:
                p = Path(raw['auditSourcePath']).parent / image
                if str(p) not in seen:
                    result.append(link(p, 'screenshot'))
                    seen.add(str(p))
        for pid in r['paneRecordIDs']:
            pane = data['panes'][pid]
            result.append(link(pane['evidencePath'], f"pane {pane['fileLine']}", pane['fileLine']))
    return '; '.join(dict.fromkeys(result))


def render(source, output):
    if output.exists():
        raise ValueError('Use a fresh report directory')
    manifest = read(source / 'manifest.json')
    assert digest(source / 'stage-evidence.json') == manifest['selectedRawRecordsSHA256']
    for relative, expected in manifest['sourceHashes'].items():
        assert digest(ROOT / relative) == expected, ('Frozen input changed', relative)
    data = read(source / 'stage-evidence.json')
    annotations = read(source / 'review-annotations.json')
    boundaries = {b['candidateLine']: b for b in data['boundaries']}
    cases = {c['case']: c for c in data['cases']}
    assert len(cases) == 74 and sum(c['original67'] for c in cases.values()) == 67
    assert set(map(int, annotations['boundaries'])) == set(boundaries) and len(boundaries) == 39
    probes = [ordering_probe(data, n) for n in [452, 1064]]
    islands = [verify_island(cases[n]) for n in annotations['oldTextIslandCases']]
    # Bind the independently replayed edit list back to reducer-selected states.
    for n in annotations['oldTextIslandCases']:
        case = cases[n]
        source_edit = case['islandEvidence'][0]
        members = data['candidates'][case['candidateID']]['members']
        assert source_edit['before'] == members[0]['beforeLogicalValue']
        assert source_edit['after'] == members[-1]['selectedTerminalLogicalValue']
        assert [e['rawLine'] for e in source_edit['localEdits']] == [m['rawLine'] for m in members]
    # The corrupt AX target in case 9 is not explained by a later typed change.
    last_member = data['candidates'][cases[9]['candidateID']]['members'][-1]
    last_raw = data['raw'][last_member['sourceRecordID']]
    frame9 = next(r for r in data['raw'].values()
                  if r['auditSourcePath'] == last_raw['auditSourcePath'] and r['auditSourceLine'] == 396)
    before_return = last_raw['returnCheckpoints'][0]
    assert frame9['capturedAt'] < before_return['inputObservedAt']
    assert not [e for e in last_raw['inputEvents'] if e['mutationCapable'] and
                frame9['capturedAt'] <= e['observedAt'] <= before_return['inputObservedAt']]
    assert last_member['selectedTerminalObservation']['observationID'] == before_return['observation']['observationID']
    image_hashes = {}
    for raw in data['raw'].values():
        if not raw.get('screenshotRelativePath'):
            continue
        p = Path(raw['auditSourcePath']).parent / raw['screenshotRelativePath']
        actual = image_hashes.setdefault(str(p), None)
        if actual is None:
            image_hashes[str(p)] = actual = digest(ROOT / p)
        if raw.get('screenshotSHA256'):
            assert actual == raw['screenshotSHA256'], ('Screenshot changed', p)
    prefix_checks = []
    for n in map(int, annotations['prefixCases']):
        case = cases[n]
        assert len(case['prefixEvidence']) == 1
        p = case['prefixEvidence'][0]
        before, current = (data['raw'][p[k]['id']] for k in ('previous', 'current'))
        onset = data['candidates'][case['candidateID']]['onsetEvidence']
        assert current['before']['value'] == '' and onset['proofReason'] == 'empty_prompt_observed'
        prefix_checks.append({'case': n, 'emptyAXBefore': True,
            'onsetAcceptedFromEmptyAX': True, 'gapSeconds': p['gapSeconds'],
            'sameElementHash': before['targetIdentity']['elementHash'] == current['targetIdentity']['elementHash'],
            'priorHadReturn': bool(before.get('returnCheckpoints')),
            'previousRawRecordID': before['recordID'], 'currentRawRecordID': current['recordID'],
            'completeRestorationProved': False})
    counts = Counter(a['disposition'] for a in annotations['boundaries'].values())
    certainty = Counter(a['certainty'] for a in annotations['boundaries'].values())
    assert counts['retain_boundary'] == 3
    assert counts['already_proved_in_v10_diagnostic'] == 9
    ledger = []
    for n, c in sorted(cases.items()):
        bs = [dict(candidateLine=i, **annotations['boundaries'][str(i)]) for i in c['boundaryGroups']]
        entry = {'case': n, 'original67': c['original67'], 'exampleID': c['cohort']['exampleID'],
                 'candidateID': c['candidateID'], 'boundaryFindings': bs,
                 'prefixFinding': annotations['prefixCases'].get(str(n)),
                 'islandCheck': next((i for i in islands if i['case'] == n), None),
                 'otherFinding': annotations['case9'] if n == 9 else None,
                 'productionRepairImplemented': False}
        assert bs or entry['prefixFinding'] or entry['islandCheck'] or entry['otherFinding']
        ledger.append(entry)
    output.mkdir(parents=True)
    save(output / 'case-ledger.json', ledger)
    save(output / 'checks.json', {'orderingCounterfactuals': probes, 'oldTextIslandReplays': islands,
                                'prefixChecks': prefix_checks, 'frozenSourceHashesVerified': True,
                                'screenshotHashes': image_hashes,
                                'case9ImageBeforeReturn': frame9['capturedAt'],
                                'case9ReturnAt': before_return['inputObservedAt'],
                                'case9NoInterveningRecordedMutation': True})
    md = ['# Sep 2–10: root-cause audit of the 67 flagged examples', '',
          'Diagnostic only. No production changes, provider calls, target edits, or capture changes. '
          'Numbers here refer to the frozen **687-example cohort**, not earlier UI indices.', '',
          '## Conclusion', '',
          'The claim that the remaining 27 boundaries were all OCR-recognition failures was incorrect. '
          'They include pane clipping, visible occlusion, wrongly ordered correctly recognized lines, '
          'UI changes, and incomplete prior/draft matching. Better language-model spelling correction cannot solve all of these.', '',
          '- All **67 original flagged examples** have individual ledger entries; **74 total cases** include controls and unresolved 529.',
          '- **39 shared boundary neighborhoods** were examined. The revised human-grouping recommendation is **36 repair candidates / 3 keep**, not 35/4: cases 360–361 already had the supposedly new response before onset.',
          '- **9** of those 36 already pass the existing v10 diagnostic; **27** still fail it. This audit does not promote v10 or claim 27 safe automatic merges.',
          f"- **{certainty['mechanism_partly_unresolved']} boundary neighborhoods** retain an explicit partly unresolved mechanism. A precise residual and source are recorded; no fabricated definitive cause or repair.",
          '- The **11 missing-prefix cases**, **8 old-text-island cases**, and **case 9** are overlapping categories, not extra disjoint counts.', '',
          '## Most useful cases', '',
          '| Cases | First demonstrated problem | Consequence |', '|---|---|---|',
          '| 369–370, 574–575, 584 | Selected pane cuts visible text | Fix geometry before judging OCR-correction models. Full screenshots survive. |',
          '| 245–246, 580–581 | Correct words emitted in the wrong line order | Same lines, geometrically reordered: 2 unexplained words → 0 in each case. |',
          '| 239–240, 380, 501–502 | Genuine recognition/layout corruption | Text is readable inside the pane in the screenshot. |',
          '| 128, 268–269 | Partially visible bottom lines | Do not invent unseen text; distinguish viewport edge from bad AX pane. |',
          '| 381–382 | Floating UI button covers words | Screenshot is faithful, but text is partly occluded. |',
          '| 457, 587, 628–630 | Status/toolbar/view changes | Some words really changed; whether they split a thought is policy, not spelling. |',
          '| 410, 499, 523, 526 | Empty AX is not an empty composer | Truncated thought gets accepted with the wrong initial query. |',
          '| 9 | AX value contradicts the pre-Return screenshot | Existing reducer targets the wrong document state. |', '',
          '## What the audit establishes—and does not', '',
          'The complete images are retained for the demonstrated pane defects. These are downstream selection failures, '
          'not justification for recollecting the corpus. Partial viewport glyphs and overlays are present in the actual pixels; '
          'text behind them cannot be recovered from that image alone. Earlier views sometimes provide it. '
          'For the false-empty WRITE cases, earlier fragments and screenshots often survive, but that does **not** establish '
          'a universally correct pre-mutation query or authorize concatenating snapshots. Case 529 stays unresolved.', '',
          'The checks reproduce the two selected ordering baselines before changing only line permutation. '
          'They also replay all eight old-text-island trajectories exactly and verify the identified spans were already '
          'in the initial field. They do not prove every human grouping judgment or a universal table-order algorithm.', '',
          '## Recommended next slice (not implemented)', '',
          '1. Correct the verified pane-edge and single-column line-order defects in shadow output, using retained images/boxes; protect genuine-new-response controls.',
          '2. Replay the same 39 boundaries and inspect the remaining residuals. Separate UI/status boundary policy from recognition repair.',
          '3. Independently address false-empty composer onset and case 9; preserve unresolved cases instead of teaching chopped or corrupt thoughts.',
          '4. Only then measure whether any residual, genuinely visual OCR error merits another model test.', '',
          '## Boundary-by-boundary evidence', '']
    for i, b in sorted(boundaries.items()):
        a = annotations['boundaries'][str(i)]
        names = str(b['leftCase']) + (f" → {b['rightCase']}" if b['rightCase'] is not None else ' → history-only continuation')
        md += [f'### Cases {names} — boundary {i}', '',
               f"Stage: **{a['stage']}**. Evidence: `{a['certainty']}`. Recommendation: `{a['disposition']}`.", '', a['finding'], '',
               f"Onset: `{b['beganAt']}`; following mutation: `{b['nextBeganAt']}`.", '']
        for rr in b['reads']:
            ev = rr['v10Assessment']['boundaryEvidence']
            remnants = ' / '.join(x['text'] for x in ev.get('unexplainedRuns', [])) or '(fully explained in v10)'
            md += [f"READ `{rr['event']['sourceEventID']}` at `{rr['event']['availableAt']}`.", '',
                   'Unexplained text: ' + json.dumps(remnants, ensure_ascii=False), '']
            if ev.get('priorObservationID'):
                md += [f"Selected earlier evidence: `{ev['priorObservationID']}` at `{ev.get('priorCapturedAt')}`.", '']
        md += [source_links(data, b), '']
    md += ['## Every case: WRITE evidence and diagnosis', '', annotations['oldTextIslandInterpretation'], '']
    for entry in ledger:
        n = entry['case']; c = cases[n]; ca = data['candidates'][c['candidateID']]
        md += [f'### Case {n}' + ('' if entry['original67'] else ' — additional/control'), '',
               'Target: ' + json.dumps(c['cohort']['targetText'], ensure_ascii=False), '']
        if entry['prefixFinding']:
            md += ['**Onset:** ' + entry['prefixFinding'], '']
        if entry['islandCheck']:
            row = entry['islandCheck']
            md += [f"**Old text:** {row['islandCharacters']} retained initial-field characters across {row['islandCount']} runs occur inside the net insertion; all local edits replay exactly.", '']
        if entry['otherFinding']:
            md += [entry['otherFinding']['finding'], '', link(entry['otherFinding']['screenshot'], 'Pre-Return screenshot'), '']
        for b in entry['boundaryFindings']:
            md += [f"Boundary {b['candidateLine']}: {b['finding']}", '']
        md += ['Raw member evidence: ' + '; '.join(link(m['rawPath'], f"raw {m['rawLine']}", m['rawLine']) for m in ca['members']), '']
    md += ['## Reproducibility', '',
           'Extraction: `audit-phase1-fidelity-causes.py`. Rendering and checks: `review-phase1-fidelity-causes.py`. '
           'Manual diagnoses: sibling `review-annotations.json`. All source files are private local artifacts. '
           'Raw records have original line hashes; frozen corpus/pane inputs are hash-checked. '
           'The report never imports training constructors or changes their outputs.', '']
    with (output / 'REPORT.md').open('x') as f:
        f.write('\n'.join(md))
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024**2 if sys.platform == 'darwin' else 1024)
    assert peak < 1024, 'Review exceeded 1 GiB RSS'
    save(output / 'manifest.json', {
        'version': 'phase1-fidelity-causes-review-v1', 'authority': 'diagnostic_only',
        'originalFlaggedCount': 67, 'reviewCaseCount': len(ledger), 'boundaryCount': len(boundaries),
        'dispositions': dict(counts), 'certaintyCounts': dict(certainty), 'peakRSSMiB': peak,
        'codeSHA256': digest(Path(__file__)), 'matcherSHA256': digest(ROOT/'scripts/phase1_read_boundary.py'),
        'inputs': {p.name: digest(p) for p in [source/'manifest.json', source/'stage-evidence.json', source/'review-annotations.json']},
        'outputs': {p.name: digest(p) for p in sorted(output.iterdir()) if p.is_file()},
        'automaticRepairs': 0, 'providerCalls': 0, 'collectorChanges': 0})
    print(json.dumps({'report': str(output/'REPORT.md'), 'dispositions': counts, 'certainty': certainty, 'peakRSSMiB': round(peak, 1)}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    render(args.source.resolve(), args.output.resolve())

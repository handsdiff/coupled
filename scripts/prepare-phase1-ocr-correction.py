#!/usr/bin/env python3
"""Build text-only correction inputs plus strictly local boundary audit evidence."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import random


def rows(path):
    with Path(path).open() as source:
        for line in source:
            if line.strip():
                yield json.loads(line)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as source:
        for part in iter(lambda: source.read(1024 * 1024), b''):
            h.update(part)
    return h.hexdigest()


def dump(path, value):
    with Path(path).open('x') as out:
        out.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n')


def prepare(project, output):
    output.mkdir(parents=True, exist_ok=False)
    base = project / 'coupled-data/sep02-10-training-prep-20260910'
    review = project / 'coupled-data/sep02-10-frontier-prep-20260910'
    probe_path = project / 'coupled-data/sep02-10-episode-boundary-v10-review-20260910/probe.json'
    evidence = json.loads((review / 'fidelity-boundary-evidence.json').read_text())
    probe = {r['candidateLine']: r for r in json.loads(probe_path.read_text())}
    events = {r['sourceEventID']: r for r in rows(base / 'micro-corpus/events.jsonl')}
    candidate_ids = {r['leftCandidateID'] for r in evidence}
    candidates = {r['candidateID']: r for r in rows(base / 'episodes/raw-episode-candidates.jsonl')
                  if r['candidateID'] in candidate_ids}
    needed_prior = {a['boundaryEvidence'].get('priorObservationID') for r in probe.values() for a in r['assessments']}
    target_ids = {a['eventID'] for r in evidence for a in r['assessments']}
    rng = random.Random(17)
    by_app = defaultdict(list)
    for r in events.values():
        if r['kind'] != 'read' or r['sourceEventID'] in target_ids:
            continue
        value = json.loads(r['serialized'])
        if 100 <= len(value.get('content', '')) <= 15000:
            by_app[value.get('source', {}).get('application', 'unknown')].append(r)
    controls = []
    for app, group in sorted(by_app.items()):
        controls.extend(rng.sample(group, min(5, len(group))))
    selected_events = target_ids | {r['sourceEventID'] for r in controls} | (needed_prior & events.keys())
    raw_ids = set().union(*(set(events[e].get('sourceRecordIDs', [])) for e in selected_events))
    raw_ids.update(needed_prior)
    pane_paths = [project / f'coupled-data/sep02-04-semantic-v24-review-20260906/sep{d}-surfaces-r2/read-surfaces.jsonl' for d in (2, 3, 4)]
    pane_paths.append(base / 'new-sep7-surfaces-native/read-surfaces.jsonl')
    priors, panes = {}, {}
    for path in pane_paths:
        for r in rows(path):
            if r['evidenceID'] in needed_prior or r['sourceRecordID'] in raw_ids:
                panes[r['sourceRecordID']] = r
                if r['evidenceID'] in needed_prior:
                    priors[r['evidenceID']] = dict(r, representation='full_pane_ocr', sourcePath=str(path))
                    raw_ids.add(r['sourceRecordID'])
    raw_paths = sorted({project / r['leftRawPath'] for r in evidence})
    raw = {}
    for path in raw_paths:
        for line, r in enumerate(rows(path), 1):
            if r.get('recordID') in raw_ids:
                raw[r['recordID']] = dict(r, sourcePath=str(path), sourceLine=line)
                if r['recordID'] in needed_prior:
                    priors[r['recordID']] = dict(r, representation='raw_ocr', sourcePath=str(path), sourceLine=line)
    def event_view(e):
        value = json.loads(e['serialized'])
        sources = [raw[r] for r in e.get('sourceRecordIDs', []) if r in raw and raw[r].get('screenshotRelativePath')]
        sources.sort(key=lambda r: (r.get('capturedAt', ''), r['recordID']))
        images = []
        for r in sources:
            image = Path(r['sourcePath']).parent / r['screenshotRelativePath']
            images.append({'path': str(image), 'capturedAt': r.get('capturedAt'),
                           'sourceRecordID': r['recordID'], 'sourceLine': r['sourceLine']})
        return {'recordID': e['sourceEventID'], 'capturedAt': e['availableAt'],
                'content': value.get('content', ''), 'representation': 'semantic_read',
                'application': value.get('source', {}).get('application'), 'screenshots': images}
    for rid in needed_prior & events.keys():
        priors[rid] = event_view(events[rid])
    documents = {}
    def add(view, role, neighborhood=None):
        text = view['content']
        key = hashlib.sha256(text.encode()).hexdigest()
        doc = documents.setdefault(key, {'documentID': key, 'text': text, 'references': []})
        ref = {k: v for k, v in view.items() if k not in {'content', 'comparisonContent', 'lines', 'comparisonLines', 'surfaceSelection'}}
        if 'sourceRecordID' in view and view['sourceRecordID'] in raw:
            source = raw[view['sourceRecordID']]
            if source.get('screenshotRelativePath'):
                ref['screenshots'] = [{'path': str(Path(source['sourcePath']).parent / source['screenshotRelativePath']),
                                       'capturedAt': source.get('capturedAt')}]
        if not ref.get('screenshots') and view.get('screenshotRelativePath') and view.get('sourcePath'):
            ref['screenshots'] = [{'path': str(Path(view['sourcePath']).parent / view['screenshotRelativePath']),
                                   'capturedAt': view.get('capturedAt')}]
        ref.update(role=role, candidateLine=neighborhood)
        if ref not in doc['references']:
            doc['references'].append(ref)
        return key
    neighborhoods = []
    for r in evidence:
        draft = r['boundary'].get('internalRevisionEvidence', {}).get('currentNetEdit', {}).get('content') or r['draft']
        candidate = candidates[r['leftCandidateID']]
        initial = candidate['members'][0].get('beforeLogicalValue') or ''
        pairs = []
        for assessment in probe[r['candidateLine']]['assessments']:
            current = event_view(events[assessment['eventID']])
            ev = assessment['boundaryEvidence']
            prior_id = ev.get('priorObservationID')
            prior = priors.get(prior_id)
            if prior_id and prior is None:
                raise ValueError(f'Missing comparison observation {prior_id}')
            if prior and not prior['capturedAt'] < r['beganAt']:
                raise ValueError('Prior observation violates onset cutoff')
            pairs.append({'eventID': assessment['eventID'], 'baselineAssessment': assessment,
                          'currentDocumentID': add(current, 'intervening_read', r['candidateLine']),
                          'priorDocumentID': add(prior, 'pre_onset_comparison', r['candidateLine']) if prior else None})
        neighborhoods.append({'candidateLine': r['candidateLine'], 'leftCase': r['leftCase'], 'rightCase': r['rightCase'],
                              'beganAt': r['beganAt'], 'pairs': pairs, 'draftForLocalAuditOnly': draft,
                              'initialFieldForLocalAuditOnly': initial, 'application': r['destination']['application'],
                              'expectedBoundary': 'retain_new_information' if r['candidateLine'] in {645, 832, 1079} else 'repair_non_novel',
                              'groundTruthCaveat': 'Human/reviewer boundary judgments; not character-level OCR ground truth'})
    for e in controls:
        add(event_view(e), 'random_ordinary_read_control')
    dump(output / 'documents.json', list(documents.values()))
    dump(output / 'neighborhoods.local.json', neighborhoods)
    dump(output / 'manifest.json', {'version': 'phase1-ocr-correction-inputs-v1', 'neighborhoods': len(neighborhoods),
          'interveningReadOccurrences': sum(len(n['pairs']) for n in neighborhoods), 'uniqueTexts': len(documents),
          'randomOrdinaryReadControls': len(controls), 'controlPolicy': 'Seed 17, five per application; not presumed error-free',
          'modelInput': 'One ocrText string only; no target, draft, future observation, screenshot, or comparison text',
          'experimentScope': 'Correction of current semantic READ text and its selected pre-onset comparison; not a full pre-deduplication pipeline replay',
          'sourceSHA256': {str(p): sha(p) for p in [review / 'fidelity-boundary-evidence.json', probe_path, base / 'micro-corpus/events.jsonl', *pane_paths]},
          'documentsSHA256': sha(output / 'documents.json'), 'localAuditSHA256': sha(output / 'neighborhoods.local.json')})
    print(json.dumps({'documents': len(documents), 'plannedModelCalls': len(documents) * 2,
                      'neighborhoods': len(neighborhoods), 'controls': len(controls)}, indent=2))


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--output', required=True, type=Path)
    a = p.parse_args(); prepare(Path.cwd(), a.output)

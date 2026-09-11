#!/usr/bin/env python3
"""Prepare the existing 36 boundary cases + 3 novelty controls, no remote calls.

Retains original correction inputs and selects <=3 original earlier full-pane
OCR views using exact session/window/title/pane identity. Raw files stream once.
"""
import argparse
from bisect import bisect_left
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path


def read(path):
    return json.loads(Path(path).read_text())


def rows(path):
    with Path(path).open() as f:
        for line in f:
            if line.strip(): yield json.loads(line)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''): h.update(chunk)
    return h.hexdigest()


def canonical(data):
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def write(path, data):
    with Path(path).open('x') as f:
        f.write(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + '\n')


def same_pane(target, view):
    if not target.get('surfaceKey') or target['surfaceKey'][:4] != view['surfaceKey'][:4]: return False
    if target['surfaceKey'] == view['surfaceKey']: return True
    a, b = target.get('regionOfInterest'), view.get('regionOfInterest')
    if not a or not b: return False
    area_a, area_b = a['width']*a['height'], b['width']*b['height']
    if min(area_a, area_b) <= 0: return False
    overlap = max(0, min(a['x']+a['width'], b['x']+b['width'])-max(a['x'], b['x'])) * max(0, min(a['y']+a['height'], b['y']+b['height'])-max(a['y'], b['y']))
    return overlap/min(area_a, area_b) >= .8 and min(area_a, area_b)/max(area_a, area_b) >= .5


def earlier_views(target, views):
    """Selection depends on provenance/time, not target wording or final grades."""
    if not target.get('surfaceKey'): return []
    candidates = views.get(tuple(target['surfaceKey'][:4]), [])
    stop = bisect_left([r['capturedAt'] for r in candidates], target['capturedAt'])
    found, seen = [], set()
    for r in reversed(candidates[:stop]):
        if not same_pane(target, r): continue
        # Repeated records from one frame must not consume all reference slots.
        frame = (r.get('screenshotSHA256'), r['content'])
        if frame in seen: continue
        seen.add(frame)
        found.append(r)
        if len(found) == 3: break
    return list(reversed(found))


def prepare(project, baseline, output):
    assert not output.exists(), 'Use a fresh output directory'
    old_docs = {r['documentID']: r for r in read(baseline/'inputs/documents.json')}
    neighborhoods = read(baseline/'inputs/neighborhoods.local.json')
    assert len(neighborhoods) == 39
    assert sum(n['expectedBoundary'] == 'repair_non_novel' for n in neighborhoods) == 36
    source_manifest = read(baseline/'inputs/manifest.json')
    pane_paths = sorted(Path(p) for p in source_manifest['sourceSHA256'] if p.endswith('read-surfaces.jsonl'))
    panes = {}
    for path in pane_paths:
        assert digest(path) == source_manifest['sourceSHA256'][str(path)]
        for r in rows(path):
            p = {k: r.get(k) for k in ['sourceRecordID', 'capturedAt', 'sessionID', 'content', 'screenshotSHA256', 'screenshotRelativePath', 'evidenceID', 'regionOfInterest']}
            p['paneIdentity'] = r.get('surfaceSelection', {}).get('paneIdentity')
            p['paneSource'] = str(path)
            panes[p['sourceRecordID']] = p
    evidence_path = project/'coupled-data/sep02-10-frontier-prep-20260910/fidelity-boundary-evidence.json'
    raw_paths = sorted({project/r['leftRawPath'] for r in read(evidence_path)})
    metadata = {}
    wanted = set(panes)
    for doc in old_docs.values():
        for ref in doc['references']:
            wanted.update(x for x in [ref.get('recordID'), ref.get('sourceRecordID')] if x)
            wanted.update(x['sourceRecordID'] for x in ref.get('screenshots', []) if x.get('sourceRecordID'))
    for path in raw_paths:
        print('Reading provenance:', path.name, path.parent.name, flush=True)
        for r in rows(path):
            if r.get('recordID') not in wanted: continue
            surface = r.get('surface') or r.get('postCaptureSurface') or r
            metadata[r['recordID']] = {
                'sessionID': r['sessionID'], 'capturedAt': r.get('capturedAt'),
                'application': surface.get('appName'), 'windowID': surface.get('windowID'),
                'windowTitle': surface.get('windowTitle'), 'sourcePath': str(path),
                'rawRecordSHA256': hashlib.sha256(canonical(r).encode()).hexdigest(),
                'screenshotRelativePath': r.get('screenshotRelativePath')}
    def identity(source_id):
        p, m = panes.get(source_id), metadata.get(source_id)
        if not p or not m or not p['paneIdentity'] or m['windowID'] is None or not m['windowTitle']:
            return None
        return [m['sessionID'], m['application'], m['windowID'], m['windowTitle'], p['paneIdentity']]
    views = defaultdict(list)
    for rid, p in panes.items():
        key = identity(rid)
        if key and p.get('content'):
            views[tuple(key[:4])].append(dict(p, sourceRecordID=rid, surfaceKey=key,
                                           rawMetadata=metadata[rid]))
    for group in views.values(): group.sort(key=lambda r: (r['capturedAt'], r['sourceRecordID']))
    targets = {}
    def add(doc_id, neighborhood, pair, which):
        if doc_id is None: return None
        old = old_docs[doc_id]
        role = 'intervening_read' if which == 'current' else 'pre_onset_comparison'
        refs = [r for r in old['references'] if r.get('candidateLine') == neighborhood['candidateLine'] and r['role'] == role]
        if which == 'current': refs = [r for r in refs if r.get('recordID') == pair['eventID']]
        if which == 'prior':
            timestamp = pair['baselineAssessment']['boundaryEvidence']['priorCapturedAt']
            refs = [r for r in refs if r['capturedAt'] == timestamp]
        assert len(refs) == 1, (doc_id, role, len(refs))
        ref = refs[0]
        ids = [ref.get('sourceRecordID'), ref.get('recordID')]
        ids += [r.get('sourceRecordID') for r in ref.get('screenshots', [])]
        choices = [i for i in ids if i in panes and panes[i]['capturedAt'] <= ref['capturedAt']]
        choices.sort(key=lambda i: (panes[i]['capturedAt'], i), reverse=True)
        source_id = choices[0] if choices else next((i for i in ids if i in metadata), None)
        # Content-only hashes are insufficient: identical words at different times
        # have different permissible reference observations.
        key = hashlib.sha256(canonical([doc_id, ref['capturedAt'], source_id]).encode()).hexdigest()
        if key in targets: return key
        target = {'documentID': key, 'originDocumentID': doc_id, 'text': old['text'],
                  'capturedAt': ref['capturedAt'], 'sourceRecordID': source_id,
                  'surfaceKey': identity(source_id), 'originalReference': ref,
                  'regionOfInterest': panes.get(source_id,{}).get('regionOfInterest'),
                  'rawMetadata':metadata.get(source_id)}
        selected = earlier_views(target, views)
        target['previousObservations'] = [{'capturedAt': r['capturedAt'], 'ocrText': r['content']} for r in selected]
        target['referenceEvidence'] = [{k:v for k,v in r.items() if k!='content'} for r in selected]
        target['disposition'] = 'sample_with_context' if selected else 'reuse_isolated_no_proven_same_pane_reference'
        targets[key] = target
        return key
    bindings = []
    for n in neighborhoods:
        entry = {k:v for k,v in n.items() if k != 'pairs'}
        entry['pairs'] = [dict(p, contextualCurrentID=add(p['currentDocumentID'], n, p, 'current'),
                               contextualPriorID=add(p['priorDocumentID'], n, p, 'prior')) for p in n['pairs']]
        bindings.append(entry)
    output.mkdir(parents=True)
    docs = sorted(targets.values(), key=lambda x:x['documentID'])
    requests = [d for d in docs if d['previousObservations']]
    for d in docs:
        assert d['text'] == old_docs[d['originDocumentID']]['text']
        for ev, ref in zip(d['referenceEvidence'], d['previousObservations']):
            assert same_pane(d, ev)
            assert ref['capturedAt'] < d['capturedAt']
            assert ref['ocrText'] == panes[ev['sourceRecordID']]['content']
    write(output/'documents.json', requests)
    write(output/'all-observations.json', docs)
    write(output/'neighborhoods.local.json', bindings)
    write(output/'manifest.json', {
        'version':'phase1-contextual-ocr-inputs-v1', 'baselinePath': str(baseline.resolve()),
        'baselineDocumentsSHA256': digest(baseline/'inputs/documents.json'),
        'baselineNeighborhoodsSHA256': digest(baseline/'inputs/neighborhoods.local.json'),
        'baselineResultsSHA256': {str(p.relative_to(baseline)):digest(p) for p in sorted((baseline/'pilot/results').glob('*/result.json'))},
        'sourceHashes': {str(p):digest(p) for p in [*pane_paths, evidence_path]},
        'preparerSHA256':digest(__file__),
        'problemNeighborhoods':36, 'noveltyControlNeighborhoods':3,
        'observationOccurrences':len(docs), 'newRequests':len(requests),
        'contextCounts':dict(Counter(len(d['previousObservations']) for d in docs)),
        'selection':'Up to three latest distinct earlier full-pane OCR frames; exact session/application/window ID/window title; same pane ID or >=80% smaller-pane overlap and >=50% area ratio; no outcome-based retrieval or corrected references',
        'noReferencePolicy':'Reuse the frozen isolated Luna result; no new request without added evidence',
        'caveats':['Pane identity is captured physical identity, not guaranteed application document identity',
                   'References may precede an earlier return to this pane; no arbitrary age cutoff',
                   'Original correction targets include semantic READ and full-window/pane comparison text, unchanged from v1'],
        'privacy':'No screenshots, raw AX fields, WRITE targets or local boundary labels sent; OCR reference text only',
        'documentsSHA256':digest(output/'documents.json'),
        'allObservationsSHA256':digest(output/'all-observations.json'),
        'bindingsSHA256':digest(output/'neighborhoods.local.json')})
    print(canonical({'observations':len(docs), 'requests':len(requests), 'contextCounts':dict(Counter(len(d['previousObservations']) for d in docs))}))


if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('--baseline',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();prepare(Path.cwd(),a.baseline.resolve(),a.output.resolve())

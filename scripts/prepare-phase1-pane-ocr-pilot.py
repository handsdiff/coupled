#!/usr/bin/env python3
"""Prepare a bounded Astra-low OCR test AFTER pane repair; no network access.

Models see current pane OCR and at most three earlier same-surface views.
Screenshots and WRITE values stay local for independent accuracy review.
"""
import argparse
from bisect import bisect_left
from collections import defaultdict
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT/'scripts'/name)
    result = importlib.util.module_from_spec(spec); spec.loader.exec_module(result); return result
context = module('prepare-phase1-contextual-ocr.py')
runner = module('run-phase1-ocr-correction.py')
rows, sha, save = context.rows, context.digest, context.write
VERSION = 'pane-first-astra-ocr-pilot-v1'
# These are reviewed failure categories, not production selection rules.
BOUNDARIES = [37, 288, 445, 498, 660, 685, 868, 929, 931, 1052, 1068, 645, 832, 1079, 836]


def prepare(surfaces, output, prior=None):
    assert not output.exists(), 'Use a fresh output'
    stage_path = ROOT/'coupled-data/sep02-10-fidelity-causes-20260911/stage-evidence.json'
    stage = json.loads(stage_path.read_text())
    selected = []
    for b in stage['boundaries']:
        if b['candidateLine'] in BOUNDARIES:
            for read in b['reads'][:1]:
                # One source frame, not a temporal union masquerading as a frame.
                choices = [rid for rid in read['paneRecordIDs'] if rid in stage['panes']]
                assert choices
                rid = max(choices, key=lambda rid: stage['panes'][rid]['capturedAt'])
                selected.append({'sourceRecordID': rid, 'candidateLine': b['candidateLine'],
                    'cases': [b['leftCase'], b['rightCase']],
                    'eventID': read['event']['sourceEventID']})
    assert len(selected) == len(BOUNDARIES)
    del stage
    panes, metadata, source_hashes = {}, {}, {str(stage_path): sha(stage_path)}
    for day in (2, 3, 4, 7):
        directory = surfaces/f'sep{day}-surfaces'
        manifest = json.loads((directory/'read-surface-evidence.json').read_text())
        assert manifest['paneRepair']['version'] in ('ax-pane-edge-selection-v5', 'ax-pane-edge-selection-v6')
        file = directory/'read-surfaces.jsonl'
        assert sha(file) == manifest['artifacts']['digestsSHA256'][file.name]
        source_hashes[str(file)] = sha(file)
        source_hashes[str(directory/'read-surface-evidence.json')] = sha(directory/'read-surface-evidence.json')
        ids = set()
        for p in rows(file):
            rid = p['sourceRecordID']; ids.add(rid)
            panes[rid] = {k: p.get(k) for k in ['sourceRecordID', 'capturedAt', 'sessionID',
                'content', 'screenshotSHA256', 'screenshotRelativePath', 'evidenceID', 'regionOfInterest']}
            panes[rid]['paneIdentity'] = p['surfaceSelection'].get('paneIdentity')
        raw = ROOT/f'coupled-data/phase1-ordinary-work-2026-09-{day:02d}-1/raw.jsonl'
        assert sha(raw) == manifest['source']['digestsSHA256']['raw.jsonl']
        source_hashes[str(raw)] = sha(raw)
        for r in rows(raw):
            if r.get('recordID') not in ids: continue
            surface = r.get('surface') or r.get('postCaptureSurface') or r
            metadata[r['recordID']] = {k: surface.get(k) for k in ['appName', 'windowID', 'windowTitle']}
            metadata[r['recordID']]['screenshot'] = str(raw.parent/r['screenshotRelativePath'])
    views = defaultdict(list)
    for rid, p in panes.items():
        m = metadata[rid]
        p['surfaceKey'] = [p['sessionID'], m['appName'], m['windowID'], m['windowTitle'], p['paneIdentity']]
        if all(x is not None for x in p['surfaceKey']):
            views[tuple(p['surfaceKey'][:4])].append(p)
    for group in views.values(): group.sort(key=lambda p: (p['capturedAt'], p['sourceRecordID']))
    documents, audit = [], []
    for selection in sorted(selected, key=lambda r: BOUNDARIES.index(r['candidateLine'])):
        p = panes[selection['sourceRecordID']]
        previous = context.earlier_views(p, views)
        assert all(r['capturedAt'] < p['capturedAt'] for r in previous)
        documents.append({'documentID': p['sourceRecordID'], 'text': p['content'],
            'capturedAt': p['capturedAt'],
            'previousObservations': [{'capturedAt': r['capturedAt'], 'ocrText': r['content']} for r in previous]})
        screenshot = Path(metadata[p['sourceRecordID']]['screenshot'])
        assert sha(screenshot) == p['screenshotSHA256']
        audit.append(dict(selection, evidenceID=p['evidenceID'], screenshot=str(screenshot),
            screenshotSHA256=p['screenshotSHA256'], regionOfInterest=p['regionOfInterest'],
            previousSourceRecordIDs=[r['sourceRecordID'] for r in previous]))
    output.mkdir(parents=True)
    reuse = []
    if prior:
        previous = {d['documentID']: d for d in json.loads((prior/'documents.json').read_text())}
        unchanged = [d['documentID'] for d in documents if previous.get(d['documentID']) == d]
        reuse = [{'documentID': rid, 'priorDirectory': str(prior.resolve()),
                 'priorDocumentsSHA256': sha(prior/'documents.json')} for rid in unchanged]
        save(output/'all-documents.json', documents)
        documents = [d for d in documents if d['documentID'] not in unchanged]
        audit = [a for a in audit if a['sourceRecordID'] not in unchanged]
        assert documents, 'No changed inputs; existing audited results suffice'
    save(output/'unchanged-prior-inputs.json', reuse)
    save(output/'documents.json', documents)
    save(output/'local-review-evidence.json', audit)
    save(output/'preparation.json', {'version': VERSION, 'sourceSHA256': source_hashes,
        'documents': len(documents), 'screenshotTransmission': False, 'writeTargetTransmission': False,
        'policy': 'No future views or human target. Full corrected pane OCR, unchanged exact-edit prompt, earlier same-surface references only.',
        'scriptSHA256': sha(Path(__file__)), 'providerCallsDuringPreparation': 0})
    runner.prepare(output/'pilot', ROOT, documents,
        'Astra-low recognition-only shadow test after same-frame pane repair; screenshot accuracy judged separately',
        edits=True, edit_model=runner.ASTRA)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--surfaces', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--prior', type=Path, help='Test only changed model inputs; preserve references to identical earlier requests')
    args = p.parse_args(); prepare(args.surfaces.resolve(), args.output.resolve(), args.prior)

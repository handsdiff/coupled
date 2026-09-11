#!/usr/bin/env python3
"""Opt-in, streaming offline pane-edge and OCR-order refinement.

Consumes existing versioned pane evidence. Does not change the collector,
baseline selector, canonical corpus, or raw observations. Default flags do
nothing. Rejected expansions retain the original pane byte-for-byte.
"""
import argparse
from copy import deepcopy
import hashlib
import json
import platform
from pathlib import Path
import resource
import subprocess
import sys
import tempfile

from phase1_ocr_geometry import ORDER_VERSION, row_order
from phase1_pane_edge import PANE_VERSION, COMPLETE_PANE_VERSION, FUSED_PANE_VERSION, ancestors, screening_witnesses, extend_edges, fuse_exposed_lines, global_box
from phase1_read_surface_v6 import pane_identity

ROOT = Path(__file__).resolve().parents[1]
VERSION = 'read-surface-refinement-v2'


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def text_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def ocr_key(image_hash, region, source_hash, architecture):
    # Foundation JSON round-trips the final binary float differently from
    # Python on some OS versions. This is far below a pixel, not a new crop.
    stable_region = {k: round(float(v), 12) for k, v in region.items()}
    return text_hash(canonical([image_hash, stable_region, source_hash, architecture]))


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


def rows(path):
    with path.open() as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def save(path, value):
    with path.open('x') as f:
        f.write(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)+'\n')


def peak_mib():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024**2 if sys.platform == 'darwin' else 1024)


def refine(args):
    pane_version = FUSED_PANE_VERSION if args.preserve_observed_text else COMPLETE_PANE_VERSION if args.complete_pane else PANE_VERSION
    if args.preserve_observed_text and not args.complete_pane:
        raise ValueError('Preserving observed text requires complete-pane edge recovery')
    if args.complete_pane and (not args.pane_edges or args.line_order):
        raise ValueError('Complete-pane repair requires pane edges and native OCR order')
    if args.output.exists():
        raise ValueError('Use a fresh output; existing artifacts are immutable')
    code_paths = [Path(__file__), ROOT/'scripts/phase1_pane_edge.py', ROOT/'scripts/phase1_ocr_geometry.py',
                  *[ROOT/'scripts'/f'phase1_read_surface_v{v}.py' for v in [2, 6]],
                  ROOT/'scripts/ocr-phase1-surface-regions.m']
    code_hashes = {p.name: sha(p) for p in code_paths}
    wanted = None
    if args.audit_only:
        with args.audit_only.open() as f:
            wanted = set(json.load(f)['panes'])
    ids = {r['sourceRecordID'] for r in rows(args.baseline) if wanted is None or r['sourceRecordID'] in wanted}
    # Only retain compact READ metadata, never input/checkpoint journals.
    metadata = {}
    initial_stat = args.raw.stat()
    fields = ['recordID', 'recordType', 'sessionID', 'appName', 'capturedAt', 'content', 'windowBounds',
              'accessibilitySurface', 'screenshotRelativePath', 'screenshotSHA256', 'windowID']
    for r in rows(args.raw):
        if r.get('recordID') in ids:
            metadata[r['recordID']] = {k: r.get(k) for k in fields}
        if peak_mib() > 1024:
            raise MemoryError('Refiner exceeds 1 GiB RSS')
    assert ids <= metadata.keys(), 'Baseline raw sources missing'
    args.output.mkdir(parents=True)
    ocr_source = ROOT/'scripts/ocr-phase1-surface-regions.m'
    ocr_hash = sha(ocr_source)
    silicon = subprocess.run(['sysctl', '-n', 'hw.optional.arm64'], capture_output=True, text=True)
    if silicon.returncode:
        raise RuntimeError('Cannot verify native OCR architecture; no silent Rosetta fallback')
    architecture = 'arm64' if silicon.stdout.strip() == '1' else 'x86_64'
    reusable = {}
    cache_hashes = {str(path.resolve()): sha(path) for path in args.reuse_ocr}
    for path in args.reuse_ocr:
        for r in rows(path):
            if r.get('ocrSourceSHA256') == ocr_hash and r.get('ocrArchitecture') == architecture:
                assert r['cacheKey'] == r['jobID']
                reusable[ocr_key(r['screenshotSHA256'], r['regionOfInterest'], ocr_hash, architecture)] = r
    counts = {'inputPanes': len(ids), 'paneScreened': 0, 'paneExpanded': 0, 'ocrCalls': 0,
              'orderingChanged': 0, 'preservedExactly': 0, 'changed': 0}
    with tempfile.TemporaryDirectory(prefix='coupled-pane-refinement-') as td, \
            (args.output/'read-surfaces.jsonl').open('x') as out, \
            (args.output/'decisions.jsonl').open('x') as decisions, \
            (args.output/'ocr-results.jsonl').open('x') as ocr_out:
        executable = Path(td)/'ocr'
        def recognize(raw, region):
            screenshot = (args.raw.parent/raw['screenshotRelativePath']).resolve()
            screenshot.relative_to(args.raw.parent.resolve())
            key = ocr_key(raw['screenshotSHA256'], region, ocr_hash, architecture)
            assert sha(screenshot) == raw['screenshotSHA256']
            if key in reusable:
                cached = reusable[key]
                assert cached['screenshotSHA256'] == raw['screenshotSHA256']
                assert all(abs(cached['regionOfInterest'][k]-v) < 1e-11 for k,v in region.items())
                return dict(cached, regionOfInterest=region)
            if counts['ocrCalls'] >= args.max_ocr_calls:
                raise RuntimeError('Explicit local OCR work limit reached; retained partial diagnostics, no promotion')
            if not executable.exists():
                subprocess.run(['clang', '-arch', architecture, '-fobjc-arc', '-fblocks', '-framework', 'Foundation', '-framework',
                    'Vision', '-framework', 'ImageIO', '-framework', 'CoreGraphics', str(ocr_source), '-o', str(executable)],
                    check=True, timeout=90)
            job = {'jobID': key, 'imagePath': str(screenshot), 'regionOfInterest': region}
            result = subprocess.run([str(executable)], input=canonical(job)+'\n', text=True,
                                    capture_output=True, check=True, timeout=90)
            value = json.loads(result.stdout)
            if value.get('error'):
                raise ValueError(value['error'])
            counts['ocrCalls'] += 1
            value.update(cacheKey=key, screenshotSHA256=raw['screenshotSHA256'], ocrSourceSHA256=ocr_hash,
                         ocrArchitecture=architecture)
            value['regionOfInterest'] = region
            ocr_out.write(canonical(value)+'\n'); ocr_out.flush()
            reusable[key] = value
            return value
        for ordinal, baseline in enumerate(rows(args.baseline)):
            if baseline['sourceRecordID'] not in ids:
                continue
            raw = metadata[baseline['sourceRecordID']]
            assert raw['capturedAt'] == baseline['capturedAt'] and raw['screenshotSHA256'] == baseline['screenshotSHA256']
            row = deepcopy(baseline)
            decision = {'sourceRecordID': row['sourceRecordID'], 'baselineEvidenceID': row['evidenceID'],
                        'pane': {'accepted': False, 'reason': 'disabled' if not args.pane_edges else 'no_cut_edge_evidence'}}
            if args.pane_edges:
                witnesses = screening_witnesses(baseline, raw)
                candidates = ancestors(baseline, raw, clipped_edge_only=args.complete_pane) if len(witnesses) >= 2 else []
                if candidates:
                    counts['paneScreened'] += 1
                    decision['pane'] = {'accepted': False, 'screeningLineIndices': witnesses, 'attempts': []}
                for candidate in candidates:
                    proposed = recognize(raw, candidate['regionOfInterest'])
                    full_lines, proof = extend_edges(baseline, proposed)
                    decision['pane']['attempts'].append(dict(proof, regionOfInterest=candidate['regionOfInterest'],
                                                             ancestorRegionOfInterest=candidate['ancestorRegionOfInterest'],
                                                             selectedDepth=candidate['node']['depth']))
                    if not proof['accepted']:
                        continue
                    comparison_region = dict(baseline['comparisonRegionOfInterest'],
                        x=proposed['regionOfInterest']['x'], width=proposed['regionOfInterest']['width'])
                    # Reuse the same-frame wider observation. All original
                    # comparison lines survive; no new crop/recognition policy.
                    comparison_old = {'lines': baseline['comparisonLines'], 'regionOfInterest': baseline['comparisonRegionOfInterest']}
                    comparison_lines, comparison_proof = extend_edges(comparison_old, proposed)
                    if comparison_proof['accepted']:
                        # extend_edges returns coordinates relative to the full
                        # wider ROI. Re-express them in the comparison ROI.
                        for item in comparison_lines:
                            box = global_box(item, proposed['regionOfInterest'])
                            item['boundingBox'] = {'x': (box['x']-comparison_region['x'])/comparison_region['width'],
                                'y': (box['y']-comparison_region['y'])/comparison_region['height'],
                                'width': box['width']/comparison_region['width'], 'height': box['height']/comparison_region['height']}
                    else:
                        comparison_region = baseline['comparisonRegionOfInterest']
                    if args.preserve_observed_text:
                        full_lines, fusion_proof = fuse_exposed_lines(baseline, proposed)
                        assert fusion_proof['accepted']
                        comparison_lines, comparison_proof = fuse_exposed_lines(
                            comparison_old, proposed, full_pane_proof=fusion_proof)
                        comparison_region = dict(baseline['comparisonRegionOfInterest'],
                            x=proposed['regionOfInterest']['x'], width=proposed['regionOfInterest']['width'])
                        for item in comparison_lines:
                            box = global_box(item, proposed['regionOfInterest'])
                            item['boundingBox'] = {'x': (box['x']-comparison_region['x'])/comparison_region['width'],
                                'y': (box['y']-comparison_region['y'])/comparison_region['height'],
                                'width': box['width']/comparison_region['width'], 'height': box['height']/comparison_region['height']}
                    elif args.complete_pane:
                        # The prefix witnesses choose the region; they do not
                        # construct its text. Use actual OCR of the whole new
                        # region, including lines wholly outside the old crop.
                        full_lines = proposed['lines']
                        comparison_region = dict(baseline['comparisonRegionOfInterest'],
                            x=proposed['regionOfInterest']['x'], width=proposed['regionOfInterest']['width'])
                        comparison_observation = recognize(raw, comparison_region)
                        from phase1_pane_edge import complete_observations_usable
                        if not complete_observations_usable(proposed, comparison_observation):
                            decision['pane']['attempts'][-1].update(
                                accepted=False, reason='empty_repaired_ocr_projection',
                                observationJobID=proposed['jobID'],
                                comparisonJobID=comparison_observation['jobID'])
                            continue  # Keep the original pane if no candidate is usable.
                        comparison_lines = comparison_observation['lines']
                        comparison_proof = {'accepted': True, 'version': pane_version,
                            'reason': 'native_ocr_of_repaired_comparison_region',
                            'observationJobID': comparison_observation['jobID']}
                    row.update(regionOfInterest=proposed['regionOfInterest'], content='\n'.join(l['text'] for l in full_lines),
                               lines=full_lines, recognizedLineCount=len(full_lines),
                               comparisonRegionOfInterest=comparison_region,
                               comparisonContent='\n'.join(l['text'] for l in comparison_lines), comparisonLines=comparison_lines,
                               comparisonRecognizedLineCount=len(comparison_lines))
                    selection = row['surfaceSelection']
                    row['baseSurfaceRuleVersion'] = baseline['ruleVersion']
                    row['ruleVersion'] = pane_version
                    selection.update(method='same_frame_verified_wider_ancestor', reason=proof['reason'],
                        selectedDepth=candidate['node']['depth'], selectedRole=candidate['node']['role'],
                        selectedSubrole=candidate['node'].get('subrole'),
                        regionOfInterest=proposed['regionOfInterest'], comparisonRegionOfInterest=comparison_region)
                    selection['ruleVersion'] = pane_version
                    selection['resolution'] = 'same_frame_verified_wider_ancestor'
                    for key, source_key in [('selectedLabel', 'label'), ('selectedTitle', 'title'),
                                            ('selectedDescription', 'description'), ('selectedIdentifier', 'identifier')]:
                        selection[key] = candidate['node'].get(source_key)
                    region = proposed['regionOfInterest']
                    selection['normalizedTopRectangle'] = dict(region, y=1-region['y']-region['height'])
                    # Historical anchors/canonicalizations describe the input
                    # selection, not the newly verified output rectangle.
                    for key in ['canonicalization', 'screenRectangle', 'comparisonScreenRectangle',
                                'comparisonNormalizedTopRectangle', 'attemptedSelection']:
                        selection.pop(key, None)
                    selection['edgeValidation'] = proof
                    selection['ancestorRegionOfInterest'] = candidate['ancestorRegionOfInterest']
                    selection['comparisonEdgeValidation'] = comparison_proof
                    if args.preserve_observed_text:
                        selection['paneObservationFusion'] = {'version': pane_version,
                            'observationJobID': proposed['jobID'], 'full': fusion_proof,
                            'comparison': comparison_proof, 'unchangedEdges': ['top', 'right', 'bottom'],
                            'textConstruction': 'original_ocr_plus_same_frame_proven_prefixes_and_wholly_exposed_lines'}
                    elif args.complete_pane:
                        selection['completePaneObservation'] = {'version': pane_version,
                            'observationJobID': proposed['jobID'],
                            'comparisonJobID': comparison_observation['jobID'],
                            'unchangedEdges': ['top', 'right', 'bottom'],
                            'textConstruction': 'native_ocr_of_corrected_region_not_prefix_stitching'}
                    selection['paneIdentity'] = pane_identity(selection)
                    row['jobID'] = proposed['jobID']
                    row['jobIDs'] = [*baseline.get('jobIDs', [baseline['jobID']]), proposed['jobID']]
                    if args.complete_pane and not args.preserve_observed_text:
                        row['jobIDs'].append(comparison_observation['jobID'])
                    decision['pane'].update(accepted=True, reason=proof['reason'])
                    counts['paneExpanded'] += 1
                    break
            if args.line_order:
                decision['ordering'] = {}
                for line_key, content_key in [('lines', 'content'), ('comparisonLines', 'comparisonContent')]:
                    if line_key not in row:
                        continue
                    ordered, audit = row_order(row[line_key])
                    decision['ordering'][line_key] = audit
                    if audit['changed']:
                        row[line_key] = ordered
                        row[content_key] = '\n'.join(l['text'] for l in ordered)
                if any(v['changed'] for v in decision['ordering'].values()):
                    counts['orderingChanged'] += 1
                    row['ocrOrderingVersion'] = ORDER_VERSION
            changed = row != baseline
            if changed:
                counts['changed'] += 1
                row['contentSHA256'] = text_hash(row['content'])
                if 'comparisonContent' in row:
                    row['comparisonContentSHA256'] = text_hash(row['comparisonContent'])
                row['refinement'] = {'version': VERSION, 'baselineEvidenceID': baseline['evidenceID'],
                    'baselineContentSHA256': baseline['contentSHA256'],
                    'paneVersion': pane_version if decision['pane']['accepted'] else None,
                    'orderingVersion': row.get('ocrOrderingVersion')}
                row['evidenceID'] = 'evidence_' + text_hash(canonical(row))
            else:
                counts['preservedExactly'] += 1
                assert row == baseline
            out.write(canonical(row)+'\n')
            decisions.write(canonical(dict(decision, changed=changed))+'\n')
            if ordinal % 100 == 0:
                out.flush(); decisions.flush()
                print(json.dumps(counts), flush=True)
            if peak_mib() > 1024:
                raise MemoryError('Refiner exceeds 1 GiB RSS')
    assert args.raw.stat().st_size == initial_stat.st_size and args.raw.stat().st_mtime_ns == initial_stat.st_mtime_ns
    assert code_hashes == {p.name: sha(p) for p in code_paths}, 'Code changed during replay'
    assert cache_hashes == {str(path.resolve()): sha(path) for path in args.reuse_ocr}, 'OCR cache changed during replay'
    save(args.output/'read-surface-evidence.json', {
        'schemaVersion': 1, 'ruleVersion': VERSION, 'authority': 'offline_candidate_not_canonical',
        'stages': {'paneEdges': pane_version if args.pane_edges else None, 'lineOrder': ORDER_VERSION if args.line_order else None},
        'counts': counts, 'peakRSSMiB': peak_mib(),
        'platform': platform.platform(),
        'ocrArchitecture': architecture,
        'source': {'baseline': str(args.baseline.resolve()), 'rawPath': str(args.raw.resolve()),
                   'baselineSHA256': sha(args.baseline), 'rawSHA256': sha(args.raw)},
        'codeHashes': code_hashes,
        'reusedOCRCacheSHA256': cache_hashes,
        'artifacts': {'digestsSHA256': {p.name: sha(p) for p in sorted(args.output.iterdir()) if p.is_file()}},
        'providerCalls': 0, 'collectorChanged': False})
    print(json.dumps(counts), flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--raw', required=True, type=Path)
    p.add_argument('--baseline', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--pane-edges', action='store_true')
    p.add_argument('--complete-pane', action='store_true', help='Repair only the cut left edge; OCR the corrected region rather than stitch prefixes')
    p.add_argument('--preserve-observed-text', action='store_true', help='Preserve original OCR and add only proven prefixes and newly exposed whole lines')
    p.add_argument('--line-order', action='store_true')
    p.add_argument('--audit-only', type=Path, help='Optional stage-evidence.json restricting to reviewed raw source IDs')
    p.add_argument('--reuse-ocr', type=Path, action='append', default=[])
    p.add_argument('--max-ocr-calls', type=int, default=500)
    refine(p.parse_args())

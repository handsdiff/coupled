#!/usr/bin/env python3
"""Bind same-frame pane repairs into reducer evidence; no extra OCR sorting.

The original selector remains recorded. V4 preserves suffixes; V5 chooses the
corrected region and uses its actual native OCR, including fully omitted lines.
No collector, raw data or target changes.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil

from phase1_pane_edge import PANE_VERSION, COMPLETE_PANE_VERSION, FUSED_PANE_VERSION, extend_edges, complete_observations_usable, fuse_exposed_lines

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('refinement', ROOT/'scripts/refine-phase1-read-surfaces.py')
refinement = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refinement)
sha, rows, save, canonical = refinement.sha, refinement.rows, refinement.save, refinement.canonical
VERSION = 'pane-edge-materialization-v1'


def materialize(baseline, refined, output):
    if output.exists():
        raise ValueError('Use a fresh immutable output directory')
    manifest = json.loads((baseline/'read-surface-evidence.json').read_text())
    repair = json.loads((refined/'read-surface-evidence.json').read_text())
    pane_version = repair['stages']['paneEdges']
    assert pane_version in (PANE_VERSION, COMPLETE_PANE_VERSION, FUSED_PANE_VERSION) and repair['stages']['lineOrder'] is None
    assert repair['source']['baselineSHA256'] == sha(baseline/'read-surfaces.jsonl')
    for name, digest in manifest['artifacts']['digestsSHA256'].items():
        assert sha(baseline/name) == digest, name
    for name, digest in repair['artifacts']['digestsSHA256'].items():
        assert sha(refined/name) == digest, name
    decisions = {r['sourceRecordID']: r for r in rows(refined/'decisions.jsonl')}
    # Recheck the actual image-grounded patches, not merely the accepted flag.
    cache = {}
    for source in [refined/'ocr-results.jsonl', *map(Path, repair.get('reusedOCRCacheSHA256', {}))]:
        for record in rows(source):
            cache[record['jobID']] = record
    output.mkdir(parents=True)
    for name in ('jobs.jsonl', 'unresolved.jsonl'):
        shutil.copyfile(baseline/name, output/name)
    job_ids = {r['jobID'] for r in rows(baseline/'jobs.jsonl')}
    shutil.copyfile(refined/'decisions.jsonl', output/'pane-decisions.jsonl')
    changed = total = 0
    with (output/'read-surfaces.jsonl').open('x') as handle, (output/'jobs.jsonl').open('a') as jobs:
        original = iter(rows(baseline/'read-surfaces.jsonl'))
        for after in rows(refined/'read-surfaces.jsonl'):
            before = next(original)
            total += 1
            assert before['sourceRecordID'] == after['sourceRecordID']
            decision = decisions[before['sourceRecordID']]
            for key in ('sessionID', 'capturedAt', 'sourceRecordID', 'screenshotSHA256'):
                assert before[key] == after[key], key
            if decision['changed']:
                assert decision['pane']['accepted'] and 'ordering' not in decision
                observation = cache[after['jobID']]
                assert observation['screenshotSHA256'] == before['screenshotSHA256']
                if pane_version == COMPLETE_PANE_VERSION:
                    comparison = cache[after['surfaceSelection']['completePaneObservation']['comparisonJobID']]
                    assert complete_observations_usable(observation, comparison), 'Empty OCR is not a usable repair'
                if pane_version == FUSED_PANE_VERSION:
                    _, full_proof = fuse_exposed_lines(before, observation)
                    assert full_proof['accepted']
                for lk, ck, rk in [('lines', 'content', 'regionOfInterest'),
                                   ('comparisonLines', 'comparisonContent', 'comparisonRegionOfInterest')]:
                    if pane_version == FUSED_PANE_VERSION:
                        expected, proof = fuse_exposed_lines({'lines': before[lk], 'regionOfInterest': before[rk]},
                            observation, full_pane_proof=full_proof if lk != 'lines' else None)
                        assert proof['accepted']
                        assert abs((before[rk]['x']+before[rk]['width'])-(after[rk]['x']+after[rk]['width'])) < 1e-10
                    elif pane_version == COMPLETE_PANE_VERSION:
                        key = 'observationJobID' if lk == 'lines' else 'comparisonJobID'
                        observed = cache[after['surfaceSelection']['completePaneObservation'][key]]
                        assert observed['regionOfInterest'] == after[rk]
                        assert observed['screenshotSHA256'] == before['screenshotSHA256']
                        assert abs((before[rk]['x']+before[rk]['width'])-(after[rk]['x']+after[rk]['width'])) < 1e-10
                        expected = observed['lines']
                        if lk == 'lines':
                            assert extend_edges({'lines': before[lk], 'regionOfInterest': before[rk]}, observed)[1]['accepted']
                    else:
                        expected, proof = extend_edges({'lines': before[lk], 'regionOfInterest': before[rk]}, observation)
                    assert [x['text'] for x in expected] == [x['text'] for x in after[lk]]
                    assert after[ck] == '\n'.join(x['text'] for x in expected)
                    assert before[rk]['y'] == after[rk]['y'] and before[rk]['height'] == after[rk]['height']
                changed += 1
                # Keep the selector's schema understood by the reducer. The
                # repair version and exact before/after remain explicit below.
                after['ruleVersion'] = before['ruleVersion']
                after['surfaceSelection']['ruleVersion'] = before['surfaceSelection']['ruleVersion']
                after['paneRepair'] = {'version': pane_version,
                    'materializerVersion': VERSION,
                    'baselineEvidenceID': before['evidenceID'],
                    'baselineSurfaceSelection': before['surfaceSelection'],
                    'refinementManifestSHA256': sha(refined/'read-surface-evidence.json')}
                if pane_version == COMPLETE_PANE_VERSION:
                    after['paneRepair']['baselineOrderingEvidence'] = after.pop('orderingEvidence', None)
                    after['ocrOrderingVersion'] = observation['orderingVersion']
                    after['orderingEvidence'] = {'version': observation['orderingVersion'],
                        'source': 'completePaneObservation',
                        'ocrSourceSHA256': observation['ocrSourceSHA256'],
                        'ocrArchitecture': observation['ocrArchitecture']}
                elif pane_version == FUSED_PANE_VERSION:
                    after['paneRepair']['baselineOrderingEvidence'] = after.pop('orderingEvidence', None)
                    after['ocrOrderingVersion'] = 'preserved_order_with_exposed_line_insertion_v1'
                    after['orderingEvidence'] = {'version': after['ocrOrderingVersion'],
                        'source': 'paneObservationFusion', 'baselineEvidenceID': before['evidenceID'],
                        'ocrSourceSHA256': observation['ocrSourceSHA256'],
                        'ocrArchitecture': observation['ocrArchitecture']}
                after['evidenceID'] = 'evidence_'+refinement.text_hash(canonical(after))
                added_jobs = [after['jobID']]
                if pane_version == COMPLETE_PANE_VERSION:
                    added_jobs.append(after['surfaceSelection']['completePaneObservation']['comparisonJobID'])
                for job_id in added_jobs:
                    if job_id in job_ids:
                        continue
                    job_observation = cache[job_id]
                    jobs.write(canonical({'jobID': job_id,
                        'sourceRecordID': after['sourceRecordID'], 'sourceSessionID': after['sessionID'],
                        'sourceRawLine': after['sourceRawLine'],
                        'imagePath': str(Path(manifest['source']['directory'])/after['screenshotRelativePath']),
                        'screenshotRelativePath': after['screenshotRelativePath'],
                        'screenshotSHA256': after['screenshotSHA256'],
                        'regionOfInterest': job_observation['regionOfInterest'],
                        'ocrArchitecture': job_observation['ocrArchitecture'],
                        'projection': 'same_frame_repaired_pane_ocr'})+'\n')
                    job_ids.add(job_id)
            else:
                assert before == after
            handle.write(canonical(after)+'\n')
        assert next(original, None) is None, 'Partial pane stream is not promotable'
    # The local OCR observations are replay evidence, not a new source journal.
    # Keep their hashes/paths bound rather than copying screenshots or raw data.
    manifest['paneRepair'] = {'version': pane_version, 'materializerVersion': VERSION,
        'codeSHA256': {p.name: sha(p) for p in (Path(__file__), ROOT/'scripts/phase1_pane_edge.py')},
        'baselineDirectory': str(baseline.resolve()),
        'baselineManifestSHA256': sha(baseline/'read-surface-evidence.json'),
        'refinementDirectory': str(refined.resolve()),
        'refinementManifestSHA256': sha(refined/'read-surface-evidence.json'),
        'policy': ('same-screenshot AX-ancestor left-edge repair; preserve original OCR suffixes and line order; add only verified prefixes and wholly newly exposed lines; no wholesale OCR replacement'
                   if pane_version == FUSED_PANE_VERSION else
                   'same-screenshot AX-ancestor left-edge repair; unchanged top/right/bottom; native OCR of corrected full and comparison regions; no text stitching'
                   if pane_version == COMPLETE_PANE_VERSION else
                   'same-screenshot AX-ancestor prefix recovery; retain all original suffixes, non-edge text, line order and capture times'),
        'totalPanes': total, 'repairedPanes': changed, 'exactlyUnchangedPanes': total-changed}
    manifest['counts']['jobs'] = len(job_ids)
    manifest['artifacts']['digestsSHA256'] = {name: sha(output/name) for name in
        ('jobs.jsonl', 'unresolved.jsonl', 'read-surfaces.jsonl', 'pane-decisions.jsonl')}
    save(output/'read-surface-evidence.json', manifest)
    print(json.dumps(manifest['paneRepair']), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline', 'refined', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    materialize(args.baseline, args.refined, args.output)

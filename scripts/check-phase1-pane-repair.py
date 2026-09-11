#!/usr/bin/env python3
"""Read-only regression audit of a complete pane-selection replay."""
import argparse
from collections import Counter
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('materializer', ROOT/'scripts/materialize-phase1-pane-repair.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
rows, sha = m.rows, m.sha

# Real, locally inspected failures. These IDs select TEST assertions only.
KNOWN = {
    'EB9C3115-7ED7-4FF5-949C-0BFED22B3794': ['For explicit scrolling', 'Dynamic content'],
    'B873BD7C-8177-465E-8AE0-5447572ED672': ['Images without OCR', '\nAudio\n'],
    '3511E017-6E89-40AA-AF0B-F910B4B4A499': ['mention these notes', 'emphasis on data collection', '\nbest\n'],
    '7695B4B6-64E9-4377-B302-D54D127794C7': ['i think either going through', '\nbest\n'],
    '1C1A7ADC-2DF6-4AF6-9565-D03137AFCBC3': ['existing dynamic consolidation is too narrow. It primarily recognizes records originating from the visual-change monitor.'],
    '6479F61C-1453-4FDA-AFCF-A2E1B3337DBA': ["threshold never gets evaluated. (It's 9,999, 959, 219, 027 raw"],
}


def audit(root):
    counts = Counter(); examples = []; found = set()
    for day in (2, 3, 4, 7):
        output = root/f'sep{day}-surfaces'
        manifest = json.loads((output/'read-surface-evidence.json').read_text())
        repair = manifest['paneRepair']
        assert repair['version'] in ('ax-pane-edge-selection-v5', 'ax-pane-edge-selection-v6')
        baseline = Path(repair['baselineDirectory'])
        assert sha(baseline/'read-surface-evidence.json') == repair['baselineManifestSHA256']
        for name, digest in manifest['artifacts']['digestsSHA256'].items():
            assert sha(output/name) == digest
        jobs = {r['jobID'] for r in rows(output/'jobs.jsonl')}
        original = iter(rows(baseline/'read-surfaces.jsonl'))
        for after in rows(output/'read-surfaces.jsonl'):
            before = next(original)
            counts['panes'] += 1
            for key in ('sourceRecordID', 'sessionID', 'sourceRawLine', 'capturedAt', 'screenshotSHA256'):
                assert before[key] == after[key]
            if after == before:
                counts['exactlyUnchanged'] += 1
                continue
            counts['repaired'] += 1
            assert after['paneRepair']['baselineEvidenceID'] == before['evidenceID']
            assert after['content'].strip() and after['comparisonContent'].strip()
            assert set(after['jobIDs']) <= jobs
            for lk, ck, rk in [('lines', 'content', 'regionOfInterest'),
                               ('comparisonLines', 'comparisonContent', 'comparisonRegionOfInterest')]:
                a, b = before[rk], after[rk]
                assert a['y'] == b['y'] and a['height'] == b['height']
                assert b['x'] < a['x'] and abs(a['x']+a['width']-b['x']-b['width']) < 1e-9
                assert after[ck] == '\n'.join(l['text'] for l in after[lk])
                assert after[ck+'SHA256'] == m.refinement.text_hash(after[ck])
                if repair['version'] == 'ax-pane-edge-selection-v6':
                    fusion = after['surfaceSelection']['paneObservationFusion']['full' if lk == 'lines' else 'comparison']
                    replacements = {w['oldIndex']: w['content'] for w in fusion['prefixProof'].get('witnesses', [])} if fusion['prefixProof']['accepted'] else {}
                    expected = [replacements.get(i, l['text']) for i, l in enumerate(before[lk])]
                    remaining = iter(l['text'] for l in after[lk])
                    assert all(any(text == actual for actual in remaining) for text in expected), 'Original text/order lost'
                    assert len(after[lk]) == len(before[lk])+len(fusion['addedLines'])
            rid = after['sourceRecordID']
            if rid in KNOWN:
                found.add(rid)
                for text in KNOWN[rid]: assert text in after['content'], (rid, text)
                examples.append({'sourceRecordID': rid, 'before': before['content'], 'after': after['content'],
                    'beforeRegion': before['regionOfInterest'], 'afterRegion': after['regionOfInterest'],
                    'screenshot': str(Path(manifest['source']['directory'])/after['screenshotRelativePath'])})
        assert next(original, None) is None
    assert found == set(KNOWN)
    result = {'counts': dict(counts), 'knownCasesPassed': len(found),
        'sourceIdentityAndTimesUnchanged': True, 'topRightBottomUnchanged': True,
        'unaffectedPanesByteIdentical': True,
        'warning': 'Correct region does not guarantee perfect OCR. No episode, packing or model-correction claim.',
        'examples': examples}
    m.save(root/'pane-audit.json', result)
    print(json.dumps({k: v for k, v in result.items() if k != 'examples'}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    audit(parser.parse_args().directory.resolve())

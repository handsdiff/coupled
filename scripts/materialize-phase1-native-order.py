#!/usr/bin/env python3
"""Apply only proven native-order permutations to retained pane evidence.

Fresh recognition differences are NOT accepted. All original strings and boxes
survive; native Vision supplies only their ordering. Unreviewed panes stay exact.
"""
import argparse
from collections import Counter, defaultdict, deque
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
PREP = ROOT/'coupled-data/sep02-10-training-prep-20260910'


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024*1024), b''): digest.update(block)
    return digest.hexdigest()


def canonical(value): return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
def text_hash(text): return hashlib.sha256(text.encode()).hexdigest()
def rows(path):
    with path.open() as handle:
        for line in handle:
            if line.strip(): yield json.loads(line)


def permute(old, observed):
    before = [line['text'] for line in old]
    after = [line['text'] for line in observed]
    if Counter(before) != Counter(after): return None
    positions = defaultdict(deque)
    for i, text in enumerate(before): positions[text].append(i)
    permutation = [positions[text].popleft() for text in after]
    assert sorted(permutation) == list(range(len(old)))
    return permutation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--diagnostic', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--adjudications', type=Path, help='Explicit screenshot-verified OCR corrections')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    summary = json.loads((args.diagnostic/'summary.json').read_text())
    observations_path = args.diagnostic/'observations.jsonl'
    assert summary['observationsSHA256'] == sha(observations_path)
    assert summary['ocrArchitecture'] == 'arm64'
    observations = {(r['sourceRecordID'], r['label']): r for r in rows(observations_path)}
    adjudications = json.loads(args.adjudications.read_text()) if args.adjudications else []
    used_adjudications = set()
    totals = {}
    for day in [2,3,4,7]:
        baseline = (PREP/'new-sep7-surfaces-native' if day == 7 else
                    ROOT/f'coupled-data/sep02-04-semantic-v24-review-20260906/sep{day}-surfaces-r2')
        output = args.output/f'sep{day}-surfaces'
        output.mkdir()
        manifest = json.loads((baseline/'read-surface-evidence.json').read_text())
        for name, digest in manifest['artifacts']['digestsSHA256'].items():
            assert sha(baseline/name) == digest
        for name in ['jobs.jsonl','unresolved.jsonl']: shutil.copyfile(baseline/name, output/name)
        counts = Counter()
        with (output/'read-surfaces.jsonl').open('x') as handle, (output/'order-decisions.jsonl').open('x') as audit:
            for before in rows(baseline/'read-surfaces.jsonl'):
                after = deepcopy(before)
                proofs = []
                for label, line_key, content_key, count_key, region_key in [
                    ('pane','lines','content','recognizedLineCount','regionOfInterest'),
                    ('comparison','comparisonLines','comparisonContent','comparisonRecognizedLineCount','comparisonRegionOfInterest'),
                ]:
                    observed = observations.get((before['sourceRecordID'], label))
                    if not observed: continue
                    assert observed['screenshotSHA256'] == before['screenshotSHA256']
                    assert observed['baselineEvidenceID'] == before['evidenceID']
                    assert observed['baselineLines'] == before[line_key]
                    assert all(abs(before[region_key][k]-v)<1e-10 for k,v in observed['observation']['regionOfInterest'].items())
                    source_lines = deepcopy(before[line_key])
                    corrections = []
                    for index, correction in enumerate(adjudications):
                        if (correction['sourceRecordID'], correction['label']) != (before['sourceRecordID'], label): continue
                        assert correction['screenshotSHA256'] == before['screenshotSHA256']
                        assert sha(ROOT/correction['evidence']) == correction['screenshotSHA256']
                        matches = [i for i,line in enumerate(source_lines) if line['text']==correction['before']]
                        assert len(matches)==1, 'Reviewed correction must match exactly one original line'
                        assert sum(line['text']==correction['after'] for line in observed['observation']['lines'])==1
                        source_lines[matches[0]]['text']=correction['after']
                        corrections.append(correction);used_adjudications.add(index)
                    permutation = permute(source_lines, observed['observation']['lines'])
                    if permutation is None:
                        counts['recognitionDifferencesNotAdopted'] += 1
                        continue
                    if permutation == list(range(len(permutation))) and not corrections: continue
                    after[line_key] = [source_lines[i] for i in permutation]
                    after[content_key] = '\n'.join(line['text'] for line in after[line_key])
                    after[content_key+'SHA256'] = text_hash(after[content_key])
                    assert after[count_key] == len(after[line_key])
                    proofs.append({'label': label, 'permutation': permutation,
                                   'observationJobID': observed['observation']['jobID'],
                                   'reviewedCorrections': corrections})
                if proofs:
                    after['orderingEvidence'] = {'version': 'vision-native-order-v1',
                        'baselineEvidenceID': before['evidenceID'],
                        'observationsSHA256': summary['observationsSHA256'], 'proofs': proofs}
                    after['evidenceID'] = 'evidence_'+text_hash(canonical([before['evidenceID'], after['orderingEvidence']]))
                    counts['changedPanes'] += 1
                else:
                    assert before == after
                    counts['exactlyUnchangedPanes'] += 1
                handle.write(canonical(after)+'\n')
                if proofs: audit.write(canonical({'sourceRecordID':before['sourceRecordID'], **after['orderingEvidence']})+'\n')
        manifest['postOCR'] = {'version': 'vision-native-order-v1', 'scope': 'reviewed_screenshots_only',
            'baselineManifestSHA256': sha(baseline/'read-surface-evidence.json'),
            'diagnosticDirectory': str(args.diagnostic.resolve()),
            'observationsSHA256': summary['observationsSHA256'],
            'adjudicationsSHA256': sha(args.adjudications) if args.adjudications else None,
            'policy': 'native-order permutation; preserve original lines and boxes except explicitly screenshot-verified, hash-bound corrections; reject all other recognition differences',
            'counts': dict(counts)}
        manifest['artifacts']['digestsSHA256'] = {name: sha(output/name) for name in ['jobs.jsonl','read-surfaces.jsonl','unresolved.jsonl','order-decisions.jsonl']}
        with (output/'read-surface-evidence.json').open('x') as handle: json.dump(manifest,handle,indent=2,sort_keys=True)
        totals[str(day)] = dict(counts)
    assert used_adjudications==set(range(len(adjudications))), 'Unused adjudication'
    with (args.output/'summary.json').open('x') as handle: json.dump(totals,handle,indent=2,sort_keys=True)
    print(json.dumps(totals,indent=2))


if __name__ == '__main__': main()

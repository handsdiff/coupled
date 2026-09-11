#!/usr/bin/env python3
"""Audit the offline refinements and replay the 39 reviewed boundaries.

The projection below is diagnostic: it maps exact retained semantic lines to
their refined observations without rerunning deduplication/episode construction.
Unmappable semantic text is explicitly left unchanged, never guessed.
"""
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import html
import json
from pathlib import Path
import resource
import sys

from phase1_read_boundary import ReadBoundaryEvidence
from phase1_order_invariance import preserve_order_invariant_proof

ROOT = Path(__file__).resolve().parents[1]
PREP = ROOT/'coupled-data/sep02-10-training-prep-20260910'
CAUSES = ROOT/'coupled-data/sep02-10-fidelity-causes-20260911'


def read(p): return json.loads(p.read_text())
def rows(p):
    with p.open() as f:
        for line in f:
            if line.strip(): yield json.loads(line)
def sha(p):
    h = hashlib.sha256()
    with p.open('rb') as f:
        for c in iter(lambda: f.read(1024*1024), b''): h.update(c)
    return h.hexdigest()
def save(p, value):
    with p.open('x') as f: f.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)+'\n')
def canonical(value): return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def validate_projection(old, new, decision, ocr):
    for key in ['sourceRecordID', 'sessionID', 'capturedAt', 'screenshotSHA256', 'screenshotRelativePath']:
        assert old[key] == new[key], ('Source identity/time changed', key)
    if not decision['changed']:
        assert old == new
    for lk, ck in [('lines', 'content'), ('comparisonLines', 'comparisonContent')]:
        expected = [l['text'] for l in old[lk]]
        edge_key = 'edgeValidation' if lk == 'lines' else 'comparisonEdgeValidation'
        proof = new.get('surfaceSelection', {}).get(edge_key) if decision['pane']['accepted'] else None
        if proof and proof['accepted']:
            observed = ocr[new['jobID']]
            assert observed['screenshotSHA256'] == old['screenshotSHA256']
            assert all(abs(observed['regionOfInterest'][k]-v) < 1e-11 for k,v in new['regionOfInterest'].items())
            for w in proof['witnesses']:
                assert observed['lines'][w['widerIndex']]['text'] == w['widerText']
                assert w['before'] == expected[w['oldIndex']]
                assert w['oldAnchorStart'] <= 1
                assert w['preservedSuffix'] == w['before'][w['oldAnchorStart']:]
                assert w['recoveredPrefix'] == w['widerText'][:w['wideAnchorStart']]
                expected[w['oldIndex']] = w['recoveredPrefix'] + w['preservedSuffix']
        order = decision.get('ordering', {}).get(lk, {})
        permutation = order.get('permutation', list(range(len(expected))))
        assert sorted(permutation) == list(range(len(expected)))
        expected = [expected[i] for i in permutation]
        assert expected == [l['text'] for l in new[lk]]
        assert len(old[lk]) == len(new[lk])
        if old[ck] != new[ck]:
            assert '\n'.join(expected) == new[ck]
        assert hashlib.sha256(new[ck].encode()).hexdigest() == new[ck+'SHA256']


def project(event, old, new, decision):
    if not decision['changed']:
        return None
    value = json.loads(event['serialized'])
    lines = value['content'].splitlines()
    positions = defaultdict(list)
    for i, line in enumerate(old['lines']): positions[line['text']].append(i)
    if not lines or any(len(positions[line]) != 1 for line in lines):
        return None
    permutation = decision.get('ordering', {}).get('lines', {}).get('permutation', list(range(len(old['lines']))))
    inverse = {old_index: new_index for new_index, old_index in enumerate(permutation)}
    indices = [inverse[positions[line][0]] for line in lines]
    value['content'] = '\n'.join(new['lines'][i]['text'] for i in sorted(indices))
    # If no semantic change is necessary, this remains the original event.
    if value['content'] == json.loads(event['serialized'])['content']:
        return None
    return dict(event, serialized=canonical(value))


def audit(root, output=None):
    output = output or root/'audit'
    if output.exists(): raise ValueError('Use a fresh audit directory')
    frozen = read(CAUSES/'stage-evidence.json')
    annotations = read(CAUSES/'review-annotations.json')
    events = list(rows(PREP/'micro-corpus/events.jsonl'))
    by_source = defaultdict(list)
    for i, event in enumerate(events):
        if event['kind'] == 'read':
            for rid in event.get('sourceRecordIDs', []): by_source[rid].append(i)
    updated = {}
    old_panes, new_panes = [], []
    changed_ids = set()
    pane_for_review = {}
    frame_decisions = {}
    verified_rows = 0
    untouched = 0
    order_reasons = Counter()
    summary = {}
    manifests = {}
    raw_paths = []
    for day in [2, 3, 4, 7]:
        directory = root/f'sep{day}-final'
        m = read(directory/'read-surface-evidence.json'); manifests[str(directory)] = sha(directory/'read-surface-evidence.json')
        baseline = Path(m['source']['baseline']); raw_paths.append(Path(m['source']['rawPath']))
        assert sha(baseline) == m['source']['baselineSHA256']
        assert sha(raw_paths[-1]) == m['source']['rawSHA256']
        for path, digest in m.get('reusedOCRCacheSHA256', {}).items(): assert sha(Path(path)) == digest
        for name, digest in m['artifacts']['digestsSHA256'].items(): assert sha(directory/name) == digest
        ocr = {}
        for p in [*[Path(p) for p in m.get('reusedOCRCacheSHA256', {})], directory/'ocr-results.jsonl']:
            for r in rows(p): ocr[r['jobID']] = r
        summary[str(day)] = m['counts']
        old_rows, new_rows, decisions = rows(baseline), rows(directory/'read-surfaces.jsonl'), rows(directory/'decisions.jsonl')
        seen = set()
        for old, new, decision in zip(old_rows, new_rows, decisions, strict=True):
            if resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/(1024**2 if sys.platform=='darwin' else 1024) > 1024:
                raise MemoryError('Audit exceeds 1 GiB RSS')
            assert old['sourceRecordID'] == new['sourceRecordID'] == decision['sourceRecordID']
            validate_projection(old, new, decision, ocr); verified_rows += 1
            rid = old['sourceRecordID']; assert rid not in seen; seen.add(rid)
            if decision['changed']: changed_ids.add(rid)
            else: untouched += 1
            for value in decision.get('ordering', {}).values(): order_reasons[value['reason']] += 1
            keys = ['sourceRecordID', 'capturedAt', 'content', 'evidenceID', 'screenshotSHA256']
            old_panes.append({k: old[k] for k in keys}); new_panes.append({k: new[k] for k in keys})
            for i in by_source[rid]:
                e = events[i]
                if new['capturedAt'] > e['availableAt']: continue
                result = project(e, old, new, decision)
                if result is not None and (i not in updated or new['capturedAt'] > updated[i][0]):
                    updated[i] = (new['capturedAt'], result)
            if rid in frozen['panes']:
                pane_for_review[rid] = (old, new)
                frame_decisions[rid] = decision
        assert len(seen) == m['counts']['inputPanes']
    assert verified_rows == 11579
    print('Verified every pane:', verified_rows, 'exactly preserved:', untouched, flush=True)
    raw_views = []
    for p in raw_paths:
        for r in rows(p):
            if resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/(1024**2 if sys.platform=='darwin' else 1024) > 1024:
                raise MemoryError('Audit exceeds 1 GiB RSS')
            if r.get('recordType') in ['screen_ocr_observation', 'visual_ocr_observation']:
                raw_views.append({k:r.get(k) for k in ['recordID','recordType','sessionID','appName','capturedAt','content','windowID','contentWasTruncated','screenshotSHA256']})
    results = {}
    # Separate indexes avoid retaining both large indexes concurrently.
    for variant, panes in [('baseline', old_panes), ('refined', new_panes)]:
        stream = events if variant == 'baseline' else [updated[i][1] if i in updated else e for i,e in enumerate(events)]
        index = ReadBoundaryEvidence(stream, raw_views, panes)
        lookup = {e['sourceEventID']: e for e in stream}
        results[variant] = []
        for b in frozen['boundaries']:
            candidate = frozen['candidates'][b['leftCandidateID']]
            field = candidate['members'][0].get('beforeLogicalValue') or ''
            draft = b['baselineBoundary'].get('internalRevisionEvidence',{}).get('currentNetEdit',{}).get('content') or b['draft']
            assessed = [index.assess(lookup[r['event']['sourceEventID']], b['beganAt'], draft, b['destination']['application'],field) for r in b['reads']]
            if variant == 'baseline':
                for r,a in zip(b['reads'], assessed):
                    assert r['v10Assessment']['boundaryEvidence']['unexplainedWords'] == a['boundaryEvidence']['unexplainedWords'], (b['candidateLine'], 'Baseline mismatch')
            else:
                baseline_decisions = next(x for x in results['baseline'] if x['candidateLine'] == b['candidateLine'])['assessments']
                assessed = [preserve_order_invariant_proof(r['event'], lookup[r['event']['sourceEventID']], prior, current)
                            for r, prior, current in zip(b['reads'], baseline_decisions, assessed)]
            results[variant].append({'candidateLine': b['candidateLine'], 'cases': [b['leftCase'],b['rightCase']],
                'fullyExplained': all(a['boundaryEvidence']['unexplainedWords'] == 0 for a in assessed), 'assessments': assessed})
            print(variant, b['candidateLine'], [a['boundaryEvidence']['unexplainedWords'] for a in assessed], flush=True)
        del index
    comparison = []
    for before, after in zip(results['baseline'], results['refined']):
        disposition = annotations['boundaries'][str(before['candidateLine'])]['disposition']
        comparison.append({'candidateLine': before['candidateLine'], 'cases': before['cases'], 'disposition': disposition,
                           'beforeFullyExplained': before['fullyExplained'], 'afterFullyExplained': after['fullyExplained']})
    controls = [r for r in comparison if r['disposition'] == 'retain_boundary']
    lost = [r for r in comparison if r['beforeFullyExplained'] and not r['afterFullyExplained']]
    # Report failures as blockers; do not hide them or re-label the gold set.
    gates = {'novelControlsRetained': all(not r['afterFullyExplained'] for r in controls),
             'previouslyExplainedRemainExplained': not lost,
             'allRowsContentProvenanceChecked': True, 'timestampsAndImagesUnchanged': True}
    output.mkdir()
    save(output/'boundaries.json', {'results': results, 'comparison': comparison, 'gates': gates})
    save(output/'summary.json', {'paneCounts': summary, 'verifiedPanes': verified_rows, 'preservedExactly': untouched,
        'orderDispositions': dict(order_reasons), 'projectedSemanticReads': len(updated), 'gates': gates,
        'lostBaselineProofs': lost, 'scope': 'Offline pane/line refinement plus diagnostic exact-line projection; NOT full semantic/episode recompilation',
        'inputManifestHashes': manifests,
        'codeHashes': {p.name: sha(p) for p in [Path(__file__), ROOT/'scripts/phase1_order_invariance.py', ROOT/'scripts/phase1_read_boundary.py']},
        'peakRSSMiB': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/(1024**2 if sys.platform=='darwin' else 1024)})
    # A compact before/after surface review; explicitly not model-context UI.
    h = ['<!doctype html><meta charset="utf-8"><title>Pane and line-order review</title>',
         '<style>body{font:16px system-ui;max-width:1500px;margin:30px auto;padding:0 20px;background:#f6f7f8}section{padding:20px;margin:25px 0;background:white;border:1px solid #ccc}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.5 ui-monospace} .pair{display:grid;grid-template-columns:1fr 1fr;gap:24px}img{max-width:720px;width:100%}summary{cursor:pointer}h3{margin:8px 0}</style>',
         '<h1>Pane and line-order fixes: before / after</h1><p>Same retained screenshots and timestamps. These panels show the OCR pane stage, <b>not finalized model context</b>. No canonical data changed.</p>']
    for b in frozen['boundaries']:
        h += [f'<section id="case-{b["leftCase"]}"><h2>Cases {b["leftCase"]} → {b["rightCase"] or "continuation"}</h2>']
        shown = set()
        for r in b['reads']:
            for rid in r['paneRecordIDs']:
                if rid not in pane_for_review or rid in shown: continue
                shown.add(rid); old,new = pane_for_review[rid]; raw = frozen['raw'][rid]
                img = ROOT/Path(raw['auditSourcePath']).parent/raw['screenshotRelativePath']
                h += [f'<p>{html.escape(rid)} · pane expanded: {frame_decisions[rid]["pane"]["accepted"]}</p>',
                      f'<details><summary>Raw screenshot</summary><a href="{html.escape(img.as_uri())}"><img loading="lazy" src="{html.escape(img.as_uri())}"></a></details>',
                      '<div class="pair"><div><h3>Before</h3><pre>'+html.escape(old['content'])+'</pre></div><div><h3>After</h3><pre>'+html.escape(new['content'])+'</pre></div></div>']
        h.append('</section>')
    with (output/'review.html').open('x') as f: f.write('\n'.join(h))
    print(json.dumps({'gates':gates, 'lostProofs':lost, 'review':str(output/'review.html')}, indent=2))
    if not all(gates.values()):
        raise RuntimeError('Regression gate failed; artifacts remain diagnostic, not promotable')


if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('--root', type=Path, required=True)
    p.add_argument('--output', type=Path)
    a=p.parse_args();audit(a.root.resolve(), a.output.resolve() if a.output else None)

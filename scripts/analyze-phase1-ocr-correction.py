#!/usr/bin/env python3
"""Local-only OCR/boundary diagnostic; never creates training-authoritative data."""
import argparse
from collections import Counter
import difflib
import hashlib
import html
import importlib.util
import json
from pathlib import Path
import re
import statistics

from phase1_read_boundary import app_name, explain, residual_runs, tokens


def read(path):
    return json.loads(Path(path).read_text())


def flat(text):
    return ' '.join(text.split())


def changes(before, after):
    a, b = flat(before), flat(after)
    return [{'before': a[i:j], 'after': b[k:l], 'prefix': a[max(0, i-45):i], 'suffix': a[j:j+45]}
            for op, i, j, k, l in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if op != 'equal']


def coverage(current, prior, draft, field):
    observed, authored = tokens(current), tokens(draft)
    known = explain(tokens(prior), observed)
    draft_known = explain(authored, observed)
    field_known = explain(tokens(field), observed) if len(authored) >= 3 and len(draft_known) >= 3 else set()
    covered = known | draft_known | field_known
    return {'unexplainedWords': len(observed) - len(covered), 'observedWords': len(observed),
            'unexplainedRuns': residual_runs(observed, covered), 'fullyExplained': len(covered) == len(observed)}


def analyze(root):
    def sha(path):
        h = hashlib.sha256()
        with Path(path).open('rb') as source:
            for part in iter(lambda: source.read(1024 * 1024), b''): h.update(part)
        return h.hexdigest()
    plan = read(root / 'pilot/plan.json')
    assert sha(root/'pilot/documents.json') == plan['documentsSHA256']
    assert read(root/'pilot/documents.json') == read(root/'inputs/documents.json')
    assert sha(root/'pilot/requests.json') == plan['requestsSHA256']
    for name, expected in plan['codeHashes'].items():
        assert sha(root/'pilot/code'/name) == expected, f'Frozen code changed: {name}'
    spec = importlib.util.spec_from_file_location('ocr_frozen_audit', root/'pilot/code/run-phase1-ocr-correction.py')
    runner = importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)
    docs = {d['documentID']: d for d in read(root / 'inputs/documents.json')}
    neighborhoods = read(root / 'inputs/neighborhoods.local.json')
    records = [read(p) for p in sorted((root / 'pilot/results').glob('*/result.json'))]
    requests = read(root/'pilot/requests.json')
    for i, r in enumerate(records):
        attempt = root/f'pilot/results/{i:04d}'
        req = requests[i]
        assert all(r[k] == req[k] for k in ('model', 'documentID'))
        binding = read(attempt/'inflight.json')
        assert binding['planSHA256'] == sha(root/'pilot/plan.json') and binding['request'] == req
        body = runner.payload(req['model'], docs[req['documentID']]['text'])
        assert read(attempt/'request.json') == body
        assert hashlib.sha256(runner.canonical(body).encode()).hexdigest() == binding['bodySHA256']
        timing = read(attempt/'transport.json')
        assert sha(attempt/'response.body') == timing['bodySHA256']
        if r['validCompletion']:
            derived = runner.interpret((attempt/'response.body').read_bytes(), req['model'])
            assert {**derived, **req, 'timing': timing} == r
    results = {(r['model'], r['documentID']): r for r in records}
    models = read(root / 'pilot/plan.json')['models']
    document_report = []
    for key, doc in docs.items():
        item = dict(doc, models={})
        for model in models:
            r = results.get((model, key))
            if not r:
                continue
            after = r.get('correctedText', '')
            item['models'][model] = {**r, 'edits': changes(doc['text'], after),
                'unchanged': doc['text'] == after, 'whitespaceOnlyChange': doc['text'] != after and flat(doc['text']) == flat(after),
                'digitsChanged': re.findall(r'\d+', doc['text']) != re.findall(r'\d+', after),
                'lengthRatio': len(after) / max(1, len(doc['text']))}
        document_report.append(item)
    boundary_report = []
    for n in neighborhoods:
        entry = {k: v for k, v in n.items() if k not in {'draftForLocalAuditOnly', 'initialFieldForLocalAuditOnly', 'pairs'}}
        entry['pairs'] = []
        for p in n['pairs']:
            cur, prev = docs[p['currentDocumentID']], docs.get(p['priorDocumentID'])
            read_app = next(ref.get('application') for ref in cur['references'] if ref.get('application'))
            draft = n['draftForLocalAuditOnly'] if app_name(read_app) == app_name(n['application']) else ''
            field = n['initialFieldForLocalAuditOnly'] if draft else ''
            pair = dict(p, baselineFixedPair=coverage(cur['text'], prev['text'] if prev else '', draft, field), models={})
            assert pair['baselineFixedPair']['unexplainedWords'] == p['baselineAssessment']['boundaryEvidence']['unexplainedWords'], 'Original comparison no longer reproduces the frozen boundary diagnostic'
            for model in models:
                a, b = results.get((model, p['currentDocumentID'])), results.get((model, p['priorDocumentID'])) if prev else None
                if a and a['validCompletion'] and (not prev or b and b['validCompletion']):
                    pair['models'][model] = coverage(a['correctedText'], b['correctedText'] if b else '', draft, field)
            entry['pairs'].append(pair)
        entry['baselineFullyExplained'] = all(p['baselineFixedPair']['fullyExplained'] for p in entry['pairs'])
        entry['modelFullyExplained'] = {m: (all(p['models'][m]['fullyExplained'] for p in entry['pairs'])
                                           if all(m in p['models'] for p in entry['pairs']) else None) for m in models}
        boundary_report.append(entry)
    summary = {'scope': 'Fixed selected-comparison diagnostic; no full episode reconstruction, no automatic promotion',
               'inputRepresentations': dict(Counter(ref['representation'] for d in docs.values() for ref in d['references'])),
               'characterAccuracy': 'Not quantified: screenshots require manual transcription audit; fewer residuals alone is not fidelity',
               'plannedRequests': len(docs) * len(models), 'recordedRequests': len(records),
               'complete': len(records) == len(docs) * len(models), 'models': {}}
    summary['artifactAudit'] = {'status': 'passed', 'verifiedResults': len(records),
                              'originalComparisonResidualsReproduced': sum(len(n['pairs']) for n in neighborhoods),
                              'planSHA256': sha(root/'pilot/plan.json'),
                              'analysisCodeSHA256': sha(__file__),
                              'boundaryCodeSHA256': sha(Path(__file__).with_name('phase1_read_boundary.py')),
                              'requestPolicy': 'Exact frozen OCR-only request; no screenshots or later WRITE targets submitted'}
    for m in models:
        group = [d['models'][m] for d in document_report if m in d['models']]
        valid = [r for r in group if r['validCompletion']]
        controls = [d['models'][m] for d in document_report if m in d['models'] and any(ref['role'] == 'random_ordinary_read_control' for ref in d['references'])]
        repaired = [b for b in boundary_report if b['expectedBoundary'] == 'repair_non_novel']
        retained = [b for b in boundary_report if b['expectedBoundary'] == 'retain_new_information']
        seconds = [r['timing']['dispatchToCompletionSeconds'] for r in group]
        summary['models'][m] = {'recorded': len(group), 'valid': len(valid),
            'unchanged': sum(r['unchanged'] for r in valid), 'whitespaceOnly': sum(r['whitespaceOnlyChange'] for r in valid),
            'nonWhitespaceEdits': sum(bool(r['edits']) for r in valid), 'documentsWithDigitsChanged': sum(r['digitsChanged'] for r in valid),
            'ordinaryControlsWithNonWhitespaceEdits': sum(bool(r['edits']) for r in controls),
            'medianLatencySeconds': statistics.median(seconds) if seconds else None,
            'meanLatencySeconds': statistics.mean(seconds) if seconds else None,
            'inputTokens': sum(r.get('usage', {}).get('input_tokens', 0) for r in group),
            'outputTokens': sum(r.get('usage', {}).get('output_tokens', 0) for r in group),
            'apiEquivalentUncachedUSD': sum(r.get('apiEquivalentUncachedUSD', 0) for r in group),
            'baselineFullyExplainedRepairNeighborhoods': sum(b['baselineFullyExplained'] for b in repaired),
            'repairNeighborhoodsWithCompletePairedResults': sum(b['modelFullyExplained'][m] is not None for b in repaired),
            'correctedFullyExplainedRepairNeighborhoods': sum(b['modelFullyExplained'][m] is True for b in repaired),
            'newlyFullyExplainedCases': [[b['leftCase'], b['rightCase']] for b in repaired if not b['baselineFullyExplained'] and b['modelFullyExplained'][m] is True],
            'lostFullyExplainedCases': [[b['leftCase'], b['rightCase']] for b in repaired if b['baselineFullyExplained'] and b['modelFullyExplained'][m] is False],
            'genuineNewInformationStillBlocking': sum(b['modelFullyExplained'][m] is False for b in retained)}
    output = root / 'analysis'
    output.mkdir(exist_ok=True)
    for name, data in [('summary.json', summary), ('document-review.json', document_report), ('boundary-review.json', boundary_report)]:
        (output / name).write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + '\n')
    lines = ['# OCR correction: Luna versus Terra', '', 'Subscription-only shadow experiment. Raw capture and canonical data are unchanged.', '',
             '## Results', '', '```json', json.dumps(summary, indent=2), '```', '',
             '## Boundary neighborhoods', '', '| Cases | Expected | Baseline explained | Luna explained | Terra explained |',
             '|---|---|---|---|---|']
    for b in boundary_report:
        label = f"{b['leftCase']} → {b['rightCase'] or 'history'}"
        lines.append(f"| {label} | {b['expectedBoundary']} | {b['baselineFullyExplained']} | {b['modelFullyExplained'][models[0]]} | {b['modelFullyExplained'][models[1]]} |")
    (output / 'report.md').write_text('\n'.join(lines) + '\n')
    parts = ['<!doctype html><meta charset="utf-8"><title>OCR correction pilot</title>',
             '<style>body{font:16px system-ui;margin:2rem;max-width:1800px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f5f5;padding:1rem}.cols{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1rem}article{border-top:2px solid #ccc;margin:3rem 0}summary{cursor:pointer}img{max-width:100%}a{color:#164d82}</style>',
             '<h1>Original OCR · Luna · Terra</h1><p>Text correction only. Neither model received screenshots, comparison text, or later WRITE targets. Edits are not automatically accepted.</p>',
             '<details><summary>Aggregate diagnostic</summary><pre>' + html.escape(json.dumps(summary, indent=2)) + '</pre></details>']
    for i, d in enumerate(document_report, 1):
        cases = sorted({ref['candidateLine'] for ref in d['references'] if ref.get('candidateLine') is not None})
        labels = [f"{n['leftCase']} → {n['rightCase'] or 'history'}" for n in neighborhoods if n['candidateLine'] in cases]
        representations = ', '.join(sorted({ref['representation'].replace('_', ' ') for ref in d['references']}))
        roles = ', '.join(sorted({ref['role'].replace('_', ' ') for ref in d['references']}))
        parts.append(f'<article id="doc-{i}"><h2>{i} · {html.escape(", ".join(labels) or "Ordinary READ control")}</h2><p>Input: {html.escape(representations)}. Role: {html.escape(roles)}.</p><div class="cols"><section><h3>Original input</h3><pre>{html.escape(d["text"])}</pre></section>')
        for m in models:
            r = d['models'].get(m, {})
            parts.append(f'<section><h3>{html.escape(m.removeprefix("chatgpt/"))}</h3><pre>{html.escape(r.get("correctedText", "Pending"))}</pre><details><summary>Non-whitespace edits</summary><pre>{html.escape(json.dumps(r.get("edits", []), indent=2, ensure_ascii=False))}</pre></details></section>')
        parts.append('</div><details><summary>Screenshot evidence (local only)</summary>')
        seen = set()
        for ref in d['references']:
            for image in ref.get('screenshots', []):
                path = image['path']
                if path in seen or not Path(path).is_file(): continue
                seen.add(path)
                parts.append(f'<p>{html.escape(str(image.get("capturedAt", "")))}</p><img loading="lazy" src="{html.escape(Path(path).as_uri())}">')
        parts.append('</details></article>')
    (output / 'review.html').write_text('\n'.join(parts))
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('directory', type=Path)
    a = p.parse_args(); analyze(a.directory.resolve())

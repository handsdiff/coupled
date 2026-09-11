#!/usr/bin/env python3
"""Audit and compare contextual Luna with saved isolated Luna, locally only."""
import argparse
import hashlib
import html
import importlib.util
import json
from pathlib import Path
import statistics

from phase1_read_boundary import app_name


def module(path, name):
    s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m


def read(p): return json.loads(Path(p).read_text())


def sha(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()


def save(p,obj): p.write_text(json.dumps(obj,indent=2,sort_keys=True,ensure_ascii=False)+'\n')


def analyze(root):
    inputs=root/'inputs'; manifest=read(inputs/'manifest.json');base=Path(manifest['baselinePath'])
    assert sha(inputs/'documents.json')==manifest['documentsSHA256']
    assert sha(inputs/'all-observations.json')==manifest['allObservationsSHA256']
    assert sha(inputs/'neighborhoods.local.json')==manifest['bindingsSHA256']
    assert sha(base/'inputs/documents.json')==manifest['baselineDocumentsSHA256']
    assert sha(base/'inputs/neighborhoods.local.json')==manifest['baselineNeighborhoodsSHA256']
    for name,h in manifest['baselineResultsSHA256'].items(): assert sha(base/name)==h
    old_docs={x['documentID']:x for x in read(base/'inputs/documents.json')}
    docs={x['documentID']:x for x in read(inputs/'all-observations.json')}
    neighborhoods=read(inputs/'neighborhoods.local.json')
    # Independently verify supplied context against the source artifact, not just
    # the preparation's summary. Keep only the selected reference records.
    wanted={e['sourceRecordID'] for d in docs.values() for e in d['referenceEvidence']}
    references={}
    for path,h in manifest['sourceHashes'].items():
        assert sha(path)==h
        if Path(path).name!='read-surfaces.jsonl':continue
        with Path(path).open() as f:
            for line in f:
                if not line.strip():continue
                r=json.loads(line)
                if r['sourceRecordID'] in wanted:references[r['sourceRecordID']]=r
    assert set(references)==wanted
    reference_count=0
    for d in docs.values():
        assert d['text']==old_docs[d['originDocumentID']]['text']
        assert len(d['previousObservations'])==len(d['referenceEvidence'])<=3
        times=[]
        for r,e in zip(d['previousObservations'],d['referenceEvidence']):
            observed=references[e['sourceRecordID']]
            assert r['ocrText']==observed['content']
            assert r['capturedAt']==observed['capturedAt']<d['capturedAt']
            assert e['surfaceKey'][:4]==d['surfaceKey'][:4]
            assert e['screenshotSHA256']==observed['screenshotSHA256']
            times.append(r['capturedAt']);reference_count+=1
        assert times==sorted(times)
    old={r['documentID']:r for p in sorted((base/'pilot/results').glob('*/result.json'))
         if (r:=read(p))['model']=='chatgpt/gpt-5.6-luna'}
    exe=root/'pilot';plan=read(exe/'plan.json')
    for name,h in plan['codeHashes'].items(): assert sha(exe/'code'/name)==h
    assert sha(exe/'documents.json')==plan['documentsSHA256']
    assert read(exe/'documents.json')==read(inputs/'documents.json')
    assert sha(exe/'requests.json')==plan['requestsSHA256']
    runner=module(exe/'code/run-phase1-ocr-correction.py','contextual_runner')
    diagnostic=module(Path(__file__).with_name('analyze-phase1-ocr-correction.py'),'isolated_diagnostic')
    schedule=read(exe/'requests.json');results={}
    for i,request in enumerate(schedule):
        attempt=exe/f'results/{i:04d}'
        if not (attempt/'result.json').exists():continue
        doc=docs[request['documentID']]
        body=runner.payload(request['model'],doc['text'],doc['previousObservations'])
        binding=read(attempt/'inflight.json')
        assert binding=={'planSHA256':sha(exe/'plan.json'),'request':request,'bodySHA256':hashlib.sha256(runner.canonical(body).encode()).hexdigest()}
        assert read(attempt/'request.json')==body
        transport=read(attempt/'transport.json');assert sha(attempt/'response.body')==transport['bodySHA256']
        result=read(attempt/'result.json')
        if result['validCompletion']:
            assert result==dict(runner.interpret((attempt/'response.body').read_bytes(),request['model']),**request,timing=transport)
        results[doc['documentID']]=result
    def text(key, mode):
        if key is None:return ''
        doc=docs[key]
        if mode=='original':return doc['text']
        if mode=='isolated' or doc['disposition'].startswith('reuse_'):
            return old[doc['originDocumentID']]['correctedText']
        r=results.get(key)
        return r['correctedText'] if r and r['validCompletion'] else None
    comparisons=[]
    for n in neighborhoods:
        row={k:n[k] for k in ['leftCase','rightCase','candidateLine','expectedBoundary']};row['pairs']=[]
        for p in n['pairs']:
            current=docs[p['contextualCurrentID']]
            origin=old_docs[current['originDocumentID']]
            application=current['originalReference']['application']
            draft=n['draftForLocalAuditOnly'] if app_name(application)==app_name(n['application']) else ''
            field=n['initialFieldForLocalAuditOnly'] if draft else ''
            q={'currentID':p['contextualCurrentID'],'priorID':p['contextualPriorID'],'modes':{}}
            for mode in ['original','isolated','contextual']:
                a,b=text(p['contextualCurrentID'],mode),text(p['contextualPriorID'],mode)
                q['modes'][mode]=diagnostic.coverage(a,b,draft,field) if a is not None and b is not None else None
            assert q['modes']['original']['unexplainedWords']==p['baselineAssessment']['boundaryEvidence']['unexplainedWords']
            row['pairs'].append(q)
        row['fullyExplained']={mode: all(p['modes'][mode]['fullyExplained'] for p in row['pairs'])
            if all(p['modes'][mode] is not None for p in row['pairs']) else None for mode in ['original','isolated','contextual']}
        comparisons.append(row)
    repair=[r for r in comparisons if r['expectedBoundary']=='repair_non_novel']
    controls=[r for r in comparisons if r['expectedBoundary']=='retain_new_information']
    valid=[r for r in results.values() if r['validCompletion']]
    times=[r['timing']['dispatchToCompletionSeconds'] for r in valid]
    summary={'complete':len(results)==len(schedule), 'plannedRequests':len(schedule),'savedRequests':len(results),'validRequests':len(valid),
             'completedContextualNeighborhoods':sum(r['fullyExplained']['contextual'] is not None for r in comparisons),
             'observationsWithoutAddedContext':sum(not d['previousObservations'] for d in docs.values()),
             'verifiedPriorReferenceOccurrences':reference_count,
             'fullyExplainedIntendedMerges':{m:sum(r['fullyExplained'][m] is True for r in repair) for m in ['original','isolated','contextual']},
             'retainedNoveltyControls':{m:sum(r['fullyExplained'][m] is False for r in controls) for m in ['original','isolated','contextual']},
             'newlyExplainedVsIsolated':[[r['leftCase'],r['rightCase']] for r in repair if r['fullyExplained']['isolated'] is False and r['fullyExplained']['contextual'] is True],
             'lostVsIsolated':[[r['leftCase'],r['rightCase']] for r in repair if r['fullyExplained']['isolated'] is True and r['fullyExplained']['contextual'] is False],
             'medianLatencySeconds':statistics.median(times) if times else None,'meanLatencySeconds':statistics.mean(times) if times else None,
             'inputTokens':sum(r['usage']['input_tokens'] for r in valid),'outputTokens':sum(r['usage']['output_tokens'] for r in valid),
             'apiEquivalentUncachedUSD':sum(r['apiEquivalentUncachedUSD'] for r in valid),
             'audit':'Exact original inputs, original prior residuals, frozen request and full wire hashes verified',
             'analysisSHA256':sha(__file__),'matchingCodeSHA256':sha(Path(__file__).with_name('phase1_read_boundary.py')),
             'scope':'Fixed-comparison diagnostic; neither full pipeline replay nor character-accuracy ground truth'}
    for baseline_mode in ['original','isolated']:
        eligible=[r for r in repair if r['fullyExplained']['contextual'] is not None]
        differences=[(sum(p['modes'][baseline_mode]['unexplainedWords'] for p in r['pairs']),
                      sum(p['modes']['contextual']['unexplainedWords'] for p in r['pairs'])) for r in eligible]
        summary['unmatchedWordChangeVs'+baseline_mode.capitalize()]={
            'completedNeighborhoods':len(eligible),'fewer':sum(b<a for a,b in differences),
            'same':sum(b==a for a,b in differences),'more':sum(b>a for a,b in differences)}
    initially_unresolved=[r for r in repair if r['fullyExplained']['original'] is False and r['fullyExplained']['contextual'] is not None]
    initial_deltas=[(sum(p['modes']['original']['unexplainedWords'] for p in r['pairs']),
                    sum(p['modes']['contextual']['unexplainedWords'] for p in r['pairs'])) for r in initially_unresolved]
    summary['originallyUnresolvedSubset']={
        'completedNeighborhoods':len(initially_unresolved),
        'fewer':sum(b<a for a,b in initial_deltas),'same':sum(b==a for a,b in initial_deltas),
        'more':sum(b>a for a,b in initial_deltas),'fullyExplained':sum(b==0 for a,b in initial_deltas)}
    output=root/'analysis';output.mkdir(exist_ok=True)
    save(output/'summary.json',summary);save(output/'boundary-review.json',comparisons)
    manual_path=output/'manual-review.json'
    manual=read(manual_path)['reviews'] if manual_path.exists() else []
    parts=['<!doctype html><meta charset="utf-8"><title>Luna OCR with earlier views</title>',
           '<style>body{font:16px system-ui;margin:2rem}.cols{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1rem}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f4f4;padding:1rem}article{border-top:2px solid #aaa;margin:3rem 0}summary{cursor:pointer}img{max-width:100%}</style>',
           '<h1>Original OCR · Luna alone · Luna with earlier views</h1><p>Only original earlier reference text was added. Current text is identical to the first experiment. This is a shadow diagnostic.</p>',
           '<details><summary>Aggregate</summary><pre>'+html.escape(json.dumps(summary,indent=2))+'</pre></details>']
    reviews=[]
    for n in neighborhoods:
        row=next(r for r in comparisons if r['candidateLine']==n['candidateLine'])
        parts.append(f'<article id="case-{n["leftCase"]}"><h2>Case {n["leftCase"]} → {n["rightCase"] or "history"}</h2><p>Fully explained: {html.escape(str(row["fullyExplained"]))}</p>')
        for review in manual:
            if review['case']==n['leftCase']:
                parts.append('<p><strong>Inspected: '+html.escape(review['verdict'])+'</strong> — '+html.escape(review['details'])+'</p>')
        for pair in n['pairs']:
            key=pair['contextualCurrentID'];doc=docs[key]
            parts.append('<div class="cols">')
            for mode in ['original','isolated','contextual']:
                t=text(key,mode)
                parts.append('<section><h3>'+mode+'</h3><pre>'+html.escape(t if t is not None else 'Pending')+'</pre></section>')
            parts.append('</div><details><summary>Earlier reference views sent to Luna</summary>')
            for ref in doc['previousObservations']:
                parts.append('<h4>'+html.escape(ref['capturedAt'])+'</h4><pre>'+html.escape(ref['ocrText'])+'</pre>')
            parts.append('</details><details><summary>Current raw screenshots (local only)</summary>')
            for ref in doc['originalReference'].get('screenshots',[]):
                parts.append('<img loading="lazy" src="'+html.escape(Path(ref['path']).as_uri())+'">')
            parts.append('</details>')
            prior_key=pair['contextualPriorID']
            if prior_key:
                parts.append('<details><summary>Selected earlier comparison: original / isolated / contextual</summary><div class="cols">')
                for mode in ['original','isolated','contextual']:
                    value=text(prior_key,mode)
                    parts.append('<section><h3>'+mode+'</h3><pre>'+html.escape(value if value is not None else 'Pending')+'</pre></section>')
                parts.append('</div></details>')
            if text(key,'contextual') is not None:
                reviews.append({'case':n['leftCase'],'documentID':key,'originDocumentID':doc['originDocumentID'],
                                'edits':diagnostic.changes(doc['text'],text(key,'contextual')),'context':doc['previousObservations']})
        parts.append('</article>')
    save(output/'document-edits.json',reviews)
    (output/'review.html').write_text('\n'.join(parts))
    lines=['# Boundary comparison', '',
           'Counts below are unmatched words summed across each neighborhood. Zero is the unchanged diagnostic gate, not proof of faithful OCR correction. See `manual-review.json` for inspected repairs and errors.', '',
           '| Case | Intended disposition | Original | Isolated Luna | Contextual Luna |',
           '|---|---|---:|---:|---:|']
    for r in comparisons:
        counts=[]
        for mode in ['original','isolated','contextual']:
            counts.append(str(sum(p['modes'][mode]['unexplainedWords'] for p in r['pairs'])) if r['fullyExplained'][mode] is not None else 'pending')
        label=str(r['leftCase'])+(('–'+str(r['rightCase'])) if r['rightCase'] else '')
        lines.append('| '+label+' | '+r['expectedBoundary']+' | '+' | '.join(counts)+' |')
    (output/'cases.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps(summary,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('directory',type=Path);a=p.parse_args();analyze(a.directory.resolve())

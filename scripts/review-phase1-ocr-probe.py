#!/usr/bin/env python3
"""Audit a bounded OCR model probe and show actual edits against its reference."""
import argparse
import html
import importlib.util
import json
from pathlib import Path
import statistics


def module(path,name):
    spec=importlib.util.spec_from_file_location(name,path)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


a=module(Path(__file__).with_name('analyze-phase1-ocr-edits.py'),'ocr_probe_audit')


def audit_run(pilot, limit=None):
    plan=a.read(pilot/'plan.json')
    for key,name in [('documentsSHA256','documents.json'),('requestsSHA256','requests.json'),('proxySHA256','proxy.json')]:
        assert a.sha(pilot/name)==plan[key]
    for name,h in plan['codeHashes'].items():assert a.sha(pilot/'code'/name)==h
    for path,h in plan.get('comparison',{}).get('artifactSHA256',{}).items():assert a.sha(path)==h
    runner=module(pilot/'code/run-phase1-ocr-correction.py','probe_runner')
    patcher=module(pilot/'code/phase1_ocr_edits.py','probe_patcher')
    docs={d['documentID']:d for d in a.read(pilot/'documents.json')}
    rows=[]
    for index,request in enumerate(a.read(pilot/'requests.json')[:limit]):
        folder=pilot/f'results/{index:04d}'
        if not (folder/'result.json').exists():continue
        r=a.audit_attempt(folder,request,docs[request['documentID']],pilot/'plan.json',runner,patcher)
        rows.append((index,docs[request['documentID']],r))
    return plan,rows


def main(root):
    plan,rows=audit_run(root/'pilot')
    assert plan['models']==['chatgpt/gpt-6-astra'] and plan['reasoning']=='low'
    reference=Path(plan['comparison']['solProbeBaseline'])
    sp,srows=audit_run(reference/'pilot',plan['plannedRequests'])
    assert sp['models']==['chatgpt/gpt-5.6-sol'] and sp['reasoning']=='xhigh'
    assert plan['prompt']==sp['prompt']
    assert a.read(root/'pilot/documents.json')==a.read(reference/'pilot/documents.json')
    assert [r['documentID'] for r in a.read(root/'pilot/requests.json')]==[r['documentID'] for r in a.read(reference/'pilot/requests.json')[:plan['plannedRequests']]]
    sr={r['documentID']:r for _,_,r in srows};ar={r['documentID']:r for _,_,r in rows}
    summary={'complete':len(rows)==plan['plannedRequests'],'plannedRequests':plan['plannedRequests'],
             'savedRequests':len(rows),'planSHA256':a.sha(root/'pilot/plan.json'),
             'referencePlanSHA256':a.sha(reference/'pilot/plan.json'),'analysisSHA256':a.sha(__file__),
             'accuracyCaveat':'Valid edit lists and fewer changes do not prove fidelity; screenshot judgments are separate.'}
    for label,rs in [('sol_xhigh',list(sr.values())),('astra_low',list(ar.values()))]:
        valid=[r for r in rs if r['validCompletion']];times=[r['timing']['dispatchToCompletionSeconds'] for r in valid]
        summary[label]={'validResponses':len(valid),'validEditLists':sum(r.get('editContractValid',False) for r in valid),
                        'appliedEdits':sum(len(json.loads(r['proposedEditsText']).get('edits',[])) for r in valid if r.get('editContractValid')),
                        'medianLatencySeconds':statistics.median(times) if times else None,
                        'meanLatencySeconds':statistics.mean(times) if times else None,
                        'inputTokens':sum(r['usage']['input_tokens'] for r in valid),
                        'outputTokens':sum(r['usage']['output_tokens'] for r in valid),
                        'reasoningTokens':sum(r['usage'].get('output_tokens_details',{}).get('reasoning_tokens',0) for r in valid),
                        'apiEquivalentUncachedUSD':sum(r['apiEquivalentUncachedUSD'] for r in valid)}
    out=root/'analysis';out.mkdir(exist_ok=True)
    manual=a.read(out/'manual-review.json') if (out/'manual-review.json').exists() else {}
    if manual:
        summary['manualReviewSHA256']=a.sha(out/'manual-review.json')
        summary['manualReviewCount']=len(manual.get('reviews',[]))
    page=['<!doctype html><meta charset="utf-8"><title>OCR five-input model review</title>',
          '<style>body{font:16px system-ui;margin:2rem}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f4f4;padding:1rem}.cols{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1rem}article{border-top:2px solid #999;margin:2rem 0}img{max-width:100%}</style>',
          '<h1>Same OCR inputs: Sol xhigh → Astra low</h1><p>Five-observation quality gate, not full-corpus performance. Neither model received screenshots.</p>',
          '<details><summary>Counts, usage and latency</summary><pre>'+html.escape(json.dumps(summary,indent=2))+'</pre></details>']
    for index,doc,s in srows:
        key=doc['documentID'];r=ar.get(key,{})
        page.append(f'<article id="observation-{index+1}"><h2>Observation {index+1}</h2><p>Source candidate {doc["originalReference"]["candidateLine"]}; {html.escape(doc["originalReference"].get("role",""))}</p>')
        notes=[x for x in manual.get('reviews',[]) if x['requestIndex']==index]
        for note in notes:page.append('<p><strong>'+html.escape(note['verdict'])+'</strong>: '+html.escape(note['details'])+'</p>')
        page.append('<div class="cols"><section><h3>Original OCR</h3><pre>'+html.escape(doc['text'])+'</pre></section><section><h3>Astra low</h3><pre>'+html.escape(r.get('correctedText','Pending'))+'</pre></section></div>')
        for title,result in [('Astra low',r),('Sol xhigh',s)]:
            page.append('<details open><summary>'+title+' proposed edits; valid='+str(result.get('editContractValid'))+'</summary><pre>'+html.escape(result.get('proposedEditsText','Pending'))+'</pre>'+html.escape(result.get('editError',''))+'</details>')
        page.append('<details><summary>Sol final text</summary><pre>'+html.escape(s.get('correctedText',''))+'</pre></details>')
        page.append('<details><summary>Earlier reference OCR supplied to both</summary><pre>'+html.escape(json.dumps(doc['previousObservations'],indent=2))+'</pre></details>')
        page.append('<details><summary>Current screenshot—manual audit only</summary>')
        for ref in doc['originalReference'].get('screenshots',[]):page.append('<img loading="lazy" src="'+html.escape(Path(ref['path']).as_uri())+'">')
        page.append('</details></article>')
    a.save(out/'summary.json',summary)
    (out/'review.html').write_text('\n'.join(page))
    print(json.dumps(summary,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('directory',type=Path);args=p.parse_args();main(args.directory.resolve())

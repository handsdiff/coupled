#!/usr/bin/env python3
"""Read-only-source audit and four-way local comparison for OCR edit proposals."""
import argparse
from collections import Counter
import hashlib
import html
import importlib.util
import json
import os
from pathlib import Path
import statistics
import time

from phase1_read_boundary import app_name


def module(path, name):
    spec=importlib.util.spec_from_file_location(name,path)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value


def read(path):return json.loads(Path(path).read_text())


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def save(path,value):path.write_text(json.dumps(value,indent=2,sort_keys=True,ensure_ascii=False)+'\n')


def audit_attempt(folder, request, doc, plan_path, runner, patcher):
    effort=read(plan_path)['reasoning']
    body=runner.payload(request['model'],doc['text'],doc['previousObservations'])
    body['instructions']=patcher.PROMPT;body['reasoning']={'effort':effort}
    expected={'planSHA256':sha(plan_path),'request':request,
              'bodySHA256':hashlib.sha256(runner.canonical(body).encode()).hexdigest()}
    assert read(folder/'inflight.json')==expected and read(folder/'request.json')==body
    timing=read(folder/'transport.json');assert sha(folder/'response.body')==timing['bodySHA256']
    result=read(folder/'result.json')
    if timing['httpStatus']==200:
        try:
            again=runner.interpret((folder/'response.body').read_bytes(),request['model'],effort)
            again.update(patcher.interpret_edits(doc['text'],again['correctedText']))
        except ValueError as error:
            again={'validCompletion':False,'interpretationError':str(error)}
    else:
        again={'validCompletion':False,'httpStatus':timing['httpStatus']}
    assert result==dict(again,**request,timing=timing)
    return result


def prepare_retry(root):
    pilot=root/'pilot';plan=read(pilot/'plan.json');requests=read(pilot/'requests.json')
    docs={d['documentID']:d for d in read(pilot/'documents.json')}
    failures=[];bindings={}
    for i,req in enumerate(requests):
        folder=pilot/f'results/{i:04d}';r=read(folder/'result.json')
        bindings[str(folder/'result.json')]=sha(folder/'result.json')
        if not r['validCompletion']:
            # Known failed stream, not an uncertain unjournaled dispatch or a
            # schema/semantic rejection. One retry after the batch only.
            wire=(folder/'response.body').read_text()
            assert 'TransferEncodingError' in wire and '"code": "500"' in wire
            failures.append(docs[req['documentID']])
    assert failures, 'No eligible server failures'
    runner=module(pilot/'code/run-phase1-ocr-correction.py','retry_preparation')
    kwargs={'edit_model':plan['models'][0]} if plan['reasoning']=='xhigh' else {}
    runner.prepare(root/'retry-1',Path(plan['project']),failures,
                   'One replacement for confirmed incomplete server-error streams; original attempts retained',edits=True,**kwargs)
    new=read(root/'retry-1/plan.json')
    new['retryParentPlanSHA256']=sha(pilot/'plan.json')
    new['parentResultSHA256']=bindings
    runner.atomic(root/'retry-1/plan.json',new)
    print(json.dumps({'retryRequests':len(failures),'retryPlanSHA256':sha(root/'retry-1/plan.json')}))


def analyze(root):
    pilot=root/'pilot';plan=read(pilot/'plan.json');baseline=Path(plan['comparison']['contextualBaseline'])
    isolated=Path(plan['comparison']['isolatedBaseline'])
    sol=plan['models']==['chatgpt/gpt-5.6-sol'] and plan['reasoning']=='xhigh'
    assert sol or (plan['models']==['chatgpt/gpt-5.6-luna'] and plan['reasoning']=='low')
    current_mode='sol_xhigh' if sol else 'edits_low'
    current_label='Sol edit-only, xhigh reasoning' if sol else 'Luna edit-only, low reasoning'
    for path,h in plan['comparison']['artifactSHA256'].items():assert sha(path)==h
    for name,h in plan['codeHashes'].items():assert sha(pilot/'code'/name)==h
    assert sha(pilot/'documents.json')==plan['documentsSHA256']
    assert sha(pilot/'requests.json')==plan['requestsSHA256']
    runner=module(pilot/'code/run-phase1-ocr-correction.py','edit_audit_runner')
    patcher=module(pilot/'code/phase1_ocr_edits.py','edit_audit_patcher')
    diagnostic=module(Path(__file__).with_name('analyze-phase1-ocr-correction.py'),'edit_audit_diagnostic')
    docs={d['documentID']:d for d in read(pilot/'documents.json')}
    assert read(pilot/'documents.json')==read(baseline/'inputs/all-observations.json')
    neighborhoods=read(baseline/'inputs/neighborhoods.local.json')
    old_results={r['documentID']:r for p in sorted((isolated/'pilot/results').glob('*/result.json')) if (r:=read(p))['model']=='chatgpt/gpt-5.6-luna'}
    contextual_results={r['documentID']:r for p in sorted((baseline/'pilot/results').glob('*/result.json')) if (r:=read(p))}
    old_boundaries={n['candidateLine']:n for n in read(baseline/'analysis/boundary-review.json')}
    low_results={};low_boundaries={}
    if sol:
        low=Path(plan['comparison']['editsBaseline'])
        assert read(low/'pilot/documents.json')==read(pilot/'documents.json')
        for folder in [low/'pilot/results',low/'retry-1/results']:
            for f in sorted(folder.glob('*/result.json')):
                r=read(f)
                if r['validCompletion']:low_results[r['documentID']]=r
        assert len(low_results)==len(docs)
        low_boundaries={n['candidateLine']:n for n in read(low/'analysis/boundary-review.json')}
    results={};schedule=read(pilot/'requests.json')
    for i,request in enumerate(schedule):
        folder=pilot/f'results/{i:04d}'
        if not (folder/'result.json').exists():continue
        doc=docs[request['documentID']]
        result=audit_attempt(folder,request,doc,pilot/'plan.json',runner,patcher)
        results[doc['documentID']]=result
    initial_failed=sum(not r['validCompletion'] for r in results.values())
    retry_results=[]
    if (root/'retry-1/plan.json').exists():
        retry=root/'retry-1';rp=read(retry/'plan.json')
        assert rp['retryParentPlanSHA256']==sha(pilot/'plan.json')
        assert rp['codeHashes']==plan['codeHashes']
        for name,h in rp['codeHashes'].items():assert sha(retry/'code'/name)==h
        for path,h in rp['parentResultSHA256'].items():assert sha(path)==h
        assert sha(retry/'documents.json')==rp['documentsSHA256']
        assert sha(retry/'requests.json')==rp['requestsSHA256']
        rd={d['documentID']:d for d in read(retry/'documents.json')}
        for i,request in enumerate(read(retry/'requests.json')):
            key=request['documentID'];assert rd[key]==docs[key] and not results[key]['validCompletion']
            folder=retry/f'results/{i:04d}'
            if not (folder/'result.json').exists():continue
            result=audit_attempt(folder,request,docs[key],retry/'plan.json',runner,patcher)
            retry_results.append(result)
            if result['validCompletion']:results[key]=result
    def text(key,mode):
        if key is None:return ''
        doc=docs[key]
        if mode=='original':return doc['text']
        if mode=='isolated':return old_results[doc['originDocumentID']]['correctedText']
        if mode=='contextual':return contextual_results[key]['correctedText'] if key in contextual_results else old_results[doc['originDocumentID']]['correctedText']
        if mode=='edits_low' and sol:return low_results[key]['correctedText']
        r=results.get(key)
        return r['correctedText'] if r and r['validCompletion'] else None
    modes=['original','isolated','contextual','edits_low']+(['sol_xhigh'] if sol else []);comparisons=[]
    for n in neighborhoods:
        row={k:n[k] for k in ['candidateLine','leftCase','rightCase','expectedBoundary']};row['pairs']=[]
        for p in n['pairs']:
            k,pkey=p['contextualCurrentID'],p['contextualPriorID'];d=docs[k]
            draft=n['draftForLocalAuditOnly'] if app_name(d['originalReference']['application'])==app_name(n['application']) else ''
            field=n['initialFieldForLocalAuditOnly'] if draft else ''
            pair={'currentID':k,'priorID':pkey,'modes':{}}
            previous=next(p for p in old_boundaries[n['candidateLine']]['pairs'] if p['currentID']==k and p['priorID']==pkey)
            for mode in modes:
                a,b=text(k,mode),text(pkey,mode)
                pair['modes'][mode]=diagnostic.coverage(a,b,draft,field) if a is not None and b is not None else None
                if mode in previous['modes']:assert pair['modes'][mode]==previous['modes'][mode]
                elif mode=='edits_low' and sol:
                    lp=next(p for p in low_boundaries[n['candidateLine']]['pairs'] if p['currentID']==k and p['priorID']==pkey)
                    assert pair['modes'][mode]==lp['modes'][mode]
            pair['rejectedProposalDocuments']=[x for x in [k,pkey] if x in results and results[x].get('editContractValid') is False]
            row['pairs'].append(pair)
        row['fullyExplained']={m:all(p['modes'][m]['fullyExplained'] for p in row['pairs']) if all(p['modes'][m] is not None for p in row['pairs']) else None for m in modes}
        comparisons.append(row)
    repairs=[r for r in comparisons if r['expectedBoundary']=='repair_non_novel']
    controls=[r for r in comparisons if r['expectedBoundary']=='retain_new_information']
    # Separate sensitivity check, never replace the frozen experiment outputs:
    # unchanged entries can be ignored without asking the model for a new answer.
    noop_overrides={};noop_review=[]
    for key,r in results.items():
        if r.get('editError')!='after must change the span':continue
        proposal=json.loads(r['proposedEditsText'])
        proposal['edits']=[e for e in proposal['edits'] if e['before']!=e['after']]
        alternative=patcher.interpret_edits(docs[key]['text'],json.dumps(proposal))
        noop_review.append({'documentID':key,'validAfterDroppingNoops':alternative['editContractValid'],
                            'remainingError':alternative.get('editError')})
        if alternative['editContractValid']:noop_overrides[key]=alternative['correctedText']
    noop_full=0
    for n in neighborhoods:
        if n['expectedBoundary']!='repair_non_novel':continue
        statuses=[]
        for p in n['pairs']:
            k,pkey=p['contextualCurrentID'],p['contextualPriorID'];d=docs[k]
            a=noop_overrides.get(k,text(k,current_mode));b=noop_overrides.get(pkey,text(pkey,current_mode))
            draft=n['draftForLocalAuditOnly'] if app_name(d['originalReference']['application'])==app_name(n['application']) else ''
            field=n['initialFieldForLocalAuditOnly'] if draft else ''
            statuses.append(a is not None and b is not None and diagnostic.coverage(a,b,draft,field)['fullyExplained'])
        noop_full+=all(statuses)
    valid=[r for r in results.values() if r['validCompletion']]
    patches=[r for r in valid if r['editContractValid']]
    times=[r['timing']['dispatchToCompletionSeconds'] for r in valid]
    summary={
        'complete':len(results)==len(schedule),'plannedRequests':len(schedule),'savedRequests':len(results),
        'initialFailedResponses':initial_failed,'retryResponses':len(retry_results),
        'failedAttemptUsage':'Unavailable from incomplete server streams; not counted as zero' if initial_failed else 'No failures',
        'validResponses':len(valid),'validEditLists':len(patches),'rejectedEditLists':len(valid)-len(patches),
        'rejectedEditReasons':dict(Counter(r['editError'] for r in valid if not r['editContractValid'])),
        'ignoreNoopSensitivity':{'fullyExplainedIntendedMerges':noop_full,'review':noop_review,
                                'policy':'Diagnostic only; frozen results and primary counts unchanged'},
        'unchangedResponses':sum(not r['edits'] for r in patches),'appliedEdits':sum(len(r['edits']) for r in patches),
        'completedNeighborhoods':sum(r['fullyExplained'][current_mode] is not None for r in comparisons),
        'fullyExplainedIntendedMerges':{m:sum(r['fullyExplained'][m] is True for r in repairs) for m in modes},
        'retainedNoveltyControls':{m:sum(r['fullyExplained'][m] is False for r in controls) for m in modes},
        'newlyExplainedVsContextual':[[r['leftCase'],r['rightCase']] for r in repairs if r['fullyExplained']['contextual'] is False and r['fullyExplained'][current_mode] is True],
        'lostVsContextual':[[r['leftCase'],r['rightCase']] for r in repairs if r['fullyExplained']['contextual'] is True and r['fullyExplained'][current_mode] is False],
        'medianLatencySeconds':statistics.median(times) if times else None,
        'meanLatencySeconds':statistics.mean(times) if times else None,
        'inputTokens':sum(r['usage']['input_tokens'] for r in valid),
        'outputTokens':sum(r['usage']['output_tokens'] for r in valid),
        'reasoningTokens':sum(r['usage'].get('output_tokens_details',{}).get('reasoning_tokens',0) for r in valid),
        'apiEquivalentUncachedUSD':sum(r['apiEquivalentUncachedUSD'] for r in valid),
        'planSHA256':sha(pilot/'plan.json'),'analysisSHA256':sha(__file__),
        'matchingCodeSHA256':sha(Path(__file__).with_name('phase1_read_boundary.py')),
        'rejectionPolicy':'Malformed or overlapping edit lists apply no edits; original OCR retained. Rejections are not successful corrections.',
        'caveat':('Changes model AND reasoning effort versus Luna low, preserving the exact edit contract. ' if sol else 'Changes output format AND reasoning effort. ')+'Boundary-match counts are not character accuracy or proof of faithful repair. Not production pipeline replay.'}
    for mode in modes[:-1]:
        deltas=[(sum(p['modes'][mode]['unexplainedWords'] for p in r['pairs']),sum(p['modes'][current_mode]['unexplainedWords'] for p in r['pairs'])) for r in repairs if r['fullyExplained'][current_mode] is not None]
        summary['unmatchedWordChangeVs'+mode.capitalize()]={'completed':len(deltas),'fewer':sum(b<a for a,b in deltas),'same':sum(b==a for a,b in deltas),'more':sum(b>a for a,b in deltas)}
    if sol:
        summary['newlyExplainedVsLunaLow']=[[r['leftCase'],r['rightCase']] for r in repairs if r['fullyExplained']['edits_low'] is False and r['fullyExplained'][current_mode] is True]
        summary['lostVsLunaLow']=[[r['leftCase'],r['rightCase']] for r in repairs if r['fullyExplained']['edits_low'] is True and r['fullyExplained'][current_mode] is False]
    out=root/'analysis';out.mkdir(exist_ok=True)
    save(out/'summary.json',summary);save(out/'boundary-review.json',comparisons)
    proposals=[dict(documentID=k,reference=docs[k]['originalReference'],**{field:r.get(field) for field in ['editContractValid','editError','locatedEdits','proposedEditsText']}) for k,r in results.items()]
    save(out/'edit-proposals.json',proposals)
    manual=read(out/'manual-review.json').get('reviews',[]) if (out/'manual-review.json').exists() else []
    page=['<!doctype html><meta charset="utf-8"><title>'+current_label+'</title>',
          '<style>body{font:16px system-ui;margin:2rem}.cols{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1rem}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f3f3;padding:1rem}article{border-top:2px solid #aaa;margin:2rem 0}summary{cursor:pointer}img{max-width:100%}</style>',
          '<h1>Original OCR → '+current_label+'</h1><p>Same 36 problem cases plus three novelty controls. Exact edits only; prior outputs retained for comparison. Matching is not proof of fidelity.</p>',
          '<details><summary>Summary and limitations</summary><pre>'+html.escape(json.dumps(summary,indent=2))+'</pre></details>']
    ledger=['# Boundary diagnostic','','Unmatched words; zero is the matching gate, not character-accuracy ground truth. Invalid proposals retain original OCR.','','| Case | Original | Isolated full text | Contextual full text | Luna edits + low |'+(' Sol edits + xhigh |' if sol else ''),'|---|---:|---:|---:|---:|'+('---:|' if sol else '')]
    for n,r in zip(neighborhoods,comparisons):
        label=str(n['leftCase'])+('–'+str(n['rightCase']) if n['rightCase'] else '')
        counts=[str(sum(p['modes'][m]['unexplainedWords'] for p in r['pairs'])) if r['fullyExplained'][m] is not None else 'pending' for m in modes]
        ledger.append('| '+label+' | '+' | '.join(counts)+' |')
        page.append(f'<article id="case-{n["leftCase"]}"><h2>Case {label}</h2><p>Fully explained: {html.escape(str(r["fullyExplained"]))}</p>')
        for review in manual:
            if review['case']==n['leftCase']:page.append('<p><strong>'+html.escape(review['verdict'])+'</strong> — '+html.escape(review['details'])+'</p>')
        for pair in n['pairs']:
            k=pair['contextualCurrentID'];d=docs[k]
            page.append('<div class="cols">')
            for m,label2 in [('original','Original OCR'),(current_mode,current_label)]:
                value=text(k,m);page.append('<section><h3>'+label2+'</h3><pre>'+html.escape(value if value is not None else 'Pending')+'</pre></section>')
            page.append('</div><details open><summary>Exact proposed edits</summary><pre>'+html.escape(results.get(k,{}).get('proposedEditsText','Pending'))+'</pre></details>')
            if sol:page.append('<details><summary>Luna low edit-only answer</summary><pre>'+html.escape(text(k,'edits_low'))+'</pre></details>')
            if results.get(k,{}).get('editContractValid') is False:page.append('<p><strong>Rejected: original retained.</strong> '+html.escape(results[k]['editError'])+'</p>')
            page.append('<details><summary>Previous contextual full-text answer</summary><pre>'+html.escape(text(k,'contextual'))+'</pre></details>')
            page.append('<details><summary>Earlier reference observations supplied</summary><pre>'+html.escape(json.dumps(d['previousObservations'],indent=2,ensure_ascii=False))+'</pre></details>')
            page.append('<details><summary>Current raw screenshot</summary>')
            for ref in d['originalReference'].get('screenshots',[]):page.append('<img loading="lazy" src="'+html.escape(Path(ref['path']).as_uri())+'">')
            page.append('</details>')
        page.append('</article>')
    (out/'review.html').write_text('\n'.join(page));(out/'cases.md').write_text('\n'.join(ledger)+'\n')
    print(json.dumps(summary,indent=2))


def watch(root, runner_pid):
    """Read-only monitoring; never launches or retries provider requests."""
    plan=root/'pilot/plan.json';total=read(plan)['plannedRequests']
    status=root/'analysis/monitor.json';status.parent.mkdir(exist_ok=True)
    save(status,{'status':'waiting','runnerPID':runner_pid,'planSHA256':sha(plan),
                 'pollSeconds':30,'providerCalls':False})
    while True:
        count=sum(1 for _ in (root/'pilot/results').glob('*/result.json'))
        try:os.kill(runner_pid,0);running=True
        except ProcessLookupError:running=False
        if count==total or not running:
            analyze(root)
            summary=read(root/'analysis/summary.json')
            save(status,{'status':'completed' if summary['validResponses']==total else 'needs_review',
                         'runnerPID':runner_pid,'planSHA256':sha(plan),'savedRequests':count,
                         'validResponses':summary['validResponses'],'providerCalls':False,
                         'summarySHA256':sha(root/'analysis/summary.json')})
            return
        time.sleep(30)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('directory',type=Path)
    mode=p.add_mutually_exclusive_group()
    mode.add_argument('--prepare-retry',action='store_true')
    mode.add_argument('--watch-runner-pid',type=int);a=p.parse_args()
    if a.watch_runner_pid:
        assert a.watch_runner_pid>1
        watch(a.directory.resolve(),a.watch_runner_pid)
    else:(prepare_retry if a.prepare_retry else analyze)(a.directory.resolve())

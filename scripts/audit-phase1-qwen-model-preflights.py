#!/usr/bin/env python3
"""Audit completed, response-bound model tests without provider calls."""
import argparse
from collections import Counter
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import statistics


def sha(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def fp(x):
    return hashlib.sha256(json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def audit(directory):
    p=directory;plan=json.loads((p/'execution.json').read_text());run=json.loads((p/'run.json').read_text())
    assert run['status']=='complete_pending_review' and not run['mainRunStarted']
    assert sha(p/'execution.json')==run['executionSHA256']
    for path,digest in {**plan['runtime']['filesSHA256'],**plan['priorAttempts']['filesSHA256']}.items():
        assert sha(Path(path))==digest, 'Bound runtime/prior attempt changed: '+path
    prepared=Path(plan['preparedDirectory']);assert sha(prepared/'preparation.json')==plan['preparedSHA256']
    prep=json.loads((prepared/'preparation.json').read_text())
    for path,digest in {**prep['codeSHA256'],**prep['sourceBindingsSHA256']}.items():assert sha(Path(path))==digest
    ref_path=next(Path(v) for v in prep['sourceBindingsSHA256'] if v.endswith('/preparation.json'))
    ref=json.loads(ref_path.read_text());cases={v['exampleID']:v for v in ref['additionalTests']['cases']}
    assert len(cases)==20
    records=[json.loads(l) for l in (p/'operations.jsonl').open()];begins={};results={};reuse={}
    total=plan['priorAttempts']['reservedTokenCostUSD']
    for r in records:
        key=r.get('key')
        if r['kind']=='operation_begin':
            assert key not in begins
            begins[key]=r;total+=r['maximumUSD'];assert total+.5<=20.+1e-9
        elif r['kind']=='operation_result':
            assert key in begins and key not in results and r['operation']==begins[key]['operation']
            results[key]=r
        elif r['kind']=='reused_prior_result':
            assert key not in reuse and key not in begins
            source=Path(r['source']);assert str(source/'operations.jsonl') in plan['priorAttempts']['filesSHA256']
            found=[x for x in map(json.loads,(source/'operations.jsonl').open()) if x.get('key')==key and x['kind']=='operation_result']
            assert len(found)==1 and fp(found[0])==r['sourceResultSHA256']
            reuse[key]=found[0]
    assert begins.keys()==results.keys() and len(reuse)==10
    assert math.isclose(total,run['totalAuthorizationReservedTokenCostUSD'],abs_tol=1e-8)
    assert math.isclose(total-plan['priorAttempts']['reservedTokenCostUSD'],run['reservedTokenCostUSD'],abs_tol=1e-8)
    all_results={**results,**reuse}
    for r in results.values():
        v=r['value']
        if r['operation'] in ('nll','train'):
            assert len(v['targetLogprobs'])==v['lossBearingTokens']
            assert all(math.isfinite(x) and x<=0 for x in v['targetLogprobs'])
            assert math.isclose(-sum(v['targetLogprobs']),v['weightedNLLSum'],abs_tol=1e-7)
            if r['operation']=='train':
                assert math.isclose(v['forwardMetrics']['loss:sum'],v['weightedNLLSum'],rel_tol=2e-5,abs_tol=1e-4)
                if v.get('optimizerUpdatePerformed') is not False:
                    assert all(math.isfinite(x) for x in v['optimizerMetrics'].values())
    summary={}
    for model,s in run['models'].items():
        assert s['tokenizerVerified'] and s['status']=='complete_pending_review'
        cap=s['capability']['scores'];assert set(cap)==set(cases)
        generations=[]
        for eid,answers in cap.items():
            assert [a['seed'] for a in answers]==[17,18,19,20]
            for a in answers:
                v=results[f"{model}/capability/{eid}/seed-{a['seed']}"]['value']
                assert a=={'seed':a['seed'],**v}
                assert a['outputTokens']==len(a['predictionTokenIDs'])
                assert a['predictionTokenIDs']==a['rawProviderResponse']['sequences'][0]['tokens']
                if model.endswith('_low'):
                    assert a['reasoningTokenCount']+a['answerAndTerminationTokenCount']==a['outputTokens']
                    assert a['reasoningClosed'] or a['prediction']==''
                generations.append(a)
        vals=[v for k,v in all_results.items() if k.startswith(model+'/')]
        counts=Counter(v['operation'] for v in vals)
        row={'operationCountsIncludingReused':dict(counts),'frozenGenerations':len(generations),
             'unfinishedReasoning':sum(a.get('reasoningClosed') is False for a in generations),
             'lengthLimited':sum(a['stopReason']=='length' for a in generations),
             'frozenLatency':{'medianSeconds':statistics.median(a['latencySeconds'] for a in generations),
                              'meanSeconds':statistics.mean(a['latencySeconds'] for a in generations)},
             'frozenInputTokens':sum(a['inputTokens'] for a in generations),
             'frozenOutputTokens':sum(a['outputTokens'] for a in generations),
             'maximumNewDispatchTokenCostUSD':sum(v['maximumUSD'] for k,v in begins.items() if k.startswith(model+'/'))}
        if 'overfit' in s:
            o=s['overfit'];epochs=o['epochsCompleted'];evaluations=len(o['snapshots'])
            assert counts['generation']==100+10*evaluations and counts['nll']==38+10*evaluations
            assert counts['train']==14+10*epochs  # Includes six no-update forward controls.
            assert sum(v['value'].get('optimizerUpdatePerformed') is False for v in vals)==6
            last=o['snapshots'][-1];target={e:c['targetText'] for e,c in cases.items()}
            exact=0
            for eid,v in last['scores'].items():
                g=results[f"{model}/overfit/eval-{last['epoch']}/{eid}/generation"]['value']
                assert g==v['generation']
                assert v['exactMatch']==(g['prediction']==target[eid])
                assert v['normalizedExactMatch']==(' '.join(g['prediction'].split())==' '.join(target[eid].split()))
                exact+=v['normalizedExactMatch']
            assert exact==last['normalizedExactMatches']
            row['overfit']={k:last[k] for k in ('epoch','baselineMeanNLL','meanNLL','normalizedExactMatches','memorizationGatePassed')}
            row['epochTrainingNLL']=[{'epoch':v['epoch'],'trainingNLL':v['trainingNLL']} for v in records
                                    if v['kind']=='overfit_progress' and v['modelKey']==model and 'epoch' in v]
            row['restorationDiagnostics']={arm:s['originalChecks'][arm]['restorationDiagnostic'] for arm in ('old','new')}
            for diagnostic in row['restorationDiagnostics'].values():
                assert diagnostic['weightReload']['maxTokenLogprobDelta']<=.005
                assert diagnostic['mainRunGatePassed']==diagnostic['exactOptimizerContinuationVerified']
        else:assert counts['generation']==80 and counts['train']==0 and counts['nll']==0
        summary[model]=row
    grade_path=p/'capability-review.json'
    if grade_path.exists():
        grades=json.loads(grade_path.read_text())
        for key,entries in grades['models'].items():
            assert set(e['exampleID'] for e in entries)==set(cases) and len(entries)==20
            good=0;any_count=0;all_count=0
            for e in entries:
                answers=run['models'][key]['capability']['scores'][e['exampleID']]
                assert e['cohortNumber']==cases[e['exampleID']]['cohortNumber']
                assert len(e['passBySeed'])==4 and all(type(v) is bool for v in e['passBySeed'])
                assert e['predictionsSHA256']==[hashlib.sha256(a['prediction'].encode()).hexdigest() for a in answers]
                assert e['reason']
                good+=sum(e['passBySeed']);any_count+=any(e['passBySeed']);all_count+=all(e['passBySeed'])
            summary[key]['intentGrades']={'passingAnswers':good,'answers':80,'anyOfFourCases':any_count,'allOfFourCases':all_count,
                                          'status':'implementer judgment; not independent or random-corpus estimate'}
    return {'status':'audit_passed_with_explicit_model_diagnostics_NOT_main_run_authorization',
            'models':summary,'pendingOperations':0,'reusedPriorBaselineCalls':len(reuse),
            'totalAdditionalAuthorizationTokenBoundUSD':total,'storageReserveUSD':.5,'actualInvoiceCostUSD':None,
            'priorAttemptsTokenBoundUSD':plan['priorAttempts']['reservedTokenCostUSD'],
            'wallMinutes':(dt.datetime.fromisoformat(run['endedAt'])-dt.datetime.fromisoformat(run['startedAt'])).total_seconds()/60,
            'lineageSHA256':{n:sha(p/n) for n in ('execution.json','run.json','operations.jsonl','reasoning-low-rows.jsonl')},
            'gradeFileSHA256':sha(grade_path) if grade_path.exists() else None,'auditCodeSHA256':sha(Path(__file__))}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('directory',type=Path);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();result=audit(a.directory)
    with a.output.open('x') as f:json.dump(result,f,indent=2,sort_keys=True);f.write('\n')
    print(json.dumps(result,indent=2))

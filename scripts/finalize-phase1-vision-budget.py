#!/usr/bin/env python3
"""Assemble 138 text results + 69 unchanged screenshot results, offline only."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
from phase1_read_model_comparison import canonical,file_hash,rows

def load(p):return json.loads(p.read_text())
def save(p,value):
    with p.open('x') as f:os.chmod(p,0o600);f.write(json.dumps(value,indent=2,sort_keys=True)+'\n')
def save_rows(p,values):
    with p.open('x') as f:
        os.chmod(p,0o600)
        for v in values:f.write(canonical(v)+'\n')

def finalize(root,out):
    assert not out.exists()
    spec=importlib.util.spec_from_file_location('frozen_vision_budget',root/'code/run-phase1-vision-pilot.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    plan=m.verify(root);assert plan['planned']==138
    requests=list(rows(root/'requests.jsonl'))
    found,audit=m.audit(root,requests,file_hash(root/'plan.json'),plan['pricesUSDPerMillion'])
    assert audit['status']=='complete' and found==list(rows(root/'predictions.jsonl'))
    data_plan=load(root/'data/plan.json');pilot=Path(data_plan['sourcePilot'])
    assert file_hash(pilot/'predictions.jsonl')==data_plan['sourcePredictionsSHA256']
    assert file_hash(pilot/'plan.json')==data_plan['sourcePlanSHA256']
    old_plan=load(pilot/'plan.json')
    baseline=list(rows(root/'data/baseline-screenshots.jsonl'))
    originals={(r['case'],r['variant']):r for r in rows(pilot/'predictions.jsonl')}
    baseline_requests=[r for r in rows(pilot/'requests.jsonl') if r['variant']=='screenshots_recent']
    verified,old_audit=m.audit(pilot,baseline_requests,file_hash(pilot/'plan.json'),old_plan['pricesUSDPerMillion'])
    assert old_audit['status']=='complete' and len(verified)==69
    for row in baseline:assert row['prediction']==originals[row['case'],'screenshots_recent']
    all_results=found+[{**r['prediction'],'baselineReuse':{'sourceRun':str(pilot),'sourcePredictionsSHA256':data_plan['sourcePredictionsSHA256'],'providerCallsForReuse':0}} for r in baseline]
    assert len(all_results)==len({r['responseID'] for r in all_results})==len({r['sampleID'] for r in all_results})==207
    out.mkdir(mode=0o700);(out/'data').mkdir()
    for name in ('cases.jsonl','plan.json'):shutil.copyfile(root/'data'/name,out/'data'/name)
    prompts=list(rows(root/'data/prompts.local.jsonl'))+list(rows(root/'data/baseline-screenshot-prompts.jsonl'))
    save_rows(out/'data/prompts.local.jsonl',prompts)
    frames={f['recordID']:f for f in rows(pilot/'data/frames.jsonl')}
    frames.update({f['recordID']:f for f in rows(root/'data/frames.jsonl')})
    save_rows(out/'data/frames.jsonl',frames.values())
    save_rows(out/'predictions.jsonl',sorted(all_results,key=lambda r:(r['case'],r['variant'])))
    scored=pilot/'review-v1/scored-v2'
    done=load(scored/'completion.json')
    assert done['predictionsSHA256']==file_hash(pilot/'predictions.jsonl')
    for n,h in done['artifactsSHA256'].items():assert file_hash(scored/n)==h
    save_rows(out/'baseline-grades.jsonl',[r for r in rows(scored/'judgments.jsonl') if r['variant']=='screenshots_recent'])
    save(out/'lineage.json',{'textRun':str(root),'textPlanSHA256':file_hash(root/'plan.json'),
        'textPredictionsSHA256':file_hash(root/'predictions.jsonl'),'baselineRun':str(pilot),
        'baselinePredictionsSHA256':file_hash(pilot/'predictions.jsonl'),'baselineScoringCompletionSHA256':file_hash(scored/'completion.json'),
        'completedNewTextCalls':sum(not r.get('reusedFrom') for r in found),'reusedCleanedAnswers':sum(bool(r.get('reusedFrom')) for r in found),
        'reusedScreenshotAnswers':69,'newScreenshotCalls':0,'providerCallsDuringAssembly':0})
    save(out/'plan.json',{'version':'phase1-vision-budget-comparison-v2','scope':data_plan['hypothesisScope'],
        'scoringContract':data_plan['scoringContract'],'planned':207,'providerCallsDuringAssembly':0,
        'artifactsSHA256':{str(p.relative_to(out)):file_hash(p) for p in out.rglob('*') if p.is_file()}})
    save(out/'audit.json',{'status':'complete','completed':207,'planned':207,'predictionsSHA256':file_hash(out/'predictions.jsonl'),
        'logicalComparisonRowsNotNewCalls':True,'rawTransportLineageVerified':True,'providerCallsDuringAudit':0})
    print(canonical({'status':'comparison_ready','path':str(out),'comparisonRows':207,'newScreenshotCalls':0}))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run',required=True,type=Path);p.add_argument('--output',required=True,type=Path);a=p.parse_args();finalize(a.run.resolve(),a.output.resolve())

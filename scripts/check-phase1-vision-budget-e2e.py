#!/usr/bin/env python3
"""No-network corrected-run save/resume, baseline assembly, and grading gates."""
import argparse
import importlib.util
import json
from pathlib import Path
import resource
import shutil
import tempfile
from unittest.mock import patch

def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run',required=True,type=Path);p.add_argument('--report',required=True,type=Path);a=p.parse_args()
    calls=0
    with tempfile.TemporaryDirectory(prefix='coupled-budget-e2e-') as tmp, \
        patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('Network forbidden')), \
        patch('subprocess.Popen',side_effect=AssertionError('Provider process forbidden')):
        root=(Path(tmp)/'run').resolve();shutil.copytree(a.run,root)
        m=module('budget_e2e_frozen',root/'code/run-phase1-vision-pilot.py');plan=m.verify(root)
        requests=list(m.rows(root/'requests.jsonl'));prompts={p['partsSHA256']:p for p in m.rows(root/'data/prompts.local.jsonl')}
        def sender(body,attempt,timeout):
            nonlocal calls
            calls+=1
            response={'id':f'synthetic-budget-{calls}','model':m.MODEL,'reasoning':{'effort':'xhigh'},'status':'completed',
                'output':[{'type':'message','content':[{'type':'output_text','text':'Synthetic fixture, not a human prediction'}]}],
                'usage':{'input_tokens':100,'output_tokens':10,'total_tokens':110,'input_tokens_details':{'cached_tokens':0}}}
            with (attempt/'response.body').open('xb') as f:f.write(('data: '+json.dumps({'type':'response.completed','response':response})+'\n\n').encode())
            m.ex.atomic_json(attempt/'transport.json',{'httpStatus':200,'bodySHA256':m.file_hash(attempt/'response.body'),'dispatchToCompletionSeconds':1})
        results=m.run_requests(requests,prompts,root,m.file_hash(root/'plan.json'),sender=sender)
        assert len(results)==138 and calls==plan['newProviderRequests']
        def forbidden(*args,**kwargs):raise AssertionError('Resume resent a request')
        assert m.run_requests(requests,prompts,root,m.file_hash(root/'plan.json'),sender=forbidden)==results
        actual,audit=m.audit(root,requests,m.file_hash(root/'plan.json'),plan['pricesUSDPerMillion'])
        assert actual==results and audit['status']=='complete'
        m.save_rows(root/'predictions.jsonl',results)
        m.ex.atomic_json(root/'audit.json',{**audit,'predictionsSHA256':m.file_hash(root/'predictions.jsonl')})
        f=module('budget_e2e_finalize',Path(__file__).with_name('finalize-phase1-vision-budget.py'))
        f.finalize(root,root/'comparison')
        analyzer=module('budget_e2e_analysis',Path(__file__).with_name('analyze-phase1-vision.py'))
        assert len(analyzer.verify_run(root/'comparison'))==207
        analyzer.prepare(root/'comparison',root/'review')
        assert len(list(m.rows(root/'review/fixed-baseline-grades.jsonl')))==69
    report={'status':'passed','networkCalls':0,'mockDispatches':calls,'textResults':138,'baselineScreenshotReuse':69,
        'comparisonRows':207,'saveResumeNoResend':True,'baselineRawTransportVerified':True,'baselineGradesPinned':69,
        'peakProcessMiB':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024**2}
    assert report['peakProcessMiB']<1024
    with a.report.open('x') as out:out.write(json.dumps(report,indent=2,sort_keys=True)+'\n')
    print(json.dumps(report))

if __name__=='__main__':main()

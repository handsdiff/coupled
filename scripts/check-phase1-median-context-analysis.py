#!/usr/bin/env python3
"""No-network regression for five-condition coverage and blinded scoring."""
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory

spec = importlib.util.spec_from_file_location('analysis', Path(__file__).with_name('analyze-phase1-median-context.py'))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)


def rejected(f):
    try: f()
    except (AssertionError, KeyError): return
    raise AssertionError('Invalid scoring artifact accepted')


def check():
    with TemporaryDirectory(prefix='coupled-median-analysis-') as td:
        root=Path(td); (root/'data').mkdir(); (root/'code').mkdir()
        requests=[];predictions=[];cases=[]
        for case in range(1,70):
            for arm in m.ARMS:
                ident=f'{case}-{arm}'; limit=['capacity'] if case==41 and arm=='time_images' else []
                q={'sampleID':ident,'case':case,'arm':arm,'requestOrdinal':len(requests)+1,
                   'partsSHA256':ident,'exampleID':str(case),'limits':limit}
                requests.append(q)
                cases.append({'case':case,'arm':arm,'query':'query','target':'target','exampleID':str(case),'limits':limit,'historySpanMinutes':1})
                if limit:continue
                r={**q,'prediction':'target','validCompletion':True}
                folder=root/'results'/f"{q['requestOrdinal']:04d}";folder.mkdir(parents=True)
                m.save(folder/'result.json',r);predictions.append(r)
        m.save_rows(root/'requests.jsonl',requests);m.save_rows(root/'predictions.jsonl',predictions)
        m.save(root/'data/cases.json',cases)
        m.save(root/'plan.json',{'version':'phase1-median-context-executor-v2','artifactsSHA256':{},'codeSHA256':{}})
        audit={'status':'complete','planSHA256':m.file_hash(root/'plan.json'),'predictionsSHA256':m.file_hash(root/'predictions.jsonl'),
               'completed':344,'capacityBlocked':[{'case':41,'arm':'time_images'}]}
        m.save(root/'audit.json',audit)
        assert len(m.verify_run(root)[0])==344
        blind={1:{'validCompletion':True},2:{'validCompletion':False}}
        m.save_rows(root/'grades.jsonl',[{'blindIndex':1,'decision':'pass','reason':'intent matches'},
                                       {'blindIndex':2,'decision':'fail','reason':'invalid'}])
        assert len(m.annotations(root/'grades.jsonl',blind))==2
        m.save_rows(root/'bad.jsonl',[{'blindIndex':1,'decision':'pass','reason':'x'},
                                    {'blindIndex':2,'decision':'pass','reason':'x'}])
        rejected(lambda:m.annotations(root/'bad.jsonl',blind))
        m.save_rows(root/'missing.jsonl',[{'blindIndex':1,'decision':'pass','reason':'x'}])
        rejected(lambda:m.annotations(root/'missing.jsonl',blind))
        left={1:{'semanticPass':False},2:{'semanticPass':True},41:{'semanticPass':True}}
        right={1:{'semanticPass':True},2:{'semanticPass':False}}
        pair=m.pair(left,right)
        assert pair['n']==2 and pair['gains']==[1] and pair['losses']==[2] and pair['netPassDifference']==0
        # Missing image condition must never become a failed prediction or enter the paired denominator.
        assert all(41 not in pair[n] for n in ('gains','losses','bothPass','bothFail'))
        with (root/'predictions.jsonl').open('a') as f:f.write('{}\n')
        rejected(lambda:m.verify_run(root))
    print(json.dumps({'status':'passed','providerCalls':0,'checks':['344-row/5-condition coverage',
        'capacity remains unscored','missing/invalid annotations rejected','paired common denominators',
        'gains/losses direction','prediction tampering rejected']}))


if __name__=='__main__':check()

#!/usr/bin/env python3
"""Local HTTP checks for the read-only prepared-input review, not inference."""
import argparse
import hashlib
import json
from pathlib import Path
from urllib import request, error


def check(data, port):
    base=f'http://127.0.0.1:{port}'
    def get(path,headers=None):
        with request.urlopen(request.Request(base+path,headers=headers or {}),timeout=30) as r:
            assert r.headers['Cache-Control']=='no-store'
            return r.read()
    for path in ('/','/app.js','/style.css'):
        assert get(path)
    index=json.loads(get('/api/index')); assert index['summary']['cases']==69
    arms=index.get('arms',('token_cleaned','token_ocr','token_images','time_cleaned','time_ocr','time_images'))
    image=None
    for case in (1,41,132,283,376):
        for arm in arms:
            p=json.loads(get(f'/api/payload/{case}/{arm}'))
            local=json.loads((data/f'case-{case:04d}/{arm}.json').read_text())
            assert p['parts']==local['parts'] and p['query']==local['query']
            assert all(x['at'] and x['at']<p['cutoffExclusive'] for x in p['items'])
            assert json.loads(get(f'/api/input/{case}/{arm}'))==local['parts']
            if arm=='time_images' and case==41:
                assert p['status']=='blocked_input_limits' and len(p['frameRecordIDs'])==611
            for part in p['parts']:
                if part['type']=='local_image':image=part;break
    assert hashlib.sha256(get('/image/'+image['sha256'])).hexdigest()==image['sha256']
    for path,headers,expected in [('/api/payload/118/time_images',{},404),('/image/'+'0'*64,{},404),('/',{'Host':'example.com'},403),('/',{'Origin':'https://example.com'},403)]:
        try:get(path,headers)
        except error.HTTPError as e:assert e.code==expected
        else:raise AssertionError('Expected rejection')
    try:
        request.urlopen(request.Request(base+'/',data=b'{}',method='POST'),timeout=30)
    except error.HTTPError as e:assert e.code==501
    else:raise AssertionError('UI accepted a write operation')
    return {'status':'passed','caseConditionPayloads':5*len(arms),'inputDownloadMatches':5*len(arms),
            'sourceTimestampsVisible':True,'imageHashMatches':True,'crossOriginRejected':True,
            'mutationsRejected':True,'browserVisualInspection':'not_available','providerCalls':0}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--data',required=True,type=Path);p.add_argument('--port',default=8784,type=int);p.add_argument('--report',type=Path);a=p.parse_args()
    r=check(a.data,a.port)
    if a.report:
        with a.report.open('x') as f:f.write(json.dumps(r,sort_keys=True)+'\n')
    print(json.dumps(r,sort_keys=True))

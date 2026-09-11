#!/usr/bin/env python3
"""Read-only checks of the localhost vision review surface (no browser actions)."""
import argparse
import json
from pathlib import Path
import urllib.error
import urllib.request


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--port',type=int,default=8782);p.add_argument('--output',type=Path);a=p.parse_args()
    root=f'http://127.0.0.1:{a.port}'
    def get(path):
        with urllib.request.urlopen(root+path,timeout=30) as r:
            assert r.headers['Cache-Control']=='no-store'
            return r.read()
    def blocked(path,headers,expected):
        try:urllib.request.urlopen(urllib.request.Request(root+path,headers=headers),timeout=10)
        except urllib.error.HTTPError as e:assert e.code==expected;return
        raise AssertionError('Request was not blocked')
    overview=json.loads(get('/api/overview'));variants=('cleaned_recent','raw_ocr_recent','screenshots_recent')
    counts={v:0 for v in variants}
    for item in overview['cases']:
        c=json.loads(get('/api/case/'+str(item['case'])))
        assert c['target']==item['target'] and set(c['answers'])==set(variants)
        for v in variants:
            counts[v]+=int(c['answers'][v]['semanticPass'])
            assert c['answers'][v]['reason'] and c['answers'][v]['prediction']
            context=json.loads(get('/api/context/'+str(c['case'])+'/'+v))
            assert context['parts'] and context['variant']==v
    assert counts=={v:overview['summary']['arms'][v]['passes'] for v in variants}
    for n in (108,283,345):
        c=json.loads(get('/api/case/'+str(n)))
        assert get(c['frames'][-1]['url']).startswith(b'\x89PNG\r\n\x1a\n')
    for path in ('/','/app.js','/style.css','/report'):assert get(path)
    blocked('/api/overview',{'Host':'example.com'},403)
    blocked('/api/overview',{'Origin':'https://example.com'},403)
    blocked('/api/case/99999',{},404);blocked('/api/frame/108/99999',{},404)
    result={'status':'passed','cases':len(overview['cases']),'answers':207,'contexts':207,
            'sourceImagesChecked':3,'currentPassCounts':counts,'localOnlySecurityChecks':True,
            'assetsAndReport':True,'browserVisualQA':False}
    if a.output:
        with a.output.open('x') as f:f.write(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))


if __name__=='__main__':main()

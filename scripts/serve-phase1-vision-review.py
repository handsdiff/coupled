#!/usr/bin/env python3
"""Read-only localhost review of frozen vision-pilot answers and explicit grades.

Indexes prompts on disk, loads one case at a time, and streams one allowlisted
image at a time. No provider calls, collection changes, or data mutation.
"""
import argparse
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import shutil
from urllib.parse import urlsplit

from phase1_read_model_comparison import file_hash, rows

ASSETS = Path(__file__).with_name('phase1-vision-ui')
VARIANTS = ('cleaned_recent', 'raw_ocr_recent', 'screenshots_recent')


def load(p):
    return json.loads(p.read_text())


class Index:
    def __init__(self, path, key, digest):
        self.path, self.offsets = path, {}
        assert file_hash(path) == digest, f'Changed artifact: {path.name}'
        with path.open('rb') as f:
            while True:
                offset = f.tell()
                line = f.readline()
                if not line:
                    break
                if line.strip():
                    k = key(json.loads(line))
                    assert k not in self.offsets, 'Duplicate key'
                    self.offsets[k] = offset

    def get(self, key):
        with self.path.open('rb') as f:
            f.seek(self.offsets[key])
            return json.loads(f.readline())


class Review:
    def __init__(self, root, scored):
        self.root, self.scored = root.resolve(), scored.resolve()
        done = load(scored / 'completion.json')
        assert done['status'] == 'scored_and_reconciled'
        assert file_hash(root/'predictions.jsonl') == done['predictionsSHA256']
        for name, digest in done['artifactsSHA256'].items():
            assert file_hash(scored/name) == digest
        for name, digest in done['evidenceSHA256'].items():
            assert file_hash(Path(name)) == digest
        hashes = load(root/'plan.json')['artifactsSHA256']
        self.prompts = Index(root/'data/prompts.local.jsonl', lambda r: (r['case'],r['variant']), hashes['data/prompts.local.jsonl'])
        self.cases = Index(root/'data/cases.jsonl', lambda r: r['case'], hashes['data/cases.jsonl'])
        assert file_hash(root/'data/frames.jsonl') == hashes['data/frames.jsonl']
        # Metadata only; full OCR and images stay on disk.
        self.frames = {r['screenshotSHA256']: {k:r.get(k) for k in ('capturedAt','source','width','height','recordID')}
                       for r in rows(root/'data/frames.jsonl')}
        self.summary = load(scored/'summary.json')
        self.grades = defaultdict(dict)
        for r in rows(scored/'judgments.jsonl'):
            v = r['variant']
            assert v in VARIANTS and v not in self.grades[r['case']]
            self.grades[r['case']][v] = {k:r[k] for k in ('prediction','semanticPass','reason','reviewAuthority','timing','apiEquivalentCostUSD')}
            self.grades[r['case']][v]['inputTokens'] = r['usage']['input_tokens']
            self.grades[r['case']][v]['outputTokens'] = r['usage']['output_tokens']
        assert len(self.grades) == 69 and all(set(v) == set(VARIANTS) for v in self.grades.values())
        self.examples = []
        for n, grades in sorted(self.grades.items()):
            c = self.cases.get(n)
            self.examples.append({'case':n, 'target':c['target'], 'application':c['application'],
                'beganAt':c['targetBeganAt'], 'passes':{v:g['semanticPass'] for v,g in grades.items()}})

    def case(self, n):
        grades = self.grades.get(n)  # Only scored cases are accessible.
        if not grades:
            raise KeyError(n)
        c = self.cases.get(n)
        prompt = self.prompts.get((n, 'screenshots_recent'))
        images = [p for p in prompt['parts'] if p['type'] == 'local_image']
        q = json.loads(c['query']) if isinstance(c['query'],str) else c['query']
        return {'case':n,'target':c['target'],'query':q,'beganAt':c['targetBeganAt'],
                'interval':c['interval'],'answers':grades,
                'frames':[{'index':i,**self.frames.get(p['sha256'],{}),'url':f'/api/frame/{n}/{i}'} for i,p in enumerate(images)]}

    def context(self, n, variant):
        if n not in self.grades:
            raise KeyError(n)
        p = self.prompts.get((n,variant))
        return {'case':n,'variant':variant,'partsSHA256':p['partsSHA256'],
                'parts':[{'type':'image','imageIndex':sum(x['type']=='local_image' for x in p['parts'][:i])}
                         if t['type']=='local_image' else {'type':'text','text':t['text']}
                         for i,t in enumerate(p['parts'])], 'imageCount':p['imageCount']}

    def image(self, n, index):
        if n not in self.grades:
            raise KeyError(n)
        parts = [p for p in self.prompts.get((n,'screenshots_recent'))['parts'] if p['type']=='local_image']
        if index < 0 or index >= len(parts):
            raise KeyError(index)
        part = parts[index]
        path = Path(part['path'])
        assert file_hash(path) == part['sha256'], 'Screenshot changed since request'
        return path


def handler(data):
    class Handler(BaseHTTPRequestHandler):
        def send_payload_headers(self, status, kind, size):
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(size))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
            self.end_headers()

        def reply(self,status,payload,kind='application/json; charset=utf-8'):
            self.send_payload_headers(status,kind,len(payload))
            self.wfile.write(payload)

        def do_GET(self):
            host = self.headers.get('Host','')
            if host not in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'):
                self.reply(403,b'Localhost only','text/plain');return
            origin = self.headers.get('Origin')
            if origin and origin not in (f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}'):
                self.reply(403,b'Cross-origin access denied','text/plain');return
            path = urlsplit(self.path).path
            try:
                assets = {'/':('index.html','text/html'),'/app.js':('app.js','text/javascript'),'/style.css':('style.css','text/css')}
                if path in assets:
                    name,kind=assets[path]
                    self.reply(200,(ASSETS/name).read_bytes(),kind+'; charset=utf-8');return
                if path == '/report':
                    self.reply(200,(data.scored/'report.md').read_bytes(),'text/plain; charset=utf-8');return
                if path == '/health': result={'status':'ok','cases':69,'predictions':207,'readOnly':True}
                elif path == '/api/overview': result={'summary':data.summary,'cases':data.examples}
                elif m:=re.fullmatch(r'/api/case/([1-9][0-9]*)',path):result=data.case(int(m[1]))
                elif m:=re.fullmatch(r'/api/context/([1-9][0-9]*)/(cleaned_recent|raw_ocr_recent|screenshots_recent)',path):result=data.context(int(m[1]),m[2])
                elif m:=re.fullmatch(r'/api/frame/([1-9][0-9]*)/([0-9]+)',path):
                    p=data.image(int(m[1]),int(m[2]))
                    with p.open('rb') as f:
                        self.send_payload_headers(200,'image/png',p.stat().st_size)
                        shutil.copyfileobj(f,self.wfile,length=262144)
                    return
                else:self.reply(404,b'Not found','text/plain');return
                self.reply(200,json.dumps(result,ensure_ascii=False).encode())
            except (KeyError,IndexError):self.reply(404,b'Unknown case or frame','text/plain')
            except (ValueError,AssertionError,OSError):self.reply(500,b'Artifact validation failed','text/plain')
    return Handler


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',type=Path,required=True)
    p.add_argument('--scored',type=Path,required=True)
    p.add_argument('--port',type=int,default=8782)
    p.add_argument('--check',action='store_true')
    a=p.parse_args();data=Review(a.run,a.scored)
    if a.check:
        for c in data.examples:
            assert set(data.case(c['case'])['answers']) == set(VARIANTS)
            for v in VARIANTS:data.context(c['case'],v)
        print(json.dumps({'status':'passed','cases':69,'answers':207,'contexts':207}));return
    server=ThreadingHTTPServer(('127.0.0.1',a.port),handler(data))
    print(f'Vision review ready: http://127.0.0.1:{a.port}',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()


if __name__=='__main__':main()

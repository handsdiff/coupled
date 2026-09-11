#!/usr/bin/env python3
"""Local, read-only inspection of prepared inputs. No execution controls."""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import shutil
from collections import defaultdict, deque
from urllib.parse import urlsplit

from phase1_read_model_comparison import file_hash, fingerprint, rows

ASSETS = Path(__file__).with_name('phase1-representation-ui')
ARMS = ('token_cleaned', 'token_ocr', 'token_images', 'time_cleaned', 'time_ocr', 'time_images')
MEDIAN_ARMS = ('time_cleaned', 'time_ocr', 'time_images', 'budget_cleaned', 'budget_ocr')


def load(p):
    return json.loads(p.read_text())


class Index:
    def __init__(self,path,key):
        self.path=path;self.offsets={}
        with path.open('rb') as f:
            while True:
                at=f.tell();line=f.readline()
                if not line:break
                if line.strip():self.offsets[key(json.loads(line))]=at

    def get(self,key):
        with self.path.open('rb') as f:
            f.seek(self.offsets[key]);return json.loads(f.readline())


class Review:
    def __init__(self, root, run=None):
        self.root = root.resolve()
        self.hashes = load(root/'artifact-hashes.json')
        for name, digest in self.hashes.items():
            p = (root/name).resolve()
            assert p.is_relative_to(self.root) and file_hash(p) == digest
        self.plan = load(root/'plan.json')
        self.median = self.plan['version'] == 'phase1-median-context-controls-v1'
        self.arms = MEDIAN_ARMS if self.median else ARMS
        self.run = run.resolve() if run else None
        if self.run:
            assert load(self.run/'plan.json')['preparedPlanSHA256'] == file_hash(root/'plan.json')
        self.summary = load(root/'summary.json')
        self.rows = load(root/'cases.json')
        self.lookup = {(x['case'], x['arm']): x for x in self.rows}
        assert len(self.lookup) == len(self.arms)*self.summary['cases']
        self.execution = {}
        self.score_cache = None
        if self.run:
            self.execution = {(r['case'], r['arm']): r for r in rows(self.run/'requests.jsonl')}
        if self.median:
            self.images = {}
            for row in self.rows:
                if row['arm'] != 'time_images': continue
                for part in load(root/row['payload'])['parts']:
                    if part['type'] == 'local_image': self.images[part['sha256']] = Path(part['path'])
            return
        pilot=Path(self.plan['sourcePilot']);budget=Path(self.plan['sourceBudgetRun'])
        frozen=Path(load(pilot/'data/plan.json')['source'])/'frozen'
        self.originals=Index(pilot/'data/prompts.local.jsonl',lambda x:(x['case'],x['variant']))
        self.expanded=Index(budget/'data/prompts.local.jsonl',lambda x:(x['case'],x['variant']))
        self.baselines=Index(frozen/'prompts.jsonl',lambda x:(x['exampleID'],x['variant']))
        self.raw_blocks=Index(budget/'data/raw-history-blocks.jsonl',lambda x:x['case'])
        self.events={x['sourceEventID']:x for x in rows(frozen/'new-context-events.jsonl')}
        self.images = {}
        for row in self.rows:
            payload = self.payload(row['case'], row['arm'])
            for part in payload['parts']:
                if part['type'] == 'local_image':
                    # Only images actually present in a prepared input are served.
                    self.images[part['sha256']] = Path(part['path'])

    def scoring(self):
        if not self.run:
            return None
        directory = self.run/'review-v1/scored-v1'
        completion_path = directory/'completion.json'
        if not completion_path.exists():
            return None
        digest = file_hash(completion_path)
        if self.score_cache and self.score_cache[0] == digest:
            return self.score_cache[1]
        completion = load(completion_path)
        assert completion['status'] == 'scored_and_reconciled'
        assert completion['runPlanSHA256'] == file_hash(self.run/'plan.json')
        assert completion['predictionsSHA256'] == file_hash(self.run/'predictions.jsonl')
        for name, expected in completion['artifactsSHA256'].items():
            assert file_hash(directory/name) == expected
        result = {'summary': load(directory/'summary.json'),
                  'judgments': {(r['case'],r['arm']): r for r in rows(directory/'judgments.jsonl')},
                  'completionSHA256': digest}
        self.score_cache = (digest,result)
        return result

    def payload(self, case, arm):
        row = self.lookup[case, arm]
        p = self.root/row['payload']
        assert file_hash(p) == self.hashes[row['payload']]
        obj = load(p)
        assert fingerprint(obj['parts']) == row['partsSHA256']
        if self.median:
            if self.run:
                request = self.execution[case, arm]
                path = self.run/'results'/f"{request['requestOrdinal']:04d}"/'result.json'
                if path.exists():
                    result = load(path)
                    assert result['partsSHA256'] == row['partsSHA256']
                    obj['execution'] = {k: result.get(k) for k in ('prediction','usage','timing','apiEquivalentCostUSD','validCompletion','reuse')}
                    scoring = self.scoring()
                    if scoring:
                        grade = scoring['judgments'][case,arm]
                        assert grade['prediction'] == result['prediction']
                        obj['execution']['grade'] = {k:grade[k] for k in ('decision','reason','reviewAuthority')}
            return obj
        # Add timestamps for display from bound source lineage, without editing
        # any model-facing part. Cleaned serializations intentionally omit time.
        if arm in ('time_cleaned','token_cleaned'):
            original=self.originals.get((case,'cleaned_recent'))
            blocks=self.baselines.get((obj['exampleID'],'new'))['retainedBlocks']
            cutoff=original['recentInterval']['startAt']
            ordered=[b for b in blocks if b['availableAt']<cutoff]
            ordered+=sorted([b for b in blocks if b['availableAt']>=cutoff],key=lambda b:b['availableAt'])
            assert [b['serialized']+'\n' for b in ordered]==[t['text'] for t in original['parts'][1:-1]]
            if arm=='token_cleaned':
                extra=self.expanded.get((case,'cleaned_recent'))['budgetPacking']['addedEventIDs']
                ordered=[self.events[i] for i in extra]+ordered
        elif arm=='token_ocr':
            ordered=self.raw_blocks.get(case)['blocks']
            assert [b['serialized']+'\n' for b in ordered]==[t['text'] for t in obj['parts'][1:-1]]
        else:
            ordered=None
        if ordered is not None:
            assert len(ordered)==len(obj['items'])
            obj['items']=[dict(item,at=b['availableAt'],kind=b['kind'],id=b.get('eventID',b.get('sourceEventID'))) for item,b in zip(obj['items'],ordered)]
            obj['displayMetadataSource']='frozen_causal_event_lineage'
        return obj


def handler(data):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # Do not persist sensitive input text or screenshot paths.

        def headers_for(self, code, mime, length):
            self.send_response(code)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(length))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'")
            self.end_headers()

        def reply(self, code, content, mime='application/json; charset=utf-8'):
            self.headers_for(code, mime, len(content))
            self.wfile.write(content)

        def do_GET(self):
            allowed = (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')
            if self.headers.get('Host') not in allowed or self.headers.get('Origin', 'http://'+allowed[0]) not in ['http://'+a for a in allowed]:
                return self.reply(403, b'Localhost only', 'text/plain')
            path = urlsplit(self.path).path
            try:
                if path in ('/', '/app.js', '/style.css'):
                    asset = ASSETS/('index.html' if path == '/' else path[1:])
                    mime = {'/': 'text/html; charset=utf-8', '/app.js': 'text/javascript; charset=utf-8', '/style.css': 'text/css; charset=utf-8'}[path]
                    return self.reply(200, asset.read_bytes(), mime)
                if path == '/api/index':
                    progress = load(data.run/'progress.json') if data.run and (data.run/'progress.json').exists() else None
                    if data.run and (data.run/'pause.json').exists():
                        progress = {**(progress or {}), 'executionPause': load(data.run/'pause.json')}
                    scored = data.scoring()
                    return self.reply(200, json.dumps({'plan': data.plan, 'summary': data.summary, 'rows': data.rows, 'arms': data.arms, 'progress': progress,
                        'scoring': scored['summary'] if scored else None}, ensure_ascii=False).encode())
                m = re.fullmatch(r'/api/(payload|input)/([1-9][0-9]*)/([a-z_]+)', path)
                if m and m[3] in data.arms:
                    payload = data.payload(int(m[2]), m[3])
                    result = payload if m[1] == 'payload' else payload['parts']
                    return self.reply(200, json.dumps(result, ensure_ascii=False).encode())
                m = re.fullmatch(r'/image/([0-9a-f]{64})', path)
                if m:
                    image = data.images[m[1]]
                    assert file_hash(image) == m[1], 'Screenshot changed since preparation'
                    self.headers_for(200, 'image/png', image.stat().st_size)
                    with image.open('rb') as f:
                        shutil.copyfileobj(f, self.wfile, 65536)
                    return
                return self.reply(404, b'Not found', 'text/plain')
            except (KeyError, ValueError):
                return self.reply(404, b'Unknown input', 'text/plain')
            except AssertionError:
                return self.reply(409, b'Artifact integrity check failed', 'text/plain')

    return Handler


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data', required=True, type=Path)
    p.add_argument('--port', default=8784, type=int)
    p.add_argument('--check', action='store_true')
    p.add_argument('--run', type=Path, help='Optional bound run for live status and saved completions')
    a = p.parse_args()
    data = Review(a.data, a.run)
    if a.check:
        print(json.dumps({'status': 'passed', 'payloads': len(data.rows), 'uniqueAllowedImages': len(data.images)}))
    else:
        server = ThreadingHTTPServer(('127.0.0.1', a.port), handler(data))
        print(f'Prepared-input review: http://127.0.0.1:{a.port} (read-only; no provider calls)', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()

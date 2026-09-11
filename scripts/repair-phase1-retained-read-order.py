#!/usr/bin/env python3
"""Audit every retained READ; propose same-image, word-preserving order repairs.

Never mutates input artifacts. Geometry only screens for inversions. Native
Vision must independently supply an ordering of the exact retained strings.
Recognition changes, missing line evidence and protected reviews stay explicit.
"""
import argparse
from collections import Counter, defaultdict, deque
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import resource
import select
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = 'retained-read-native-order-audit-v1'


def canonical(x): return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
def sha(p):
    h = hashlib.sha256()
    with Path(p).open('rb') as f:
        for b in iter(lambda: f.read(1024*1024), b''): h.update(b)
    return h.hexdigest()
def rows(p):
    with Path(p).open() as f:
        for l in f:
            if l.strip(): yield json.loads(l)
def save(p, x):
    with Path(p).open('x') as f: f.write(canonical(x)+'\n')


def line_indices(text, lines):
    positions = defaultdict(deque)
    for i, line in enumerate(lines): positions[line['text']].append(i)
    result = []
    for s in text.split('\n'):
        if not positions[s]: return None
        result.append(positions[s].popleft())
    return result


def inversions(lines):
    """Screen only vertically disjoint observations sharing substantial x span."""
    bad = []
    for i, a in enumerate(lines):
        x = a['boundingBox']
        if len(a['text']) < 12: continue
        for j in range(i+1, len(lines)):
            b = lines[j]; y = b['boundingBox']
            if len(b['text']) < 12: continue
            overlap = min(x['x']+x['width'], y['x']+y['width'])-max(x['x'], y['x'])
            if overlap < .5*min(x['width'], y['width']): continue
            if y['y'] > x['y']+x['height']+.05*min(x['height'], y['height']):
                bad.append([i,j])
    return bad


def reorder_projection(text, old_lines, native_lines):
    """No OCR substitutions. Require whole source vocabulary to be unchanged."""
    if Counter(x['text'] for x in old_lines) != Counter(x['text'] for x in native_lines):
        return None, 'recognition_changed'
    chosen = line_indices(text, old_lines)
    if chosen is None: return None, 'retained_text_not_exact_source_lines'
    occurrences = defaultdict(deque)
    for i, x in enumerate(old_lines): occurrences[x['text']].append(i)
    order = [occurrences[x['text']].popleft() for x in native_lines]
    chosen = set(chosen)
    after = '\n'.join(old_lines[i]['text'] for i in order if i in chosen)
    assert Counter(after.split('\n')) == Counter(text.split('\n'))
    return after, 'native_permutation' if after != text else 'native_confirms_existing_order'


def patch_order(event, pane, native, line_key):
    e = deepcopy(event); text = json.loads(e['serialized'])['content']
    after, reason = reorder_projection(text, pane[line_key], native['lines'])
    if after is None or after == text: return None, reason
    # Deltas must be reordered from the same source, not replaced by full state.
    novelty = e.get('readNovelty', {})
    delta = novelty.get('content')
    if delta not in (None, '', text):
        new_delta, _ = reorder_projection(delta, pane[line_key], native['lines'])
        if new_delta is None: return None, 'delta_requires_separate_review'
    else: new_delta = after if delta == text else delta
    for key in ('serialized','auditSerialized'):
        if key in e:
            payload = json.loads(e[key])
            if 'content' in payload:
                assert payload['content'] == text
                payload['content'] = after; e[key] = canonical(payload)
    if 'content' in novelty: novelty['content'] = new_delta
    return e, reason


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input',type=Path,required=True)
    ap.add_argument('--surfaces',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--max-ocr-calls',type=int,default=3000)
    args = ap.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    events = list(rows(args.input/'new/events.jsonl'))
    read_events = [e for e in events if e['kind']=='read']
    needed = {rid for e in read_events for rid in e['sourceRecordIDs']}
    panes = {}; source_hashes = {str(args.input/'new/events.jsonl'):sha(args.input/'new/events.jsonl')}
    for day in (2,3,4,7):
        path=args.surfaces/f'sep{day}-surfaces/read-surfaces.jsonl'
        source_hashes[str(path)]=sha(path)
        for r in rows(path):
            if r['sourceRecordID'] in needed:
                r['_day']=day; panes[r['sourceRecordID']]=r
    jobs=[]; decisions=[]
    for e in read_events:
        text=json.loads(e['serialized']).get('content'); options=[]
        if not isinstance(text,str) or not text:
            decisions.append({'eventID':e['sourceEventID'],'status':'no_text_payload'})
            continue
        for rid in e['sourceRecordIDs']:
            pane=panes.get(rid)
            if not pane or pane['capturedAt']>e['availableAt']: continue
            for label,key,region in [('comparison','comparisonLines','comparisonRegionOfInterest'),('pane','lines','regionOfInterest')]:
                if not pane.get(region): continue
                indices=line_indices(text,pane[key])
                if indices is None:continue
                bad=inversions([pane[key][i] for i in indices])
                options.append((pane['capturedAt'],label,key,region,pane,bad))
        d={'eventID':e['sourceEventID'],'beforeSHA256':hashlib.sha256(canonical(e).encode()).hexdigest()}
        if not options:d['status']='no_exact_source_projection'
        else:
            # Latest eligible same-image source; comparison region preferred.
            item=sorted(options,key=lambda x:(x[0],x[1]=='comparison'))[-1]
            _,label,key,region,pane,bad=item
            d.update(sourceRecordID=pane['sourceRecordID'],label=label,inversions=bad)
            if bad:
                d['status']='native_check_pending';jobs.append((e,pane,key,region,d))
            else:d['status']='no_same_column_inversion'
        decisions.append(d)
    print(canonical({'screenedReads':len(read_events),'suspectReadEvents':len(jobs)}),flush=True)
    source=ROOT/'scripts/ocr-phase1-surface-regions.m'; source_hashes[str(source)]=sha(source)
    silicon=subprocess.check_output(['sysctl','-n','hw.optional.arm64'],text=True).strip()=='1'
    assert silicon,'Native ARM OCR required'
    cache={}; count=0
    with tempfile.TemporaryDirectory(prefix='coupled-read-order-') as td, (args.output/'observations.jsonl').open('x') as out, (args.output/'read-changes.jsonl').open('x') as changes:
        binary=Path(td)/'ocr'
        subprocess.run(['clang','-arch','arm64','-fobjc-arc','-fblocks','-framework','Foundation','-framework','Vision','-framework','ImageIO','-framework','CoreGraphics',str(source),'-o',str(binary)],check=True,timeout=90)
        worker=None
        try:
            for index,(e,pane,key,region,d) in enumerate(jobs,1):
                image=ROOT/f"coupled-data/phase1-ordinary-work-2026-09-{pane['_day']:02d}-1"/pane['screenshotRelativePath']
                assert sha(image)==pane['screenshotSHA256']
                job_id=hashlib.sha256(canonical([pane['screenshotSHA256'],pane[region],source_hashes[str(source)]]).encode()).hexdigest()
                if job_id not in cache:
                    assert count<args.max_ocr_calls,'Explicit local OCR limit reached'
                    if worker is None:worker=subprocess.Popen([str(binary)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
                    job={'jobID':job_id,'imagePath':str(image),'regionOfInterest':pane[region]}
                    worker.stdin.write(canonical(job)+'\n');worker.stdin.flush()
                    if not select.select([worker.stdout],[],[],60)[0]:raise TimeoutError('OCR exceeded 60 seconds')
                    native=json.loads(worker.stdout.readline());assert not native.get('error'),native.get('error')
                    native.update(screenshotSHA256=pane['screenshotSHA256'],sourceRecordID=pane['sourceRecordID'],ocrArchitecture='arm64')
                    out.write(canonical(native)+'\n');out.flush();cache[job_id]=native;count+=1
                    if count%100==0:
                        worker.stdin.close();assert worker.wait(timeout=10)==0;worker=None
                native=cache[job_id]
                updated,status=patch_order(e,pane,native,key);d.update(status=status,jobID=job_id)
                if updated:
                    proof={'version':VERSION,'sourceRecordID':pane['sourceRecordID'],'capturedAt':pane['capturedAt'],
                           'screenshot':str(image.relative_to(ROOT)),'screenshotSHA256':pane['screenshotSHA256'],
                           'regionOfInterest':pane[region],'nativeJobID':job_id,'label':d['label'],
                           'wordMultisetUnchanged':True,'decision':'native_order_of_exact_retained_lines'}
                    changes.write(canonical({'eventID':e['sourceEventID'],'beforeSHA256':d['beforeSHA256'],'event':updated,'proof':proof})+'\n');changes.flush()
                if index%25==0:print(canonical({'checked':index,'total':len(jobs),'ocrCalls':count,'statuses':dict(Counter(d['status'] for d in decisions))}),flush=True)
                assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss<1024**3,'Parent exceeded 1 GiB'
        finally:
            if worker:
                worker.stdin.close()
                try:worker.wait(timeout=10)
                except subprocess.TimeoutExpired:worker.kill();worker.wait()
    with (args.output/'decisions.jsonl').open('x') as f:
        for d in decisions:f.write(canonical(d)+'\n')
    save(args.output/'audit.json',{'version':VERSION,'status':'proposals_require_review','reads':len(read_events),'ocrCalls':count,
        'counts':dict(Counter(d['status'] for d in decisions)),'sourceHashes':source_hashes,
        'artifactsSHA256':{p.name:sha(p) for p in args.output.glob('*.jsonl')},
        'scope':'All retained READs screened; no geometric anomaly does not prove general reading-order correctness',
        'rawModified':False,'providerCalls':0,'peakRSSMiB':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024**2})
    print((args.output/'audit.json').read_text(),flush=True)


if __name__=='__main__':main()

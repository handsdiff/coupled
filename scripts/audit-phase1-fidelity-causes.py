#!/usr/bin/env python3
"""Read-only stage evidence for the frozen 687-case cohort. No model calls.

Screening signals are explicitly not causal diagnoses. Human/assistant review
is a separate sidecar; neither this script nor its output feeds construction.
Raw JSONL is streamed and only referenced records retained. Screenshots linked,
never copied. Never imports a producer module that executes on import.
"""
import argparse
from collections import defaultdict
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import resource
import sys

ROOT = Path(__file__).resolve().parents[1]
PREP = ROOT/'coupled-data/sep02-10-training-prep-20260910'
REVIEW = ROOT/'coupled-data/sep02-10-frontier-prep-20260910'
PROBE = ROOT/'coupled-data/sep02-10-episode-boundary-v10-review-20260910/probe.json'


def read(path): return json.loads(Path(path).read_text())
def rows(path):
    with Path(path).open() as f:
        for n,line in enumerate(f,1):
            if line.strip(): yield n,json.loads(line),hashlib.sha256(line.encode()).hexdigest()
def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()
def save(path,value):
    with Path(path).open('x') as f:json.dump(value,f,ensure_ascii=False,indent=2,sort_keys=True);f.write('\n')
def memory():
    peak=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    mib=peak/(1024**2 if sys.platform=='darwin' else 1024)
    if mib>1024:raise RuntimeError('Audit exceeded 1 GiB RSS; stop without touching source')
    return mib


def pane_signals(pane,raw):
    """Geometry/substring witnesses only; do not infer unseen ground truth."""
    region=pane['regionOfInterest'];lines=pane.get('lines',[])
    full=(raw or {}).get('content','').splitlines()
    cut=[]
    for line in lines:
        text=line['text'];box=line.get('boundingBox',{})
        # Native pane observations are ROI-relative in the persisted evidence.
        if box.get('x',1)>0.002 or len(text)<20:continue
        for original in full:
            match=SequenceMatcher(None,original,text,autojunk=False).find_longest_match()
            if match.b<=5 and match.a>=match.b+2 and match.size>=max(20,len(text)-6):
                cut.append({'paneText':text,'sameFrameFullWindowOCR':original,
                            'matchedCharacters':match.size,'missingPrefixCharacters':match.a-match.b,
                            'exactSuffix':match.b==0 and match.size==len(text),
                            'paneLineBoundingBox':box})
                break
    inversions=[]
    for i in range(len(lines)-1):
        a,b=lines[i],lines[i+1];aa=a.get('boundingBox',{});bb=b.get('boundingBox',{})
        if not aa or not bb:continue
        # Lower line emitted before a physically distinct upper line.
        if bb['y'] > aa['y']+aa['height']*.8:
            inversions.append({'firstEmitted':a['text'],'nextEmitted':b['text'],
                               'firstBox':aa,'nextBox':bb})
    return {'leftEdgeTruncationWitnesses':cut,'geometricReadingOrderInversions':inversions,
            'interpretation':'Measured text/geometry discrepancies. Review determines materiality and whether each discrepancy blocks the episode.'}


def prepare(output):
    if output.exists():raise ValueError('Use a new audit directory')
    adjud=read(REVIEW/'fidelity-adjudication.json');scope=read(REVIEW/'fidelity-scope-diagnostic.json')
    bounds=read(REVIEW/'fidelity-boundary-evidence.json');probe=read(PROBE)
    cases=set(adjud['allRecommendedRepairCases'])|set(adjud['unresolvedCases'])
    cases.update(x[k] for x in bounds for k in ['leftCase','rightCase'] if x[k] is not None)
    cohort={n:r for n,r,_ in rows(REVIEW/'cohort.jsonl') if n in cases}
    wanted_events={r['targetEventID'] for r in cohort.values()}
    for b in bounds:
        wanted_events.update(a['eventID'] for a in b['assessments'])
    wanted_prior={a['boundaryEvidence'].get('priorObservationID') for b in probe for a in b['assessments']}
    wanted_events.update(wanted_prior)
    episode_events={r['sourceEventID']:dict(r,fileLine=n) for n,r,_ in rows(PREP/'episodes/events.jsonl') if r['sourceEventID'] in wanted_events}
    micro_events={r['sourceEventID']:dict(r,fileLine=n) for n,r,_ in rows(PREP/'micro-corpus/events.jsonl') if r['sourceEventID'] in wanted_events}
    wanted_candidates={e['candidateID'] for e in episode_events.values() if e.get('candidateID')}
    wanted_candidates.update(b[k] for b in bounds for k in ['leftCandidateID','rightCandidateID'])
    candidates={r['candidateID']:dict(r,fileLine=n) for n,r,_ in rows(PREP/'episodes/raw-episode-candidates.jsonl') if r['candidateID'] in wanted_candidates}
    raw_ids={x for x in wanted_prior if x}
    for e in micro_events.values():raw_ids.update(e.get('sourceRecordIDs',[]))
    raw_lines=defaultdict(set)
    for c in candidates.values():
        for m in c['members']:
            raw_ids.update(m.get('sourceRecordIDs',[]));raw_ids.add(m['sourceRecordID'])
            raw_lines[m['rawPath']].add(m['rawLine'])
    def collect_refs(obj):
        if isinstance(obj,list):
            for v in obj:collect_refs(v)
        elif isinstance(obj,dict):
            if obj.get('rawPath') and obj.get('rawLine'):raw_lines[obj['rawPath']].add(obj['rawLine'])
            for k,v in obj.items():
                if k in ('rawIDs','sourceRecordIDs') and isinstance(v,list):raw_ids.update(v)
                elif isinstance(v,(dict,list)):collect_refs(v)
    collect_refs(bounds)
    prefix=[r for r in scope['prefixCandidates'] if r['case'] in set(adjud['missingPrefixCases'])|{529}]
    for p in prefix:
        for k in ['previous','current']:
            r=p[k];raw_ids.add(r['id'])
            raw_lines[r['path']].update(range(max(1,r['line']-12),r['line']+2))
    for spec in [adjud['additionalCase9'],adjud['unresolved529']]:
        for line in spec['rawLines']:raw_lines[spec['rawPath']].update(range(max(1,line-12),line+3))
    pane_paths=[ROOT/f'coupled-data/sep02-04-semantic-v24-review-20260906/sep{d}-surfaces-r2/read-surfaces.jsonl' for d in [2,3,4]]+[PREP/'new-sep7-surfaces-native/read-surfaces.jsonl']
    panes={}
    for path in pane_paths:
        for n,r,_ in rows(path):
            if r['sourceRecordID'] in raw_ids or r['evidenceID'] in wanted_prior:
                panes[r['sourceRecordID']]=dict(r,evidencePath=str(path.relative_to(ROOT)),fileLine=n)
                raw_ids.add(r['sourceRecordID'])
    raw={};raw_stats=[]
    for rel in sorted(raw_lines):
        path=ROOT/rel;before=path.stat();count=0
        for n,r,h in rows(path):
            if n in raw_lines[rel] or r.get('recordID') in raw_ids:
                raw[r['recordID']]=dict(r,auditSourcePath=rel,auditSourceLine=n,auditRawLineSHA256=h);count+=1
            if n%500==0:memory()
        raw_stats.append({'path':rel,'sizeAtStart':before.st_size,'sizeAtEnd':path.stat().st_size,'retainedRecords':count})
        print('Streamed',rel,'retained',count,flush=True)
    pane_by_evidence={p['evidenceID']:p for p in panes.values()}
    boundary_rows=[]
    for b,p in zip(bounds,probe):
        assert b['candidateLine']==p['candidateLine']
        c=candidates[b['leftCandidateID']]
        row={k:b[k] for k in ['candidateLine','leftCase','rightCase','beganAt','nextBeganAt','leftCandidateID','rightCandidateID','destination','draft','nextTarget']}
        row['baselineBoundary']=b['boundary'];row['reads']=[]
        for original,assessment in zip(b['assessments'],p['assessments']):
            assert original['eventID']==assessment['eventID']
            e=micro_events[original['eventID']];selected=[panes[i] for i in e.get('sourceRecordIDs',[]) if i in panes]
            rs=[raw[i] for i in e.get('sourceRecordIDs',[]) if i in raw]
            ev=assessment['boundaryEvidence'];prior=raw.get(ev.get('priorObservationID')) or pane_by_evidence.get(ev.get('priorObservationID')) or micro_events.get(ev.get('priorObservationID'))
            row['reads'].append({'event':e,'v10Assessment':assessment,'rawRecordIDs':[r['recordID'] for r in rs],
                'paneRecordIDs':[s['sourceRecordID'] for s in selected], 'prior':prior,
                'signals':[{'sourceRecordID':s['sourceRecordID'],**pane_signals(s,raw.get(s['sourceRecordID']))} for s in selected]})
        boundary_rows.append(row)
    case_rows=[]
    for n,r in sorted(cohort.items()):
        ev=episode_events[r['targetEventID']];c=candidates[ev['candidateID']]
        groups=[b['candidateLine'] for b in bounds if n in (b['leftCase'],b['rightCase'])]
        case_rows.append({'case':n,'original67':n in adjud['allRecommendedRepairCases'],'cohort':r,'episodeEvent':ev,'candidateID':c['candidateID'],'boundaryGroups':groups,
                         'prefixEvidence':[p for p in prefix if p['case']==n],
                         'islandEvidence':[p for p in scope['unchangedTargetIslands'] if p['case']==n]})
    inputs=[REVIEW/'fidelity-adjudication.json',REVIEW/'fidelity-scope-diagnostic.json',REVIEW/'fidelity-boundary-evidence.json',REVIEW/'cohort.jsonl',PROBE,PREP/'episodes/events.jsonl',PREP/'episodes/raw-episode-candidates.jsonl',PREP/'micro-corpus/events.jsonl',*pane_paths]
    for path in inputs:
        expected=adjud['inputSHA256'].get(str(path.relative_to(ROOT)))
        if expected:assert sha(path)==expected,('Input differs from original review',path)
    output.mkdir(parents=True)
    save(output/'stage-evidence.json',{'cases':case_rows,'boundaries':boundary_rows,'candidates':candidates,'panes':panes,'raw':raw})
    save(output/'manifest.json',{'version':'phase1-fidelity-root-cause-audit-v1','authority':'diagnostic_only','originalFlaggedCount':len(adjud['allRecommendedRepairCases']),'reviewCaseCount':len(cases),'boundaryGroupCount':len(bounds),'rawRecords':len(raw),'paneRecords':len(panes),'peakRSSMiB':memory(),'sourceHashes':{str(p.relative_to(ROOT)):sha(p) for p in inputs},'selectedRawRecordsSHA256':sha(output/'stage-evidence.json'),'rawSources':raw_stats,'scriptSHA256':sha(__file__),'limits':'1 GiB process peak RSS; streaming raw; no screenshot copies; no source modifications; no provider calls'})
    print('Prepared',output,'peak MiB',round(memory(),1),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True,type=Path);args=p.parse_args();prepare(args.output.resolve())

#!/usr/bin/env python3
"""Read-only neighborhood audit; candidates are not automatic thought merges."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
from datetime import datetime

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
s=importlib.util.spec_from_file_location('batch',ROOT/'scripts/apply-phase1-reviewed-batch.py');b=importlib.util.module_from_spec(s);s.loader.exec_module(b)
from phase1_read_boundary import ReadBoundaryEvidence

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    writes=sorted(b.h.rows(a.input/'common-write-events.jsonl'),key=lambda r:(r['beganAt'],r['sourceEventID']))
    examples={r['targetEventID']:i for i,r in enumerate(b.h.rows(a.input/'cohort.jsonl'),1)}
    reads=[e for e in b.h.rows(a.input/'new/events.jsonl') if e['kind']=='read'];assessor=ReadBoundaryEvidence(reads)
    pairs=[];wanted=set()
    for x,y in zip(writes,writes[1:]):
        if x['sessionID']!=y['sessionID'] or x.get('logicalDestinationKey')!=y.get('logicalDestinationKey'):continue
        if x.get('modelFacingDestination',{}).get('application')!='Obsidian':continue
        if not (x['sourceEventID'] in examples or y['sourceEventID'] in examples):continue
        pairs.append((x,y));wanted.update(x['sourceRecordIDs']+y['sourceRecordIDs'])
    raw={};locations={}
    keys=['recordID','sessionID','before','after','beforeAXErrors','afterAXErrors','targetIdentity','conditioningState','inputHints',
          'pasteCheckpoints','tapTimeoutCountDuringBurst','lastInputAt','beganAt','terminalDecisionAt','returnCheckpoints']
    for day in (2,3,4,7):
        for line,r in enumerate(b.h.rows(ROOT/f'coupled-data/phase1-ordinary-work-2026-09-{day:02d}-1/raw.jsonl'),1):
            if r.get('recordID') in wanted and 'inputEvents' in r:
                raw[r['recordID']]={k:r.get(k) for k in keys};locations[r['recordID']]=(day,line)
    output=[]
    for x,y in pairs:
        ids=set(x['sourceRecordIDs']+y['sourceRecordIDs']);rs=sorted([raw[rid] for rid in ids if rid in raw],key=lambda r:r['beganAt'])
        d={'leftCase':examples.get(x['sourceEventID']),'rightCase':examples.get(y['sourceEventID']),
           'leftID':x['sourceEventID'],'rightID':y['sourceEventID'],
           'leftText':json.loads(x['serialized']).get('authorshipSegments'),'rightText':json.loads(y['serialized']).get('authorshipSegments'),
           'rawLines':[locations[r['recordID']] for r in rs]}
        try:
            content,edit=b.verify_persistent_insertion(rs)
            d.update(reconstructible=True,proposedContent=content)
            between=[r for r in reads if r['sessionID']==x['sessionID'] and rs[0]['beganAt']<r['availableAt']<rs[-1]['terminalDecisionAt']]
            assessments=[]
            for r in between:
                states=[v for v in rs if v['after'] and v['after']['observedAt']<r['availableAt']]
                if not states: assessments.append({'eventID':r['sourceEventID'],'status':'no_prior_draft'});continue
                alg=b.load('construct-phase1-raw-episode-corpus');draft=alg.normalize_authored_content(alg.minimal_edit(rs[0]['before']['value'],states[-1]['after']['value'])['content'])
                ar=assessor.assess(r,rs[0]['beganAt'],draft,'Obsidian',rs[0]['before']['value'])
                assessments.append({'eventID':r['sourceEventID'],'status':ar['status'],'unexplainedRuns':ar.get('boundaryEvidence',{}).get('unexplainedRuns',[])})
            d['readAssessments']=assessments
        except (ValueError,KeyError,TypeError,IndexError) as e:d.update(reconstructible=False,rejection=str(e))
        output.append(d)
    b.h.save(a.output,{'scope':'All adjacent same-note WRITE neighborhoods touching an eligible target; reconstruction is necessary, not sufficient for one thought','pairs':output})
    for d in output:
        if d['reconstructible']:print(json.dumps(d,ensure_ascii=False))
    print('Total',len(output),'reconstructible',sum(d['reconstructible'] for d in output))

if __name__=='__main__':main()

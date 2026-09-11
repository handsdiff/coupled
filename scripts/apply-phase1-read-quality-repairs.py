#!/usr/bin/env python3
"""Materialize reviewed READ corrections and bound native-order permutations.

Experiment curation, not a collector heuristic. Preserve the old diagnostic arm,
all WRITE contracts, source timestamps and raw lineage. A duplicate is suppressed
only when its retained predecessor is present; its corrected full fallback stays.
"""
import argparse
from collections import Counter
from copy import deepcopy
import importlib.util
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
spec=importlib.util.spec_from_file_location('read_batch',ROOT/'scripts/apply-phase1-reviewed-batch.py')
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)


def exact_adjacent_repeats(events, protected, write_intervals=None):
    """Recheck exact repeats and contiguous suffix/prefix overlap after repair.

    The predecessor must be the immediately preceding semantic event in this
    session, on the same named source. A WRITE interval also blocks this rule.
    Full payloads survive for dependency-aware packing when history is cropped.
    """
    previous=None;changes=[]
    writes=list(write_intervals) if write_intervals is not None else [e for e in events.values() if e['kind']=='write']
    b.h.require(all('beganAt' in w and 'availableAt' in w for w in writes),'Missing full WRITE intervals')
    for e in sorted(events.values(),key=lambda v:(v['sessionID'],v['availableAt'],v['sourceEventID'])):
        p=previous;previous=e
        if not p or e['kind']!='read' or p['kind']!='read' or e['sessionID']!=p['sessionID']:continue
        if e['sourceEventID'] in protected or p['availableAt']>=e['availableAt']:continue
        a=json.loads(p['serialized']);z=json.loads(e['serialized'])
        if not isinstance(z.get('content'),str) or not z['content'].strip():continue
        if {k:v for k,v in a.items() if k!='content'}!={k:v for k,v in z.items() if k!='content'}:continue
        prior=a.get('content');current=z['content']
        if not isinstance(prior,str):continue
        overlap=0
        if prior==current:delta='';overlap=len(current)
        else:
            left,right=prior.split('\n'),current.split('\n');matched=0
            for size in range(min(len(left),len(right)),1,-1):
                if left[-size:]==right[:size] and len('\n'.join(right[:size]))>=80:
                    matched=size;break
            if not matched:continue
            delta='\n'.join(right[matched:]);overlap=len('\n'.join(right[:matched]))
        if any(w['sessionID']==e['sessionID'] and w['beganAt']<=e['availableAt'] and w['availableAt']>=p['availableAt'] for w in writes):continue
        n=e.get('readNovelty',{})
        if n.get('content')==delta and n.get('dependsOnEventID')==p['sourceEventID']:continue
        e['readNovelty']={**n,'currentEventID':e['sourceEventID'],'content':delta,
            'decision':'emit_contiguous_new_content' if delta else 'suppress_no_new_content',
            'reason':'exact_adjacent_same_source_after_read_quality_repair','dependsOnEventID':p['sourceEventID'],
            'currentCharacterCount':len(current),'overlapCharacterCount':overlap}
        changes.append({'eventID':e['sourceEventID'],'previousEventID':p['sourceEventID'],
            'rule':'exact_adjacent_same_source_contiguous_overlap_no_intervening_or_active_write',
            'overlapCharacterCount':overlap,'remainingCharacterCount':len(delta),
            'fullPayloadSHA256':b.digest(z)})
    return changes


def materialize(a, *, verify_only=False):
    manifest=json.loads((a.input/'review.json').read_text())
    for name,digest in manifest['artifactsSHA256'].items():
        b.h.require(b.h.sha(a.input/name)==digest,'Input changed: '+name)
    source={r['sourceEventID']:r for r in b.h.rows(a.input/'new/events.jsonl')}
    updated=deepcopy(source);proofs={};hashes={str(a.input/'review.json'):b.h.sha(a.input/'review.json'),str(a.manual):b.h.sha(a.manual)}
    if a.order_audit:
        audit=json.loads((a.order_audit/'audit.json').read_text())
        for name,digest in audit['artifactsSHA256'].items():
            b.h.require(b.h.sha(a.order_audit/name)==digest,'Order evidence changed')
        for path,digest in audit['sourceHashes'].items():
            b.h.require(b.h.sha(Path(path))==digest,'Order source changed')
        hashes[str(a.order_audit/'audit.json')]=b.h.sha(a.order_audit/'audit.json')
        for r in b.h.rows(a.order_audit/'read-changes.jsonl'):
            eid=r['eventID'];b.h.require(b.digest(source[eid])==r['beforeSHA256'],'Wrong native-order parent')
            updated[eid]=r['event'];proofs[eid]=[r['proof']]
    manual=json.loads(a.manual.read_text())
    b.h.require(manual['version']=='read-quality-adjudication-v1','Wrong review version')
    for r in manual['reads']:
        eid=r['eventID'];e=updated[eid]
        b.h.require(e['kind']=='read','Not a READ')
        for image in r['images']:
            path=ROOT/image['path'];digest=b.h.sha(path)
            b.h.require(digest==image['sha256'],'Screenshot changed')
            b.h.require(image['capturedAt']<=e['availableAt'],'Future evidence')
            hashes[str(path)]=digest
        b.h.require(r.get('reason') and r['images'],'Missing visual adjudication')
        payload=json.loads(e['serialized']);text=payload['content']
        if r.get('replaceContent') is not None:
            b.h.require(r['beforeContentSHA256']==hashlib.sha256(text.encode()).hexdigest(),'Manual content parent differs')
            text=r['replaceContent']
        else:
            for old,new in r.get('edits',[]):
                b.h.require(text.count(old)==1,'Correction not unique: '+old)
                text=text.replace(old,new)
            for removal in r.get('removeLines',[]):
                line=removal if isinstance(removal,str) else removal['text']
                count=1 if isinstance(removal,str) else removal['count']
                lines=text.split('\n');b.h.require(count>0 and lines.count(line)==count,'Reviewed line count differs: '+line)
                text='\n'.join(value for value in lines if value!=line)
        if r.get('lineOrder'):
            b.h.require(Counter(r['lineOrder'])==Counter(text.split('\n')),'Reviewed ordering changes text')
            text='\n'.join(r['lineOrder'])
        e,_=b.load('assemble-phase1-curated-review').patch_read(e,[(payload['content'],text,1)])
        if r.get('source'):
            for key in ('serialized','auditSerialized'):
                if key in e:
                    value=json.loads(e[key]);value['source']=r['source'];e[key]=b.p.canonical_json(value)
        updated[eid]=e;proofs.setdefault(eid,[]).append(r)
    # The user-approved golden records are frozen at this iteration's input.
    golden=json.loads(Path(manual['goldenReview']).read_text())
    protected={r['eventID'] for r in golden['cases']}
    held=[]
    for eid in protected & proofs.keys():
        if updated[eid]!=source[eid]:
            held.append({'eventID':eid,'reason':'previous_user_acceptance_requires_new_review','proposals':proofs.pop(eid)})
            updated[eid]=source[eid]
    for d in manual.get('duplicates',[]):
        eid,pid=d['eventID'],d['previousEventID'];e=updated[eid];prev=updated[pid]
        b.h.require(e['sessionID']==prev['sessionID'] and prev['availableAt']<e['availableAt'],'Noncausal repeat')
        interval=[x for x in source.values() if x['sessionID']==e['sessionID'] and prev['availableAt']<x['availableAt']<e['availableAt']]
        b.h.require(not interval,'Not an adjacent event pair')
        content=json.loads(e['serialized'])['content'];prior=json.loads(prev['serialized'])['content']
        b.h.require(content==prior,'Repeat payload differs after reviewed normalization')
        e['readNovelty']={**e.get('readNovelty',{}),'content':'','decision':'suppress_no_new_content','reason':'reviewed_same_screen_no_new_content',
            'dependsOnEventID':pid,'currentCharacterCount':len(content),'overlapCharacterCount':len(content)}
        proofs.setdefault(eid,[]).append(d)
    exact_repeats=exact_adjacent_repeats(updated,protected,b.h.rows(a.input/'common-write-events.jsonl'))
    for proof in exact_repeats:proofs.setdefault(proof['eventID'],[]).append(proof)
    changes=[{'eventID':eid,'beforeSHA256':b.digest(source[eid]),'event':updated[eid],
              'review':{'version':'retained-read-quality-v1','proofs':proofs[eid]}}
             for eid in sorted(proofs) if updated[eid]!=source[eid]]
    for r in changes:
        old=source[r['eventID']];new=r['event']
        b.h.require(all(old[k]==new[k] for k in ['sourceEventID','sessionID','sourceRecordIDs','availableAt','kind']),'READ evidence identity/time changed')
    for path in [Path(__file__).resolve(),ROOT/'scripts/apply-phase1-reviewed-batch.py',
                 ROOT/'scripts/assemble-phase1-curated-review.py',ROOT/'scripts/construct-phase1-closed-episode-corpus.py',
                 ROOT/'scripts/construct-phase1-raw-episode-corpus.py',ROOT/'scripts/phase1_read_boundary.py',
                 ROOT/'scripts/phase1_read_novelty.py',ROOT/'scripts/phase1_example_storage.py']:
        hashes[str(path)]=b.h.sha(path)
    write_changes=[];write_spec=None
    write_path=getattr(a,'write_reviews',None)
    if write_path:
        write_spec=json.loads(write_path.read_text())
        write_changes,extra_reads,extra_hashes=b.make_batch(a.input,write_spec,
            read_overrides={r['eventID']:r['event'] for r in changes})
        b.h.require(not extra_reads,'Keep READ adjudication in the authoritative READ review')
        hashes.update(extra_hashes);hashes[str(write_path)]=b.h.sha(write_path)
    spec={'version':'read-quality-materialization-v1','orderAudit':str(a.order_audit),
          'manualReview':str(a.manual),'reads':manual['reads'],'writes':write_spec['writes'] if write_spec else [],
          'writeReview':str(write_path) if write_path else None,'goldenProposalsHeld':held,
          'exactAdjacentRepeats':exact_repeats}
    if verify_only:return write_changes,changes,spec,hashes
    b.render(a.input,a.output,spec,write_changes,changes,hashes)


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input',type=Path,required=True)
    ap.add_argument('--manual',type=Path,required=True)
    ap.add_argument('--order-audit',type=Path)
    ap.add_argument('--write-reviews',type=Path,help='Validate WRITE joins against corrected READ evidence, then render once')
    ap.add_argument('--output',type=Path,required=True)
    materialize(ap.parse_args())


if __name__=='__main__':main()

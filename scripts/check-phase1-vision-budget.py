#!/usr/bin/env python3
"""Offline independent joins/counts plus whole-prefix packing regressions."""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import tiktoken
from phase1_read_model_comparison import canonical,file_hash,fingerprint,rows

spec=importlib.util.spec_from_file_location('vision_budget',Path(__file__).with_name('prepare-phase1-vision-budget.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--data',type=Path,required=True);p.add_argument('--report',type=Path,required=True);a=p.parse_args()
    plan=m.load(a.data/'plan.json');pilot=Path(plan['sourcePilot'])
    for n,h in m.load(a.data/'artifact-hashes.json').items():assert file_hash(a.data/n)==h
    assert file_hash(pilot/'plan.json')==plan['sourcePlanSHA256']
    assert file_hash(pilot/'predictions.jsonl')==plan['sourcePredictionsSHA256']
    old={(r['case'],r['variant']):r for r in rows(pilot/'data/prompts.local.jsonl')}
    usage={(r['case'],r['variant']):r['usage']['input_tokens'] for r in rows(pilot/'predictions.jsonl')}
    frozen=Path(m.load(pilot/'data/plan.json')['source'])/'frozen'
    assert file_hash(frozen/'new-context-events.jsonl')==plan['sourceEventArtifactSHA256']
    assert file_hash(frozen/'cohort.jsonl')==plan['sourceCohortSHA256']
    cohort={r['exampleID']:r for r in rows(frozen/'cohort.jsonl') if any(r['exampleID']==o['exampleID'] for o in old.values())}
    events={r['sourceEventID']:r for r in rows(frozen/'new-context-events.jsonl')}
    enc=tiktoken.get_encoding('o200k_base');seen=set();matched_images=0
    for r in rows(a.data/'prompts.local.jsonl'):
        key=(r['case'],r['variant']);assert key not in seen;seen.add(key);o=old[key];d=r['budgetPacking'];n=d['addedEventCount']
        assert r['partsSHA256']==fingerprint(r['parts'])
        assert r['parts'][0]==o['parts'][0] and r['parts'][n+1:]==o['parts'][1:]
        added=r['parts'][1:n+1];added_tokens=sum(len(enc.encode_ordinary(t['text'])) for t in added)
        assert d['estimatedInputTokens']==usage[key]+added_tokens<=usage[r['case'],'screenshots_recent']
        assert d['addedTextTokens']==added_tokens and n==len(d['addedEventIDs'])
        c=cohort[r['exampleID']];ids=d['addedEventIDs'];forbidden={c['targetEventID'],*c['episode']['memberWriteEventIDs']}
        assert not forbidden.intersection(ids) and all(i in c['contextBlockIDs'] for i in ids)
        assert [i for i in c['contextBlockIDs'] if i in set(ids)]==ids
        assert all(events[i]['availableAt']<r['targetBeganAt'] for i in ids)
        assert r['parts'][-1]['text']==c['query']
        if r['variant']=='screenshots_recent':assert n==0 and r['partsSHA256']==o['partsSHA256'];matched_images+=1
    assert len(seen)==207 and matched_images==69
    # Greedy whole-event suffixes: stop rather than skip/pad a too-large event.
    def render(blocks,events,encode):return copy.deepcopy(blocks),{}
    ev={str(i):{'serialized':'x'*size} for i,size in enumerate([20,3,4])}
    b,_,reason,_=m.pack_prefix(['0','1','2'],ev,10,len,render)
    assert [x['eventID'] for x in b]==['1','2'] and reason=='next_whole_event_exceeds_budget'
    b,_,reason,_=m.pack_prefix(['0','1','2'],ev,100,len,render)
    assert len(b)==3 and reason=='all_earlier_history_used'
    assert m.pack_prefix([],ev,100,len,render)[0]==[]
    result={'status':'passed','providerCalls':0,'prompts':207,'cases':69,'unchangedScreenshotControls':69,
        'exactRecentPromptPreservation':True,'wholeEventPrefixLineage':True,'causalAndTargetMemberExclusion':True,
        'tokenAccounting':True,'underfillAndNoPaddingTests':True,'preparedPlanSHA256':file_hash(a.data/'plan.json')}
    m.save(a.report,result);print(canonical(result))


if __name__=='__main__':main()

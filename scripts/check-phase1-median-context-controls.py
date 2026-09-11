#!/usr/bin/env python3
"""Exhaustive, offline audit of the median-budget experiment's actual inputs."""
import argparse
from collections import Counter
from functools import lru_cache
import json
import math
from pathlib import Path
import statistics
from unittest.mock import patch

from phase1_read_model_comparison import file_hash, fingerprint, canonical, rows
from phase1_vision_raw_history import pilot_module
import importlib.util

spec=importlib.util.spec_from_file_location('median_prep',Path(__file__).with_name('prepare-phase1-median-context-controls.py'))
prep=importlib.util.module_from_spec(spec);spec.loader.exec_module(prep)


def unit_checks():
    # No time-group splitting, no skipping an expensive middle group.
    timeline=[{'at':str(i),'id':str(i),'cost':v} for i,v in enumerate((1,100,3,3))]
    chosen,cost,next_cost=prep.controls.choose_suffix(timeline,10,lambda x:x['cost'])
    assert [x['id'] for x in chosen]==['2','3'] and cost==6 and next_cost==106
    timeline=[{'at':'a','cost':3},{'at':'a','cost':3},{'at':'b','cost':3}]
    assert len(prep.controls.choose_suffix(timeline,8,lambda x:x['cost'])[0])==1
    # Median uses the whole prespecified cohort, not just feasible image cases.
    assert statistics.median([1,10,1000])==10
    with patch('socket.socket.connect',side_effect=AssertionError('No network')):
        assert prep.controls.offline_encoding().encode_ordinary('offline')
    renderer=prep.module(Path(__file__).with_name('phase1_read_novelty.py'),'unit_novelty_renderer')
    e0={'sourceEventID':'a','kind':'read','availableAt':'1','serialized':canonical({'kind':'read','content':'a'*160})}
    e1={'sourceEventID':'b','kind':'read','availableAt':'2','serialized':canonical({'kind':'read','content':'a'*160+'b'*20}),
        'readNovelty':{'currentEventID':'b','dependsOnEventID':'a','decision':'emit_new_content','content':'b'*20}}
    e2={'sourceEventID':'c','kind':'write','availableAt':'3','serialized':canonical({'kind':'write','content':'yes'})}
    events=[e0,e1,e2];mapping={e['sourceEventID']:e for e in events}
    allowance=len(e1['serialized']+'\n')+len(e2['serialized']+'\n')
    selected,cost,overflow,decisions=prep.pack_clean(events,mapping,allowance,len,renderer.apply_dependency_aware_read_rendering)
    assert [b['eventID'] for b in selected]==['b','c'] and selected[0]['serialized']==e1['serialized']
    assert cost==allowance and overflow>allowance and decisions['completeFallbackDependencyUnavailable']==1
    selected,_,overflow,_=prep.pack_clean(events,mapping,10000,len,renderer.apply_dependency_aware_read_rendering)
    assert len(selected)==3 and json.loads(selected[1]['serialized'])['content']=='b'*20 and overflow is None
    e1['availableAt']='3';e2['availableAt']='2'
    selected,_,_,_=prep.pack_clean(events,mapping,10000,len,renderer.apply_dependency_aware_read_rendering)
    assert [b['eventID'] for b in selected]==['a','b','c'], 'Do not reorder native episodes by final availability'


def audit(root):
    unit_checks(); load=prep.load
    plan=load(root/'plan.json'); budget=plan['sharedBudgetInputTokens']
    assert plan['version']==prep.VERSION and not plan['dispatchAuthorized'] and plan['providerCalls']==0
    assert plan['fourConditions']==list(prep.NEW_CONDITIONS) and plan['baseline']=='time_cleaned'
    for path,digest in load(root/'artifact-hashes.json').items():
        assert file_hash(root/path)==digest
    for path,digest in {**plan['sourceHashes'],**plan['sourceRawDigests'],**plan['localOCRSidecarsSHA256']}.items():
        assert file_hash(path)==digest
    source=Path(plan['sourcePreparation']); pilot=Path(plan['sourcePilot'])
    frozen=Path(load(pilot/'data/plan.json')['source'])/'frozen'
    for path,digest in load(frozen/'artifact-hashes.json').items():
        assert file_hash(frozen/path)==digest
    config=load(frozen.parent/'config.json')
    renderer=prep.module(Path(config['frozenProducer'])/'scripts/phase1_read_novelty.py','audit_frozen_renderer')
    baselines=prep.controls.Index(frozen/'prompts.jsonl',lambda x:(x['exampleID'],x['variant']))
    originals=prep.controls.Index(pilot/'data/prompts.local.jsonl',lambda x:(x['case'],x['variant']))
    results={(r['case'],r['variant']):r for r in rows(pilot/'predictions.jsonl')}
    events={e['sourceEventID']:e for e in rows(frozen/'new-context-events.jsonl')}
    cohort={e['exampleID']:e for e in rows(frozen/'cohort.jsonl')}
    # Independently enumerate all source frames, not just the prepared subset.
    raw_frames={f['recordID']:f for fs in pilot_module().read_evidence(
        [p for p in plan['sourceRawDigests'] if p.endswith('/raw.jsonl')]).values() for f in fs}
    frames={f['recordID']:f for f in rows(root/'frames.jsonl')}
    enc=prep.controls.offline_encoding()
    @lru_cache(maxsize=32768)
    def count(s):return len(enc.encode_ordinary(s))
    checked_images=set()
    for ident,f in frames.items():
        source_frame=raw_frames[ident]
        for key in ('capturedAt','screenshotSHA256','screenshotPath','width','height','sessionID','source'):
            assert f[key]==source_frame[key]
        if f['screenshotSHA256'] not in checked_images:
            assert file_hash(f['screenshotPath'])==f['screenshotSHA256']
            checked_images.add(f['screenshotSHA256'])
        o=f['ocr']
        assert o['capturedAt']==f['capturedAt'] and o['screenshotSHA256']==f['screenshotSHA256']
        assert not o['contentWasTruncated'] and not any(o['cropFractions'].values())
        if source_frame['ocr'] is not None:
            assert o==source_frame['ocr'], 'Recorded raw OCR replaced by rerun'
    saved=load(root/'cases.json');cases=sorted({r['case'] for r in saved})
    assert len(cases)==69 and Counter(r['arm'] for r in saved)=={a:69 for a in prep.ARMS}
    assert not set(cases).intersection({118,126,127})
    image_estimates=[]; reuses=[]; blocked=[]; underfilled={}; frame_occurrences=0; clean_blocks=0; measured_images=0
    def history_writes(payload):
        return [p['text'] for p in payload['parts'][1:-1] if p['type']=='input_text'
                and json.loads(p['text']).get('kind') not in ('read','read_observation')]
    for case in cases:
        payloads={a:load(root/f'case-{case:04d}/{a}.json') for a in prep.ARMS}
        ref=payloads['time_cleaned'];ex=cohort[ref['exampleID']];cutoff=ex['targetBeganAt']
        base=baselines.get((ex['exampleID'],'new'));floor=min(b['availableAt'] for b in base['retainedBlocks'])
        native=[events[i] for i in ex['contextBlockIDs']]
        forbidden={ex['targetEventID'],*ex['episode']['memberWriteEventIDs']}
        assert not forbidden.intersection(ex['contextBlockIDs'])
        assert all(e['availableAt']<cutoff for e in native)
        sessions={e['sessionID'] for e in native if e.get('sessionID')}|{ex['sessionID']}
        eligible=[f for f in raw_frames.values() if f['sessionID'] in sessions and f['capturedAt']<cutoff]
        time_ids=[i for _,i in sorted((f['capturedAt'],f['recordID']) for f in eligible if f['capturedAt']>=floor)]
        assert payloads['time_ocr']['frameRecordIDs']==payloads['time_images']['frameRecordIDs']==time_ids
        assert history_writes(payloads['time_ocr'])==history_writes(payloads['time_images'])==history_writes(ref)
        for arm,p in payloads.items():
            assert p['partsSHA256']==fingerprint(p['parts']) and p['sharedBudgetInputTokens']==budget
            assert p['query']==ex['query']==ref['query'] and p['target']==ref['target']
            assert p['targetSHA256']==fingerprint(p['target']) and p['cutoffExclusive']==cutoff
            assert p['parts'][0]==ref['parts'][0] and p['parts'][-1]==ref['parts'][-1]
            assert p['parts'][-1]['text']==p['query'] and not p['dispatchAuthorized']
            assert p['wirePayloadBytes']==prep.controls.wire_size(p['parts'])
            covered=[]
            for item in p['items']:
                assert item['at']<cutoff
                covered.extend(range(item['partStart'],item['partEnd']))
            assert covered==list(range(1,len(p['parts'])-1)), 'Unrepresented/duplicated context parts'
            if arm.startswith('time_'):
                original=load(source/f'case-{case:04d}/{arm}.json')
                assert p['parts']==original['parts'] and p['fullIntervalRetained']
                assert p['intervalStartAt']==floor
            else:
                assert p['assignedInputTokens']==budget and not p['fullIntervalRetained']
                assert p['estimatedInputTokens']<=budget-prep.MARGIN
                assert all(x['type']=='input_text' for x in p['parts'])
                packing=p['packing'];allowance=packing['historyAllowanceTokens']
                assert packing['budget']==budget and packing['guardTokens']==prep.MARGIN
                assert count(p['parts'][0]['text'])+count(p['parts'][-1]['text'])+packing['calibratedOverheadTokens']==packing['fixedInputTokens']
                history_cost=sum(count(x['text']) for x in p['parts'][1:-1])
                assert history_cost==packing['historyTokens'] and history_cost+packing['fixedInputTokens']==p['estimatedInputTokens']
                if arm=='budget_cleaned':
                    timeline=[(e['availableAt'],e['sourceEventID']) for e in native]
                    selected=[events[i['id']] for i in p['items']]
                    rendered,decisions=prep.render_clean(selected,events,count,renderer.apply_dependency_aware_read_rendering)
                    assert [b['serialized']+'\n' for b in rendered]==[x['text'] for x in p['parts'][1:-1]]
                    assert decisions==packing['readRenderingCounts']
                    clean_blocks+=len(selected)
                else:
                    timeline=[(e['availableAt'],False,e['sourceEventID']) for e in native if e['kind']!='read']
                    timeline+= [(f['capturedAt'],True,f['recordID']) for f in eligible]
                    timeline.sort();timeline=[(t[0],t[2]) for t in timeline]
                start=len(timeline)-len(p['items'])
                assert [i['id'] for i in p['items']]==[t[1] for t in timeline[start:]]
                assert start==0 or start==len(timeline) or timeline[start-1][0]!=timeline[start][0]
                assert packing['eligibleItems']==len(timeline) and packing['allAvailableHistoryUsed']==(start==0)
                assert p['intervalStartAt']==min((t[0] for t in timeline[start:]),default=cutoff)
                if start:
                    previous=start-1
                    while previous and timeline[previous-1][0]==timeline[start-1][0]:previous-=1
                    if arm=='budget_cleaned':
                        trial,_=prep.render_clean([events[i] for _,i in timeline[previous:]],events,count,renderer.apply_dependency_aware_read_rendering)
                        next_cost=sum(len(b['tokenIDs']) for b in trial)
                    else:
                        # Independently render the immediately excluded raw group.
                        original_events=Path(config['newCorpus'])/'events.jsonl'
                        if 'privacy' not in locals():
                            privacy=prep.Privacy({r['sourceEventID']:r for r in rows(original_events)},config['privacyPolicy'])
                        extra=0
                        for _,ident in timeline[previous:start]:
                            if ident in raw_frames:
                                # The overflow group is captured in OCR sidecars,
                                # but intentionally absent from the retained payload.
                                f=raw_frames[ident]
                                if f.get('ocr') is None:
                                    if 'sidecars' not in locals():
                                        sidecars={load(path)['binding']['frameRecordID']:load(path) for path in plan['localOCRSidecarsSHA256']}
                                        for old in rows(pilot/'data/frames.jsonl'):
                                            if old.get('ocr'):sidecars.setdefault(old['recordID'],old['ocr'])
                                    f=dict(f,ocr=sidecars[ident])
                                b=prep.raw_block(f,f['ocr'],privacy);text=b['serialized']+'\n'
                            else:text=events[ident]['serialized']+'\n'
                            extra+=count(text)
                        next_cost=history_cost+extra
                    assert next_cost==packing['nextWholeTimestampHistoryTokens'] and next_cost>allowance
                else:
                    assert packing['nextWholeTimestampHistoryTokens'] is None
                    underfilled.setdefault(arm,[]).append(case)
            if not arm.endswith('cleaned'):
                assert p['cleanedReadCount']==0
                assert all(json.loads(x['text']).get('kind')!='read' for x in p['parts'][1:-1] if x['type']=='input_text')
            for item in p['items']:
                if item['kind']=='read_observation':
                    ident=item['id'];f=frames[ident];obs=json.loads(p['parts'][item['partStart']]['text'])
                    assert obs['capturedAt']==f['capturedAt']==item['at']
                    frame_occurrences+=1
                    if not obs.get('privacy'):
                        assert obs['source']==f['source']
                        if arm=='time_images':
                            image=p['parts'][item['partStart']+1]
                            assert image['sha256']==f['screenshotSHA256'] and image['path']==f['screenshotPath'] and image['detail']=='original'
                        else:
                            assert obs['content']==f['ocr']['content'] and obs['ocrAvailable']
            if p['reuse']:
                record=p['reuse'];path=Path(record['run'])/'predictions.jsonl'
                assert file_hash(path)==record['predictionsSHA256']
                matches=[r for r in rows(path) if r['sampleID']==record['sampleID']]
                assert len(matches)==1 and matches[0]['case']==case and matches[0]['partsSHA256']==p['partsSHA256']
                reuses.append([case,arm])
            if p['limits']:
                assert p['status']=='blocked_input_limits';blocked.append([case,arm])
        # Independently rebuild the screenshot token estimate from dimensions,
        # local text and the old measured image request's framing overhead.
        prior=originals.get((case,'screenshots_recent'));r=results[case,'screenshots_recent']
        sizes={f['screenshotSHA256']:(f['width'],f['height']) for f in raw_frames.values()}
        def image_tokens(p):
            w,h=sizes[p['sha256']]
            return (math.ceil(w/32)*math.ceil(h/32)*6)//5+1
        attributed=[v['content'] for v in r['usage']['attribution']['items'].values() if len(v.get('content',[]))==len(prior['parts'])]
        assert len(attributed)==1
        for part,usage in zip(prior['parts'],attributed[0]):
            if part['type']=='local_image':
                assert image_tokens(part)==usage['input_tokens']
                measured_images+=1
        overhead=r['usage']['input_tokens']-sum(count(p['text']) if p['type']=='input_text' else image_tokens(p) for p in prior['parts'])
        image_input=payloads['time_images']
        estimate=overhead+sum(count(p['text']) if p['type']=='input_text' else image_tokens(p) for p in image_input['parts'])
        assert estimate==image_input['estimatedInputTokens']
        image_estimates.append(estimate)
    assert budget==statistics.median(image_estimates)==load(root/'budget-evidence.json')['budgetInputTokens']
    requests=load(root/'requests.proposed.json')
    assert len(requests)==276 and Counter(r['arm'] for r in requests)=={a:69 for a in prep.NEW_CONDITIONS}
    assert all(not r['dispatchAuthorized'] for r in requests)
    assert blocked==[[41,'time_images']]
    return {'status':'passed','cases':69,'completePayloads':345,'additionalConditionInputs':276,
        'sharedBudgetInputTokens':budget,'BRecomputedFromAll69ImageInputs':True,
        'unchangedTimeMatchedInputs':207,'identicalTimeMatchedRawFrameSets':69,
        'identicalTimeMatchedHistoricalWrites':True,'wholeHistoryReadRepresentationVerified':True,
        'nativeCleanedOrderAndRawChronologicalSuffixVerified':True,'nextOverflowVerified':True,'fullQueryAndStrictCausalityVerified':True,
        'cleanedBlocksDependencyRendererVerified':clean_blocks,'fullWindowRawReadOccurrencesVerified':frame_occurrences,
        'uniqueScreenshotHashesVerified':len(checked_images),'exactCompletedReuses':len(reuses),
        'priorMeasuredImageAttributionsMatchingEstimator':measured_images,
        'allAvailableHistoryUsedCases':underfilled,'blockedConditions':blocked,'providerCalls':0,
        'planSHA256':file_hash(root/'plan.json')}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--data',type=Path);p.add_argument('--report',type=Path);a=p.parse_args()
    with patch('socket.socket.connect',side_effect=AssertionError('Audit must remain offline')):
        result=audit(a.data) if a.data else (unit_checks() or {'status':'unit_checks_passed'})
    if a.report:prep.save(a.report,result)
    print(json.dumps(result,sort_keys=True))

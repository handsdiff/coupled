#!/usr/bin/env python3
"""Independent offline gates for the corrected raw/cleaned budget comparison."""
import argparse
from collections import Counter
import json
from pathlib import Path
import tiktoken

from phase1_read_model_comparison import Privacy,canonical,file_hash,fingerprint,rows
from phase1_vision_raw_history import pack_raw_suffix,pilot_module,raw_block


def load(p):return json.loads(Path(p).read_text())


def check(data):
    plan=load(data/'plan.json');assert plan['version']=='phase1-vision-budget-v2'
    for n,h in load(data/'artifact-hashes.json').items():assert file_hash(data/n)==h
    pilot=Path(plan['sourcePilot']);paused=Path(plan['supersededHybridRun'])
    assert file_hash(pilot/'plan.json')==plan['sourcePlanSHA256']
    assert file_hash(pilot/'predictions.jsonl')==plan['sourcePredictionsSHA256']
    old_plan=load(pilot/'data/plan.json');frozen=Path(old_plan['source'])/'frozen'
    config=load(frozen.parent/'config.json')
    events={e['sourceEventID']:e for e in rows(frozen/'new-context-events.jsonl')}
    assert file_hash(frozen/'new-context-events.jsonl')==plan['sourceEventArtifactSHA256']
    original=Path(config['newCorpus'])/'events.jsonl'
    assert file_hash(original)==load(frozen/'plan.json')['sourceHashes'][str(original)]
    privacy=Privacy({e['sourceEventID']:e for e in rows(original)},config['privacyPolicy'])
    cases={c['case']:c for c in rows(data/'cases.jsonl')}
    original_cases={c['case']:c for c in rows(pilot/'data/cases.jsonl')}
    ids={c['exampleID'] for c in cases.values()}
    cohort={c['exampleID']:c for c in rows(frozen/'cohort.jsonl') if c['exampleID'] in ids}
    old={(p['case'],p['variant']):p for p in rows(pilot/'data/prompts.local.jsonl')}
    clean={p['case']:p for p in rows(paused/'data/prompts.local.jsonl') if p['variant']=='cleaned_recent'}
    baseline={(r['case'],r['variant']):r for r in rows(pilot/'predictions.jsonl')}
    frames={f['recordID']:f for f in rows(data/'frames.jsonl')}
    for p,h in plan['sourceRawDigests'].items():assert file_hash(p)==h
    all_frames=[f for group in pilot_module().read_evidence([p for p in plan['sourceRawDigests'] if p.endswith('/raw.jsonl')]).values() for f in group]
    blocks={r['case']:r['blocks'] for r in rows(data/'raw-history-blocks.jsonl')}
    enc=tiktoken.get_encoding('o200k_base');count=lambda s:len(enc.encode_ordinary(s))
    seen=set();raw_reads=0;redacted=0
    for p in rows(data/'prompts.local.jsonl'):
        key=(p['case'],p['variant']);assert key not in seen;seen.add(key)
        c=cases[p['case']];ex=cohort[c['exampleID']];b=p['budgetPacking']
        assert p['partsSHA256']==fingerprint(p['parts']) and all(x['type']=='input_text' for x in p['parts'])
        assert p['parts'][0]==old[key]['parts'][0] and p['parts'][-1]['text']==c['query']==ex['query']
        assert c==original_cases[c['case']] and c['targetSHA256']==ex['targetSHA256']
        assert not privacy.unsafe(''.join(x['text'] for x in p['parts']))
        assigned=baseline[p['case'],'screenshots_recent']['usage']['input_tokens']
        overhead=baseline[key]['usage']['input_tokens']-sum(count(x['text']) for x in old[key]['parts'])
        assert b['estimatedInputTokens']==overhead+sum(count(x['text']) for x in p['parts'])<=assigned
        if p['variant']=='cleaned_recent':
            assert p['parts']==clean[p['case']]['parts'] and p['partsSHA256']==clean[p['case']]['partsSHA256']
            continue
        selected=blocks[p['case']];assert p['parts'][1:-1]==[{'type':'input_text','text':x['serialized']+'\n'} for x in selected]
        assert all(x['availableAt']<c['targetBeganAt'] and x['kind']!='read' for x in selected)
        assert len({x['eventID'] for x in selected})==len(selected)
        native=[events[i] for i in ex['contextBlockIDs']]
        sessions={e['sessionID'] for e in native if e.get('sessionID')}|{ex['sessionID']}
        timeline=[(f['capturedAt'],1,f['recordID']) for f in all_frames if f['sessionID'] in sessions and f['capturedAt']<c['targetBeganAt']]
        timeline += [(e['availableAt'],0,e['sourceEventID']) for e in native if e['kind']!='read']
        timeline.sort()
        assert [x[2] for x in timeline[-len(selected):]]==[x['eventID'] for x in selected]
        if len(selected)<len(timeline):assert timeline[-len(selected)-1][0]!=selected[0]['availableAt']
        forbidden={ex['targetEventID'],*ex['episode']['memberWriteEventIDs']}
        for x in selected:
            assert x['eventID'] not in forbidden
            if x['kind']=='read_observation':
                f=frames[x['eventID']];assert f['ocr'] is not None
                assert file_hash(f['screenshotPath'])==f['screenshotSHA256']
                assert not f['ocr']['contentWasTruncated'] and not any(f['ocr']['cropFractions'].values())
                assert f['ocr']['capturedAt']==f['capturedAt'] and f['ocr']['screenshotSHA256']==f['screenshotSHA256']
                assert x==raw_block(f,f['ocr'],privacy)
                raw_reads+=1;redacted+=bool(x['privacyRedacted'])
            else:
                assert x['eventID'] in ex['contextBlockIDs'] and x['serialized']==events[x['eventID']]['serialized']
    assert len(seen)==138 and Counter(v for _,v in seen)=={'cleaned_recent':69,'raw_ocr_recent':69}
    requests=list(rows(data/'requests.proposed.jsonl'))
    assert {(r['case'],r['variant']) for r in requests}==seen
    assert all(r['variant']!='screenshots_recent' for r in requests)
    for r in rows(data/'baseline-screenshots.jsonl'):
        assert r['prediction']==baseline[r['case'],'screenshots_recent']
    # No cleaned fallback; keep repetitions; stop on the first large group.
    frames_fixture=[{'recordID':str(i),'capturedAt':str(i)} for i in range(4)]
    def observed(f):return {'eventID':f['recordID'],'kind':'read_observation','availableAt':f['capturedAt'],'serialized':'x'*(20 if f['recordID']=='1' else 3)}
    selected,_,reason,_=pack_raw_suffix(frames_fixture,[],9,len,observed)
    assert [x['eventID'] for x in selected]==['2','3'] and reason=='next_whole_timestamp_exceeds_budget'
    try:pack_raw_suffix([], [{'sourceEventID':'bad','kind':'read','availableAt':'0','serialized':'clean'}],100,len,observed)
    except AssertionError:pass
    else:raise AssertionError('Accepted cleaned READ in raw arm')
    for path,h in plan['localOCRSidecarsSHA256'].items():assert file_hash(path)==h
    return {'status':'passed','providerCalls':0,'cases':69,'textPrompts':138,'newScreenshotRequests':0,
        'reusedScreenshotBaseline':69,'cleanedPromptsByteIdentical':69,'rawFrameOccurrences':raw_reads,
        'rawContainsCleanedREADs':False,'privacyRedactedRawOccurrences':redacted,
        'sameInstructionQueryTarget':True,'causalWholeTimestampSuffix':True,'noSkippedOversizeGroups':True,
        'fullWindowOCRBindings':True,'tokenBudgetAudit':True,'preparedPlanSHA256':file_hash(data/'plan.json')}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--data',required=True,type=Path);p.add_argument('--report',required=True,type=Path);a=p.parse_args()
    r=check(a.data)
    with a.report.open('x') as out:out.write(json.dumps(r,indent=2,sort_keys=True)+'\n')
    print(canonical(r))

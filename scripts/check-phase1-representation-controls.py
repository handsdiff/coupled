#!/usr/bin/env python3
"""Offline input-integrity, reuse, full-interval, and chronological packing gates."""
import argparse
from collections import Counter
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

from phase1_read_model_comparison import file_hash, fingerprint, rows
from phase1_vision_raw_history import pilot_module

spec = importlib.util.spec_from_file_location('controls', Path(__file__).with_name('prepare-phase1-representation-controls.py'))
prep = importlib.util.module_from_spec(spec); spec.loader.exec_module(prep)


def unit_checks():
    # An overflowing middle group must not be skipped to recover cheap older data.
    t = [{'at': str(i), 'id': str(i), 'price': cost} for i, cost in enumerate((1, 100, 3, 3))]
    chosen, used, overflow = prep.choose_suffix(t, 10, lambda x: x['price'])
    assert [x['id'] for x in chosen] == ['2', '3'] and used == 6 and overflow == 106
    # Same-time observations stay atomic, including at the budget edge.
    t = [{'at': 'a', 'id': '1', 'price': 3}, {'at': 'a', 'id': '2', 'price': 3}, {'at': 'b', 'id': '3', 'price': 3}]
    assert len(prep.choose_suffix(t, 8, lambda x:x['price'])[0]) == 1
    assert len(prep.choose_suffix(t, 9, lambda x:x['price'])[0]) == 3
    assert prep.choose_suffix(t, 2, lambda x:x['price'])[0] == []
    assert prep.estimated_image_tokens(2048,1600) == 3841
    # Offline vocabulary load must not attempt a network connection.
    with patch('socket.socket.connect', side_effect=AssertionError('Network forbidden during local validation')):
        assert prep.offline_encoding().encode_ordinary('hello')


def check(data):
    unit_checks()
    load = prep.load
    plan = load(data/'plan.json')
    assert plan['version'] == prep.VERSION and not plan['dispatchAuthorized'] and plan['providerCalls'] == 0
    for path, digest in load(data/'artifact-hashes.json').items():
        assert file_hash(data/path) == digest
    for path, digest in {**plan['sourceHashes'], **plan['sourceRawDigests'], **plan['localOCRSidecarsSHA256']}.items():
        assert file_hash(path) == digest
    pilot = Path(plan['sourcePilot']); budget = Path(plan['sourceBudgetRun'])
    frozen = Path(load(pilot/'data/plan.json')['source'])/'frozen'
    base = prep.Index(frozen/'prompts.jsonl', lambda x:(x['exampleID'],x['variant']))
    cohort = {x['exampleID']:x for x in rows(frozen/'cohort.jsonl')}
    events = {x['sourceEventID']:x for x in rows(frozen/'new-context-events.jsonl')}
    originals = prep.Index(pilot/'data/prompts.local.jsonl',lambda x:(x['case'],x['variant']))
    expanded = prep.Index(budget/'data/prompts.local.jsonl',lambda x:(x['case'],x['variant']))
    all_frames = {f['recordID']:f for fs in pilot_module().read_evidence([p for p in plan['sourceRawDigests'] if p.endswith('/raw.jsonl')]).values() for f in fs}
    summaries = load(data/'cases.json'); cases = sorted({x['case'] for x in summaries})
    assert len(cases) == 69 and not set(cases).intersection({118,126,127})
    assert Counter(r['arm'] for r in summaries) == {a:69 for a in prep.ARMS}
    checked_images = set(); reused = 0; blocked = []; frame_occurrences = 0
    def writes(p):
        return [x['text'] for x in p['parts'][1:-1] if x['type']=='input_text' and json.loads(x['text'])['kind'] not in ('read','read_observation')]
    for case in cases:
        payloads = {a:load(data/f'case-{case:04d}/{a}.json') for a in prep.ARMS}
        reference = payloads['time_cleaned']; ex = cohort[reference['exampleID']]
        b = base.get((ex['exampleID'],'new'))
        start = min(x['availableAt'] for x in b['retainedBlocks']); cutoff = ex['targetBeganAt']
        native = [events[i] for i in ex['contextBlockIDs']]
        sessions = {e['sessionID'] for e in native if e.get('sessionID')} | {ex['sessionID']}
        eligible = [f for f in all_frames.values() if f['sessionID'] in sessions and f['capturedAt']<cutoff]
        all_time = sorted((f['capturedAt'],f['recordID']) for f in eligible if f['capturedAt']>=start)
        expected_ids = [i for _,i in all_time]
        assert payloads['time_ocr']['frameRecordIDs'] == payloads['time_images']['frameRecordIDs'] == expected_ids
        assert writes(payloads['time_ocr']) == writes(payloads['time_images']) == writes(reference)
        assert payloads['time_ocr']['intervalStartAt'] == payloads['time_images']['intervalStartAt'] == start
        for arm,p in payloads.items():
            assert p['partsSHA256'] == fingerprint(p['parts']) and not p['dispatchAuthorized']
            assert p['query'] == reference['query'] == ex['query']
            assert p['target'] == reference['target'] and p['targetSHA256'] == fingerprint(p['target'])
            assert p['parts'][0] == reference['parts'][0] and p['parts'][-1] == reference['parts'][-1]
            assert p['cutoffExclusive'] == cutoff
            assert p['wirePayloadBytes'] == prep.wire_size(p['parts'])
            if arm in ('time_cleaned','token_cleaned','token_ocr'):
                v = 'raw_ocr_recent' if arm=='token_ocr' else 'cleaned_recent'
                source = originals if arm=='time_cleaned' else expanded
                assert p['parts'] == source.get((case,v))['parts'] and p['reuse']['partsSHA256'] == p['partsSHA256']
                reused += 1
            else:
                assert p['reuse'] is None
            if not arm.endswith('cleaned'):
                assert p['cleanedReadCount'] == 0
                assert all(json.loads(x['text']).get('kind')!='read' for x in p['parts'][1:-1] if x['type']=='input_text')
            for item in p['items']:
                if item['at']:
                    assert item['at'] < cutoff
            for ident in p['frameRecordIDs']:
                f = all_frames[ident]; assert f['capturedAt'] < cutoff
                if arm.startswith('time_'):
                    assert f['capturedAt'] >= start
                frame_occurrences += 1
            for part in p['parts']:
                if part['type']=='local_image' and part['sha256'] not in checked_images:
                    assert file_hash(part['path']) == part['sha256'] and part['detail']=='original'
                    checked_images.add(part['sha256'])
            if p['limits']:
                blocked.append([case,arm,p['limits']])
                assert p['status']=='blocked_input_limits'
            assert p['fullIntervalRetained'] == arm.startswith('time_')
        # Images are the whole suffix of the same eligible raw/WRITE timeline.
        timeline = [(f['capturedAt'],True,f['recordID']) for f in eligible]
        timeline += [(e['availableAt'],False,e['sourceEventID']) for e in native if e['kind']!='read']
        timeline.sort()
        p = payloads['token_images']; selected = p['items']
        assert [i['id'] for i in selected] == [i[2] for i in timeline[len(timeline)-len(selected):]]
        assert p['estimatedInputTokens'] <= p['assignedInputTokens']
        if p['wholeTimestampOverflowCost'] is not None:
            assert p['wholeTimestampOverflowCost'] > 0
        # Same raw frames and privacy decisions in time-matched OCR/images.
        for left,right in zip(payloads['time_ocr']['items'],payloads['time_images']['items']):
            assert left['id']==right['id'] and left['at']==right['at']
            if left['kind']=='read_observation':
                a=json.loads(payloads['time_ocr']['parts'][left['partStart']]['text'])
                b=json.loads(payloads['time_images']['parts'][right['partStart']]['text'])
                if a.get('privacy'):
                    assert a==b and right['partEnd']-right['partStart']==1
                else:
                    assert a['ocrAvailable'] and a['capturedAt']==b['capturedAt'] and a['source']==b['source']
                    assert right['partEnd']-right['partStart']==2
    return {'status':'passed','cases':len(cases),'payloads':len(summaries),'exactPromptReuses':reused,
            'timeMatchedPairs':69,'rawContainsCleanedBackground':False,'unchangedTimeMatchedWRITEs':True,
            'untruncatedTimeIntervals':True,'sameQueryInstructionTarget':True,'causalWholeTimestampSuffix':True,
            'checkedUniqueImages':len(checked_images),'rawFrameOccurrences':frame_occurrences,
            'blockedConditions':blocked,'providerCalls':0,'planSHA256':file_hash(data/'plan.json')}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--data',type=Path);p.add_argument('--report',type=Path);a=p.parse_args()
    if a.data:
        report=check(a.data)
        if a.report: prep.save(a.report,report)
        print(json.dumps(report,sort_keys=True))
    else:
        unit_checks(); print('Offline packing unit checks passed')

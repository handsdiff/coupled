#!/usr/bin/env python3
"""Prepare the four clarified representation conditions, entirely offline.

The full-interval screenshot inputs determine ONE median input budget. The two
expanded text arms use that budget, not the old hybrid image budgets. Existing
inputs/results and the collector are never modified. No dispatch code exists.
"""
import argparse
from collections import Counter
from functools import lru_cache
import importlib.util
import json
import os
from pathlib import Path
import resource
import statistics

from phase1_read_model_comparison import Privacy, canonical, file_hash, fingerprint, rows
from phase1_vision_raw_history import FullWindowOCR, pack_raw_suffix, pilot_module, raw_block

VERSION = 'phase1-median-context-controls-v1'
ARMS = ('time_cleaned', 'time_ocr', 'time_images', 'budget_cleaned', 'budget_ocr')
NEW_CONDITIONS = ARMS[1:]
MARGIN = 512


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


controls = module(Path(__file__).with_name('prepare-phase1-representation-controls.py'), 'median_controls_source')
load, save, dt = controls.load, controls.save, controls.dt


def render_clean(events, event_map, count, render):
    blocks = [{'eventID': e['sourceEventID'], 'serialized': e['serialized'],
               'tokenIDs': range(count(e['serialized']+'\n')), 'contentTruncated': False}
              for e in events]
    return render(blocks, event_map, lambda s: range(count(s)))


def pack_clean(events, event_map, allowance, count, render):
    """Retain a native-order suffix with dependency-safe READ rendering.

    Rendering with all predecessors gives a lower-bound sizing pass. Rerender
    the selected suffix, restoring full READ states where predecessors fell
    out, and remove oldest timestamp groups if needed. Check the immediate
    excluded group as well: neither stale deltas nor hidden underfill pass.
    """
    rendered, _ = render_clean(events, event_map, count, render)
    sizes = {b['eventID']: len(b['tokenIDs']) for b in rendered}
    timeline = [{'at': e['availableAt'], 'id': e['sourceEventID']} for e in events]
    selected, _, _ = controls.choose_suffix(timeline, allowance, lambda x: sizes[x['id']])
    start = len(events)-len(selected)
    while True:
        chosen, decisions = render_clean(events[start:], event_map, count, render)
        cost = sum(len(b['tokenIDs']) for b in chosen)
        if cost <= allowance:
            break
        at = events[start]['availableAt']
        while start < len(events) and events[start]['availableAt'] == at:
            start += 1
    next_cost = None
    if start:
        previous = start-1
        while previous and events[previous-1]['availableAt'] == events[start-1]['availableAt']:
            previous -= 1
        trial, _ = render_clean(events[previous:], event_map, count, render)
        next_cost = sum(len(b['tokenIDs']) for b in trial)
        assert next_cost > allowance, 'Packing left an immediately older group that fits'
    result = [{k:v for k,v in b.items() if k != 'tokenIDs'} for b in chosen]
    return result, cost, next_cost, decisions


def prepare(source, cache, output):
    assert not output.exists(), 'Use a fresh immutable output directory'
    old_plan = load(source/'plan.json')
    for name, digest in load(source/'artifact-hashes.json').items():
        assert file_hash(source/name) == digest
    for path, digest in {**old_plan['sourceHashes'], **old_plan['sourceRawDigests'],
                         **old_plan['localOCRSidecarsSHA256']}.items():
        assert file_hash(path) == digest
    old_rows = load(source/'cases.json')
    image_rows = sorted((r for r in old_rows if r['arm']=='time_images'), key=lambda r:r['case'])
    assert len(image_rows) == 69
    budget = int(statistics.median(r['estimatedInputTokens'] for r in image_rows))
    pilot = Path(old_plan['sourcePilot']); prior_budget = Path(old_plan['sourceBudgetRun'])
    pilot_plan = load(pilot/'data/plan.json')
    frozen = Path(pilot_plan['source'])/'frozen'
    for name, digest in load(frozen/'artifact-hashes.json').items():
        assert file_hash(frozen/name) == digest
    config = load(frozen.parent/'config.json')
    renderer_path = Path(config['frozenProducer'])/'scripts/phase1_read_novelty.py'
    renderer = module(renderer_path, 'median_frozen_renderer')
    originals = controls.Index(pilot/'data/prompts.local.jsonl', lambda x:(x['case'],x['variant']))
    baselines = controls.Index(frozen/'prompts.jsonl', lambda x:(x['exampleID'],x['variant']))
    ids = {r['exampleID'] for r in image_rows}
    cohort = {r['exampleID']:r for r in rows(frozen/'cohort.jsonl') if r['exampleID'] in ids}
    event_map = {r['sourceEventID']:r for r in rows(frozen/'new-context-events.jsonl')}
    original_events = Path(config['newCorpus'])/'events.jsonl'
    assert file_hash(original_events) == load(frozen/'plan.json')['sourceHashes'][str(original_events)]
    privacy = Privacy({r['sourceEventID']:r for r in rows(original_events)}, config['privacyPolicy'])
    frames = {f['recordID']:f for fs in pilot_module().read_evidence(
        [p for p in old_plan['sourceRawDigests'] if p.endswith('/raw.jsonl')]).values() for f in fs}
    for f in rows(pilot/'data/frames.jsonl'):
        if f['recordID'] in frames and frames[f['recordID']]['ocr'] is None and f.get('ocr'):
            dest = frames[f['recordID']]
            assert dest['screenshotSHA256']==f['screenshotSHA256'] and dest['capturedAt']==f['capturedAt']
            dest['ocr'] = f['ocr']
    # Reuse already verified local OCR, never replace raw OCR with cleaner text.
    for path in old_plan['localOCRSidecarsSHA256']:
        value = load(path); ident = value['binding']['frameRecordID']
        if ident in frames and frames[ident]['ocr'] is None:
            f = frames[ident]
            assert value['capturedAt']==f['capturedAt'] and value['screenshotSHA256']==f['screenshotSHA256']
            f['ocr'] = value
    observer = FullWindowOCR(cache)
    @lru_cache(maxsize=None)
    def observe(ident):
        f=frames[ident]
        return raw_block(f, observer.ensure(f), privacy)
    enc = controls.offline_encoding()
    @lru_cache(maxsize=32768)
    def count(s):
        return len(enc.encode_ordinary(s))
    completed = {}; original_results = {}
    for root in (pilot, prior_budget):
        digest = file_hash(root/'predictions.jsonl')
        assert digest == load(root/'audit.json')['predictionsSHA256']
        for result in rows(root/'predictions.jsonl'):
            completed.setdefault((result['case'],result['partsSHA256']), {
                'run':str(root), 'sampleID':result['sampleID'], 'predictionsSHA256':digest,
                'partsSHA256':result['partsSHA256']})
            if root==pilot:
                original_results[result['case'],result['variant']]=result
    output.mkdir(parents=True, mode=0o700)
    summaries=[]; bound={}; used_frames=set(); calibration=[]; requests=[]
    for image_row in image_rows:
        case=image_row['case']; ex=cohort[image_row['exampleID']]; cutoff=ex['targetBeganAt']
        reference=load(source/f'case-{case:04d}/time_cleaned.json')
        header, query = reference['parts'][0], reference['parts'][-1]
        baseline=baselines.get((ex['exampleID'],'new'))
        # Native causal context is not the old 32K retained subset.
        native=[event_map[i] for i in ex['contextBlockIDs']]
        assert not {ex['targetEventID'],*ex['episode']['memberWriteEventIDs']}.intersection(ex['contextBlockIDs'])
        assert all(e['availableAt']<cutoff for e in native)
        # Episode projection order is intentional and can differ from final
        # availability order. Preserve it, as the frozen cleaned baseline does.
        sessions={e['sessionID'] for e in native if e.get('sessionID')} | {ex['sessionID']}
        eligible=[f for f in frames.values() if f['sessionID'] in sessions and f['capturedAt']<cutoff]
        (output/f'case-{case:04d}').mkdir(mode=0o700)
        for arm in ARMS:
            if arm.startswith('time_'):
                full=load(source/f'case-{case:04d}/{arm}.json')
                assert full['parts'][0]==header and full['parts'][-1]==query
                if arm=='time_cleaned':
                    # Original pilot grouped the recent part by availability.
                    recent=originals.get((case,'cleaned_recent'))['recentInterval']['startAt']
                    ordered=[b for b in baseline['retainedBlocks'] if b['availableAt']<recent]
                    ordered+=sorted((b for b in baseline['retainedBlocks'] if b['availableAt']>=recent),key=lambda b:b['availableAt'])
                    assert [b['serialized']+'\n' for b in ordered]==[p['text'] for p in full['parts'][1:-1]]
                    full['items']=[dict(item,id=b['eventID'],kind=b['kind'],at=b['availableAt']) for item,b in zip(full['items'],ordered)]
                full['sourceExactPartsSHA256']=full['partsSHA256']
            else:
                variant='cleaned_recent' if arm=='budget_cleaned' else 'raw_ocr_recent'
                old=originals.get((case,variant)); r=original_results[case,variant]
                overhead=r['usage']['input_tokens']-sum(count(p['text']) for p in old['parts'])
                fixed=count(header['text'])+count(query['text'])+overhead
                allowance=budget-fixed-MARGIN
                assert allowance>0
                if arm=='budget_cleaned':
                    blocks,cost,next_cost,decisions=pack_clean(native,event_map,allowance,count,renderer.apply_dependency_aware_read_rendering)
                    blocks=[dict(b,kind=event_map[b['eventID']]['kind'],availableAt=event_map[b['eventID']]['availableAt']) for b in blocks]
                    candidates=len(native)
                else:
                    writes=[e for e in native if e['kind']!='read']
                    blocks,cost,reason,next_cost=pack_raw_suffix(eligible,writes,allowance,count,lambda f:observe(f['recordID']))
                    decisions={}; candidates=len(eligible)+len(writes)
                parts=[header]+[{'type':'input_text','text':b['serialized']+'\n'} for b in blocks]+[query]
                assert not privacy.unsafe(''.join(p['text'] for p in parts))
                estimated=fixed+cost
                assert estimated<=budget-MARGIN
                first=min((b['availableAt'] for b in blocks),default=cutoff)
                frame_ids=[b['eventID'] for b in blocks if b['kind']=='read_observation']
                full={k:reference[k] for k in ('application','case','exampleID','target','targetSHA256','query','fixed32KStartAt','fixed32KReferenceTokens','cutoffExclusive')}
                full.update(arm=arm,parts=parts,partsSHA256=fingerprint(parts),intervalStartAt=first,
                    historySpanMinutes=(dt(cutoff)-dt(first)).total_seconds()/60,
                    assignedInputTokens=budget,estimatedInputTokens=estimated,tokenAccounting='locally_calibrated_text_estimate',
                    imageCount=0,frameCount=len(frame_ids),frameRecordIDs=frame_ids,historyItems=len(blocks),
                    historicalWriteCount=sum(b['kind']=='write' for b in blocks),
                    cleanedReadCount=sum(b['kind']=='read' for b in blocks),
                    privacyRedactedFrames=sum(b.get('privacyRedacted',False) for b in blocks),
                    unmeasuredImageGeometries=[],wirePayloadBytes=controls.wire_size(parts),fullIntervalRetained=False,
                    wholeTimestampOverflowCost=next_cost,
                    packing={'budget':budget,'fixedInputTokens':fixed,'calibratedOverheadTokens':overhead,'guardTokens':MARGIN,
                             'historyAllowanceTokens':allowance,'historyTokens':cost,'eligibleItems':candidates,
                             'retainedItems':len(blocks),'allAvailableHistoryUsed':len(blocks)==candidates,
                             'underfillReason':'all_available_history_used' if next_cost is None else 'next_whole_timestamp_exceeds_budget',
                             'nextWholeTimestampHistoryTokens':next_cost,'readRenderingCounts':decisions},
                    items=[{'id':b['eventID'],'kind':b['kind'],'at':b['availableAt'],'partStart':i+1,'partEnd':i+2,
                            **({'readRendering':b['readRendering']} if b.get('readRendering') else {})} for i,b in enumerate(blocks)])
                full['limits']=['estimated_context_over_limit'] if estimated+controls.OUTPUT_RESERVE>controls.CONTEXT_LIMIT else []
                calibration.append({'case':case,'arm':arm,'overheadTokens':overhead,'originalReportedInputTokens':r['usage']['input_tokens'],
                                    'originalLocalTextTokens':sum(count(p['text']) for p in old['parts'])})
            full.update(version=VERSION,sharedBudgetInputTokens=budget,dispatchAuthorized=False,
                        payload=f'case-{case:04d}/{arm}.json')
            full['reuse']=completed.get((case,full['partsSHA256']))
            full['status']='blocked_input_limits' if full['limits'] else 'reused_completed' if full['reuse'] else 'prepared_not_sampled'
            assert full['parts'][0]==header and full['parts'][-1]==query
            assert full['partsSHA256']==fingerprint(full['parts'])
            assert all(x['at']<cutoff for x in full['items'])
            used_frames.update(full['frameRecordIDs'])
            save(output/full['payload'],full);bound[full['payload']]=file_hash(output/full['payload'])
            row={k:v for k,v in full.items() if k not in ('parts','items','frameRecordIDs')}
            summaries.append(row)
            if arm in NEW_CONDITIONS:
                requests.append({k:row[k] for k in ('case','arm','exampleID','partsSHA256','payload','status','reuse','limits','estimatedInputTokens','dispatchAuthorized')})
        print(f'Prepared case {case}: complete time inputs + both text arms at shared B={budget}; zero calls',flush=True)
    frame_rows=[]
    for ident in sorted(used_frames):
        f=frames[ident]; ocr=observer.ensure(f)
        frame_rows.append({**f,'ocr':ocr})
    with (output/'frames.jsonl').open('x') as out:
        os.chmod(output/'frames.jsonl',0o600)
        for f in frame_rows:
            out.write(canonical(f)+'\n')
    budget_evidence={'budgetInputTokens':budget,'statistic':'median','cohortCases':69,'includeBlockedCases':True,
        'counting':'Pre-run calibrated estimate of TOTAL input: screenshot tokens + history text + instruction/query + request overhead.',
        'measuredAfterDispatch':False,'frozenBeforeGeneration':True,
        'rows':[{k:r[k] for k in ('case','estimatedInputTokens','imageCount','frameCount','partsSHA256','limits')} for r in image_rows]}
    summary={'cases':69,'fourAdditionalConditions':list(NEW_CONDITIONS),'baseline':'time_cleaned','payloads':len(summaries),
        'sharedBudgetInputTokens':budget,'providerCalls':0,'arms':{},
        'feasibleNewRequests':sum(r['status']=='prepared_not_sampled' for r in requests),
        'reusedAdditionalRequests':sum(r['status']=='reused_completed' for r in requests),
        'blockedConditions':[{k:r[k] for k in ('case','arm','limits','estimatedInputTokens')} for r in requests if r['limits']]}
    for arm in ARMS:
        a=[r for r in summaries if r['arm']==arm]
        summary['arms'][arm]={'cases':len(a),'statuses':dict(Counter(r['status'] for r in a)),
            'meanInputTokens':statistics.mean(r['estimatedInputTokens'] for r in a),
            'medianInputTokens':statistics.median(r['estimatedInputTokens'] for r in a),
            'minInputTokens':min(r['estimatedInputTokens'] for r in a),'maxInputTokens':max(r['estimatedInputTokens'] for r in a),
            'nearBudget95Percent':sum(r['estimatedInputTokens']>=.95*budget for r in a) if arm.startswith('budget_') else None,
            'allAvailableHistoryUsedCases':[r['case'] for r in a if r.get('packing',{}).get('allAvailableHistoryUsed')],
            'medianHistorySpanMinutes':statistics.median(r['historySpanMinutes'] for r in a)}
    dependencies=[Path(__file__),Path(__file__).with_name('prepare-phase1-representation-controls.py'),
                  Path(__file__).with_name('phase1_vision_raw_history.py'),Path(__file__).with_name('phase1_read_model_comparison.py'),
                  Path(__file__).with_name('prepare-phase1-vision-pilot.py'),renderer_path]
    plan={'version':VERSION,'status':'offline_inputs_prepared_not_authorized','dispatchAuthorized':False,'providerCalls':0,
        'model':'chatgpt/gpt-6-astra','reasoning':'xhigh','temperature':'omitted_as_in_completed_runs','tools':[],
        'generationSettings':'Unchanged from source pilot; no additional generation cap introduced.',
        'fourConditions':list(NEW_CONDITIONS),'baseline':'time_cleaned','sourcePreparation':str(source),
        'sourcePilot':str(pilot),'sourceBudgetRun':str(prior_budget),'sharedBudgetInputTokens':budget,
        'budgetRule':'One fixed median of all 69 full-interval screenshot input estimates, frozen before any generation. NOT old hybrid budgets; NOT per-case matching.',
        'timeRule':'Exact original cleaned 32K interval, all eligible raw frames and identical historical WRITE/gap text; no shortening.',
        'expandedTextRule':'Full causal history at B including instruction/query/overhead. Cleaned preserves native episode order; raw OCR uses captured/available-time order. Retain contiguous suffixes without cutting adjacent equal-time groups. No time cap, padding, skipped older groups or mixed READ representation.',
        'cleanedRendering':'Rebuild the entire retained suffix using the frozen dependency-aware renderer. No permanently truncated legacy boundary block.',
        'rawRendering':'Full-window OCR throughout, same raw privacy policy; never cleaned fallback. Preserve full screenshots in time_images.',
        'underfillRule':'Retain early examples with insufficient history and report actual usage; do not invent data to fill B.',
        'imageAccounting':old_plan['imageAccounting'],'capacityChecks':old_plan['capacityChecks'],
        'scoringContract':pilot_plan['scoringContract'],'sourceRawDigests':old_plan['sourceRawDigests'],
        'sourceHashes':{**{str(source/name):file_hash(source/name) for name in ('artifact-hashes.json','plan.json','cases.json')},
                        **{str(p):file_hash(p) for p in dependencies},str(frozen/'artifact-hashes.json'):file_hash(frozen/'artifact-hashes.json')},
        'localOCRSidecarsSHA256':{**old_plan['localOCRSidecarsSHA256'],**observer.used}}
    for name,value in (('plan.json',plan),('summary.json',summary),('budget-evidence.json',budget_evidence),
                       ('cases.json',summaries),('requests.proposed.json',requests),('token-calibration.json',calibration)):
        save(output/name,value)
    for name in ('frames.jsonl','plan.json','summary.json','budget-evidence.json','cases.json','requests.proposed.json','token-calibration.json'):
        bound[name]=file_hash(output/name)
    save(output/'artifact-hashes.json',bound)
    print(canonical(summary));print('New local OCR frames',observer.new_count,flush=True)
    print('Peak MiB',resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024**2,flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source','cache','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    prepare(args.source.resolve(),args.cache.resolve(),args.output.resolve())

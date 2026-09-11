#!/usr/bin/env python3
"""Corrected offline budget comparison: cleaned text versus full-history raw OCR.

Only text requests are scheduled. Existing screenshot predictions are a bound
baseline, not fresh requests. Unchanged completed cleaned requests are reusable.
"""
import argparse
from collections import Counter
from datetime import datetime
from functools import lru_cache
import json
import os
from pathlib import Path
import resource
import statistics
import tiktoken

from phase1_read_model_comparison import Privacy, canonical, file_hash, fingerprint, rows
from phase1_vision_raw_history import FullWindowOCR, pack_raw_suffix, pilot_module, raw_block

VERSION = 'phase1-vision-budget-v2'
TEXT_ARMS = ('cleaned_recent', 'raw_ocr_recent')
MARGIN = 512


def load(p): return json.loads(Path(p).read_text())
def save(p, value):
    with p.open('x') as out:
        os.chmod(p, 0o600); out.write(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)+'\n')
def save_rows(p, values):
    with p.open('x') as out:
        os.chmod(p, 0o600)
        for value in values: out.write(canonical(value)+'\n')
def dt(s): return datetime.fromisoformat(s.replace('Z', '+00:00'))


def prepare(pilot, paused, cache, output):
    assert not output.exists(), 'Fresh immutable preparation required'
    old_plan = load(pilot/'data/plan.json'); execution = load(pilot/'plan.json')
    assert load(pilot/'audit.json')['status'] == 'complete'
    assert file_hash(pilot/'predictions.jsonl') == load(pilot/'audit.json')['predictionsSHA256']
    for n,h in execution['artifactsSHA256'].items(): assert file_hash(pilot/n) == h
    for n,h in load(paused/'plan.json')['artifactsSHA256'].items(): assert file_hash(paused/n) == h
    assert load(paused/'progress.json')['status'] == 'paused'
    frozen = Path(old_plan['source'])/'frozen'; config = load(frozen.parent/'config.json')
    for n,h in load(frozen/'artifact-hashes.json').items(): assert file_hash(frozen/n) == h
    for path,h in old_plan['sourceRawDigests'].items(): assert file_hash(path) == h
    originals = {(r['case'],r['variant']):r for r in rows(pilot/'data/prompts.local.jsonl')}
    predictions = {(r['case'],r['variant']):r for r in rows(pilot/'predictions.jsonl')}
    cases = {r['case']:r for r in rows(pilot/'data/cases.jsonl') if (r['case'],'screenshots_recent') in predictions}
    case_ids = {c['exampleID'] for c in cases.values()}
    cohort = {r['exampleID']:r for r in rows(frozen/'cohort.jsonl') if r['exampleID'] in case_ids}
    events = {r['sourceEventID']:r for r in rows(frozen/'new-context-events.jsonl')}
    original_events = Path(config['newCorpus'])/'events.jsonl'
    assert file_hash(original_events) == load(frozen/'plan.json')['sourceHashes'][str(original_events)]
    privacy = Privacy({r['sourceEventID']:r for r in rows(original_events)}, config['privacyPolicy'])
    clean = {r['case']:r for r in rows(paused/'data/prompts.local.jsonl') if r['variant']=='cleaned_recent'}
    frames = [f for group in pilot_module().read_evidence([p for p in old_plan['sourceRawDigests'] if p.endswith('/raw.jsonl')]).values() for f in group]
    old_frames = {f['recordID']:f for f in rows(pilot/'data/frames.jsonl')}
    for f in frames:
        if f['ocr'] is None and old_frames.get(f['recordID'],{}).get('ocr') is not None:
            prior = old_frames[f['recordID']]
            assert prior['screenshotSHA256']==f['screenshotSHA256'] and prior['capturedAt']==f['capturedAt']
            f['ocr'] = prior['ocr']
    enc = tiktoken.get_encoding('o200k_base')
    @lru_cache(maxsize=16384)
    def count(s): return len(enc.encode_ordinary(s))
    observer = FullWindowOCR(cache)
    @lru_cache(maxsize=None)
    def observe(ident):
        f = frame_map[ident]
        return raw_block(f, observer.ensure(f), privacy)
    frame_map = {f['recordID']:f for f in frames}
    prompts=[]; packing=[]; blocks=[]; baseline=[]
    for case,c in sorted(cases.items()):
        ex = cohort[c['exampleID']]; cutoff = c['targetBeganAt']
        assigned = predictions[case,'screenshots_recent']['usage']['input_tokens']
        cp = clean[case]
        assert cp['parts'][-1]['text']==c['query'] and fingerprint(cp['parts'])==cp['partsSHA256']
        prompts.append({**cp,'version':VERSION})
        packing.append({**cp['budgetPacking'],'reusedPreparedCleanedPrompt':True,'cleanedReadsAllowed':True})
        native = [events[i] for i in ex['contextBlockIDs']]
        forbidden = {ex['targetEventID'],*ex['episode']['memberWriteEventIDs']}
        assert not forbidden.intersection(ex['contextBlockIDs']) and all(e['availableAt']<cutoff for e in native)
        sessions = {e['sessionID'] for e in native if e.get('sessionID')} | {ex['sessionID']}
        eligible = [f for f in frames if f['sessionID'] in sessions and f['capturedAt']<cutoff]
        writes = [e for e in native if e['kind']!='read']
        old = originals[case,'raw_ocr_recent']
        overhead = predictions[case,'raw_ocr_recent']['usage']['input_tokens']-sum(count(p['text']) for p in old['parts'])
        fixed = count(old['parts'][0]['text'])+count(c['query'])+overhead
        chosen, tokens, reason, next_cost = pack_raw_suffix(eligible, writes, assigned-fixed-MARGIN, count, lambda f:observe(f['recordID']))
        parts = [old['parts'][0]]+[{'type':'input_text','text':b['serialized']+'\n'} for b in chosen]+[old['parts'][-1]]
        assert parts[-1]['text']==c['query'] and not privacy.unsafe(''.join(p['text'] for p in parts))
        assert all(b['kind']!='read' and b['availableAt']<cutoff for b in chosen)
        estimate = fixed+tokens
        assert estimate <= assigned
        begin = min((b['availableAt'] for b in chosen), default=cutoff)
        previous = old['recentInterval']['startAt']
        detail = {'case':case,'variant':'raw_ocr_recent','assignedInputTokens':assigned,'estimatedInputTokens':estimate,
            'baselineReportedInputTokens':predictions[case,'raw_ocr_recent']['usage']['input_tokens'],
            'calibratedOverheadTokens':overhead,'budgetUtilization':estimate/assigned,'remainingTokens':assigned-estimate,
            'approachesBudget95Percent':estimate>=.95*assigned,'underfillReason':reason,'nextWholeTimestampTokens':next_cost,
            'targetBeganAt':cutoff,'expandedHistoryStartAt':begin,'baselineRawIntervalStartAt':previous,
            'expandedHistorySpanSeconds':(dt(cutoff)-dt(begin)).total_seconds(),
            'additionalEarlierSeconds':(dt(previous)-dt(begin)).total_seconds(),
            'retainedBlockIDs':[b['eventID'] for b in chosen],'rawFrameCount':sum(b['kind']=='read_observation' for b in chosen),
            'historicalWriteCount':sum(b['kind']=='write' for b in chosen),'cleanedReadCount':0,
            'privacyRedactedFrameCount':sum(b.get('privacyRedacted',False) for b in chosen),
            'earlierFramesAvailable':len(eligible),'recentPromptUnchanged':False,
            'rawPolicy':'Every READ is full-window raw OCR. Previous cleaned background is replaced too; only historical WRITEs and coverage gaps retain semantic serialization.'}
        prompts.append({**old,'version':VERSION,'parts':parts,'partsSHA256':fingerprint(parts),'budgetPacking':detail,
            'frameRecordIDs':[b['eventID'] for b in chosen if b['kind']=='read_observation'],
            'recentReadEventIDs':[],'backgroundSHA256':None,'priorWritesSHA256':fingerprint([b['serialized'] for b in chosen if b['kind']=='write']),
            'textCharacters':sum(len(p['text']) for p in parts),'rawHistoryStartAt':begin})
        packing.append(detail);blocks.append({'case':case,'blocks':chosen})
        baseline.append({'case':case,'variant':'screenshots_recent','prediction':predictions[case,'screenshots_recent'],
            'promptPartsSHA256':originals[case,'screenshots_recent']['partsSHA256'],'assignedInputTokens':assigned})
        print(f'Case {case}: raw OCR {estimate}/{assigned} tokens, {detail["rawFrameCount"]} frames, zero cleaned READs',flush=True)
    saved={}
    for path in sorted((paused/'results').glob('*/result.json')):
        r=load(path)
        if r['variant']=='cleaned_recent': saved[r['case']] = (r,path)
    requests=[]
    for p in prompts:
        r={k:p[k] for k in ('case','exampleID','variant','partsSHA256')};r.update(replicate=1,model='chatgpt/gpt-6-astra')
        if p['variant']=='cleaned_recent' and p['case'] in saved:
            old,path=saved[p['case']]
            assert old['partsSHA256']==p['partsSHA256']
            r['reuse']={'sourceRun':str(paused),'sourceRequestOrdinal':old['requestOrdinal'],
                'sourceResultSHA256':file_hash(path),'sourcePlanSHA256':file_hash(paused/'plan.json')}
        requests.append(r)
    requests.sort(key=lambda r:fingerprint([20260909,'corrected-raw-budget',r]))
    for i,r in enumerate(requests,1):r['requestOrdinal']=i
    summary={'cases':69,'textPredictions':138,'newRequests':138-len(saved),'reusedCleanedPredictions':len(saved),'reusedScreenshotPredictions':69,
        'newScreenshotRequests':0,'providerCalls':0,'newLocalOCRFrames':observer.new_count,'arms':{}}
    for v in TEXT_ARMS:
        a=[r for r in packing if r['variant']==v]
        summary['arms'][v]={'meanEstimatedInputTokens':statistics.mean(r['estimatedInputTokens'] for r in a),
            'medianEstimatedInputTokens':statistics.median(r['estimatedInputTokens'] for r in a),
            'nearBudget95Percent':sum(r['approachesBudget95Percent'] for r in a),
            'underfilledCases':[r['case'] for r in a if not r['approachesBudget95Percent']],
            'underfillReasons':dict(Counter(r['underfillReason'] for r in a)),
            'medianHistorySpanMinutes':statistics.median(r['expandedHistorySpanSeconds']/60 for r in a),
            'medianAdditionalEarlierMinutes':statistics.median(r['additionalEarlierSeconds']/60 for r in a)}
    plan={'version':VERSION,'sourcePilot':str(pilot),'supersededHybridRun':str(paused),
        'hypothesisScope':'Per-case screenshot-input-budget comparison: expanded cleaned history versus expanded full-window raw OCR history. Reuse original screenshot predictions; no new image requests.',
        'scoringContract':old_plan['scoringContract'],'cases':69,'logicalTextPredictions':138,'proposedPersonalRequests':summary['newRequests'],
        'variants':list(TEXT_ARMS),'replicatesPerArm':1,'screenshotsReused':69,
        'sourcePredictionsSHA256':file_hash(pilot/'predictions.jsonl'),'sourcePlanSHA256':file_hash(pilot/'plan.json'),
        'sourceCleanedPreparationSHA256':file_hash(paused/'data/plan.json'),'sourceRawDigests':old_plan['sourceRawDigests'],
        'sourceEventArtifactSHA256':file_hash(frozen/'new-context-events.jsonl'),'sourceCohortSHA256':file_hash(frozen/'cohort.jsonl'),
        'contextPolicy':{'cleaned':'Exact previously prepared expanded cleaned prompts; no changes.',
            'rawOCR':'Chronological whole-timestamp suffix of full-window OCR frames plus eligible closed WRITEs and coverage gaps. No cleaned READ anywhere, no deduplication, no crop, no target-based selection.',
            'budget':'Per-case original screenshot reported input tokens, using locally calibrated text counts with 512-token margin. Actual usage is recorded separately.',
            'privacy':'Same known-credential filter. Raw sensitive frames become explicit redacted observations, never replaced by cleaned text. No target-dependent exclusions.',
            'causality':'Every raw frame captured before target onset; every historical WRITE belongs to frozen causal context. Initial query unchanged.',
            'underfill':'Stop at first overflowing timestamp, never skip/pad. Retain early cases. Missing images/OCR fail preparation; no cleaned fallback.',
            'comparison':'Input budgets matched, not time spans. Additional past WRITE history varies with the selected history suffix. Original screenshots retain their original older cleaned background.'},
        'counting':{'encoding':'o200k_base','tiktokenVersion':tiktoken.__version__,'exactRemoteTokenizer':'unverified; local counts plus per-case measured envelope overhead'},
        'localOCRSidecarsSHA256':observer.used,'implementationSHA256':{n:file_hash(Path(__file__).with_name(n)) for n in ('prepare-phase1-vision-budget-v2.py','phase1_vision_raw_history.py')},
        'providerCalls':0,'newScreenshotRequests':0}
    output.mkdir(parents=True,mode=0o700)
    save_rows(output/'cases.jsonl',[cases[k] for k in sorted(cases)])
    save_rows(output/'prompts.local.jsonl',prompts);save_rows(output/'requests.proposed.jsonl',requests)
    save_rows(output/'packing.jsonl',packing);save_rows(output/'raw-history-blocks.jsonl',blocks)
    used={i for p in prompts if p['variant']=='raw_ocr_recent' for i in p['frameRecordIDs']}
    save_rows(output/'frames.jsonl',[frame_map[i] for i in sorted(used)])
    save_rows(output/'baseline-screenshots.jsonl',baseline)
    save_rows(output/'baseline-screenshot-prompts.jsonl',[originals[c,'screenshots_recent'] for c in sorted(cases)])
    save(output/'offline-prep.json',summary);save(output/'plan.json',plan)
    save(output/'artifact-hashes.json',{p.name:file_hash(p) for p in sorted(output.iterdir()) if p.is_file()})
    print(canonical(summary));print('Peak MiB',resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024**2,flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for key in ('pilot','paused','cache','output'):p.add_argument('--'+key,type=Path,required=True)
    a=p.parse_args();prepare(a.pilot.resolve(),a.paused.resolve(),a.cache.resolve(),a.output.resolve())

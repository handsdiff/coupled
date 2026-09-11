#!/usr/bin/env python3
"""Offline per-case screenshot-budget follow-up; no provider calls.

Preserve each prior prompt's recent evidence exactly. Prepend an event-suffix
of earlier frozen, privacy-filtered semantic history to the text arms only.
Count newly added text with locally calibrated o200k_base; do not pad data.
"""
import argparse
from collections import Counter
from datetime import datetime
from functools import lru_cache
import importlib.util
import json
import os
from pathlib import Path
import resource
import statistics
import tiktoken

from phase1_read_model_comparison import canonical, file_hash, fingerprint, rows

VERSION='phase1-vision-budget-v1'
VARIANTS=('cleaned_recent','raw_ocr_recent','screenshots_recent')
MARGIN=512


def load(p):return json.loads(p.read_text())
def save(p,value):
    with p.open('x') as f:os.chmod(p,0o600);f.write(json.dumps(value,indent=2,ensure_ascii=False,sort_keys=True)+'\n')
def save_rows(p,data):
    with p.open('x') as f:
        os.chmod(p,0o600)
        for r in data:f.write(canonical(r)+'\n')
def dt(s):return datetime.fromisoformat(s.replace('Z','+00:00'))


def pack_prefix(eligible,event_map,allowance,count,render):
    """Whole-event backwards growth, no skipped big events or target selection.

    Re-render the prefix with the frozen dependency-aware renderer whenever a
    predecessor is added. Ranges provide count-only token sequences, avoiding
    allocating millions of token IDs solely to ask their lengths.
    """
    selected=[];best=[];counts={};reason='all_earlier_history_used';next_cost=None
    for ident in reversed(eligible):
        e=event_map[ident]
        trial=[{'eventID':ident,'serialized':e['serialized'],'tokenIDs':range(count(e['serialized']+'\n')),'contentTruncated':False}]+selected
        rendered,decisions=render(trial,event_map,lambda s:range(count(s)))
        cost=sum(len(b['tokenIDs']) for b in rendered)
        if cost>allowance:
            reason='next_whole_event_exceeds_budget';next_cost=cost;break
        selected=trial;best=rendered;counts=decisions
    return [{k:v for k,v in b.items() if k!='tokenIDs'} for b in best],counts,reason,next_cost


def prepare(pilot,out):
    assert not out.exists(),'Fresh output required'
    old_plan=load(pilot/'plan.json');old_data=load(pilot/'data/plan.json')
    assert load(pilot/'audit.json')['status']=='complete'
    assert file_hash(pilot/'predictions.jsonl')==load(pilot/'audit.json')['predictionsSHA256']
    for name,digest in old_plan['artifactsSHA256'].items():assert file_hash(pilot/name)==digest
    parent=Path(old_data['source']);frozen=parent/'frozen';source_plan=load(frozen/'plan.json')
    for name,digest in load(frozen/'artifact-hashes.json').items():assert file_hash(frozen/name)==digest
    config=load(parent/'config.json');renderer=Path(config['frozenProducer'])/'scripts/phase1_read_novelty.py'
    spec=importlib.util.spec_from_file_location('budget_frozen_novelty',renderer);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    enc=tiktoken.get_encoding('o200k_base')
    @lru_cache(maxsize=32768)
    def count(s):return len(enc.encode_ordinary(s))
    results={(r['case'],r['variant']):r for r in rows(pilot/'predictions.jsonl')}
    assert len(results)==207
    cases={r['case']:r for r in rows(pilot/'data/cases.jsonl') if (r['case'],'screenshots_recent') in results}
    ids={r['exampleID'] for r in cases.values()}
    cohort={r['exampleID']:r for r in rows(frozen/'cohort.jsonl') if r['exampleID'] in ids}
    baselines={r['exampleID']:r for r in rows(frozen/'prompts.jsonl') if r['exampleID'] in ids and r['variant']=='new'}
    event_map={e['sourceEventID']:e for e in rows(frozen/'new-context-events.jsonl')}
    # Privacy filtering already preceded the frozen event artifact. Recheck
    # propagated secrets using the original privacy seeds, never print values.
    from phase1_read_model_comparison import Privacy
    original_events=Path(config['newCorpus'])/'events.jsonl'
    assert file_hash(original_events)==source_plan['sourceHashes'][str(original_events)]
    privacy=Privacy({r['sourceEventID']:r for r in rows(original_events)},config['privacyPolicy'])
    prompts=[];packing=[];calibration=[]
    for old in rows(pilot/'data/prompts.local.jsonl'):
        c=cases[old['case']];v=old['variant'];case=old['case'];example=cohort[c['exampleID']];baseline=baselines[c['exampleID']]
        assigned=results[case,'screenshots_recent']['usage']['input_tokens']
        used_before=results[case,v]['usage']['input_tokens'];old_count=sum(count(p['text']) for p in old['parts'] if p['type']=='input_text')
        retained=baseline['retainedBlocks'];first=retained[0]['eventID'];native=example['contextBlockIDs']
        assert first in native
        eligible=native[:native.index(first)]
        forbidden={example['targetEventID'],*example['episode']['memberWriteEventIDs']}
        assert not forbidden.intersection(eligible)
        assert all(event_map[k]['availableAt']<c['targetBeganAt'] for k in eligible)
        prefix=[];decisions={};reason='unchanged_screenshot_control';next_cost=None
        if v!='screenshots_recent':
            assert used_before<=assigned,'Existing text prompt exceeds assigned screenshot budget'
            prefix,decisions,reason,next_cost=pack_prefix(eligible,event_map,max(0,assigned-used_before-MARGIN),count,module.apply_dependency_aware_read_rendering)
            calibration.append({'case':case,'variant':v,'localContentTokens':old_count,'reportedInputTokens':used_before,'observedOverheadTokens':used_before-old_count})
        added=[{'type':'input_text','text':b['serialized']+'\n'} for b in prefix]
        parts=old['parts'][:1]+added+old['parts'][1:]
        assert parts[0]==old['parts'][0] and parts[len(added)+1:]==old['parts'][1:]
        assert parts[-1]['text']==c['query'] and fingerprint(c['query'])==c['querySHA256']
        assert not privacy.unsafe(''.join(p.get('text','') for p in parts))
        added_tokens=sum(count(p['text']) for p in added);expected=used_before+added_tokens
        assert expected<=assigned
        before_start=min(b['availableAt'] for b in retained)
        after_start=min([before_start]+[event_map[b['eventID']]['availableAt'] for b in prefix])
        detail={'case':case,'variant':v,'assignedInputTokens':assigned,'baselineReportedInputTokens':used_before,
            'estimatedInputTokens':expected,'addedTextTokens':added_tokens,'budgetUtilization':expected/assigned,
            'remainingTokens':assigned-expected,'approachesBudget95Percent':expected>=.95*assigned,
            'baselineHistoryStartAt':before_start,'expandedHistoryStartAt':after_start,'targetBeganAt':c['targetBeganAt'],
            'baselineHistorySpanSeconds':(dt(c['targetBeganAt'])-dt(before_start)).total_seconds(),
            'expandedHistorySpanSeconds':(dt(c['targetBeganAt'])-dt(after_start)).total_seconds(),
            'additionalEarlierSeconds':(dt(before_start)-dt(after_start)).total_seconds(),
            'earlierEventsAvailable':len(eligible),'addedEventCount':len(prefix),
            'addedReadCount':sum(event_map[b['eventID']]['kind']=='read' for b in prefix),
            'addedWriteCount':sum(event_map[b['eventID']]['kind']=='write' for b in prefix),
            'underfillReason':reason,'nextWholeEventAdditionalTokens':next_cost,
            'addedEventIDs':[b['eventID'] for b in prefix],'readRenderingCounts':decisions,
            'baselinePartsSHA256':old['partsSHA256'],'recentPromptUnchanged':True}
        new={**old,'version':VERSION,'parts':parts,'partsSHA256':fingerprint(parts),'textCharacters':sum(len(p.get('text','')) for p in parts),
             'budgetPacking':detail,'addedEarlierHistorySHA256':fingerprint(added)}
        if v=='screenshots_recent':assert new['partsSHA256']==old['partsSHA256']
        prompts.append(new);packing.append(detail)
        print(f'Case {case} {v}: {used_before} → {expected} / {assigned}; {len(prefix)} older events',flush=True)
    assert len(prompts)==207 and len(cases)==69
    requests=[{'case':p['case'],'exampleID':p['exampleID'],'variant':p['variant'],'replicate':1,'partsSHA256':p['partsSHA256'],'model':'chatgpt/gpt-6-astra'} for p in prompts]
    # Fresh scheduling seed, no response caching or changes to human content.
    requests.sort(key=lambda r:fingerprint([20260909,'budget-followup',r]))
    for i,r in enumerate(requests,1):r['requestOrdinal']=i
    summary={'cases':69,'predictions':207,'providerCalls':0,'arms':{},'counting':{'encoding':'o200k_base','tiktokenVersion':tiktoken.__version__,
        'vocabularySHA256':fingerprint(sorted((k.hex(),v) for k,v in enc._mergeable_ranks.items())),
        'method':'Prior actual per-case/per-arm reported input tokens + local token count of the added text; 512-token guard margin. Exact new input usage available only after dispatch.',
        'calibrationExamples':len(calibration),'overheadMin':min(c['observedOverheadTokens'] for c in calibration),'overheadMax':max(c['observedOverheadTokens'] for c in calibration)}}
    for v in VARIANTS:
        a=[r for r in packing if r['variant']==v]
        summary['arms'][v]={'meanEstimatedInputTokens':statistics.mean(r['estimatedInputTokens'] for r in a),
            'medianEstimatedInputTokens':statistics.median(r['estimatedInputTokens'] for r in a),
            'meanAddedTokens':statistics.mean(r['addedTextTokens'] for r in a),'minEstimatedInputTokens':min(r['estimatedInputTokens'] for r in a),'maxEstimatedInputTokens':max(r['estimatedInputTokens'] for r in a),
            'nearBudget95Percent':sum(r['approachesBudget95Percent'] for r in a),'noAddedHistory':sum(r['addedEventCount']==0 for r in a),
            'usedAllEarlierHistory':sum(r['underfillReason']=='all_earlier_history_used' for r in a),
            'underfillReasons':dict(Counter(r['underfillReason'] for r in a)),
            'medianAdditionalEarlierMinutes':statistics.median(r['additionalEarlierSeconds']/60 for r in a),
            'medianHistorySpanMinutes':statistics.median(r['expandedHistorySpanSeconds']/60 for r in a),
            'minHistorySpanMinutes':min(r['expandedHistorySpanSeconds']/60 for r in a),'maxHistorySpanMinutes':max(r['expandedHistorySpanSeconds']/60 for r in a),
            'meanAddedEvents':statistics.mean(r['addedEventCount'] for r in a)}
    plan={'version':VERSION,'hypothesisScope':'Token-budget opportunity-cost comparison: unchanged recent representations; text arms spend spare capacity on earlier cleaned semantic history. Not equal time span or full-history raw OCR.',
        'scoringContract':old_data['scoringContract'],'sourcePilot':str(pilot.resolve()),'sourcePredictionsSHA256':file_hash(pilot/'predictions.jsonl'),
        'sourcePlanSHA256':file_hash(pilot/'plan.json'),'sourceEventArtifactSHA256':file_hash(frozen/'new-context-events.jsonl'),
        'sourceCohortSHA256':file_hash(frozen/'cohort.jsonl'),'frozenRenderer':str(renderer),'frozenRendererSHA256':file_hash(renderer),
        'cases':69,'proposedPersonalRequests':207,'replicatesPerArm':1,'variants':list(VARIANTS),
        'contextPolicy':{'budget':'Each case uses its prior screenshot-arm actual input tokens as maximum; text estimate retains a 512-token margin.',
            'extension':'Add only whole events from the native immediately-earlier context prefix, stopping at first overflow. Earlier events use frozen cleaned semantic serialization in BOTH text arms.',
            'recentEvidence':'Exact original prompt parts preserved; no changes to recent READs, old retained boundary, historical WRITEs, instruction, or query.',
            'newPrefixRendering':'Frozen dependency-aware renderer applied within the added prefix; no invented missing predecessor. The unchanged old prompt remains a conservative valid baseline at the join.',
            'underfill':'Never pad, skip older oversized events, or exclude early cases. Record insufficient history and whole-event budget gaps.',
            'causality':'Every added event belongs to the original example context, precedes target onset, and is not a target/member WRITE.',
            'historicalWrites':'Existing WRITEs unchanged. Additional older WRITEs may enter the expanded arms along with older READs; screenshot history stays unchanged.',
            'screenshots':'Fresh control using byte-identical approved image/text parts; no new images.'},
        'privacy':'Use the already frozen privacy-filtered history, recheck known propagated payloads and patterns in every final text prompt.',
        'generation':'Same Astra xhigh subscription configuration; no training, no fallback model, no paid API.',
        'counting':summary['counting'],'implementationSHA256':file_hash(Path(__file__)),'providerCalls':0}
    out.mkdir(parents=True,mode=0o700)
    save_rows(out/'cases.jsonl',[cases[k] for k in sorted(cases)])
    save_rows(out/'frames.jsonl',rows(pilot/'data/frames.jsonl'))
    save_rows(out/'prompts.local.jsonl',prompts);save_rows(out/'requests.proposed.jsonl',requests)
    save_rows(out/'packing.jsonl',packing);save_rows(out/'token-calibration.jsonl',calibration)
    save(out/'offline-prep.json',summary);save(out/'plan.json',plan)
    save(out/'artifact-hashes.json',{p.name:file_hash(p) for p in sorted(out.iterdir()) if p.is_file()})
    print(canonical(summary));print('Peak MiB',resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024**2)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pilot',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();prepare(a.pilot.resolve(),a.output.resolve())

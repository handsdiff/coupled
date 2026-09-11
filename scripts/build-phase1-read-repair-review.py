#!/usr/bin/env python3
"""Reconstruct frozen closed-WRITE examples with repaired READ histories.

This is a context-only review artifact, NOT authorization to train. Existing
episode membership, targets, onsets, queries, masks, and privacy policy stay
fixed. Pending WRITE/episode findings must be resolved before a training freeze.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from phase1_example_storage import write_examples
from phase1_jsonl import JSONLSequence
from phase1_read_model_comparison import Privacy

ROOT=Path(__file__).resolve().parents[1]
PREP=ROOT/'coupled-data/sep02-10-training-prep-20260910'


def rows(path):
    with path.open() as handle:
        for line in handle:
            if line.strip():yield json.loads(line)


def sha(path):
    digest=hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda:handle.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def save_rows(path,values):
    with path.open('x') as handle:
        for row in values:handle.write(json.dumps(row,ensure_ascii=False,sort_keys=True)+'\n')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--replay',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--replacement',action='append',default=[],help='DAY=completed replay directory')
    args=p.parse_args()
    assert (args.replay/'complete.json').is_file()
    args.output.mkdir(parents=True,exist_ok=False)
    baseline=PREP/'episodes'
    cohort_path=PREP/'paired-qwen38-reference32k/cohort.jsonl'
    cohort={r['exampleID']:r for r in rows(cohort_path)}
    replacements={int(s.split('=',1)[0]):Path(s.split('=',1)[1]) for s in args.replacement}
    for directory in replacements.values():assert (directory/'complete.json').is_file()
    repaired={}
    for day in [2,3,4,7]:
        for r in rows(replacements.get(day,args.replay)/f'sep{day}-causal/events.jsonl'):
            if r['kind']=='read':repaired[r['sourceEventID']]=r
    originals={e['sourceEventID']:e for e in rows(baseline/'events.jsonl')}
    suppressed_raw=set()
    for event in rows(PREP/'micro-corpus/events.jsonl'):
        if event['kind']=='read' and event['sourceEventID'] not in originals:
            suppressed_raw.update(event['sourceRecordIDs'])
    events=[deepcopy(e) for e in originals.values() if e['kind']=='write']
    changes=[]
    for eid,new in repaired.items():
        overlap=set(new['sourceRecordIDs']) & suppressed_raw
        if overlap:
            assert overlap==set(new['sourceRecordIDs']), 'Mixed suppressed/retained episode lineage needs review'
            continue
        before=originals.get(eid)
        if before:
            assert before['availableAt']==new['availableAt']
            assert before['sourceRecordIDs']==new['sourceRecordIDs']
            assert before.get('modelFacingReadSource')==new.get('modelFacingReadSource')
        if not before or new['serialized']!=before['serialized'] or new.get('readNovelty')!=before.get('readNovelty'):
            changes.append(eid)
        events.append(new)
    # Exactly the existing closed-episode projection's ordering; no old READ
    # IDs or timestamps are forced onto the new semantic stream.
    events.sort(key=lambda e:(e['availableAt'],e.get('beganAt') or e['availableAt'],e['sourceEventID']))
    policy_path=ROOT/'coupled-data/phase1-read-pipeline-factorial-full-20260907/frozen/plan.json'
    policy=json.loads(policy_path.read_text())['privacyPolicy']
    privacy=Privacy({e['sourceEventID']:e for e in events},policy)
    events=privacy.filter_stream(events)
    by_id={e['sourceEventID']:e for e in events}
    blocks=list(rows(baseline/'gaps.jsonl'))
    for event in events:
        blocks.append({'contextBlockID':event['sourceEventID'],'contextBlockType':'semantic_event',
            'sessionID':event['sessionID'],'availableAt':event['availableAt'],
            'serialized':event['serialized'],'sourceEventID':event['sourceEventID']})
    blocks.sort(key=lambda b:(b.get('availableAt') or b['beforeAt'],b['contextBlockID']))
    by_block={b['contextBlockID']:b for b in blocks}
    save_rows(args.output/'events.jsonl',events)
    save_rows(args.output/'context-blocks.jsonl',blocks)
    accepted=[]
    def examples():
        for original in JSONLSequence(baseline/'examples.jsonl'):
            if original['exampleID'] not in cohort:continue
            frozen=cohort[original['exampleID']]
            for key in ['query','target','targetMask','targetBeganAt','targetAvailableAt']:
                assert original[key]==frozen[key], ('Frozen example differs',key)
            row=dict(original)
            context=[]
            row['contextBlockIDs']=[b['contextBlockID'] for b in blocks
                if (b.get('availableAt') or b.get('beforeAt'))<row['targetBeganAt']]
            for bid in row['contextBlockIDs']:
                block=by_block[bid]
                if block['contextBlockType']=='semantic_event':
                    assert block['availableAt']<row['targetBeganAt'], 'Future event in input'
                    assert bid!=row['targetEventID'], 'Target leaked into its history'
                context.append(block['serialized'])
            row['contextEventIDs']=[bid for bid in row['contextBlockIDs'] if by_block[bid]['contextBlockType']=='semantic_event']
            row['contextSourceRecordIDs']=sorted({rid for bid in row['contextEventIDs'] for rid in by_id[bid]['sourceRecordIDs']})
            row['sourceRecordIDs']=sorted(set(row['contextSourceRecordIDs']) | set(row['targetSourceRecordIDs']))
            row['context']='\n'.join(context)
            row['modelInput']=row['query'] if not row['context'] else row['context']+'\n'+row['query']
            accepted.append(row['exampleID'])
            yield row
    storage=write_examples(args.output/'examples.jsonl',examples(),compact=True)
    assert set(accepted)==set(cohort)
    report={'version':'phase1-read-repair-review-v1','trainingFreezeApproved':False,
        'examples':len(accepted),'changedHistoricalReads':len(changes),
        'episodeMembership':'frozen; pending reviewed WRITE repairs are NOT implemented here',
        'targetsQueriesOnsetsAndMasks':'exactly unchanged',
        'contextRendering':'rebuilt from current READs and frozen closed WRITEs, strict availableAt < onset, before token-budget packing',
        'readMembership':'new reducer stream, not patched old context IDs; episode-level suppressions retained by raw lineage',
        'privacyPolicy':policy,'sourceCohortSHA256':sha(cohort_path),
        'sourceReplayManifestSHA256':sha(args.replay/'complete.json'),
        'replacementManifestsSHA256':{str(d.resolve()):sha(d/'complete.json') for d in replacements.values()},
        'exampleStorage':storage,'artifactsSHA256':{name:sha(args.output/name) for name in ['examples.jsonl','context-blocks.jsonl','events.jsonl']}}
    with (args.output/'review.json').open('x') as handle:json.dump(report,handle,indent=2,sort_keys=True)
    print(json.dumps({k:v for k,v in report.items() if k not in ['privacyPolicy']},indent=2))


if __name__=='__main__':main()

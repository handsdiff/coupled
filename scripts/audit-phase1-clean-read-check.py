#!/usr/bin/env python3
"""Audit the real reducer outputs, independently of OCR diagnostic success."""
import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREP = ROOT/'coupled-data/sep02-10-training-prep-20260910'


def rows(path):
    with path.open() as handle:
        for line in handle:
            if line.strip(): yield json.loads(line)


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024*1024), b''): digest.update(chunk)
    return digest.hexdigest()


def outcome(row):
    # Only the append ordinal may change when READ interpretation changes.
    return {k:v for k,v in row.items() if k != 'sequence'}


def audit_compiled(directory, reduced):
    manifest=json.loads((directory/'dataset.json').read_text())
    assert manifest['source']['reducerVersion']=='phase1-semantic-v26'
    for name in ['events.jsonl','reduction.json','unresolved.jsonl']:
        assert manifest['source']['digestsSHA256'][name]==sha(reduced/name)
    events={r['sourceEventID']:r for r in rows(directory/'events.jsonl')}
    count=0
    for example in rows(directory/'examples.jsonl'):
        count+=1
        assert example['targetEventID'] in events
        assert example['targetEventID'] not in example['contextEventIDs']
        ids=example['contextEventIDs']
        assert all(events[eid]['availableAt']<example['targetBeganAt'] for eid in ids)
        assert example['context']=='\n'.join(events[eid]['serialized'] for eid in ids)
        assert example['modelInput']==(example['context']+'\n' if example['context'] else '')+example['query']
        mask=example['targetMask']
        assert mask['authoredTextReceivesLoss'] and mask['pasteActionsReceiveLoss']
        assert not mask['pastedPayloadReceivesLoss']
        assert mask['eosReceivesLoss'] and mask['eosTokenCount']==1
    assert count==manifest['counts']['examples']
    assert len(events)==manifest['counts']['convertedEvents']
    return {'examplesChecked':count,'eventsChecked':len(events),'causalInputsAndMaskContract':'passed'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--replay',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--replacement',action='append',default=[],help='DAY=completed replay directory')
    args=p.parse_args()
    assert (args.replay/'complete.json').is_file(), 'Replay must be complete'
    args.output.mkdir(parents=True,exist_ok=False)
    result={'days':{},'trainingFreezeApproved':False,
            'auditSourceSHA256':sha(Path(__file__)),
            'scope':'READ-only repair; no claim that pending WRITE/episode findings are fixed'}
    replacements={int(s.split('=',1)[0]):Path(s.split('=',1)[1]) for s in args.replacement}
    for directory in replacements.values():assert (directory/'complete.json').is_file()
    changed_ids=set()
    restored=Counter()
    details=[]
    for day in [2,3,4,7]:
        old=(PREP/'new-sep7-reduced' if day==7 else ROOT/f'coupled-data/sep02-04-semantic-v25-review-20260907-r4/sep{day}-reduced')
        new=replacements.get(day,args.replay)/f'sep{day}-phase1-semantic-v26'
        old_rows={r['eventID']:r for r in rows(old/'events.jsonl')}
        new_rows={r['eventID']:r for r in rows(new/'events.jsonl')}
        ow={k:outcome(v) for k,v in old_rows.items() if v['kind']=='write'}
        nw={k:outcome(v) for k,v in new_rows.items() if v['kind']=='write'}
        assert ow==nw, f'WRITE changed in session {day}'
        assert all(line['reason']!='stable_session_interface_text' for r in new_rows.values()
                   for line in r.get('reduction',{}).get('semanticReadContent',{}).get('removedLines',[]))
        counts=Counter()
        for eid,row in new_rows.items():
            if row['kind']!='read':continue
            before=old_rows.get(eid)
            if before is None:
                counts['addedReads']+=1;changed_ids.add(eid);continue
            assert row['capturedAt']==before['capturedAt'], 'Capture time changed'
            assert row['sourceRecordIDs']==before['sourceRecordIDs'], 'Lineage changed'
            changed=row['content']!=before['content'] or row.get('readNovelty')!=before.get('readNovelty')
            if changed:
                counts['changedReadContentOrNovelty']+=1;changed_ids.add(eid)
                details.append({'eventID':eid,'day':day,'capturedAt':row['capturedAt'],
                    'sourceRecordIDs':row['sourceRecordIDs'], 'appName':row['appName'],
                    'before':before['content'],'after':row['content'],
                    'beforeNovelty':before.get('readNovelty'), 'afterNovelty':row.get('readNovelty')})
            else:counts['unchangedReadContentAndNovelty']+=1
            restored_here=set()
            for line in before.get('reduction',{}).get('semanticReadContent',{}).get('removedLines',[]):
                text=line.get('text','')
                if line['reason']=='stable_session_interface_text' and text and text in row['content']:
                    restored_here.add(text.strip().lstrip('•›»> ').strip())
            restored.update(restored_here)
        counts['removedReads']=sum(r['kind']=='read' and k not in new_rows for k,r in old_rows.items())
        counts['unchangedWrites']=len(nw)
        result['days'][str(day)]={'counts':dict(counts),'directory':str(new.resolve()),'eventsSHA256':sha(new/'events.jsonl')}
        causal=new.parent/f'sep{day}-causal'
        if causal.is_dir():result['days'][str(day)]['causalAudit']=audit_compiled(causal,new)
        if day==7:
            eid='evt_88ff16de8b5346175b5a6da52adf9ca7a3d49a85c49fa85d34e1fc501c8979a9'
            before,after=old_rows[eid],new_rows[eid]
            assert 'cases regressed from new to old, for both models' in after['content']
            assert 'continued work on data cleanup' in after['content']
            result['motivatingObsidianRead']={'eventID':eid,'before':before['content'],'after':after['content']}
    result['restoredSubstantiveLines']={k:v for k,v in restored.items() if k in [
        'import json', "emphasis on data collection since 'algorithms' get smarter and cheaper by default"]}
    cohort=PREP/'paired-qwen38-reference32k/cohort.jsonl'
    affected=[];total=0
    for example in rows(cohort):
        total+=1
        overlap=changed_ids.intersection(example['contextBlockIDs'])
        if overlap: affected.append({'case':example.get('ordinal',total),'exampleID':example['exampleID'],
                                     'changedHistoricalReadCount':len(overlap)})
    result['frozenCohort']={'examples':total,'withChangedHistoricalRead':len(affected),
                          'beforeTokenBudgetPacking':True,
                          'meaning':'context exposure count, NOT an error or resolved-target count',
                          'cohortSHA256':sha(cohort)}
    for name,value in [('summary.json',result),('affected-examples.json',affected)]:
        with (args.output/name).open('x') as handle:json.dump(value,handle,ensure_ascii=False,indent=2,sort_keys=True)
    with (args.output/'read-differences.jsonl').open('x') as handle:
        for row in details:handle.write(json.dumps(row,ensure_ascii=False,sort_keys=True)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='motivatingObsidianRead'},indent=2))


if __name__=='__main__':main()

#!/usr/bin/env python3
"""Reuse native observations to order original glyph strings by matched boxes.

Recognition is not being repaired here. When Vision spells an unrelated word
differently, that must not prevent reordering exact, uniquely overlapping line
observations. Conversely changed segmentation/ambiguous boxes are not accepted.
"""
import argparse
from collections import Counter
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from phase1_ocr_geometry import row_order
spec=importlib.util.spec_from_file_location('native_order',ROOT/'scripts/repair-phase1-retained-read-order.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
VERSION='retained-read-native-geometry-order-v4'


def overlap(a,b):
    width=max(0,min(a['x']+a['width'],b['x']+b['width'])-max(a['x'],b['x']))
    height=max(0,min(a['y']+a['height'],b['y']+b['height'])-max(a['y'],b['y']))
    intersection=width*height
    return intersection/(a['width']*a['height']+b['width']*b['height']-intersection)


def geometric_projection(text,old,native):
    indices=m.line_indices(text,old)
    if indices is None:return None,[]
    matches=[];used=set()
    for position,index in enumerate(indices):
        candidates=[(overlap(old[index]['boundingBox'],n['boundingBox']),j) for j,n in enumerate(native)]
        candidates.sort(reverse=True)
        if not candidates:return None,[]
        score,j=candidates[0]
        # Identical text plus an unambiguous overlapping box is stronger than
        # either geometry or words alone. Vision can adjust glyph-box heights
        # on a repeated run without moving the actual line.
        exact=old[index]['text']==native[j]['text']
        if score < (.5 if exact else .85):return None,[]
        if j in used or (len(candidates)>1 and candidates[1][0]>.3):return None,[]
        used.add(j);matches.append({'oldIndex':index,'nativeIndex':j,'overlapIoU':score,'exactText':exact,'position':position})
    ordered=sorted(matches,key=lambda r:r['nativeIndex'])
    value='\n'.join(old[r['oldIndex']]['text'] for r in ordered)
    assert Counter(value.split('\n'))==Counter(text.split('\n'))
    return value,matches


def within_column_runs(text,old,native):
    """Order prose locally, without moving labels/cells across old columns.

    Each contiguous run must share an actual horizontal interval. Its original
    position and membership are immutable. This can fix reversed lines inside
    a table description without gathering all dates into a separate column.
    Native one-to-one box evidence still determines order within each run.
    """
    _,matches=geometric_projection(text,old,native)
    if not matches:return None,[]
    runs=[];left=right=None
    for match in matches:
        box=old[match['oldIndex']]['boundingBox'];lo,hi=box['x'],box['x']+box['width']
        if runs and min(right,hi)-max(left,lo)>.1*min(right-left,hi-lo):
            runs[-1].append(match);left,right=max(left,lo),min(right,hi)
        else:runs.append([match]);left,right=lo,hi
    ordered=[v for run in runs for v in sorted(run,key=lambda v:v['nativeIndex'])]
    result='\n'.join(old[v['oldIndex']]['text'] for v in ordered)
    assert Counter(result.split('\n'))==Counter(text.split('\n'))
    return result,[[v['oldIndex'] for v in run] for run in runs]


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--audit',type=Path,required=True);p.add_argument('--input',type=Path,required=True)
    p.add_argument('--surfaces',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
    prior=json.loads((a.audit/'audit.json').read_text())
    for name,digest in prior['artifactsSHA256'].items():assert m.sha(a.audit/name)==digest
    events={r['sourceEventID']:r for r in m.rows(a.input/'new/events.jsonl') if r['kind']=='read'}
    decisions=list(m.rows(a.audit/'decisions.jsonl'));needed={r.get('sourceRecordID') for r in decisions}
    panes={}
    for day in (2,3,4,7):
        for r in m.rows(a.surfaces/f'sep{day}-surfaces/read-surfaces.jsonl'):
            if r['sourceRecordID'] in needed:panes[r['sourceRecordID']]=r
    native={r['jobID']:r for r in m.rows(a.audit/'observations.jsonl')}
    old_changes={r['eventID']:r for r in m.rows(a.audit/'read-changes.jsonl')}
    output=[]
    for d in decisions:
        eid=d['eventID']
        if 'jobID' not in d:continue
        e=deepcopy(events[eid]);pane=panes[d['sourceRecordID']];n=native[d['jobID']]
        key='comparisonLines' if d['label']=='comparison' else 'lines'
        text=json.loads(e['serialized'])['content']
        indices=m.line_indices(text,pane[key])
        _,layout=row_order([pane[key][i] for i in indices])
        d['layoutScreen']=layout['reason']
        if layout['reason'] in {'ambiguous_parallel_columns_preserve_order','ambiguous_multiline_box_preserve_order'}:
            after,runs=within_column_runs(text,pane[key],n['lines'])
            if after is None:d['status']='layout_requires_visual_review';continue
            if after==text:d['status']='no_proven_change_within_fixed_column_runs';continue
            novelty=e.get('readNovelty',{});delta=novelty.get('content')
            # A partial delta has its own column context; do not regroup it.
            if delta not in (None,'',text):d['status']='delta_layout_requires_review';continue
            for field in ('serialized','auditSerialized'):
                if field in e:
                    v=json.loads(e[field])
                    if 'content' in v:assert v['content']==text;v['content']=after;e[field]=m.canonical(v)
            if delta==text:novelty['content']=after
            d['status']='native_within_fixed_column_runs'
            output.append({'eventID':eid,'beforeSHA256':d['beforeSHA256'],'event':e,
                'proof':{'version':VERSION,'decision':d['status'],'sourceRecordID':d['sourceRecordID'],
                    'capturedAt':pane['capturedAt'],'screenshotSHA256':pane['screenshotSHA256'],
                    'label':d['label'],'nativeJobID':d['jobID'],'fixedOriginalColumnRuns':runs,
                    'wordMultisetUnchanged':True,'newOCRStringsAdopted':False}})
            continue
        if eid in old_changes:output.append(old_changes[eid]);continue
        after,matching=geometric_projection(text,pane[key],n['lines'])
        if after is None:d['status']='native_geometry_not_one_to_one';continue
        if after==text:d['status']='native_geometry_confirms_existing_order';continue
        novelty=e.get('readNovelty',{});delta=novelty.get('content')
        if delta not in (None,'',text):
            new_delta,_=geometric_projection(delta,pane[key],n['lines'])
            if new_delta is None:d['status']='delta_geometry_requires_review';continue
        else:new_delta=after if delta==text else delta
        for field in ('serialized','auditSerialized'):
            if field in e:
                v=json.loads(e[field])
                if 'content' in v:assert v['content']==text;v['content']=after;e[field]=m.canonical(v)
        if 'content' in novelty:novelty['content']=new_delta
        d['status']='native_geometric_permutation_original_words'
        output.append({'eventID':eid,'beforeSHA256':d['beforeSHA256'],'event':e,
            'proof':{'version':VERSION,'decision':d['status'],'sourceRecordID':d['sourceRecordID'],
                     'capturedAt':pane['capturedAt'],'screenshotSHA256':pane['screenshotSHA256'],
                     'label':d['label'],'nativeJobID':d['jobID'],'lineMapping':matching,
                     'wordMultisetUnchanged':True,'newOCRStringsAdopted':False}})
    for name,values in [('read-changes.jsonl',output),('decisions.jsonl',decisions)]:
        with (a.output/name).open('x') as f:
            for r in values:f.write(m.canonical(r)+'\n')
    m.save(a.output/'audit.json',{'version':VERSION,'reads':len(events),'counts':dict(Counter(d['status'] for d in decisions)),
        'changedReads':len(output),'sourceHashes':{**prior['sourceHashes'],str(a.audit/'audit.json'):m.sha(a.audit/'audit.json'),
        str(Path(__file__).resolve()):m.sha(Path(__file__)),str(ROOT/'scripts/phase1_ocr_geometry.py'):m.sha(ROOT/'scripts/phase1_ocr_geometry.py'),
        str(ROOT/'scripts/repair-phase1-retained-read-order.py'):m.sha(ROOT/'scripts/repair-phase1-retained-read-order.py')},
        'artifactsSHA256':{f.name:m.sha(f) for f in a.output.glob('*.jsonl')},
        'providerCalls':0,'ocrCalls':0,'recognitionStringsChanged':False})
    print((a.output/'audit.json').read_text())


if __name__=='__main__':main()

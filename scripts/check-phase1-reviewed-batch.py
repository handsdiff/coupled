#!/usr/bin/env python3
"""No-network guards for the finite manual-curation path."""
from copy import deepcopy
from importlib.machinery import SourceFileLoader
from pathlib import Path
import tempfile

m = SourceFileLoader('batch_under_test', str(Path(__file__).with_name('apply-phase1-reviewed-batch.py'))).load_module()


def observation(value):
    return {'value':value,'valueWasTruncated':False,'observationID':'observation','observedAt':'2026-01-01T00:00:01Z'}


def attempt(before, after, terminal=False):
    return {'before':observation(before),'after':observation(after), 'targetIdentity':{'field':'same'},
            'conditioningState':{'clipboard':{'changeCount':7}}, 'inputHints':['typed','return'] if terminal else ['typed'],
            'inputEvents':[{'hint':'return' if terminal else 'typed'}], 'lastEventTimestampNanoseconds':10,
            'returnCheckpoints':[{'observation':observation(after),'eventTimestampNanoseconds':10}] if terminal else []}


def rejects(records):
    try:
        m.verify_continuous_target(records)
    except (ValueError, KeyError):
        return
    raise AssertionError('Unsafe target was accepted')


valid=[attempt('\nDo anything','can you take a quick look'),attempt('can you take a quick look','can you take a look',True)]
assert m.verify_continuous_target(valid)=='can you take a look'
for field,value in [('before',observation('different draft')),('targetIdentity',{'field':'other'}),
                    ('conditioningState',{'clipboard':{'changeCount':8}}),('inputHints',['paste']),
                    ('tapTimeoutCountDuringBurst',1),('lastEventTimestampNanoseconds',11),
                    ('inputEvents',[{'hint':'typed'}])]:
    broken=deepcopy(valid);broken[-1][field]=value;rejects(broken)
broken=deepcopy(valid);broken[0]['returnCheckpoints']=[{'observation':observation('can you take a quick look')}];rejects(broken)
broken=deepcopy(valid);broken[0]['before']['value']='existing text';rejects(broken)
broken=deepcopy(valid);broken[-1]['returnCheckpoints'][0]['observation']['valueWasTruncated']=True;rejects(broken)
broken=deepcopy(valid);broken[-1]['returnCheckpoints'][0]['axErrors']=['invalid_ui_element'];rejects(broken)
assert m.digest({'a':1,'b':2})==m.digest({'b':2,'a':1})
# Long-editor curation uses exact final insertion, not intermediate keystrokes.
note=[attempt('prior: END','prior: befoe END'),attempt('prior: befoe END','prior: before after END')]
for r in note:
    r['lastInputAt']='2026-01-01T00:00:00Z'
    r['conditioningState']['cursorContext']={'leftContext':'prior: ','rightContext':'END','selectedText':''}
text, edit=m.verify_persistent_insertion(note)
assert text=='before after ' and edit['removedContent']==''
for key,value in [('before',observation('different state')),('after',observation('replaced whole doc')),
                  ('lastInputAt','2026-01-02T00:00:00Z'),('inputHints',['paste']),
                  ('targetIdentity',{'field':'elsewhere'})]:
    bad=deepcopy(note);bad[-1][key]=value
    try:m.verify_persistent_insertion(bad)
    except (ValueError,KeyError):pass
    else:raise AssertionError('Unsafe persistent insertion accepted: '+key)
bad=deepcopy(note);bad[0]['conditioningState']['cursorContext']['leftContext']='different prefix'
try:m.verify_persistent_insertion(bad)
except ValueError:pass
else:raise AssertionError('Wrong semantic caret accepted')
# Regression auditing permits only explicitly listed READ changes, and never
# relaxes the old-pipeline arm to accommodate a new-pipeline correction.
a=m.load('audit-phase1-curated-review')
with tempfile.TemporaryDirectory() as td:
    base=Path(td)/'a';out=Path(td)/'b'
    for path in (base,out):
        path.mkdir();m.h.save_rows(path/'cohort.jsonl',[]);m.h.save_rows(path/'common-write-events.jsonl',[])
        for arm in ('old','new'):
            (path/arm).mkdir();m.h.save_rows(path/arm/'events.jsonl',[{'kind':'read','sourceEventID':'read','content':'old'}])
    assert a.baseline_regression(base,out,[])['status']=='passed'
    original={'kind':'read','sourceEventID':'read','content':'old'}
    changed={**original,'content':'new'}
    # Modifying test fixtures is not modifying captured evidence.
    (out/'new/events.jsonl').unlink();m.h.save_rows(out/'new/events.jsonl',[changed])
    try:a.baseline_regression(base,out,[])
    except AssertionError:pass
    else:raise AssertionError('Unreviewed READ change accepted')
    proof=[{'eventID':'read','beforeSHA256':m.digest(original),'event':changed}]
    assert a.baseline_regression(base,out,[],proof)['status']=='passed'
    (out/'old/events.jsonl').unlink();m.h.save_rows(out/'old/events.jsonl',[changed])
    try:a.baseline_regression(base,out,[],proof)
    except AssertionError:pass
    else:raise AssertionError('Old READ arm changed')
print('PASS: exact final-state targets, clipboard/identity/Return guards, reviewed-only READ changes')

#!/usr/bin/env python3
"""Regression gates for word-preserving order and dependency-aware repeats."""
import importlib.util
from pathlib import Path
import json

def load(name):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).with_name(name+'.py'))
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
m=load('repair-phase1-retained-read-order')

def line(s,x,y):return {'text':s,'boundingBox':{'x':x,'y':y,'width':.8,'height':.01}}
a=line('first coherent sentence',.1,.8);b=line('second continuation line',.1,.7)
assert m.inversions([b,a])==[[0,1]]
assert m.inversions([a,b])==[]
assert m.reorder_projection(b['text']+'\n'+a['text'],[b,a],[a,b])[0]==a['text']+'\n'+b['text']
assert m.reorder_projection('unknown',[b,a],[a,b])[0] is None
assert m.reorder_projection(a['text'],[b,a],[a,line('silently new content',.1,.7)])[0] is None
assert m.reorder_projection('a\na',[line('a',.1,.5),line('a',.1,.4)],[line('a',.1,.4),line('a',.1,.5)])[0]=='a\na'
event={'serialized':json.dumps({'kind':'read','content':b['text']+'\n'+a['text']}),
       'readNovelty':{'content':'','dependsOnEventID':'previous'}}
e,why=m.patch_order(event,{'lines':[b,a]},{'lines':[a,b]},'lines')
assert e['readNovelty']['content']=='' and e['readNovelty']['dependsOnEventID']=='previous'
assert json.loads(e['serialized'])['content']==a['text']+'\n'+b['text']
from phase1_read_novelty import apply_dependency_aware_read_rendering
payload=json.dumps({'kind':'read','content':'same thought'})
events={'a':{'kind':'read','serialized':payload},'b':{'kind':'read','serialized':payload,
    'readNovelty':{'currentEventID':'b','decision':'suppress_no_new_content','content':'','dependsOnEventID':'a'}}}
blocks=[{'eventID':x,'serialized':payload,'tokenIDs':list(payload.encode())} for x in ('a','b')]
rendered,_=apply_dependency_aware_read_rendering(blocks,events,lambda s:list(s.encode()))
assert json.loads(rendered[1]['serialized'])['content']==''
rendered,_=apply_dependency_aware_read_rendering(blocks[1:],events,lambda s:list(s.encode()))
assert json.loads(rendered[0]['serialized'])['content']=='same thought'
print('PASS: exact words, ordered geometry, delta preservation, repeat dependency and full fallback')
n=load('reconcile-phase1-native-order')
assert n.geometric_projection(b['text']+'\n'+a['text'],[b,a],[a,b])[0]==a['text']+'\n'+b['text']
variant={**a,'text':'OCR recognition changed but location did not'}
assert n.geometric_projection(b['text']+'\n'+a['text'],[b,a],[variant,b])[0]==a['text']+'\n'+b['text']
shifted={**a,'boundingBox':{**a['boundingBox'],'y':.2}}
assert n.geometric_projection(a['text'],[a],[shifted])[0] is None
merged={'text':a['text']+' '+b['text'],'boundingBox':{'x':.1,'y':.7,'width':.8,'height':.11}}
assert n.geometric_projection(b['text']+'\n'+a['text'],[b,a],[merged])[0] is None
print('PASS: unique native line correspondence; original glyphs retained; displaced/merged lines rejected')
date1={'text':'Date one','boundingBox':{'x':.01,'y':.8,'width':.1,'height':.01}}
date2={'text':'Date two','boundingBox':{'x':.01,'y':.5,'width':.1,'height':.01}}
p1=line('paragraph one beginning',.3,.8);p2=line('paragraph one continuation',.3,.7)
p3=line('paragraph two beginning',.3,.5);p4=line('paragraph two continuation',.3,.4)
old=[date1,p2,p1,date2,p4,p3];native=[date1,date2,p1,p2,p3,p4]
ordered,runs=n.within_column_runs('\n'.join(x['text'] for x in old),old,native)
assert ordered=='\n'.join(x['text'] for x in [date1,p1,p2,date2,p3,p4])
assert runs==[[0],[1,2],[3],[4,5]]
print('PASS: native ordering cannot detach table labels or move text between original column runs')
from copy import deepcopy
q=load('apply-phase1-read-quality-repairs')
def read(eid,t,content='ABCD',source='note'):
    return {'kind':'read','sessionID':'s','sourceEventID':eid,'availableAt':t,
            'serialized':json.dumps({'kind':'read','source':source,'content':content})}
rs={r['sourceEventID']:r for r in [read('a','1'),read('b','2'),read('c','3')]}
assert len(q.exact_adjacent_repeats(rs,set()))==2
assert rs['b']['readNovelty']['dependsOnEventID']=='a'
assert rs['c']['readNovelty']['dependsOnEventID']=='b'
for variant in [read('b','2','AHJD'),read('b','2',source='other')]:
    assert q.exact_adjacent_repeats({'a':read('a','1'),'b':variant},set())==[]
assert q.exact_adjacent_repeats({'a':read('a','1'),'b':read('b','2')},{'b'})==[]
ws={'a':read('a','1'),'b':read('b','2'),'w':{'kind':'write','sourceEventID':'w','sessionID':'s','beganAt':'1.5','availableAt':'3'}}
assert q.exact_adjacent_repeats(ws,set())==[]
first='preceding paragraph\n'+'identical overlapping first sentence'*2+'\n'+'identical overlapping second sentence'*2
second='\n'.join(first.split('\n')[1:])+'\nGenuinely new paragraph'
rs={'a':read('a','1',first),'b':read('b','2',second)}
assert len(q.exact_adjacent_repeats(rs,set()))==1
assert rs['b']['readNovelty']['content']=='Genuinely new paragraph'
assert rs['b']['readNovelty']['decision']=='emit_contiguous_new_content'
rs={'a':read('a','1',first),'b':read('b','2',second.replace('second sentence','changed sentence'))}
assert q.exact_adjacent_repeats(rs,set())==[]
print('PASS: exact adjacent repeats only; new text, other sources, protected records and active WRITEs preserved')

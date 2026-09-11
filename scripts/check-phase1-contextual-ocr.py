#!/usr/bin/env python3
"""Offline tests for context provenance, unchanged targets, and subscription contract."""
import copy
import importlib.util
import json
from pathlib import Path


def module(name):
    spec=importlib.util.spec_from_file_location(name, Path(__file__).with_name(name+'.py'))
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


def main():
    p=module('prepare-phase1-contextual-ocr'); r=module('run-phase1-ocr-correction')
    key=['session','app',1,'document','pane1']
    rect={'x':.1,'y':.1,'width':.7,'height':.8}
    current={'surfaceKey':key,'capturedAt':'2026-09-10T12:00:04.000Z','regionOfInterest':rect}
    def view(t,text='AI text',pane='pane1',window=1,region=None):
        return {'capturedAt':f'2026-09-10T12:00:0{t}.000Z','content':text,'screenshotSHA256':text,
                'sourceRecordID':str(t),'surfaceKey':['session','app',window,'document',pane],
                'regionOfInterest':region or rect}
    prior=[view(0,'earliest'),view(1),view(2),view(3,'earlier'),view(4,'same-time'),view(5,'future')]
    actual=p.earlier_views(current,{tuple(key[:4]):prior})
    assert [x['sourceRecordID'] for x in actual]==['0','2','3'], actual
    assert not p.same_pane(current,view(1,window=2))
    assert p.same_pane(current,view(1,pane='new-hash',region={'x':.1,'y':.12,'width':.7,'height':.78}))
    assert not p.same_pane(current,view(1,pane='sidebar',region={'x':.85,'y':.1,'width':.1,'height':.8}))
    data=[{'capturedAt':x['capturedAt'],'ocrText':x['content']} for x in actual]
    body=r.payload(r.MODELS[0],'Al text',data)
    decoded=json.loads(body['input'][0]['content'][0]['text'])
    assert decoded=={'ocrText':'Al text','previousObservations':data}
    assert body['reasoning']=={'effort':'none'} and body['tools']==[]
    assert body['model']=='chatgpt/gpt-5.6-luna'
    assert r.PROMPT in body['instructions'] and r.CONTEXT_INSTRUCTION in body['instructions']
    for bad in [data+[data[0]], [dict(data[0],futureWriteTarget='secret')]]:
        try: r.payload(r.MODELS[0],'Al text',bad)
        except ValueError: pass
        else: raise AssertionError('Invalid context accepted')
    # The old isolated request is unchanged when no context argument is supplied.
    isolated=r.payload(r.MODELS[0],'Al text')
    assert isolated['instructions']==r.PROMPT
    assert json.loads(isolated['input'][0]['content'][0]['text'])=={'ocrText':'Al text'}
    print('PASS: prior-only selection, resized-pane continuity, unrelated-pane exclusion, original-target preservation, model/effort, bounded inert context')


if __name__=='__main__': main()

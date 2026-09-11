#!/usr/bin/env python3
"""Synthetic and prepared-data vision checks; never calls a provider."""
import argparse
import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile

spec=importlib.util.spec_from_file_location('vision_pilot',Path(__file__).with_name('prepare-phase1-vision-pilot.py'))
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')


def rejected(fn):
    try:fn()
    except (ValueError,AssertionError):return
    raise AssertionError('Invalid fixture accepted')


def fixtures():
    examples=[]
    for case,flags in [(1,(False,True)),(2,(True,False)),(3,(True,True)),(283,(False,False)),(4,(False,False)),(5,(False,False)),(353,(True,True))]:
        examples.append({'case':case,'exampleID':str(case),'substantive':True,'passes':{'astra_old':flags[0],'astra_new':flags[1]}})
    chosen=m.choose_cases(examples,total=5)
    assert len(chosen)==5 and {1,2,3,283}.issubset({x['case'] for x in chosen}) and 353 not in {x['case'] for x in chosen}
    assert chosen==m.choose_cases(list(reversed(examples)),total=5)
    with tempfile.TemporaryDirectory(prefix='coupled-vision-fixtures-') as tmp:
        journal=Path(tmp)/'raw.jsonl';journal.write_text('\n{"recordID":"first"}\n\n{"recordID":"second"}\n')
        assert list(m.numbered_rows(journal))==[(2,{'recordID':'first'}),(4,{'recordID':'second'})]
        p=Path(tmp)/'frame.png';p.write_bytes(PNG)
        def frame(ident,at):
            return {'recordID':ident,'capturedAt':at,'screenshotPath':str(p),'screenshotSHA256':m.file_hash(p),
                'patchGrid32px':1,'source':{'application':'Fixture'},'ocr':{'content':'Visible material '+ident}}
        frames=[frame('first','2026-09-01T10:00:00.000Z'),frame('second','2026-09-01T10:00:00.000Z'),
                frame('third','2026-09-01T10:01:00.000Z'),frame('future','2026-09-01T10:02:00.000Z')]
        missing=copy.deepcopy(frames[0]);missing['ocr']=None
        replay={'recordID':'replay','sourceFrameRecordID':'first','capturedAt':missing['capturedAt'],
                'processedAt':'2026-09-09T12:00:00Z','screenshotSHA256':missing['screenshotSHA256'],
                'origin':'local_full_window_ocr_backfill','content':'recovered text','contentWasTruncated':False,
                'cropFractions':{'viewportSideCropFraction':0,'viewportTopCropFraction':0,'viewportBottomCropFraction':0}}
        m.apply_ocr_sidecar([missing],{'first':replay})
        assert missing['ocr']==replay and missing['capturedAt']!=replay['processedAt']
        original=copy.deepcopy(frames[0]);m.apply_ocr_sidecar([original],{'first':replay});assert original==frames[0]
        wrong=copy.deepcopy(replay);wrong['screenshotSHA256']='wrong'
        missing['ocr']=None;rejected(lambda:m.apply_ocr_sidecar([missing],{'first':wrong}))
        selected,interval=m.select_interval(frames,'2026-09-01T10:02:00.000Z','2026-09-01T09:00:00.000Z',max_frames=2)
        assert [x['recordID'] for x in selected]==['third'], 'Must remove entire earliest timestamp group'
        assert all(x['recordID']!='future' for x in selected), 'Capture at onset must be excluded'
        stale,stale_interval=m.select_interval(frames,'2026-09-01T11:00:00.000Z','2026-09-01T09:00:00.000Z')
        assert stale_interval['latestFrameAgeSeconds']==3480, 'Idle time should remain explicit, not backdated'
        baseline={'modelInput':'reference','modelInputSHA256':'fixture','query':'unchanged cursor query',
          'retainedBlocks':[{'kind':'read','availableAt':'2026-09-01T09:00:00.000Z','serialized':'older READ','eventID':'r0'},
            {'kind':'write','availableAt':'2026-09-01T09:01:00.000Z','serialized':'older WRITE','eventID':'w0'},
            {'kind':'read','availableAt':'2026-09-01T10:01:00.000Z','serialized':'clean recent READ','eventID':'r1'},
            {'kind':'write','availableAt':'2026-09-01T10:01:30.000Z','serialized':'recent WRITE','eventID':'w1'}]}
        example={'case':1,'exampleID':'e1','targetBeganAt':'2026-09-01T10:02:00.000Z'}
        triplet=[m.build_prompt(example,baseline,selected,interval,v,'Predict completion') for v in m.VARIANTS]
        assert len({x['querySHA256'] for x in triplet})==len({x['priorWritesSHA256'] for x in triplet})==len({x['backgroundSHA256'] for x in triplet})==1
        assert triplet[1]['frameRecordIDs']==triplet[2]['frameRecordIDs']==['third']
        assert triplet[0]['imageCount']==triplet[1]['imageCount']==0 and triplet[2]['imageCount']==1
        b=copy.deepcopy(baseline);b['retainedBlocks'][0]['availableAt']=example['targetBeganAt']
        rejected(lambda:m.build_prompt(example,b,selected,interval,m.VARIANTS[0],'instruction'))
        rejected(lambda:m.build_prompt(example,baseline,[frames[-1]],interval,m.VARIANTS[2],'instruction'))
        wire=m.wire_payload(triplet[2]); parts=wire['input'][0]['content']
        image=next(x for x in parts if x['type']=='input_image')
        assert base64.b64decode(image['image_url'].split(',')[1])==PNG and image['detail']=='original'
        assert parts[-1]['text']==baseline['query'] and wire['tools']==[] and wire['model']==m.MODEL
        bad=copy.deepcopy(triplet[2]);bad['parts'][0]['text']='tampered'
        rejected(lambda:m.wire_payload(bad))
        p.write_bytes(PNG+b'tampered')
        rejected(lambda:m.wire_payload(triplet[2]))
    print('PASS synthetic: strict capture cutoff, timestamp-group limits, idle gaps, shared writes/query/background, image encoding and tamper rejection')


def prepared(folder):
    hashes=m.load(folder/'artifact-hashes.json')
    for name,digest in hashes.items():assert m.file_hash(folder/name)==digest
    plan=m.load(folder/'plan.json');cases=list(m.rows(folder/'cases.jsonl'));prompts=list(m.rows(folder/'prompts.local.jsonl'))
    for name,digest in plan['implementationSHA256'].items():
        assert m.file_hash(Path(__file__).with_name(name))==digest, 'Pilot implementation changed'
    for path,digest in {**plan['sourceArtifactSHA256'],**plan['sourceRawDigests']}.items():
        assert m.file_hash(path)==digest, 'Pilot source artifact changed'
    frames={x['recordID']:x for x in m.rows(folder/'frames.jsonl')}
    requests=list(m.rows(folder/'requests.proposed.jsonl'))
    assert plan['providerCalls']==0 and plan['status']=='local_preparation_only_not_authorized_for_transmission'
    source=Path(plan['source']); all_frames=m.read_evidence([p for p in plan['sourceRawDigests'] if p.endswith('raw.jsonl')])
    assert not plan['inventoryOnly'] and plan['replicatesPerArm']==1 and len(cases)==72
    sidecar,provenance=m.load_ocr_sidecar(Path(plan['ocrSidecar']['path']))
    assert provenance==plan['ocrSidecar']
    for values in all_frames.values():m.apply_ocr_sidecar(values,sidecar)
    cohort={x['exampleID']:x for x in m.rows(source/'frozen/cohort.jsonl')}
    baseline={x['exampleID']:x for x in m.rows(source/'frozen/prompts.jsonl') if x['variant']=='new' and x['exampleID'] in {c['exampleID'] for c in cases}}
    source_plan=m.load(source/'frozen/plan.json')
    for c in cases:
        triplet=[x for x in prompts if x['case']==c['case']]
        if c['sensitiveFrameRecordIDs']:
            assert not triplet and not any(r['case']==c['case'] for r in requests)
            continue
        assert sorted(x['variant'] for x in triplet)==sorted(m.VARIANTS)
        e={**cohort[c['exampleID']],'case':c['case']}; b=baseline[c['exampleID']]
        f,interval=m.select_interval(all_frames[e['sessionID']],e['targetBeganAt'],min(x['availableAt'] for x in b['retainedBlocks']))
        assert all(x['ocr'] is not None for x in f) and c['missingOCRFrames']==0
        assert interval==c['interval'] and [x['recordID'] for x in f]==c['frameRecordIDs']
        for p in triplet:
            assert p==m.build_prompt(e,b,f,interval,p['variant'],source_plan['contextBudget']['instruction']), 'Non-deterministic/reconstructed prompt mismatch'
            assert p['parts'][-1]=={'type':'input_text','text':b['query']}
            selected_requests=[r for r in requests if r['exampleID']==e['exampleID'] and r['variant']==p['variant']]
            assert sorted(r['replicate'] for r in selected_requests)==[1]
    for f in frames.values():assert m.file_hash(f['screenshotPath'])==f['screenshotSHA256']
    print(json.dumps({'status':'passed','cases':len(cases),'prompts':len(prompts),'proposedRequests':len(requests),
      'uniqueFrameRecords':len(frames),'exactRegeneratedPrompts':True,'providerCalls':0,
      'peakMiB':m.resource.getrusage(m.resource.RUSAGE_SELF).ru_maxrss/1024**2}))


def proxy_offline():
    # Import and transform without constructing Authenticator or permitting a
    # socket connection. No real request or OAuth cache access is necessary.
    def no_network(event,args):
        if event in ('socket.connect','socket.getaddrinfo','socket.gethostbyname'):
            raise AssertionError('Network forbidden in local proxy contract test')
    sys.addaudithook(no_network)
    os.environ['LITELLM_LOCAL_MODEL_COST_MAP']='True'
    from litellm.llms.chatgpt.responses.transformation import ChatGPTResponsesAPIConfig
    from litellm.types.router import GenericLiteLLMParams
    cls=ChatGPTResponsesAPIConfig
    config=object.__new__(cls)
    content=[{'type':'input_text','text':'First fixture image, then second fixture image.'},
             {'type':'input_image','image_url':'data:image/png;base64,'+base64.b64encode(PNG).decode(),'detail':'original'},
             {'type':'input_text','text':'Second image follows.'},
             {'type':'input_image','image_url':'data:image/png;base64,'+base64.b64encode(PNG).decode(),'detail':'original'}]
    incoming=[{'role':'user','content':content}]
    result=config.transform_responses_api_request('gpt-6-astra',copy.deepcopy(incoming),
        {'reasoning':{'effort':'xhigh'},'tools':[],'stream':True},GenericLiteLLMParams(),{})
    assert result['input']==incoming and result['model']=='gpt-6-astra'
    assert result['stream'] is True and result['store'] is False and result['tools']==[]
    assert result['reasoning']=={'effort':'xhigh'} and result['instructions']
    print(json.dumps({'status':'local_transform_passed','imagePartsPreserved':2,'detail':'original',
        'nativePreambleSHA256':hashlib.sha256(result['instructions'].encode()).hexdigest(),
        'authenticatorConstructed':False,'credentialsRead':False,'providerCalls':0,
        'remoteImageAcceptance':'not_tested','transformationSHA256':m.file_hash(Path(sys.modules[cls.__module__].__file__))}))


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--prepared',type=Path);ap.add_argument('--proxy-offline',action='store_true');args=ap.parse_args()
    fixtures()
    if args.prepared:prepared(args.prepared)
    if args.proxy_offline:proxy_offline()

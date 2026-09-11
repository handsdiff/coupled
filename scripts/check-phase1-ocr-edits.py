#!/usr/bin/env python3
"""No-network regressions for exact OCR edits and low-reasoning execution."""
from contextlib import nullcontext
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

import phase1_ocr_edits as edits


def main():
    def proposal(before, after, occurrence=1):
        return {'before': before, 'after': after, 'occurrence': occurrence, 'reason': 'earlier OCR agrees'}
    def apply(text, changes):
        return edits.apply_edits(text, json.dumps({'edits': changes}))
    original = 'Keep this draft.\nAl text and Al text.\nKeep 123 and 😀.'
    change = proposal('Al text', 'AI text', 2)
    result = apply(original, [change])
    assert result['correctedText'] == original.replace('and Al text', 'and AI text')
    assert result['unchangedCharacters'] == len(original)-len('Al text')
    assert apply(original, [])['correctedText'] == original
    # Simultaneous original coordinates, not sequential fuzzy find/replace.
    assert apply('ab cd', [proposal('ab', 'cd'), proposal('cd', 'EF')])['correctedText'] == 'cd EF'
    assert apply('b\na', [proposal('b\na', 'a\nb')])['correctedText'] == 'a\nb'
    for bad in [[proposal('missing', 'x')], [proposal('Al text', 'x', 3)],
                [proposal('Al', 'AI'), proposal('Al text', 'AI text')],
                [proposal('', 'text')], [proposal('Keep', 'Keep')],
                [proposal('Keep', 'x', True)], [dict(change, unknown='ignored')]]:
        out=edits.interpret_edits(original,json.dumps({'edits':bad}))
        assert not out['editContractValid'] and out['correctedText']==original
    for bad in ['```json\n{"edits":[]}\n```', '{"edits":[],"edits":[]}', 'whole rewritten text']:
        assert not edits.interpret_edits(original,bad)['editContractValid']
    # A semantic mistake can still be syntactically valid. Do not claim otherwise.
    assert apply('my current draft', [proposal('my current draft','old placeholder')])['correctedText']=='old placeholder'

    spec=importlib.util.spec_from_file_location('ocr_edit_runner',Path(__file__).with_name('run-phase1-ocr-correction.py'))
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    model=m.MODELS[0]
    response={'model':model.removeprefix('chatgpt/'),'reasoning':{'effort':'low'},'status':'completed',
              'usage':{'input_tokens':12,'output_tokens':5,'total_tokens':17,'output_tokens_details':{'reasoning_tokens':2}},
              'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps({'edits':[proposal('Al','AI')]})}]}]}
    assert m.interpret(json.dumps(response).encode(),model,'low')['reasoningEffort']=='low'
    try:m.interpret(json.dumps(response).encode(),model)
    except ValueError:pass
    else:raise AssertionError('Reasoning mismatch accepted')
    with tempfile.TemporaryDirectory(prefix='ocr-edits-contract-') as tmp:
        root=Path(tmp);doc={'documentID':'d','text':'Al text','capturedAt':'2026-01-01T00:00:01Z','previousObservations':[]}
        m.atomic(root/'documents.json',[doc]);m.atomic(root/'requests.json',[{'documentID':'d','model':model}]);m.atomic(root/'proxy.json',{})
        m.atomic(root/'plan.json',{'version':edits.VERSION,'models':[model],'prompt':edits.PROMPT,'reasoning':'low','port':4019,
                 'documentsSHA256':m.digest(root/'documents.json'),'requestsSHA256':m.digest(root/'requests.json'),
                 'proxySHA256':m.digest(root/'proxy.json'),'codeHashes':{},'runtimeHashes':{},'timeoutSeconds':10})
        auth=m.digest(root/'plan.json');calls=[]
        def send(body,attempt,timeout):
            assert body['reasoning']=={'effort':'low'} and body['instructions']==edits.PROMPT
            calls.append(body);(attempt/'response.body').write_bytes(json.dumps(response).encode())
            m.atomic(attempt/'transport.json',{'httpStatus':200,'bodySHA256':m.digest(attempt/'response.body'),'dispatchToCompletionSeconds':1})
        m.module=lambda path:SimpleNamespace(transport=send);m.proxy=lambda *args:nullcontext()
        assert m.run(root,auth)==1
        saved=m.read(root/'results/0000/result.json')
        assert saved['editContractValid'] and saved['correctedText']=='AI text'
        assert saved['usage']['output_tokens_details']['reasoning_tokens']==2
        assert m.run(root,auth)==1 and len(calls)==1
        (root/'results/0000/result.json').unlink()
        assert m.run(root,auth)==1 and len(calls)==1
        (root/'results/0000/transport.json').unlink()
        try:m.run(root,auth)
        except ValueError as error:assert 'Uncertain' in str(error)
        else:raise AssertionError('Uncertain dispatch replayed')
    # The Sol route changes only the named model/effort, not prompt or patching.
    with tempfile.TemporaryDirectory(prefix='ocr-sol-contract-') as tmp:
        root=Path(tmp);model=m.SOL
        response['model']=model.removeprefix('chatgpt/');response['reasoning']={'effort':'xhigh'}
        m.atomic(root/'documents.json',[doc]);m.atomic(root/'requests.json',[{'documentID':'d','model':model}]);m.atomic(root/'proxy.json',{})
        m.atomic(root/'plan.json',{'version':m.SOL_EDITS_VERSION,'models':[model],'prompt':edits.PROMPT,'reasoning':'xhigh','port':4020,
                 'documentsSHA256':m.digest(root/'documents.json'),'requestsSHA256':m.digest(root/'requests.json'),
                 'proxySHA256':m.digest(root/'proxy.json'),'codeHashes':{},'runtimeHashes':{},'timeoutSeconds':900})
        auth=m.digest(root/'plan.json');calls=[]
        def send_sol(body,attempt,timeout):
            assert body['model']==m.SOL and body['reasoning']=={'effort':'xhigh'}
            assert body['instructions']==edits.PROMPT and timeout==900 and not body['tools']
            calls.append(body);(attempt/'response.body').write_bytes(json.dumps(response).encode())
            m.atomic(attempt/'transport.json',{'httpStatus':200,'bodySHA256':m.digest(attempt/'response.body'),'dispatchToCompletionSeconds':1})
        m.module=lambda path:SimpleNamespace(transport=send_sol)
        assert m.run(root,auth)==1 and m.run(root,auth)==1 and len(calls)==1
        saved=m.read(root/'results/0000/result.json')
        assert saved['correctedText']=='AI text' and saved['reasoningEffort']=='xhigh'
        assert saved['apiEquivalentUncachedUSD']==(12*4+5*20)/1e6
        for changed in [{'reasoning':'low'},{'models':[m.MODELS[0]]}]:
            plan=m.read(root/'plan.json');plan.update(changed);m.atomic(root/'plan.json',plan)
            try:m.run(root,m.digest(root/'plan.json'))
            except ValueError:pass
            else:raise AssertionError('Sol profile substitution accepted')
        assert len(calls)==1
    with tempfile.TemporaryDirectory(prefix='ocr-astra-contract-') as tmp:
        root=Path(tmp);model=m.ASTRA
        response['model']=model.removeprefix('chatgpt/');response['reasoning']={'effort':'low'}
        m.atomic(root/'documents.json',[doc]);m.atomic(root/'requests.json',[{'documentID':'d','model':model}]);m.atomic(root/'proxy.json',{})
        m.atomic(root/'plan.json',{'version':m.ASTRA_EDITS_VERSION,'models':[model],'prompt':edits.PROMPT,'reasoning':'low','port':4021,
                 'documentsSHA256':m.digest(root/'documents.json'),'requestsSHA256':m.digest(root/'requests.json'),
                 'proxySHA256':m.digest(root/'proxy.json'),'codeHashes':{},'runtimeHashes':{},'timeoutSeconds':300})
        auth=m.digest(root/'plan.json');calls=[]
        def send_astra(body,attempt,timeout):
            assert body['model']==m.ASTRA and body['reasoning']=={'effort':'low'}
            assert body['instructions']==edits.PROMPT and timeout==300 and not body['tools']
            calls.append(body);(attempt/'response.body').write_bytes(json.dumps(response).encode())
            m.atomic(attempt/'transport.json',{'httpStatus':200,'bodySHA256':m.digest(attempt/'response.body'),'dispatchToCompletionSeconds':1})
        m.module=lambda path:SimpleNamespace(transport=send_astra)
        assert m.run(root,auth)==1 and m.run(root,auth)==1 and len(calls)==1
        saved=m.read(root/'results/0000/result.json')
        assert saved['correctedText']=='AI text' and saved['reasoningEffort']=='low'
        assert saved['apiEquivalentUncachedUSD']==(12*10+5*50)/1e6
    print('PASS: exact edits, unchanged bytes, Luna low/Sol xhigh/Astra low contracts, profile rejection and durable resume')


if __name__=='__main__':main()

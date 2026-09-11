#!/usr/bin/env python3
"""No-network tests for OCR subscription routing, response integrity and resume."""
from contextlib import nullcontext
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace


def main():
    spec = importlib.util.spec_from_file_location('ocr_test_runner', Path(__file__).with_name('run-phase1-ocr-correction.py'))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    model = m.MODELS[0]
    response = {'model': model.removeprefix('chatgpt/'), 'reasoning': {'effort': 'none'},
                'status': 'completed', 'usage': {'input_tokens': 12, 'output_tokens': 3, 'total_tokens': 15},
                'output': [{'type': 'message', 'id': 'msg_test', 'content': [{'type': 'output_text', 'text': 'AI text'}]}]}
    assert m.interpret(json.dumps(response).encode(), model)['validCompletion']
    for bad in ['wrong_model', 'wrong_effort', 'wrong_usage', 'unexpected_tool']:
        r = copy.deepcopy(response)
        if bad == 'wrong_model': r['model'] = 'gpt-6-astra'
        if bad == 'wrong_effort': r['reasoning']['effort'] = 'low'
        if bad == 'wrong_usage': r['usage']['total_tokens'] = 1
        if bad == 'unexpected_tool':
            r['output'].append({'type': 'function_call', 'name': 'secret'})
            assert not m.interpret(json.dumps(r).encode(), model)['validCompletion']; continue
        try: m.interpret(json.dumps(r).encode(), model)
        except ValueError: pass
        else: raise AssertionError(bad)
    try: m.payload('openai/gpt-5.6-luna', 'sample')
    except ValueError: pass
    else: raise AssertionError('Paid route accepted')
    assert set(m.payload(model, 'sample')) == {'model', 'instructions', 'input', 'tools', 'reasoning', 'stream'}
    assert 'draft' not in m.payload(model, 'sample')['input'][0]['content'][0]['text']
    with tempfile.TemporaryDirectory(prefix='ocr-contract-') as temp:
        root = Path(temp)
        m.atomic(root/'documents.json', [{'documentID': 'd', 'text': 'Al text'}])
        m.atomic(root/'requests.json', [{'documentID': 'd', 'model': model}])
        m.atomic(root/'proxy.json', {})
        m.atomic(root/'plan.json', {'version': m.VERSION, 'models': m.MODELS, 'port': 4017,
                 'documentsSHA256': m.digest(root/'documents.json'), 'requestsSHA256': m.digest(root/'requests.json'),
                 'proxySHA256': m.digest(root/'proxy.json'), 'codeHashes': {}, 'runtimeHashes': {}, 'timeoutSeconds': 10})
        auth = m.digest(root/'plan.json'); calls = []
        def send(body, attempt, timeout):
            calls.append(body)
            (attempt/'response.body').write_bytes(json.dumps(response).encode())
            m.atomic(attempt/'transport.json', {'httpStatus': 200, 'bodySHA256': m.digest(attempt/'response.body'), 'dispatchToCompletionSeconds': 1})
        m.module = lambda path: SimpleNamespace(transport=send)
        m.proxy = lambda *args: nullcontext()
        assert m.run(root, auth) == 1
        assert m.run(root, auth) == 1 and len(calls) == 1
        # A durable transport without a saved result is recoverable locally.
        (root/'results/0000/result.json').unlink()
        assert m.run(root, auth) == 1 and len(calls) == 1
        # An uncertain dispatch is not replayable.
        (root/'results/0000/transport.json').unlink()
        try: m.run(root, auth)
        except ValueError as err: assert 'Uncertain' in str(err)
        else: raise AssertionError('Uncertain dispatch replayed')
        assert len(calls) == 1
        m.atomic(root/'documents.json', [{'documentID': 'd', 'text': 'tampered'}])
        try: m.run(root, auth)
        except ValueError as err: assert 'Inputs changed' in str(err)
        else: raise AssertionError('Tampered inputs accepted')
    print('PASS: model/effort/usage, no API fallback, immutable inputs, complete-stream recovery, no uncertain replay')


if __name__ == '__main__': main()

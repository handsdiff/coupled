#!/usr/bin/env python3
"""No-network contract tests for the extra model preflight preparation."""
import copy
import importlib.util
from pathlib import Path
import socket

socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError('Network prohibited'))
spec = importlib.util.spec_from_file_location('model_prep', Path(__file__).with_name('prepare-phase1-qwen-model-preflights.py'))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)


def rejected(fn):
    try:
        fn()
    except Exception:
        return
    raise AssertionError('Expected invalid source to be rejected')


tok = m.AutoTokenizer.from_pretrained(m.PREP / 'tokenizer', local_files_only=True, trust_remote_code=False)
prompt = 'Predict the next write.\n<|paste|> is an action marker.'
target = ' leading 🧠 <|paste|> trailing \n'
ids = m.hf_prompt(tok, [{'role': 'user', 'content': prompt}], enable_thinking=False)
completion = tok.encode(target, add_special_tokens=False) + [248046]
source = {'promptTokenIDs': ids, 'completionTokenIDs': completion,
          'modelInputSHA256': m.text_hash(prompt), 'targetSHA256': m.text_hash(target),
          'fullSequenceTokenSHA256': m.fingerprint(ids + completion)}
assert m.source_text(source, tok) == (prompt, target)
broken = copy.deepcopy(source); broken['promptTokenIDs'][10] += 1
rejected(lambda: m.source_text(broken, tok))
broken = copy.deepcopy(source); broken['completionTokenIDs'][-1] = 248044
rejected(lambda: m.source_text(broken, tok))
e = {'exampleID': 'synthetic', 'experimentBlockID': 'synthetic', 'targetEventID': 'synthetic',
     'targetText': target, 'target': {'segments': [{'type': 'authored_text', 'content': target}]}}
for key, stop in [('qwen35_base', 248044), ('qwen36_hybrid', 248046)]:
    native_tok = m.AutoTokenizer.from_pretrained(m.OLD_TOKENIZERS / key / 'tokenizer', local_files_only=True, trust_remote_code=False)
    runtime = m.native_runtime_from_tokenizer(key, native_tok)
    row = m.supervised_row(e, prompt, runtime)
    assert row['completionTokenIDs'][-1] == stop
    assert native_tok.decode(row['completionTokenIDs'][:-1], clean_up_tokenization_spaces=False) == target
    assert row['causalSDKShiftVerified'] is True
    assert row['trainingDatumPositions'] == row['promptTokenCount'] + row['lossBearingTokenCount'] - 1
    if key == 'qwen35_base':
        assert row['promptTokenIDs'] == native_tok.encode(prompt, add_special_tokens=False)
renderer = m.ExactReasoningRenderer(tok, reasoning_effort='xhigh')
on = m.reasoning_row(e, prompt, renderer)
assert on['generationOnly'] and on['trainingDatum'] is None
assert 'completionTokenIDs' not in on and on['targetText'] == target
unfinished = tok.encode('Still considering the input.', add_special_tokens=False)
for ending, reason in [([], 'length'), ([248046], 'stop')]:
    parsed = m.parse_reasoning_output(renderer, unfinished + ending, reason)
    assert not parsed['prediction'] and not parsed['reasoningClosed'], 'Unfinished reasoning must not be scored as an answer'
finished = tok.encode('Consider the request.\n</think>\n\n' + target, add_special_tokens=False)
for ending, reason in [([], 'length'), ([248046], 'stop')]:
    parsed = m.parse_reasoning_output(renderer, finished + ending, reason)
    assert parsed['prediction'] == target and parsed['reasoningClosed']
    assert parsed['stopReason'] == reason
    assert parsed['parserOnlyTerminatorAdded'] == (not ending)
print('PASS: exact contexts/targets, altered-token rejection, model-specific EOS, complete SDK causal masks, and reasoning-only safeguards; no network')

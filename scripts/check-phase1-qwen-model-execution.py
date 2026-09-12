#!/usr/bin/env python3
"""No-network tests of the actual multi-model SDK bridge and shared budget."""
import copy
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace

socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError('Network prohibited'))
spec = importlib.util.spec_from_file_location('model_run', Path(__file__).with_name('run-phase1-qwen-model-preflights.py'))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
import numpy as np
import tinker


class Future:
    def __init__(self, value): self.value = value
    def result(self): return self.value


class Sampler:
    def __init__(self, service, state): self.service, self.state = service, copy.deepcopy(state)
    def sample(self, prompt, num_samples, sampling_params):
        s = self.service; row = s.prompts[m.fingerprint(prompt.to_ints())]
        assert num_samples == 1 and sampling_params.stop == [s.stop]
        assert sampling_params.temperature == .6 and sampling_params.seed in (17, 18, 19, 20)
        tokens = row['completionTokenIDs']
        if s.omit_stop: tokens = tokens[:-1]
        result = tinker.types.SampledSequence(tokens_np=np.asarray(tokens), logprobs_np=np.asarray([-.5] * len(tokens)), stop_reason='stop')
        return Future(tinker.types.SampleResponse(sequences=[result]))
    def compute_logprobs(self, prompt):
        row = self.service.full[m.fingerprint(prompt.to_ints())]
        # Context's deliberately extreme likelihood must never enter target NLL.
        return Future([None] + [-99.] * (row['promptTokenCount'] - 1) + [-.5 + self.state['w']] * row['lossBearingTokenCount'])


class Trainer:
    def __init__(self, service, state): self.service, self.state = service, copy.deepcopy(state)
    def get_info(self): return {'model': self.service.model}
    def forward_backward(self, data, loss):
        assert loss == 'cross_entropy' and len(data) == 1
        d = data[0]; inputs = d.model_input.to_ints(); targets = d.loss_fn_inputs['target_tokens'].to_numpy().tolist()
        row = self.service.full[m.fingerprint(inputs + [targets[-1]])]
        assert inputs[1:] == targets[:-1]
        assert d.loss_fn_inputs['weights'].to_numpy().tolist() == [0.] * (row['promptTokenCount'] - 1) + [1.] * row['lossBearingTokenCount']
        self.service.trained.append(row['exampleID'])
        values = [-99.] * (row['promptTokenCount'] - 1) + [-.5] * row['lossBearingTokenCount']
        return Future(SimpleNamespace(loss_fn_outputs=[{'logprobs': np.asarray(values)}], metrics={'loss:sum': .5 * row['lossBearingTokenCount']}))
    def optim_step(self, p):
        assert (p.learning_rate, p.beta1, p.beta2, p.eps, p.weight_decay, p.grad_clip_norm) == (.0002, .9, .95, 1e-12, 0., 1.)
        self.state['m'] = self.state['m'] * .95 + .1; self.state['w'] += .01 * self.state['m']
        return Future(SimpleNamespace(metrics={'gradient_norm': 1.}))
    def save_weights_for_sampler(self, name, ttl_seconds): return self._save('sampler/' + name, ttl_seconds)
    def save_state(self, name, ttl_seconds): return self._save('optimizer/' + name, ttl_seconds)
    def _save(self, name, ttl):
        assert ttl == 3600
        self.service.saved[name] = copy.deepcopy(self.state)
        return Future(SimpleNamespace(path=name))


class Service:
    def __init__(self, model, stop, rows):
        self.model, self.stop, self.saved, self.trained, self.omit_stop = model, stop, {}, [], False
        self.prompts = {m.fingerprint(r['promptTokenIDs']): r for r in rows.values()}
        self.full = {m.fingerprint(r['promptTokenIDs'] + r['completionTokenIDs']): r for r in rows.values()}
    def create_sampling_client(self, base_model=None, model_path=None):
        assert (base_model == self.model) != bool(model_path)
        return Sampler(self, self.saved[model_path] if model_path else {'w': 0., 'm': 0.})
    def create_lora_training_client(self, **kwargs):
        assert kwargs['base_model'] == self.model and kwargs['rank'] == 32 and kwargs['seed'] == 17
        assert kwargs['train_attn'] and kwargs['train_mlp'] and kwargs['train_unembed']
        return Trainer(self, {'w': 0., 'm': 0.})
    def create_training_client_from_state_with_optimizer(self, path, **kwargs):
        assert path.startswith('optimizer/') and kwargs['base_model'] == self.model
        return Trainer(self, self.saved[path])


report = json.loads((m.ROOT / 'coupled-data/sep02-10-qwen-model-preflights-20260912-v4/preparation.json').read_text())
for key in ('qwen35_base', 'qwen36_hybrid'):
    tok, renderer = m.local_model(key); runtime = m.prep.native_runtime_from_tokenizer(key, tok)
    rows = {}
    for i in range(5):
        text = f' leading 🧠 <|paste|> {i} trailing \n'
        e = {'exampleID': str(i), 'experimentBlockID': 'test', 'targetEventID': 'test', 'targetText': text,
             'target': {'segments': [{'type': 'authored_text', 'content': text}]}}
        rows[str(i)] = m.prep.supervised_row(e, f'Predict exactly {i}', runtime)
    cfg = report['models'][key]; contract = copy.deepcopy(cfg['trainingContract'])
    contract['generation'] = {**cfg['generation'], 'seed': 17, 'samplesPerExample': 1}
    service = Service(cfg['model'], cfg['nativeStopTokenIDs'][0], rows)
    bridge = m.NativeBridge(service, tinker, tok, renderer, contract)
    for omit in (False, True):
        service.omit_stop = omit
        g = bridge.generate(bridge.sampler(), rows['0'])
        assert g['prediction'] == ' leading 🧠 <|paste|> 0 trailing \n' and g['parserOnlyTerminatorAdded'] == omit
    service.omit_stop = False
    assert bridge.nll(bridge.sampler(), rows['0'])['meanNLL'] == .5
    with tempfile.TemporaryDirectory() as directory:
        calls = m.SharedCalls(m.Journal(Path(directory), {'test': key}), key, {'prefill': 0., 'sample': 0., 'train': 0.}, 512)
        result = m.original.phases(m.original.JournaledBridge(bridge), calls, rows,
                                  {'probeIDs': list(rows), 'trainIDs': ['0', '1', '2']}, 'old')
        assert service.trained == ['0', '1', '2', '2']
        assert result['weightRestoreMaxLogprobDelta'] == result['optimizerContinuationMaxLogprobDelta'] == 0
    wrong = copy.deepcopy(rows['0']); wrong['completionTokenIDs'][-1] = 248046 if service.stop == 248044 else 248044
    try: m.native_datum(wrong, tinker, service.stop)
    except m.ContractError: pass
    else: raise AssertionError('Wrong-model EOS accepted')

tok, renderer = m.local_model('qwen38_reasoning')
reasoning_spec = report['models']['qwen38_reasoning']
e = {'exampleID': 'reasoning', 'targetText': ' review <|paste|> \n'}
row = m.prep.reasoning_row(e, 'Predict the next write.', renderer)
contract = {'model': reasoning_spec['model'], 'reasoning': True,
            'generation': {**reasoning_spec['generation'], 'seed': 17, 'samplesPerExample': 1}}
for content, expected in [('Consider the context.', ''), ('Consider it.\n</think>\n\n review <|paste|> \n', e['targetText'])]:
    class ReasoningSampler:
        def sample(self, prompt, num_samples, sampling_params):
            assert prompt.to_ints() == row['promptTokenIDs'] and sampling_params.max_tokens == 8192
            ids = tok.encode(content, add_special_tokens=False)
            return Future(tinker.types.SampleResponse(sequences=[tinker.types.SampledSequence(
                tokens_np=np.asarray(ids), logprobs_np=np.asarray([-.1]*len(ids)), stop_reason='length')]))
    bridge = m.NativeBridge(None, tinker, tok, renderer, contract)
    generated = bridge.generate(ReasoningSampler(), row)
    assert generated['prediction'] == expected and generated['stopReason'] == 'length'
    assert generated['reasoningTokenCount'] + generated['answerAndTerminationTokenCount'] == generated['outputTokens']
    try: m.native_datum(row, tinker, 248046)
    except m.ContractError: pass
    else: raise AssertionError('Generation-only reasoning artifact became training data')

with tempfile.TemporaryDirectory() as directory:
    journal = m.Journal(Path(directory), {'test': 'shared ceiling'})
    journal.append({'kind': 'operation_begin', 'key': 'prior-model', 'maximumUSD': 19.49})
    row = {'exampleID': 'x', 'promptTokenCount': 32000}
    calls = m.SharedCalls(journal, 'other-model', {'prefill': 1.86, 'sample': 5.595}, 8192)
    try: calls.call('too-expensive', 'generation', row, lambda: 1)
    except m.ContractError: pass
    else: raise AssertionError('Cross-model ceiling was not enforced')
    assert len(journal.records) == 2
with tempfile.TemporaryDirectory() as directory:
    journal = m.Journal(Path(directory), {'test': 'interruption'})
    calls = m.SharedCalls(journal, 'model', {'prefill': 1.86, 'sample': 5.595}, 8192)
    try: calls.call('sample', 'generation', row, lambda: 1 / 0)
    except ZeroDivisionError: pass
    assert journal.records[-1]['maximumUSD'] == (32000 * 1.86 + 8192 * 5.595) / 1e6
    try: calls.call('sample', 'generation', row, lambda: 1)
    except m.ContractError: pass
    else: raise AssertionError('Uncertain operation was replayed')

print('PASS: both native SDK training/sampling/NLL paths, causal masks, full optimizer restore, native EOS, shared budget and interrupted-dispatch refusal; no network')

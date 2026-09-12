#!/usr/bin/env python3
"""No-network regression of fresh/fresh/restored controls and native loss masks."""
import copy
import contextlib
import importlib.util
import io
from pathlib import Path
import socket
import tempfile

socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError('Network prohibited'))


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(file))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


d = load('client_control', 'diagnose-phase1-qwen-client-control.py')
with contextlib.redirect_stdout(io.StringIO()):
    f = load('native_control_checks', 'check-phase1-qwen-model-execution.py')


def forward(self, data, loss):
    trained, state = list(self.service.trained), copy.deepcopy(self.state)
    result = self.forward_backward(data, loss)
    self.service.trained = trained
    assert self.state == state
    return result


def restore(self, path):
    assert path.startswith('optimizer/')
    self.state = copy.deepcopy(self.service.saved[path])
    return f.Future({'path': path})


f.Trainer.forward = forward
f.Trainer.load_state_with_optimizer = restore
for key in ('qwen36_hybrid', 'qwen35_base'):
    tok, renderer = d.m.local_model(key)
    native = d.m.prep.native_runtime_from_tokenizer(key, tok)
    rows = {}
    for i in range(3):
        event = {'exampleID': str(i), 'experimentBlockID': 'control', 'targetEventID': str(i),
            'targetText': f' author 🧠 <|paste|> {i}\n'}
        event['target'] = {'segments': [{'type': 'authored_text', 'content': event['targetText']}]}
        rows[str(i)] = d.m.prep.supervised_row(event, f'History and cursor {i}', native)
    spec = f.report['models'][key]
    cfg = copy.deepcopy(spec['trainingContract']); cfg['generation'] = {**spec['generation'], 'seed': 17}
    service = f.Service(spec['model'], spec['nativeStopTokenIDs'][0], rows)
    bridge = d.r.DiagnosticBridge(service, f.tinker, tok, renderer, cfg)
    schedule = d.schedule(list(rows))
    assert len({op['key'] for op in schedule}) == len(schedule)
    assert sum(op['action'] == 'update' for op in schedule) == 7
    assert sum(op['action'] == 'forward' for op in schedule) == 20
    assert sum(op['action'] == 'nll' for op in schedule) == 12
    with tempfile.TemporaryDirectory() as temp:
        journal = d.m.Journal(Path(temp), {'test': key})
        calls = d.m.SharedCalls(journal, key, {'train': 0., 'prefill': 0., 'sample': 0.}, 512)
        result = d.experiment(bridge, rows, schedule,
            lambda op, row, fn: calls.call(op['key'], op['kind'], row, fn))
        assert result['finding'] == 'discrepancy_not_reproduced_on_selected_probes'
        assert service.trained == ['0', '0', '1', '1', '2', '2', '2']
        assert service.saved['optimizer/post-A-checkpoint-optimizer'] == service.saved['optimizer/post-C-checkpoint-optimizer']
        values = {x['key'].split('/', 1)[1]: x['value'] for x in journal.records if x['kind'] == 'operation_result'}
        for op in schedule:
            if op['action'] == 'forward': assert values[op['key']]['optimizerUpdatePerformed'] is False
        # A cross-client discrepancy is not mislabeled a restore defect.
        def changed(value): return d.m.nll_result([x-.1 for x in value['targetLogprobs']])
        mutated = copy.deepcopy(values)
        for i in range(3): mutated[f'pre/B/forward/{i}'] = changed(values[f'pre/B/forward/{i}'])
        assert d.comparisons(mutated)['finding'] == 'independent_fresh_clients_differ_restore_not_isolated'
        mutated = copy.deepcopy(values)
        for i in range(3): mutated[f'pre/C/forward/{i}'] = changed(values[f'pre/C/forward/{i}'])
        assert d.comparisons(mutated)['finding'] == 'restore_specific_training_forward_difference_before_next_update'
        mutated = copy.deepcopy(values)
        mutated['post/C/nll/0'] = changed(values['post/C/nll/0'])
        assert d.comparisons(mutated)['finding'] == 'difference_emerges_after_next_update'
        assert not d.comparisons(mutated)['mainRunAuthorized']
        try: calls.call(schedule[0]['key'], 'admin', rows['0'], lambda: 1/0)
        except d.m.ContractError: pass
        else: raise AssertionError('Paid operation replay was permitted')

with tempfile.TemporaryDirectory() as temp:
    journal = d.m.Journal(Path(temp), {'test': 'uncertainty'})
    calls = d.m.SharedCalls(journal, 'control', {'train': 1., 'prefill': 1., 'sample': 1.}, 512, carried_usd=19.49)
    row = {'exampleID': 'x', 'trainingDatumPositions': 1000}
    try: calls.call('interrupted', 'train', row, lambda: (_ for _ in ()).throw(RuntimeError('interrupted')))
    except RuntimeError: pass
    assert sum(x.get('maximumUSD', 0) for x in journal.records) == .001
    try: calls.call('interrupted', 'train', row, lambda: 1)
    except d.m.ContractError: pass
    else: raise AssertionError('Uncertain operation replayed')
    try: calls.call('over-budget', 'train', {**row, 'trainingDatumPositions': 100000}, lambda: 1)
    except d.m.ContractError: pass
    else: raise AssertionError('Shared budget ignored')

print('PASS: exact frozen schedule; independent fresh clients; full optimizer continuation; both native EOS/masks; discrepancy classification; uncertain costs; no replay; shared ceiling; no network')

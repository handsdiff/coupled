#!/usr/bin/env python3
"""No-network pilot recovery tests and saved-response audit."""
import argparse
import copy
import importlib.util
import json
import math
from pathlib import Path
import socket
import tempfile

socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError('Network prohibited'))
spec = importlib.util.spec_from_file_location('lr_run', Path(__file__).with_name('run-phase1-qwen-lr-pilot.py'))
r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)
m = r.m


class Crash(BaseException):
    pass


class Fake:
    def __init__(self, contract, world):
        self.contract, self.world = contract, world

    def trainer(self, name, parent=None):
        self.world['creates'].append((name, parent))
        return {'name': name, 'steps': 0}

    def train(self, trainer, row):
        self.world['train'].append((trainer['name'], row['exampleID']))
        if self.world.get('crash_train'):
            self.world['crash_train'] = False
            raise Crash()
        if self.world.get('error_train'):
            self.world['error_train'] = False
            raise OSError('synthetic transient')
        trainer['steps'] += 1
        return {'meanNLL': 1., 'weightedNLLSum': 2., 'lossBearingTokens': 2,
                'targetLogprobs': [-1., -1.], 'inputTokens': row['trainingDatumPositions'], 'latencySeconds': .1}

    def checkpoint(self, trainer, name):
        self.world['checkpoints'].append((name, trainer['steps']))
        return {'samplerCheckpointPath': 'sample:' + name, 'optimizerStatePath': 'state:' + name}

    def sampler(self, checkpoint=None):
        return checkpoint

    def generate(self, sampler, row):
        self.world['generations'].append((sampler, row['exampleID']))
        if self.world.get('crash_gen'):
            self.world['crash_gen'] = False
            raise Crash()
        eos = self.contract.get('generation', {}).get('stopTokenIDs', [248046])[0]
        return {'prediction': 'x', 'predictionTokenIDs': [1, eos], 'latencySeconds': .1,
                'inputTokens': row['promptTokenCount'], 'outputTokens': 2, 'stopReason': 'stop'}

    def nll(self, sampler, row):
        self.world['nll'].append((sampler, row['exampleID']))
        return {'meanNLL': 1., 'weightedNLLSum': 2., 'lossBearingTokens': 2,
                'targetLogprobs': [-1., -1.], 'inputTokens': row['promptTokenCount'] + 2, 'latencySeconds': .1}


def tests():
    base_native = {'model':'Qwen/Qwen3.5-35B-A3B-Base', 'reasoning':'not_applicable',
        'tokenizer':{'nativeStopTokenIDs':[248044]},
        'arms':[{'contract':{'model':'Qwen/Qwen3.5-35B-A3B-Base', 'reasoning':None,
                            'generation':{'stopTokenIDs':[248044]}, 'loss':{'nativeTerminatorTokenID':248044}}}]}
    assert r.native_spec(base_native) == ('qwen35_base', 248044)
    for field in ('tokenizer', 'generation', 'loss'):
        wrong = copy.deepcopy(base_native)
        if field == 'tokenizer': wrong['tokenizer']['nativeStopTokenIDs'] = [248046]
        elif field == 'generation': wrong['arms'][0]['contract']['generation']['stopTokenIDs'] = [248046]
        else: wrong['arms'][0]['contract']['loss']['nativeTerminatorTokenID'] = 248046
        try: r.native_spec(wrong)
        except m.ContractError: pass
        else: raise AssertionError('Mixed Base/hybrid native contract accepted')
    ids = ['t1', 't2', 'e1', 'e2']
    rows = {eid: {'exampleID': eid, 'promptTokenCount': 10, 'lossBearingTokenCount': 2,
                  'trainingDatumPositions': 11, 'modelInputSHA256': 'input', 'targetSHA256': 'target'} for eid in ids}
    report = {'trainIDs': ids[:2], 'evaluationIDs': ids[2:], 'prices': {'train': 1., 'prefill': 1., 'sample': 1.},
              'arms': [{'name': str(lr), 'contract': {'optimizer': {'learningRate': lr}}, 'trainingOrder': ids[:2]} for lr in r.p.RATES]}
    for mode in ('normal', 'crash_gen', 'crash_train', 'error_train'):
        with tempfile.TemporaryDirectory(prefix='coupled-lr-test-') as td:
            world = {k: [] for k in ('creates', 'train', 'generations', 'nll', 'checkpoints')}
            if mode != 'normal': world[mode] = True
            journal = m.Journal(Path(td), {'test': 1})
            make = lambda: r.Executor(report, rows, journal, lambda c: Fake(c, world))
            try:
                with journal.exclusive(): make().run()
            except Crash:
                # Fresh Journal models a process restart, with no live trainer.
                journal = m.Journal(Path(td), {'test': 1})
                with journal.exclusive(): make().run()
            assert len(world['generations']) == 8 + (mode == 'crash_gen')
            assert len(world['nll']) == 8
            assert len(world['train']) == 6 + (mode in ('crash_train', 'error_train'))
            assert all(e in ids[:2] for _, e in world['train'])
            if mode in ('crash_train', 'error_train'):
                assert world['creates'][1][1]['optimizerStatePath'].endswith('-initial')
            before = copy.deepcopy(world)
            with journal.exclusive(): make().run()
            assert world == before, 'Completed work replayed on resume'
            begins = [x for x in journal.records if x['kind'] == 'operation_begin']
            assert math.isclose(make().calls.spent(), sum(x['maximumUSD'] for x in begins))
            with journal.exclusive():
                calls = r.Calls(journal, report['prices'], ceiling=0.5)
                try: calls.call('unaffordable', 'generation', rows['e1'], lambda: (_ for _ in ()).throw(AssertionError('Dispatched over cap')))
                except m.ContractError: pass
                else: raise AssertionError('Budget guard failed')
            # Incomplete journal must fail without truncating or dispatching.
            with journal.path.open('ab') as f: f.write(b'{')
            try: m.Journal(Path(td), {'test': 1})
            except m.ContractError: pass
            else: raise AssertionError('Broken journal accepted')
    base_report = copy.deepcopy(report)
    base_report['arms'].append(copy.deepcopy(base_report['arms'][-1]))
    base_report['arms'][-1]['name'] = 'lr-0.0005'
    base_report['arms'][-1]['contract']['optimizer']['learningRate'] = 5e-4
    for arm in base_report['arms']:
        arm['contract']['model'] = base_native['model']
        arm['contract']['generation'] = {'stopTokenIDs':[248044]}
    with tempfile.TemporaryDirectory(prefix='coupled-base-four-rate-') as td:
        j = m.Journal(Path(td), {'base':1})
        world = {k:[] for k in ('creates','train','generations','nll','checkpoints')}
        with j.exclusive(): r.Executor(base_report, rows, j, lambda c:Fake(c,world)).run()
        assert len(world['train']) == 8 and len(world['generations']) == len(world['nll']) == 10
        assert len(world['creates']) == 4 and all(parent is None for _,parent in world['creates'])
        assert all(v['value']['predictionTokenIDs'][-1] == 248044 for v in j.results().values() if v['operation']=='generation')
        before = copy.deepcopy(world)
        with j.exclusive(): r.Executor(base_report, rows, j, lambda c:Fake(c,world)).run()
        assert world == before
    # Explicitly exercise crash after checkpoint result but before logical commit.
    with tempfile.TemporaryDirectory(prefix='coupled-lr-checkpoint-gap-') as td:
        j = m.Journal(Path(td), {'test': 2})
        world = {k: [] for k in ('creates', 'train', 'generations', 'nll', 'checkpoints')}
        first = report['arms'][0]['name']
        j.append({'kind': 'operation_result', 'key': first + '/checkpoint/0', 'operation': 'admin',
                  'value': {'samplerCheckpointPath': 'sample:saved', 'optimizerStatePath': 'state:saved'}})
        with j.exclusive(): r.Executor(report, rows, j, lambda c: Fake(c, world)).run()
        assert all(name != first for name, _ in world['train'])
    # An extension must not recreate/resample frozen or any previously trained arm.
    extra = r.extension_report(report)
    assert report['arms'][0]['contract']['optimizer']['learningRate'] == r.p.RATES[0]
    assert extra['arms'][0]['contract']['optimizer']['learningRate'] == 5e-4
    assert extra['arms'][0]['trainingOrder'] == report['arms'][-1]['trainingOrder']
    normalized = copy.deepcopy(extra['arms'][0]['contract'])
    normalized['optimizer']['learningRate'] = report['arms'][-1]['contract']['optimizer']['learningRate']
    assert normalized == report['arms'][-1]['contract']
    for mode in ('normal', 'crash_train', 'crash_gen'):
        with tempfile.TemporaryDirectory(prefix='coupled-lr-extension-') as td:
            world = {k: [] for k in ('creates', 'train', 'generations', 'nll', 'checkpoints')}
            if mode != 'normal': world[mode] = True
            j = m.Journal(Path(td), {'extension': 1})
            make = lambda: r.Executor(extra, rows, j, lambda c: Fake(c, world), carried=13.58, storage=1.5)
            try:
                with j.exclusive(): make().run()
            except Crash:
                j = m.Journal(Path(td), {'extension': 1})
                with j.exclusive(): make().run()
            assert len(world['generations']) == 2 + (mode == 'crash_gen')
            assert len(world['nll']) == 2
            assert len(world['train']) == 2 + (mode == 'crash_train')
            assert all(name == 'lr-0.0005' for name, _ in world['train'])
            assert world['creates'][0][1] is None, 'Extension warm-started a prior adapter'
            before = copy.deepcopy(world)
            with j.exclusive(): make().run()
            assert world == before
            with j.exclusive():
                calls = r.Calls(j, report['prices'], carried=19.99, storage=.02)
                try: calls.call('over-shared-cap', 'generation', rows['e1'], lambda: (_ for _ in ()).throw(AssertionError('Spent prior funds twice')))
                except m.ContractError: pass
                else: raise AssertionError('Carried costs omitted')
    print('LR execution tests passed: native source separate; no eval training; completed-score resume; bounded mutation restart; charged uncertainty; checkpoint gap; pre-dispatch budget; damaged journal.')


def audit(directory):
    plan = json.loads((directory / 'execution.json').read_text())
    prepared = Path(plan['preparedDirectory'])
    report, rows = r.load_prepared(prepared)
    carried = 0.
    storage = plan['checkpointStorage']['reserveUSD']
    if plan.get('extension'):
        prior = r.prior_binding(Path(plan['extension']['directory']), prepared)
        m.require(prior == plan['extension'], 'Previous artifacts/budget changed')
        report = r.extension_report(report)
        m.require(plan['training'] == report['arms'] and plan['budget'] == r.extension_budget(report, rows, prior), 'Changed extension contract')
        carried = prior['priorReservedTokenUSD'] + prior['priorStorageReserveUSD']
    m.require(m.file_hash(prepared / 'preparation.json') == plan['preparedSHA256'], 'Changed preparation')
    result = json.loads((directory / 'result.json').read_text())
    m.require(result['status'] == 'complete' and result['executionSHA256'] == m.file_hash(directory / 'execution.json'), 'Incomplete/mismatched execution')
    j = m.Journal(directory, {'executionSHA256': result['executionSHA256']})
    m.require(result['journalSHA256'] == m.file_hash(j.path), 'Journal modified')
    begins = [x for x in j.records if x['kind'] == 'operation_begin']
    outputs = j.results()
    for op in begins:
        row = rows[op['exampleID']] if op.get('exampleID') else {}
        m.require(math.isclose(op['maximumUSD'], m.original.charge(op['operation'], row, report['prices']), abs_tol=1e-12), 'Wrong cost reservation')
    arms = ([] if report.get('reuseFrozenBaseline') else ['frozen']) + [a['name'] for a in report['arms']]
    m.require(all(op['key'].split('/')[0] in arms for op in begins), 'Unexpected/duplicated prior arm dispatched')
    for arm in arms:
        for eid in report['evaluationIDs']:
            row = rows[eid]
            for kind in ('generation', 'nll'):
                key = f'{arm}/score/{eid}/{kind}'; value = outputs[key]['value']
                request = next(x for x in begins if x['key'] == key)
                m.require(request['identity']['targetSHA256'] == row['targetSHA256']
                          and request['identity']['modelInputSHA256'] == row['modelInputSHA256'], 'Score task identity mismatch')
                if kind == 'nll':
                    m.require(m.nll_result(value['targetLogprobs']) == {k: value[k] for k in ('targetLogprobs', 'meanNLL', 'weightedNLLSum', 'lossBearingTokens')}, 'NLL recomputation failed')
                    m.require(value['lossBearingTokens'] == row['lossBearingTokenCount'], 'Wrong target likelihood mask')
                else:
                    m.require(value['inputTokens'] == row['promptTokenCount'] and len(value['predictionTokenIDs']) == value['outputTokens'] <= 512, 'Generation token accounting mismatch')
    for arm in report['arms']:
        name = arm['name']; cp = outputs[name + '/final-checkpoint']['value']; attempt = cp['trainingAttemptOrdinal']
        for i, eid in enumerate(arm['trainingOrder']):
            key = f'{name}/train/{attempt}/{i}'; value = outputs[key]['value']; row = rows[eid]
            request = next(x for x in begins if x['key'] == key)
            m.require(request['exampleID'] == eid and eid not in report['evaluationIDs'], 'Wrong train order/membership')
            m.require(value['lossBearingTokens'] == row['lossBearingTokenCount'], 'Training mask count mismatch')
            m.require(math.isclose(sum(-x for x in value['targetLogprobs']), value['forwardMetrics']['loss:sum'], rel_tol=2e-5, abs_tol=1e-4), 'Returned loss sum differs')
        initial = outputs[name + '/initial-checkpoint']['value']
        for start in j.records:
            if start['kind'] == 'training_begin' and start['arm'] == name:
                m.require(start['exampleIDs'] == arm['trainingOrder'] and start['initialOptimizerStatePath'] == initial['optimizerStatePath'], 'Retry parent/order mismatch')
    cost = sum(x['maximumUSD'] for x in begins)
    m.require(math.isclose(result['reservedTokenCostUSD'], cost, abs_tol=1e-9) and carried + cost + storage <= 20, 'Budget audit failed')
    m.require(math.isclose(result['includingStorageReserveUSD'], carried + cost + storage, abs_tol=1e-9), 'Carried/storage total mismatch')
    m.require(result['summary'] == r.summarize(report, j), 'Summary changed')
    m.require(sum(x['kind'] == 'training_begin' and x['attemptOrdinal'] > 0 for x in j.records) <= 1, 'Training retry bound exceeded')
    for kind in ('generation', 'nll'):
        m.require(sum(x['operation'] == kind and x['attemptOrdinal'] > 0 for x in begins) <= 1, 'Scoring retry bound exceeded')
    report_out = {'status': 'audit_passed', 'distinctTrainingExamples': 50, 'distinctFutureExamples': 50,
                  'scoredGenerations': 50 * len(arms), 'targetNLLCalls': 50 * len(arms), 'committedOptimizerSteps': 50 * len(report['arms']),
                  'reservedTokenCostUSD': cost, 'withStorageReserveUSD': carried + cost + storage,
                  'filesSHA256': {n: m.file_hash(directory / n) for n in ('execution.json', 'result.json', 'operations.jsonl')},
                  'auditCodeSHA256': m.file_hash(Path(__file__))}
    m.original.save(directory / 'audit.json', report_out)
    print(json.dumps(report_out, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--audit', type=Path); args = parser.parse_args()
    audit(args.audit) if args.audit else tests()

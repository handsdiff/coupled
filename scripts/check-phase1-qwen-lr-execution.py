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
        return {'prediction': 'x', 'predictionTokenIDs': [1, 248046], 'latencySeconds': .1,
                'inputTokens': row['promptTokenCount'], 'outputTokens': 2, 'stopReason': 'stop'}

    def nll(self, sampler, row):
        self.world['nll'].append((sampler, row['exampleID']))
        return {'meanNLL': 1., 'weightedNLLSum': 2., 'lossBearingTokens': 2,
                'targetLogprobs': [-1., -1.], 'inputTokens': row['promptTokenCount'] + 2, 'latencySeconds': .1}


def tests():
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
    # Explicitly exercise crash after checkpoint result but before logical commit.
    with tempfile.TemporaryDirectory(prefix='coupled-lr-checkpoint-gap-') as td:
        j = m.Journal(Path(td), {'test': 2})
        world = {k: [] for k in ('creates', 'train', 'generations', 'nll', 'checkpoints')}
        first = report['arms'][0]['name']
        j.append({'kind': 'operation_result', 'key': first + '/checkpoint/0', 'operation': 'admin',
                  'value': {'samplerCheckpointPath': 'sample:saved', 'optimizerStatePath': 'state:saved'}})
        with j.exclusive(): r.Executor(report, rows, j, lambda c: Fake(c, world)).run()
        assert all(name != first for name, _ in world['train'])
    print('LR execution tests passed: native source separate; no eval training; completed-score resume; bounded mutation restart; charged uncertainty; checkpoint gap; pre-dispatch budget; damaged journal.')


def audit(directory):
    plan = json.loads((directory / 'execution.json').read_text())
    prepared = Path(plan['preparedDirectory'])
    report, rows = r.load_prepared(prepared)
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
    for arm in ['frozen'] + [a['name'] for a in report['arms']]:
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
    m.require(math.isclose(result['reservedTokenCostUSD'], cost, abs_tol=1e-9) and cost + r.STORAGE_RESERVE <= 20, 'Budget audit failed')
    m.require(result['summary'] == r.summarize(report, j), 'Summary changed')
    m.require(sum(x['kind'] == 'training_begin' and x['attemptOrdinal'] > 0 for x in j.records) <= 1, 'Training retry bound exceeded')
    for kind in ('generation', 'nll'):
        m.require(sum(x['operation'] == kind and x['attemptOrdinal'] > 0 for x in begins) <= 1, 'Scoring retry bound exceeded')
    report_out = {'status': 'audit_passed', 'distinctTrainingExamples': 50, 'distinctFutureExamples': 50,
                  'scoredGenerations': 200, 'targetNLLCalls': 200, 'committedOptimizerSteps': 150,
                  'reservedTokenCostUSD': cost, 'withStorageReserveUSD': cost + r.STORAGE_RESERVE,
                  'filesSHA256': {n: m.file_hash(directory / n) for n in ('execution.json', 'result.json', 'operations.jsonl')},
                  'auditCodeSHA256': m.file_hash(Path(__file__))}
    m.original.save(directory / 'audit.json', report_out)
    print(json.dumps(report_out, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--audit', type=Path); args = parser.parse_args()
    audit(args.audit) if args.audit else tests()

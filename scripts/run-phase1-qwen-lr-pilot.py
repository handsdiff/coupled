#!/usr/bin/env python3
"""Resumable, separately authorized $20 future-write LR pilot, not the main run.

One shared frozen baseline, three fresh adapters, 50 updates apiece, then 50
future queries. Every dispatch is fsynced before provider submission. Uncertain
cost is never refunded. A partial update restarts from that arm's INITIAL full
optimizer checkpoint once globally; completed scores are never resampled.
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import math
import os
from pathlib import Path
import resource
import statistics
import subprocess
import time

spec = importlib.util.spec_from_file_location('lr_prepare', Path(__file__).with_name('prepare-phase1-qwen-lr-pilot.py'))
p = importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
m = p.m
VERSION = 'phase1-qwen36-future-lr-execution-v1'
CEILING = 20.0
STORAGE_RESERVE = 4.0


def runtime():
    value = m.runtime_binding()
    for name in ('prepare-phase1-qwen-lr-pilot.py', 'check-phase1-qwen-lr-pilot.py',
                 'run-phase1-qwen-lr-pilot.py', 'check-phase1-qwen-lr-execution.py'):
        path = m.ROOT / 'scripts' / name
        value['filesSHA256'][str(path)] = m.file_hash(path)
    return value


def load_prepared(directory):
    report = json.loads((directory / 'preparation.json').read_text())
    for path, digest in {**report['sourceBindingsSHA256'], **report['codeSHA256']}.items():
        m.require(m.file_hash(path) == digest, 'Prepared source or code changed: ' + path)
    for name, field in (('native-rows.jsonl', 'nativeRowsSHA256'), ('cohort.jsonl', 'cohortSHA256')):
        m.require(m.file_hash(directory / name) == report[field], 'Prepared rows changed')
    audit = json.loads((directory / 'audit.json').read_text())
    m.require(audit['status'] == 'audit_passed_OFFLINE_ONLY', 'Offline native audit missing')
    m.require(audit['filesSHA256']['preparation.json'] == m.file_hash(directory / 'preparation.json'), 'Stale native audit')
    m.require(report['model'] == 'Qwen/Qwen3.6-35B-A3B' and report['reasoning'] == 'off', 'Wrong model mode')
    m.require(report['projectID'] == m.PROJECT and report['counts']['uniqueTrainingExamples'] == 50
              and report['counts']['uniqueFutureExamples'] == 50, 'Wrong project or split')
    cohort = [json.loads(line) for line in (directory / 'cohort.jsonl').open()]
    blocks = json.loads((Path(report['sourcePack']) / 'blocks.json').read_text())
    m.require(p.select(cohort, blocks) == (report['trainIDs'], report['evaluationIDs']), 'Split changed')
    return report, p.NativeRows(directory / 'native-rows.jsonl')


def prepare(args):
    report, _ = load_prepared(args.prepared)
    m.require(not args.output.exists(), 'Use a new execution directory')
    budget = {**report['budget'], 'checkpointStorageReserveUSD': STORAGE_RESERVE,
              'proposedAuthorizationUSD': report['budget']['proposedAuthorizationUSD'] + STORAGE_RESERVE - 1.0}
    m.require(budget['proposedAuthorizationUSD'] <= CEILING, 'Plan exceeds separate $20 ceiling')
    m.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=m.ROOT, text=True).strip(), 'Commit before freezing execution')
    args.output.mkdir(parents=True)
    plan = {'version': VERSION, 'preparedDirectory': str(args.prepared.resolve()),
            'preparedSHA256': m.file_hash(args.prepared / 'preparation.json'),
            'auditSHA256': m.file_hash(args.prepared / 'audit.json'),
            'implementationCommit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=m.ROOT, text=True).strip(),
            'runtime': runtime(), 'projectID': m.PROJECT, 'hardCeilingUSD': CEILING,
            'authorization': 'Separate additional $20 explicitly authorized by user on 2026-09-12 for this pilot; no full-run authorization.',
            'training': report['arms'], 'budget': budget,
            'schedule': 'Frozen future block once; each fresh LR arm trains first 50 once then scores same next 50. Never train evaluation block.',
            'recovery': {'maximumTrainingArmRestartsAcrossRun': 1,
                         'maximumScoreRetriesAcrossRun': {'generation': 1, 'nll': 1},
                         'policy': 'Retain uncertain dispatch maximum cost. Restart interrupted training from initial full optimizer state; never resume partial updates blindly.'},
            'checkpointStorage': {'ttlSeconds': 604800, 'reserveUSD': STORAGE_RESERVE,
                                  'cookbookTrainableParameterEstimate': 561463296,
                                  'conservativeBudgetBasis': 'Eight checkpoint pairs at 32 bytes per trainable parameter retained seven days: under $4 at quoted storage rate.',
                                  'priceUSDPerGBMonth': 0.10, 'source': 'https://tinker-docs.thinkingmachines.ai/tinker/models/'},
            'mainRunAuthorized': False}
    m.original.save(args.output / 'execution.json', plan)
    print(json.dumps({'status': 'frozen', 'executionSHA256': m.file_hash(args.output / 'execution.json'),
                      'scheduledTokenMaximumUSD': report['budget']['scheduledTokenMaximumUSD'],
                      'hardCeilingUSD': CEILING, 'providerCalls': 0}))


class Calls:
    def __init__(self, journal, prices, ceiling=CEILING, storage=STORAGE_RESERVE):
        self.journal, self.prices, self.ceiling, self.storage = journal, prices, ceiling, storage

    def spent(self):
        return sum(r['maximumUSD'] for r in self.journal.records if r['kind'] == 'operation_begin')

    def value(self, key):
        values = [r['value'] for r in self.journal.records if r['kind'] == 'operation_result' and r['key'] == key]
        m.require(len(values) <= 1, 'Duplicate result')
        return values[0] if values else None

    def attempt_count(self, key):
        return sum(r['kind'] == 'operation_begin' and r['key'] == key for r in self.journal.records)

    def call(self, key, kind, row, fn, identity=None):
        result = self.value(key)
        if result is not None:
            return result
        while True:
            count = self.attempt_count(key)
            if count:
                m.require(kind in ('generation', 'nll'), 'Uncertain mutation/admin must not replay: ' + key)
                retries = sum(r['kind'] == 'operation_begin' and r['operation'] == kind and r.get('attemptOrdinal', 0) > 0
                              for r in self.journal.records)
                m.require(count == 1 and retries == 0, 'Bounded scoring retry exhausted: ' + key)
            maximum = m.original.charge(kind, row, self.prices)
            m.require(self.spent() + maximum + self.storage <= self.ceiling, 'Budget reached BEFORE dispatch')
            self.journal.append({'kind': 'operation_begin', 'key': key, 'operation': kind,
                                 'attemptOrdinal': count, 'maximumUSD': maximum, 'exampleID': row.get('exampleID'),
                                 'identity': identity or {}, 'at': m.utc()})
            started = time.monotonic()
            try:
                result = fn()
            except Exception as error:
                self.journal.append({'kind': 'operation_error', 'key': key, 'operation': kind,
                                     'attemptOrdinal': count, 'errorType': type(error).__name__, 'at': m.utc()})
                if isinstance(error, m.ContractError) or kind not in ('generation', 'nll'):
                    raise
                continue
            elapsed = time.monotonic() - started
            self.journal.append({'kind': 'operation_result', 'key': key, 'operation': kind,
                                 'attemptOrdinal': count, 'value': result, 'wallSeconds': elapsed, 'at': m.utc()})
            print(json.dumps({'completed': key, 'seconds': round(elapsed, 2),
                              'reservedTokenCostUSD': round(self.spent(), 5)}), flush=True)
            return result


class PilotBridge(m.NativeBridge):
    def generate(self, sampler, row):
        # Preserve even malformed model output. A parser failure is a recorded
        # prediction failure, not permission to resample until a good answer.
        c = self.contract['generation']; start = time.monotonic()
        response = sampler.sample(prompt=self.sdk.ModelInput.from_ints(row['promptTokenIDs']), num_samples=1,
            sampling_params=self.sdk.SamplingParams(max_tokens=c['maximumTokens'], temperature=c['temperature'],
                                                    seed=c['seed'], stop=c['stopTokenIDs'])).result()
        elapsed = time.monotonic() - start
        m.require(len(response.sequences) == 1, 'Unexpected generation count')
        sequence = response.sequences[0]; ids = list(sequence.tokens)
        m.require(len(ids) <= c['maximumTokens'], 'Provider exceeded output ceiling')
        stop = str(getattr(sequence.stop_reason, 'value', sequence.stop_reason))
        added = (not ids or ids[-1] != self.terminator) and stop in ('stop', 'stop_sequence', 'eos')
        result = {'predictionTokenIDs': ids, 'rawProviderResponse': m.plain(response),
                  'rawDecodedPrediction': self.tokenizer.decode(ids, clean_up_tokenization_spaces=False),
                  'latencySeconds': elapsed, 'inputTokens': len(row['promptTokenIDs']), 'outputTokens': len(ids),
                  'stopReason': stop, 'parserOnlyTerminatorAdded': added}
        try:
            message, termination = self.renderer.parse_response(ids + [self.terminator] if added else ids)
            result.update(prediction=m.prep.get_text_content(message), parseTermination=str(termination.value), parseValid=True)
        except Exception as error:
            result.update(prediction='', parseValid=False, parseErrorType=type(error).__name__)
        return result


class Executor:
    def __init__(self, report, rows, journal, bridge_factory):
        self.report, self.rows, self.journal, self.factory = report, rows, journal, bridge_factory
        self.calls = Calls(journal, report['prices'])

    def score(self, name, bridge, checkpoint=None):
        ids = self.report['evaluationIDs']
        if all(self.calls.value(f'{name}/score/{eid}/{kind}') is not None for eid in ids for kind in ('generation', 'nll')):
            return
        sampler = bridge.sampler(checkpoint)
        for eid in ids:
            row = self.rows[eid]
            identity = {'arm': name, 'checkpointPath': checkpoint, 'modelInputSHA256': row['modelInputSHA256'],
                        'targetSHA256': row['targetSHA256']}
            for kind in ('generation', 'nll'):
                function = bridge.generate if kind == 'generation' else bridge.nll
                self.calls.call(f'{name}/score/{eid}/{kind}', kind, row,
                                lambda f=function, r=row: f(sampler, r), identity)

    def train(self, arm, bridge):
        name, order = arm['name'], arm['trainingOrder']
        final = self.calls.value(f'{name}/final-checkpoint')
        if final is not None:
            return final
        initial = self.calls.value(f'{name}/initial-checkpoint')
        trainer = None
        if initial is None:
            # Client creation is non-token-billed. Saving its initial optimizer
            # state is journaled before any updates and is the only retry parent.
            trainer = bridge.trainer(name)
            initial = self.calls.call(f'{name}/initial-checkpoint', 'admin', {},
                                      lambda: bridge.checkpoint(trainer, name + '-initial'))
        while True:
            previous = [r for r in self.journal.records if r['kind'] == 'training_begin' and r['arm'] == name]
            ordinal = len(previous)
            if previous:
                retries = sum(r['kind'] == 'training_begin' and r['attemptOrdinal'] > 0 for r in self.journal.records)
                m.require(ordinal == 1 and retries == 0, 'One whole-arm training recovery already used')
                trainer = None
            total = sum(m.original.charge('train', self.rows[e], self.report['prices']) for e in order)
            m.require(self.calls.spent() + total + self.calls.storage <= CEILING, 'Cannot afford complete update')
            if trainer is None:
                trainer = bridge.trainer(name, initial)
            self.journal.append({'kind': 'training_begin', 'arm': name, 'attemptOrdinal': ordinal,
                                 'initialOptimizerStatePath': initial['optimizerStatePath'],
                                 'exampleIDs': order, 'at': m.utc()})
            try:
                for index, eid in enumerate(order):
                    m.require(eid in self.report['trainIDs'] and eid not in self.report['evaluationIDs'], 'Evaluation target in training')
                    row = self.rows[eid]
                    self.calls.call(f'{name}/train/{ordinal}/{index}', 'train', row,
                                    lambda r=row: bridge.train(trainer, r), {'arm': name, 'position': index, 'attemptOrdinal': ordinal})
                # Use attempt-specific checkpoint operations, then a separate
                # atomic logical commit. A crash between these reuses saved paths.
                cp = self.calls.call(f'{name}/checkpoint/{ordinal}', 'admin', {},
                                     lambda: bridge.checkpoint(trainer, name + f'-final-{ordinal}'))
                self.journal.append({'kind': 'operation_result', 'key': f'{name}/final-checkpoint',
                                     'operation': 'logical_commit', 'value': {**cp, 'trainingAttemptOrdinal': ordinal}, 'at': m.utc()})
                return {**cp, 'trainingAttemptOrdinal': ordinal}
            except m.ContractError:
                raise
            except Exception:
                # Restart from untouched initial Adam state, not a partially
                # mutated or new independently initialized adapter.
                continue

    def run(self):
        contract = self.report['arms'][0]['contract']
        self.score('frozen', self.factory(contract))
        for arm in self.report['arms']:
            bridge = self.factory(arm['contract'])
            # Repair the narrow crash gap after saved checkpoint / before commit.
            name = arm['name']
            saved = [(r['key'], r['value']) for r in self.journal.records if r['kind'] == 'operation_result'
                     and r['key'].startswith(name + '/checkpoint/')]
            if saved and self.calls.value(name + '/final-checkpoint') is None:
                key, cp = saved[-1]
                cp = {**cp, 'trainingAttemptOrdinal': int(key.rsplit('/', 1)[1])}
                self.journal.append({'kind': 'operation_result', 'key': name + '/final-checkpoint',
                                     'operation': 'logical_commit', 'value': cp, 'at': m.utc()})
            checkpoint = self.train(arm, bridge)
            self.score(name, bridge, checkpoint['samplerCheckpointPath'])


def summarize(report, journal):
    results = journal.results()
    summary = {}
    for name in ['frozen'] + [a['name'] for a in report['arms']]:
        generations = [r['value'] for key, r in results.items() if key.startswith(name + '/score/') and key.endswith('/generation')]
        nll = [r['value'] for key, r in results.items() if key.startswith(name + '/score/') and key.endswith('/nll')]
        times = [r['latencySeconds'] for r in generations]
        entry = {'generations': len(generations), 'nllQueries': len(nll),
                 'meanGenerationSeconds': statistics.mean(times) if times else None,
                 'medianGenerationSeconds': statistics.median(times) if times else None,
                 'emptyGenerations': sum(not r.get('prediction', '').strip() for r in generations),
                 'lengthStops': sum(r.get('stopReason') == 'length' for r in generations),
                 'macroMeanNLL': statistics.mean(r['meanNLL'] for r in nll) if nll else None,
                 'microMeanNLL': sum(r['weightedNLLSum'] for r in nll) / sum(r['lossBearingTokens'] for r in nll) if nll else None}
        summary[name] = entry
    return summary


def run(args):
    plan_path = args.output / 'execution.json'; plan = json.loads(plan_path.read_text())
    m.require(args.confirm_transfer and plan['hardCeilingUSD'] == CEILING and plan['projectID'] == m.PROJECT, 'Explicit separate-budget transfer gate')
    m.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=m.ROOT, text=True).strip(), 'Dirty worktree; do not mix implementation revisions')
    m.require(subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=m.ROOT, text=True).strip() == plan['implementationCommit'], 'Execution Git revision changed')
    m.require(runtime() == plan['runtime'], 'Runtime/dependencies changed')
    directory = Path(plan['preparedDirectory'])
    m.require(m.file_hash(directory / 'preparation.json') == plan['preparedSHA256']
              and m.file_hash(directory / 'audit.json') == plan['auditSHA256'], 'Prepared artifact changed')
    report, rows = load_prepared(directory)
    m.require(report['arms'] == plan['training'], 'Frozen hyperparameters changed')
    journal = m.Journal(args.output, {'executionSHA256': m.file_hash(plan_path)})
    with journal.exclusive():
        completed = args.output / 'result.json'
        if completed.exists() and json.loads(completed.read_text())['status'] == 'complete':
            print('Already complete; no provider calls'); return
        prices_doc = json.loads(subprocess.check_output(['curl', '--fail', '--silent', '--show-error', '--max-time', '30', m.original.PRICING_URL], text=True))
        price = next(v for v in prices_doc if v['tinker_id'] == report['model'])
        prices = {k: float(price[k].lstrip('$')) for k in ('train', 'prefill', 'sample')}
        m.require(prices == report['prices'], 'Provider pricing changed')
        m.original.save(args.output / 'pricing.json', {'checkedAt': m.utc(), 'prices': prices, 'source': m.original.PRICING_URL})
        import tinker
        os.environ['TINKER_API_KEY'] = m.original.api_key(args.env_file)
        os.environ.pop('HF_HUB_OFFLINE', None); os.environ.pop('TRANSFORMERS_OFFLINE', None)
        service = tinker.ServiceClient(project_id=m.PROJECT, user_metadata={'purpose': VERSION}, max_retries=0)
        proxy = m.original.NoAutomaticResampling(service)
        session = service.holder.get_session_id()
        journal.append({'kind': 'provider_session', 'sessionID': session, 'at': m.utc()})
        tok, renderer = m.local_model('qwen36_hybrid')
        capabilities = {x.model_name: x for x in service.get_server_capabilities().supported_models}
        m.require(capabilities[report['model']].max_context_length >= 65536, 'Model context unavailable')
        remote = proxy.create_sampling_client(base_model=report['model']).get_tokenizer()
        m.require(m.fingerprint(remote.get_vocab()) == report['tokenizer']['tokenizerVocabularySHA256'] == m.fingerprint(tok.get_vocab()), 'Complete vocabulary mismatch')
        for eid in report['trainIDs'] + report['evaluationIDs']:
            row = rows[eid]; ids = row['promptTokenIDs'] + row['completionTokenIDs']
            m.require(remote.decode(ids, clean_up_tokenization_spaces=False) == tok.decode(ids, clean_up_tokenization_spaces=False), 'Native decode mismatch')
            m.native_datum(row, tinker, 248046)
        journal.append({'kind': 'remote_tokenizer_verified', 'vocabularySHA256': report['tokenizer']['tokenizerVocabularySHA256'],
                        'examples': 100, 'providerModelRevision': 'unverified: only model name exposed', 'at': m.utc()})
        executor = Executor(report, rows, journal, lambda c: PilotBridge(proxy, tinker, tok, renderer, c))
        state = {'version': VERSION, 'status': 'running', 'executionSHA256': m.file_hash(plan_path),
                 'implementationCommit': plan['implementationCommit'], 'startedAt': m.utc(), 'sessionID': session}
        if completed.exists(): state['priorAttempt'] = json.loads(completed.read_text()).get('startedAt')
        m.original.save(completed, state)
        try:
            executor.run()
            state['status'] = 'complete'
        except BaseException as error:
            state.update(status='paused_requires_review_or_bounded_resume', errorType=type(error).__name__)
            raise
        finally:
            state.update(finishedAt=m.utc(), reservedTokenCostUSD=executor.calls.spent(),
                         includingStorageReserveUSD=executor.calls.spent() + STORAGE_RESERVE, hardCeilingUSD=CEILING,
                         summary=summarize(report, journal), memoryPeakMiB=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024**2,
                         journalSHA256=m.file_hash(journal.path))
            m.original.save(completed, state)
    print(json.dumps(state, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepared', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--confirm-transfer', action='store_true')
    parser.add_argument('--env-file', type=Path, default=m.ROOT / '.env')
    args = parser.parse_args()
    run(args) if args.execute else prepare(args)

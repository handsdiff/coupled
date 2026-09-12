#!/usr/bin/env python3
"""Bounded additional-model preflights, with a single shared $20 journal."""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


prep = module('qwen_model_prep', 'prepare-phase1-qwen-model-preflights.py')
original = module('qwen_reference_preflight', 'preflight-phase1-qwen38.py')
from phase1_qwen38_execution import (ContractError, Journal, TinkerBridge, file_hash,
    fingerprint, nll_result, plain, require, text_bpb_result, utc)

VERSION = 'phase1-qwen-model-preflight-execution-v1'
MODEL_ORDER = ['qwen38_reasoning', 'qwen36_hybrid', 'qwen35_base']
PROJECT = '10b258ab-25fe-45e0-a54b-fef023154281'


def native_datum(row, sdk, terminator):
    import numpy as np
    require(not row.get('generationOnly'), 'Reasoning-only row cannot train')
    prompt, target = row['promptTokenIDs'], row['completionTokenIDs']
    require(prompt and target and target[-1] == terminator and target.count(terminator) == 1,
            'Wrong native target termination')
    ids = prompt + target
    require(len(ids) <= 65536 and row['trainingDatumPositions'] == len(ids) - 1, 'Wrong sequence length')
    require(len(prompt) == row['promptTokenCount'] and len(target) == row['lossBearingTokenCount'], 'Wrong token counts')
    require(fingerprint(ids) == row['fullSequenceTokenSHA256'], 'Native row changed')
    weights = [0.] * (len(prompt) - 1) + [1.] * len(target)
    datum = sdk.Datum(model_input=sdk.ModelInput.from_ints(ids[:-1]), loss_fn_inputs={
        'target_tokens': sdk.TensorData.from_numpy(np.asarray(ids[1:], dtype=np.int64)),
        'weights': sdk.TensorData.from_numpy(np.asarray(weights, dtype=np.float32))})
    require(datum.model_input.to_ints() == ids[:-1] and datum.loss_fn_inputs['target_tokens'].to_numpy().tolist() == ids[1:]
            and datum.loss_fn_inputs['weights'].to_numpy().tolist() == weights, 'SDK causal shift changed')
    return datum


class NativeBridge(TinkerBridge):
    """Reuse checkpoint/optimizer policy; make EOS and answer extraction explicit."""
    @property
    def terminator(self): return self.contract['generation']['stopTokenIDs'][0]

    def generate(self, sampler, row):
        c = self.contract['generation']; start = time.monotonic()
        response = sampler.sample(prompt=self.sdk.ModelInput.from_ints(row['promptTokenIDs']), num_samples=1,
            sampling_params=self.sdk.SamplingParams(max_tokens=c['maximumTokens'], temperature=c['temperature'],
                                                    seed=c['seed'], stop=c['stopTokenIDs'])).result()
        elapsed = time.monotonic() - start
        require(len(response.sequences) == 1, 'Unexpected generation count')
        s = response.sequences[0]; ids = list(s.tokens)
        require(len(ids) <= c['maximumTokens'], 'Output exceeds frozen ceiling')
        stop = str(getattr(s.stop_reason, 'value', s.stop_reason))
        if self.contract.get('reasoning'):
            parsed = prep.parse_reasoning_output(self.renderer, ids, stop)
            close = self.tokenizer.encode('</think>', add_special_tokens=False)[0]
            parsed['reasoningTokenCount'] = ids.index(close) + 1 if close in ids else len(ids)
            parsed['answerAndTerminationTokenCount'] = len(ids) - parsed['reasoningTokenCount']
        else:
            added = (not ids or ids[-1] != self.terminator) and stop in ('stop', 'stop_sequence', 'eos')
            message, termination = self.renderer.parse_response(ids + [self.terminator] if added else ids)
            parsed = {'prediction': prep.get_text_content(message), 'parseTermination': str(termination.value),
                      'parserOnlyTerminatorAdded': added, 'stopReason': stop,
                      'rawDecodedPrediction': self.tokenizer.decode(ids, clean_up_tokenization_spaces=False)}
        return {**parsed, 'predictionTokenIDs': ids, 'rawProviderResponse': plain(response),
                'latencySeconds': elapsed, 'inputTokens': len(row['promptTokenIDs']), 'outputTokens': len(ids)}

    def nll(self, sampler, row):
        native_datum(row, self.sdk, self.terminator)
        target = row['completionTokenIDs']; text = self.tokenizer.decode(target[:-1], clean_up_tokenization_spaces=False)
        require(prep.text_hash(text) == row['targetSHA256'], 'NLL target bytes changed')
        ids = row['promptTokenIDs'] + target; start = time.monotonic()
        values = sampler.compute_logprobs(self.sdk.ModelInput.from_ints(ids)).result()
        require(len(values) == len(ids), 'NLL response length mismatch')
        result = nll_result(values[len(row['promptTokenIDs']):])
        return {**result, **text_bpb_result(result['targetLogprobs'], text),
                'latencySeconds': time.monotonic() - start, 'inputTokens': len(ids), 'fullLogprobsSHA256': fingerprint(values)}

    def train(self, trainer, row):
        datum = native_datum(row, self.sdk, self.terminator); c = self.contract['optimizer']; start = time.monotonic()
        forward = trainer.forward_backward([datum], 'cross_entropy')
        step = trainer.optim_step(self.sdk.AdamParams(learning_rate=c['learningRate'], beta1=c['beta1'], beta2=c['beta2'],
            eps=c['epsilon'], weight_decay=c['weightDecay'], grad_clip_norm=c['gradientClipNorm']))
        output, optimized = forward.result(), step.result()
        values = output.loss_fn_outputs[0]['logprobs'].tolist()
        require(len(values) == row['trainingDatumPositions'], 'Training logprob length mismatch')
        result = nll_result(values[len(row['promptTokenIDs']) - 1:])
        metrics = plain(output.metrics); opt_metrics = plain(optimized.metrics)
        require(math.isclose(metrics['loss:sum'], result['weightedNLLSum'], rel_tol=2e-5, abs_tol=1e-4), 'Loss/mask mismatch')
        require(all(math.isfinite(v) for v in opt_metrics.values()), 'Nonfinite optimizer metrics')
        return {**result, 'latencySeconds': time.monotonic() - start, 'inputTokens': row['trainingDatumPositions'],
                'forwardMetrics': metrics, 'optimizerMetrics': opt_metrics}


class SharedCalls:
    def __init__(self, journal, key, prices, maximum_tokens):
        self.journal, self.key, self.prices, self.maximum_tokens = journal, key, prices, maximum_tokens

    def call(self, key, kind, row, fn):
        full_key = self.key + '/' + key
        require(not any(r.get('key') == full_key for r in self.journal.records), 'Already attempted; do not replay spend')
        p = self.prices
        if kind == 'generation': maximum = (row['promptTokenCount'] * p['prefill'] + self.maximum_tokens * p['sample']) / 1e6
        elif kind == 'nll': maximum = ((row['promptTokenCount'] + row['lossBearingTokenCount']) * p['prefill'] + p['sample']) / 1e6
        elif kind == 'train': maximum = row['trainingDatumPositions'] * p['train'] / 1e6
        else:
            require(kind == 'admin', 'Unknown operation'); maximum = 0.
        spent = sum(r.get('maximumUSD', 0.) for r in self.journal.records if r['kind'] == 'operation_begin')
        require(spent + maximum + .5 <= 20., 'Shared $20 ceiling reached before dispatch')
        self.journal.append({'kind': 'operation_begin', 'key': full_key, 'operation': kind, 'modelKey': self.key,
            'exampleID': row['exampleID'], 'maximumUSD': maximum, 'at': utc()})
        start = time.monotonic(); value = fn()
        self.journal.append({'kind': 'operation_result', 'key': full_key, 'operation': kind, 'modelKey': self.key,
                            'value': value, 'wallSeconds': time.monotonic() - start, 'at': utc()})
        print(json.dumps({'completed': full_key, 'seconds': round(time.monotonic() - start, 2),
                          'reservedTokenCostUSD': round(spent + maximum, 4)}), flush=True)
        return value


def runtime_binding():
    result = original.runtime()
    for name in ('run-phase1-qwen-model-preflights.py', 'prepare-phase1-qwen-model-preflights.py',
                 'phase1_qwen35_native.py', 'phase1_training_contract.py', 'phase1_experiment.py'):
        path = ROOT / 'scripts' / name; result['filesSHA256'][str(path)] = file_hash(path)
    return result


def load_inputs(directory):
    report = json.loads((directory / 'preparation.json').read_text())
    for path, digest in {**report['sourceBindingsSHA256'], **report['codeSHA256']}.items():
        require(file_hash(path) == digest, 'Preparation input changed: ' + path)
    reference_path = next(Path(p) for p in report['sourceBindingsSHA256'] if p.endswith('/preparation.json'))
    reference = json.loads(reference_path.read_text()); rows = {}
    for key, spec in report['models'].items():
        path = directory / (key + '.jsonl'); require(file_hash(path) == spec['nativeRowsSHA256'], 'Native rows changed')
        rows[key] = {'old': {}, 'new': {}}
        for row in map(json.loads, path.open()):
            require(row['exampleID'] not in rows[key][row['pipeline']], 'Duplicate native row')
            rows[key][row['pipeline']][row['exampleID']] = row
    return report, reference, rows


def local_model(key):
    if key == 'qwen38_reasoning':
        tok = prep.AutoTokenizer.from_pretrained(prep.PREP / 'tokenizer', local_files_only=True, trust_remote_code=False)
        return tok, prep.ExactReasoningRenderer(tok, reasoning_effort='xhigh')
    tok = prep.AutoTokenizer.from_pretrained(prep.OLD_TOKENIZERS / key / 'tokenizer', local_files_only=True, trust_remote_code=False)
    return tok, prep.native_runtime_from_tokenizer(key, tok).renderer


def prepare(args):
    require(not args.output.exists(), 'Use a fresh output directory')
    report, reference, rows = load_inputs(args.prepared)
    maximum = sum(s['budget'].get('maximumTokenCostUSD', s['budget']['frozen80GenerationsMaximumUSD']) for s in report['models'].values()) + .5
    require(maximum <= 20., 'Prepared tests exceed shared approval')
    args.output.mkdir(parents=True)
    original.save(args.output / 'execution.json', {'version': VERSION, 'preparedDirectory': str(args.prepared.resolve()),
        'preparedSHA256': file_hash(args.prepared / 'preparation.json'), 'runtime': runtime_binding(),
        'projectID': PROJECT, 'hardCeilingUSD': 20., 'maximumIncludingStorageUSD': maximum,
        'modelOrder': MODEL_ORDER, 'reasoningOn': 'frozen inference only; score final answer, never reasoning text',
        'retryPolicy': 'No automatic resampling or replay; all uncertain dispatches remain charged against shared ceiling',
        'authorization': 'User approved additional $20 on 2026-09-12; not authorization for main experiment'})
    print(json.dumps({'status': 'ready_for_code_gate', 'maximumIncludingStorageUSD': maximum, 'providerCalls': 0}))


def run(args):
    execution = json.loads((args.output / 'execution.json').read_text())
    require(args.confirm_transfer and execution['projectID'] == PROJECT, 'Explicit transfer confirmation required')
    require(not (args.output / 'operations.jsonl').exists(), 'Prior operations exist; inspect instead of replay')
    require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip(), 'Commit before execution')
    require(execution['runtime'] == runtime_binding(), 'Runtime changed after preparation')
    require(file_hash(args.prepared / 'preparation.json') == execution['preparedSHA256'], 'Prepared manifest changed')
    report, reference, rows = load_inputs(args.prepared)
    prices_doc = json.loads(subprocess.check_output(['curl', '--fail', '--silent', '--show-error', '--max-time', '30',
                                                    original.PRICING_URL], text=True))
    checked = {}
    for key in MODEL_ORDER:
        model = report['models'][key]['model']; price = next(v for v in prices_doc if v['tinker_id'] == model)
        checked[key] = {k: float(price[k].lstrip('$')) for k in ('train', 'prefill', 'sample')}
        require(checked[key] == prep.PRICES[key], 'Prices changed; review before dispatch')
    original.save(args.output / 'pricing.json', {'checkedAt': utc(), 'source': original.PRICING_URL, 'models': checked})
    import tinker
    os.environ['TINKER_API_KEY'] = original.api_key(args.env_file)
    # Local preparation is offline. Authenticated SDK tokenizer preflight may
    # fetch public model metadata, never weights or personal data.
    os.environ.pop('HF_HUB_OFFLINE', None); os.environ.pop('TRANSFORMERS_OFFLINE', None)
    service = tinker.ServiceClient(project_id=PROJECT, user_metadata={'purpose': VERSION}, max_retries=0)
    proxy = original.NoAutomaticResampling(service)
    available = {v.model_name: v for v in service.get_server_capabilities().supported_models}
    state = {'version': VERSION, 'status': 'running', 'startedAt': utc(), 'sessionID': service.holder.get_session_id(),
             'executionSHA256': file_hash(args.output / 'execution.json'),
             'implementationCommit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
             'projectID': PROJECT, 'maximumUSD': 20., 'models': {}, 'mainRunStarted': False}
    journal = Journal(args.output, {'executionSHA256': state['executionSHA256']})
    def save(): original.save(args.output / 'run.json', state)
    save()
    try:
        with journal.exclusive():
            for key in MODEL_ORDER:
                spec = report['models'][key]; model = spec['model']
                require(model in available and available[model].max_context_length >= 65536, 'Model or context unavailable')
                tok, renderer = local_model(key)
                model_state = {'status': 'tokenizer_preflight', 'model': model, 'modelRevision': 'unverified: provider exposes model name only'}
                state['models'][key] = model_state; save()
                base = proxy.create_sampling_client(base_model=model); remote = base.get_tokenizer()
                require(fingerprint(remote.get_vocab()) == spec['tokenizerVocabularySHA256'] == fingerprint(tok.get_vocab()),
                        'Complete tokenizer vocabulary mismatch')
                for pipeline in rows[key].values():
                    for row in pipeline.values():
                        ids = row['promptTokenIDs'] + row.get('completionTokenIDs', [])
                        require(remote.decode(ids, clean_up_tokenization_spaces=False) == tok.decode(ids, clean_up_tokenization_spaces=False),
                                'Remote/local token decode mismatch')
                        if not row.get('generationOnly'): native_datum(row, tinker, spec['nativeStopTokenIDs'][0])
                model_state['tokenizer'] = {'completeVocabularyMatched': True, 'selectedRowsDecodedIdentically': True}; save()
                contract = copy.deepcopy(spec['trainingContract'] or {})
                contract.update(model=model, reasoning=key == 'qwen38_reasoning',
                    generation={**spec['generation'], 'seed': 17, 'samplesPerExample': 1})
                bridge = NativeBridge(proxy, tinker, tok, renderer, contract)
                calls = SharedCalls(journal, key, checked[key], spec['generation']['maximumTokens'])
                extra = reference['additionalTests']
                if key != 'qwen38_reasoning':
                    model_state['originalChecks'] = {}
                    for arm in ('old', 'new'):
                        model_state['originalChecks'][arm] = original.phases(original.JournaledBridge(bridge), calls,
                            rows[key][arm], reference['arms'][arm], arm); save()
                seeded = {s: NativeBridge(proxy, tinker, tok, renderer,
                    {**contract, 'generation': {**contract['generation'], 'seed': s}}) for s in extra['generationSeeds']}
                # Four already-planned samples, one per app, establish the output
                # envelope before buying the rest. They are not extra requests.
                first = {}; capability = {}
                for case in extra['cases']: first.setdefault(case['application'], case['exampleID'])
                gate = []
                for eid in first.values():
                    value = calls.call(f'capability/{eid}/seed-17', 'generation', rows[key]['new'][eid],
                                       lambda eid=eid: seeded[17].generate(base, rows[key]['new'][eid]))
                    capability[eid] = [{'seed': 17, **value}]; gate.append(value)
                model_state['capability'] = {'status': 'sampling', 'scores': capability}; save()
                if key == 'qwen38_reasoning':
                    require(all(v.get('reasoningClosed') for v in gate),
                            'Reasoning gate lacks final answers; retain responses and review token budget before continuing')
                for eid in extra['probeIDs']:
                    capability.setdefault(eid, [])
                    for seed in extra['generationSeeds']:
                        if any(v['seed'] == seed for v in capability[eid]): continue
                        value = calls.call(f'capability/{eid}/seed-{seed}', 'generation', rows[key]['new'][eid],
                                           lambda eid=eid, seed=seed: seeded[seed].generate(base, rows[key]['new'][eid]))
                        capability[eid].append({'seed': seed, **value}); save()
                model_state['capability']['status'] = 'complete_pending_intent_review'
                if key != 'qwen38_reasoning':
                    def progress(value):
                        journal.append({'kind': 'overfit_progress', 'modelKey': key, 'at': utc(), **value})
                        model_state['latestOverfitProgress'] = value; save()
                    model_state['overfit'] = original.overfit_phase(original.JournaledBridge(bridge), calls,
                                                                   rows[key]['new'], extra, progress)
                model_state['status'] = 'complete_pending_review'; save()
            state['status'] = 'complete_pending_review'
    except BaseException as error:
        state.update(status='stopped_requires_review', errorType=type(error).__name__)
        raise
    finally:
        state['endedAt'] = utc()
        state['reservedTokenCostUSD'] = sum(r.get('maximumUSD', 0.) for r in journal.records if r['kind'] == 'operation_begin')
        save()
    start = dt.datetime.fromisoformat(state['startedAt']).replace(minute=0, second=0, microsecond=0)
    end = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
    try:
        billing = plain(service.create_rest_client().get_billing_usage(start, end).result())
        original.save(args.output / 'billing.json', {'status': 'may_lag_hours', 'retrievedAt': utc(), 'sessionID': state['sessionID'],
            'data': {k: [r for r in v if r.get('session_id') == state['sessionID']] for k, v in billing.items() if isinstance(v, list)}})
    except Exception as error: original.save(args.output / 'billing.json', {'status': 'unavailable', 'errorType': type(error).__name__})
    print(json.dumps({'status': state['status'], 'reservedTokenCostUSD': state['reservedTokenCostUSD'], 'mainRunStarted': False}))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--prepared', type=Path, required=True); p.add_argument('--output', type=Path, required=True)
    p.add_argument('--env-file', type=Path, default=ROOT / '.env'); p.add_argument('--confirm-transfer', action='store_true')
    p.add_argument('--prepare', action='store_true'); p.add_argument('--execute', action='store_true'); a = p.parse_args()
    require(a.prepare != a.execute, 'Choose preparation or execution')
    prepare(a) if a.prepare else run(a)


if __name__ == '__main__': main()

#!/usr/bin/env python3
"""Offline model-choice preparation; no authentication or provider dispatch.

Reuse the reviewed preflight selection and exact frozen semantic inputs. Native
rows are independent artifacts, never replacements for the running 27B pack.
Reasoning-on rows are generation-only: human final answers are not CoT labels.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import resource
import sys

ROOT = Path(__file__).resolve().parents[1]
PREP = ROOT / 'coupled-data/sep02-10-training-prep-20260910'
sys.path.insert(0, str(PREP / 'runtime'))
from phase1_qwen38_execution import NativeRows, canonical, file_hash, fingerprint, require
from phase1_qwen35_native import MODEL_SPECS, native_runtime_from_tokenizer, render_native_row
from transformers import AutoTokenizer
from tinker_cookbook.renderers import get_text_content
from tinker_cookbook.renderers.qwen3 import Qwen3VLRenderer
from tinker_cookbook.renderers.qwen3_8 import Qwen3_8Renderer

VERSION = 'phase1-qwen-model-preflight-preparation-v1'
OLD_TOKENIZERS = ROOT / 'coupled-data/phase1-qwen35-450-preflight-v2-generation-only-db17c81'
PRICES = {
    'qwen38_reasoning': {'prefill': 1.86, 'sample': 5.595, 'train': 4.103},
    'qwen36_hybrid': {'prefill': .54, 'sample': 1.335, 'train': 1.177},
    'qwen35_base': {'prefill': .54, 'sample': 1.335, 'train': 1.177},
}
SYNTHETIC_TARGETS = [' plain ', 'First line\n\nSecond line\n',
                     'review <|paste|> tomorrow', '🧠 café — 中文']


def text_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def write_json(path, value):
    with path.open('x') as f:
        f.write(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + '\n')


def source_text(row, tokenizer):
    """Invert only the proven single-user template; hashes bind every byte."""
    rendered = tokenizer.decode(row['promptTokenIDs'], clean_up_tokenization_spaces=False)
    prefix = '<|im_start|>user\n'
    suffix = '<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n'
    require(rendered.startswith(prefix) and rendered.endswith(suffix), 'Unknown source envelope')
    semantic = rendered[len(prefix):-len(suffix)]
    require(text_hash(semantic) == row['modelInputSHA256'], 'Semantic input hash mismatch')
    target = tokenizer.decode(row['completionTokenIDs'][:-1], clean_up_tokenization_spaces=False)
    require(row['completionTokenIDs'][-1] == 248046 and text_hash(target) == row['targetSHA256'],
            'Source target or terminator changed')
    require(fingerprint(row['promptTokenIDs'] + row['completionTokenIDs']) == row['fullSequenceTokenSHA256'],
            'Source tokens changed')
    return semantic, target


def hf_prompt(tokenizer, messages, **kwargs):
    value = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True, **kwargs)
    return value['input_ids'] if hasattr(value, 'keys') else value


class ExactReasoningRenderer(Qwen3_8Renderer):
    def render_message(self, message, ctx):
        return Qwen3VLRenderer.render_message(self, message, ctx)


def parse_reasoning_output(renderer, tokens, stop_reason):
    """Never grade unfinished thinking as the user's proposed completion.

    Cookbook parse_response returns raw text when EOS is missing. Retain the
    actual tokens/stop reason; a parser-only terminator can parse a truncated
    final answer, but is not evidence that generation terminated normally.
    """
    closing = renderer.tokenizer.encode('</think>', add_special_tokens=False)
    require(len(closing) == 1, 'Reasoning-close token is not atomic')
    raw = renderer.tokenizer.decode(tokens, clean_up_tokenization_spaces=False)
    if closing[0] not in tokens:
        return {'prediction': '', 'reasoningClosed': False, 'rawDecodedPrediction': raw,
                'stopReason': stop_reason, 'parserOnlyTerminatorAdded': False}
    stops = renderer.get_stop_sequences()
    added = not tokens or tokens[-1] != stops[0]
    parsed, termination = renderer.parse_response(tokens + stops if added else tokens)
    return {'prediction': get_text_content(parsed), 'reasoningClosed': True,
            'rawDecodedPrediction': raw, 'stopReason': stop_reason,
            'parseTermination': str(termination.value), 'parserOnlyTerminatorAdded': added}


def reasoning_row(example, semantic, renderer):
    messages = [{'role': 'user', 'content': semantic}]
    ids = renderer.build_generation_prompt(messages).to_ints()
    require(ids == hf_prompt(renderer.tokenizer, messages, enable_thinking=True, reasoning_effort='xhigh'),
            'Reasoning prompt differs from pinned HF template')
    require(renderer.tokenizer.decode(ids[-3:]).endswith('<think>\n'), 'Reasoning not enabled')
    return {'exampleID': example['exampleID'], 'promptTokenIDs': ids,
            'promptTokenCount': len(ids), 'modelInputSHA256': text_hash(semantic),
            'targetText': example['targetText'], 'targetSHA256': text_hash(example['targetText']),
            'generationOnly': True, 'trainingDatum': None,
            'reason': 'No human reasoning labels; do not train an empty or invented think block',
            'generationStopTokenIDs': renderer.get_stop_sequences()}


def supervised_row(example, semantic, runtime):
    r = render_native_row(example=example, semantic_input=semantic, runtime=runtime)
    n = r['modelInputTokenCount']
    if runtime.renderer_strategy != 'raw_completion_eos':
        require(r['inputIDs'][:n] == hf_prompt(runtime.tokenizer, [{'role': 'user', 'content': semantic}],
                                               enable_thinking=False), 'HF non-thinking template mismatch')
    return {'exampleID': example['exampleID'], 'promptTokenIDs': r['inputIDs'][:n],
            'completionTokenIDs': r['inputIDs'][n:], 'promptTokenCount': n,
            'lossBearingTokenCount': r['targetTokenCount'], 'trainingDatumPositions': len(r['inputIDs']) - 1,
            'modelInputSHA256': r['semanticModelInputSHA256'], 'targetSHA256': r['targetTextSHA256'],
            'generationStopTokenIDs': r['generationStopTokenIDs'],
            'fullSequenceTokenSHA256': fingerprint(r['inputIDs']),
            'lossContract': r['lossContract'], 'causalSDKShiftVerified': True}


def budget(rows, selected, additional, model_key):
    p = PRICES[model_key]
    cap = 8192 if model_key == 'qwen38_reasoning' else 512
    def cost(kind, pipeline, eid):
        r = rows[pipeline, eid]
        if kind == 'gen': return (r['promptTokenCount'] * p['prefill'] + cap * p['sample']) / 1e6
        if kind == 'train': return r['trainingDatumPositions'] * p['train'] / 1e6
        return ((r['promptTokenCount'] + r['lossBearingTokenCount']) * p['prefill'] + p['sample']) / 1e6
    multi = sum(4 * cost('gen', 'new', e) for e in additional['probeIDs'])
    if model_key == 'qwen38_reasoning':
        return {'frozen80GenerationsMaximumUSD': multi, 'trainingCost': None,
                'trainingDecisionRequired': True, 'maximumGenerationTokens': cap}
    original = 0.
    for pipeline, spec in selected.items():
        original += sum(2 * (cost('gen', pipeline, e) + cost('nll', pipeline, e)) for e in spec['probeIDs'])
        original += 4 * cost('nll', pipeline, spec['trainIDs'][0])
        original += sum(cost('train', pipeline, e) for e in spec['trainIDs'])
        original += cost('train', pipeline, spec['trainIDs'][2])
    overfit = sum(10 * cost('train', 'new', e) + 3 * cost('nll', 'new', e) + 2 * cost('gen', 'new', e)
                  for e in additional['overfitIDs'])
    return {'originalOldNewTestsMaximumUSD': original, 'frozen80GenerationsMaximumUSD': multi,
            'tenExampleOverfitMaximumUSD': overfit, 'maximumTokenCostUSD': original + multi + overfit,
            'storageReserveUSD': .25, 'maximumGenerationTokens': cap}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference-preparation', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists(), 'Use a fresh output directory')
    reference = json.loads(args.reference_preparation.read_text())
    plan = json.loads(Path(reference['executionPlan']).read_text())
    require(fingerprint(plan) == reference['planSHA256'], 'Plan differs from selected preflight')
    source_tok = AutoTokenizer.from_pretrained(PREP / 'tokenizer', local_files_only=True, trust_remote_code=False)
    selected = reference['arms']; additional = reference['additionalTests']
    sources = {}; bindings = {str(args.reference_preparation.resolve()): file_hash(args.reference_preparation)}
    for pipeline, arm in plan['arms'].items():
        pack = Path(arm['packDirectory'])
        require(file_hash(pack / 'native-rows.jsonl') == arm['nativeRowsSHA256'], 'Source pack changed')
        rows = NativeRows(pack / 'native-rows.jsonl')
        ids = set(selected[pipeline]['probeIDs'])
        if pipeline == 'new': ids.update(additional['probeIDs'])
        cohort = {e['exampleID']: e for e in map(json.loads, (pack / 'cohort.jsonl').open()) if e['exampleID'] in ids}
        for eid in sorted(ids):
            r = rows[eid]; semantic, target = source_text(r, source_tok)
            require(cohort[eid]['targetText'] == target, 'Cohort target changed')
            cases = selected[pipeline]['cases'] + (additional['cases'] if pipeline == 'new' else [])
            require(all(c['fullSequenceTokenSHA256'] == r['fullSequenceTokenSHA256'] for c in cases if c['exampleID'] == eid),
                    'Selected occurrence changed')
            sources[pipeline, eid] = (semantic, cohort[eid], r['fullSequenceTokenSHA256'])
        for name in ('cohort.jsonl', 'native-rows.jsonl'):
            bindings[str((pack / name).resolve())] = file_hash(pack / name)
    args.output.mkdir(parents=True)
    report = {'version': VERSION, 'status': 'offline_prepared_NOT_AUTHORIZED',
              'providerCalls': 0, 'sameSemanticInputAndTargetAcrossModels': True,
              'sourceBindingsSHA256': bindings, 'referencePlanSHA256': reference['planSHA256'],
              'selectedOldNewOccurrences': len(sources), 'models': {},
              'codeSHA256': {str(Path(__file__).resolve()): file_hash(Path(__file__)),
                             str(ROOT / 'scripts/phase1_qwen35_native.py'): file_hash(ROOT / 'scripts/phase1_qwen35_native.py')},
              'priceSource': 'https://tinker-docs.thinkingmachines.ai/tinker/models.json',
              'pricesCheckedDate': '2026-09-12', 'pricePolicy': 'uncached prefill and maximum output; recheck before execution',
              'referenceTrainingRecipe': plan['contract'],
              'warning': 'Preparation only; the live 27B bridge hardcodes its stop token and must NOT execute Base rows',
              'reasoningOnTrainingDecision': 'Deferred; generation-only artifact does not imply CoT supervision',
              'pendingLiveGates': ['additional spending approval', 'model-specific executor and budget tests',
                                   'served-model tokenizer vocabulary check', 'native stop and answer parsing on real responses']}
    for key in ('qwen36_hybrid', 'qwen35_base', 'qwen38_reasoning'):
        if key == 'qwen38_reasoning':
            tok = source_tok; renderer = ExactReasoningRenderer(tok, reasoning_effort='xhigh')
            runtime = None; model = 'Qwen/Qwen3.8-27B'; tokenizer_dir = PREP / 'tokenizer'
            revision = '1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0'
        else:
            tokenizer_dir = OLD_TOKENIZERS / key / 'tokenizer'
            metadata = json.loads((OLD_TOKENIZERS / key / 'native-pack.json').read_text())
            for name, digest in metadata['tokenizerFileDigestsSHA256'].items():
                require(file_hash(tokenizer_dir / name) == digest, 'Pinned legacy tokenizer changed')
            revision = MODEL_SPECS[key]['localTokenizerRevision']
            require(revision == metadata['localTokenizerRevision'], 'Tokenizer revision differs')
            tok = AutoTokenizer.from_pretrained(tokenizer_dir, local_files_only=True, trust_remote_code=False)
            runtime = native_runtime_from_tokenizer(key, tok); renderer = runtime.renderer; model = MODEL_SPECS[key]['model']
        before_vocab = fingerprint(tok.get_vocab())
        for index, target in enumerate(SYNTHETIC_TARGETS):
            e = {'exampleID': f'synthetic-{index}', 'experimentBlockID': 'synthetic', 'targetEventID': 'synthetic',
                 'targetText': target, 'target': {'segments': [{'type': 'authored_text', 'content': target}]}}
            if runtime:
                supervised_row(e, 'Predict the next human write.', runtime)
            else:
                reasoning_row(e, 'Predict the next human write.', renderer)
                synthetic = tok.encode('Consider the request.\n</think>\n\n' + target, add_special_tokens=False) + renderer.get_stop_sequences()
                parsed, stop = renderer.parse_response(synthetic)
                require(get_text_content(parsed) == target and str(stop.value) == 'stop_sequence', 'Reasoning answer extraction changed')
                require(parse_reasoning_output(renderer, synthetic, 'stop')['prediction'] == target, 'Answer parser disagrees')
        counts = []; compact_rows = {}
        with (args.output / f'{key}.jsonl').open('x') as output:
            for (pipeline, eid), (semantic, example, source_hash) in sorted(sources.items()):
                r = supervised_row(example, semantic, runtime) if runtime else reasoning_row(example, semantic, renderer)
                require(r['modelInputSHA256'] == text_hash(semantic) and r['targetSHA256'] == text_hash(example['targetText']),
                        'Cross-model semantic mismatch')
                cap = 512 if runtime else 8192
                require(r['promptTokenCount'] + cap <= 65536, 'Sampling would exceed served model context')
                r.update({'pipeline': pipeline, 'model': model, 'sourceFullSequenceTokenSHA256': source_hash})
                output.write(canonical(r) + '\n')
                small = {k: v for k, v in r.items() if not k.endswith('TokenIDs') and k != 'targetText'}
                compact_rows[pipeline, eid] = small; counts.append(r['promptTokenCount'])
        require(fingerprint(tok.get_vocab()) == before_vocab and '<|paste|>' not in tok.get_added_vocab(), 'Vocabulary mutated')
        training = None
        if runtime:
            training = {k: copy.deepcopy(plan['contract'][k]) for k in (
                'rank', 'seed', 'trainAttention', 'trainMLP', 'trainUnembedding',
                'batchExamplesPerForwardBackward', 'optimizer')}
            training.update({'model': model, 'reasoning': False if key == 'qwen36_hybrid' else None,
                'loss': {**plan['contract']['loss'], 'nativeTerminatorTokenID': renderer.get_stop_sequences()[0]},
                'startingPoint': 'fresh base weights; separate adapters for old probe, new probe, and overfit',
                'overfitMaximumEpochs': additional['overfitEpochsMaximum'],
                'overfitEvaluationEpochs': additional['overfitEvaluationEpochs'],
                'overfitOrder': 'sha256 of canonical {seed:17, epoch, exampleID}, ascending',
                'checkpointTTLSeconds': reference['checkpointTTLSeconds']})
        report['models'][key] = {'model': model, 'localTokenizerRevision': revision,
            'serverModelRevision': 'unverified; local revision is not a server weight revision',
            'tokenizerVocabularySHA256': before_vocab, 'vocabularySize': len(tok.get_vocab()),
            'tokenizerFilesSHA256': {p.name: file_hash(p) for p in sorted(tokenizer_dir.iterdir()) if p.is_file()},
            'nativeStopTokenIDs': renderer.get_stop_sequences(), 'pasteMarkerIDs': tok.encode('<|paste|>', add_special_tokens=False),
            'syntheticContractCasesPassed': len(SYNTHETIC_TARGETS), 'occurrencesVerified': len(counts),
            'promptTokensMin': min(counts), 'promptTokensMax': max(counts),
            'generationReasoning': 'xhigh' if not runtime else 'off' if key == 'qwen36_hybrid' else 'not_applicable',
            'generation': {'temperature': .6, 'seeds': additional['generationSeeds'],
                           'maximumTokens': 512 if runtime else 8192, 'stopTokenIDs': renderer.get_stop_sequences()},
            'trainingContract': training,
            'trainingRowsPrepared': bool(runtime), 'budget': budget(compact_rows, selected, additional, key),
            'nativeRowsSHA256': file_hash(args.output / f'{key}.jsonl')}
    report['peakRSSMiB'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024**2
    write_json(args.output / 'preparation.json', report)
    print(json.dumps({k: report[k] for k in ('status', 'providerCalls', 'selectedOldNewOccurrences', 'models', 'peakRSSMiB')}, indent=2))


if __name__ == '__main__':
    main()

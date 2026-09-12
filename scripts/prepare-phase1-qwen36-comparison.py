#!/usr/bin/env python3
"""Offline Qwen3.6 repack of the independent, reviewed old/new corpora.

No dataset reconstruction, context refill, authentication, or provider client.
Keep the previous packs/results immutable. Audit native rows one at a time.
"""
from __future__ import annotations

import argparse
import copy
import importlib.metadata
import importlib.util
import json
import math
from pathlib import Path
import shutil
import socket
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('model_prep', ROOT / 'scripts/prepare-phase1-qwen-model-preflights.py')
prep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prep)
from phase1_qwen38_execution import (NativeRows, datum_from_row, file_hash,
                                    fingerprint, require, verify_execution_binding)

VERSION = 'phase1-qwen36-pipeline-era-preparation-v1'
MODEL = 'Qwen/Qwen3.6-35B-A3B'
PRICES = {'prefill': .54, 'sample': 1.335, 'train': 1.177}
PRICE_URL = 'https://tinker-docs.thinkingmachines.ai/tinker/models/'
TOKENIZER = prep.OLD_TOKENIZERS / 'qwen36_hybrid/tokenizer'
PACKAGES = ('tinker', 'tinker-cookbook', 'transformers', 'tokenizers', 'torch')


def offline():
    def forbidden(*args, **kwargs):
        raise RuntimeError('Network/provider construction forbidden in local preparation')
    import tinker
    tinker.ServiceClient = forbidden
    socket.create_connection = forbidden
    socket.socket.connect = forbidden
    socket.socket.connect_ex = forbidden


def load(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    prep.write_json(Path(path), value)


def runtime_bindings():
    import tinker
    import tinker_cookbook
    paths = [Path(__file__), ROOT / 'scripts/check-phase1-qwen36-comparison.py',
             ROOT / 'scripts/prepare-phase1-qwen-model-preflights.py',
             ROOT / 'scripts/phase1_qwen35_native.py', ROOT / 'scripts/phase1_training_contract.py',
             ROOT / 'scripts/phase1_qwen38_execution.py', ROOT / 'scripts/check-phase1-pipeline-era-execution.py']
    for package in (tinker, tinker_cookbook):
        paths.extend(sorted(Path(package.__file__).parent.rglob('*.py')))
    for module in tuple(sys.modules.values()):
        path = getattr(module, '__file__', None)
        if path:
            path = Path(path).resolve()
            if path.suffix == '.py' and (path.is_relative_to(ROOT/'scripts') or path.is_relative_to(prep.PREP/'runtime')):
                paths.append(path)
    return {str(p.resolve()): file_hash(p) for p in paths}


def native_runtime(pilot):
    meta = pilot['tokenizer']
    require(meta['localTokenizerRevision'] == prep.MODEL_SPECS['qwen36_hybrid']['localTokenizerRevision'],
            'Pinned native tokenizer revision changed')
    for name, digest in meta['tokenizerFilesSHA256'].items():
        require(file_hash(TOKENIZER / name) == digest, 'Tokenizer file changed')
    tok = prep.AutoTokenizer.from_pretrained(TOKENIZER, local_files_only=True, trust_remote_code=False)
    require(fingerprint(tok.get_vocab()) == meta['tokenizerVocabularySHA256'], 'Full vocabulary changed')
    require(tok.encode('<|paste|>', add_special_tokens=False) == meta['pasteMarkerIDs'], 'Paste encoding changed')
    return prep.native_runtime_from_tokenizer('qwen36_hybrid', tok)


def validate_row(row, example, semantic, native):
    """Independently verify generation prefix, supervision, SDK shift and bytes."""
    import tinker
    from tinker_cookbook.renderers import TrainOnWhat, get_text_content
    tok, renderer = native.tokenizer, native.renderer
    target = example['targetText']
    messages = [{'role': 'user', 'content': semantic}]
    prompt = renderer.build_generation_prompt(messages).to_ints()
    require(prompt == row['promptTokenIDs'] == prep.hf_prompt(tok, messages, enable_thinking=False),
            'Generation prompt is not the exact native non-thinking prefix')
    require(row['completionTokenIDs'] == tok.encode(target, add_special_tokens=False) + [248046],
            'Completion bytes/EOS changed')
    full, weights = renderer.build_supervised_example(messages + [{'role': 'assistant', 'content': target}],
        train_on_what=TrainOnWhat.LAST_ASSISTANT_MESSAGE)
    ids = prompt + row['completionTokenIDs']
    require(full.to_ints() == ids and weights.tolist() == [0.] * len(prompt) + [1.] * len(row['completionTokenIDs']),
            'Renderer supervision differs from generation or includes prompt loss')
    datum = datum_from_row(row, tinker)
    require(datum.loss_fn_inputs['weights'].to_numpy().tolist() == weights.tolist()[1:], 'Wrong shifted mask')
    require(len(prompt) + 512 <= 65536, 'Generation exceeds native model context')
    require(prep.text_hash(semantic) == row['modelInputSHA256'] and prep.text_hash(target) == row['targetSHA256'],
            'Semantic input/target hash changed')
    parsed, _ = renderer.parse_response(row['completionTokenIDs'])
    require(get_text_content(parsed) == target, 'Native response parser changes target')


def summarize(cohort, blocks, compact):
    ids = [e['exampleID'] for e in cohort]
    require(ids == [eid for b in blocks for eid in b['exampleIDs']] and len(ids) == len(set(ids)),
            'Cohort and chronological blocks differ')
    seen = []
    by_id = {e['exampleID']: e for e in cohort}
    for ordinal, block in enumerate(blocks, 1):
        members = [by_id[eid] for eid in block['exampleIDs']]
        require(block['blockOrdinal'] == ordinal and block['trainedExamplesBeforeScoring'] == len(seen),
                'Schedule exposure changed')
        require(block['trainThisBlockAfterScoring'] == (ordinal < len(blocks)), 'Final block must not train')
        require(0 < len(members) <= 50 and (ordinal == len(blocks) or len(members) == 50), 'Invalid block size')
        require(block['firstBeganAt'] == min(e['targetBeganAt'] for e in members), 'Block start changed')
        require(block['lastAvailableAt'] == max(e['targetAvailableAt'] for e in members), 'Block availability changed')
        if ordinal > 1:
            require(blocks[ordinal-2]['lastAvailableAt'] < block['firstBeganAt'], 'Future evidence in training prefix')
        seen.extend(block['exampleIDs'])
    train = [eid for b in blocks if b['trainThisBlockAfterScoring'] for eid in b['exampleIDs']]
    future = ids[len(blocks[0]['exampleIDs']):]
    total = lambda field, group: sum(compact[e][field] for e in group)
    tokens = {'trainingPositions': total('trainingDatumPositions', train),
              'trainingLossBearingPresentations': total('lossBearingTokenCount', train),
              'uniqueLossBearing': total('lossBearingTokenCount', ids),
              'generationPrefillOneModel': total('promptTokenCount', future),
              'NLLSequenceTokensOneModel': total('promptTokenCount', future) + total('lossBearingTokenCount', future),
              'warmupFrozenNLLSequenceTokens': total('promptTokenCount', ids[:50]) + total('lossBearingTokenCount', ids[:50]),
              'minimumPrompt': min(r['promptTokenCount'] for r in compact.values()),
              'maximumPrompt': max(r['promptTokenCount'] for r in compact.values()),
              'medianPrompt': statistics.median(r['promptTokenCount'] for r in compact.values()),
              'maxSequence': max(r['trainingDatumPositions']+1 for r in compact.values())}
    costs = {'train': tokens['trainingPositions'] * PRICES['train'] / 1e6,
             'generationPrefill': 2 * tokens['generationPrefillOneModel'] * PRICES['prefill'] / 1e6,
             'generationMaximumOutput': 2 * len(future) * 512 * PRICES['sample'] / 1e6,
             'nll': ((2*tokens['NLLSequenceTokensOneModel'] + tokens['warmupFrozenNLLSequenceTokens']) * PRICES['prefill']
                     + (2*len(future)+len(ids[:50]))*PRICES['sample']) / 1e6}
    retry = max(sum(compact[e]['trainingDatumPositions']*PRICES['train']/1e6 for e in b['exampleIDs'])
                for b in blocks if b['trainThisBlockAfterScoring'])
    return {'counts': {'examples': len(ids), 'blocks': len(blocks), 'trainingExamples': len(train),
                       'scoredExamples': len(future), 'generations': 2*len(future), 'nllCalls': 2*len(future)+50},
            'tokens': tokens, 'maximumTokenCostUSD': costs, 'oneLargestBlockRetryUSD': retry,
            'scoredTargetsExceedingGenerationCeiling': [eid for eid in future if compact[eid]['lossBearingTokenCount'] > 512]}


def prepare(source_plan_path, pilot_path, output):
    require(not output.exists(), 'Use a new immutable output directory')
    source_plan, pilot = load(source_plan_path), load(pilot_path)
    verify_execution_binding(source_plan)
    require(source_plan['contract']['model'] == 'Qwen/Qwen3.8-27B', 'Unexpected source native format')
    require(pilot['model'] == MODEL and pilot['referencePlanFingerprint'] == fingerprint(source_plan),
            'Pilot did not use this exact corpus preparation')
    native = native_runtime(pilot)
    source_tok = prep.AutoTokenizer.from_pretrained(prep.PREP / 'tokenizer', local_files_only=True, trust_remote_code=False)
    runtime = runtime_bindings()
    contract = copy.deepcopy(source_plan['contract'])
    contract.update(model=MODEL, version='phase1-qwen36-pipeline-era-contract-v1')
    pilot_contract = next(a['contract'] for a in pilot['arms'] if a['contract']['optimizer']['learningRate'] == 2e-4)
    for key in ('model', 'reasoning', 'optimizer', 'rank', 'seed', 'trainMLP', 'trainAttention', 'trainUnembedding',
                'batchExamplesPerForwardBackward', 'epochsPerNewBlockUpdate', 'checkpointTTLSeconds', 'loss', 'generation'):
        require(contract[key] == pilot_contract[key], 'Validated pilot recipe changed: '+key)
    output.mkdir(parents=True)
    arms, bindings = {}, dict(source_plan['sourceFilesSHA256'])
    for path in (source_plan_path, pilot_path):
        bindings[str(path.resolve())] = file_hash(path)
    for name, digest in pilot['tokenizer']['tokenizerFilesSHA256'].items():
        bindings[str((TOKENIZER/name).resolve())] = digest
    for arm in ('old', 'new'):
        original = Path(source_plan['arms'][arm]['packDirectory'])
        require(file_hash(original/'native-rows.jsonl') == source_plan['arms'][arm]['nativeRowsSHA256'], 'Source rows changed')
        require(file_hash(original/'blocks.json') == source_plan['arms'][arm]['blocksSHA256'], 'Source schedule changed')
        rows = NativeRows(original/'native-rows.jsonl')
        cohort = [json.loads(l) for l in (original/'cohort.jsonl').open()]
        require(len(rows.offsets) == len(cohort), 'Source row count mismatch')
        compact = {}; dest = output/arm; dest.mkdir()
        with (dest/'native-rows.jsonl').open('x') as f:
            for i, e in enumerate(cohort, 1):
                old = rows[e['exampleID']]
                semantic, target = prep.source_text(old, source_tok)
                require(target == e['targetText'], 'Source target changed')
                row = prep.supervised_row(e, semantic, native)
                validate_row(row, e, semantic, native)
                row.update(arm=arm, model=MODEL, sourceFullSequenceTokenSHA256=old['fullSequenceTokenSHA256'])
                f.write(json.dumps(row, ensure_ascii=False, separators=(',', ':'))+'\n')
                compact[e['exampleID']] = {k:v for k,v in row.items() if not k.endswith('TokenIDs')}
                if i % 150 == 0: print(f'{arm}: native re-render and audit {i}/{len(cohort)}', flush=True)
        for name in ('cohort.jsonl', 'blocks.json'):
            shutil.copyfile(original/name, dest/name)
        blocks = load(dest/'blocks.json')
        report = summarize(cohort, blocks, compact)
        report.update(packDirectory=arm, sourcePackDirectory=str(original),
                      filesSHA256={n:file_hash(dest/n) for n in ('native-rows.jsonl','cohort.jsonl','blocks.json')},
                      taskInstruction=source_plan['arms'][arm]['taskInstruction'])
        arms[arm] = report
        for name in ('native-rows.jsonl', 'cohort.jsonl', 'blocks.json'):
            bindings[str((original/name).resolve())] = file_hash(original/name)
    require(arms['old']['taskInstruction'] == arms['new']['taskInstruction'], 'Instructions differ')
    totals = {k:sum(a['maximumTokenCostUSD'][k] for a in arms.values()) for k in ('train','generationPrefill','generationMaximumOutput','nll')}
    scheduled = sum(totals.values())
    # Reserve one entire interrupted block per pipeline, and one maximum native
    # context generation+NLL retry. Actual dispatch still needs a bound launcher.
    retry = sum(a['oneLargestBlockRetryUSD'] for a in arms.values()) + (2*65536*.54 + 513*1.335)/1e6
    pairs = sum(a['counts']['blocks']-1 for a in arms.values()) + 2
    storage_bound = pairs * 561463296 * 32 / 1e9 * .10 * 7/30
    storage_reserve = math.ceil(storage_bound)
    budget = {'scheduledMaximumTokenCostUSD': scheduled, 'breakdownUSD': totals,
              'optionalRecoveryReserveUSD': retry, 'checkpointStorageReserveUSD': storage_reserve,
              'includingRecoveryAndStorageUSD': scheduled + retry + storage_reserve,
              'suggestedCeilingUSD': math.ceil((scheduled+retry+storage_reserve)/10)*10,
              'meaning': 'Uncached prefill; all answers hit 512 tokens; exact shifted training positions; NLL includes the SDK one-token sample. Not invoice cost or launch permission.'}
    require(all(file_hash(path) == digest for path,digest in runtime.items()), 'Runtime changed during preparation')
    plan = {'version': VERSION, 'status': 'offline_prepared_NOT_AUTHORIZED', 'contract': contract,
            'sourcePlanPath': str(source_plan_path.resolve()), 'sourcePlanFingerprint': fingerprint(source_plan),
            'pilotPreparationPath': str(pilot_path.resolve()), 'arms': arms,
            'sourceFilesSHA256': bindings, 'runtimeFilesSHA256': runtime,
            'packageVersions': {p:importlib.metadata.version(p) for p in PACKAGES},
            'tokenizer': pilot['tokenizer'], 'serverModelRevision': 'unverified: provider exposes model name only',
            'contextPolicy': 'Exactly the prior frozen semantic inputs, targets and per-pipeline 32K reference-budget selection. Native re-render only; no refill or new selection.',
            'independentBlockSchedules': True, 'sameTargetsRequired': False, 'trainOnFinalBlock': False,
            'startingPoint': 'Two fresh rank-32 adapters from the same Qwen3.6 model; no pilot checkpoints.',
            'developmentDisclosure': 'The first 100 new-pipeline examples informed model/LR selection; report later blocks separately, not the entire trace as untouched validation.',
            'evaluation': source_plan['evaluation'], 'pricesPerMillionTokens': PRICES,
            'priceReferenceDate': '2026-09-12', 'priceSource': PRICE_URL, 'budget': budget,
            'checkpointStorage': {'ttlSeconds':604800, 'maximumPairsIncludingRecovery':pairs,
                'trainableParameterEstimate':561463296, 'bytesPerParameterPerPairBound':32,
                'priceUSDPerGBMonth':.10, 'sevenDayBoundUSD':storage_bound, 'reserveUSD':storage_reserve},
            'providerCalls':0, 'trainingLaunched':False,
            'remainingGates':['Complete native audit and deterministic repeat',
                'Qwen3.6 full-schedule offline execution/recovery rehearsal',
                'Clean immutable launcher binding; reserve storage before token dispatch; bound retries',
                'Recheck remote model/tokenizer/prices before dispatch, under explicit authorization',
                'User review of this plan and explicit full-run cost/GO approval']}
    save(output/'preparation.json', plan)
    print(json.dumps({'planFingerprint':fingerprint(plan),'budget':budget,'providerCalls':0}, indent=2), flush=True)


def audit(directory):
    plan = load(directory/'preparation.json')
    require(plan['version'] == VERSION and plan['contract']['model'] == MODEL, 'Wrong prepared plan')
    for path,digest in {**plan['sourceFilesSHA256'], **plan['runtimeFilesSHA256']}.items():
        require(file_hash(path) == digest, 'Bound input/runtime changed: '+path)
    require(plan['packageVersions'] == {p:importlib.metadata.version(p) for p in PACKAGES}, 'Package versions changed')
    source_plan = load(plan['sourcePlanPath'])
    expected_contract = copy.deepcopy(source_plan['contract'])
    expected_contract.update(model=MODEL, version='phase1-qwen36-pipeline-era-contract-v1')
    require(plan['contract'] == expected_contract, 'Native switch changed the frozen training recipe')
    require(plan['pricesPerMillionTokens'] == PRICES, 'Planning rates changed')
    require(plan['sourcePlanFingerprint'] == fingerprint(source_plan), 'Source plan changed')
    native = native_runtime(load(plan['pilotPreparationPath']))
    source_tok = prep.AutoTokenizer.from_pretrained(prep.PREP/'tokenizer', local_files_only=True, trust_remote_code=False)
    for arm, report in plan['arms'].items():
        dest = directory/report['packDirectory']; original = Path(report['sourcePackDirectory'])
        for name,digest in report['filesSHA256'].items():
            require(file_hash(dest/name) == digest, 'Native artifact changed')
        for name in ('cohort.jsonl','blocks.json'):
            require(file_hash(dest/name) == file_hash(original/name), 'Cohort/schedule changed')
        rows, old = NativeRows(dest/'native-rows.jsonl'), NativeRows(original/'native-rows.jsonl')
        cohort = [json.loads(l) for l in (dest/'cohort.jsonl').open()]; compact = {}
        require(set(rows.offsets) == {e['exampleID'] for e in cohort}, 'Extra/missing native rows')
        for e in cohort:
            row, before = rows[e['exampleID']], old[e['exampleID']]
            semantic,target = prep.source_text(before,source_tok)
            require(e['targetText'] == target and row['sourceFullSequenceTokenSHA256'] == before['fullSequenceTokenSHA256'], 'Source lineage changed')
            validate_row(row,e,semantic,native)
            compact[e['exampleID']] = {k:v for k,v in row.items() if not k.endswith('TokenIDs')}
        computed = summarize(cohort,load(dest/'blocks.json'),compact)
        require(all(report[k] == v for k,v in computed.items()), 'Computed counts/tokens/cost changed')
        print(f'{arm}: independently audited {len(cohort)} native rows', flush=True)
    budget = plan['budget']
    totals = {k:sum(a['maximumTokenCostUSD'][k] for a in plan['arms'].values())
              for k in ('train','generationPrefill','generationMaximumOutput','nll')}
    require(budget['breakdownUSD'] == totals and budget['scheduledMaximumTokenCostUSD'] == sum(totals.values()),
            'Budget does not match exact native submitted positions')
    require(budget['includingRecoveryAndStorageUSD'] == sum(totals.values()) +
            budget['optionalRecoveryReserveUSD'] + budget['checkpointStorageReserveUSD'], 'Reserve omitted from total')
    require(budget['suggestedCeilingUSD'] >= budget['includingRecoveryAndStorageUSD'], 'Insufficient proposed ceiling')
    result = {'status':'passed', 'planFingerprint':fingerprint(plan), 'providerCalls':0, 'networkBlocked':True,
              'everyNativeRowChecked':sum(a['counts']['examples'] for a in plan['arms'].values()),
              'sourceSemanticInputTargetAndScheduleUnchanged':True, 'nativeHFPromptMatches':True,
              'exactContentPlusOneNativeEOS':True, 'everySDKShiftAndMaskVerified':True,
              'targetsNotTruncated':True, 'independentCausalSchedulesChecked':True}
    save(directory/'audit.json',result)
    print(json.dumps(result,indent=2))


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source-plan', type=Path)
    ap.add_argument('--pilot', type=Path)
    ap.add_argument('--output', type=Path)
    ap.add_argument('--audit', type=Path)
    args = ap.parse_args(); offline()
    if args.audit: audit(args.audit.resolve())
    else:
        require(args.source_plan and args.pilot and args.output, 'Source plan, pilot and output are required')
        prepare(args.source_plan.resolve(), args.pilot.resolve(), args.output.resolve())

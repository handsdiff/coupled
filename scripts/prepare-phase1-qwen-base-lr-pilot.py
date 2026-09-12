#!/usr/bin/env python3
"""Re-render the exact Qwen3.6 pilot cohort for Base; no provider calls.

One frozen baseline plus four fresh learning-rate arms. Do not change semantic
contexts, targets, sampling settings, or optimizer recipe while changing model.
"""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import shutil

spec = importlib.util.spec_from_file_location('lr', Path(__file__).with_name('run-phase1-qwen-lr-pilot.py'))
r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)
m, p = r.m, r.p
VERSION = 'phase1-qwen35-base-lr-preparation-v1'
RATES = (5e-5, 1e-4, 2e-4, 5e-4)


def budget(rows, train, test, prices):
    t = sum(m.original.charge('train', rows[e], prices) for e in train)
    g = sum(m.original.charge('generation', rows[e], prices) for e in test)
    n = sum(m.original.charge('nll', rows[e], prices) for e in test)
    retry = t + sum(max(m.original.charge(k, rows[e], prices) for e in test) for k in ('generation', 'nll'))
    total = len(RATES) * t + (len(RATES) + 1) * (g + n)
    return {'perRateTrainingUSD': t, 'allFourTrainingUSD': 4*t,
            'oneArmGenerationMaximumUSD': g, 'oneArmTargetNLLUSD': n,
            'scheduledTokenMaximumUSD': total, 'optionalRecoveryMaximumUSD': retry,
            'checkpointStorageReserveUSD': 4., 'proposedAuthorizationUSD': total + retry + 4.,
            'pricingMeaning': 'Uncached tokens, 512 output ceiling, exact shifted native rows; not invoice cost.'}


def contracts(reference, model):
    result = []
    template = reference['arms'][0]
    for rate in RATES:
        arm = copy.deepcopy(template)
        arm['name'] = f'lr-{rate:g}'
        c = arm['contract']; c['model'] = model; c['reasoning'] = None
        c['generation']['stopTokenIDs'] = [248044]
        c['loss']['nativeTerminatorTokenID'] = 248044
        c['optimizer']['learningRate'] = rate
        result.append(arm)
    return result


def prepare(args):
    reference, _ = r.load_prepared(args.reference)
    m.require(reference['model'] == 'Qwen/Qwen3.6-35B-A3B', 'Expected original matched pilot')
    m.require(not args.output.exists(), 'Use a fresh immutable directory')
    cohort = [json.loads(l) for l in (args.reference / 'cohort.jsonl').open()]
    source = p.NativeRows(Path(reference['sourcePack']) / 'native-rows.jsonl')
    source_tok, _ = m.original.local_renderer()
    tok, _ = m.local_model('qwen35_base')
    native = m.prep.native_runtime_from_tokenizer('qwen35_base', tok)
    meta_dir = m.prep.OLD_TOKENIZERS / 'qwen35_base'
    meta = json.loads((meta_dir / 'native-pack.json').read_text())
    for name, digest in meta['tokenizerFileDigestsSHA256'].items():
        m.require(m.file_hash(meta_dir / 'tokenizer' / name) == digest, 'Pinned Base tokenizer changed')
    import tinker
    args.output.mkdir(parents=True)
    compact = {}
    with (args.output / 'native-rows.jsonl').open('x') as out:
        for e in cohort:
            old = source[e['exampleID']]
            semantic, target = m.prep.source_text(old, source_tok)
            m.require(target == e['targetText'], 'Target changed')
            row = m.prep.supervised_row(e, semantic, native)
            m.native_datum(row, tinker, 248044)
            m.require(row['promptTokenIDs'] == tok.encode(semantic, add_special_tokens=False), 'Base input is not exact raw continuation')
            m.require(row['completionTokenIDs'] == tok.encode(target, add_special_tokens=False) + [248044], 'Base content/EOS changed')
            m.require(row['promptTokenCount'] + 512 <= 65536, 'Generation context too long')
            row.update(pipeline='new', model='Qwen/Qwen3.5-35B-A3B-Base', sourceFullSequenceTokenSHA256=old['fullSequenceTokenSHA256'])
            out.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')
            compact[e['exampleID']] = {k:v for k,v in row.items() if not k.endswith('TokenIDs')}
    shutil.copyfile(args.reference / 'cohort.jsonl', args.output / 'cohort.jsonl')
    report = copy.deepcopy(reference)
    # The reference's process memory is not a measurement of this preparation.
    report.pop('memoryPeakMiB', None)
    report.update(version=VERSION, model='Qwen/Qwen3.5-35B-A3B-Base', reasoning='not_applicable',
        referencePilotDirectory=str(args.reference.resolve()),
        purpose='Matched Base-versus-Qwen3.6 small future-write learning comparison; 2e-4 primary, other rates diagnostic.',
        arms=contracts(reference, 'Qwen/Qwen3.5-35B-A3B-Base'),
        nativeRowsSHA256=m.file_hash(args.output / 'native-rows.jsonl'), cohortSHA256=m.file_hash(args.output / 'cohort.jsonl'),
        tokenizer={'localTokenizerRevision': m.prep.MODEL_SPECS['qwen35_base']['localTokenizerRevision'],
            'tokenizerVocabularySHA256': m.fingerprint(tok.get_vocab()),
            'tokenizerFilesSHA256': {f.name:m.file_hash(f) for f in sorted((meta_dir/'tokenizer').iterdir()) if f.is_file()},
            'nativeStopTokenIDs': [248044], 'pasteMarkerIDs': tok.encode('<|paste|>', add_special_tokens=False)},
        nativeFormat='Exact semantic text without chat or empty reasoning envelopes; exact authored target/literal paste string plus native EOS248044.',
        primaryComparisonLearningRate=2e-4,
        counts={'uniqueTrainingExamples':50, 'uniqueFutureExamples':50, 'trainingPresentations':200,
                'frozenGenerations':50, 'personalizedGenerations':200, 'targetNLLCalls':250, 'optimizerSteps':200})
    report['sourceBindingsSHA256'].update({str((args.reference / n).resolve()):m.file_hash(args.reference / n)
                                         for n in ('preparation.json', 'native-rows.jsonl', 'cohort.jsonl', 'audit.json')})
    report['codeSHA256'][str(Path(__file__).resolve())] = m.file_hash(Path(__file__))
    report['sourceBindingsSHA256'][str((meta_dir / 'native-pack.json').resolve())] = m.file_hash(meta_dir / 'native-pack.json')
    report['tokens'].update(trainingPositionsPerRate=sum(compact[e]['trainingDatumPositions'] for e in report['trainIDs']),
        lossBearingPresentationsPerRate=sum(compact[e]['lossBearingTokenCount'] for e in report['trainIDs']),
        evaluationTargetTokens=sum(compact[e]['lossBearingTokenCount'] for e in report['evaluationIDs']),
        promptMinimum=min(x['promptTokenCount'] for x in compact.values()),
        promptMaximum=max(x['promptTokenCount'] for x in compact.values()))
    import statistics
    report['tokens']['promptMedian'] = statistics.median(x['promptTokenCount'] for x in compact.values())
    report['budget'] = budget(compact, report['trainIDs'], report['evaluationIDs'], report['prices'])
    m.require(report['budget']['proposedAuthorizationUSD'] < 20, 'Budget exceeds quoted $20 ceiling')
    r.native_spec(report)
    m.original.save(args.output / 'preparation.json', report)
    print(json.dumps({'status':'offline_prepared', 'counts':report['counts'], 'tokens':report['tokens'], 'budget':report['budget']}, indent=2))


def audit(directory):
    report = json.loads((directory / 'preparation.json').read_text())
    for path, digest in {**report['sourceBindingsSHA256'], **report['codeSHA256']}.items():
        m.require(m.file_hash(path) == digest, 'Bound source/code changed: ' + path)
    reference_dir = Path(report['referencePilotDirectory'])
    reference, previous_rows = r.load_prepared(reference_dir)
    cohort = [json.loads(l) for l in (directory / 'cohort.jsonl').open()]
    blocks = json.loads((Path(report['sourcePack']) / 'blocks.json').read_text())
    assert p.select(cohort, blocks) == (reference['trainIDs'], reference['evaluationIDs'])
    assert report['arms'] == contracts(reference, report['model'])
    assert m.file_hash(directory / 'cohort.jsonl') == reference['cohortSHA256'] == report['cohortSHA256']
    assert m.file_hash(directory / 'native-rows.jsonl') == report['nativeRowsSHA256']
    tok, _ = m.local_model('qwen35_base'); native = m.prep.native_runtime_from_tokenizer('qwen35_base', tok)
    assert m.fingerprint(tok.get_vocab()) == report['tokenizer']['tokenizerVocabularySHA256']
    source_tok, _ = m.original.local_renderer()
    source = p.NativeRows(Path(reference['sourcePack']) / 'native-rows.jsonl')
    rows = p.NativeRows(directory / 'native-rows.jsonl'); compact = {}
    import tinker
    for e in cohort:
        row = rows[e['exampleID']]
        semantic, target = m.prep.source_text(source[e['exampleID']], source_tok)
        expected = m.prep.supervised_row(e, semantic, native)
        assert all(row[k] == v for k,v in expected.items())
        assert row['modelInputSHA256'] == previous_rows[e['exampleID']]['modelInputSHA256']
        assert row['targetSHA256'] == previous_rows[e['exampleID']]['targetSHA256']
        datum = m.native_datum(row, tinker, 248044)
        assert datum.loss_fn_inputs['weights'].to_numpy().tolist() == [0.]*(row['promptTokenCount']-1)+[1.]*row['lossBearingTokenCount']
        assert row['completionTokenIDs'] == tok.encode(target, add_special_tokens=False)+[248044]
        try: m.native_datum(row, tinker, 248046)
        except m.ContractError: pass
        else: raise AssertionError('Wrong hybrid EOS accepted for Base')
        compact[e['exampleID']] = row
    assert budget(compact, report['trainIDs'], report['evaluationIDs'], report['prices']) == report['budget']
    result = {'status':'audit_passed_OFFLINE_ONLY', 'examples':100, 'trainingExamples':50, 'futureExamples':50,
              'exactSameSemanticInputsTargetsAndOrderAsQwen36':True, 'nativeRawCompletionEOS248044Verified':True,
              'everyShiftedLossPositionVerified':True, 'wrongHybridEOSRejected':True, 'providerCalls':0,
              'filesSHA256':{n:m.file_hash(directory/n) for n in ('preparation.json','native-rows.jsonl','cohort.jsonl')},
              'auditCodeSHA256':m.file_hash(Path(__file__))}
    m.original.save(directory/'audit.json', result); print(json.dumps(result, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--reference',type=Path)
    parser.add_argument('--output',type=Path); parser.add_argument('--audit',type=Path)
    a = parser.parse_args(); audit(a.audit) if a.audit else prepare(a)

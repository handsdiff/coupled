#!/usr/bin/env python3
"""No-network Qwen3.6 rehearsal on the actual independent block schedules."""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import tempfile


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    result = importlib.util.module_from_spec(spec); spec.loader.exec_module(result)
    return result


p = module('q36_comparison', 'prepare-phase1-qwen36-comparison.py')
f = module('execution_fakes', 'check-phase1-pipeline-era-execution.py')
from phase1_qwen38_execution import (Executor, Journal, TinkerBridge, datum_from_row,
                                    fingerprint, require, verify_execution_binding)


def check(directory):
    p.offline()
    plan = p.load(directory/'preparation.json')
    audit = p.load(directory/'audit.json')
    require(audit['status'] == 'passed' and audit['planFingerprint'] == fingerprint(plan), 'Native audit missing/stale')
    for path,digest in {**plan['sourceFilesSHA256'],**plan['runtimeFilesSHA256']}.items():
        require(p.file_hash(path) == digest, 'Changed input or runtime: '+path)
    verify_execution_binding(plan)
    contract = plan['contract']
    require(contract['model'] == p.MODEL and contract['reasoning'] is False and
            contract['optimizer']['learningRate'] == 2e-4, 'Wrong chosen model/recipe')
    native = p.native_runtime(p.load(plan['pilotPreparationPath']))
    tok, renderer = native.tokenizer, native.renderer
    cohorts = {arm:[json.loads(l) for l in (directory/arm/'cohort.jsonl').open()] for arm in ('old','new')}
    blocks = {arm:p.load(directory/arm/'blocks.json') for arm in cohorts}
    rows = f.compact_rows(cohorts,tok)
    expected_steps = {a:sum(len(b['exampleIDs']) for b in bs if b['trainThisBlockAfterScoring']) for a,bs in blocks.items()}
    expected_counts = {'generation':sum(a['counts']['generations'] for a in plan['arms'].values()),
                       'nll':sum(a['counts']['nllCalls'] for a in plan['arms'].values()),
                       'train':sum(expected_steps.values())}
    output = tok.encode('offline fixture',add_special_tokens=False)+[248046]
    checks = []
    with tempfile.TemporaryDirectory(prefix='qwen36-comparison-check-') as temp:
        temp = Path(temp)
        binding = {'plan':fingerprint(plan),'contract':fingerprint(contract)}

        def execute(name, service, **kwargs):
            journal = Journal(temp/name,binding)
            bridge = TinkerBridge(service,f.tinker,tok,renderer,contract)
            result = Executor(bridge,rows,blocks,journal,p.PRICES,100,**kwargs).run()
            return journal,result

        base = f.FakeService(rows,blocks,contract,output)
        journal,result = execute('full',base)
        require(result['committedSteps'] == expected_steps, 'Incorrect training exposure')
        counts = {k:sum(c[0] == k for c in base.calls) for k in expected_counts}
        require(counts == expected_counts, 'Wrong operation counts')
        require(all(r['value']['meanNLL'] == .25 for r in journal.records
                    if r['kind'] == 'operation_result' and r['operation'] == 'nll'), 'Prompt loss leaked into NLL')
        states = {arm:base.saved[journal.commits(arm)[-1]['optimizerStatePath']] for arm in cohorts}
        calls = len(base.calls); execute('full',base)
        require(len(base.calls) == calls, 'Completed run replays requests')
        checks += ['actual_independent_schedules', 'complete_block_scored_before_update',
                   'each_new_example_trained_once', 'no_final_update', 'target_only_NLL', 'completed_resume_no_replay']
        for name,mode in (('training','train'),('scoring','score'),('checkpoint','save')):
            fake = f.FakeService(rows,blocks,contract,output)
            if mode == 'train': fake.fail_train_at = ('old',63)
            if mode == 'score': fake.fail_generation = True
            if mode == 'save': fake.fail_save = True
            try:
                execute(name,fake)
                raise AssertionError('Injected failure not observed')
            except RuntimeError as error:
                require('injected' in str(error),str(error))
            f.rejected(lambda:execute(name,fake))
            resumed,_ = execute(name,fake,retry_incomplete=True)
            for arm in cohorts:
                require(fake.saved[resumed.commits(arm)[-1]['optimizerStatePath']] == states[arm],
                        'Recovery changed Adam state or final exposure')
            # All abandoned paid attempts are still counted, not overwritten.
            require(sum(r.get('maximumUSD',0) for r in resumed.records if r['kind']=='operation_begin') >=
                    sum(r.get('maximumUSD',0) for r in journal.records if r['kind']=='operation_begin'),
                    'Retry spending disappeared')
            checks.append(name+'_interruption_preserves_optimizer_and_attempt_cost')
        pause = f.FakeService(rows,blocks,contract,output)
        execute('between',pause,max_blocks=2)
        resumed,_ = execute('between',pause)
        for arm in cohorts:
            require(pause.saved[resumed.commits(arm)[-1]['optimizerStatePath']] == states[arm], 'Clean resume changed optimizer')
        require(len(pause.restores) == 2, 'Full optimizer restoration missing')
        checks.append('clean_resume_restores_full_optimizer_not_weights_only')
        bad = copy.deepcopy(blocks); bad['old'][0]['lastAvailableAt'] = bad['old'][1]['firstBeganAt']
        bridge = TinkerBridge(base,f.tinker,tok,renderer,contract)
        f.rejected(lambda:Executor(bridge,rows,bad,Journal(temp/'leak',binding),p.PRICES,100))
        bad = copy.deepcopy(blocks); bad['old'] = copy.deepcopy(blocks['new'])
        f.rejected(lambda:Executor(bridge,rows,bad,Journal(temp/'same',binding),p.PRICES,100))
        f.rejected(lambda:Journal(temp/'full',{'plan':'changed'}))
        zero = f.FakeService(rows,blocks,contract,output)
        f.rejected(lambda:Executor(TinkerBridge(zero,f.tinker,tok,renderer,contract),rows,blocks,
            Journal(temp/'zero',binding),p.PRICES,0).run())
        require(not zero.calls,'Cost check happened after dispatch')
        checks += ['future_target_and_shared_schedule_rejected','changed_binding_rejected','zero_budget_no_dispatch']
    # The first authored token and whitespace/paste/Unicode must remain in loss.
    for text in prep_targets():
        example = {'exampleID':'synthetic','experimentBlockID':'synthetic-block','targetEventID':'synthetic-event',
                   'targetText':text,'target':{'segments':[{'type':'authored_text','content':text}]}}
        row = p.prep.supervised_row(example,'synthetic causal input',native)
        p.validate_row(row,example,'synthetic causal input',native)
        datum = datum_from_row(row,f.tinker)
        n = row['promptTokenCount']
        require(datum.loss_fn_inputs['weights'].to_numpy().tolist()[n-1] == 1., 'First content token is masked')
        broken = copy.deepcopy(row); broken['completionTokenIDs'][-1] = 248044
        f.rejected(lambda:datum_from_row(broken,f.tinker))
    checks += ['synthetic_whitespace_Unicode_paste_first_token_EOS_loss', 'Base_EOS_rejected']
    result = {'status':'passed','networkBlocked':True,'providerCalls':0,'planFingerprint':fingerprint(plan),
              'checks':checks,'mockOperationCounts':expected_counts,'committedTrainingExamples':expected_steps,
              'note':'Real rows audited exhaustively separately; lightweight token fixtures use the exact full schedules for recovery tests. No remote GPU/launch certification.'}
    p.save(directory/'execution-checks.json',result)
    print(json.dumps(result,indent=2))


def prep_targets():
    return [' leading and trailing ', 'First\n\nSecond\n', 'review <|paste|> tomorrow', '🧠 café 中文']


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__); ap.add_argument('--prepared',type=Path,required=True)
    check(ap.parse_args().prepared.resolve())

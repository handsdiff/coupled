#!/usr/bin/env python3
"""Bounded fresh/fresh/restored control; no change to production training.

All calls, including uncertain dispatches, retain their original cost reservation.
An existing execution is never replayed. Comparisons are descriptive, not a new
acceptance tolerance or authorization for the full experiment.
"""
from __future__ import annotations

import argparse
import copy
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import importlib.util
_spec = importlib.util.spec_from_file_location('control_recovery', Path(__file__).with_name('continue-phase1-qwen-preflights.py'))
r = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(r)
m = r.m
VERSION = 'phase1-qwen-fresh-client-control-v1'
CASES = [('qwen36_hybrid', 'old'), ('qwen35_base', 'new')]
TOKEN_CAP = 1.75
TOLERANCE = .005  # Existing descriptive threshold; never adjusted to the result.


def schedule(ids):
    """One frozen dispatch list is used for planning, execution and audit."""
    out = []
    def add(key, action, branch, index=2):
        kind = 'train' if action in ('forward', 'update') else 'nll' if action == 'nll' else 'admin'
        out.append(dict(key=key, action=action, branch=branch, exampleID=ids[index], kind=kind))
    for b in 'AB': add(f'{b}/create', 'create', b, 0)
    for b in 'AB': add(f'initial/{b}/forward', 'forward', b, 0)
    for i in range(2):
        for b in 'AB': add(f'{b}/update/{i+1}', 'update', b, i)
    for b in 'AB': add(f'pre/{b}/checkpoint', 'checkpoint', b)
    add('C/create', 'create', 'C', 0)
    add('C/load', 'load', 'C', 0)
    add('pre/C/checkpoint', 'checkpoint', 'C')
    for phase in ('pre', 'post'):
        if phase == 'post':
            for b in 'ABC': add(f'{b}/update/3', 'update', b)
        # Rotate call order so branch and position are not inseparable.
        for i, order in enumerate(('ABC', 'BCA', 'CAB')):
            for b in order: add(f'{phase}/{b}/forward/{i}', 'forward', b)
        if phase == 'post':
            for b in 'ABC': add(f'post/{b}/checkpoint', 'checkpoint', b)
        for b in 'ABC':
            for i in (0, 2): add(f'{phase}/{b}/nll/{i}', 'nll', b, i)
    return out


def comparisons(values):
    result = {}
    for phase in ('pre', 'post'):
        result[phase] = {'trainingForward': {}, 'sampler': {}, 'withinClient': {}}
        for a, b in itertools.combinations('ABC', 2):
            result[phase]['trainingForward'][a+b] = [r.difference(values[f'{phase}/{a}/forward/{i}'],
                values[f'{phase}/{b}/forward/{i}']) for i in range(3)]
            result[phase]['sampler'][a+b] = {str(i): r.difference(values[f'{phase}/{a}/nll/{i}'],
                values[f'{phase}/{b}/nll/{i}']) for i in (0, 2)}
        for b in 'ABC':
            result[phase]['withinClient'][b] = [r.difference(values[f'{phase}/{b}/forward/{i}'],
                values[f'{phase}/{b}/forward/{j}']) for i, j in itertools.combinations(range(3), 2)]
    result['initialFreshClients'] = r.difference(values['initial/A/forward'], values['initial/B/forward'])
    def largest(items): return max(x['maxTokenLogprobDelta'] for x in items)
    fresh = largest(result['pre']['trainingForward']['AB'])
    restored = largest(result['pre']['trainingForward']['AC'])
    if result['initialFreshClients']['maxTokenLogprobDelta'] > TOLERANCE or fresh > TOLERANCE:
        finding = 'independent_fresh_clients_differ_restore_not_isolated'
    elif restored > TOLERANCE:
        finding = 'restore_specific_training_forward_difference_before_next_update'
    elif max(largest(result['post']['trainingForward']['AC']),
             largest(result['post']['sampler']['AC'].values())) > TOLERANCE:
        finding = 'difference_emerges_after_next_update'
    else:
        finding = 'discrepancy_not_reproduced_on_selected_probes'
    result.update(finding=finding, descriptiveTolerance=TOLERANCE,
        mainRunAuthorized=False,
        limitation='One three-example trajectory per model; not proof of optimizer equality or general provider determinism.')
    return result


def experiment(bridge, rows, operations, invoke):
    clients, checkpoints, values = {}, {}, {}
    for op in operations:
        b, action, key = op['branch'], op['action'], op['key']
        row = rows[op['exampleID']]
        def call():
            if action == 'create':
                clients[b] = bridge.trainer('fresh-client-control-'+b)
                return {'info': m.plain(clients[b].get_info()), 'contract': bridge.contract}
            if action == 'load':
                parent = checkpoints['pre/A/checkpoint']
                response = clients[b].load_state_with_optimizer(parent['optimizerStatePath']).result()
                return {'response': m.plain(response), 'info': m.plain(clients[b].get_info()), 'parent': parent}
            if action == 'checkpoint':
                checkpoints[key] = bridge.checkpoint(clients[b], key.replace('/', '-'))
                return checkpoints[key]
            if action == 'forward': return bridge.forward_only(clients[b], row)
            if action == 'update': return bridge.train(clients[b], row)
            if action == 'nll':
                path = checkpoints[f'{key.split("/")[0]}/{b}/checkpoint']['samplerCheckpointPath']
                return bridge.nll(bridge.sampler(path), row)
            raise m.ContractError('Unknown action')
        values[key] = invoke(op, row, call)
    return comparisons(values)


def bind_prior(source):
    audit = json.loads((source/'audit.json').read_text())
    m.require(audit['pendingOperations'] == 0, 'Main suite has uncertain work')
    bindings = {str((source/'audit.json').resolve()): m.file_hash(source/'audit.json')}
    for name, digest in audit['lineageSHA256'].items(): bindings[str((source/name).resolve())] = digest
    execution = json.loads((source/'execution.json').read_text())
    bindings.update(execution['priorAttempts']['filesSHA256'])
    for control in audit['restorationControls']:
        directory = m.ROOT/control['directory']
        for name, digest in control['lineageSHA256'].items(): bindings[str(directory/name)] = digest
        if control['resolution'] is not None:
            bindings[str(directory/'resolution.json')] = m.file_hash(directory/'resolution.json')
    for path, digest in bindings.items(): m.require(m.file_hash(path) == digest, 'Prior evidence changed: '+path)
    return {'filesSHA256': bindings, 'tokenBoundUSD': audit['totalAdditionalAuthorizationTokenBoundUSD'],
            'storageReserveUSD': audit['storageReserveUSD']}


def runtime():
    value = r.runtime()
    value['filesSHA256'][str(Path(__file__).resolve())] = m.file_hash(Path(__file__))
    return value


def prepare(a):
    m.require(not a.output.exists(), 'Use a fresh output directory')
    prior = bind_prior(a.source)
    source = json.loads((a.source/'execution.json').read_text())
    prepared = Path(source['preparedDirectory'])
    report, reference, rows = m.load_inputs(prepared)
    cases, maximum = [], 0.
    import tinker
    for model, pipeline in CASES:
        ids = reference['arms'][pipeline]['trainIDs'][:3]
        spec = report['models'][model]
        contract = copy.deepcopy(spec['trainingContract'])
        contract['generation'] = {**spec['generation'], 'seed': 17}
        tok, renderer = m.local_model(model)
        for eid in ids: m.native_datum(rows[model][pipeline][eid], tinker, spec['nativeStopTokenIDs'][0])
        ops = schedule(ids)
        cost = sum(m.original.charge(o['kind'], rows[model][pipeline][o['exampleID']], m.prep.PRICES[model]) for o in ops)
        maximum += cost
        cases.append({'modelKey': model, 'pipeline': pipeline, 'contract': contract, 'exampleIDs': ids,
            'rowsSHA256': {eid: m.fingerprint(rows[model][pipeline][eid]) for eid in ids},
            'vocabularySHA256': m.fingerprint(tok.get_vocab()), 'operations': ops,
            'maximumTokenCostUSD': cost, 'prices': m.prep.PRICES[model]})
    combined = prior['tokenBoundUSD'] + prior['storageReserveUSD'] + maximum
    m.require(maximum <= TOKEN_CAP and combined <= 20., 'Diagnostic exceeds existing authorization')
    a.output.mkdir(parents=True)
    plan = {'version': VERSION, 'projectID': m.PROJECT, 'source': str(a.source.resolve()),
        'preparedDirectory': str(prepared), 'preparedSHA256': m.file_hash(prepared/'preparation.json'),
        'runtime': runtime(), 'implementationCommit': subprocess.check_output(['git','rev-parse','HEAD'],cwd=m.ROOT,text=True).strip(),
        'prior': prior, 'cases': cases, 'maximumTokenCostUSD': maximum, 'diagnosticTokenCapUSD': TOKEN_CAP,
        'combinedMaximumIncludingStorageUSD': combined, 'sharedCeilingUSD': 20.,
        'authorization': 'User requested the three-branch control; within remaining additional-model preflight authorization.',
        'recovery': 'Never replay an existing output; uncertain calls stay charged; stop for review.',
        'reasoning': 'Off; frozen native targets, masks and optimizer recipe unchanged.',
        'checkpointPolicy': 'Fresh parents, 3600-second TTL; previous expired checkpoints are not reused.',
        'mainRunAuthorized': False}
    m.original.save(a.output/'plan.json', plan)
    print(json.dumps({k: plan[k] for k in ('version','maximumTokenCostUSD','combinedMaximumIncludingStorageUSD')}))


def execute(a):
    plan = json.loads((a.output/'plan.json').read_text())
    m.require(plan['version'] == VERSION and plan['projectID'] == m.PROJECT, 'Wrong plan')
    m.require(not (a.output/'operations.jsonl').exists(), 'Do not replay existing work')
    m.require(not subprocess.check_output(['git','status','--porcelain'],cwd=m.ROOT,text=True).strip(), 'Commit before paid calls')
    m.require(subprocess.check_output(['git','rev-parse','HEAD'],cwd=m.ROOT,text=True).strip() == plan['implementationCommit'], 'Commit changed')
    m.require(plan['runtime'] == runtime(), 'Runtime changed')
    m.require(bind_prior(Path(plan['source'])) == plan['prior'], 'Prior budget/evidence changed')
    prepared = Path(plan['preparedDirectory'])
    m.require(m.file_hash(prepared/'preparation.json') == plan['preparedSHA256'], 'Preparation changed')
    report, reference, rows = m.load_inputs(prepared)
    price_data = json.loads(subprocess.check_output(['curl','--fail','--silent','--show-error','--max-time','30',m.original.PRICING_URL],text=True))
    for case in plan['cases']:
        model, pipeline = case['modelKey'], case['pipeline']
        m.require(case['operations'] == schedule(case['exampleIDs']), 'Schedule changed')
        for eid, digest in case['rowsSHA256'].items(): m.require(m.fingerprint(rows[model][pipeline][eid]) == digest, 'Input changed')
        price = next(x for x in price_data if x['tinker_id'] == case['contract']['model'])
        m.require({k: float(price[k].lstrip('$')) for k in ('train','prefill','sample')} == case['prices'], 'Pricing changed')
    m.original.save(a.output/'pricing.json', {'source':m.original.PRICING_URL,'checkedAt':m.utc(),
        'prices':{c['modelKey']:c['prices'] for c in plan['cases']}})
    import tinker
    os.environ['TINKER_API_KEY'] = m.original.api_key(a.env_file)
    service = tinker.ServiceClient(project_id=m.PROJECT,max_retries=0,user_metadata={'purpose':VERSION})
    proxy = m.original.NoAutomaticResampling(service)
    journal = m.Journal(a.output, {'planSHA256':m.file_hash(a.output/'plan.json')})
    state = {'version':VERSION,'status':'running','startedAt':m.utc(),'sessionID':service.holder.get_session_id(),
        'planSHA256':m.file_hash(a.output/'plan.json'),'implementationCommit':plan['implementationCommit'],
        'modelRevision':'unverified: provider exposes model name only','models':{},'mainRunStarted':False}
    def save():
        state['reservedTokenCostUSD'] = sum(o.get('maximumUSD',0.) for o in journal.records if o['kind']=='operation_begin')
        state['combinedIncludingStorageUSD'] = plan['prior']['tokenBoundUSD'] + plan['prior']['storageReserveUSD'] + state['reservedTokenCostUSD']
        m.original.save(a.output/'result.json', state)
    save()
    try:
        with journal.exclusive():
            for case in plan['cases']:
                model, pipeline = case['modelKey'], case['pipeline']
                tok, renderer = m.local_model(model)
                remote = proxy.create_sampling_client(base_model=case['contract']['model']).get_tokenizer()
                m.require(m.fingerprint(remote.get_vocab()) == case['vocabularySHA256'] == m.fingerprint(tok.get_vocab()), 'Tokenizer mismatch')
                for eid in case['exampleIDs']:
                    row = rows[model][pipeline][eid]
                    for field in ('promptTokenIDs','completionTokenIDs'):
                        m.require(remote.decode(row[field],clean_up_tokenization_spaces=False) == tok.decode(row[field],clean_up_tokenization_spaces=False), 'Token decoding changed')
                bridge = r.DiagnosticBridge(proxy,tinker,tok,renderer,case['contract'])
                calls = m.SharedCalls(journal,model,case['prices'],512,carried_usd=plan['prior']['tokenBoundUSD'])
                index = 0
                def invoke(op,row,fn):
                    nonlocal index
                    m.require(op == case['operations'][index], 'Dispatch out of order')
                    spent = sum(o.get('maximumUSD',0.) for o in journal.records if o['kind']=='operation_begin')
                    fee = m.original.charge(op['kind'],row,case['prices'])
                    m.require(spent+fee <= plan['maximumTokenCostUSD']+1e-9 and spent+fee <= TOKEN_CAP, 'Diagnostic cap reached')
                    value = calls.call(op['key'],op['kind'],row,fn)
                    index += 1
                    save()
                    return value
                state['models'][model] = {'pipeline':pipeline,'tokenizerMatched':True,
                    **experiment(bridge,rows[model][pipeline],case['operations'],invoke)}
                save()
            state.update(status='complete',finishedAt=m.utc())
    except BaseException as error:
        state.update(status='stopped_requires_review',errorType=type(error).__name__)
        raise
    finally: save()
    print(json.dumps({'status':state['status'],'reservedTokenCostUSD':state['reservedTokenCostUSD'],
        'findings':{k:v['finding'] for k,v in state['models'].items()}},indent=2))


def audit(a):
    """Recompute conclusions from immutable responses, without a provider."""
    plan = json.loads((a.output/'plan.json').read_text())
    state = json.loads((a.output/'result.json').read_text())
    m.require(state['status'] == 'complete', 'Diagnostic did not complete')
    m.require(state['planSHA256'] == m.file_hash(a.output/'plan.json'), 'Plan binding changed')
    m.require(bind_prior(Path(plan['source'])) == plan['prior'], 'Prior evidence changed')
    records = [json.loads(line) for line in (a.output/'operations.jsonl').open()]
    begins = [v for v in records if v['kind'] == 'operation_begin']
    ends = [v for v in records if v['kind'] == 'operation_result']
    expected = [(c, op) for c in plan['cases'] for op in c['operations']]
    keys = [c['modelKey']+'/'+op['key'] for c, op in expected]
    m.require([v['key'] for v in begins] == keys == [v['key'] for v in ends], 'Incomplete or reordered calls')
    report, reference, rows = m.load_inputs(Path(plan['preparedDirectory']))
    for (case, op), begin, end in zip(expected, begins, ends):
        row = rows[case['modelKey']][case['pipeline']][op['exampleID']]
        m.require(begin['operation'] == end['operation'] == op['kind'], 'Wrong operation')
        m.require(begin['exampleID'] == row['exampleID'], 'Wrong input')
        m.require(abs(begin['maximumUSD']-m.original.charge(op['kind'],row,case['prices'])) < 1e-12, 'Wrong charge')
        if op['action'] == 'forward': m.require(end['value']['optimizerUpdatePerformed'] is False, 'Forward performed update')
        if op['action'] in ('forward','update','nll'):
            value = end['value']
            m.require(m.nll_result(value['targetLogprobs'])['meanNLL'] == value['meanNLL'], 'NLL corrupted')
            m.require(value['lossBearingTokens'] == row['lossBearingTokenCount'], 'Loss token count changed')
    calculated = {}
    for case in plan['cases']:
        key = case['modelKey']
        values = {v['key'].split('/',1)[1]:v['value'] for v in ends if v['modelKey'] == key}
        calculated[key] = comparisons(values)
        m.require(all(state['models'][key][k] == v for k,v in calculated[key].items()), 'Conclusion changed')
    cost = sum(v['maximumUSD'] for v in begins)
    m.require(abs(cost-state['reservedTokenCostUSD']) < 1e-9 and cost <= plan['maximumTokenCostUSD']+1e-9, 'Cost drift')
    output = {'version':VERSION,'status':'audit_passed','operations':len(begins),'pendingOperations':0,
        'models':calculated,'reservedTokenCostUSD':cost,'combinedIncludingStorageUSD':state['combinedIncludingStorageUSD'],
        'invoiceCostUSD':None,'mainRunAuthorized':False,
        'lineageSHA256':{n:m.file_hash(a.output/n) for n in ('plan.json','result.json','operations.jsonl','pricing.json')},
        'auditCodeSHA256':m.file_hash(Path(__file__))}
    m.original.save(a.output/'audit.json',output)
    print(json.dumps({'status':'audit_passed','operations':len(begins),'reservedTokenCostUSD':cost}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--env-file',type=Path,default=m.ROOT/'.env')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--execute',action='store_true')
    mode.add_argument('--audit',action='store_true')
    args = parser.parse_args()
    (execute if args.execute else audit if args.audit else prepare)(args)


if __name__ == '__main__': main()

#!/usr/bin/env python3
"""Split a full frozen comparison into exact reusable rows and unsent examples.

Local only. The existing frozen executor runs the unsent subset unchanged.
Combining preserves original wire/usage/timing evidence and records reuse.
"""
import argparse
from collections import Counter
import copy
import json
from pathlib import Path
import shutil

from phase1_read_model_comparison import (
    MODELS, VARIANTS, Privacy, audit_records, canonical, dump, dump_rows,
    file_hash, fingerprint, request_plan, rows,
)

VERSION = 'phase1-factorial-full-expansion-v1'


def load(path):
    return json.loads(Path(path).read_text())


def check_files(hashes):
    for path, digest in hashes.items():
        if file_hash(path) != digest:
            raise ValueError(f'Bound source changed: {path}')


def frozen(folder):
    check_files({str(folder/name): digest for name, digest in load(folder/'artifact-hashes.json').items()})
    plan = load(folder/'plan.json')
    cohort = list(rows(folder/'cohort.jsonl'))
    prompts = list(rows(folder/'prompts.jsonl'))
    requests = list(rows(folder/'requests.planned.jsonl'))
    assert requests == request_plan(prompts, cohort, plan['selectionSeed'])
    assert len(requests) == len(cohort)*4 == plan['plannedPredictions']
    return plan, cohort, prompts, requests


def prediction_map(projection):
    manifest = load(projection/'projection.json')
    assert file_hash(projection/'predictions.jsonl') == manifest['predictionsSHA256']
    check_files(manifest['sourceSHA256'])
    records = list(rows(projection/'predictions.jsonl'))
    assert len(records) == manifest['recorded']
    result = {r['requestSHA256']:r for r in records}
    assert len(result) == len(records), 'Duplicate request evidence'
    return result


def reuse_matches(full_requests, old_requests, predictions):
    full = {r['requestSHA256']:r for r in full_requests}
    assert len(full) == len(full_requests)
    assert set(predictions) == {r['requestSHA256'] for r in old_requests}
    reuse = []
    fields = ('exampleID','model','variant','promptID','reasoningEffort','requestSHA256')
    for old in old_requests:
        current = full.get(old['requestSHA256'])
        assert current is not None, 'Prior input/model/effort differs: reuse forbidden'
        record = predictions[old['requestSHA256']]
        assert all(record[k] == old[k] == current[k] for k in fields)
        reuse.append({'fullRequestOrdinal':current['requestOrdinal'],
                      'sourceRequestOrdinal':old['requestOrdinal'],
                      **{k:current[k] for k in fields}})
    return sorted(reuse,key=lambda r:r['fullRequestOrdinal'])


def split(full_root, prior_root, output):
    assert not output.exists(), 'Fresh expansion directory required'
    full_root, prior_root = full_root.resolve(), prior_root.resolve()
    full_dir, old_dir = full_root/'frozen', prior_root/'frozen'
    plan, cohort, prompts, requests = frozen(full_dir)
    old_plan, old_cohort, old_prompts, old_requests = frozen(old_dir)
    assert plan['models'] == old_plan['models'] == list(MODELS)
    for field in ('pipelineArms','privacyPolicy','providerContract','historyOrdering','reasoningEffort'):
        assert plan[field] == old_plan[field], f'Experiment contract changed: {field}'
    for field in ('tokens','referenceTokenizer','revision','instruction'):
        assert plan['contextBudget'][field] == old_plan['contextBudget'][field]
    old_ids = {r['exampleID'] for r in old_cohort}
    assert old_ids < {r['exampleID'] for r in cohort}, 'Not a strict expansion'
    by_id = {r['exampleID']:r for r in cohort}
    for old in old_cohort:
        assert old == by_id[old['exampleID']], 'Reused target/query/evidence changed'
    prompt_by_id = {p['promptID']:p for p in prompts}
    for old in old_prompts:
        assert old == prompt_by_id[old['promptID']], 'Reused prompt/packing changed'
    projection = prior_root/'execution-v1-final/final-output-v2'
    existing = prediction_map(projection)
    reuse = reuse_matches(requests, old_requests, existing)
    pending_cohort = [e for e in cohort if e['exampleID'] not in old_ids]
    pending_prompts = [p for p in prompts if p['exampleID'] not in old_ids]
    pending_requests = request_plan(pending_prompts, pending_cohort, plan['selectionSeed'])
    assert {r['requestSHA256'] for r in pending_requests}.isdisjoint(existing)
    assert set(existing) | {r['requestSHA256'] for r in pending_requests} == {r['requestSHA256'] for r in requests}
    config_path = next(Path(k) for k in plan['sourceHashes'] if Path(k).name == 'config.json')
    config = load(config_path)
    privacy = Privacy({r['sourceEventID']:r for r in rows(Path(config['newCorpus'])/'events.jsonl')},plan['privacyPolicy'])
    audit = audit_records(pending_cohort,pending_prompts,pending_requests,privacy,plan['contextBudget']['tokens'])
    output.mkdir(parents=True,mode=0o700)
    out = output/'remaining-frozen';out.mkdir(mode=0o700)
    for name in ('old-context-events.jsonl','new-context-events.jsonl','old-native-event-order.jsonl','excluded-examples.jsonl'):
        shutil.copy2(full_dir/name,out/name)
    dump_rows(out/'cohort.jsonl',pending_cohort)
    dump_rows(out/'prompts.jsonl',pending_prompts)
    dump_rows(out/'requests.planned.jsonl',pending_requests)
    dump(out/'audit.json',audit)
    source_bindings={str(p):file_hash(p) for p in (
        full_dir/'plan.json',full_dir/'artifact-hashes.json',old_dir/'plan.json',
        old_dir/'artifact-hashes.json',projection/'predictions.jsonl',projection/'projection.json',
        Path(__file__).resolve(),Path(__file__).with_name('phase1_read_model_comparison.py').resolve())}
    subset = copy.deepcopy(plan)
    subset.update(exampleCount=len(pending_cohort),plannedPredictions=len(pending_requests),
        exampleCountsByApplication=dict(Counter(e['modelFacingDestination']['application'] for e in pending_cohort)),
        exampleCountsBySession=dict(Counter(e['sessionID'] for e in pending_cohort)),
        groundedPasteActions=sum(s['type']=='paste' for e in pending_cohort for s in e['target']['segments']),
        cohortSelection={'policy':'all eligible frozen targets minus exactly completed prior cohort','fullCount':len(cohort),'reusedCount':len(old_cohort)},
        sourceHashes=plan['sourceHashes'] | source_bindings)
    subset['contextBudget']['totalReferenceInputTokensPerModel']={v:sum(p['referenceInputTokens'] for p in pending_prompts if p['variant']==v) for v in VARIANTS}
    dump(out/'plan.json',subset)
    dump(out/'artifact-hashes.json',{p.name:file_hash(p) for p in sorted(out.iterdir()) if p.is_file()})
    dump_rows(output/'reuse.jsonl',reuse)
    expansion={'version':VERSION,'fullRoot':str(full_root),'priorRoot':str(prior_root),
        'fullTargets':len(cohort),'fullPredictions':len(requests),'reusedPredictions':len(reuse),
        'newTargets':len(pending_cohort),'newPredictions':len(pending_requests),'sourceSHA256':source_bindings,
        'remainingIndexSHA256':file_hash(out/'artifact-hashes.json'),'reuseSHA256':file_hash(output/'reuse.jsonl'),
        'authorization':'User requested the same test on the full dataset; same subscription route/privacy policy, no API fallback or training.',
        'executionDifference':'Prior sample responses are reused with original timestamps; remaining requests occur later. Model aliases are not server-revision-pinned.',
        'providerCallsDuringPreparation':0}
    dump(output/'expansion.json',expansion)
    print(json.dumps({k:expansion[k] for k in ('fullTargets','fullPredictions','reusedPredictions','newTargets','newPredictions')},indent=2))


def combine(expansion_dir, execution, output):
    assert not output.exists(), 'Fresh combined projection required'
    expansion=load(expansion_dir/'expansion.json');check_files(expansion['sourceSHA256'])
    assert file_hash(expansion_dir/'reuse.jsonl') == expansion['reuseSHA256']
    assert file_hash(expansion_dir/'remaining-frozen/artifact-hashes.json') == expansion['remainingIndexSHA256']
    _,cohort,_,requests=frozen(Path(expansion['fullRoot'])/'frozen')
    _,_,_,pending=frozen(expansion_dir/'remaining-frozen')
    prior=Path(expansion['priorRoot'])/'execution-v1-final/final-output-v2'
    fresh=execution/'final-output-v2'
    old_map,new_map=prediction_map(prior),prediction_map(fresh)
    assert set(new_map) == {r['requestSHA256'] for r in pending}
    assert set(old_map).isdisjoint(new_map), 'An already completed call was retransmitted'
    assert set(old_map)|set(new_map) == {r['requestSHA256'] for r in requests}
    result=[]
    for request in requests:
        reused=request['requestSHA256'] in old_map
        row=(old_map if reused else new_map)[request['requestSHA256']]
        assert all(row[k]==request[k] for k in ('exampleID','promptID','variant','model','reasoningEffort','requestSHA256'))
        result.append({**row,'requestOrdinal':request['requestOrdinal'],
            'reuseProvenance':{'reused':reused,'sourceRequestOrdinal':row['requestOrdinal'],
                              'sourcePredictions':str((prior if reused else fresh)/'predictions.jsonl')}})
    output.mkdir(parents=True,mode=0o700);dump_rows(output/'predictions.jsonl',result)
    sources={str(p.resolve()):file_hash(p) for p in (
        expansion_dir/'expansion.json',expansion_dir/'reuse.jsonl',prior/'predictions.jsonl',prior/'projection.json',
        fresh/'predictions.jsonl',fresh/'projection.json',Path(expansion['fullRoot'])/'frozen/artifact-hashes.json')}
    report={'version':VERSION,'recorded':len(result),'targets':len(cohort),'reused':len(old_map),'newlyRequested':len(new_map),
        'sourceSHA256':sources,'predictionsSHA256':file_hash(output/'predictions.jsonl'),
        'implementationSHA256':file_hash(Path(__file__)),
        'invalidPredictions':sum(not r['validCompletion'] for r in result),
        'emptyPredictions':sum(not r['prediction'] for r in result),'providerCallsDuringProjection':0}
    dump(output/'projection.json',report)
    print(json.dumps({k:report[k] for k in ('recorded','targets','reused','newlyRequested','invalidPredictions','emptyPredictions')},indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['split','combine'])
    p.add_argument('--full',type=Path);p.add_argument('--prior',type=Path);p.add_argument('--expansion',type=Path)
    p.add_argument('--execution',type=Path);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    if a.action=='split':split(a.full,a.prior,a.output)
    else:combine(a.expansion,a.execution,a.output)

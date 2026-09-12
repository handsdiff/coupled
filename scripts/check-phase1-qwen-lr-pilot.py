#!/usr/bin/env python3
"""Offline audit of all pilot rows, exact native masks and the frozen 50/50 split."""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import socket

socket.socket.connect=lambda *a,**k: (_ for _ in ()).throw(AssertionError('Network prohibited'))
spec=importlib.util.spec_from_file_location('pilot',Path(__file__).with_name('prepare-phase1-qwen-lr-pilot.py'))
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p);m=p.m


def check(directory):
    report=json.loads((directory/'preparation.json').read_text())
    for path,digest in {**report['sourceBindingsSHA256'],**report['codeSHA256']}.items():m.require(m.file_hash(path)==digest,'Bound input changed')
    m.require(m.file_hash(directory/'native-rows.jsonl')==report['nativeRowsSHA256'],'Rows changed')
    m.require(m.file_hash(directory/'cohort.jsonl')==report['cohortSHA256'],'Cohort changed')
    cohort=[json.loads(line) for line in (directory/'cohort.jsonl').open()]
    blocks=json.loads((Path(report['sourcePack'])/'blocks.json').read_text())
    train,test=p.select(cohort,blocks)
    assert (train,test)==(report['trainIDs'],report['evaluationIDs'])
    malformed=copy.deepcopy(cohort)
    malformed[0]['targetAvailableAt']=malformed[50]['targetBeganAt']
    try:p.select(malformed,blocks)
    except m.ContractError:pass
    else:raise AssertionError('Unavailable training target accepted')
    malformed=copy.deepcopy(cohort);malformed[50]['exampleID']=malformed[0]['exampleID']
    try:p.select(malformed,blocks)
    except m.ContractError:pass
    else:raise AssertionError('Overlapping split accepted')
    source=p.NativeRows(Path(report['sourcePack'])/'native-rows.jsonl')
    new=p.NativeRows(directory/'native-rows.jsonl')
    source_tok,_=m.original.local_renderer();tok,_=m.local_model('qwen36_hybrid')
    native=m.prep.native_runtime_from_tokenizer('qwen36_hybrid',tok)
    import tinker
    compact={}
    for e in cohort:
        old,row=source[e['exampleID']],new[e['exampleID']]
        semantic,target=m.prep.source_text(old,source_tok)
        expected=m.prep.supervised_row(e,semantic,native)
        assert all(row[k]==v for k,v in expected.items())
        assert row['modelInputSHA256']==old['modelInputSHA256']
        assert row['targetSHA256']==old['targetSHA256'] and target==e['targetText']
        datum=m.native_datum(row,tinker,248046)
        assert datum.loss_fn_inputs['weights'].to_numpy().tolist()==[0.]*(row['promptTokenCount']-1)+[1.]*row['lossBearingTokenCount']
        assert datum.loss_fn_inputs['target_tokens'].to_numpy().tolist()==(row['promptTokenIDs']+row['completionTokenIDs'])[1:]
        assert row['completionTokenIDs']==tok.encode(target,add_special_tokens=False)+[248046]
        assert not row.get('generationOnly') and row['promptTokenCount']+512<=65536
        compact[e['exampleID']]={k:v for k,v in row.items() if not k.endswith('TokenIDs')}
    assert p.budget(compact,train,test,report['prices'])==report['budget']
    normalized=[]
    for arm,rate in zip(report['arms'],p.RATES):
        assert arm['contract']['optimizer']['learningRate']==rate
        assert arm['trainingOrder']==p.prior_order(train,1,17)
        contract=copy.deepcopy(arm['contract']);contract['optimizer'].pop('learningRate');normalized.append(contract)
    assert len(report['arms'])==3 and normalized[0]==normalized[1]==normalized[2]
    assert report['counts']['trainingPresentations']==150 and report['counts']['targetNLLCalls']==200
    assert not report['mainRunAuthorized'] and report['providerCalls']==0
    result={'status':'audit_passed_OFFLINE_ONLY','examples':100,'trainingExamples':50,'futureExamples':50,
        'everyRowNativeHFAndCookbookAndSDKVerified':True,'causalShiftAndContentPlusEOSWeightsVerified':True,
        'onlyLearningRateChangesAcrossArms':True,'providerCalls':0,
        'filesSHA256':{name:m.file_hash(directory/name) for name in ('preparation.json','native-rows.jsonl','cohort.jsonl')},
        'auditCodeSHA256':m.file_hash(Path(__file__))}
    m.original.save(directory/'audit.json',result)
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('directory',type=Path);args=parser.parse_args();check(args.directory)

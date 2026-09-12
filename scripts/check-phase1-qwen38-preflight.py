#!/usr/bin/env python3
"""No-network checks for preflight scope, budget and optimizer fork evidence."""
import copy
import importlib.util
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace

spec=importlib.util.spec_from_file_location('preflight',Path(__file__).with_name('preflight-phase1-qwen38.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
socket.socket.connect=lambda *a,**k: (_ for _ in ()).throw(AssertionError('Network prohibited'))


class FakeBridge:
    def __init__(self,weights_only=False):self.states={};self.weights_only=weights_only;self.trained=[]
    def sampler(self,path=None):return copy.deepcopy(self.states[path]) if path else {'weight':0.,'moment':0.}
    def trainer(self,arm,parent=None):
        state=self.sampler(parent['optimizerStatePath']) if parent else self.sampler()
        if parent and self.weights_only:state['moment']=0.
        return state
    def train(self,state,row):
        state['moment']=state['moment']*.95+1
        state['weight']+=state['moment']*.1
        self.trained.append(row['exampleID'])
        return {'targetLogprobs':[-1.], 'latencySeconds':0.}
    def nll(self,sampler,row):return {'targetLogprobs':[-2.+sampler['weight']], 'meanNLL':2.-sampler['weight']}
    def generate(self,sampler,row):return {'prediction':'useful completion','latencySeconds':.01,'stopReason':'stop'}
    def checkpoint(self,state,name):
        self.states[name]=copy.deepcopy(state)
        return {'optimizerStatePath':name,'samplerCheckpointPath':name}


def rejected(fn):
    try:fn()
    except m.ContractError:return
    raise AssertionError('Expected fail-closed check')


prices={'prefill':1.86,'train':4.103,'sample':5.595}
rows={str(i):{'exampleID':str(i),'promptTokenCount':32000,'lossBearingTokenCount':32,'trainingDatumPositions':32031} for i in range(5)}
selection={'probeIDs':list(rows),'trainIDs':['0','1','2']}
with tempfile.TemporaryDirectory() as d:
    journal=m.Journal(Path(d)/'pass',{'test':True});calls=m.Calls(journal,prices,5)
    fake=FakeBridge();result=m.phases(fake,calls,rows,selection,'new')
    assert fake.trained==['0','1','2','2']
    assert result['weightRestoreMaxLogprobDelta']==result['optimizerContinuationMaxLogprobDelta']==0
    results=[r for r in journal.records if r['kind']=='operation_result']
    assert sum(r['operation']=='generation' for r in results)==10
    assert sum(r['operation']=='nll' for r in results)==14
    assert sum(r['operation']=='train' for r in results)==4
    for begin,end in zip(journal.records[1::2],journal.records[2::2]):
        assert begin['kind']=='operation_begin' and end['kind']=='operation_result' and begin['key']==end['key']
    rejected(lambda:m.phases(fake,calls,rows,selection,'new'))
    insufficient=m.Calls(m.Journal(Path(d)/'budget',{}),prices,.251)
    rejected(lambda:insufficient.call('too-large','generation',rows['0'],lambda:1))
    assert len(insufficient.journal.records)==1
    incomplete=m.Calls(m.Journal(Path(d)/'interrupted',{}),prices,5)
    try:incomplete.call('train','train',rows['0'],lambda:1/0)
    except ZeroDivisionError:pass
    assert incomplete.journal.records[-1]['kind']=='operation_begin'
    rejected(lambda:incomplete.call('train','train',rows['0'],lambda:1))
    reset=m.Calls(m.Journal(Path(d)/'bad-restoration',{}),prices,5)
    rejected(lambda:m.phases(FakeBridge(weights_only=True),reset,rows,selection,'new'))
print('PASS: 5 probes, 3 unique updates, explicit restoration fork; no silent replay; pre-dispatch budget; weights-only restore rejected')

class OverfitFake(FakeBridge):
    def nll(self,sampler,row):
        loss=.01 if sampler['weight']>=5 else 2.
        return {'targetLogprobs':[-loss]*2,'weightedNLLSum':2*loss,'lossBearingTokens':2,'meanNLL':loss}
    def train(self,state,row):
        result=super().train(state,row)
        return {**result,**self.nll(state,row)}
    def generate(self,sampler,row):
        return {'prediction':'the exact authored thought' if sampler['weight']>=5 else 'not learned',
                'latencySeconds':.01,'stopReason':'stop'}

with tempfile.TemporaryDirectory() as d:
    free={'train':0.,'prefill':0.,'sample':0.}
    rows={str(i):{'exampleID':str(i),'promptTokenCount':32000,'lossBearingTokenCount':32,'trainingDatumPositions':32031} for i in range(20)}
    journal=m.Journal(Path(d)/'extended',{});calls=m.Calls(journal,free,20)
    seen=[]
    cap=m.capability_phase(calls,rows,{'probeIDs':list(rows),'generationSeeds':[17,18,19,20]},
        lambda row,seed:(seen.append((row['exampleID'],seed)) or {'prediction':'candidate'}))
    assert len(seen)==80 and len(set(seen))==80
    extra={'overfitIDs':list(rows)[:10],'cases':[{'exampleID':e,'targetText':'the exact authored thought'} for e in rows],
           'overfitEpochsMaximum':10,'overfitEvaluationEpochs':[5,10],
           'earlyStop':{'maximumMeanNLL':.25,'maximumNLLRatioToBase':.25,'minimumNormalizedExactMatches':8}}
    fake=OverfitFake();progress=[]
    result=m.overfit_phase(fake,calls,rows,extra,progress.append)
    assert result['status']=='passed_memorization_only' and result['epochsCompleted']==5
    assert len(fake.trained)==50 and all(fake.trained.count(e)==5 for e in extra['overfitIDs'])
    assert result['snapshots'][0]['normalizedExactMatches']==10
    assert len([r for r in journal.records if r.get('operation')=='train' and r['kind']=='operation_begin'])==50
print('PASS: 20 cases x 4 distinct seeds; fresh 10-example overfit; exact target checks; epoch-5 early stop')

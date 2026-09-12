#!/usr/bin/env python3
"""No-network regression of measured restoration and authorized cost carry."""
import copy
import importlib.util
from pathlib import Path
import socket
import tempfile

socket.socket.connect=lambda *a,**k: (_ for _ in ()).throw(AssertionError('Network prohibited'))

def load(name,file):
    s=importlib.util.spec_from_file_location(name,Path(__file__).with_name(file))
    m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m

r=load('recovery','continue-phase1-qwen-preflights.py')
# Reuse the real-SDK fake service, and execute its existing native/loss tests.
f=load('native_checks','check-phase1-qwen-model-execution.py')
n=r.m.nll_result
same=n([-.5,-.6]);different=n([-.6,-.7])
assert r.classify_restore(same,same,[same,same,same],same,same)['mainRunGatePassed']
out=r.classify_restore(same,same,[same,different,same],same,different)
assert not out['mainRunGatePassed'] and out['status']=='inconclusive_training_forward_varies_without_update'
out=r.classify_restore(same,same,[same,same,same],same,different)
assert not out['mainRunGatePassed'] and out['status']=='failed_continuation_without_measured_forward_variability'
try:r.classify_restore(same,different,[same,different,same],same,different)
except r.m.ContractError:pass
else:raise AssertionError('Weight reload failure hidden by variability')

def forward(self,data,loss):
    before=list(self.service.trained);state=copy.deepcopy(self.state)
    result=self.forward_backward(data,loss)
    self.service.trained=before
    assert state==self.state
    return result
f.Trainer.forward=forward
for key in ('qwen35_base','qwen36_hybrid'):
    tok,renderer=r.m.local_model(key);native=r.m.prep.native_runtime_from_tokenizer(key,tok)
    rows={}
    for i in range(5):
        e={'exampleID':str(i),'experimentBlockID':'test','targetEventID':'test','targetText':f' example {i} <|paste|> \n',
           'target':{'segments':[{'type':'authored_text','content':f' example {i} <|paste|> \n'}]}}
        rows[str(i)]=r.m.prep.supervised_row(e,f'Predict exactly {i}',native)
    spec=f.report['models'][key];c=copy.deepcopy(spec['trainingContract'])
    c['generation']={**spec['generation'],'seed':17}
    service=f.Service(spec['model'],spec['nativeStopTokenIDs'][0],rows)
    bridge=r.DiagnosticBridge(service,f.tinker,tok,renderer,c)
    with tempfile.TemporaryDirectory() as d:
        journal=r.m.Journal(Path(d),{'test':key})
        calls=r.RecoveryCalls(journal,key,{'prefill':0.,'sample':0.,'train':0.},512)
        result=r.phases(bridge,calls,rows,{'probeIDs':list(rows),'trainIDs':['0','1','2']},'old')
        assert result['restorationDiagnostic']['status']=='exact_continuation_pass'
        assert service.trained==['0','1','2','2']
        forwards=[x for x in journal.records if x['kind']=='operation_result' and 'diagnostic-forward/' in x['key']]
        assert len(forwards)==3 and all(x['value']['optimizerUpdatePerformed'] is False for x in forwards)

with tempfile.TemporaryDirectory() as d:
    journal=r.m.Journal(Path(d),{'test':'cache'})
    cached={'model/old/base/x/nll':{'record':{'operation':'nll','value':same},'source':'bound'}}
    calls=r.RecoveryCalls(journal,'model',{'prefill':1.,'sample':1.,'train':1.},512,carried_usd=19.49,cached=cached)
    assert calls.call('old/base/x/nll','nll',{'exampleID':'x'},lambda:1/0)==same
    assert journal.records[-1]['kind']=='reused_prior_result'
    try:calls.call('new','generation',{'exampleID':'x','promptTokenCount':32000},lambda:1)
    except r.m.ContractError:pass
    else:raise AssertionError('Prior spend dropped')

prepared=r.m.ROOT/'coupled-data/sep02-10-qwen-model-preflights-20260912-v4'
report,reference,rows=r.m.load_inputs(prepared)
tok,_=r.m.local_model('qwen38_reasoning');renderer,low=r.low_rows(rows,tok)
assert len(low)==len(rows['qwen38_reasoning']['new'])
assert all(low[e]['targetSHA256']==rows['qwen38_reasoning']['new'][e]['targetSHA256'] for e in low)
assert all(low[e]['modelInputSHA256']==rows['qwen38_reasoning']['new'][e]['modelInputSHA256'] for e in low)
assert all(low[e]['promptTokenIDs']!=rows['qwen38_reasoning']['new'][e]['promptTokenIDs'] for e in low)
print('PASS: no-update diagnostic, strict reload, non-exact remains gated, both native formats, cached results, carried budget, low-reasoning same task; no network')

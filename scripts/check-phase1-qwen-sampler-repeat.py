#!/usr/bin/env python3
"""No-network branch/replay-policy checks for the zero-update scoring diagnostic."""
import importlib.util
from pathlib import Path
import socket
from types import SimpleNamespace

socket.socket.connect=lambda *a,**k: (_ for _ in ()).throw(AssertionError('Network prohibited'))
s=importlib.util.spec_from_file_location('repeat',Path(__file__).with_name('diagnose-phase1-qwen-sampler-repeat.py'))
d=importlib.util.module_from_spec(s);s.loader.exec_module(d)


class Bridge:
    def __init__(self,variable=False,exports_vary=False):
        self.clients=0;self.scores=0;self.saves=0;self.restores=0
        self.variable=variable;self.exports_vary=exports_vary
    def sampler(self,path):self.clients+=1;return (self.clients,path)
    def nll(self,client,row):
        self.scores+=1
        delta=.1 if (self.variable and self.scores==2) or (self.exports_vary and client[1]=='export/1') else 0
        return {**d.m.nll_result([-.5-delta,-.7]),'fullLogprobsSHA256':str(delta)}
    def trainer(self,name,checkpoint):
        self.restores+=1;assert checkpoint['optimizerStatePath']=='parent-state';return self
    def get_info(self):return {'model':'mock'}
    def save_weights_for_sampler(self,name,ttl_seconds):
        path=f'export/{self.saves}';self.saves+=1;assert ttl_seconds==3600
        return SimpleNamespace(result=lambda:{'path':path})


for variable,exports_vary in ((False,False),(True,False),(False,True)):
    bridge=Bridge(variable,exports_vary);seen=[]
    def op(key,kind,fn):
        assert key not in seen and kind in ('admin','nll');seen.append(key);return fn()
    result=d.phases(bridge,{'samplerCheckpointPath':'fixed','optimizerStatePath':'parent-state'},{},op)
    assert result['optimizerUpdates']==result['generations']==0
    if variable:
        assert result['finding']=='same_saved_checkpoint_scoring_varies'
        assert bridge.restores==bridge.saves==0 and bridge.scores==6
    else:
        assert bridge.restores==1 and bridge.saves==2 and bridge.scores==10
        assert result['finding']==('export_or_scoring_difference_after_stable_fixed_probe' if exports_vary else 'no_material_difference_in_fixed_checkpoint_or_repeated_exports')
print('PASS: same and independent sampler clients, conditional exports, explicit variability, zero updates/generations; no network')

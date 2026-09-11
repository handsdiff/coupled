#!/usr/bin/env python3
"""No-network checks for exact-request reuse in a full-cohort expansion."""
import copy
import importlib.util
from pathlib import Path
from unittest.mock import patch

from phase1_read_model_comparison import request_plan

spec=importlib.util.spec_from_file_location('extension',Path(__file__).with_name('extend-phase1-read-model-comparison.py'))
extension=importlib.util.module_from_spec(spec);spec.loader.exec_module(extension)


def rejected(fn):
    try:fn()
    except (AssertionError,KeyError,ValueError):return
    raise AssertionError('Invalid reuse was accepted')


with patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('Network forbidden')):
    cohort=[{'exampleID':f'example-{n}'} for n in range(4)]
    prompts=[{'exampleID':e['exampleID'],'variant':v,'promptID':e['exampleID']+v,
              'modelInput':'Synthetic public input '+e['exampleID']+v} for e in cohort for v in ('old','new')]
    full=request_plan(prompts,cohort,17)
    prior=request_plan(prompts[:4],cohort[:2],17)
    predictions={r['requestSHA256']:{**r,'prediction':'synthetic output','status':'recorded'} for r in prior}
    matched=extension.reuse_matches(full,prior,predictions)
    assert len(matched)==8
    assert {r['requestSHA256'] for r in matched}==set(predictions)
    # New schedule ordinals may differ; request body identity must not.
    assert any(r['fullRequestOrdinal']!=r['sourceRequestOrdinal'] for r in matched)
    altered=copy.deepcopy(predictions);first=next(iter(altered));altered[first]['model']='wrong-model'
    rejected(lambda:extension.reuse_matches(full,prior,altered))
    altered=copy.deepcopy(predictions);altered[first]['promptID']='different-prompt'
    rejected(lambda:extension.reuse_matches(full,prior,altered))
    rejected(lambda:extension.reuse_matches(full,prior,dict(list(predictions.items())[1:])))
    changed_prompts=copy.deepcopy(prompts);changed_prompts[0]['modelInput']+=' changed'
    rejected(lambda:extension.reuse_matches(request_plan(changed_prompts,cohort,17),prior,predictions))
    rejected(lambda:extension.reuse_matches(full+[full[0]],prior,predictions))
print('PASS: reused requests survive ordinal changes; changed prompt/model/body, missing and duplicate evidence are rejected; no network')

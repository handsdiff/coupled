#!/usr/bin/env python3
"""Refine mixed-stage labels from already saved exact packed witnesses.

No tokenization, semantic decisions, or source content is changed.
"""
import argparse
from collections import defaultdict
from copy import deepcopy
import hashlib
import json
from pathlib import Path


def read(p): return json.loads(p.read_text())
def save(p,v):
    with p.open('x') as f:json.dump(v,f,ensure_ascii=False,indent=2,sort_keys=True);f.write('\n')


def split(witness):
    if witness['stage']!='viewport_edge_and_ocr':return [witness]
    grouped=defaultdict(list)
    for phrase in witness['phrases']:
        grouped['ocr_recognition_layout' if phrase.casefold()=='rhf' else 'viewport_edge'].append(phrase)
    return [dict(witness,stage=stage,phrases=phrases) for stage,phrases in grouped.items()]


def main():
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
    summary=read(a.source/'summary.json');cases=read(a.source/'cases.json')
    for variant in ['before','after']:
        groups=defaultdict(set)
        for c in cases:
            original=c[variant]['verifiedReadDefects']
            c[variant]['verifiedReadDefects']=[w for old in original for w in split(old)]
            assert bool(original)==bool(c[variant]['verifiedReadDefects'])
            for w in c[variant]['verifiedReadDefects']:groups[w['stage']].add(c['case'])
        summary[variant]['byEarliestStage']={k:sorted(v) for k,v in groups.items()}
    summary['sourcePackedAudit']=str(a.source.resolve())
    summary['labelRefinement']='RHF recognition and clipped-bottom garbage counted separately using the exact retained phrase; token inputs unchanged'
    summary['sourceCasesSHA256']=hashlib.sha256((a.source/'cases.json').read_bytes()).hexdigest()
    save(a.output/'cases.json',cases);save(a.output/'summary.json',summary)
    save(a.output/'verified-read-witnesses.json',[w for old in read(a.source/'verified-read-witnesses.json') for w in split(old)])
    print(json.dumps({k:len(v) for k,v in summary['after']['byEarliestStage'].items()},indent=2))


if __name__=='__main__':main()

#!/usr/bin/env python3
"""Bind locally reviewed intent judgments to immutable saved predictions."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    p=argparse.ArgumentParser();p.add_argument('directory',type=Path)
    p.add_argument('--judgments',type=Path,required=True);p.add_argument('--reference',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    run=json.loads((a.directory/'run.json').read_text());ref=json.loads(a.reference.read_text())
    source=json.loads(a.judgments.read_text());by_number={v['cohortNumber']:v for v in ref['additionalTests']['cases']}
    assert run['status']=='complete_pending_review'
    assert set(source['models'])==set(run['models'])
    result={'rubric':source['rubric'],'judgmentSourceSHA256':hashlib.sha256(a.judgments.read_bytes()).hexdigest(),'models':{}}
    for model,grades in source['models'].items():
        assert len(grades)==len(by_number) and {g['cohortNumber'] for g in grades}==set(by_number)
        result['models'][model]=[]
        for grade in grades:
            c=by_number[grade['cohortNumber']];answers=run['models'][model]['capability']['scores'][c['exampleID']]
            assert len(grade['passBySeed'])==4 and [v['seed'] for v in answers]==[17,18,19,20]
            assert all(type(v) is bool for v in grade['passBySeed'])
            assert all(not passed or v.get('reasoningClosed') is not False for passed,v in zip(grade['passBySeed'],answers))
            result['models'][model].append({**grade,'exampleID':c['exampleID'],
                'predictionsSHA256':[hashlib.sha256(v['prediction'].encode()).hexdigest() for v in answers]})
    with a.output.open('x') as f:json.dump(result,f,indent=2,ensure_ascii=False);f.write('\n')


if __name__=='__main__':main()

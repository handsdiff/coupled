#!/usr/bin/env python3
"""No-network checks of holistic annotation joins and paired accounting."""
import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
import shutil

spec=importlib.util.spec_from_file_location('vision_analysis',Path(__file__).with_name('analyze-phase1-vision.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


def rejected(fn):
    try:fn()
    except (AssertionError,KeyError,ValueError):return
    raise AssertionError('Invalid annotations were accepted')


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--review',type=Path,required=True);p.add_argument('--scored',type=Path,required=True);a=p.parse_args()
    for name in ('implementer.tsv','reviewer.tsv'):assert len(m.judgments(a.review,a.review/name))==207
    with tempfile.TemporaryDirectory(prefix='coupled-vision-analysis-') as tmp:
        t=Path(tmp);text=(a.review/'implementer.tsv').read_text();lines=text.splitlines()
        for name,value in [('missing', '\n'.join(lines[:-1])),('duplicate',text+'\n'+lines[0]),('invalid',text.replace('\tpass\t','\tmaybe\t',1))]:
            path=t/name;path.write_text(value);rejected(lambda:m.judgments(a.review,path))
        pinned=t/'pinned';pinned.mkdir()
        shutil.copyfile(a.review/'blinded.jsonl',pinned/'blinded.jsonl')
        annotation=m.judgments(a.review,a.review/'implementer.tsv')[0]
        m.save_rows(pinned/'fixed-baseline-grades.jsonl',[annotation])
        assert len(m.judgments(pinned,a.review/'implementer.tsv'))==207
        flipped=[]
        for line in lines:
            if not line.strip() or line.startswith('#'):flipped.append(line);continue
            i,d,reason=line.split('\t',2)
            if int(i)==annotation['blindIndex']:d='fail' if d=='pass' else 'pass'
            flipped.append('\t'.join((i,d,reason)))
        changed=t/'regraded-baseline.tsv';changed.write_text('\n'.join(flipped))
        rejected(lambda:m.judgments(pinned,changed))
    r=list(m.rows(a.scored/'judgments.jsonl'));s=m.load(a.scored/'summary.json')
    assert len(r)==207 and len({(x['case'],x['variant']) for x in r})==207
    by={v:{x['case']:int(x['semanticPass']) for x in r if x['variant']==v} for v in m.VARIANTS}
    for v,b in by.items():assert sum(b.values())==s['arms'][v]['passes'] and len(b)==69
    for pair,group in s['paired'].items():
        before,after=pair.split('__');parts=[group[k] for k in ('gains','losses','bothPass','bothFail')]
        assert len([x for xs in parts for x in xs])==len({x for xs in parts for x in xs})==69
        assert len(group['gains'])-len(group['losses'])==sum(by[after].values())-sum(by[before].values())
        d=[by[after][c]-by[before][c] for c in sorted(by[before])]
        assert m.paired_uncertainty(d)==group['uncertainty']
    assert m.paired_uncertainty([0]*69)['netRateDifference']==0
    assert m.paired_uncertainty([1]*69)['pairedBootstrap95PercentInterval']==[1,1]
    print(json.dumps({'status':'passed','completeGrading':207,'pairedCases':69,'rejectsMissingDuplicateInvalidGrades':True,'rejectsBaselineRegrading':True,'deterministicPairedAnalysis':True,'networkCalls':0}))


if __name__=='__main__':main()

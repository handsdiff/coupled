#!/usr/bin/env python3
"""Prepare label-blinded review; validate annotations and summarize paired arms.

Judgments are explicit assistant adjudications, not an automated similarity
threshold or human ground truth. This script makes no provider calls.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
import statistics

from phase1_prediction_metrics import score_prediction, summarize_prediction_metrics

VERSION = "phase1-factorial-holistic-v2-full-cohort"
BAR = ("Binary pass only when the suggestion would be positively surprising and desirable to use because it "
       "substantially predicts the whole intended write. Topic overlap, restating visible context, one fragment "
       "of a larger thought, plausible adjacent actions, generic paste scaffolding, and substantial rewriting fail. "
       "Borderline cases fail. Different wording is acceptable when the intended semantic commitments survive.")


def read(path):
    return json.loads(path.read_text())


def rows(path):
    with path.open() as f:
        return [json.loads(line) for line in f if line.strip()]


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + '\n')


def jsonl(path, values):
    path.write_text(''.join(json.dumps(x, sort_keys=True, ensure_ascii=False) + '\n' for x in values))


def target_rows(source):
    cohort = sorted(rows(source/'frozen/cohort.jsonl'), key=lambda x:(x['targetBeganAt'], x['exampleID']))
    targets=[]
    for i, example in enumerate(cohort, 1):
        query=json.loads(example['query'])
        targets.append({'case':i,'exampleID':example['exampleID'],'originalTargetNumber':example['originalTargetNumber'],
                        'target':example['targetText'],'destination':query['destination'],'query':query})
    return cohort,targets


def prepare_targets(source, output, prior):
    assert not output.exists(), 'Fresh target review required'
    cohort,targets=target_rows(source)
    by_id={r['exampleID']:r for r in targets}
    prior_plan=read(prior/'review-plan.json')
    old_cohort=sorted(rows(Path(prior_plan['source'])/'frozen/cohort.jsonl'),key=lambda x:(x['targetBeganAt'],x['exampleID']))
    old_classes=rows(prior/'substantiveness.jsonl');old_grades=rows(prior/'judgments.jsonl')
    assert len(old_cohort)==len(old_classes)==len(old_grades)==prior_plan['cases']
    mapping={i:by_id[e['exampleID']]['case'] for i,e in enumerate(old_cohort,1)}
    old_targets={r['case']:r for r in rows(prior/'targets.jsonl')}
    for i,e in enumerate(old_cohort,1):
        current=by_id[e['exampleID']];old=old_targets[i]
        assert current['target']==old['target'] and current['query']==old['query']
    imported_classes=[{**r,'case':mapping[r['case']],'priorCase':r['case']} for r in old_classes]
    imported_grades=[{**r,'case':mapping[r['case']],'priorCase':r['case']} for r in old_grades]
    output.mkdir(parents=True)
    jsonl(output/'targets.jsonl',targets)
    jsonl(output/'substantiveness.imported.jsonl',imported_classes)
    jsonl(output/'judgments.imported.jsonl',imported_grades)
    imported_ids=set(mapping.values())
    jsonl(output/'remaining-targets.jsonl',[r for r in targets if r['case'] not in imported_ids])
    sources=[source/'frozen/cohort.jsonl',source/'frozen/plan.json',prior/'review-plan.json',
             prior/'targets.jsonl',prior/'substantiveness.jsonl',prior/'judgments.jsonl',prior/'blinded.jsonl']
    dump(output/'target-review-plan.json',{'version':VERSION,'source':str(source.resolve()),'priorReview':str(prior.resolve()),
        'passBar':BAR,'cases':len(cohort),'importedCases':len(mapping),'newCases':len(cohort)-len(mapping),
        'sourceSHA256':{str(p.resolve()):sha(p) for p in sources},
        'targetArtifactsSHA256':{n:sha(output/n) for n in ('targets.jsonl','substantiveness.imported.jsonl','judgments.imported.jsonl','remaining-targets.jsonl')},
        'policy':'Preserve all prior labels/reasons exactly. Classify new targets before prediction review. No outcome-driven denominator changes.',
        'providerCalls':0})
    print(json.dumps({'targets':len(cohort),'priorLabelsPreserved':len(mapping),'newTargetClassificationsNeeded':len(cohort)-len(mapping)}))


def lock_classifications(folder):
    plan=read(folder/'target-review-plan.json')
    assert not (folder/'classification-lock.json').exists(), 'Classifications already locked'
    assert not (folder/'blinded.jsonl').exists(), 'Lock target eligibility before prediction review'
    for path,digest in plan['sourceSHA256'].items():assert sha(Path(path))==digest
    for name,digest in plan['targetArtifactsSHA256'].items():assert sha(folder/name)==digest
    imported=rows(folder/'substantiveness.imported.jsonl')
    added=rows(folder/'substantiveness.new.jsonl')
    combined=sorted(imported+added,key=lambda x:x['case'])
    assert len(combined)==plan['cases']
    assert {r['case'] for r in combined}==set(range(1,plan['cases']+1))
    for r in combined:
        assert type(r['substantive']) is bool and isinstance(r['reason'],str) and r['reason'].strip()
    assert not (folder/'substantiveness.jsonl').exists()
    jsonl(folder/'substantiveness.jsonl',combined)
    dump(folder/'classification-lock.json',{'version':VERSION,'lockedAt':datetime.now(timezone.utc).isoformat(),
        'cases':len(combined),'substantive':sum(r['substantive'] for r in combined),
        'policy':'Target-only classifications fixed before new blinded prediction review; prior classifications preserved.',
        'sourceSHA256':{str((folder/n).resolve()):sha(folder/n) for n in (
            'target-review-plan.json','targets.jsonl','substantiveness.imported.jsonl',
            'substantiveness.new.jsonl','substantiveness.jsonl')}})
    print(json.dumps({'locked':len(combined),'substantive':sum(r['substantive'] for r in combined)}))


def verify_classification_lock(folder):
    lock=read(folder/'classification-lock.json')
    for path,digest in lock['sourceSHA256'].items():assert sha(Path(path))==digest


def prepare(source, output):
    cohort,targets=target_rows(source)
    staged=output.exists()
    if staged:
        staged_plan=read(output/'target-review-plan.json')
        assert staged_plan['source']==str(source.resolve())
        for path,digest in staged_plan['sourceSHA256'].items():assert sha(Path(path))==digest
        for name,digest in staged_plan['targetArtifactsSHA256'].items():assert sha(output/name)==digest
        assert rows(output/'targets.jsonl')==targets
        assert not (output/'review-plan.json').exists(), 'Blinded review already exists'
        verify_classification_lock(output)
    prediction_file = source/'execution-v1-final/final-output-v2/predictions.jsonl'
    projection = read(prediction_file.parent/'projection.json')
    assert sha(prediction_file) == projection['predictionsSHA256'] and projection['recorded'] == 4*len(cohort)
    for path,digest in projection['sourceSHA256'].items():assert sha(Path(path))==digest
    predictions = rows(prediction_file)
    assert len(predictions)==projection['recorded']
    assert len({r['requestOrdinal'] for r in predictions})==len(predictions)
    by_example = defaultdict(list)
    for row in predictions:
        by_example[row['exampleID']].append(row)
    blinded, key = [], []
    for target, example in zip(targets,cohort):
        i=target['case']
        candidates = sorted(by_example[example['exampleID']], key=lambda r:hashlib.sha256(
            ('blind-v1-17:'+example['exampleID']+':'+r['model']+':'+r['variant']).encode()).digest())
        assert len(candidates) == 4 and len({(r['model'],r['variant']) for r in candidates}) == 4
        candidate_list=[]
        for label, row in zip('ABCD', candidates):
            candidate_list.append({'label':label,'prediction':row['prediction'],'validCompletion':row['validCompletion']})
            key.append({'case':i,'label':label,'exampleID':example['exampleID'], 'requestOrdinal':row['requestOrdinal'],
                        'model':row['model'],'variant':row['variant']})
        blinded.append({**target,'candidates':candidate_list})
    if not staged:
        output.mkdir(parents=True)
        jsonl(output/'targets.jsonl',targets)
    else:
        old={r['case']:r for r in rows(Path(staged_plan['priorReview'])/'blinded.jsonl')}
        for imported in rows(output/'judgments.imported.jsonl'):
            assert blinded[imported['case']-1]['candidates']==old[imported['priorCase']]['candidates'], 'Prior blinded labels changed'
    jsonl(output/'blinded.jsonl',blinded)
    jsonl(output/'blinding-key.jsonl',key)
    dump(output/'review-plan.json',{'version':VERSION,'passBar':BAR,
        'substantiveness':'At least one user-specific claim, decision, rationale, question or specific instruction; not generic workflow/status control. Determined from target/query before viewing predictions.',
        'authority':'assistant_adjudication_provisional_subject_to_user_review',
        'blinding':'Per-case randomized A-D; model, pipeline, latency and cost hidden during scoring. The four pilot outputs were seen before this review; not an independent fully blinded human evaluation.',
        'cases':len(cohort),'predictions':len(predictions),'semanticScoreIndependentOfLatency':True,
        'priorReview':staged_plan['priorReview'] if staged else None,
        'priorLabelPolicy':'Already reviewed examples retain exact prior judgments; only added examples are newly blinded.' if staged else None,
        'targetReviewPlanSHA256':sha(output/'target-review-plan.json') if staged else None,
        'classificationLockSHA256':sha(output/'classification-lock.json') if staged else None,
        'source':str(source.resolve()),'sourceSHA256':{str(p.resolve()):sha(p) for p in (
            source/'frozen/plan.json', source/'frozen/cohort.jsonl',prediction_file,prediction_file.parent/'projection.json')},
        'blindArtifactSHA256':{n:sha(output/n) for n in ('targets.jsonl','blinded.jsonl','blinding-key.jsonl')},
        'primary':'binary strict semantic usefulness on model-independent substantive denominator',
        'realTimeScores':'not computed in this semantic review; require separately bound timing artifact',
        'providerCalls':0})
    print(json.dumps({'preparedCases':len(cohort),'blindedPredictions':len(predictions)}))


def paired_interval(values):
    rng = random.Random(17)
    n=len(values)
    draws=sorted(sum(values[rng.randrange(n)] for _ in range(n))/n for _ in range(10000))
    return [draws[249],draws[9749]]


def finish(folder):
    plan=read(folder/'review-plan.json')
    for path,digest in plan['sourceSHA256'].items():assert sha(Path(path))==digest
    for name,digest in plan['blindArtifactSHA256'].items():assert sha(folder/name)==digest
    classification_rows=rows(folder/'substantiveness.jsonl')
    grade_rows=rows(folder/'judgments.jsonl')
    assert len(classification_rows)==len(grade_rows)==plan['cases'], 'Duplicate or missing annotation rows'
    classifications={r['case']:r for r in classification_rows}
    grades={r['case']:r for r in grade_rows}
    if plan.get('targetReviewPlanSHA256'):
        assert sha(folder/'classification-lock.json')==plan['classificationLockSHA256']
        verify_classification_lock(folder)
        assert sha(folder/'target-review-plan.json')==plan['targetReviewPlanSHA256']
        target_plan=read(folder/'target-review-plan.json')
        for name,digest in target_plan['targetArtifactsSHA256'].items():assert sha(folder/name)==digest
        for old in rows(folder/'substantiveness.imported.jsonl'):
            assert all(classifications[old['case']][k]==old[k] for k in ('substantive','reason'))
        for old in rows(folder/'judgments.imported.jsonl'):
            assert grades[old['case']]['judgments']==old['judgments'], 'Prior judgment changed'
    blind={r['case']:r for r in rows(folder/'blinded.jsonl')}
    assert set(classifications)==set(grades)==set(blind)==set(range(1,plan['cases']+1))
    for i in blind:
        assert type(classifications[i]['substantive']) is bool and classifications[i]['reason']
        assert set(grades[i]['judgments'])==set('ABCD')
        for grade in grades[i]['judgments'].values():
            assert grade['decision'] in ('pass','fail','excluded_non_substantive') and grade['reason']
            assert (grade['decision']=='excluded_non_substantive') == (not classifications[i]['substantive'])
    source=Path(plan['source'])
    preds={r['requestOrdinal']:r for r in rows(source/'execution-v1-final/final-output-v2/predictions.jsonl')}
    details=[]; metrics=defaultdict(list); all_metrics=defaultdict(list); arms=defaultdict(list)
    for mapping in rows(folder/'blinding-key.jsonl'):
        i,label=mapping['case'],mapping['label']
        row=preds[mapping['requestOrdinal']]
        candidate=next(c for c in blind[i]['candidates'] if c['label']==label)
        assert row['prediction']==candidate['prediction']
        grade=grades[i]['judgments'][label]
        assert row['validCompletion'] or grade['decision']!='pass'
        arm=mapping['model']+' / '+mapping['variant']
        target=blind[i]['target']
        metric=score_prediction(target,row['prediction'],target_paste_actions=target.count('<|paste|>'))
        all_metrics[arm].append(metric)
        eligible=classifications[i]['substantive']
        if eligible:metrics[arm].append(metric)
        detail={**mapping, 'target':target,'prediction':row['prediction'],'substantive':eligible,
            'substantivenessReason':classifications[i]['reason'], **grade, 'semanticPass':grade['decision']=='pass',
            'generationLatencySeconds':row['timing']['dispatchToCompletionSeconds'],
            'apiEquivalentCostUSD':row['apiEquivalentCostUSD'], 'deterministicMetrics':metric}
        details.append(detail);arms[arm].append(detail)
    n=sum(c['substantive'] for c in classifications.values())
    summary={}
    for arm, data in sorted(arms.items()):
        eligible=[r for r in data if r['substantive']]
        latencies=[r['generationLatencySeconds'] for r in data]
        summary[arm]={'semanticPasses':sum(r['semanticPass'] for r in eligible),'substantiveExamples':n,
            'semanticPassRate':sum(r['semanticPass'] for r in eligible)/n,
            'passCases':[r['case'] for r in eligible if r['semanticPass']],
            'deterministicSubstantive':summarize_prediction_metrics(metrics[arm]),
            'deterministicAllExamples':summarize_prediction_metrics(all_metrics[arm]),
            'operationalMeasurementExamples':len(data),
            'medianLatencySecondsAllExamples':statistics.median(latencies),'meanLatencySecondsAllExamples':statistics.mean(latencies),
            'apiEquivalentCostUSDAllExamples':sum(r['apiEquivalentCostUSD'] or 0 for r in data),
            'costRowsWithoutExactEstimate':sum(r['apiEquivalentCostUSD'] is None for r in data)}
    matched={arm:{r['case']:int(r['semanticPass']) for r in data if r['substantive']} for arm,data in arms.items()}
    paired={}
    for model in ('chatgpt/gpt-5.6-sol','chatgpt/gpt-6-astra'):
        old,new=matched[model+' / old'],matched[model+' / new']
        differences=[new[i]-old[i] for i in sorted(old)]
        paired[model]={'oldOnlyPassCases':[i for i in sorted(old) if old[i] and not new[i]],
            'newOnlyPassCases':[i for i in sorted(old) if new[i] and not old[i]],
            'newMinusOldRate':sum(differences)/n,'pairedBootstrap95PercentInterval':paired_interval(differences)}
    output=folder/'scored'
    assert not output.exists(), 'Fresh result output required'
    output.mkdir()
    jsonl(output/'judgments.unblinded.jsonl',details)
    report={'version':plan['version'],'authority':plan['authority'],'passBar':plan['passBar'],'cases':len(blind),'predictions':len(details),
        'substantiveExamples':n,'excludedExamples':len(blind)-n,
        'excludedCases':[i for i in sorted(classifications) if not classifications[i]['substantive']],
        'models':summary,'pairedPipelineComparisons':paired,
        'intervalCaveat':'Paired example bootstrap is descriptive; temporally correlated work and subjective labels limit inference.',
        'sourceSHA256':plan['sourceSHA256'],
        'annotationSHA256':{name:sha(folder/name) for name in ('review-plan.json','substantiveness.jsonl','judgments.jsonl')},
        'scoringImplementationSHA256':sha(Path(__file__)),'providerCalls':0,'realTimeScoresComputed':False}
    dump(output/'summary.json',report)
    print(json.dumps({'substantive':n,'excluded':len(blind)-n,'semanticPasses':{k:v['semanticPasses'] for k,v in summary.items()}},indent=2))


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('action',choices=['targets','lock-classifications','prepare','finish'])
    ap.add_argument('--source',type=Path)
    ap.add_argument('--prior-review',type=Path)
    ap.add_argument('--output',type=Path,required=True)
    args=ap.parse_args()
    if args.action=='targets':prepare_targets(args.source,args.output,args.prior_review)
    elif args.action=='lock-classifications':lock_classifications(args.output)
    elif args.action=='prepare':prepare(args.source,args.output)
    else:finish(args.output)

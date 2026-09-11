#!/usr/bin/env python3
"""Local 32K diagnostic: count actual surviving defect witnesses, not just IDs.

No packed training rows, model calls, or source mutations. Exposures are lower
bounds from the reviewed witnesses, not an exhaustive human error-rate audit.
"""
import argparse
from array import array
from collections import Counter, defaultdict
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import sys
import unicodedata

os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
os.environ['TOKENIZERS_PARALLELISM'] = 'false'
from transformers import AutoTokenizer
from phase1_jsonl import JSONLSequence
from phase1_read_model_comparison import Privacy

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'coupled-data'
PREP = DATA / 'sep02-10-training-prep-20260910'
CURRENT = DATA / 'clean-read-closed-write-review-20260911-r2'

# Each phrase is a previously screenshot-verified READ defect, not merely an
# unexplained novelty token. Limit it to its reviewed source event. A phrase
# removed by adjacent rendering or oldest-event truncation is not counted.
WITNESSES = {
    240: ('viewport_edge', ['direntian ranair', 'flat nunthaniza nunral idann']),
    445: ('ocr_recognition_layout', ['identity.e panes.']),
    498: ('viewport_edge_and_ocr', ['rasenning aur', 'learnina', 'RHF']),
    505: ('viewport_edge_and_ocr', ['9n manninn', 'RHF']),
    660: ('pane_selection', ['r explicit', 'namic', 'mplete']),
    685: ('ocr_recognition_layout', ['Store the ser fishes compose oss']),
    687: ('visible_overlay', ['trainin Ji']),
    931: ('ocr_recognition_layout', ['havine', 'soviversinnient']),
    1052: ('pane_selection', ['nages without OCR', 'ontent', 'nmediate']),
    1068: ('pane_selection', ['ation these notes', 'ohasis']),
    1079: ('pane_selection', ['onversation', 'ottleneck']),
}
FOOTERS = ['gpt-5.6-sol xhigh fast • ~/coupled', 'Do anything', '+ © Approve for me']
RECOVERED = ['import json', "emphasis on data collection since 'algorithms' get smarter and cheaper by default"]


def rows(p):
    with p.open() as f:
        for line in f:
            if line.strip(): yield json.loads(line)


def read(p): return json.loads(p.read_text())


def save(p, v):
    with p.open('x') as f:
        json.dump(v, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write('\n')


def norm(text): return ' '.join(unicodedata.normalize('NFKC', text).casefold().split())


def contains_phrase(phrase, text):
    # "mplete" must not match a correctly retained "complete".
    needle = norm(phrase)
    return bool(re.search(r'(?<!\w)' + re.escape(needle) + r'(?!\w)', norm(text)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    spec = importlib.util.spec_from_file_location('diagnostic_packer', ROOT/'scripts/pack-phase1-dataset.py')
    packer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(packer)
    settings = read(PREP/'paired-qwen38-reference32k/packing-audit.json')
    tokenizer = AutoTokenizer.from_pretrained(settings['contextBudget']['referenceTokenizerDirectory'],
        local_files_only=True, trust_remote_code=False, split_special_tokens=True)
    audit = read(args.audit/'summary.json')
    boundary = read(args.audit/'boundaries.json')
    defects = defaultdict(list)
    witnesses = []
    for b in boundary:
        if b['candidateLine'] not in WITNESSES: continue
        stage, phrases = WITNESSES[b['candidateLine']]
        for r in b['currentReads']:
            found = [phrase for phrase in phrases if contains_phrase(phrase, r['content'])]
            if not found: continue
            w = {'boundary': b['candidateLine'], 'reviewCases': b['cases'], 'stage': stage,
                 'eventID': r['eventID'], 'sourceRecordIDs': r['sourceRecordIDs'], 'phrases': found}
            witnesses.append(w)
            defects[r['eventID']].append(w)
    # Directly inspected screenshot: rg's flags belong before the next sed
    # command. Native ordering moves sed between rg and its flags. This is a
    # real new ordering defect, not merely a novelty-matcher disagreement.
    introduced = {'boundary':None,'reviewCases':[],'stage':'native_order_regression',
        'eventID':'evt_73900df9ac3f11dd760f6d5cc45d5f1ff57b8ab282146eadbe46009f15ac4452',
        'sourceRecordIDs':['BE0DAC18-E692-40E3-B9D1-711A8A06A6CB'],
        'phrases':['• Ran rg\nsed\n-files'],
        'screenshot':'coupled-data/phase1-ordinary-work-2026-09-07-1/screenshots/visual-67D79D17-9323-4252-A66E-9977028C60D0.png'}
    witnesses.append(introduced)
    defects[introduced['eventID']].append(introduced)
    save(args.output/'verified-read-witnesses.json', witnesses)
    restored = read(args.audit/'restored-lines.json')
    # Follow only lines that were actually reinstated by the deleted blacklist.
    footer_sources = {r['text']: {x['eventID'] for x in r['examples']} for r in restored if r['text'] in FOOTERS}
    # The first diagnostic records only four examples per string. Rebuild the
    # full source map from reduced evidence for exact packed exposure counts.
    footer_sources = defaultdict(set)
    for day in [2,3,4,7]:
        old_dir = PREP/'new-sep7-reduced' if day==7 else DATA/f'sep02-04-semantic-v25-review-20260907-r4/sep{day}-reduced'
        for r in rows(old_dir/'events.jsonl'):
            for line in r.get('reduction',{}).get('semanticReadContent',{}).get('removedLines',[]):
                if line['reason']=='stable_session_interface_text' and line.get('text') in FOOTERS:
                    footer_sources[line['text']].add(r['eventID'])
    final_events = {r['sourceEventID']:r for r in rows(CURRENT/'events.jsonl')}
    baseline_events = {r['sourceEventID']:r for r in rows(PREP/'episodes/events.jsonl')}
    policy = read(CURRENT/'review.json')['privacyPolicy']
    baseline_events = {r['sourceEventID']:r for r in Privacy(baseline_events,policy).filter_stream(list(baseline_events.values()))}
    gap_blocks = list(rows(PREP/'episodes/gaps.jsonl'))
    gaps = {r['contextBlockID']:r for r in gap_blocks}
    all_blocks = {'before':{**baseline_events, **gaps}, 'after':{**final_events, **gaps}}
    compared_keys = ['query','target','targetMask','targetBeganAt','targetAvailableAt','targetText']
    cohort = {r['exampleID']: (n,{k:r[k] for k in compared_keys})
              for n,r in enumerate(rows(PREP/'paired-qwen38-reference32k/cohort.jsonl'),1)}
    caches = {'before':{},'after':{}}
    by_case = {}
    exemplar_contexts = []
    for variant, directory in [('before',PREP/'episodes'),('after',CURRENT)]:
        if variant=='after':caches['before'].clear()
        for example in JSONLSequence(directory/'examples.jsonl'):
            if example['exampleID'] not in cohort: continue
            ordinal, frozen = cohort[example['exampleID']]
            for key in ['query','target','targetMask','targetBeganAt','targetAvailableAt']:
                assert example[key]==frozen[key]
            source = all_blocks[variant]
            # Match the frozen privacy projection; gaps retain their original
            # IDs. We are measuring regenerated 32K inputs, not reusing IDs as
            # a proxy for whether text survives dependency-aware rendering.
            ids = example.get('contextBlockIDs',example['contextEventIDs'])
            context = '\n'.join(source[eid]['serialized'] for eid in ids)
            e = dict(example,context=context,modelInput=context+'\n'+example['query'] if context else example['query'])
            packed = packer.pack_model_input(e,source,tokenizer,32768,settings['taskInstruction'],caches[variant],True)
            # Millions of cached IDs as Python ints are needlessly expensive.
            # Arrays preserve exact IDs while bounding this read-only audit.
            for key,(text,ids_cached) in caches[variant].items():
                if isinstance(ids_cached,list):caches[variant][key]=(text,array('I',ids_cached))
            assert len(packed['inputIDs'])<=32768
            found, footer_hits, recovery_hits = [],[],[]
            for span in packed['contextEventSpans']:
                eid=span['eventID']
                serialized=span.get('packedSerialized',source[eid]['serialized'])
                payload=json.loads(serialized).get('content','')
                for witness in defects.get(eid,[]):
                    actual=[p for p in witness['phrases'] if contains_phrase(p, payload)]
                    if actual:found.append(dict(witness,phrases=actual))
                for phrase in FOOTERS:
                    if eid in footer_sources[phrase] and phrase in payload:
                        footer_hits.append({'eventID':eid,'phrase':phrase})
                for phrase in RECOVERED:
                    if phrase in payload:recovery_hits.append({'eventID':eid,'phrase':phrase})
            result={'inputTokens':len(packed['inputIDs']),'retainedEvents':len(packed['contextEventSpans']),
                    'verifiedReadDefects':found,'restoredUIFooters':footer_hits,'substantiveWitnesses':recovery_hits,
                    'readRenderingCounts':packed['readRenderingCounts']}
            by_case.setdefault(ordinal,{'case':ordinal,'exampleID':example['exampleID'],'knownTargetOrGroupingIssue':ordinal in audit['knownUnrepairedCases']})[variant]=result
            if variant=='after' and ordinal in [27,128,139,239,245,262,263,268,336,353,369,380,381,499,501,523,544,574,580,584,587,589,628,631,644]:
                exemplar_contexts.append({'case':ordinal,'exampleID':example['exampleID'],
                    'blocks':[{'eventID':s['eventID'],'serialized':s.get('packedSerialized',source[s['eventID']]['serialized'])} for s in packed['contextEventSpans']],
                    'query':example['query'],'target':frozen['targetText']})
            if ordinal%100==0:print(variant,ordinal,flush=True)
            if resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/(1024**2 if sys.platform=='darwin' else 1024)>1800:
                raise MemoryError('Diagnostic exceeds 1.8 GiB')
    assert len(by_case)==687 and all('before' in r and 'after' in r for r in by_case.values())
    save(args.output/'cases.json',[by_case[n] for n in sorted(by_case)])
    save(args.output/'selected-actual-contexts.json',exemplar_contexts)
    summary={}
    for variant in ['before','after']:
        values=[r for r in by_case.values() if r[variant]['verifiedReadDefects']]
        affected=[r['case'] for r in values]
        by_stage=defaultdict(set)
        for r in values:
            for w in r[variant]['verifiedReadDefects']:by_stage[w['stage']].add(r['case'])
        summary[variant]={'verifiedReadDefectExposureCount':len(values),'cases':affected,
            'percent':len(values)/687*100,'byEarliestStage':{k:sorted(v) for k,v in by_stage.items()},
            'restoredUIFooterExposureCases':[r['case'] for r in by_case.values() if r[variant]['restoredUIFooters']],
            'substantiveWitnessExposureCases':[r['case'] for r in by_case.values() if r[variant]['substantiveWitnesses']],
            'totalInputTokens':sum(r[variant]['inputTokens'] for r in by_case.values())}
    summary.update(scope='Known-witness lower bound in regenerated dependency-aware 32K context, not an exhaustive error rate',
        referenceTokenizer=settings['contextBudget']['referenceTokenizerDirectory'],
        packingVersion=packer.PACKER_VERSION,
        explicitDependencyAwareReadRendering=True,
        collectorChanged=False,providerCalls=0,
        modelFacingRegressionConclusion='See human review; reinstated control text is noise, not proof of target corruption',
        peakRSSMiB=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/(1024**2 if sys.platform=='darwin' else 1024))
    save(args.output/'summary.json',summary)
    print(json.dumps({k:{kk:vv for kk,vv in v.items() if not isinstance(vv,(list,dict))} if isinstance(v,dict) else v for k,v in summary.items()},indent=2))


if __name__=='__main__':main()

#!/usr/bin/env python3
"""Repack frozen targets against post-pane READs and recount known defects.

Local diagnostic only: no provider calls, target edits, episode merges, or new
training artifacts. Keep the witness inventory fixed across the comparison.
"""
import argparse
from array import array
from collections import defaultdict
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import sys

os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
os.environ['TOKENIZERS_PARALLELISM'] = 'false'
os.environ['USE_TORCH'] = '0'
os.environ['USE_TF'] = '0'
os.environ['USE_FLAX'] = '0'
from transformers import AutoTokenizer
from phase1_read_model_comparison import Privacy

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT/'coupled-data'
PREP = DATA/'sep02-10-training-prep-20260910'
BEFORE = DATA/'clean-read-closed-write-review-20260911-r2'
PANE = DATA/'pane-selection-v6-20260911-r2'
PRIOR_AUDIT = DATA/'context-fidelity-audit-20260911-r2/packed-audit-classified'


def rows(path):
    with path.open() as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def read(path):
    return json.loads(path.read_text())


def save(path, value):
    with path.open('x') as f:
        json.dump(value, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write('\n')


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT/'scripts'/name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def budget_suffix(ids, source, query, instruction, tokenizer, packer, cache):
    """Exact suffix plus first overflowing block; not a heuristic pre-crop.

    The production packer selects its suffix BEFORE dependency rendering and
    never backfills. Earlier blocks therefore cannot affect its final tokens.
    Retaining the complete overflowing block preserves boundary truncation.
    """
    remaining = 32768 - len(packer.encode_plain_text(tokenizer, instruction+'\n')) - len(packer.encode_plain_text(tokenizer, query))
    selected = []
    for eid in reversed(ids):
        text = source[eid]['serialized']
        if eid not in cache:
            cache[eid] = (text, array('I', packer.encode_plain_text(tokenizer, text+'\n')))
        assert cache[eid][0] == text
        selected.append(eid)
        remaining -= len(cache[eid][1])
        if remaining < 0:
            break
    return list(reversed(selected))


def reconstruct_reads(cohort):
    originals = {r['sourceEventID']: r for r in rows(PREP/'episodes/events.jsonl')}
    suppressed = set()
    for r in rows(PREP/'micro-corpus/events.jsonl'):
        if r['kind'] == 'read' and r['sourceEventID'] not in originals:
            suppressed.update(r['sourceRecordIDs'])
    events = [r for r in originals.values() if r['kind'] == 'write']
    allowed = {r['eventID']: r for r in read(PANE/'boundary-audit-r2/selection-changes.json')}
    observed, source_changes = [], []
    sources = {}
    for day in (2, 3, 4, 7):
        path = PANE/f'replay/sep{day}-causal/events.jsonl'
        manifest = read(path.with_name('dataset.json'))
        reduced = PANE/f'replay/sep{day}-phase1-semantic-v26'
        assert manifest['source']['digestsSHA256']['events.jsonl'] == sha(reduced/'events.jsonl')
        sources[str(path.resolve())] = sha(path)
        for r in rows(path):
            if r['kind'] != 'read':
                continue
            overlap = suppressed.intersection(r['sourceRecordIDs'])
            if overlap:
                assert overlap == set(r['sourceRecordIDs']), 'Mixed episode suppression lineage'
                continue
            old = originals.get(r['sourceEventID'])
            if old:
                assert old['sourceRecordIDs'] == r['sourceRecordIDs']
                old_source, new_source = old.get('modelFacingReadSource'), r.get('modelFacingReadSource')
                if old_source != new_source:
                    # A wider AX ancestor can change the surface kind/label.
                    # Do not freeze stale pane metadata into repaired content.
                    assert old_source['application'] == new_source['application']
                    source_changes.append({'eventID': r['sourceEventID'], 'before': old_source, 'after': new_source})
                if old['availableAt'] != r['availableAt']:
                    proof = allowed[r['sourceEventID']]
                    assert proof['beforeCapturedAt'] == old['availableAt']
                    assert proof['afterCapturedAt'] == r['availableAt']
                    assert all((old['availableAt'] < e['targetBeganAt']) ==
                               (r['availableAt'] < e['targetBeganAt']) for e in cohort)
                    observed.append(proof)
            events.append(r)
    assert {r['eventID'] for r in observed} == set(allowed)
    events.sort(key=lambda e: (e['availableAt'], e.get('beganAt') or e['availableAt'], e['sourceEventID']))
    policy = read(BEFORE/'review.json')['privacyPolicy']
    events = Privacy({e['sourceEventID']: e for e in events}, policy).filter_stream(events)
    return events, sources, observed, source_changes


def merge_audits(output, directories, followup):
    """Combine disjoint bounded workers; bind the additional residual check."""
    merged, manifests, hashes = {}, [], {}
    for directory in directories:
        manifest = read(directory/'summary.json')
        manifests.append(manifest)
        for name in ('summary.json', 'cases.json', 'verified-read-witnesses.json'):
            hashes[str((directory/name).resolve())] = sha(directory/name)
        for r in read(directory/'cases.json'):
            assert r['case'] not in merged
            merged[r['case']] = r
    assert set(merged) == set(range(1, 688)), 'Incomplete or overlapping corpus audit'
    for manifest in manifests[1:]:
        for key in ('sourceEventsSHA256', 'sourceCohortSHA256', 'sourceWitnessesSHA256', 'packerSHA256'):
            assert manifest[key] == manifests[0][key], ('Mixed inputs/code', key)
    for r in read(followup/'cases.json'):
        old = merged[r['case']]
        assert all(old[key] == r[key] for key in ('exampleID', 'before', 'after'))
        for variant in ('before', 'after'):
            old[variant+'FollowupDefects'] = r[variant+'FollowupDefects']
    for name in ('cases.json', 'summary.json', 'followup-witnesses.json'):
        hashes[str((followup/name).resolve())] = sha(followup/name)
    summary = {}
    for variant in ('before', 'after'):
        fixed, all_cases, stages = set(), set(), defaultdict(set)
        for n, r in merged.items():
            if r[variant]['verifiedReadDefects']:
                fixed.add(n)
            defects = r[variant]['verifiedReadDefects'] + r.get(variant+'FollowupDefects', [])
            if defects:
                all_cases.add(n)
            for w in defects:
                stages[w['stage']].add(n)
        summary[variant] = {'originalWitnessCount': len(fixed), 'originalWitnessCases': sorted(fixed),
            'verifiedRemainingDefectCount': len(all_cases), 'percent': len(all_cases)/687*100,
            'cases': sorted(all_cases), 'byStage': {k: sorted(v) for k, v in stages.items()}}
    assert summary['before']['originalWitnessCount'] == 83
    summary.update(version='post-pane-packed-context-review-v1', examples=687,
        noLongerFlagged=sorted(set(summary['before']['cases'])-set(summary['after']['cases'])),
        newlyFlagged=sorted(set(summary['after']['cases'])-set(summary['before']['cases'])),
        scope='Known screenshot-verified defects after actual dependency-aware 32K packing; lower bound, not exhaustive error rate',
        allQueriesTargetsMasksHistoricalWritesUnchanged=True, astraCorrectionsApplied=False,
        providerCalls=0, peakCompletedWorkerRSSMiB=max(m['peakRSSMiB'] for m in manifests),
        baselineTokenCountChanges=[r for m in manifests for r in m.get('baselineTokenCountChanges', [])],
        sourceHashes=hashes, sourceEventsSHA256=manifests[0]['sourceEventsSHA256'],
        sourceCohortSHA256=manifests[0]['sourceCohortSHA256'],
        sourceWitnessesSHA256=manifests[0]['sourceWitnessesSHA256'],
        scriptSHA256=sha(Path(__file__)))
    output.mkdir(parents=True, exist_ok=False)
    save(output/'summary.json', summary)
    save(output/'cases.json', [merged[n] for n in sorted(merged)])
    save(output/'followup-witnesses.json', read(followup/'followup-witnesses.json'))
    print(json.dumps({v: {k: x for k, x in summary[v].items() if not isinstance(x, (list, dict))}
                      for v in ('before', 'after')}, indent=2))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--start-case', type=int, default=1)
    p.add_argument('--end-case', type=int, default=687)
    p.add_argument('--merge-from', type=Path, nargs='+')
    p.add_argument('--followup', type=Path)
    a = p.parse_args()
    if a.merge_from:
        assert a.followup
        merge_audits(a.output, a.merge_from, a.followup)
        return
    a.output.mkdir(parents=True, exist_ok=False)
    packer = load_script('pack-phase1-dataset.py')
    audit = load_script('pack-phase1-fidelity-audit.py')
    settings = read(PREP/'paired-qwen38-reference32k/packing-audit.json')
    tokenizer = AutoTokenizer.from_pretrained(settings['contextBudget']['referenceTokenizerDirectory'],
        local_files_only=True, trust_remote_code=False, split_special_tokens=True)
    # Retain only invariant target metadata, never 687 expanded histories.
    keys = ('exampleID', 'query', 'target', 'targetMask', 'targetBeganAt',
            'targetAvailableAt', 'targetText', 'targetEventID')
    cohort = [{k: r[k] for k in keys} for r in rows(PREP/'paired-qwen38-reference32k/cohort.jsonl')]
    assert len(cohort) == 687
    new, source_hashes, selections, source_changes = reconstruct_reads(cohort)
    old = list(rows(BEFORE/'events.jsonl'))
    assert {e['sourceEventID']: e for e in old if e['kind'] == 'write'} == {
        e['sourceEventID']: e for e in new if e['kind'] == 'write'}
    gaps = {r['contextBlockID']: r for r in rows(PREP/'episodes/gaps.jsonl')}
    witnesses = read(PRIOR_AUDIT/'verified-read-witnesses.json')
    # Re-inspected original screenshots: do not declare a repaired pane clean
    # merely because one old probe vanished while another damaged prefix stays.
    followup_witnesses = [
        {'eventID': 'evt_8f94cdc3c953391f9f72d8b742795426bac7ade9cd87ea518bc6e6921883fc31',
         'stage': 'pane_selection', 'phrases': ['›ther interruptions'], 'reviewCases': [574, 575],
         'screenshot': 'coupled-data/phase1-ordinary-work-2026-09-07-1/screenshots/B873BD7C-8177-465E-8AE0-5447572ED672.png',
         'expected': 'Other interruptions', 'reason': 'Unrecovered damaged prefix in the same originally clipped pane'},
        {'eventID': 'evt_3dbb8149c92e39111856e2e0486c48f843f907c75211bf5f1a7cfce1148b8e12',
         'stage': 'pane_selection', 'phrases': ['*a>> sol'], 'reviewCases': [584],
         'screenshot': 'coupled-data/phase1-ordinary-work-2026-09-07-1/screenshots/visual-187A9F40-31E4-49E9-A32B-A050BDFB894E.png',
         'expected': 'astra >> sol', 'reason': 'Unrecovered damaged prefix in the same originally clipped pane'},
    ]
    save(a.output/'followup-witnesses.json', followup_witnesses)
    defects = defaultdict(list)
    for w in witnesses:
        defects[w['eventID']].append(w)
    save(a.output/'verified-read-witnesses.json', witnesses)
    prior_cases = {r['case']: r for r in read(PRIOR_AUDIT/'cases.json')}
    results, summary, baseline_token_deltas = {}, {}, []
    for variant, events in [('before', old), ('after', new)]:
        source = {**{e['sourceEventID']: e for e in events}, **gaps}
        order = sorted(source, key=lambda k: (source[k].get('availableAt') or source[k]['beforeAt'], k))
        cache = {}
        selected = []
        for n, frozen in enumerate(cohort, 1):
            if not a.start_case <= n <= a.end_case:
                continue
            ids = [k for k in order if (source[k].get('availableAt') or source[k]['beforeAt']) < frozen['targetBeganAt']]
            assert frozen['targetEventID'] not in ids
            full_ids = ids
            ids = budget_suffix(ids, source, frozen['query'], settings['taskInstruction'], tokenizer, packer, cache)
            context = '\n'.join(source[k]['serialized'] for k in ids)
            example = dict(frozen, contextBlockIDs=ids,
                contextEventIDs=[k for k in ids if k not in gaps], context=context,
                modelInput=(context+'\n' if context else '')+frozen['query'])
            packed = packer.pack_model_input(example, source, tokenizer, 32768,
                settings['taskInstruction'], cache, True)
            if n in (1, 100, 150, 559):
                # Real-data regression against the unoptimized production path.
                full_context = '\n'.join(source[k]['serialized'] for k in full_ids)
                full = packer.pack_model_input(dict(example, contextBlockIDs=full_ids,
                    contextEventIDs=[k for k in full_ids if k not in gaps], context=full_context,
                    modelInput=(full_context+'\n' if full_context else '')+frozen['query']),
                    source, tokenizer, 32768, settings['taskInstruction'], cache, True)
                for key in ('inputIDs', 'contextEventSpans', 'readRenderingCounts'):
                    assert packed[key] == full[key], ('Bounded packing changed output', variant, n, key)
            for key, (text, token_ids) in cache.items():
                if isinstance(token_ids, list):
                    cache[key] = (text, array('I', token_ids))
            if len(cache) > 1000:
                cache = {key: cache[key] for key in ids}
            assert len(packed['inputIDs']) <= 32768
            hits, followup_hits = [], []
            for span in packed['contextEventSpans']:
                eid = span['eventID']
                serialized = span.get('packedSerialized', source[eid]['serialized'])
                content = json.loads(serialized).get('content', '')
                for w in defects.get(eid, []):
                    actual = [phrase for phrase in w['phrases'] if audit.contains_phrase(phrase, content)]
                    if actual:
                        hits.append(dict(w, phrases=actual))
                for w in followup_witnesses:
                    if eid != w['eventID']:
                        continue
                    actual = [phrase for phrase in w['phrases'] if audit.contains_phrase(phrase, content)]
                    if actual:
                        followup_hits.append(dict(w, phrases=actual))
            result = {'inputTokens': len(packed['inputIDs']), 'verifiedReadDefects': hits,
                'retainedEvents': len(packed['contextEventSpans']),
                'readRenderingCounts': packed['readRenderingCounts']}
            results.setdefault(n, {'case': n, 'exampleID': frozen['exampleID']})[variant] = result
            results[n][variant+'FollowupDefects'] = followup_hits
            if variant == 'before':
                expected = {k: prior_cases[n]['after'][k] for k in result}
                if result['inputTokens'] != expected['inputTokens']:
                    baseline_token_deltas.append({'case': n, 'previous': expected['inputTokens'], 'current': result['inputTokens']})
                assert all(result[k] == expected[k] for k in result if k != 'inputTokens'), ('Baseline did not reproduce', n,
                    {k: {'expected': expected[k], 'actual': result[k]} for k in result if expected[k] != result[k]})
            if n in (128, 239, 268, 274, 369, 370, 380, 381, 501, 574, 575, 584, 589):
                selected.append({'case': n, 'blocks': [{'eventID': s['eventID'],
                    'serialized': s.get('packedSerialized', source[s['eventID']]['serialized'])}
                    for s in packed['contextEventSpans']]})
            if n % 100 == 0:
                print(variant, n, flush=True)
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/(1024**2 if sys.platform == 'darwin' else 1024)
            if rss > 1800:
                raise MemoryError('Context audit exceeded 1800 MiB')
        save(a.output/f'{variant}-selected-contexts.json', selected)
        cases, stages = [], defaultdict(set)
        for n, r in results.items():
            if r[variant]['verifiedReadDefects']:
                cases.append(n)
            for w in r[variant]['verifiedReadDefects']:
                stages[w['stage']].add(n)
        summary[variant] = {'affectedContexts': len(cases), 'percent': len(cases)/687*100,
            'cases': cases, 'byStage': {k: sorted(v) for k, v in stages.items()}}
        cache.clear()
    before, after = set(summary['before']['cases']), set(summary['after']['cases'])
    summary.update(version='post-pane-packed-context-audit-v1', cohortExamples=687,
        startCase=a.start_case, endCase=a.end_case, auditedExamples=len(results),
        noLongerFlagged=sorted(before-after), newlyFlagged=sorted(after-before),
        targetQueryOnsetMaskAndHistoricalWritesUnchanged=True, baselineDefectsAndRenderingCountsReproducedExactly=True,
        baselineTokenCountChanges=baseline_token_deltas,
        boundedPacking='Exact token-count suffix including overflowing boundary block; same production truncation and dependency rendering; full-path regression on cases 1, 100, 150, 559',
        scope='Fixed screenshot-verified defect witnesses surviving actual dependency-aware 32K packing; lower bound, not exhaustive pixel adjudication',
        astraCorrectionsApplied=False, providerCalls=0, peakRSSMiB=rss,
        sourceEventsSHA256=source_hashes, selectionChanges=selections, paneSourceMetadataChanges=source_changes,
        scriptSHA256=sha(Path(__file__)), packerSHA256=sha(ROOT/'scripts/pack-phase1-dataset.py'),
        sourceWitnessesSHA256=sha(PRIOR_AUDIT/'verified-read-witnesses.json'),
        sourceCohortSHA256=sha(PREP/'paired-qwen38-reference32k/cohort.jsonl'))
    save(a.output/'cases.json', list(results.values()))
    save(a.output/'summary.json', summary)
    print(json.dumps({k: v for k, v in summary.items() if k in ('before', 'after', 'noLongerFlagged', 'newlyFlagged', 'peakRSSMiB')}, indent=2))


if __name__ == '__main__':
    main()

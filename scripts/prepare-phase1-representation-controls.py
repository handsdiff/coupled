#!/usr/bin/env python3
"""Offline whole-history OCR/image controls. No dispatch or credential path.

Four raw conditions (token/time x OCR/images) plus two reusable cleaned controls.
Time controls never truncate; token controls stop at the first overflowing time.
"""
import argparse
from collections import Counter, defaultdict, deque
from datetime import datetime
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import resource
import tiktoken

from phase1_read_model_comparison import Privacy, canonical, file_hash, fingerprint, rows
from phase1_vision_raw_history import FullWindowOCR, pilot_module, raw_block

VERSION = 'phase1-representation-controls-v1'
ARMS = ('token_cleaned', 'token_ocr', 'token_images', 'time_cleaned', 'time_ocr', 'time_images')
MARGIN = 512
CONTEXT_LIMIT = 1_050_000
OUTPUT_RESERVE = 8192  # Capacity check only, not a change to generation settings.
PAYLOAD_LIMIT = 512_000_000
IMAGE_LIMIT = 1500
IMAGE_PATCH_LIMIT = 30000


def load(p):
    return json.loads(Path(p).read_text())


def save(p, x):
    with p.open('x') as f:
        os.chmod(p, 0o600)
        f.write(canonical(x) + '\n')


def dt(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00'))


class Index:
    def __init__(self, path, key):
        self.path, self.offsets = path, {}
        with path.open('rb') as f:
            while True:
                at = f.tell(); line = f.readline()
                if not line:
                    break
                if line.strip():
                    k = key(json.loads(line))
                    assert k not in self.offsets
                    self.offsets[k] = at

    def get(self, key):
        with self.path.open('rb') as f:
            f.seek(self.offsets[key])
            return json.loads(f.readline())


def choose_suffix(timeline, allowance, cost):
    """A whole-timestamp suffix, never a cherry-picked subset or fixed time cap."""
    pos = len(timeline); used = 0; start = pos; next_cost = None
    while pos:
        end = pos; at = timeline[pos-1]['at']
        while pos and timeline[pos-1]['at'] == at:
            pos -= 1
        price = sum(cost(x) for x in timeline[pos:end])
        if used + price > allowance:
            next_cost = used + price
            break
        used += price; start = pos
    return timeline[start:], used, next_cost


def estimated_image_tokens(width, height):
    # Empirical Astra relationship, checked against every recorded original image
    # attribution below. Not claimed as an official Astra tokenization contract.
    return ((math.ceil(width/32)*math.ceil(height/32))*6)//5 + 1


def offline_encoding():
    cache = Path(os.environ.get('TIKTOKEN_CACHE_DIR', Path(__file__).resolve().parents[1]/'.build/tiktoken-budget-cache'))
    vocab = cache/'fb374d419588a4632f3f557e76b4b70aebbca790'
    assert vocab.is_file(), 'Local o200k vocabulary required; preparation must not download it'
    assert file_hash(vocab) == '446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d'
    os.environ['TIKTOKEN_CACHE_DIR'] = str(cache)
    return tiktoken.get_encoding('o200k_base')


def wire_size(parts):
    wire = []
    image_bytes = 0
    for p in parts:
        if p['type'] == 'input_text':
            wire.append(p)
        else:
            prefix = 'data:image/png;base64,'
            wire.append({'type': 'input_image', 'detail': 'original', 'image_url': prefix})
            image_bytes += 4*((Path(p['path']).stat().st_size+2)//3)
    body = {'model': 'chatgpt/gpt-6-astra', 'input': [{'role': 'user', 'content': wire}],
            'reasoning': {'effort': 'xhigh'}, 'tools': [], 'stream': True}
    return len(canonical(body).encode()) + image_bytes


def prepare(pilot, budget, cache, output):
    assert not output.exists(), 'Use a fresh review preparation directory'
    for root in (pilot, budget):
        plan = load(root/'plan.json')
        for n, h in plan['artifactsSHA256'].items():
            assert file_hash(root/n) == h
        assert file_hash(root/'predictions.jsonl') == load(root/'audit.json')['predictionsSHA256']
    pp = load(pilot/'data/plan.json'); frozen = Path(pp['source'])/'frozen'
    for n, h in load(frozen/'artifact-hashes.json').items():
        assert file_hash(frozen/n) == h
    for n, h in pp['sourceRawDigests'].items():
        assert file_hash(n) == h
    originals = Index(pilot/'data/prompts.local.jsonl', lambda x: (x['case'], x['variant']))
    expanded = Index(budget/'data/prompts.local.jsonl', lambda x: (x['case'], x['variant']))
    source_baselines = Index(frozen/'prompts.jsonl', lambda x: (x['exampleID'], x['variant']))
    results = {(x['case'], x['variant']): x for x in rows(pilot/'predictions.jsonl')}
    expanded_results = {(x['case'], x['variant']): x for x in rows(budget/'predictions.jsonl')}
    cases = {x['case']: x for x in rows(pilot/'data/cases.jsonl') if (x['case'], 'cleaned_recent') in results}
    ids = {x['exampleID'] for x in cases.values()}
    cohort = {x['exampleID']: x for x in rows(frozen/'cohort.jsonl') if x['exampleID'] in ids}
    events = {x['sourceEventID']: x for x in rows(frozen/'new-context-events.jsonl')}
    config = load(frozen.parent/'config.json')
    original_events = Path(config['newCorpus'])/'events.jsonl'
    assert file_hash(original_events) == load(frozen/'plan.json')['sourceHashes'][str(original_events)]
    privacy = Privacy({x['sourceEventID']: x for x in rows(original_events)}, config['privacyPolicy'])
    frames = {f['recordID']: f for fs in pilot_module().read_evidence([n for n in pp['sourceRawDigests'] if n.endswith('/raw.jsonl')]).values() for f in fs}
    for f in rows(pilot/'data/frames.jsonl'):
        if f['recordID'] in frames and frames[f['recordID']]['ocr'] is None and f.get('ocr'):
            original = frames[f['recordID']]
            assert original['screenshotSHA256'] == f['screenshotSHA256']
            assert original['capturedAt'] == f['capturedAt']
            original['ocr'] = f['ocr']
    enc = offline_encoding()
    @lru_cache(maxsize=16384)
    def count(t):
        return len(enc.encode_ordinary(t))
    by_hash = {f['screenshotSHA256']: f for f in frames.values()}
    observed_geometry = set(); image_matches = 0; image_overheads = {}
    for c in sorted(cases):
        p = originals.get((c, 'screenshots_recent')); r = results[c, 'screenshots_recent']
        attributed = [v['content'] for v in r['usage']['attribution']['items'].values() if len(v.get('content', [])) == len(p['parts'])]
        assert len(attributed) == 1
        image_sum = 0
        for part, usage in zip(p['parts'], attributed[0]):
            if part['type'] == 'local_image':
                f = by_hash[part['sha256']]
                expected = estimated_image_tokens(f['width'], f['height'])
                assert expected == usage['input_tokens'], 'Image formula fails measured Astra evidence'
                image_sum += expected; image_matches += 1
                observed_geometry.add((f['width'], f['height']))
        image_overheads[c] = r['usage']['input_tokens'] - image_sum - sum(count(t['text']) for t in p['parts'] if t['type'] == 'input_text')
    observer = FullWindowOCR(cache)
    @lru_cache(maxsize=None)
    def observe(ident):
        f = frames[ident]
        return raw_block(f, observer.ensure(f), privacy)
    def parts_for(item, visual):
        if item['kind'] != 'frame':
            return [{'type': 'input_text', 'text': item['serialized']+'\n'}]
        f = frames[item['id']]; b = observe(item['id'])
        if not visual or b['privacyRedacted']:
            return [{'type': 'input_text', 'text': b['serialized']+'\n'}]
        meta = {'kind': 'read_observation', 'capturedAt': f['capturedAt'], 'source': f['source']}
        return [{'type': 'input_text', 'text': canonical(meta)+'\n'},
                {'type': 'local_image', 'path': f['screenshotPath'], 'sha256': f['screenshotSHA256'], 'detail': 'original'}]
    def item_cost(item, visual):
        return sum(count(p['text']) if p['type'] == 'input_text' else estimated_image_tokens(frames[item['id']]['width'], frames[item['id']]['height']) for p in parts_for(item, visual))
    output.mkdir(parents=True, mode=0o700)
    summaries = []; used_frames = set(); payload_hashes = {}; baseline_rows = []
    for c, case in sorted(cases.items()):
        ex = cohort[case['exampleID']]; cutoff = case['targetBeganAt']
        b = source_baselines.get((case['exampleID'], 'new'))
        floor = min(t['availableAt'] for t in b['retainedBlocks'])
        native = [events[i] for i in ex['contextBlockIDs']]
        forbidden = {ex['targetEventID'], *ex['episode']['memberWriteEventIDs']}
        assert not forbidden.intersection(ex['contextBlockIDs'])
        assert all(t['availableAt'] < cutoff for t in native)
        sessions = {t['sessionID'] for t in native if t.get('sessionID')} | {ex['sessionID']}
        eligible_frames = [f for f in frames.values() if f['sessionID'] in sessions and f['capturedAt'] < cutoff]
        timeline = [{'at': f['capturedAt'], 'kind': 'frame', 'id': f['recordID']} for f in eligible_frames]
        timeline += [{'at': e['availableAt'], 'kind': e['kind'], 'id': e['sourceEventID'], 'serialized': e['serialized']} for e in native if e['kind'] != 'read']
        timeline.sort(key=lambda x: (x['at'], x['kind'] == 'frame', x['id']))
        time_frames = [x for x in timeline if x['kind'] == 'frame' and floor <= x['at']]
        # Preserve EXACT retained WRITE/gap serialization, including the boundary
        # event if the 32K pack truncated one. No newly reconstructed WRITE here.
        time_writes = [{'at': x['availableAt'], 'kind': x['kind'], 'id': x['eventID'], 'serialized': x['serialized']} for x in b['retainedBlocks'] if x['kind'] != 'read']
        same_time = sorted(time_frames+time_writes, key=lambda x: (x['at'], x['kind'] == 'frame', x['id']))
        assigned = results[c, 'screenshots_recent']['usage']['input_tokens']
        original_clean = originals.get((c, 'cleaned_recent'))
        header, query = original_clean['parts'][0], original_clean['parts'][-1]
        assert query['text'] == case['query']
        fixed_image = count(header['text']) + count(query['text']) + image_overheads[c]
        token_images, _, next_cost = choose_suffix(timeline, assigned-fixed_image-MARGIN, lambda t: item_cost(t, True))
        case_dir = output/f'case-{c:04d}'; case_dir.mkdir(mode=0o700)
        for arm in ARMS:
            visual = arm.endswith('images'); is_time = arm.startswith('time_')
            reuse = None; items = []; seen = []; source_prompt = None
            if arm in ('time_cleaned', 'token_cleaned', 'token_ocr'):
                v = 'raw_ocr_recent' if arm == 'token_ocr' else 'cleaned_recent'
                root = pilot if is_time else budget
                source_prompt = original_clean if is_time else expanded.get((c, v))
                p = source_prompt['parts']
                result = results[c, v] if is_time else expanded_results[c, v]
                assert fingerprint(p) == result['partsSHA256']
                reuse = {'run': str(root), 'sampleID': result['sampleID'], 'predictionsSHA256': file_hash(root/'predictions.jsonl'), 'partsSHA256': result['partsSHA256']}
                estimate = result['usage']['input_tokens']; accounting = 'previous_reported_usage_exact_same_prompt'
                seen = source_prompt.get('frameRecordIDs', [])
                if arm == 'token_ocr':
                    start = source_prompt['budgetPacking']['expandedHistoryStartAt']
                elif arm == 'token_cleaned':
                    start = source_prompt['budgetPacking']['expandedHistoryStartAt']
                else:
                    start = floor
                # Preserve payload byte-for-byte; indices remain review metadata.
                items = [{'id': f'part-{i}', 'kind': 'read_or_write', 'at': None, 'partStart': i, 'partEnd': i+1} for i in range(1, len(p)-1)]
            else:
                selected = same_time if is_time else token_images
                p = [header]
                for t in selected:
                    ix = len(p); p.extend(parts_for(t, visual))
                    items.append({'id': t['id'], 'kind': 'read_observation' if t['kind'] == 'frame' else t['kind'], 'at': t['at'], 'partStart': ix, 'partEnd': len(p)})
                    if t['kind'] == 'frame':
                        seen.append(t['id'])
                p.append(query)
                start = floor if is_time else min((t['at'] for t in selected), default=cutoff)
                if visual:
                    overhead = image_overheads[c]
                else:
                    old = originals.get((c, 'raw_ocr_recent'))
                    overhead = results[c, 'raw_ocr_recent']['usage']['input_tokens'] - sum(count(q['text']) for q in old['parts'])
                estimate = count(header['text'])+count(query['text'])+overhead+sum(item_cost(t, visual) for t in selected)
                accounting = 'empirical_image_attributions_plus_local_text' if visual else 'locally_calibrated_text_estimate'
            assert p[0] == header and p[-1] == query
            assert not privacy.unsafe(''.join(x.get('text', '') for x in p))
            for ident in seen:
                assert frames[ident]['capturedAt'] < cutoff
                if is_time:
                    assert frames[ident]['capturedAt'] >= floor
                used_frames.add(ident)
            text_read_count = 0; write_text = []
            for part in p[1:-1]:
                if part['type'] != 'input_text':
                    continue
                d = json.loads(part['text']); text_read_count += d.get('kind') == 'read'
                if d.get('kind') not in ('read', 'read_observation'):
                    write_text.append(part['text'])
            if arm in ('time_ocr', 'time_images'):
                assert write_text == [x['serialized']+'\n' for x in time_writes], 'Time-matched WRITE representation/order changed'
                assert seen == [x['id'] for x in time_frames], 'Time-matched frame interval was truncated'
            if not arm.endswith('cleaned'):
                assert text_read_count == 0, 'Hidden cleaned READ background'
            nimages = sum(x['type'] == 'local_image' for x in p); nbytes = wire_size(p)
            limits = []
            if estimate+OUTPUT_RESERVE > CONTEXT_LIMIT:
                limits.append('estimated_context_over_limit')
            if nimages > IMAGE_LIMIT:
                limits.append('documented_image_count_over_limit')
            if nbytes > PAYLOAD_LIMIT:
                limits.append('documented_payload_over_limit')
            if visual and any(math.ceil(frames[i]['width']/32)*math.ceil(frames[i]['height']/32) > IMAGE_PATCH_LIMIT for i in seen):
                limits.append('unprocessed_image_patch_grid_over_documented_limit')
            if arm == 'token_images':
                assert estimate <= assigned
            redacted = sum(bool(observe(i)['privacyRedacted']) for i in seen)
            unknown_geometries = sorted({(frames[i]['width'], frames[i]['height']) for i in seen if (frames[i]['width'], frames[i]['height']) not in observed_geometry})
            row = {'case': c, 'arm': arm, 'application': case['application'], 'target': case['target'], 'targetSHA256': fingerprint(case['target']),
                   'exampleID': case['exampleID'], 'query': case['query'], 'partsSHA256': fingerprint(p),
                   'intervalStartAt': start, 'cutoffExclusive': cutoff, 'historySpanMinutes': (dt(cutoff)-dt(start)).total_seconds()/60,
                   'fixed32KStartAt': floor, 'fixed32KReferenceTokens': b['referenceInputTokens'],
                   'assignedInputTokens': assigned if not is_time else None, 'estimatedInputTokens': estimate, 'tokenAccounting': accounting,
                   'imageCount': nimages, 'frameCount': len(seen), 'historyItems': len(items), 'historicalWriteCount': len(write_text),
                   'wirePayloadBytes': nbytes, 'cleanedReadCount': text_read_count, 'privacyRedactedFrames': redacted,
                   'unmeasuredImageGeometries': unknown_geometries if visual else [], 'limits': limits,
                   'status': 'reused_completed' if reuse else 'blocked_input_limits' if limits else 'prepared_needs_review_and_remote_preflight',
                   'reuse': reuse, 'dispatchAuthorized': False, 'payload': f'case-{c:04d}/{arm}.json'}
            full = dict(row, parts=p, items=items, frameRecordIDs=seen, fullIntervalRetained=is_time,
                        wholeTimestampOverflowCost=next_cost if arm == 'token_images' else None)
            save(output/row['payload'], full); payload_hashes[row['payload']] = file_hash(output/row['payload']); summaries.append(row)
        baseline_rows.append({'case': c, 'intervalStartAt': floor, 'cutoffExclusive': cutoff,
                              'boundaryBlockTruncated': b['retainedBlocks'][0]['contentTruncated'], 'sourcePlan': str(frozen/'prompts.jsonl')})
        print(f'Prepared case {c}: {len(time_frames)} full-interval frames; no calls', flush=True)
    with (output/'frames.jsonl').open('x') as f:
        os.chmod(output/'frames.jsonl', 0o600)
        for i in sorted(used_frames):
            record = frames[i]
            f.write(canonical({k: record.get(k) for k in ('recordID', 'capturedAt', 'source', 'screenshotPath', 'screenshotSHA256', 'width', 'height')})+'\n')
    save(output/'cases.json', summaries)
    save(output/'time-boundaries.json', baseline_rows)
    summary = {'cases': len(cases), 'preparedConditions': 4, 'reusableCleanedBaselines': 2, 'providerCalls': 0,
               'arms': {a: {'cases': sum(r['arm']==a for r in summaries), 'statuses': dict(Counter(r['status'] for r in summaries if r['arm']==a)),
                            'meanInputTokens': sum(r['estimatedInputTokens'] for r in summaries if r['arm']==a)/len(cases),
                            'maxImages': max(r['imageCount'] for r in summaries if r['arm']==a)} for a in ARMS}}
    save(output/'summary.json', summary)
    save(output/'plan.json', {'version': VERSION, 'status': 'offline_manual_review_only', 'dispatchAuthorized': False,
        'fourConditions': ['token_ocr', 'token_images', 'time_ocr', 'time_images'], 'cleanedControls': ['token_cleaned', 'time_cleaned'],
        'model': 'chatgpt/gpt-6-astra', 'reasoning': 'xhigh', 'temperature': 'omitted_as_in_completed_runs', 'tools': [],
        'tokenBudget': 'Same per-case original screenshot input budget as the completed expanded text experiment; whole chronological suffix, no time cutoff.',
        'timeBoundary': 'Exact per-case earliest retained event of the frozen 32K cleaned plan through exclusive target onset; all causal raw frames in that interval. No shortening at size limits.',
        'writePolicy': 'Exact prior WRITE serialization. Time arms preserve baseline retained WRITEs; token arms retain the eligible WRITE suffix fitting their representation budget.',
        'readPolicy': 'OCR/images throughout raw arms; no cleaned background, pane filter, deduplication, or target-informed selection.',
        'privacyPolicy': 'Same known-credential filter; sensitive frames are explicit redacted observations in both raw forms. New full-window evidence still requires user visual approval.',
        'imageAccounting': {'method': 'floor(1.2 * ceil(width/32) * ceil(height/32)) + 1, empirically fits all prior per-image input attributions',
                            'matchingObservedImages': image_matches, 'matchingObservedGeometries': len(observed_geometry),
                            'exactFutureRemoteTokenizer': 'unverified', 'remotePreflightRequired': True},
        'capacityChecks': {'officialModelContextTokens': CONTEXT_LIMIT, 'outputReserveForSizingOnly': OUTPUT_RESERVE,
                           'documentedPayloadBytes': PAYLOAD_LIMIT, 'documentedImageCount': IMAGE_LIMIT,
                           'documentedPerImageProcessedPatchLimit': IMAGE_PATCH_LIMIT,
                           'subscriptionProxyLimits': 'unverified beyond prior completed requests; no silent truncation permitted'},
        'documentation': ['https://developers.openai.com/api/docs/models/gpt-6-astra', 'https://developers.openai.com/api/docs/guides/images-vision'],
        'sourcePilot': str(pilot), 'sourceBudgetRun': str(budget), 'sourceRawDigests': pp['sourceRawDigests'],
        'sourceHashes': {str(p): file_hash(p) for p in [pilot/'predictions.jsonl', budget/'predictions.jsonl', frozen/'artifact-hashes.json', Path(__file__), Path(__file__).with_name('phase1_vision_raw_history.py')]},
        'localOCRSidecarsSHA256': observer.used, 'providerCalls': 0})
    payload_hashes.update({n: file_hash(output/n) for n in ('frames.jsonl','cases.json','summary.json','plan.json','time-boundaries.json')})
    save(output/'artifact-hashes.json', payload_hashes)
    print(canonical(summary)); print('Peak MiB', resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024**2, flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for key in ('pilot', 'budget', 'cache', 'output'):
        p.add_argument('--'+key, type=Path, required=True)
    a = p.parse_args(); prepare(a.pilot.resolve(), a.budget.resolve(), a.cache.resolve(), a.output.resolve())

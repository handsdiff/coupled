#!/usr/bin/env python3
"""Prepare a private recent-READ modality pilot. No network or credentials.

All source journals/predictions are immutable. Three fresh controls share a
recent interval, preceding WRITE content, old background and onset query.
Artifacts are NOT an authorization to transmit screenshots.
"""
import argparse
import base64
from collections import Counter, defaultdict
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import resource
import struct

from phase1_read_model_comparison import Privacy, canonical, dump, dump_rows, file_hash, fingerprint, rows
from phase1_repeatability_review import scored_directory

VERSION = 'phase1-recent-read-vision-pilot-v3'
VARIANTS = ('cleaned_recent', 'raw_ocr_recent', 'screenshots_recent')
MODEL = 'chatgpt/gpt-6-astra'
MAX_FRAMES = 60
MAX_PATCHES = 180_000  # geometry guard, NOT a verified Astra token estimate
MAX_IMAGE_BYTES = 128 * 1024 * 1024


def require(test, message):
    if not test:
        raise ValueError(message)


def load(path):
    return json.loads(Path(path).read_text())


def import_ui():
    spec = importlib.util.spec_from_file_location('vision_pilot_ui_source', Path(__file__).with_name('serve-phase1-model-comparison.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def iso(value):
    return value.isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def numbered_rows(path):
    with Path(path).open() as stream:
        for number, line in enumerate(stream, 1):
            if line.strip():
                yield number, json.loads(line)


def choose_cases(examples, total=72):
    groups = defaultdict(list)
    for e in examples:
        if not e['substantive'] or e['case'] in (353, 354):
            continue
        key = {(False, True): 'improved', (True, False): 'regressed',
               (True, True): 'both_pass', (False, False): 'both_fail'}[
                   e['passes']['astra_old'], e['passes']['astra_new']]
        groups[key].append(e)
    chosen = []
    for group in ('improved', 'regressed', 'both_pass', 'both_fail'):
        values = sorted(groups[group], key=lambda e: fingerprint([17, e['exampleID']]))
        if group != 'both_fail':
            chosen.extend({**e, 'selectionStratum': group} for e in values)
            continue
        count = total-len(chosen)
        require(1 <= count <= len(values), 'Case budget cannot cover all changed/both-pass cases plus failures')
        picked = [next(e for e in values if e['case'] == 283)]
        for e in values:
            if len(picked) == count:
                break
            if e not in picked:
                picked.append(e)
        chosen.extend({**e, 'selectionStratum': group} for e in picked)
    return sorted(chosen, key=lambda e: e['case'])


def load_ocr_sidecar(folder):
    if folder is None:
        return {}, None
    folder = Path(folder).resolve()
    manifest = load(folder/'manifest.json')
    require(manifest['status'] == 'complete', 'OCR sidecar incomplete')
    for name,digest in manifest['artifactSHA256'].items():
        require(file_hash(folder/name) == digest, 'OCR sidecar artifact changed')
    result = {r['sourceFrameRecordID']:r for r in rows(folder/'observations.jsonl')}
    require(len(result) == manifest['completedFrames'], 'OCR sidecar count mismatch')
    return result, {'path':str(folder),'manifestSHA256':file_hash(folder/'manifest.json')}


def apply_ocr_sidecar(frames, observations):
    for frame in frames:
        if frame['ocr'] is not None:
            continue  # Never replace existing raw OCR, even if replay seems cleaner.
        value = observations.get(frame['recordID'])
        if value is None:
            continue
        require(value['capturedAt'] == frame['capturedAt']
                and value['screenshotSHA256'] == frame['screenshotSHA256'], 'OCR backfill frame binding mismatch')
        require(value['origin'] == 'local_full_window_ocr_backfill'
                and value['cropFractions'] == {'viewportSideCropFraction':0,'viewportTopCropFraction':0,'viewportBottomCropFraction':0}
                and not value['contentWasTruncated'] and isinstance(value['content'],str), 'Invalid full-window OCR backfill')
        frame['ocr'] = value
        frame['localOCRSidecarRecordID'] = value['recordID']


def read_evidence(raw_paths):
    """Stream journals; retain only image/text metadata, never AX trees/values."""
    frames, ocr = {}, defaultdict(list)
    for path in raw_paths:
        path = Path(path)
        for line_number, r in numbered_rows(path):
            kind = r.get('recordType')
            if kind not in ('screen_ocr_observation', 'visual_frame_observation', 'visual_ocr_observation'):
                continue
            rel = r.get('screenshotRelativePath')
            if not rel:
                continue
            image_path = (path.parent / rel).resolve()
            require(image_path.is_relative_to(path.parent.resolve() / 'screenshots'), 'Image path escapes session screenshots')
            key = (r['sessionID'], rel)
            record = {'recordID': r['recordID'], 'rawPath': str(path.resolve()), 'rawLine': line_number}
            if kind != 'visual_ocr_observation':
                require(key not in frames, 'Duplicate frame identity')
                width = r.get('screenshotPixelWidth', r.get('framePixelWidth'))
                height = r.get('screenshotPixelHeight', r.get('framePixelHeight'))
                require(isinstance(width, int) and isinstance(height, int), 'Missing image dimensions')
                surface = r.get('surface', r)
                frames[key] = {**record, 'sessionID': r['sessionID'], 'capturedAt': r['capturedAt'],
                    'screenshotPath': str(image_path), 'screenshotSHA256': r['screenshotSHA256'],
                    'width': width, 'height': height,
                    'patchGrid32px': ((width + 31)//32) * ((height + 31)//32),
                    'source': {'application': surface.get('appName'), 'resourceTitle': surface.get('windowTitle')},
                    'evidenceReason': r.get('evidenceReason', kind), 'rawRecordType': kind,
                    'observedAt': r.get('observedAt'),
                    'pointer': {'x': r.get('x'), 'y': r.get('y')},
                    'captureBounds': r.get('captureBounds', surface.get('windowBounds'))}
            if kind != 'visual_frame_observation':
                ocr[key].append({**record, 'capturedAt': r['capturedAt'], 'observedAt': r.get('observedAt'),
                    'content': r.get('content', ''), 'contentWasTruncated': r.get('contentWasTruncated', False),
                    'screenshotSHA256': r.get('screenshotSHA256'),
                    'captureScope': r.get('captureScope', 'raw_screen_observation'),
                    'cropFractions': {k: r.get(k, 0) for k in ('viewportSideCropFraction', 'viewportTopCropFraction', 'viewportBottomCropFraction')}})
    result = defaultdict(list)
    for key, frame in frames.items():
        observations = sorted(ocr[key], key=lambda x: (x['observedAt'] or '', x['recordID']))
        require(all(x['capturedAt'] == frame['capturedAt'] and x['screenshotSHA256'] == frame['screenshotSHA256'] for x in observations), 'OCR does not bind exact captured frame')
        # Preserve the first recorded full-window OCR, not a later cleaned rerun.
        selected = observations[0] if observations else None
        frame['ocr'] = selected
        frame['allOCRRecordIDs'] = [x['recordID'] for x in observations]
        result[frame['sessionID']].append(frame)
    for values in result.values():
        values.sort(key=lambda x: (x['capturedAt'], x['rawLine'], x['recordID']))
    return result


def select_interval(frames, cutoff, floor, seconds=300, max_frames=MAX_FRAMES, max_patches=MAX_PATCHES):
    eligible = [f for f in frames if floor <= f['capturedAt'] < cutoff]
    require(eligible, 'No causal screenshot in the retained context interval')
    start = max(floor, iso(stamp(eligible[-1]['capturedAt']) - timedelta(seconds=seconds)))
    chosen = [f for f in eligible if f['capturedAt'] >= start]
    before_limits = len(chosen)
    # Remove whole earliest timestamp groups, never cherry-pick frames using
    # target content, visual quality, pane selection, novelty, or model output.
    def too_large(values):
        return (len(values) > max_frames or sum(f['patchGrid32px'] for f in values) > max_patches
                or sum(Path(f['screenshotPath']).stat().st_size for f in values) > MAX_IMAGE_BYTES)
    while chosen and too_large(chosen):
        oldest = chosen[0]['capturedAt']
        chosen = [f for f in chosen if f['capturedAt'] != oldest]
        if chosen:
            start = chosen[0]['capturedAt']
    require(chosen, 'A single timestamp group exceeds the image budget')
    return chosen, {'startAt': start, 'targetBeganAt': cutoff, 'latestFrameAt': chosen[-1]['capturedAt'],
        'latestFrameAgeSeconds': (stamp(cutoff)-stamp(chosen[-1]['capturedAt'])).total_seconds(),
        'nominalRecentSeconds': seconds, 'framesBeforeSizeLimits': before_limits,
        'framesAfterSizeLimits': len(chosen), 'sizeLimitShortenedInterval': before_limits != len(chosen)}


def frame_is_sensitive(frame, privacy):
    text = frame['ocr']['content'] if frame['ocr'] else ''
    source = frame['source']
    return ((source.get('application'), source.get('resourceTitle')) in privacy.credential_sources
            or frame['recordID'] in privacy.raw_ids
            or bool(set(frame['allOCRRecordIDs']) & privacy.raw_ids)
            or privacy.unsafe(text) or privacy.unsafe(canonical(source)))


def render_parts(instruction, query, background, recent, variant):
    parts = [{'type': 'input_text', 'text': instruction + '\n'}]
    for block in background:
        parts.append({'type': 'input_text', 'text': block['serialized'] + '\n'})
    for item in recent:
        if item['type'] == 'event':
            parts.append({'type': 'input_text', 'text': item['serialized'] + '\n'})
        elif variant == 'screenshots_recent':
            # Absolute pixel coordinates are excluded from all model inputs;
            # no target-informed crop/arrow/annotation is applied to images.
            meta = {'kind': 'read_observation', 'capturedAt': item['capturedAt'], 'source': item['source']}
            parts.append({'type': 'input_text', 'text': canonical(meta) + '\n'})
            parts.append({'type': 'local_image', 'path': item['screenshotPath'],
                          'sha256': item['screenshotSHA256'], 'detail': 'original'})
        else:
            meta = {'kind': 'read_observation', 'capturedAt': item['capturedAt'], 'source': item['source'],
                    'content': item['ocr']['content'] if item['ocr'] else '',
                    'ocrAvailable': item['ocr'] is not None}
            parts.append({'type': 'input_text', 'text': canonical(meta) + '\n'})
    parts.append({'type': 'input_text', 'text': query})
    return parts


def build_prompt(example, baseline, frame_list, interval, variant, instruction):
    start, cutoff = interval['startAt'], example['targetBeganAt']
    blocks = baseline['retainedBlocks']
    require(all(b['availableAt'] < cutoff for b in blocks), 'Unavailable baseline history')
    background = [b for b in blocks if b['availableAt'] < start]
    events = [b for b in blocks if b['availableAt'] >= start and (variant == 'cleaned_recent' or b['kind'] != 'read')]
    recent = [{'type': 'event', 'capturedAt': b['availableAt'], 'stableOrdinal': i,
               'eventID': b['eventID'], 'serialized': b['serialized'], 'kind': b['kind']} for i,b in enumerate(events)]
    if variant != 'cleaned_recent':
        recent.extend({**f, 'type': 'frame', 'stableOrdinal': len(events)+i} for i,f in enumerate(frame_list))
    recent.sort(key=lambda x: (x['capturedAt'], x['stableOrdinal']))
    parts = render_parts(instruction, baseline['query'], background, recent, variant)
    writes = [b['serialized'] for b in background if b['kind'] == 'write'] + [e['serialized'] for e in recent if e.get('kind') == 'write']
    require(writes == [b['serialized'] for b in blocks if b['kind'] == 'write'], 'Prior WRITE order/content changed')
    require(all(f['capturedAt'] < cutoff for f in frame_list), 'Target-time screenshot included')
    return {'version': VERSION, 'case': example['case'], 'exampleID': example['exampleID'], 'variant': variant,
        'targetBeganAt': cutoff, 'querySHA256': fingerprint(baseline['query']),
        'backgroundSHA256': fingerprint([b['serialized'] for b in background]),
        'priorWritesSHA256': fingerprint(writes), 'recentInterval': interval,
        'recentReadEventIDs': [e['eventID'] for e in recent if e.get('kind') == 'read'],
        'frameRecordIDs': [] if variant == 'cleaned_recent' else [f['recordID'] for f in frame_list],
        'imageCount': sum(p['type'] == 'local_image' for p in parts),
        'textCharacters': sum(len(p.get('text', '')) for p in parts),
        'parts': parts, 'partsSHA256': fingerprint(parts),
        'baselinePromptSHA256': baseline['modelInputSHA256'],
        'cleanedControlMatchesOriginalTextExactly': ''.join(p.get('text','') for p in parts) == baseline['modelInput'] if variant == 'cleaned_recent' else None}


def wire_payload(prompt):
    """Pure local encoding; this function cannot send a request."""
    require(fingerprint(prompt['parts']) == prompt['partsSHA256'], 'Prompt content changed')
    content = []
    for part in prompt['parts']:
        if part['type'] == 'input_text':
            content.append(copy.deepcopy(part))
        else:
            require(part['type'] == 'local_image', 'Unexpected content type')
            p = Path(part['path'])
            require(file_hash(p) == part['sha256'], 'Image bytes changed')
            content.append({'type': 'input_image', 'detail': part['detail'],
                            'image_url': 'data:image/png;base64,' + base64.b64encode(p.read_bytes()).decode('ascii')})
    return {'model': MODEL, 'input': [{'role': 'user', 'content': content}],
            'reasoning': {'effort': 'xhigh'}, 'tools': [], 'stream': True}


def prepare(source, output, ocr_sidecar=None, inventory_only=False, image_preflight=None):
    require(not output.exists(), 'Use a new output directory')
    source = source.resolve()
    review = source/'repeatability/run-v1/review-v1'
    source_scoring = scored_directory(review)
    # Freeze the exact selection inputs before reading them. Review publication
    # can continue independently, but must not silently change this pilot.
    source_files = [source/'config.json', source/'completion.json',
                    source/'frozen/artifact-hashes.json',
                    source/'holistic-v3/scoring-revision.json',
                    source_scoring/'completion.json',
                    source_scoring/'answers.jsonl',source_scoring/'cases.jsonl',source_scoring/'summary.json']
    source_digests = {str(p):file_hash(p) for p in source_files}
    preflight = None
    if image_preflight:
        image_preflight=Path(image_preflight).resolve()
        checked=load(image_preflight/'completion.json')
        require(checked['status']=='passed' and checked['personalData'] is False
                and [r['imageCount'] for r in checked['results']]==[1,8]
                and all(r['imageReadPassed'] and r['responseModel'].removeprefix('chatgpt/')==MODEL.removeprefix('chatgpt/') for r in checked['results']), 'Image preflight not verified')
        require(checked['planSHA256']==file_hash(image_preflight/'plan.json'),'Image preflight plan changed')
        for name in ('completion.json','plan.json','request-1.json','request-8.json','attempt-1/response.body','attempt-8/response.body','attempt-1/transport.json','attempt-8/transport.json'):
            source_digests[str(image_preflight/name)]=file_hash(image_preflight/name)
        for r in checked['results']:
            require(file_hash(image_preflight/f"attempt-{r['imageCount']}"/'response.body')==r['timing']['bodySHA256'],'Image preflight response changed')
        preflight={'path':str(image_preflight),'completionSHA256':file_hash(image_preflight/'completion.json'),
            'measuredResponses':2,'syntheticOnly':True,
            'latencySeconds':[r['timing']['dispatchToCompletionSeconds'] for r in checked['results']],
            'totalInputTokens':[r['usage']['input_tokens'] for r in checked['results']],
            'notPersonalPredictionLatency':True}
    original = load(source/'frozen/plan.json')
    agreed_scoring = load(source_scoring/'summary.json')
    for name, digest in load(source/'frozen/artifact-hashes.json').items():
        require(file_hash(source/'frozen'/name) == digest, f'Frozen artifact changed: {name}')
        source_digests[str(source/'frozen'/name)] = digest
    config = load(source/'config.json')
    new_events_path = Path(config['newCorpus'])/'events.jsonl'
    require(file_hash(new_events_path) == original['sourceHashes'][str(new_events_path)], 'Source event artifact changed')
    source_digests[str(new_events_path)] = file_hash(new_events_path)
    privacy = Privacy({r['sourceEventID']: r for r in rows(new_events_path)}, original['privacyPolicy'])
    for path,digest in original['rawDigests'].items():
        require(file_hash(path) == digest, 'Raw source changed')
    data = import_ui().Comparison(source, 'holistic-v3', source/'repeatability/run-v1')
    overview = data.overview()
    selected = choose_cases(overview['examples'])
    selected_by_id = {e['exampleID']: e for e in selected}
    cohort = {r['exampleID']: r for r in rows(source/'frozen/cohort.jsonl') if r['exampleID'] in selected_by_id}
    baseline = {r['exampleID']: r for r in rows(source/'frozen/prompts.jsonl') if r['exampleID'] in selected_by_id and r['variant']=='new'}
    evidence = read_evidence([p for p in original['rawDigests'] if p.endswith('raw.jsonl')])
    backfill, backfill_provenance = load_ocr_sidecar(ocr_sidecar)
    for values in evidence.values():
        apply_ocr_sidecar(values, backfill)
    instruction = original['contextBudget']['instruction']
    prompts, candidates, images = [], [], {}
    for e in selected:
        x = {**cohort[e['exampleID']], 'case': e['case']}
        b = baseline[e['exampleID']]
        floor = min(z['availableAt'] for z in b['retainedBlocks'])
        f, interval = select_interval(evidence[x['sessionID']], x['targetBeganAt'], floor)
        if not inventory_only:
            require(all(z['ocr'] is not None for z in f), 'Missing OCR: prepare --inventory-only, backfill, then prepare --ocr-sidecar')
        sensitive = [z['recordID'] for z in f if frame_is_sensitive(z, privacy)]
        for z in f:
            image_path = Path(z['screenshotPath'])
            require(file_hash(image_path) == z['screenshotSHA256'], 'Captured image digest mismatch')
            with image_path.open('rb') as stream:
                head = stream.read(24)
            require(head[:8] == b'\x89PNG\r\n\x1a\n' and struct.unpack('>II',head[16:24]) == (z['width'],z['height']), 'Image format/dimensions mismatch')
            images[z['recordID']] = z
        c = {'case': e['case'], 'exampleID': x['exampleID'], 'selectionStratum': e['selectionStratum'],
             'application': e['application'], 'target': e['target'], 'targetBeganAt': x['targetBeganAt'],
             'targetSHA256': x['targetSHA256'], 'query': b['query'], 'querySHA256': fingerprint(b['query']),
             'episode': x['episode'], 'interval': interval, 'frameRecordIDs': [z['recordID'] for z in f],
             'sensitiveFrameRecordIDs': sensitive, 'missingOCRFrames': sum(z['ocr'] is None for z in f),
             'locallyBackfilledOCRFrames':sum('localOCRSidecarRecordID' in z for z in f),
             'fullImagePrivacyReview': 'pending_visual_review_not_guaranteed_by_OCR_scan',
             'estimatedRawPatchGrid': sum(z['patchGrid32px'] for z in f),
             'pngBytes': sum(Path(z['screenshotPath']).stat().st_size for z in f)}
        if inventory_only:
            c['status'] = 'inventory_only_no_proposed_requests'
        elif sensitive:
            c['status'] = 'blocked_sensitive_raw_frame_do_not_transmit'
        else:
            c['status'] = 'prepared_pending_full_screenshot_review' if preflight else 'prepared_pending_visual_privacy_review_and_remote_preflight'
            triplet = [build_prompt(x,b,f,interval,v,instruction) for v in VARIANTS]
            for key in ('querySHA256','backgroundSHA256','priorWritesSHA256'):
                require(len({p[key] for p in triplet}) == 1, f'Unmatched common conditioning: {key}')
            require(triplet[1]['frameRecordIDs'] == triplet[2]['frameRecordIDs'], 'OCR/image frame mismatch')
            for p in triplet:
                require(not privacy.unsafe(''.join(z.get('text','') for z in p['parts'])), 'Sensitive model text')
                prompts.append(p)
        candidates.append(c)
        print(f"Prepared case {e['case']}: {len(f)} frames; {c['status']}",flush=True)
    require(scored_directory(review) == source_scoring, 'Scoring revision changed during preparation; rerun')
    for path,digest in source_digests.items():
        require(file_hash(path) == digest, f'Source changed during preparation: {path}')
    output.mkdir(mode=0o700, parents=True)
    dump_rows(output/'cases.jsonl', candidates)
    dump_rows(output/'frames.jsonl', sorted(images.values(),key=lambda x:(x['capturedAt'],x['recordID'])))
    dump_rows(output/'prompts.local.jsonl', prompts)
    requests = [{'case':p['case'],'exampleID':p['exampleID'],'variant':p['variant'],'replicate':rep,
                 'partsSHA256':p['partsSHA256'],'model':MODEL} for rep in (1,) for p in prompts]
    requests.sort(key=lambda r:fingerprint([17,r]))
    for i,r in enumerate(requests,1):r['requestOrdinal']=i
    dump_rows(output/'requests.proposed.jsonl', requests)
    plan = {'version':VERSION,'status':'local_preparation_only_not_authorized_for_transmission',
        'model':MODEL,'reasoningEffort':'xhigh','temperature':'omitted_as_in_prior_subscription_runs',
        'variants':list(VARIANTS),'cases':len(candidates),'proposedPersonalRequests':len(requests),
        'replicatesPerArm':1,'selectionSeed':17,'strata':dict(Counter(c['selectionStratum'] for c in candidates)),
        'inventoryOnly':inventory_only,'ocrSidecar':backfill_provenance,
        'applications':dict(Counter(c['application'] for c in candidates)),
        'knownConstructionCasesExcludedBeforeSelection':[353,354], 'forcedDiagnosticCase':283,
        'selectionMeaning':'All changed and both-pass Astra original-answer cases plus deterministic both-fail sample, forcing #283, to reach 72. Diagnostic, not representative full-corpus accuracy.',
        'hypothesisScope':'Astra-only, recent evidence representation. Cannot establish that stronger models generally need less cleanup, whole-history vision benefit, or continual-learning performance.',
        'scoringContract':{'passBar':agreed_scoring['passBar'],
            'clarifications':agreed_scoring.get('passBarClarifications',[]),
            'blindedToInputVariant':True,'constructionAuditSeparateFromPredictionGrade':True,
            'invalidPredictions':'Retain and score as failures; no result-driven exclusions or silent retries.'},
        'priorFreshAnswerResults':{'AstraOld':[61,140],'AstraNew':[58,140],'SolOld':[22,140],'SolNew':[24,140],
            'interpretation':'Small observed differences motivate investigation; not established cleanup damage or a general capability effect.'},
        'source':str(source),'sourceScoring':str(source_scoring),'sourceScoringSHA256':file_hash(source_scoring/'completion.json'),
        'sourceArtifactSHA256':source_digests,
        'sourceRawDigests':original['rawDigests'],
        'contextPolicy':{'olderBackground':'Actual frozen new-pipeline retained blocks before the common recent boundary, identical in every arm.',
            'recentInterval':'Five minutes preceding the last causal screenshot in the baseline retained history interval; capped by shortening its earliest end only. Long idle gaps are retained and reported.',
            'frameSelection':'All screenshot-backed screen/visual frame observations in the chosen interval. No pane/novelty/target-text selection or fuzzy/exact image deduplication.',
            'imagePolicy':'Unmodified full retained screenshot, detail original. No added crops or highlights. These are captured windows, not a recording of everything on all displays.',
            'ocrPolicy':'First recorded OCR of the exact screenshot, before offline cleanup. Missing observations are completed locally with the same full-window Vision settings in a hash-bound sidecar; original capture time is preserved separately from replay processing time. No missing OCR permitted in executable prompts.',
            'cleanedControl':'Current frozen cleaned READ serialization; not the old pipeline. Recent items are availability-ordered in every arm; original recent-order differences are recorded. Fresh controls required.',
            'historicalWrites':'Exact same serialized closed-WRITE content and relative WRITE order as frozen new baseline; unchanged across arms.',
            'query':'Exact frozen onset query, unchanged; target never supplied.',
            'causality':'Every raw image must have capturedAt strictly before targetBeganAt; later OCR processing is not later pixels.',
            'maxFrames':MAX_FRAMES,'maxRaw32pxPatches':MAX_PATCHES,'maxPNGBytes':MAX_IMAGE_BYTES,
            'interpretationLimit':'Raw OCR and images share frame selection and source/timing metadata. Cleaned READs retain their existing semantic metadata and consolidation. This tests recent evidence representation, not visual modality alone.',
            'notFullHistoryVision':True,'notEqualTokenBudget':True},
        'provider':{'endpoint':'http://127.0.0.1:4000/v1/responses','path':'existing LiteLLM ChatGPT/Codex subscription',
            'apiKeyFallback':False,'modelFallback':False,'tools':[],'stream':True,
            'remoteImagePreflight':preflight or 'not_performed','imageDetailOriginalServerSupport':'verified_for_1_and_8_synthetic_1525x1318_images' if preflight else 'must_verify_for_Astra',
            'runtimeMustBeFrozenBeforeExecution':True,'concurrency':1,'pauseAfterConsecutiveServerErrors':2},
        'usage':{'textCharactersByArm':{v:sum(p['textCharacters'] for p in prompts if p['variant']==v) for v in VARIANTS},
            'imagePresentations':sum(p['imageCount'] for p in prompts),'uniqueImages':len(images),
            'distinctFrameRecords':len(images),'distinctImageSHA256':len({f['screenshotSHA256'] for f in images.values()}),
            'rawPatchGridIsNotBillableTokens':True,
            'imageTokenEstimate':'Synthetic 1525x1318 images measured approximately 2420 tokens each; other geometries and full personal requests remain unmeasured.' if preflight else 'unverified_pending_small_remote_preflight',
            'apiEquivalentUSDPerMillion':original['measurementContract']['pricesUSDPerMillion'][MODEL],
            'actualLatencyUsageAndResponsesMustBeRetained':True},
        'privacy':{'textAndKnownCredentialLineageScan':'performed; candidates with known flagged frames have no proposed requests',
            'flaggedCases':[c['case'] for c in candidates if c['sensitiveFrameRecordIDs']],
            'visualReview':'pending; raw screenshots may contain private content outside the prior cleaned pane',
            'transmissionGate':'explicit review/approval of full screenshots and frozen execution plan required'},
        'gates':['local synthetic wire-preservation test','visual screenshot privacy review','separate non-personal remote image canary','final usage/authorization gate','versioned blinded scoring with the agreed intention-level bar'],
        'providerCalls':0,'training':False,'peakProcessMiB':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024**2,
        'implementationSHA256':{name:file_hash(Path(__file__).with_name(name)) for name in
            ('prepare-phase1-vision-pilot.py','check-phase1-vision-pilot.py',
             'phase1_read_model_comparison.py','phase1_repeatability_review.py','serve-phase1-model-comparison.py')}}
    dump(output/'plan.json',plan)
    lines=['# Astra recent-READ modality pilot — local preparation only','',
        'No model requests were sent. This is a recent-window diagnostic, not a whole-workday image-context test.','',
        f"{len(candidates)} cases; {len(requests)} proposed predictions; {len(images)} distinct frame records. Every arm retains the same earlier background, closed WRITE content and onset query.",'',
        'The cleaned control must be sampled afresh. Do not compare changed recent ordering against an old answer as though its input were identical.','',
        ('The synthetic remote image canary passed. Full-window screenshot privacy review/approval remains outstanding.' if preflight
         else 'Full-window screenshot privacy review and a remote image canary are outstanding.')
        + ' OCR secret scans are not a complete visual privacy review.','']
    for c in candidates:
        lines += [f"## Case {c['case']} — {c['application']} — {c['selectionStratum']}",'',c['target'],'',
            f"Status: {c['status']}. Frames: {len(c['frameRecordIDs'])}. Latest frame age: {c['interval']['latestFrameAgeSeconds']:.3f}s. Size-limited interval: {c['interval']['sizeLimitShortenedInterval']}.",'',
            f"[Existing review](http://127.0.0.1:8788/#case={c['case']}&view=repeatability)",'']
        for ident in c['frameRecordIDs']:
            f=images[ident];lines.append(f"- {f['capturedAt']} — [full screenshot](<{f['screenshotPath']}>) — raw line {f['rawLine']}")
        lines.append('')
    (output/'review.md').write_text('\n'.join(lines)+'\n')
    dump(output/'artifact-hashes.json',{p.name:file_hash(p) for p in sorted(output.iterdir()) if p.is_file()})
    print(json.dumps({'status':plan['status'],'output':str(output),'cases':len(candidates),'proposedRequests':len(requests),'uniqueFrames':len(images),'privacyFlaggedCases':plan['privacy']['flaggedCases'],'peakMiB':plan['peakProcessMiB'],'providerCalls':0}))


if __name__ == '__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--ocr-sidecar',type=Path)
    ap.add_argument('--inventory-only',action='store_true')
    ap.add_argument('--image-preflight',type=Path)
    args=ap.parse_args()
    prepare(args.source,args.output.resolve(),args.ocr_sidecar,args.inventory_only,args.image_preflight)

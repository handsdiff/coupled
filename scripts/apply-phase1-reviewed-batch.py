#!/usr/bin/env python3
"""Apply a finite, manually reviewed curation batch; never alter raw collection.

This is deliberately not another automatic episode/OCR policy. Reviews identify
exact raw observations and explain editorial judgments. The resulting manifest
binds all evidence and the input corpus. Unreviewed records remain byte-identical.
"""
import argparse
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import resource
import sys

from phase1_example_storage import write_examples

ROOT = Path(__file__).resolve().parents[1]
VERSION = "phase1-reviewed-batch-v2"


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / 'scripts' / (name + '.py'))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


h = load('curate-phase1-episode-joins')
p = load('construct-phase1-closed-episode-corpus')


def digest(value):
    return hashlib.sha256(p.canonical_json(value).encode()).hexdigest()


def endpoint(record, source):
    if source == 'return':
        h.require(len(record.get('returnCheckpoints', [])) == 1, 'Ambiguous Return endpoint')
        value = record['returnCheckpoints'][0]['observation']
        h.require(not record['returnCheckpoints'][0].get('axErrors'), 'Return AX error')
    else:
        h.require(source in {'before', 'after'}, 'Unsupported observation')
        value = record.get(source)
        h.require(not record.get(source + 'AXErrors'), 'AX endpoint error')
    h.require(isinstance(value, dict) and isinstance(value.get('value'), str) and not value.get('valueWasTruncated'), 'Missing/truncated endpoint')
    return value


def verify_continuous_target(records):
    first = endpoint(records[0], 'before')
    h.require(first['value'] in ('', '\nDo anything'), 'No empty onset')
    identity = records[0]['targetIdentity']
    clipboard = records[0]['conditioningState']['clipboard']
    previous = None
    for i, r in enumerate(records):
        h.require(r['targetIdentity'] == identity, 'Target editable changed')
        h.require(r['conditioningState']['clipboard']['changeCount'] == clipboard['changeCount'], 'Clipboard changed during target')
        h.require(not r.get('pasteCheckpoints') and set(r['inputHints']) <= {'typed', 'delete', 'return'}, 'Unproven target authorship')
        h.require(not r.get('tapTimeoutCountDuringBurst'), 'Tap timeout')
        if previous is not None:
            h.require(endpoint(r, 'before')['value'] == previous, 'Discontinuous target')
        if i < len(records) - 1:
            h.require(not r.get('returnCheckpoints'), 'Intermediate submission')
            previous = endpoint(r, 'after')['value']
        else:
            final = endpoint(r, 'return')
            h.require(r['inputEvents'][-1]['hint'] == 'return', 'Input after submission')
            h.require(r['returnCheckpoints'][0]['eventTimestampNanoseconds'] == r['lastEventTimestampNanoseconds'], 'Wrong terminal Return')
    return final['value']


def verify_persistent_insertion(records):
    """Prove a reviewed note insertion, including local revisions, from AX states.

    This is not a new automatic closure rule. The sidecar must still establish
    closure and READ novelty. Numeric AX offsets are not used to bias the diff.
    """
    algorithm = load('construct-phase1-raw-episode-corpus')
    first = endpoint(records[0], 'before')
    last = endpoint(records[-1], 'after')
    edit = algorithm.minimal_edit(first['value'], last['value'])
    h.require(edit['operation'] == 'insert' and edit['content'], 'Not one surviving insertion')
    start = edit['characterOffset']
    prefix, suffix = first['value'][:start], first['value'][start:]
    cursor = records[0]['conditioningState']['cursorContext']
    normal = algorithm.normalized_text
    h.require(not cursor.get('selectedText'), 'Selected old text is not an insertion')
    h.require(bool(normal(cursor.get('leftContext', ''))) and
              normal(prefix).endswith(normal(cursor['leftContext'])) and
              normal(suffix).startswith(normal(cursor.get('rightContext', ''))), 'Semantic onset mismatch')
    previous = first['value']
    for r in records:
        h.require(r['targetIdentity'] == records[0]['targetIdentity'], 'Target editable changed')
        h.require(all(r['conditioningState']['clipboard'].get(k) == records[0]['conditioningState']['clipboard'].get(k)
                      for k in ('changeCount', 'textSHA256')), 'Clipboard changed')
        h.require(not r.get('pasteCheckpoints') and set(r['inputHints']) <= {'typed', 'delete', 'return'}, 'Unproven authorship')
        h.require(not r.get('tapTimeoutCountDuringBurst'), 'Tap timeout')
        h.require(endpoint(r, 'before')['value'] == previous, 'Discontinuous target')
        after = endpoint(r, 'after')
        h.require(after['observedAt'] >= r['lastInputAt'], 'Terminal observation precedes final input')
        previous = after['value']
        h.require(previous.startswith(prefix) and previous.endswith(suffix) and
                  len(previous) >= len(prefix) + len(suffix), 'Edit escaped composition span')
    h.require(algorithm.apply_edit(first['value'], edit) == last['value'], 'Final transition does not reconstruct')
    return algorithm.normalize_authored_content(edit['content']), edit


def make_batch(source, spec):
    h.require(spec['version'] in {'phase1-manual-composition-batch-v1', 'phase1-manual-composition-batch-v2'}, 'Unsupported review version')
    h.require(spec['reviewedBy'] == 'assistant_raw_and_screenshot_review', 'Missing adjudicator')
    writes = list(h.rows(source / 'common-write-events.jsonl'))
    examples = list(h.rows(source / 'cohort.jsonl'))
    by_example_event = {r['targetEventID']: r for r in examples}
    original = {}
    for i, r in enumerate(h.rows(ROOT / 'coupled-data/sep02-10-training-prep-20260910/paired-qwen38-reference32k/cohort.jsonl'), 1):
        original[i] = r['targetEventID']
    by_write = {e['sourceEventID']: e for e in writes}
    new_reads = {r['sourceEventID']: r for r in h.rows(source / 'new/events.jsonl') if r['kind'] == 'read'}
    needed = {}
    frames = {}
    for r in spec['writes']:
        needed.setdefault(r['day'], set()).update(r['lines'] + r['images'])
    for r in spec['reads']:
        frames.setdefault(r['day'], set()).add(r['frameID'])
    raw, raw_ids, evidence_hashes, locations = {}, {}, {}, {}
    for day in sorted(needed.keys() | frames.keys()):
        path = ROOT / f'coupled-data/phase1-ordinary-work-2026-09-{day:02d}-1/raw.jsonl'
        evidence_hashes[str(path)] = h.sha(path)
        all_attempts = set()
        for line, r in enumerate(h.rows(path), 1):
            if 'inputEvents' in r:
                all_attempts.add(r['recordID'])
            if line in needed.get(day, set()) or r.get('recordID') in frames.get(day, set()):
                raw[day, line] = r
                locations[r['recordID']] = (day, line)
        raw_ids[day] = all_attempts
    def image_evidence(day, line):
        r = raw[day, line]
        path = ROOT / f'coupled-data/phase1-ordinary-work-2026-09-{day:02d}-1' / r['screenshotRelativePath']
        h.require(h.sha(path) == r['screenshotSHA256'], 'Screenshot changed')
        evidence_hashes[str(path)] = r['screenshotSHA256']
        return {'recordID': r['recordID'], 'rawLine': line, 'capturedAt': r['capturedAt'],
                'path': str(path.relative_to(ROOT)), 'sha256': r['screenshotSHA256']}
    changes, retired_all, retired_examples = [], set(), set()
    for review in spec['writes']:
        day = review['day']
        attempts = [raw[day, line] for line in review['lines']]
        h.require(review['lines'] == sorted(set(review['lines'])), 'Repeated/unordered raw members')
        h.require(len({r['sessionID'] for r in attempts}) == 1, 'Cross-session composition')
        def logical_field(r):
            i = r['targetIdentity']
            label = i.get('fieldDescription', '')
            if label.startswith('Terminal '):
                label = label.split(',')[0]
            return i['bundleIdentifier'], i['processIdentifier'], i['role'], label
        h.require(len({logical_field(r) for r in attempts}) == 1, 'Different receiving editables')
        ids = {r['recordID'] for r in attempts}
        old = [w for w in writes if ids.intersection(w['sourceRecordIDs'])]
        h.require(old, 'No historical members')
        h.require(all((set(w['sourceRecordIDs']) & raw_ids[day]) <= ids for w in old), 'Review clips an existing episode')
        retired = {w['sourceEventID'] for w in old}
        h.require(not retired_all.intersection(retired), 'Overlapping adjudications')
        retired_all.update(retired)
        retired_e = {r['exampleID'] for r in examples if r['targetEventID'] in retired}
        retired_examples.update(retired_e)
        template = deepcopy(by_example_event[review['templateEventID'] if 'templateEventID' in review else original[review['templateCase']]])
        images = [image_evidence(day, line) for line in review['images']]
        selected = []
        content = ''
        for part in review['contentParts']:
            if 'line' in part:
                h.require(part['line'] in review['lines'], 'Unbound content observation')
                r = raw[day, part['line']]
                value = endpoint(r, part['observation'])
                text = value['value']
                prefix = part.get('removePrefix')
                if prefix:
                    h.require(text.startswith(prefix), 'Reviewed stale prefix changed')
                    text = text[len(prefix):]
                selected.append({'recordID': r['recordID'], 'observationID': value['observationID'],
                                 'observedAt': value['observedAt'], 'valueSHA256': digest(value['value'])})
            elif 'historyCase' in part:
                payload = json.loads(by_write[original[part['historyCase']]]['serialized'])
                text = ''.join(s['content'] for s in payload['authorshipSegments'])
            else:
                h.require(images and not review['eligible'], 'Manual text needs visual evidence and history-only disposition')
                text = part['literal']
            content += text
        persistent = review.get('reconstruction') == 'continuous_persistent_insertion'
        edit = None
        if persistent:
            h.require(not review['contentParts'] and review['eligible'], 'Persistent insertion must be reconstructed, not supplied')
            content, edit = verify_persistent_insertion(attempts)
            selected = [{'recordID':r['recordID'], 'beforeObservationID':endpoint(r,'before')['observationID'],
                         'afterObservationID':endpoint(r,'after')['observationID'],
                         'beforeSHA256':digest(endpoint(r,'before')), 'afterSHA256':digest(endpoint(r,'after'))} for r in attempts]
        h.require(len(content.strip()) >= 4, 'Empty reviewed composition')
        onset = attempts[0]['beganAt']
        available = max([r['terminalDecisionAt'] for r in attempts] + [r['capturedAt'] for r in images])
        non_novel = review.get('nonNovelReads', [])
        interval = [r for r in new_reads.values() if r['sessionID'] == attempts[0]['sessionID'] and onset < r['availableAt'] < available]
        outside = [w['sourceEventID'] for w in writes if w['sourceEventID'] not in retired and w['sessionID'] == attempts[0]['sessionID']
                   and w['beganAt'] < available and w['availableAt'] >= onset]
        if review['eligible']:
            if not persistent:
                h.require(content == verify_continuous_target(attempts), 'Target is not exact final authored state')
            h.require(not outside, 'Outside WRITE in target interval')
            h.require(set(non_novel) == {r['sourceEventID'] for r in interval}, 'Unreviewed READ in target interval')
            if persistent:
                from phase1_read_boundary import ReadBoundaryEvidence
                assessor = ReadBoundaryEvidence(list(new_reads.values()))
                assessments = []
                for read in interval:
                    prior = [r for r in attempts if endpoint(r,'after')['observedAt'] < read['availableAt']]
                    h.require(prior, 'No pre-READ composition state')
                    algorithm = load('construct-phase1-raw-episode-corpus')
                    draft = algorithm.normalize_authored_content(algorithm.minimal_edit(endpoint(attempts[0],'before')['value'], endpoint(prior[-1],'after')['value'])['content'])
                    result = assessor.assess(read, onset, draft, template['modelFacingDestination']['application'],
                                             initial_field=endpoint(attempts[0],'before')['value'])
                    h.require(result['status'] in h.NON_NOVEL, 'Novel or unexplained READ')
                    assessments.append(result)
                h.require(review.get('closureReason') and review.get('closureEventID') in new_reads.keys() | by_write.keys(), 'Unbound closure review')
                closure = (new_reads | by_write)[review['closureEventID']]
                h.require(closure.get('beganAt', closure['availableAt']) > available, 'Closure witness is not subsequent')
            else:
                h.require(review.get('priorReadEvidence'), 'Missing prior-view review')
                h.require(all(new_reads[eid]['availableAt'] < onset for eid in review['priorReadEvidence']), 'Future prior-view evidence')
        if review.get('historySegmentsFromCases'):
            segments = [s for case in review['historySegmentsFromCases'] for s in json.loads(by_write[original[case]]['serialized'])['authorshipSegments']]
            h.require(''.join(s['content'] for s in segments) == content, 'Paste history differs from reviewed composition')
        else:
            segments = [{'type': 'unresolved_authorship' if review.get('mixedAuthorshipUnresolved') else 'authored_text', 'content': content}]
        proof = {'version': VERSION, 'review': review, 'selectedObservations': selected, 'images': images,
                 'rawRecords': [{'line': line, 'recordID': raw[day, line]['recordID'], 'sha256': digest(raw[day, line])} for line in review['lines']],
                 'interveningReads': [{'eventID': r['sourceEventID'], 'sha256': digest(r)} for r in interval],
                 'priorReadEvidence': [{'eventID': eid, 'sha256': digest(new_reads[eid])} for eid in review.get('priorReadEvidence', [])],
                 'outsideWritesRetained': outside, 'evidenceAvailableAt': available,
                 'pasteObservations': [c for r in attempts for c in r.get('pasteCheckpoints', [])]}
        if persistent:
            proof.update(observedNetEdit=edit, readAssessments=assessments,
                         closureWitness={'event':closure,'sha256':digest(closure)})
        member_ids = sorted({mid for w in old for mid in w['memberWriteEventIDs']})
        event_id = p.stable_id('evt_', {'reviewedRawComposition': sorted(ids)})
        event = deepcopy(min(old, key=lambda w: w['beganAt']))
        compact = json.loads(event['serialized'])
        compact.update(operation='closed_composition_episode', authorshipSegments=segments,
                       authorshipResolution='unresolved' if review.get('mixedAuthorshipUnresolved') else 'resolved')
        compact.pop('content', None)
        event.update(sourceEventID=event_id, beganAt=onset, availableAt=available,
                     sourceRecordIDs=sorted(set().union(*(set(w['sourceRecordIDs']) for w in old), ids, {r['recordID'] for r in images})),
                     memberWriteEventIDs=member_ids, memberCount=len(member_ids),
                     episodeID=p.stable_id('episode_', {'reviewedRawComposition': sorted(ids)}),
                     episodeDecision='closed_loss_episode' if review['eligible'] else 'history_only_episode',
                     lossEligibility='eligible' if review['eligible'] else 'ineligible',
                     conversionVersion=VERSION, closureStatus='reviewed_complete_composition',
                     serialized=p.canonical_json(compact), auditSerialized=p.canonical_json({**compact, 'batchReview': proof}),
                     curation=proof)
        if not review['eligible']:
            event['targetExclusionReason'] = review['reason']
        new_example = None
        if review['eligible']:
            conditioning = deepcopy(attempts[0]['conditioningState'])
            new_example = template
            new_example.update(exampleID='reviewed_example_' + event_id, targetEventID=event_id,
                target={'schemaVersion': 1, 'resolvedContent': content, 'segments': segments},
                query=p.serialize_query(conditioning, event['modelFacingDestination']), conditioningState=conditioning,
                targetBeganAt=onset, targetAvailableAt=available, targetSourceRecordIDs=event['sourceRecordIDs'],
                targetUnitID=event['episodeID'], cursorFidelity=None, conversionVersion=VERSION,
                episode={'memberWriteEventIDs': member_ids, 'memberCount': len(member_ids), 'decision': 'closed_loss_episode',
                         'closureReason': 'reviewed_complete_submission', 'episodeVersion': VERSION,
                         'onsetEvidence': {'requiresProvenPromptOnset': True, 'promptOnsetProven': True, 'proofReason': 'raw_empty_prompt_before_first_mutation'}},
                targetMetadata={'memberWriteEventIDs':member_ids, 'availableAt':available}, batchCuration=proof)
            new_example['targetText'] = content
            if persistent:
                # Keep the already audited semantic query exactly, not the raw
                # AX numeric coordinates or a later cursor position.
                new_example['query'] = by_example_event[review['templateEventID']]['query']
                new_example['conditioningState'] = deepcopy(by_example_event[review['templateEventID']]['conditioningState'])
                new_example['episode']['closureReason'] = review['closureReason']
                new_example['episode']['onsetEvidence'] = deepcopy(by_example_event[review['templateEventID']]['episode']['onsetEvidence'])
                new_example['episode']['onsetEvidence']['proofReason'] = 'continuous_raw_insertion_semantic_anchor'
        changes.append({'name':review['name'], 'retiredWriteEventIDs':sorted(retired), 'retiredExampleIDs':sorted(retired_e),
                        'event':event, 'example':new_example, 'targetEligible':review['eligible'], 'proof':proof})
    read_changes = []
    for review in spec['reads']:
        event = new_reads[review['eventID']]
        day, line = locations[review['frameID']]
        image = image_evidence(day, line)
        h.require(image['capturedAt'] <= event['availableAt'], 'READ repair uses future screenshot')
        updated, _ = load('assemble-phase1-curated-review').patch_read(event, [(a,b,1) for a,b in review['edits']])
        updated['curation'] = {'version':VERSION, 'review':review, 'image':image}
        read_changes.append({'eventID':event['sourceEventID'], 'beforeSHA256':digest(event), 'event':updated, 'review':review, 'image':image})
    return changes, read_changes, evidence_hashes


def render(source, output, spec, changes, reads, hashes):
    old_examples = list(h.rows(source / 'cohort.jsonl'))
    old_writes = list(h.rows(source / 'common-write-events.jsonl'))
    retired = {eid for c in changes for eid in c['retiredWriteEventIDs']}
    retired_examples = {eid for c in changes for eid in c['retiredExampleIDs']}
    examples = [r for r in old_examples if r['exampleID'] not in retired_examples] + [c['example'] for c in changes if c['example']]
    examples.sort(key=lambda r:(r['targetBeganAt'],r['exampleID']))
    examples = [{**r, 'chronologicalOrdinal':i, 'experimentBlockID':f'block-{i//50+1:04d}'} for i,r in enumerate(examples)]
    writes = [w for w in old_writes if w['sourceEventID'] not in retired] + [c['event'] for c in changes]
    members = [mid for w in writes for mid in w['memberWriteEventIDs']]
    h.require(len(members) == len(set(members)), 'Duplicate micro-WRITE lineage')
    counts = {}
    output.mkdir(parents=True, exist_ok=False)
    h.save_rows(output/'common-write-events.jsonl',writes)
    h.save_rows(output/'cohort.jsonl', examples)
    h.save_rows(output/'batch-changes.jsonl',changes)
    h.save(output/'batch-read-changes.json',reads)
    h.save(output/'batch-reviews.json',spec)
    # Keep earlier independent adjudications auditable; the batch does not
    # replace or reinterpret them.
    for name in ('target-corrections.json', 'read-corrections.json'):
        h.save(output/name,json.loads((source/name).read_text()))
    h.save_rows(output/'prompt-restorations.jsonl',h.rows(source/'prompt-restorations.jsonl'))
    from phase1_read_model_comparison import Privacy
    policy = json.loads((ROOT/'coupled-data/clean-read-closed-write-review-20260911-r2/review.json').read_text())['privacyPolicy']
    privacy_evidence = {e['sourceEventID']:e for e in h.rows(source/'new/events.jsonl')}
    privacy_evidence.update({e['sourceEventID']:e for e in writes})
    privacy = Privacy(privacy_evidence,policy)
    for r in examples:
        h.require(not privacy.unsafe(r['query']+json.dumps(r['target'])), 'Unsafe curated target/query')
    suppress = {eid for c in changes for eid in c['proof']['review'].get('nonNovelReads',[])}
    for arm in ('old','new'):
        path = output/arm; path.mkdir()
        original = list(h.rows(source/arm/'events.jsonl'))
        retained_reads = [e for e in original if e['kind']=='read' and not (arm=='new' and e['sourceEventID'] in suppress)]
        read_by_id = {c['eventID']:c['event'] for c in reads} if arm=='new' else {}
        retained_reads = [read_by_id.get(e['sourceEventID'],e) for e in retained_reads]
        # Compact arm artifacts omit raw/audit provenance, as existing artifacts do.
        events = retained_reads + privacy.filter_stream(writes)
        mapping = {e['sourceEventID']:e for e in events}
        blocks = list(h.rows(source/arm/'context-blocks.jsonl'))
        blocks = [b for b in blocks if b['contextBlockID'] not in retired and not (arm=='new' and b['contextBlockID'] in suppress)]
        for b in blocks:
            if b['contextBlockID'] in mapping:
                e = mapping[b['contextBlockID']]; b.update(serialized=e['serialized'],availableAt=e['availableAt'])
        for c in changes:
            e = mapping[c['event']['sourceEventID']]
            b = {'contextBlockID':e['sourceEventID'],'contextBlockType':'semantic_event','sourceEventID':e['sourceEventID'],
                 'availableAt':e['availableAt'],'sessionID':e['sessionID'],'serialized':e['serialized']}
            i = next((i for i,old in enumerate(blocks) if (old.get('availableAt') or old['beforeAt'])>e['availableAt']),len(blocks))
            blocks.insert(i,b)
        if arm=='new':
            blocks.sort(key=lambda b:((b.get('availableAt') or b['beforeAt']),b['contextBlockID']))
        h.require(len({b['contextBlockID'] for b in blocks})==len(blocks), 'Duplicate context blocks')
        h.save_rows(path/'events.jsonl',events); h.save_rows(path/'context-blocks.jsonl',blocks)
        def stream():
            for original_row in examples:
                r = deepcopy(original_row)
                selected = [b for b in blocks if (b.get('availableAt') or b['beforeAt'])<r['targetBeganAt']]
                ids = [b['contextBlockID'] for b in selected]
                h.require(not ({r['targetEventID']}|set(r['episode']['memberWriteEventIDs'])).intersection(ids), 'Target leakage')
                r['contextBlockIDs']=ids; r['contextEventIDs']=[eid for eid in ids if eid in mapping]
                r['contextSourceRecordIDs']=sorted({rid for eid in r['contextEventIDs'] for rid in mapping[eid]['sourceRecordIDs']})
                r['sourceRecordIDs']=sorted(set(r['targetSourceRecordIDs']+r['contextSourceRecordIDs']))
                r['context']='\n'.join(b['serialized'] for b in selected)
                r['modelInput']=(r['context']+'\n' if r['context'] else '')+r['query']
                r.pop('_sharedContext',None)
                yield r
        storage=write_examples(path/'examples.jsonl',stream(),compact=True)
        counts[arm]={'reads':len(retained_reads),'writes':len(writes),'examples':len(examples),'exampleStorage':storage}
    h.save(output/'review.json',{'version':VERSION,'trainingFreezeApproved':False,'parentCorpus':str(source.resolve()),
        'counts':counts,'sourceHashes':hashes,'sameTargetsQueriesMasksAndWriteHistoryAcrossArms':True,
        'retiredTargets':len(retired_examples),'newTargets':sum(c['targetEligible'] for c in changes),
        'historyOnlyCompositions':sum(not c['targetEligible'] for c in changes),'suppressedNonNovelReadsNewArm':sorted(suppress),
        'readRepairsNewArmOnly':len(reads),'rawAndCollectorUnchanged':True,
        'artifactsSHA256':{str(f.relative_to(output)):h.sha(f) for f in output.rglob('*.jsonl')},
        'targetCorrectionsSHA256':h.sha(output/'target-corrections.json'),
        'promptRestorationsSHA256':h.sha(output/'prompt-restorations.jsonl'),
        'batchReadChangesSHA256':h.sha(output/'batch-read-changes.json'), 'batchReviewsSHA256':h.sha(output/'batch-reviews.json')})
    print(json.dumps({'counts':counts,'retiredTargets':len(retired_examples),'newTargets':sum(c['targetEligible'] for c in changes),
                      'peakRSSMiB':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/(1024**2 if sys.platform=='darwin' else 1024)},indent=2))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True); parser.add_argument('--reviews',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    manifest=json.loads((args.input/'review.json').read_text())
    for name,sha in manifest['artifactsSHA256'].items():
        h.require(h.sha(args.input/name)==sha,'Input corpus changed: '+name)
    spec=json.loads(args.reviews.read_text())
    changes,reads,hashes=make_batch(args.input,spec)
    for f in [args.input/'review.json',args.reviews,Path(__file__),ROOT/'scripts/assemble-phase1-curated-review.py',ROOT/'scripts/construct-phase1-closed-episode-corpus.py',ROOT/'scripts/construct-phase1-raw-episode-corpus.py',ROOT/'scripts/phase1_read_boundary.py']:
        hashes[str(f.resolve())]=h.sha(f)
    render(args.input,args.output,spec,changes,reads,hashes)


if __name__=='__main__':
    main()

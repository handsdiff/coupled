#!/usr/bin/env python3
"""Read-only, bounded-memory audit of the v26 READ candidate and frozen targets.

This is a diagnostic, never a reducer or a source of training adjudications.
It checks the known case set against the actual new events and separately
measures changes caused by the blacklist removal and native ordering.
"""
import argparse
from collections import Counter, defaultdict
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import resource
import sys

from phase1_read_boundary import ReadBoundaryEvidence

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'coupled-data'
PREP = DATA / 'sep02-10-training-prep-20260910'
CAUSES = DATA / 'sep02-10-fidelity-causes-20260911'
CURRENT = DATA / 'clean-read-closed-write-review-20260911-r2'


def rows(p):
    with p.open() as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def read(p):
    return json.loads(p.read_text())


def save(p, v):
    with p.open('x') as f:
        json.dump(v, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write('\n')


def sha(p):
    h = hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def memory():
    mib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 ** 2 if sys.platform == 'darwin' else 1024)
    if mib > 1800:
        raise MemoryError('Audit exceeded 1.8 GiB; sources remain untouched')
    return mib


def root_for(day):
    return DATA / ('clean-read-v26-adjudicated-20260911' if day == 4 else 'clean-read-v26-native-20260911')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--replay', type=Path, help='Alternative completed v26 replay for a separately versioned READ-stage audit')
    p.add_argument('--surfaces', type=Path, help='Surface evidence root paired with --replay')
    args = p.parse_args()
    assert bool(args.replay) == bool(args.surfaces), 'Replay and surface evidence must be specified together'
    if args.replay:
        args.replay, args.surfaces = args.replay.resolve(), args.surfaces.resolve()
    replay_root = lambda day: args.replay if args.replay else root_for(day)
    args.output.mkdir(parents=True, exist_ok=False)
    frozen = read(CAUSES / 'stage-evidence.json')
    annotations = read(CAUSES / 'review-annotations.json')
    current = {e['sourceEventID']: e for e in rows(CURRENT / 'events.jsonl')}
    cohort = list(rows(PREP / 'paired-qwen38-reference32k/cohort.jsonl'))
    cases = {r['case']: r for r in frozen['cases']}
    observations, panes, micro = [], [], []
    reduction_changes = []
    retained_session_lines = Counter()
    restored_examples = defaultdict(list)
    native_deltas = []
    per_day = {}
    inputs = {}
    selection_changes = []
    for day in [2, 3, 4, 7]:
        new_dir = replay_root(day) / f'sep{day}-phase1-semantic-v26'
        old_dir = (PREP / 'new-sep7-reduced' if day == 7 else
                   DATA / f'sep02-04-semantic-v25-review-20260907-r4/sep{day}-reduced')
        filter_dir = DATA / f'clean-read-v26-check-20260911-r3/sep{day}-phase1-semantic-v26'
        baseline = {r['eventID']: r for r in rows(old_dir / 'events.jsonl')}
        filtered = {r['eventID']: r for r in rows(filter_dir / 'events.jsonl')}
        final = {r['eventID']: r for r in rows(new_dir / 'events.jsonl')}
        before_writes = {k: {a: b for a, b in r.items() if a != 'sequence'} for k, r in baseline.items() if r['kind'] == 'write'}
        after_writes = {k: {a: b for a, b in r.items() if a != 'sequence'} for k, r in final.items() if r['kind'] == 'write'}
        assert before_writes == after_writes
        counts = Counter()
        for eid, r in final.items():
            if r['kind'] != 'read':
                continue
            if eid in baseline:
                assert r['sourceRecordIDs'] == baseline[eid]['sourceRecordIDs']
                if r['capturedAt'] != baseline[eid]['capturedAt']:
                    before = baseline[eid]
                    selected = r['reduction']['selectedObservationID']
                    assert selected in r['sourceRecordIDs']
                    selection_changes.append({'day': day, 'eventID': eid,
                        'beforeCapturedAt': before['capturedAt'], 'afterCapturedAt': r['capturedAt'],
                        'beforeSelectedObservationID': before['reduction']['selectedObservationID'],
                        'afterSelectedObservationID': selected,
                        'sourceRecordIDs': r['sourceRecordIDs'],
                        'beforeReconciliation': before['reduction'].get('observationReconciliation'),
                        'afterReconciliation': r['reduction'].get('observationReconciliation'),
                        'cohortTargetCutoffsCrossed': [c['exampleID'] for c in cohort if
                            min(before['capturedAt'], r['capturedAt']) < c['targetBeganAt'] <=
                            max(before['capturedAt'], r['capturedAt'])]})
            b, f = baseline.get(eid), filtered.get(eid)
            if b and f:
                removed = b.get('reduction', {}).get('semanticReadContent', {}).get('removedLines', [])
                for line in removed:
                    text = line.get('text', '')
                    if line['reason'] == 'stable_session_interface_text' and text and text in f['content']:
                        retained_session_lines[text] += 1
                        if len(restored_examples[text]) < 4:
                            restored_examples[text].append({'eventID': eid, 'day': day, 'capturedAt': r['capturedAt'],
                                                           'survivesFinal': text in r['content']})
                if b['content'] != f['content']:
                    counts['blacklistChangedContent'] += 1
            if f and r['content'] != f['content']:
                counts['nativeChangedContent'] += 1
                native_deltas.append({'day': day, 'eventID': eid, 'sourceRecordIDs': r['sourceRecordIDs'],
                                      'capturedAt': r['capturedAt'], 'before': f['content'], 'after': r['content'],
                                      'sameLineMultiset': Counter(f['content'].splitlines()) == Counter(r['content'].splitlines())})
            if not f:
                counts['addedAfterNative'] += 1
        counts['removedAfterNative'] = sum(eid not in final and r['kind'] == 'read' for eid, r in filtered.items())
        per_day[str(day)] = dict(counts, unchangedWrites=len(before_writes))
        for eid in set(filtered) - set(final):
            if filtered[eid]['kind'] == 'read':
                reduction_changes.append({'day': day, 'eventID': eid, 'before': filtered[eid]})
        for r in rows(replay_root(day) / f'sep{day}-causal/events.jsonl'):
            micro.append(r)
        pane_file = (args.surfaces if args.surfaces else DATA / 'native-order-evidence-20260911-r2') / f'sep{day}-surfaces/read-surfaces.jsonl'
        for r in rows(pane_file):
            panes.append({k: r[k] for k in ['sourceRecordID', 'capturedAt', 'content', 'evidenceID', 'screenshotSHA256']})
        raw_file = DATA / f'phase1-ordinary-work-2026-09-{day:02d}-1/raw.jsonl'
        for r in rows(raw_file):
            if r.get('recordType') in ['screen_ocr_observation', 'visual_ocr_observation']:
                observations.append({k: r.get(k) for k in ['recordID', 'recordType', 'sessionID', 'appName',
                    'capturedAt', 'content', 'windowID', 'contentWasTruncated', 'screenshotSHA256']})
        memory()
        for source in [new_dir / 'events.jsonl', old_dir / 'events.jsonl', filter_dir / 'events.jsonl', pane_file]:
            inputs[str(source.relative_to(ROOT))] = sha(source)
        print('Read session', day, 'peak MiB', round(memory()), flush=True)
    captured = {o['recordID']: o['capturedAt'] for o in observations}
    for change in selection_changes:
        assert captured[change['beforeSelectedObservationID']] == change['beforeCapturedAt']
        assert captured[change['afterSelectedObservationID']] == change['afterCapturedAt']
    save(args.output / 'selection-changes.json', selection_changes)
    index = ReadBoundaryEvidence(micro, observations, panes)
    lookup = {e['sourceEventID']: e for e in micro}
    by_raw = defaultdict(list)
    for e in micro:
        if e['kind'] == 'read':
            for rid in e['sourceRecordIDs']:
                by_raw[rid].append(e)
    boundary_results = []
    for b in frozen['boundaries']:
        candidate = frozen['candidates'][b['leftCandidateID']]
        initial = candidate['members'][0].get('beforeLogicalValue') or ''
        draft = b['baselineBoundary'].get('internalRevisionEvidence', {}).get('currentNetEdit', {}).get('content') or b['draft']
        selected = {}
        absent = []
        for old in b['reads']:
            eid = old['event']['sourceEventID']
            if eid in lookup:
                selected[eid] = lookup[eid]
            else:
                descendants = {e['sourceEventID']: e for rid in old['event']['sourceRecordIDs'] for e in by_raw[rid]}
                if not descendants:
                    absent.append(eid)
                selected.update(descendants)
        assessments = [index.assess(e, b['beganAt'], draft, b['destination']['application'], initial)
                       for e in sorted(selected.values(), key=lambda e: (e['availableAt'], e['sourceEventID']))]
        a = annotations['boundaries'][str(b['candidateLine'])]
        entry = {'candidateLine': b['candidateLine'], 'cases': [b['leftCase'], b['rightCase']],
                 'onset': b['beganAt'], 'nextOnset': b['nextBeganAt'], 'annotation': a,
                 'oldAssessments': [r['v10Assessment'] for r in b['reads']], 'newAssessments': assessments,
                 'readsRemoved': absent, 'proofNowComplete': all(r['boundaryEvidence']['unexplainedWords'] == 0 for r in assessments),
                 'currentReads': [{'eventID': eid, 'content': json.loads(e['serialized']).get('content', ''),
                                   'availableAt': e['availableAt'], 'sourceRecordIDs': e['sourceRecordIDs'],
                                   'includedInEpisodeHistory': eid in current} for eid, e in selected.items()]}
        boundary_results.append(entry)
        print('Boundary', b['candidateLine'], entry['cases'], [r['boundaryEvidence']['unexplainedWords'] for r in assessments], flush=True)
    save(args.output / 'boundaries.json', boundary_results)
    save(args.output / 'native-read-deltas.json', native_deltas)
    save(args.output / 'read-membership-changes.json', reduction_changes)
    save(args.output / 'restored-lines.json', [{'text': text, 'readCount': count, 'examples': restored_examples[text]}
         for text, count in retained_session_lines.most_common()])
    problem_cases = set()
    for b in boundary_results:
        if b['annotation']['disposition'] != 'retain_boundary':
            problem_cases.update(n for n in b['cases'] if n is not None)
    boundary_cases = sorted(problem_cases)
    prefix_cases = sorted(int(n) for n in annotations['prefixCases'] if n != '529')
    problem_cases.update(prefix_cases)
    problem_cases.update(annotations['oldTextIslandCases'])
    problem_cases.add(9)
    ledger = []
    for n in sorted(cases):
        c = cases[n]
        current_target = current[c['cohort']['targetEventID']]
        # Privacy projection deliberately strips rich audit-only properties.
        # Check actual historical payload, chronology, and raw lineage instead.
        for key in ['serialized', 'sourceRecordIDs', 'sourceEventID', 'availableAt', 'kind']:
            assert current_target[key] == c['episodeEvent'][key], (n, key)
        members = frozen['candidates'][c['candidateID']]['members']
        ledger.append({'case': n, 'exampleID': c['cohort']['exampleID'], 'targetEventID': c['cohort']['targetEventID'],
             'targetText': c['cohort']['targetText'], 'knownIssueStillPresent': n in problem_cases,
             'unresolvedHold': n == 529, 'boundaryGroups': c['boundaryGroups'],
             'prefixFinding': annotations['prefixCases'].get(str(n)),
             'oldTextIsland': n in annotations['oldTextIslandCases'],
             'islandEvidence': c['islandEvidence'],
             'otherFinding': annotations['case9'] if n == 9 else None,
             'rawMembers': [{'path': m['rawPath'], 'line': m['rawLine'], 'recordID': m['sourceRecordID']} for m in members]})
    save(args.output / 'cases.json', ledger)
    source_proof_cases = []
    for n, c in enumerate(cohort, 1):
        event = current[c['targetEventID']]
        source_proof_cases.append({'case': n, 'exampleID': c['exampleID'], 'targetEventID': c['targetEventID'],
                                  'knownIssue': n in problem_cases, 'hold': n == 529,
                                  'targetBeganAt': c['targetBeganAt'], 'targetText': c['targetText']})
    save(args.output / 'cohort-screening.json', source_proof_cases)
    summary = {'scope': 'Known-case audit, NOT an exhaustive ground-truth error rate; no packing performed',
        'alternativeReplay': str(args.replay) if args.replay else None,
        'episodeHistoryMembershipNotRebuilt': bool(args.replay),
        'semanticReadSelectionsChanged': len(selection_changes),
        'changedSelectionsUseActualCaptureTimes': True,
        'cohortSize': len(cohort), 'knownUnrepairedExamples': len(problem_cases),
        'knownUnrepairedCases': sorted(problem_cases), 'knownUnrepairedPercent': len(problem_cases) / len(cohort) * 100,
        'boundaryExamples': boundary_cases, 'prefixCases': prefix_cases,
        'oldTextIslandCases': annotations['oldTextIslandCases'], 'unresolvedHoldCases': [529],
        'boundariesReassessed': len(boundary_results), 'nonNovelBoundariesReviewed': 36,
        'nonNovelBoundariesProvedNow': [b['candidateLine'] for b in boundary_results if b['annotation']['disposition'] != 'retain_boundary' and b['proofNowComplete']],
        'genuineNovelControlsStillBlocking': all(not b['proofNowComplete'] for b in boundary_results if b['annotation']['disposition'] == 'retain_boundary'),
        'perDay': per_day, 'uniqueRestoredLineStrings': len(retained_session_lines),
        'peakRSSMiB': memory(), 'inputSHA256': inputs, 'auditSHA256': sha(Path(__file__))}
    save(args.output / 'summary.json', summary)
    print(json.dumps({k: v for k, v in summary.items() if k != 'inputSHA256'}, indent=2))


if __name__ == '__main__':
    main()

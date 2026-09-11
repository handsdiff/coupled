#!/usr/bin/env python3
"""Apply explicitly reviewed, evidence-bound episode joins to a frozen corpus.

This is corpus curation, not a change to collection or the general episode
heuristic. Endpoints and authorship are reconstructed by the existing episode
builder; neither targets nor queries are copied from a reviewer suggestion.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import resource
import sys

VERSION = "phase1-reviewed-episode-joins-v1"
ROOT = Path(__file__).resolve().parents[1]
NON_NOVEL = {
    "exact_repeat_available_at_episode_onset",
    "exact_repeat_already_in_episode_onset_model_input",
    "repeated_non_novel_read", "self_derived_active_composition_read",
    "screenshot_reviewed_non_novel_read",
}


def rows(path):
    with path.open() as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def save(path, value):
    with path.open("x") as f:
        json.dump(value, f, ensure_ascii=False, sort_keys=True, indent=2)
        f.write("\n")


def save_rows(path, values):
    with path.open("x") as f:
        for row in values:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def load_module(name):
    key = name.replace("-", "_")
    spec = importlib.util.spec_from_file_location(key, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


def require(condition, message):
    if not condition:
        raise ValueError(message)


def contiguous_join_groups(requested):
    groups = []
    for line in sorted(set(requested)):
        require(line > 0, "Invalid candidate boundary")
        if groups and line == groups[-1][-1] + 1:
            groups[-1].append(line)
        else:
            groups.append([line])
    return groups


def require_chain_onset(group, candidates, reviews, closures):
    if len(group) == 1:
        return
    first = candidates[group[0] - 1]
    for line in group:
        review = reviews.get(line, {})
        field = review.get("initialFieldEvidence", {})
        require(any(o["capturedAt"] < first["beganAt"] for o in review.get("observations", []))
                or field.get("recordID") == first["members"][0]["sourceRecordID"],
                "Chain boundary lacks evidence from before the whole composition began")
    require(not set(group[1:]) & set(closures), "An interior closure cannot close a longer chain")


def require_join(left, right, evidence, algorithm, events):
    require(evidence.get("proofNowComplete") is True or evidence.get("screenshotReviewAccepted") is True, "READ proof is incomplete")
    assessments = evidence.get("newAssessments", [])
    require(bool(assessments), "Join has no READ evidence")
    require(all(r["status"] in NON_NOVEL for r in assessments), "Novel or unresolved READ")
    require(evidence["onset"] == left["beganAt"], "Evidence onset mismatch")
    require(evidence["nextOnset"] == right["beganAt"], "Evidence continuation mismatch")
    for candidate in (left, right):
        require(candidate["continuityEvidence"]["continuousReplayableState"], "Broken member trajectory")
        require(candidate["surfaceEvidence"]["logicalEditableIdentityStable"], "Changed member destination")
    a, b = left["members"][-1], right["members"][0]
    require(algorithm.episode_state_continuous(a, b), "State discontinuity across proposed join")
    require(algorithm.same_episode_destination(a, b), "Destination changed across proposed join")
    require(not left["closureEvidence"].get("objectiveSubmissionBoundary"), "Cannot join across submission")
    ids = set(left["memberWriteEventIDs"] + right["memberWriteEventIDs"])
    session = events[a["writeEventID"]]["sessionID"]
    upper = right["candidateAvailableAt"]
    outside = [r["sourceEventID"] for r in events.values() if r["kind"] == "write"
               and r["sessionID"] == session and r["sourceEventID"] not in ids
               and r["beganAt"] < upper and r["availableAt"] >= left["beganAt"]]
    require(not outside, "Overlapping outside WRITE: " + str(outside))


def verify_member(member, raw):
    first = raw[member["sourceRecordID"]]
    # The primitive adds value hashes/counts and omits the bulky range probe.
    keys = ("observationID", "observedAt", "value", "valueWasTruncated",
            "selectedRangeLocation", "selectedRangeLength")
    same = lambda a, b: isinstance(a, dict) and isinstance(b, dict) and all(a.get(k) == b.get(k) for k in keys)
    require(same(member["before"], first.get("before")), "Raw BEFORE changed")
    selected = member["selectedTerminalObservation"]
    terminal = raw[member["terminalSourceRecordID"]]
    observations = [terminal.get("after"), terminal.get("before")]
    for key in ("mutationCheckpoints", "returnCheckpoints", "pasteCheckpoints"):
        for checkpoint in terminal.get(key, []):
            observations.extend(checkpoint.get(k) for k in ("observation", "before", "after"))
    require(any(same(selected, observation) for observation in observations), "Selected terminal not present in raw evidence")
    require(not selected.get("valueWasTruncated"), "Truncated selected terminal")


def apply_read_review(boundary, review):
    digest = hashlib.sha256(json.dumps(boundary, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    require(digest == review["boundarySHA256"], "Reviewed boundary evidence changed")
    require(review["decision"] == "non_novel" and review["reviewedBy"] == "assistant_visual_evidence_review", "Unsupported READ adjudication")
    require(set(review["eventIDs"]) == {a["eventID"] for a in boundary["newAssessments"]}, "READ review does not cover whole boundary")
    require(bool(review["reason"]) and bool(review["screenshots"]), "Missing READ review evidence")
    for image in review["screenshots"]:
        require(sha(ROOT / image["path"]) == image["sha256"], "READ review screenshot changed")
    result = deepcopy(boundary)
    result["screenshotReviewAccepted"] = True
    result["manualNonNovelReview"] = review
    # Keep the original automatic result; do not pretend OCR matching passed.
    result["automaticAssessments"] = deepcopy(result["newAssessments"])
    for a in result["newAssessments"]:
        a["status"] = "screenshot_reviewed_non_novel_read"
    return result


def verify_read_observations(boundary, review, raw, initial_member=None):
    """Bind visual adjudication to actual prior/current captures, not a caption.

    V1 sidecars predate this extra provenance. New sidecars opt in explicitly;
    their prior screenshot must be available before the ORIGINAL episode onset.
    """
    if "observations" not in review:
        return
    observations = review["observations"]
    require(bool(observations), "Missing visual observation lineage")
    screenshot_hashes = {r["sha256"] for r in review["screenshots"]}
    by_time = {}
    for reference in observations:
        record = raw[reference["recordID"]]
        require(record.get("capturedAt") == reference["capturedAt"], "Visual observation time changed")
        require(record.get("screenshotSHA256") == reference["screenshotSHA256"], "Visual observation image changed")
        require(reference["screenshotSHA256"] in screenshot_hashes, "Unreviewed observation image")
        by_time.setdefault(reference["capturedAt"], set()).add(reference["recordID"])
    field = review.get("initialFieldEvidence")
    if field:
        # An existing personal note can be proven from its initial retained AX
        # state even when the previous screenshot shows a different viewport.
        # This is a manually reviewed witness, not an automatic novelty rule.
        require(initial_member is not None, "Initial field evidence lacks its episode")
        require(field["recordID"] == initial_member["sourceRecordID"]
                and field["rawPath"] == initial_member["rawPath"], "Wrong initial field record")
        before = raw[field["recordID"]]["before"]
        require(before.get("observationID") == field["observationID"]
                and before.get("observedAt") == field["observedAt"], "Initial field observation changed")
        require(isinstance(before.get("value"), str) and not before.get("valueWasTruncated"), "Invalid initial field")
        require(hashlib.sha256(before["value"].encode()).hexdigest() == field["valueSHA256"], "Initial field text changed")
    else:
        require(any(t < boundary["onset"] for t in by_time), "No pre-onset visual evidence")
    for read in boundary["currentReads"]:
        require(bool(by_time.get(read["availableAt"], set()) & set(read["sourceRecordIDs"])),
                "READ lacks its reviewed capture-time raw observation")


def build(base, boundaries_path, requested, output, project_corpus=True, closure_path=None, read_review_path=None, supplemental_path=None):
    algorithm = load_module("construct-phase1-raw-episode-corpus")
    projection = load_module("construct-phase1-closed-episode-corpus")
    source = Path(json.loads((base / "corpus.json").read_text())["source"]["path"])
    candidates = list(rows(base / "raw-episode-candidates.jsonl"))
    decisions = {r["candidateID"]: r for r in rows(base / "episode-adjudications.jsonl")}
    events = list(rows(source / "events.jsonl"))
    by_event = {r["sourceEventID"]: r for r in events}
    boundaries = {r["candidateLine"]: r for r in json.loads(boundaries_path.read_text())}
    boundary_sources = {line: boundaries_path for line in boundaries}
    if supplemental_path:
        for boundary in json.loads(supplemental_path.read_text()):
            line = boundary["candidateLine"]
            require(line not in boundaries, "Supplemental evidence cannot replace a reviewed boundary")
            boundaries[line] = boundary
            boundary_sources[line] = supplemental_path
    read_reviews = {r["candidateLine"]: r for r in json.loads(read_review_path.read_text())} if read_review_path else {}
    require(set(read_reviews) <= set(requested), "Unrequested READ adjudication")
    for line, review in read_reviews.items():
        boundaries[line] = apply_read_review(boundaries[line], review)
    closures = {r["candidateLine"]: r for r in json.loads(closure_path.read_text())} if closure_path else {}
    requested = sorted(set(requested))
    groups = contiguous_join_groups(requested)
    selected_members = []
    for line in requested:
        left, right = candidates[line - 1:line + 1]
        require_join(left, right, boundaries[line], algorithm, by_event)
        selected_members.extend(left["members"] + right["members"])
    for group in groups:
        require_chain_onset(group, candidates, read_reviews, closures)
    needed = {}
    for member in selected_members:
        needed.setdefault(ROOT / member["rawPath"], set()).update(member["sourceRecordIDs"])
    for closure in closures.values():
        needed.setdefault(ROOT / closure["rawPath"], set()).add(closure["rawRecordID"])
    for review in read_reviews.values():
        for observation in review.get("observations", []):
            needed.setdefault(ROOT / observation["rawPath"], set()).add(observation["recordID"])
    raw, hashes = {}, {str(p.resolve()): sha(p) for p in (
        base / "corpus.json", base / "raw-episode-candidates.jsonl",
        base / "episode-adjudications.jsonl", source / "events.jsonl", boundaries_path,
        Path(__file__), ROOT / "scripts/construct-phase1-raw-episode-corpus.py",
        ROOT / "scripts/construct-phase1-closed-episode-corpus.py",
    )}
    if closure_path:
        hashes[str(closure_path.resolve())] = sha(closure_path)
    if supplemental_path:
        hashes[str(supplemental_path.resolve())] = sha(supplemental_path)
    if read_review_path:
        hashes[str(read_review_path.resolve())] = sha(read_review_path)
        for review in read_reviews.values():
            for image in review["screenshots"]:
                hashes[str((ROOT / image["path"]).resolve())] = image["sha256"]
    for closure in closures.values():
        image = ROOT / closure["screenshot"]
        hashes[str(image)] = sha(image)
        require(hashes[str(image)] == closure["screenshotSHA256"], "Closure screenshot changed")
    for path, ids in needed.items():
        hashes[str(path.resolve())] = sha(path)
        for record in rows(path):
            if record.get("recordID") in ids:
                raw[record["recordID"]] = record
        require(ids <= set(raw), "Missing raw records")
    for member in selected_members:
        verify_member(member, raw)
    group_starts = {line: group[0] for group in groups for line in group}
    for line, review in read_reviews.items():
        verify_read_observations(boundaries[line], review, raw,
                                 candidates[group_starts[line] - 1]["members"][0])
    output.mkdir(parents=True, exist_ok=False)
    replacements, review, changed_ids = {}, [], set()
    for group in groups:
        line = group[0]
        parts = candidates[line - 1:group[-1] + 1]
        left, right = parts[0], parts[-1]
        proof = boundaries[line]
        proofs = [boundaries[k] for k in group]
        cases = proof["cases"] if len(group) == 1 else sorted({c for p in proofs for c in p["cases"] if c is not None})
        members = [m for c in parts for m in c["members"]]
        episode = algorithm.OpenEpisode(
            [{"members": [m], "initialObservationSource": left.get("initialObservationSource")} for m in members],
            left["episodeStateMachine"]["onsetPartitionReason"],
        )
        # Keep every already-verified internal revision bridge. Replace only
        # the reviewed split boundary, never silently permit a new reset.
        episode.boundary_evidence = [deepcopy(b) for c in parts
            for b in c["episodeStateMachine"]["boundaryEvidence"]
            if b.get("decision") == "continue"]
        for a, b, p in zip(parts, parts[1:], proofs):
            episode.boundary_evidence.append({
                "between": [a["memberWriteEventIDs"][-1], b["memberWriteEventIDs"][0]],
                "decision": "continue", "stateContinuous": True,
                "sameLogicalDestination": True, "readAssessments": p["newAssessments"],
                "curationVersion": VERSION,
            })
        # The final boundary must remain the terminal boundary used for closure.
        episode.boundary_evidence.extend(deepcopy(b) for b in
            right["episodeStateMachine"]["boundaryEvidence"] if b.get("decision") != "continue")
        episode.read_assessments = [deepcopy(a) for c in parts
            for a in c["causalEvidence"].get("interveningReadAssessments", [])]
        episode.read_assessments.extend(deepcopy(a) for p in proofs for a in p["newAssessments"])
        episode.close_reason = right["episodeStateMachine"]["closeReason"]
        candidate, decision = algorithm.classify_episode(episode, events, by_event, raw)
        if line in closures:
            closure = closures[line]
            observation = raw[closure["rawRecordID"]]
            capture_time = observation.get("capturedAt") or observation.get("observedAt")
            require(capture_time == closure["capturedAt"], "Closure observation time mismatch")
            require(observation.get("screenshotSHA256") == closure["screenshotSHA256"], "Closure image not bound to raw")
            require(capture_time >= candidate["candidateAvailableAt"], "Closure precedes completion")
            require(observation["sessionID"] == by_event[members[0]["writeEventID"]]["sessionID"], "Closure belongs to another session")
            require(decision["reconstructionStatus"] == "reconstructed", "Closure cannot repair reconstruction")
            require(candidate["onsetEvidence"].get("promptOnsetProven") is True, "Closure cannot invent prompt onset")
            content = decision["finalizedTarget"]["resolvedContent"]
            require(hashlib.sha256(content.encode()).hexdigest() == closure["finalContentSHA256"], "Reviewed submitted content differs")
            require(closure["decision"] == "submitted_message_and_empty_composer_visible", "Unsupported visual adjudication")
            candidate["candidateAvailableAt"] = capture_time
            candidate["closureEvidence"].update(status="closed_submission", reviewedVisualSubmission=closure)
            decision.update(decision="closed_loss_episode", closureStatus="closed_submission",
                            closureReason="reviewed_screenshot_shows_submission", lossEligibility="eligible",
                            reason="closed_reconstructed_substantive_authored_completion")
        require(candidate["mechanicalGates"]["passed"], "Reconstructed join failed mechanical gates")
        require(decision["finalizedTarget"] is not None, "Join has no surviving completion")
        require(candidate["beganAt"] == left["beganAt"], "Join changed original onset")
        require(candidate["initialConditioningState"] == left["initialConditioningState"], "Join changed original query state")
        candidate["curation"] = {
            "version": "phase1-reviewed-episode-joins-v2" if line in read_reviews else VERSION, "sourceCandidateIDs": [c["candidateID"] for c in parts],
            "boundaryEvidenceSHA256": sha(boundary_sources[line]), "boundaryCandidateLine": line,
            "originalCases": cases, "policy": "reviewed_non_novel_read_join",
        }
        if line in read_reviews:
            candidate["curation"]["manualReadAdjudication"] = read_reviews[line]
        if len(group) > 1:
            candidate["curation"].update(version="phase1-reviewed-episode-joins-v3",
                boundaryCandidateLines=group, manualReadAdjudications=[read_reviews[k] for k in group],
                boundaryEvidenceHashes={str(k): sha(boundary_sources[k]) for k in group})
        decision["classificationProvenance"] = VERSION
        replacements[line] = (candidate, decision)
        changed_ids.update(c["candidateID"] for c in parts)
        review.append({"candidateLine": line, "originalCases": cases,
            "before": [decisions[c["candidateID"]]["finalizedTarget"] for c in parts],
            "after": decision["finalizedTarget"], "decision": decision["decision"],
            "beganAt": candidate["beganAt"], "availableAt": candidate["candidateAvailableAt"],
            "query": projection.serialize_query(candidate["initialConditioningState"], candidate["initialModelFacingDestination"]),
            "memberWriteEventIDs": candidate["memberWriteEventIDs"],
            "reconstruction": candidate["singleCompletionDiagnostic"],
            "closure": {"status": decision["closureStatus"], "reason": decision["closureReason"]},
            "selectedRaw": [{k: m.get(k) for k in ("rawPath", "rawLine", "sourceRecordIDs", "selectedTerminalObservationSource", "selectedTerminalCheckpointID")} for m in members]})
        if len(group) > 1:
            review[-1]["candidateLines"] = group
    out_candidates, out_decisions = [], []
    for line, candidate in enumerate(candidates, 1):
        if line in replacements:
            c, d = replacements[line]
            out_candidates.append(c); out_decisions.append(d)
        elif line - 1 not in requested:
            out_candidates.append(candidate); out_decisions.append(decisions[candidate["candidateID"]])
    require(len(out_candidates) == len(candidates) - len(requested), "Unexpected candidate count")
    untouched = {c["candidateID"]: c for c in candidates if c["candidateID"] not in changed_ids}
    require(all(c == untouched[c["candidateID"]] for c in out_candidates if c["candidateID"] in untouched), "Unrelated candidate changed")
    save_rows(output / "candidates.jsonl", out_candidates)
    save_rows(output / "adjudications.jsonl", out_decisions)
    save(output / "join-review.json", review)
    artifact = projection.construct(source, output / "adjudications.jsonl", [output / "candidates.jsonl"], output / "episodes") if project_corpus else None
    report = {"version": "phase1-reviewed-episode-joins-v3" if any(len(g) > 1 for g in groups) else ("phase1-reviewed-episode-joins-v2" if read_reviews else VERSION), "sourceHashes": hashes, "joinedBoundaries": len(requested),
        "joinedEpisodeGroups": len(review),
        "screenshotAdjudicatedBoundaries": sorted(read_reviews),
        "affectedOriginalCases": sorted({n for r in review for n in r["originalCases"] if n is not None}),
        "unrelatedCandidatesUnchanged": len(untouched), "counts": artifact["counts"] if artifact else None,
        "trainingFreezeApproved": False, "remainingKnownIssuesNotResolved": True,
        "peakRSSMiB": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 ** 2 if sys.platform == "darwin" else 1024),
        "artifactsSHA256": {n: sha(output / n) for n in ("candidates.jsonl", "adjudications.jsonl", "join-review.json")}}
    save(output / "curation.json", report)
    print(json.dumps(report, indent=2))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--baseline", required=True, type=Path)
    p.add_argument("--boundaries", required=True, type=Path)
    p.add_argument("--candidate-lines", required=True, nargs="+", type=int)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--evidence-only", action="store_true")
    p.add_argument("--closure-evidence", type=Path)
    p.add_argument("--read-adjudications", type=Path)
    p.add_argument("--supplemental-boundaries", type=Path)
    a = p.parse_args()
    build(a.baseline.resolve(), a.boundaries.resolve(), a.candidate_lines, a.output.resolve(), not a.evidence_only, a.closure_evidence, a.read_adjudications, a.supplemental_boundaries)


if __name__ == "__main__":
    main()

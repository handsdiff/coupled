#!/usr/bin/env python3
"""Evidence-bound restoration of submitted prompts split by false empty AX.

This is a versioned offline curation rule, not an empty-value merge heuristic.
Each accepted interval needs a reviewed empty onset, exact retained identity,
complete local mutation trajectories, no intervening submission/outside input,
and explicit evidence for every intervening READ. Raw files remain unchanged.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess

from importlib.machinery import SourceFileLoader
h = SourceFileLoader("onset_helpers", str(Path(__file__).with_name("curate-phase1-episode-joins.py"))).load_module()
projection = h.load_module("construct-phase1-closed-episode-corpus")
algorithm = h.load_module("construct-phase1-raw-episode-corpus")
VERSION = "phase1-reviewed-prompt-onset-restoration-v2"
ROOT = h.ROOT


def observation_ok(o):
    return isinstance(o, dict) and isinstance(o.get("value"), str) and not o.get("valueWasTruncated")


def identity(r):
    target = r["targetIdentity"]
    return tuple(target.get(k) for k in ("bundleIdentifier", "processIdentifier", "elementHash", "role", "windowTitle"))


def local_endpoint(r, continuation=False, submitted=False):
    """Use final local states, never concatenate checkpoint/keystroke strings."""
    h.require(observation_ok(r.get("before")) and not r.get("beforeAXErrors"), "Missing complete BEFORE")
    h.require(not r.get("tapTimeoutCountDuringBurst"), "Event tap timeout")
    h.require(not r.get("pasteCheckpoints"), "Paste requires separate segment lineage")
    inputs = r["inputEvents"]
    h.require(inputs and len(inputs) == r["inputEventCount"], "Incomplete input interval")
    h.require(set(i["hint"] for i in inputs) <= {"typed", "delete", "return"}, "Unsupported input action")
    by_input = {i["eventTimestampNanoseconds"]: i for i in inputs}
    h.require(len(by_input) == len(inputs), "Duplicate input evidence")
    mutations = [i for i in inputs if i.get("mutationCapable") and i["hint"] != "return"]
    h.require(mutations and mutations[0]["observedAt"] == r["beganAt"], "Onset is not first mutation")
    checkpoints = sorted(r.get("mutationCheckpoints", []), key=lambda c: c["observation"]["observedAt"])
    h.require(checkpoints, "No mutation trajectory")
    if continuation:
        checkpoint_inputs = {c["eventTimestampNanoseconds"] for c in checkpoints}
        h.require(all(i["eventTimestampNanoseconds"] in checkpoint_inputs for i in inputs if i["hint"] == "delete"),
                  "Unobserved deletion could alter hidden prefix")
    previous = r["before"]["value"]
    for c in checkpoints:
        o = c.get("observation")
        h.require(observation_ok(o) and not c.get("axErrors"), "Invalid mutation checkpoint")
        i = by_input.get(c["eventTimestampNanoseconds"])
        h.require(i is not None and i.get("mutationCapable"), "Checkpoint input lineage missing")
        h.require(i["observedAt"] <= o["observedAt"], "Checkpoint predates input")
        # An erase at the beginning of a reset epoch may delete hidden prefix
        # text. Do not pretend it merely edits the new local suffix.
        if continuation and i["hint"] == "delete":
            h.require(bool(previous) and bool(o["value"]), "Deletion can cross hidden prefix boundary")
        if previous and not o["value"]:
            h.require(i["hint"] == "delete", "Unexplained reset inside mutation trajectory")
        previous = o["value"]
    last = checkpoints[-1]
    h.require(last["eventTimestampNanoseconds"] == mutations[-1]["eventTimestampNanoseconds"], "Checkpoint misses final mutation")
    h.require(last["observation"]["observedAt"] >= mutations[-1]["observedAt"], "Checkpoint is before final input")
    returns = r.get("returnCheckpoints", [])
    if submitted:
        h.require(r["boundaryReason"] == "return_pressed" and len(returns) == 1, "No single final submission")
        terminal = returns[0]
        h.require(inputs[-1]["hint"] == "return" and terminal["eventTimestampNanoseconds"] == inputs[-1]["eventTimestampNanoseconds"], "Input after submission")
        h.require(observation_ok(terminal.get("observation")) and not terminal.get("axErrors"), "Invalid pre-Return")
        h.require(terminal["observation"]["value"] == last["observation"]["value"], "Return differs from last local state")
        h.require(last["observation"]["observedAt"] <= terminal["observation"]["observedAt"], "Return precedes final checkpoint")
        selected, source = terminal, "pre_return_checkpoint"
    else:
        h.require(not returns and all(i["hint"] != "return" for i in inputs), "Submission closes prior prompt")
        h.require(r["boundaryReason"] in {"write_delay_elapsed", "pointer_selection_boundary"}, "Unreviewed composition boundary")
        after = r.get("after")
        h.require(observation_ok(after) and not r.get("afterAXErrors"), "Invalid terminal field")
        h.require(after["observedAt"] >= last["observation"]["observedAt"], "Terminal precedes final checkpoint")
        if after["value"] == last["observation"]["value"]:
            selected, source = {"observation": after}, "terminal_after"
        else:
            h.require(after["value"] == "" and bool(last["observation"]["value"]), "Unexplained nonempty terminal change")
            selected, source = last, "post_final_input_checkpoint_terminal_reset"
    h.require(bool(selected["observation"]["value"]), "Actual empty/delete-only epoch")
    return {"recordID": r["recordID"], "selectedObservationSource": source,
            "selectedObservationID": selected["observation"]["observationID"],
            "selectedCheckpointID": selected.get("checkpointID"),
            "selectedObservation": selected["observation"], "observedTerminal": r.get("after"),
            "lastMutationAt": mutations[-1]["observedAt"], "mutationCheckpointCount": len(checkpoints)}


def reconstruct(attempts):
    h.require(len(attempts) >= 2, "Not a multi-epoch prompt")
    first = attempts[0]
    h.require(all(x is not None for x in identity(first)), "Incomplete retained identity")
    h.require(algorithm.is_prompt_surface(first), "Not a prompt surface")
    epochs = []
    parts = []
    clipboard = first["conditioningState"].get("clipboard", {})
    for i, r in enumerate(attempts):
        h.require(r["sessionID"] == first["sessionID"] and identity(r) == identity(first), "Different receiving editable")
        h.require(r["conditioningState"]["sourceObservationID"] == r["before"]["observationID"], "Query not bound to BEFORE")
        current_clipboard = r["conditioningState"].get("clipboard", {})
        h.require(all(clipboard.get(k) == current_clipboard.get(k) for k in ("changeCount", "textSHA256")), "Clipboard changed during opportunity")
        if i:
            h.require(attempts[i-1]["terminalDecisionAt"] < r["beganAt"], "Overlapping input attempts")
        before = r["before"]["value"]
        if not i:
            h.require(before == "", "No empty original query")
        continuous = bool(i and before and before == epochs[-1]["selectedObservation"]["value"])
        h.require(before == "" or continuous, "Nonempty BEFORE does not continue prior local state")
        endpoint = local_endpoint(r, continuation=len(parts) > 1 if continuous else i > 0,
                                  submitted=i == len(attempts)-1)
        endpoint["compositionTransition"] = "continuous_local_revision" if continuous else "reviewed_empty_reset_epoch"
        endpoint["localEpochOrdinal"] = len(parts)-1 if continuous else len(parts)
        epochs.append(endpoint)
        if continuous:
            # Replace the endpoint of this local epoch, folding revisions into
            # its final value. Appending would duplicate the existing suffix.
            parts[-1] = endpoint["selectedObservation"]["value"]
        else:
            parts.append(endpoint["selectedObservation"]["value"])
    # These are final locally edited strings, not each intermediate checkpoint.
    content = "".join(parts)
    h.require(len(content.strip()) >= 4, "Non-substantive completion")
    return content, epochs


def require_interval(attempts, raw_interval, arm_events, reviews, probe_reviews=(), history_only=False):
    ids = {r["recordID"] for r in attempts}
    start, end = attempts[0]["beganAt"], attempts[-1]["terminalDecisionAt"]
    probe_reviews = {p["recordID"]: p for p in probe_reviews}
    seen_probes = set()
    for r in raw_interval:
        if r.get("recordType") == "active_tap_write_attempt" and r["recordID"] not in ids:
            h.require(not (r["beganAt"] < end and r["terminalDecisionAt"] >= start), "Unexplained outside raw WRITE")
        if r.get("recordType") == "prompt_submission_observation":
            at = r.get("observedAt") or r.get("beganAt")
            h.require(at is not None, "Untimed raw submission signal")
            if start < at < attempts[-1]["lastInputAt"]:
                review = probe_reviews.get(r["recordID"])
                h.require(review is not None and review["rawRecordSHA256"] == projection.stable_id("", r), "Unreviewed submission probe")
                h.require(review["decision"] == "verified_non_submission_probe", "Intervening raw submission signal")
                source = next((a for a in attempts if a["recordID"] == r.get("sourceWriteRecordID")), None)
                h.require(source is not None and identity(source) == identity(r), "Probe has different source editable")
                h.require(r.get("disposition") in {"surface_changed", "superseded_by_subsequent_mutation"}, "Actual or uncertain submission")
                h.require(not any(r.get(k) for k in ("preActionAXErrors", "postActionAXErrors", "surfaceValidationErrors")), "Failed submission probe")
                h.require(r.get("referenceRetainedAt") == source["terminalDecisionAt"], "Probe does not reference terminal state")
                h.require(r.get("terminalObservationID") == source["after"]["observationID"], "Probe terminal identity changed")
                h.require(r.get("terminalValueSHA256") == hashlib.sha256(source["after"]["value"].encode()).hexdigest(), "Probe terminal value changed")
                if r["disposition"] == "superseded_by_subsequent_mutation":
                    h.require(not r.get("action") and any(a["beganAt"] == at and a["inputEvents"][0]["hint"] == "typed" for a in attempts), "Probe was not superseded by typing")
                else:
                    h.require(r.get("action", {}).get("kind") == "pointer_click" and bool(review.get("visualContinuationRecordID")), "Surface change lacks visual continuation review")
                    h.require(observation_ok(r.get("preActionObservation")) and r["preActionObservation"]["value"] == source["after"]["value"], "Surface change lacks retained prefix")
                seen_probes.add(r["recordID"])
    h.require(seen_probes == probe_reviews.keys(), "Unused submission probe review")
    actual = {}
    for arm, events in arm_events.items():
        for e in events:
            if e["kind"] == "read" and start <= e["availableAt"] <= end:
                actual[(arm, e["sourceEventID"])] = e
    expected = {(r["arm"], r["eventID"]): r for r in reviews}
    h.require(actual.keys() == expected.keys(), "Intervening READ lacks exact review")
    for key, e in actual.items():
        r = expected[key]
        h.require(r["decision"] in {"repeated_prior_response_plus_own_draft", "repeated_pre_onset_information"}
                  or (history_only and r["decision"] == "novel_read"), "Novel READ partitions composition")
        h.require(r["serializedSHA256"] == projection.stable_id("", e["serialized"]), "Reviewed READ changed")
        h.require(set(e["sourceRecordIDs"]) <= set(r["observationRecordIDs"]), "READ lacks capture evidence")


def require_onset_prefix(ref, capture, first):
    """A reviewed full composer showing only the initial, observed prefix.

    This later image corroborates the empty raw query; its content is never
    placed in that query. The first trajectory must grow monotonically from
    empty. A key intercepted during screenshot capture is allowed only when
    its later checkpoint proves a typed extension of that same visible prefix.
    """
    h.require(first["before"]["value"] == "", "No initial empty baseline")
    prior = ""
    matched = None
    for c in sorted(first["mutationCheckpoints"], key=lambda c: c["observation"]["observedAt"]):
        if c["observation"]["observedAt"] > capture["capturedAt"]:
            break
        value = c["observation"]["value"]
        h.require(value.startswith(prior) and len(value) > len(prior), "Onset trajectory contains revision/reset")
        prior, matched = value, c
    h.require(matched is not None and matched["observation"]["observationID"] == ref["checkpointObservationID"], "Onset image is not bound to latest checkpoint")
    h.require(prior == ref["reviewedContent"] and bool(prior), "Onset composer differs from observed prefix")
    checkpoints = {c["eventTimestampNanoseconds"]: c for c in first["mutationCheckpoints"]}
    for i in first["inputEvents"]:
        if matched["eventTimestampNanoseconds"] < i["eventTimestampNanoseconds"] and i["observedAt"] <= capture["capturedAt"]:
            c = checkpoints.get(i["eventTimestampNanoseconds"])
            h.require(i.get("hint") == "typed" and c is not None and observation_ok(c.get("observation")) and not c.get("axErrors"), "Unobserved input before onset image")
            value = c["observation"]["value"]
            h.require(c["observation"]["observedAt"] > capture["capturedAt"] and value.startswith(prior) and len(value) > len(prior), "Pending input revises visible onset")
            prior = value


def build(source, review_path, normalizer, output):
    reviews = json.loads(review_path.read_text())
    needed = {}
    for review in reviews:
        needed.setdefault(ROOT / review["rawPath"], set()).update(review["attemptRecordIDs"] + [o["recordID"] for o in review["observations"]])
    raw, intervals, hashes = {}, {}, {str(review_path.resolve()): h.sha(review_path), str(normalizer.resolve()): h.sha(normalizer)}
    for path, ids in needed.items():
        hashes[str(path)] = h.sha(path)
        # Retain only requested records and low-volume input/submission metadata.
        intervals[path] = []
        for line, r in enumerate(h.rows(path), 1):
            if r.get("recordID") in ids:
                raw[r["recordID"]] = r
            if r.get("recordType") in {"active_tap_write_attempt", "prompt_submission_observation"}:
                intervals[path].append(r if r["recordType"] == "prompt_submission_observation" else
                                      {k: v for k, v in r.items() if k in {"recordID", "recordType", "beganAt", "terminalDecisionAt", "observedAt"}})
        h.require(ids <= raw.keys(), "Missing raw evidence")
    writes = list(h.rows(source / "common-write-events.jsonl"))
    examples = list(h.rows(source / "cohort.jsonl"))
    arm_events = {arm: list(h.rows(source / arm / "events.jsonl")) for arm in ("old", "new")}
    result = []
    for review in reviews:
        h.require(review["decision"] == "restore_reviewed_unsent_prompt_epochs" and bool(review["reason"])
                  and review["reviewedBy"] == "assistant_raw_trajectory_and_visual_review", "Unsupported onset adjudication")
        attempts = [raw[rid] for rid in review["attemptRecordIDs"]]
        content, epochs = reconstruct(attempts)
        history_only = any(o["role"] == "novel_read_during_composition" for o in review["observations"])
        require_interval(attempts, intervals[ROOT / review["rawPath"]], arm_events, review["reads"], review.get("submissionProbes", []), history_only)
        onset_witness = None
        for ref in review["observations"]:
            r = raw[ref["recordID"]]
            h.require(r["sessionID"] == attempts[0]["sessionID"], "Cross-session visual witness")
            h.require(r["capturedAt"] == ref["capturedAt"] and r["screenshotSHA256"] == ref["sha256"], "Changed raw visual observation")
            path = ROOT / review["rawPath"]
            image = path.parent / r["screenshotRelativePath"]
            h.require(h.sha(image) == ref["sha256"], "Screenshot changed")
            hashes[str(image)] = ref["sha256"]
            if ref["role"] == "empty_composer_before_onset":
                h.require(r["capturedAt"] < attempts[0]["beganAt"], "Onset witness is later than query")
                h.require(r.get("bundleIdentifier", r.get("surface", {}).get("bundleIdentifier")) == attempts[0]["targetIdentity"]["bundleIdentifier"], "Onset witness is a different application")
                onset_witness = ref
            if ref["role"] == "initial_composer_prefix_after_onset":
                h.require(r.get("bundleIdentifier", r.get("surface", {}).get("bundleIdentifier")) == attempts[0]["targetIdentity"]["bundleIdentifier"], "Onset witness is a different application")
                require_onset_prefix(ref, r, attempts[0])
                onset_witness = ref
            if ref["role"] == "complete_unsent_composition":
                index = next(i for i, a in enumerate(attempts) if a["recordID"] == ref["throughAttemptRecordID"])
                local_values = {}
                for e in epochs[:index+1]:
                    local_values[e["localEpochOrdinal"]] = e["selectedObservation"]["value"]
                h.require(ref["reviewedContent"] == "".join(local_values.values()), "Rendered composition differs from reconstructed local states")
                final_checkpoint = attempts[index]["mutationCheckpoints"][-1]["observation"]
                h.require(final_checkpoint["value"] == epochs[index]["selectedObservation"]["value"] and final_checkpoint["observedAt"] <= r["capturedAt"], "Composition image precedes final local edit")
                h.require(index+1 < len(attempts) and r["capturedAt"] < attempts[index+1]["beganAt"], "Composition image is not before continuation")
            if ref["role"] == "novel_read_during_composition":
                h.require(attempts[0]["beganAt"] < r["capturedAt"] < attempts[-1]["lastInputAt"] and bool(ref.get("novelContent")), "Unproven novel READ witness")
            if ref["role"] == "submitted_complete_prompt":
                h.require(ref["reviewedContent"] == content, "Reconstruction differs from rendered submission")
                h.require(r["capturedAt"] >= attempts[-1]["lastInputAt"], "Submission image precedes Return")
        h.require(onset_witness is not None, "No reviewed real empty onset")
        for p in review.get("submissionProbes", []):
            if p.get("visualContinuationRecordID"):
                h.require(any(o["recordID"] == p["visualContinuationRecordID"] and o["role"] == "submitted_complete_prompt" for o in review["observations"]), "Unbound non-submission visual witness")
        for read in review["reads"]:
            h.require(set(read["observationRecordIDs"]) <= {o["recordID"] for o in review["observations"]}, "Unbound READ review")
        ids = set(review["attemptRecordIDs"])
        retired = [e for e in writes if ids.intersection(e["sourceRecordIDs"])]
        h.require({e["sourceEventID"] for e in retired} == set(review["retiredWriteEventIDs"]), "Retirement scope changed")
        h.require(all(set(e["sourceRecordIDs"]) <= ids for e in retired), "Partial retirement would erase another composition")
        retired_examples = [e for e in examples if e["targetEventID"] in review["retiredWriteEventIDs"]]
        h.require(retired_examples, "No affected target")
        normalized = []
        for r in attempts:
            p = subprocess.run([str(normalizer.resolve())], input=json.dumps(r["conditioningState"]["destination"])+"\n", text=True, capture_output=True, check=True)
            normalized.append(json.loads(p.stdout))
        h.require(all(n["logicalDestinationKey"] == normalized[0]["logicalDestinationKey"] for n in normalized), "Logical destination changed")
        member_by_raw = {rid: e["memberWriteEventIDs"] for e in retired for rid in e["sourceRecordIDs"]}
        members = []
        for r in attempts:
            members.extend(member_by_raw.get(r["recordID"], [projection.stable_id("curated_write_", {"sessionID": r["sessionID"], "sourceRecordIDs": [r["recordID"]]})]))
        members = list(dict.fromkeys(members))
        eid = projection.stable_id("evt_", {"closedEpisodeMembers": members})
        target = {"schemaVersion": 1, "resolvedContent": content, "segments": [{"type": "authored_text", "content": content}]}
        available = max([attempts[-1]["terminalDecisionAt"]] + [o["capturedAt"] for o in review["observations"] if o["role"] == "submitted_complete_prompt"])
        proof = {"version": VERSION, "review": review, "epochs": epochs,
                 "rule": "reviewed_same_editable_unsent_epochs_final_local_states",
                 "rawRecordHashes": {r["recordID"]: projection.stable_id("", r) for r in attempts},
                 "onsetObservationID": attempts[0]["before"]["observationID"],
                 "terminalDecisionAt": attempts[-1]["terminalDecisionAt"], "evidenceAvailableAt": available}
        payload = {"kind": "write", "destination": normalized[0]["modelFacingDestination"], "operation": "closed_composition_episode", "authorshipResolution": "resolved", "authorshipSegments": target["segments"]}
        event = {"schemaVersion": 12, "conversionVersion": VERSION, "sourceEventID": eid, "sessionID": attempts[0]["sessionID"],
                 "beganAt": attempts[0]["beganAt"], "availableAt": available, "kind": "write", "sourceRecordIDs": sorted(ids | {o["recordID"] for o in review["observations"]}),
                 "memberWriteEventIDs": members, "memberCount": len(members), "episodeID": projection.stable_id("episode_", {"memberWriteEventIDs": members}),
                 "episodeDecision": "closed_loss_episode", "lossEligibility": "eligible", "closureStatus": "closed_submission", "reconstructionStatus": "reconstructed",
                 "serialized": projection.canonical_json(payload), "auditSerialized": projection.canonical_json({**payload, "promptOnsetRestoration": proof}),
                 **normalized[0], "curation": proof}
        if history_only:
            event.update(episodeDecision="history_only_episode", lossEligibility="ineligible",
                         targetExclusionReason="novel_read_during_reconstructed_prompt")
            proof["targetEligibility"] = "history_only_novel_read"
        conditioning = deepcopy(attempts[0]["conditioningState"])
        example = deepcopy(retired_examples[-1])
        example.update(exampleID=projection.stable_id("example_", {"targetEventID": eid}), targetEventID=eid, targetBeganAt=event["beganAt"],
                       targetAvailableAt=available, target=target, conditioningState=conditioning, query=projection.serialize_query(conditioning, normalized[0]["modelFacingDestination"]),
                       targetSourceRecordIDs=event["sourceRecordIDs"], targetUnitID=event["episodeID"], **normalized[0], promptOnsetRestoration=proof,
                       conversionVersion=VERSION, cursorFidelity=None,
                       targetMetadata={"availableAt": available, "decision": "closed_loss_episode", "episodeVersion": VERSION,
                                       "memberWriteEventIDs": members, "microWriteCount": len(members)})
        for k in ("chronologicalOrdinal", "experimentBlockID", "targetText", "context", "modelInput", "contextBlockIDs", "contextEventIDs", "contextSourceRecordIDs", "sourceRecordIDs"):
            example.pop(k, None)
        example["episode"] = {"memberWriteEventIDs": members, "memberCount": len(members), "decision": "closed_loss_episode", "closureReason": "reviewed_unsent_epochs_then_return", "episodeVersion": VERSION,
                              "onsetEvidence": {"requiresProvenPromptOnset": True, "promptOnsetProven": True, "proofReason": "reviewed_empty_composer_and_complete_local_trajectories"}}
        event["auditSerialized"] = projection.canonical_json({**payload, "promptOnsetRestoration": proof})
        result.append({"event": event, "example": example, "retiredWriteEventIDs": review["retiredWriteEventIDs"], "retiredExampleIDs": [e["exampleID"] for e in retired_examples],
                       "targetEligible": not history_only,
                       "exampleDisposition": "audit_only_not_training" if history_only else "training_candidate",
                       "suppressedReadEventIDs": [] if history_only else sorted({r["eventID"] for r in review["reads"]}), "proof": proof})
    for p in (source / "review.json", source / "common-write-events.jsonl", source / "cohort.jsonl", Path(__file__), ROOT / "scripts/construct-phase1-closed-episode-corpus.py", ROOT / "scripts/construct-phase1-raw-episode-corpus.py"):
        hashes[str(p.resolve())] = h.sha(p)
    for arm in arm_events:
        p = source / arm / "events.jsonl"
        hashes[str(p.resolve())] = h.sha(p)
    output.mkdir(parents=True, exist_ok=False)
    h.save_rows(output / "restored-prompts.jsonl", result)
    h.save(output / "restoration.json", {"version": VERSION, "sourceHashes": hashes, "restoredPrompts": len(result), "trainingFreezeApproved": False,
           "lossBearingRestorations": sum(r["targetEligible"] for r in result),
           "historyOnlyRestorations": sum(not r["targetEligible"] for r in result),
           "artifactSHA256": h.sha(output / "restored-prompts.jsonl"), "rawAndCollectorUnchanged": True})
    print(json.dumps([{"case": r["proof"]["review"]["case"], "content": r["example"]["target"]["resolvedContent"], "beganAt": r["event"]["beganAt"]} for r in result], indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", required=True, type=Path)
    p.add_argument("--reviews", required=True, type=Path)
    p.add_argument("--normalizer", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    a = p.parse_args()
    build(a.source, a.reviews, a.normalizer, a.output)

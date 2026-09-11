#!/usr/bin/env python3
"""Recover reviewed empty-Gemini → authored-prompt → reset observations.

Produces evidence-bound closed WRITEs for a curated corpus. Does not weaken
the reducer's general shortcut guard or reinterpret unreviewed attempts.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import subprocess

from importlib.machinery import SourceFileLoader
helper = SourceFileLoader("episode_curation", str(Path(__file__).with_name("curate-phase1-episode-joins.py"))).load_module()
ROOT = helper.ROOT
VERSION = "phase1-reviewed-submission-recovery-v1"
REVIEWED = {4: {4071, 4319, 4676}, 7: {8138, 8341, 10090}}


def valid_observation(value):
    return isinstance(value, dict) and isinstance(value.get("value"), str) and not value.get("valueWasTruncated", False)


def prove(raw, extend_submissions=False):
    """Final complete content is the target, never concatenated checkpoints."""
    check = helper.require
    before, after = raw.get("before"), raw.get("after")
    check(not raw.get("beforeAXErrors"), "Initial AX errors")
    check(valid_observation(before), "Missing/truncated initial endpoint")
    identity = raw["targetIdentity"]
    gemini = identity.get("bundleIdentifier") == "com.google.Chrome" and identity.get("role") == "AXTextArea" and identity.get("fieldDescription") == "Enter a prompt for Gemini"
    codex = extend_submissions and identity.get("bundleIdentifier") == "com.openai.codex" and identity.get("role") == "AXTextArea" and identity.get("fieldDescription") == "Do anything"
    check(gemini or codex, "Surface needs separate terminal/long-editor composition review")
    prompt = "Ask Gemini\n" if gemini else "\nDo anything"
    check(before["value"] == prompt, "Nonempty initial field needs earlier composition review")
    if gemini:
        check(not raw.get("afterAXErrors") and valid_observation(after) and after["value"] == prompt,
              "No complete post-submission prompt reset")
    else:
        check(after is None and raw.get("afterAXErrors") == ["AXValue:invalid_ui_element"],
              "No expected post-submission invalidation")
    check(raw["conditioningState"]["cursorContext"].get("fieldState") == "unpopulated_prompt", "Onset is not empty")
    check(not raw.get("tapTimeoutCountDuringBurst") and not raw.get("pasteCheckpoints"), "Timeout or paste ambiguity")
    check(set(raw["inputHints"]) <= {"typed", "delete", "return", "shortcut"}, "Unsupported input signal")
    check("typed" in raw["inputHints"], "No authored input")
    returns = raw.get("returnCheckpoints", [])
    check(len(returns) == 1, "Multiple submissions need separate review")
    terminal = returns[0]
    check(not terminal.get("axErrors") and valid_observation(terminal.get("observation")), "Invalid pre-Return")
    inputs = raw["inputEvents"]
    check(inputs[-1]["hint"] == "return" and terminal["eventTimestampNanoseconds"] == inputs[-1]["eventTimestampNanoseconds"], "Mutation after submission")
    check(raw["lastEventTimestampNanoseconds"] == terminal["eventTimestampNanoseconds"], "Terminal input mismatch")
    content = terminal["observation"]["value"]
    check(len(content.strip()) >= (4 if extend_submissions else 40) and content != before["value"], "No substantive final prompt")
    checkpoints = raw.get("mutationCheckpoints", [])
    check(checkpoints and all(not c.get("axErrors") and valid_observation(c.get("observation")) for c in checkpoints), "Incomplete mutation trajectory")
    checkpoints = sorted(checkpoints, key=lambda c: c["observation"]["observedAt"])
    # Post-input observations can coalesce rapid keys. Require valid linkage,
    # not a fictitious one-snapshot-per-key contract. These six trajectories
    # were reviewed as complete prompt rewrites, not cursor-level edits.
    by_input = {e["eventTimestampNanoseconds"]: e for e in inputs}
    check(all(c["eventTimestampNanoseconds"] in by_input for c in checkpoints), "Checkpoint without input lineage")
    pre = [c for c in checkpoints if c["observation"]["observedAt"] < terminal["observation"]["observedAt"]]
    check(pre and pre[-1]["observation"]["value"] == content, "Final prompt not confirmed by mutation trajectory")
    populated = False
    for c in pre:
        if c["observation"]["value"] != prompt:
            populated = True
        elif populated or not extend_submissions:
            check(by_input[c["eventTimestampNanoseconds"]]["hint"] == "delete", "Unexplained field reset before submission")
    end_at = after["observedAt"] if after is not None else raw["terminalDecisionAt"]
    check(before["observedAt"] <= pre[0]["observation"]["observedAt"] < terminal["observation"]["observedAt"] <= end_at, "Observation order invalid")
    return terminal, checkpoints


def require_closed_interval(raw, events):
    helper.require(not any(e["kind"] == "read" and raw["beganAt"] < e["availableAt"] < raw["terminalDecisionAt"] for e in events),
                   "Intervening READ needs novelty adjudication")
    helper.require(not any(e["kind"] == "write" and e["beganAt"] < raw["terminalDecisionAt"] and e["availableAt"] >= raw["beganAt"] for e in events),
                   "Outside WRITE requires composition reconstruction")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--replay", required=True, type=Path)
    p.add_argument("--normalizer", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--extend-submissions", action="store_true", help="Inspect all 64 rejected attempts under the conservative empty-field submission rule")
    a = p.parse_args()
    version = "phase1-reviewed-submission-recovery-v2" if a.extend_submissions else VERSION
    projection = helper.load_module("construct-phase1-closed-episode-corpus")
    recovered, inventory, hashes = [], [], {}
    for day in (2, 3, 4, 7):
        raw_path = ROOT / f"coupled-data/phase1-ordinary-work-2026-09-{day:02d}-1/raw.jsonl"
        unresolved_path = a.replay / f"sep{day}-phase1-semantic-v26/unresolved.jsonl"
        missing = {rid for r in helper.rows(unresolved_path) if r.get("reason") == "shortcut_changed_semantic_position_without_observation" for rid in r["sourceRecordIDs"]}
        events_path = a.replay / f"sep{day}-causal/events.jsonl"
        events = list(helper.rows(events_path)) if a.extend_submissions else []
        if a.extend_submissions:
            hashes[str(events_path.resolve())] = helper.sha(events_path)
        hashes[str(raw_path)] = helper.sha(raw_path)
        hashes[str(unresolved_path.resolve())] = helper.sha(unresolved_path)
        for line, raw in enumerate(helper.rows(raw_path), 1):
            if raw.get("recordID") not in missing:
                continue
            row = {"day": day, "rawLine": line, "rawPath": str(raw_path.relative_to(ROOT)),
                   "rawRecordID": raw["recordID"], "beganAt": raw.get("beganAt"),
                   "disposition": "pending_checkpoint_and_composition_review"}
            accepted = line in REVIEWED.get(day, set())
            if a.extend_submissions:
                try:
                    terminal, checkpoints = prove(raw, True)
                    require_closed_interval(raw, events)
                    accepted = True
                except ValueError as error:
                    helper.require(not accepted, "Previously recovered submission regressed: " + str(error))
                    row["reason"] = str(error)
                    row["disposition"] = "requires_further_composition_review"
            if accepted:
                if not a.extend_submissions:
                    terminal, checkpoints = prove(raw)
                conditioning = deepcopy(raw["conditioningState"])
                proc = subprocess.run([str(a.normalizer.resolve())], input=json.dumps(conditioning["destination"])+"\n", text=True, capture_output=True, check=True)
                normalized = json.loads(proc.stdout)
                rid = raw["recordID"]
                member = projection.stable_id("curated_write_", {"sessionID": raw["sessionID"], "sourceRecordIDs": [rid]})
                eid = projection.stable_id("evt_", {"closedEpisodeMembers": [member]})
                target = {"schemaVersion": 1, "resolvedContent": terminal["observation"]["value"],
                          "segments": [{"type": "authored_text", "content": terminal["observation"]["value"]}]}
                compact = {"kind": "write", "destination": normalized["modelFacingDestination"],
                           "operation": "closed_composition_episode", "authorshipResolution": "resolved",
                           "authorshipSegments": projection.resolved_history_segments(target)}
                proof = {"version": version, "rawPath": row["rawPath"], "rawLine": line, "rawRecordID": rid,
                         "rawRecordSHA256": projection.stable_id("", raw), "rawFileSHA256": hashes[str(raw_path)],
                         "selectedObservationID": terminal["observation"]["observationID"],
                         "selectedCheckpointID": terminal["checkpointID"], "selectedObservationSource": "pre_return_checkpoint",
                         "observationCapturedAt": terminal["observation"]["observedAt"],
                         "terminalDecisionAt": raw["terminalDecisionAt"], "mutationCheckpointCount": len(checkpoints),
                         "rule": "empty_prompt_complete_trajectory_return_reset_or_invalidation" if a.extend_submissions else "reviewed_empty_prompt_complete_trajectory_return_reset",
                         "originalRejection": "shortcut_changed_semantic_position_without_observation"}
                event = {"schemaVersion": 12, "conversionVersion": version, "sourceEventID": eid,
                         "sessionID": raw["sessionID"], "kind": "write", "beganAt": raw["beganAt"],
                         "availableAt": raw["terminalDecisionAt"], "sourceRecordIDs": [rid],
                         "memberWriteEventIDs": [member], "memberCount": 1,
                         "episodeID": projection.stable_id("episode_", {"memberWriteEventIDs": [member]}),
                         "episodeDecision": "closed_loss_episode", "lossEligibility": "eligible",
                         "closureStatus": "closed_submission", "reconstructionStatus": "reconstructed",
                         "serialized": projection.canonical_json(compact),
                         "auditSerialized": projection.canonical_json({**compact, "recovery": proof}),
                         **normalized, "curation": proof}
                recovered.append({"event": event, "target": target, "conditioningState": conditioning,
                                  "query": projection.serialize_query(conditioning, normalized["modelFacingDestination"]),
                                  "proof": proof, "trajectory": [{"capturedAt": c["observation"]["observedAt"],
                                  "value": c["observation"]["value"], "checkpointID": c["checkpointID"]} for c in checkpoints]})
                row["disposition"] = "recovered_submitted_composition"
                row["eventID"] = eid
            inventory.append(row)
    helper.require(len(inventory) == 64 and (len(recovered) >= 6 if a.extend_submissions else len(recovered) == 6), "Reviewed scope changed")
    a.output.mkdir(parents=True, exist_ok=False)
    helper.save_rows(a.output / "recovered-compositions.jsonl", recovered)
    helper.save_rows(a.output / "shortcut-dispositions.jsonl", inventory)
    hashes[str(a.normalizer.resolve())] = helper.sha(a.normalizer)
    hashes[str(Path(__file__).resolve())] = helper.sha(Path(__file__))
    for name in ("normalize-phase1-write-destination.swift", "../Sources/CoupledCore/Phase1WriteDestination.swift"):
        path = ROOT / "scripts" / name
        hashes[str(path.resolve())] = helper.sha(path)
    helper.save(a.output / "recovery.json", {"version": version, "sourceHashes": hashes,
        "recoveredRawAttempts": len(recovered), "recoveredCompositions": len(recovered), "remainingAttemptsPending": 64 - len(recovered),
        "trainingFreezeApproved": False, "collectorAndGeneralReducerUnchanged": True,
        "artifactsSHA256": {n: helper.sha(a.output / n) for n in ("recovered-compositions.jsonl", "shortcut-dispositions.jsonl")}})
    print(f"Recovered {len(recovered)} complete submissions; {64 - len(recovered)} attempts retain concrete review reasons.")


if __name__ == "__main__":
    main()

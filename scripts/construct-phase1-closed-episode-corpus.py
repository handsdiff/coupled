#!/usr/bin/env python3
"""Construct a strict, episode-normalized Phase 1 corpus.

The input corpus contains faithful micro-WRITE transitions. This projection
keeps those transitions only as lineage. Model-facing history and loss targets
contain closed composition episodes. Every source WRITE must be covered by an
evidence-bound disposition; ambiguity is excluded rather than guessed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterable


EPISODE_VERSION = "phase1-episode-v2"
CONVERSION_VERSION = "phase1-episode-causal-v2"


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_id(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected object")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected object")
            rows.append(value)
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("wb") as handle:
        for row in rows:
            handle.write(canonical_bytes(row))


def serialized_destination(event: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(event["serialized"])
    destination = value.get("destination")
    return destination if isinstance(destination, dict) else {}


def resolved_history_segments(target: dict[str, Any]) -> list[dict[str, Any]]:
    """Restore paste payloads for later history without putting them in targets."""
    resolved = target.get("resolvedContent")
    source = target.get("segments")
    if not isinstance(resolved, str) or not isinstance(source, list):
        raise ValueError("closed episode target is not structured")
    def parse(index: int, cursor: int) -> list[list[dict[str, Any]]]:
        if index == len(source):
            return [[]] if cursor == len(resolved) else []
        segment = source[index]
        if not isinstance(segment, dict):
            raise ValueError("invalid target segment")
        kind = segment.get("type")
        if kind == "authored_text":
            content = segment.get("content")
            if not isinstance(content, str) or not resolved.startswith(content, cursor):
                return []
            return [
                [dict(segment), *tail]
                for tail in parse(index + 1, cursor + len(content))
            ]
        if kind != "paste":
            raise ValueError(f"unsupported target segment type: {kind}")
        if index + 1 < len(source) and source[index + 1].get("type") == "paste":
            raise ValueError("adjacent paste payloads cannot be separated")
        next_authored = (
            source[index + 1].get("content", "") if index + 1 < len(source) else ""
        )
        boundaries: list[int] = []
        if next_authored:
            start = cursor
            while True:
                boundary = resolved.find(next_authored, start)
                if boundary < 0:
                    break
                boundaries.append(boundary)
                start = boundary + 1
        else:
            boundaries = [len(resolved)]
        results: list[list[dict[str, Any]]] = []
        for boundary in boundaries:
            payload = resolved[cursor:boundary]
            if not payload:
                continue
            for tail in parse(index + 1, boundary):
                results.append([{**segment, "content": payload}, *tail])
        return results

    solutions = parse(0, 0)
    if len(solutions) != 1:
        raise ValueError(
            "episode segments do not uniquely reconstruct resolved content"
        )
    return solutions[0]


def model_target(target: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(target))
    for segment in result.get("segments", []):
        if isinstance(segment, dict) and segment.get("type") == "paste":
            for key in ("content", "payload", "resolvedContent", "clipboardContent"):
                segment.pop(key, None)
    return result


def serialize_query(conditioning: dict[str, Any]) -> str:
    destination = dict(conditioning.get("destination") or {})
    destination.pop("processIdentifier", None)
    cursor = conditioning.get("cursorContext") or {}
    if cursor.get("source") == "accessibility_string_for_range":
        cursor = {
            key: cursor[key]
            for key in (
                "schemaVersion", "fieldState", "leftContext", "selectedText",
                "rightContext", "surfacePrompt",
            )
            if key in cursor
        }
    query: dict[str, Any] = {
        "schemaVersion": 3 if conditioning.get("clipboard") is not None else 2,
        "kind": "write_conditioning_state",
        "destination": destination,
        "cursorContext": cursor,
    }
    clipboard = conditioning.get("clipboard")
    if isinstance(clipboard, dict):
        query["clipboard"] = {
            "changeCount": clipboard.get("changeCount"),
            "content": clipboard.get("text"),
            "contentWasTruncated": clipboard.get("textWasTruncated"),
        }
    return canonical_json(query)


def require_candidate_gate(
    adjudication: dict[str, Any], candidate: dict[str, Any]
) -> None:
    members = adjudication.get("memberWriteEventIDs")
    if candidate.get("memberWriteEventIDs") != members:
        raise ValueError(f"candidate/member mismatch: {adjudication.get('label')}")
    causal = candidate.get("causalEvidence") or {}
    surface = candidate.get("surfaceEvidence") or {}
    continuity = candidate.get("continuityEvidence") or {}
    failures: list[str] = []
    if not causal.get("noNovelCausallyAvailableReadDuringCandidate"):
        failures.append("novel_read")
    if not causal.get("noOverlappingOutsideWrite"):
        failures.append("outside_write")
    if not surface.get("logicalEditableIdentityStable"):
        failures.append("logical_destination_changed")
    if not continuity.get("continuousReplayableState"):
        failures.append("state_discontinuity")
    if failures:
        raise ValueError(
            f"closed episode lacks production evidence ({', '.join(failures)}): "
            f"{adjudication.get('label')}"
        )


def construct(
    source: Path,
    adjudications_path: Path,
    candidate_paths: list[Path],
    output: Path,
) -> dict[str, Any]:
    manifest = load_json(source / "corpus.json")
    if manifest.get("conversionVersion") != "phase1-causal-v14":
        raise ValueError("strict episodes require phase1-causal-v14 source")
    source_events = load_jsonl(source / "events.jsonl")
    source_blocks = load_jsonl(source / "context-blocks.jsonl")
    source_examples = load_jsonl(source / "examples.jsonl")
    event_by_id = {row["sourceEventID"]: row for row in source_events}
    block_by_id = {row["contextBlockID"]: row for row in source_blocks}
    example_by_target = {row["targetEventID"]: row for row in source_examples}
    write_ids = {row["sourceEventID"] for row in source_events if row["kind"] == "write"}

    candidates: dict[str, dict[str, Any]] = {}
    for path in candidate_paths:
        for candidate in load_jsonl(path):
            candidate_id = candidate.get("candidateID")
            if not isinstance(candidate_id, str) or candidate_id in candidates:
                raise ValueError(f"duplicate/invalid candidate: {candidate_id}")
            candidates[candidate_id] = candidate

    adjudications = load_jsonl(adjudications_path)
    covered: set[str] = set()
    closed: list[tuple[dict[str, Any], dict[str, Any]]] = []
    exclusions: list[dict[str, Any]] = []
    for row in adjudications:
        members = row.get("memberWriteEventIDs")
        if not isinstance(members, list) or not members or any(x not in write_ids for x in members):
            raise ValueError(f"invalid WRITE lineage: {row.get('label')}")
        overlap = covered.intersection(members)
        if overlap:
            raise ValueError(f"WRITE covered more than once: {sorted(overlap)}")
        covered.update(members)
        candidate = candidates.get(row.get("candidateID"))
        if candidate is None:
            raise ValueError(f"missing candidate evidence: {row.get('label')}")
        decision = row.get("decision")
        if decision in {"closed_loss_episode", "closed_history_episode"}:
            require_candidate_gate(row, candidate)
            target = row.get("finalizedTarget")
            if not isinstance(target, dict):
                raise ValueError(f"closed episode lacks finalized target: {row.get('label')}")
            resolved_history_segments(target)
            closed.append((row, candidate))
        elif decision == "exclude_unresolved_episode":
            exclusions.append({
                "schemaVersion": 1,
                "episodeVersion": EPISODE_VERSION,
                "reason": row.get("reason") or "unresolved_episode",
                "memberWriteEventIDs": members,
                "candidateID": row.get("candidateID"),
                "label": row.get("label"),
            })
        else:
            raise ValueError(f"unsupported episode decision: {decision}")
    missing = write_ids - covered
    if missing:
        raise ValueError(
            f"strict episode coverage missing {len(missing)} source WRITEs; "
            f"first={sorted(missing)[0]}"
        )

    closed_thresholds = {
        (row.get("minimumAuthoredCharacters"), row.get("minimumWords"))
        for row, _ in closed
    }
    if len(closed_thresholds) != 1:
        raise ValueError(f"closed episode thresholds disagree: {closed_thresholds}")
    minimum_authored_characters, minimum_words = next(iter(closed_thresholds))
    if not isinstance(minimum_authored_characters, int) or not isinstance(minimum_words, int):
        raise ValueError("closed episode thresholds are missing")

    normalized_events: list[dict[str, Any]] = []
    normalized_by_id: dict[str, dict[str, Any]] = {}
    micro_to_episode: dict[str, str] = {}
    suppressed_repeated_reads: set[str] = set()
    loss_units: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for adjudication, candidate in closed:
        members = adjudication["memberWriteEventIDs"]
        member_events = [event_by_id[value] for value in members]
        episode_event_id = stable_id("evt_", {"closedEpisodeMembers": members})
        episode_id = stable_id("episode_", {"memberWriteEventIDs": members})
        for member in members:
            micro_to_episode[member] = episode_event_id
        for assessment in (candidate.get("causalEvidence") or {}).get(
            "interveningReadAssessments", []
        ):
            status = assessment.get("status") or assessment.get("classification")
            if status in {
                "exact_repeat_available_at_episode_onset",
                "exact_repeat_already_in_episode_onset_model_input",
                "repeated_non_novel_read",
            } and isinstance(assessment.get("eventID"), str):
                suppressed_repeated_reads.add(assessment["eventID"])

        full_target = adjudication["finalizedTarget"]
        history_segments = resolved_history_segments(full_target)
        destination = serialized_destination(member_events[0])
        began_at = min(event["beganAt"] for event in member_events)
        available_at = max(event["availableAt"] for event in member_events)
        source_record_ids = sorted({
            record_id
            for event in member_events
            for record_id in event.get("sourceRecordIDs", [])
        })
        compact = {
            "kind": "write",
            "destination": destination,
            "operation": "closed_composition_episode",
            "authorshipResolution": "resolved",
            "authorshipSegments": history_segments,
        }
        audit = {
            **compact,
            "schemaVersion": 1,
            "resolvedCompletion": full_target["resolvedContent"],
            "episodeVersion": EPISODE_VERSION,
            "memberWriteEventIDs": members,
            "closureReason": adjudication.get("closureReason"),
            "decision": adjudication["decision"],
        }
        event = {
            "schemaVersion": 1,
            "kind": "write",
            "sourceEventID": episode_event_id,
            "sessionID": member_events[0].get("sessionID"),
            "corpusID": manifest["corpusID"],
            "beganAt": began_at,
            "availableAt": available_at,
            "serialized": canonical_json(compact),
            "auditSerialized": canonical_json(audit),
            "sourceRecordIDs": source_record_ids,
            "episodeID": episode_id,
            "memberWriteEventIDs": members,
            "memberCount": len(members),
            "episodeDecision": adjudication["decision"],
            "candidateID": adjudication["candidateID"],
        }
        normalized_events.append(event)
        normalized_by_id[episode_event_id] = event
        if adjudication["decision"] == "closed_loss_episode":
            loss_units.append((adjudication, candidate, event))

    for event in source_events:
        if event["kind"] != "read" or event["sourceEventID"] in suppressed_repeated_reads:
            continue
        normalized_events.append(event)
        normalized_by_id[event["sourceEventID"]] = event
    normalized_events.sort(key=lambda row: (
        row["availableAt"], row.get("beganAt") or row["availableAt"], row["sourceEventID"]
    ))

    normalized_blocks: list[dict[str, Any]] = []
    normalized_block_by_id: dict[str, dict[str, Any]] = {}
    for block in source_blocks:
        if block["contextBlockType"] == "coverage_gap":
            normalized_blocks.append(block)
            normalized_block_by_id[block["contextBlockID"]] = block
    for event in normalized_events:
        block = {
            "contextBlockID": event["sourceEventID"],
            "contextBlockType": "semantic_event",
            "sessionID": event.get("sessionID"),
            "availableAt": event["availableAt"],
            "serialized": event["serialized"],
            "sourceEventID": event["sourceEventID"],
        }
        normalized_blocks.append(block)
        normalized_block_by_id[block["contextBlockID"]] = block
    normalized_blocks.sort(key=lambda row: (
        row.get("availableAt") or row.get("beforeAt") or "",
        row["contextBlockID"],
    ))

    def normalized_context(source_example: dict[str, Any], began_at: str, current: str) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for block_id in source_example.get("contextBlockIDs", []):
            source_block = block_by_id[block_id]
            if source_block["contextBlockType"] == "coverage_gap":
                mapped = block_id
            else:
                source_event = event_by_id[block_id]
                if source_event["kind"] == "write":
                    mapped = micro_to_episode.get(block_id)
                    if mapped is None:
                        continue
                else:
                    mapped = block_id
                    if mapped in suppressed_repeated_reads:
                        continue
            if mapped == current or mapped in seen:
                continue
            normalized_block = normalized_block_by_id[mapped]
            if normalized_block["contextBlockType"] == "semantic_event":
                if normalized_by_id[mapped]["availableAt"] >= began_at:
                    continue
            seen.add(mapped)
            result.append(mapped)
        return result

    episode_examples: list[dict[str, Any]] = []
    for adjudication, candidate, event in loss_units:
        members = adjudication["memberWriteEventIDs"]
        member_examples = [example_by_target[value] for value in members if value in example_by_target]
        if not member_examples:
            raise ValueError(f"loss episode has no source target: {adjudication.get('label')}")
        first = min(member_examples, key=lambda row: (row["targetBeganAt"], row["exampleID"]))
        conditioning = candidate.get("initialConditioningState")
        if not isinstance(conditioning, dict):
            raise ValueError(f"candidate lacks initial conditioning: {adjudication.get('label')}")
        query = serialize_query(conditioning)
        context_ids = normalized_context(first, event["beganAt"], event["sourceEventID"])
        serialized_blocks = [normalized_block_by_id[value]["serialized"] for value in context_ids]
        context = "\n".join(serialized_blocks)
        target = model_target(adjudication["finalizedTarget"])
        target_record_ids = event["sourceRecordIDs"]
        context_record_ids = sorted({
            record_id
            for block_id in context_ids
            if normalized_block_by_id[block_id]["contextBlockType"] == "semantic_event"
            for record_id in normalized_by_id[block_id].get("sourceRecordIDs", [])
        })
        episode_examples.append({
            **first,
            "schemaVersion": 12,
            "conversionVersion": CONVERSION_VERSION,
            "exampleID": stable_id("example_", {"episodeEventID": event["sourceEventID"]}),
            "targetEventID": event["sourceEventID"],
            "targetUnitID": event["episodeID"],
            "targetUnitType": "closed_composition_episode",
            "targetBeganAt": event["beganAt"],
            "targetAvailableAt": event["availableAt"],
            "conditioningState": conditioning,
            "query": query,
            "contextBlockIDs": context_ids,
            "contextEventIDs": [
                value for value in context_ids
                if normalized_block_by_id[value]["contextBlockType"] == "semantic_event"
            ],
            "context": context,
            "modelInput": query if not context else context + "\n" + query,
            "target": target,
            "targetSourceRecordIDs": target_record_ids,
            "contextSourceRecordIDs": context_record_ids,
            "sourceRecordIDs": sorted(set(target_record_ids) | set(context_record_ids)),
            "episode": {
                "schemaVersion": 2,
                "episodeVersion": EPISODE_VERSION,
                "memberWriteEventIDs": members,
                "memberCount": len(members),
                "decision": adjudication["decision"],
                "label": adjudication.get("label"),
                "candidateID": adjudication["candidateID"],
                "closureReason": adjudication.get("closureReason"),
                "conditioningSource": "candidate_episode_onset",
            },
            "targetMetadata": {
                "episodeVersion": EPISODE_VERSION,
                "memberWriteEventIDs": members,
                "microWriteCount": len(members),
                "availableAt": event["availableAt"],
                "decision": adjudication["decision"],
            },
        })

    episode_examples.sort(key=lambda row: (row["targetBeganAt"], row["exampleID"]))
    for ordinal, row in enumerate(episode_examples):
        row["chronologicalOrdinal"] = ordinal
        row["experimentBlockID"] = f"block-{ordinal // 50 + 1:04d}"

    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        write_jsonl(temporary / "events.jsonl", normalized_events)
        write_jsonl(temporary / "context-blocks.jsonl", normalized_blocks)
        write_jsonl(temporary / "examples.jsonl", episode_examples)
        write_jsonl(temporary / "episode-exclusions.jsonl", exclusions)
        write_jsonl(temporary / "episode-adjudications.jsonl", adjudications)
        for name in ("gaps.jsonl", "privacy-policy.json"):
            if (source / name).exists():
                shutil.copy2(source / name, temporary / name)
        blocks = []
        for index in range(0, len(episode_examples), 50):
            subset = episode_examples[index:index + 50]
            blocks.append({
                "blockID": f"block-{index // 50 + 1:04d}",
                "exampleIDs": [row["exampleID"] for row in subset],
                "exampleCount": len(subset),
            })
        write_jsonl(temporary / "episode-blocks.jsonl", blocks)
        source_hashes = {
            name: sha256(source / name)
            for name in ("corpus.json", "events.jsonl", "examples.jsonl", "context-blocks.jsonl")
        }
        artifact = {
            "schemaVersion": 2,
            "artifactType": "phase1_episode_corpus",
            "conversionVersion": CONVERSION_VERSION,
            "episodeVersion": EPISODE_VERSION,
            "corpusID": stable_id("episode_corpus_", {
                "sourceCorpusID": manifest["corpusID"],
                "sourceHashes": source_hashes,
                "adjudicationsSHA256": sha256(adjudications_path),
                "candidateEvidenceSHA256": [sha256(path) for path in candidate_paths],
            }),
            "sessionID": None,
            "sourceCorpusID": manifest["corpusID"],
            "source": {
                "path": str(source.resolve()),
                "digestsSHA256": source_hashes,
                "adjudicationsSHA256": sha256(adjudications_path),
                "candidateEvidenceSHA256": {
                    str(path.resolve()): sha256(path) for path in candidate_paths
                },
            },
            "serialization": manifest["serialization"],
            "objective": {
                **manifest["objective"],
                "predictionUnit": "closed_composition_episode",
                "microWritesReceiveLoss": False,
                "microWritesAppearInModelHistory": False,
                "modelFacingWritesAreClosedEpisodes": True,
            },
            "eligibility": {
                "sourceSemanticEventEligibility": manifest["eligibility"],
                "episodePolicy": "complete evidence-bound coverage; ambiguity excluded",
                "closedEpisodeEvidenceRequired": [
                    "continuous_replayable_state",
                    "stable_logical_editable_identity",
                    "no_novel_causally_available_read",
                    "no_overlapping_outside_write",
                    "observed_closure_boundary",
                ],
                "minimumTrimmedAuthoredCharacters": minimum_authored_characters,
                "minimumAuthoredWords": minimum_words,
                "groundedPasteActionBypassesMinimumAuthoredContent": False,
                "closedButBelowThresholdRemainsHistoryOnly": True,
                "unresolvedOrUnclosedMicroWritesAppearInModelHistory": False,
            },
            "timing": manifest["timing"],
            "counts": {
                "sourceSemanticEvents": len(source_events),
                "sourceWrites": len(write_ids),
                "convertedEvents": len(normalized_events),
                "closedEpisodeEvents": len(closed),
                "examples": len(episode_examples),
                "multiWriteEpisodes": sum(
                    len(row["memberWriteEventIDs"]) > 1 for row, _ in closed
                ),
                "multiWriteLossEpisodes": sum(
                    event["memberCount"] > 1 for _, _, event in loss_units
                ),
                "sourceMicroWritesAbsorbed": sum(len(row["memberWriteEventIDs"]) for row, _ in closed),
                "sourceMicroWritesExcluded": sum(
                    len(row["memberWriteEventIDs"]) for row in adjudications
                    if row["decision"] == "exclude_unresolved_episode"
                ),
                "excludedEpisodeGroups": len(exclusions),
                "suppressedRepeatedReadsInsideEpisodes": len(suppressed_repeated_reads),
            },
        }
        artifact["sessionID"] = artifact["corpusID"]
        artifact["artifactDigestsSHA256"] = {
            name: sha256(temporary / name)
            for name in (
                "events.jsonl", "context-blocks.jsonl", "examples.jsonl",
                "episode-exclusions.jsonl", "episode-adjudications.jsonl",
                "episode-blocks.jsonl",
            )
        }
        (temporary / "corpus.json").write_bytes(canonical_bytes(artifact))
        (temporary / "dataset.json").write_bytes(canonical_bytes(artifact))
        if output.exists():
            raise ValueError(f"output already exists: {output}")
        temporary.rename(output)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--adjudications", required=True, type=Path)
    parser.add_argument("--candidate-evidence", action="append", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    artifact = construct(
        args.corpus.resolve(), args.adjudications.resolve(),
        [path.resolve() for path in args.candidate_evidence], args.output.resolve(),
    )
    print(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

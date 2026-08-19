#!/usr/bin/env python3
"""Build non-authoritative Phase 1 composition-episode review artifacts."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BUILDER_VERSION = "phase1-episode-v0-shadow-r2"
SEMANTIC_ANCHOR_MINIMUM_CHARACTERS = 32


class ReviewError(RuntimeError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ReviewError(f"expected JSON object: {path}")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ReviewError(f"expected object at {path}:{line_number}")
            rows.append(value)
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("wb") as handle:
        for row in rows:
            handle.write(canonical_bytes(row))


def timestamp(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def target_text(example: dict[str, Any]) -> str:
    return marker_target(example.get("target", {}))


def marker_target(target: dict[str, Any]) -> str:
    pieces: list[str] = []
    for segment in target.get("segments", []):
        if segment.get("type") == "paste":
            pieces.append("<|paste|>")
        else:
            pieces.append(str(segment.get("content", "")))
    return "".join(pieces)


def utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def utf16_slice(value: str, location: int, length: int) -> str | None:
    encoded = value.encode("utf-16-le")
    start = location * 2
    end = (location + length) * 2
    if start < 0 or end > len(encoded):
        return None
    try:
        return encoded[start:end].decode("utf-16-le")
    except UnicodeDecodeError:
        return None


def minimal_edit(before: str, after: str) -> dict[str, Any]:
    prefix = 0
    limit = min(len(before), len(after))
    while prefix < limit and before[prefix] == after[prefix]:
        prefix += 1
    suffix = 0
    before_remaining = len(before) - prefix
    after_remaining = len(after) - prefix
    while (
        suffix < before_remaining
        and suffix < after_remaining
        and before[len(before) - suffix - 1] == after[len(after) - suffix - 1]
    ):
        suffix += 1
    before_end = len(before) - suffix if suffix else len(before)
    after_end = len(after) - suffix if suffix else len(after)
    removed = before[prefix:before_end]
    inserted = after[prefix:after_end]
    if removed and inserted:
        operation = "replace"
    elif removed:
        operation = "delete"
    else:
        operation = "insert"
    return {
        "operation": operation,
        "characterOffset": prefix,
        "utf16Offset": utf16_length(before[:prefix]),
        "removedContent": removed,
        "content": inserted,
    }


def normalized_semantic_anchor(value: str) -> str:
    """Remove AX layout scaffolding while retaining semantic characters."""
    return "".join(
        character
        for character in value
        if not character.isspace() and character not in {"\u200b", "\ufeff"}
    )


def semantic_anchor_diagnostic(
    initial_value: str,
    net_edit: dict[str, Any] | None,
    cursor_context: Any,
) -> dict[str, Any]:
    """Test whether independent semantic ranges uniquely bracket an edit.

    Some rich editors expose a selected-range coordinate that disagrees with
    their separately queried AXStringForRange content. This diagnostic does not
    rewrite either observation. It accepts the semantic observation only when
    long anchors on both sides uniquely meet at the observed net-edit boundary.
    """
    result: dict[str, Any] = {
        "status": "not_proven",
        "normalization": "remove_unicode_whitespace_zero_width_space_and_bom",
        "minimumAnchorCharacters": SEMANTIC_ANCHOR_MINIMUM_CHARACTERS,
    }
    if net_edit is None or not isinstance(cursor_context, dict):
        result["reason"] = "missing_edit_or_cursor_context"
        return result
    left_raw = cursor_context.get("leftContext")
    right_raw = cursor_context.get("rightContext")
    selected_raw = cursor_context.get("selectedText")
    if not all(isinstance(value, str) for value in (left_raw, right_raw, selected_raw)):
        result["reason"] = "incomplete_semantic_cursor_context"
        return result
    left = normalized_semantic_anchor(left_raw)
    right = normalized_semantic_anchor(right_raw)
    selected = normalized_semantic_anchor(selected_raw)
    removed = normalized_semantic_anchor(net_edit.get("removedContent", ""))
    offset = net_edit.get("characterOffset")
    removed_characters = len(net_edit.get("removedContent", ""))
    if not isinstance(offset, int) or offset < 0:
        result["reason"] = "invalid_edit_offset"
        return result
    if (
        len(left) < SEMANTIC_ANCHOR_MINIMUM_CHARACTERS
        or len(right) < SEMANTIC_ANCHOR_MINIMUM_CHARACTERS
    ):
        result["reason"] = "semantic_anchors_too_short"
        return result
    if selected != removed:
        result["reason"] = "semantic_selection_does_not_match_removed_content"
        return result
    before_prefix = normalized_semantic_anchor(initial_value[:offset])
    after_removed = normalized_semantic_anchor(
        initial_value[offset + removed_characters :]
    )
    normalized_initial = normalized_semantic_anchor(initial_value)
    left_occurrences = normalized_initial.count(left)
    right_occurrences = normalized_initial.count(right)
    left_end = normalized_initial.find(left) + len(left) if left_occurrences else None
    right_start = normalized_initial.find(right) if right_occurrences else None
    normalized_boundary = len(before_prefix)
    result.update(
        {
            "leftAnchorCharacters": len(left),
            "rightAnchorCharacters": len(right),
            "leftAnchorOccurrences": left_occurrences,
            "rightAnchorOccurrences": right_occurrences,
            "leftAnchorEnd": left_end,
            "rightAnchorStart": right_start,
            "observedEditBoundary": normalized_boundary,
        }
    )
    if left_occurrences != 1 or right_occurrences != 1:
        result["reason"] = "semantic_anchor_not_unique"
        return result
    if not before_prefix.endswith(left) or not after_removed.startswith(right):
        result["reason"] = "semantic_anchors_do_not_bracket_observed_edit"
        return result
    if left_end != normalized_boundary or right_start != normalized_boundary:
        result["reason"] = "semantic_anchors_meet_at_different_boundary"
        return result
    result["status"] = "proven"
    result["reason"] = "unique_semantic_anchors_bracket_observed_edit"
    return result


def observation_projection(observation: Any) -> dict[str, Any] | None:
    if not isinstance(observation, dict):
        return None
    value = observation.get("value")
    return {
        "observationID": observation.get("observationID"),
        "observedAt": observation.get("observedAt"),
        "reason": observation.get("reason"),
        "selectedRangeLocation": observation.get("selectedRangeLocation"),
        "selectedRangeLength": observation.get("selectedRangeLength"),
        "valueRepresentedPlaceholder": observation.get(
            "valueRepresentedPlaceholder"
        ),
        "valueWasTruncated": observation.get("valueWasTruncated"),
        "value": value if isinstance(value, str) else None,
        "valueSHA256": (
            hashlib.sha256(value.encode()).hexdigest()
            if isinstance(value, str)
            else None
        ),
        "valueCharacters": len(value) if isinstance(value, str) else None,
    }


def raw_observations(
    record: dict[str, Any],
) -> list[tuple[str, dict[str, Any], str | None]]:
    result: list[tuple[str, dict[str, Any], str | None]] = []
    for field, source in (("before", "before"), ("after", "terminal_after")):
        observation = record.get(field)
        if isinstance(observation, dict):
            result.append((source, observation, None))
    for field, source in (
        ("returnCheckpoints", "pre_return_checkpoint"),
        ("pasteCheckpoints", "post_paste_checkpoint"),
        ("mutationCheckpoints", "post_input_checkpoint"),
    ):
        for checkpoint in record.get(field, []):
            if not isinstance(checkpoint, dict):
                continue
            observation = checkpoint.get("observation")
            if isinstance(observation, dict):
                result.append((source, observation, checkpoint.get("checkpointID")))
            if field == "pasteCheckpoints":
                pre_paste = checkpoint.get("prePasteObservation")
                if isinstance(pre_paste, dict):
                    result.append(
                        (
                            "pre_paste_checkpoint",
                            pre_paste,
                            checkpoint.get("checkpointID"),
                        )
                    )
    return result


def reducer_selected_observation(
    record: dict[str, Any], semantic_event: dict[str, Any]
) -> tuple[str, dict[str, Any], str | None]:
    reduction = semantic_event.get("reduction")
    if not isinstance(reduction, dict):
        raise ReviewError("semantic WRITE lacks reduction provenance")
    selected_id = reduction.get("selectedObservationID")
    selected_source = reduction.get("selectedObservationSource")
    matches = [
        item
        for item in raw_observations(record)
        if item[1].get("observationID") == selected_id
        and item[0] == selected_source
    ]
    if len(matches) != 1:
        raise ReviewError(
            f"selected observation {selected_id!r} occurs {len(matches)} times in raw record"
        )
    source, observation, checkpoint_id = matches[0]
    if selected_source != source:
        raise ReviewError("selected observation source mismatch after source filtering")
    reduced_checkpoint = semantic_event.get("usedCheckpointID")
    if reduced_checkpoint is not None and reduced_checkpoint != checkpoint_id:
        raise ReviewError("selected checkpoint does not match reducer provenance")
    return source, observation, checkpoint_id


def logical_initial_value(
    example: dict[str, Any], observation: dict[str, Any]
) -> tuple[str, str]:
    cursor = example.get("conditioningState", {}).get("cursorContext", {})
    if cursor.get("fieldState") == "unpopulated_prompt":
        return "", "conditioning_field_state_unpopulated_prompt"
    value = observation.get("value")
    if not isinstance(value, str):
        raise ReviewError("initial raw observation has no string value")
    if value != "" and logical_observation_value(observation) == "":
        return "", "accessibility_value_represented_placeholder"
    return value, "raw_before_value"


def logical_observation_value(observation: Any) -> str | None:
    if not isinstance(observation, dict):
        return None
    value = observation.get("value")
    if not isinstance(value, str):
        return None
    placeholder = observation.get("placeholderValue")
    represents_placeholder = observation.get("valueRepresentedPlaceholder") is True
    if isinstance(placeholder, str):
        represents_placeholder = represents_placeholder or bool(placeholder.strip()) and (
            value.strip() == placeholder.strip()
        )
    return "" if represents_placeholder else value


def identity_projection(record: dict[str, Any]) -> dict[str, Any]:
    identity = record.get("targetIdentity")
    if not isinstance(identity, dict):
        return {}
    return {
        key: identity.get(key)
        for key in (
            "bundleIdentifier",
            "processIdentifier",
            "elementHash",
            "windowTitle",
            "role",
            "fieldDescription",
            "fieldLabel",
        )
    }


@dataclass(frozen=True)
class Neighborhood:
    label: str
    first: int
    last: int
    category: str | None = None
    rationale: str | None = None


def parse_neighborhood(value: str) -> Neighborhood:
    try:
        label, bounds = value.split("=", 1)
        first_text, last_text = bounds.split(":", 1)
        first = int(first_text)
        last = int(last_text)
    except (ValueError, TypeError) as error:
        raise argparse.ArgumentTypeError(
            "expected LABEL=FIRST:LAST using one-based inclusive ordinals"
        ) from error
    if not label or first <= 0 or last < first:
        raise argparse.ArgumentTypeError("invalid neighborhood label or bounds")
    return Neighborhood(label=label, first=first, last=last)


def load_selection(path: Path, corpus_id: str) -> tuple[list[Neighborhood], dict[str, Any]]:
    selection = load_json(path)
    if selection.get("corpusID") != corpus_id:
        raise ReviewError("selection corpusID does not match source corpus")
    rows = selection.get("neighborhoods")
    if not isinstance(rows, list) or not rows:
        raise ReviewError("selection must contain nonempty neighborhoods")
    neighborhoods = []
    for row in rows:
        if not isinstance(row, dict):
            raise ReviewError("selection neighborhood must be an object")
        label = row.get("label")
        first = row.get("firstOneBasedExampleOrdinal")
        last = row.get("lastOneBasedExampleOrdinal")
        category = row.get("category")
        rationale = row.get("rationale")
        if (
            not isinstance(label, str)
            or not label
            or not isinstance(first, int)
            or not isinstance(last, int)
            or first <= 0
            or last < first
            or not isinstance(category, str)
            or not category
            or not isinstance(rationale, str)
            or not rationale
        ):
            raise ReviewError(f"invalid selection neighborhood: {row!r}")
        neighborhoods.append(
            Neighborhood(
                label=label,
                first=first,
                last=last,
                category=category,
                rationale=rationale,
            )
        )
    labels = [value.label for value in neighborhoods]
    if len(set(labels)) != len(labels):
        raise ReviewError("selection neighborhood labels must be unique")
    return neighborhoods, selection


def discover_raw_sessions(
    project: Path, session_ids: set[str]
) -> dict[str, tuple[Path, str]]:
    result: dict[str, tuple[Path, str]] = {}
    for manifest_path in (project / "coupled-data").glob("*/session.json"):
        try:
            manifest = load_json(manifest_path)
        except (OSError, json.JSONDecodeError, ReviewError):
            continue
        session_id = manifest.get("sessionID")
        raw_path = manifest_path.parent / "raw.jsonl"
        if session_id in session_ids and raw_path.is_file():
            relative = str(raw_path.relative_to(project))
            candidate = (raw_path, relative)
            previous = result.get(session_id)
            if previous is not None and sha256(previous[0]) != sha256(raw_path):
                raise ReviewError(f"multiple different raw journals for {session_id}")
            result[session_id] = candidate
    missing = sorted(session_ids - set(result))
    if missing:
        raise ReviewError(f"cannot find raw journals for sessions: {missing}")
    return result


def matching_hashed_artifacts(
    project: Path, filename: str, expected_sha256: str
) -> list[Path]:
    matches = [
        path
        for path in (project / "coupled-data").glob(f"**/{filename}")
        if path.is_file() and sha256(path) == expected_sha256
    ]
    return sorted(matches, key=lambda value: str(value))


def discover_semantic_sessions(
    project: Path,
    corpus_manifest: dict[str, Any],
    session_ids: set[str],
) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, dict[str, Any]]]:
    """Resolve the exact semantic artifacts bound into the frozen corpus."""
    events_by_session: dict[str, dict[str, dict[str, Any]]] = {}
    sources: dict[str, dict[str, Any]] = {}
    source_manifests = {
        source.get("sessionID"): source
        for source in corpus_manifest.get("sources", [])
        if source.get("sessionID") in session_ids
    }
    missing_sources = sorted(session_ids - set(source_manifests))
    if missing_sources:
        raise ReviewError(f"corpus does not identify sessions: {missing_sources}")
    for session_id in sorted(session_ids):
        corpus_source = source_manifests[session_id]
        dataset_digest = corpus_source.get("digestsSHA256", {}).get("dataset.json")
        if not isinstance(dataset_digest, str):
            raise ReviewError(f"corpus source lacks dataset digest: {session_id}")
        dataset_paths = matching_hashed_artifacts(project, "dataset.json", dataset_digest)
        if not dataset_paths:
            raise ReviewError(f"cannot find causal dataset artifact for {session_id}")
        dataset = load_json(dataset_paths[0])
        if dataset.get("sessionID") != session_id:
            raise ReviewError(f"causal dataset session mismatch: {session_id}")
        source_digests = dataset.get("source", {}).get("digestsSHA256", {})
        reduction_digest = source_digests.get("reduction.json")
        events_digest = source_digests.get("events.jsonl")
        if not isinstance(reduction_digest, str) or not isinstance(events_digest, str):
            raise ReviewError(f"causal dataset lacks semantic lineage: {session_id}")
        reduction_paths = matching_hashed_artifacts(
            project, "reduction.json", reduction_digest
        )
        semantic_path = next(
            (
                path.parent / "events.jsonl"
                for path in reduction_paths
                if (path.parent / "events.jsonl").is_file()
                and sha256(path.parent / "events.jsonl") == events_digest
            ),
            None,
        )
        if semantic_path is None:
            raise ReviewError(f"cannot find bound semantic events for {session_id}")
        semantic_rows = load_jsonl(semantic_path)
        by_id = {row.get("eventID"): row for row in semantic_rows}
        if len(by_id) != len(semantic_rows) or None in by_id:
            raise ReviewError(f"invalid semantic event IDs for {session_id}")
        events_by_session[session_id] = by_id
        sources[session_id] = {
            "causalDatasetPath": str(dataset_paths[0].relative_to(project)),
            "causalDatasetSHA256": dataset_digest,
            "semanticEventsPath": str(semantic_path.relative_to(project)),
            "semanticEventsSHA256": events_digest,
            "reductionPath": str(semantic_path.parent.joinpath("reduction.json").relative_to(project)),
            "reductionSHA256": reduction_digest,
            "reducerVersion": dataset.get("source", {}).get("reducerVersion"),
        }
    return events_by_session, sources


def load_raw_records(
    raw_sessions: dict[str, tuple[Path, str]], needed_ids: set[str]
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    records: dict[str, dict[str, Any]] = {}
    sources: dict[str, dict[str, Any]] = {}
    for session_id, (path, relative) in raw_sessions.items():
        sources[session_id] = {
            "path": relative,
            "sha256": sha256(path),
        }
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                record_id = value.get("recordID")
                if record_id not in needed_ids:
                    continue
                if record_id in records:
                    raise ReviewError(f"duplicate raw record ID: {record_id}")
                records[record_id] = {
                    "record": value,
                    "rawPath": relative,
                    "rawLine": line_number,
                }
    missing = sorted(needed_ids - set(records))
    if missing:
        raise ReviewError(f"missing {len(missing)} raw records")
    return records, sources


def stable_destination(identity: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(
        identity.get(key)
        for key in (
            "bundleIdentifier",
            "windowTitle",
            "role",
            "fieldDescription",
            "fieldLabel",
        )
    )


def logical_member_before(
    record: dict[str, Any], semantic_event: dict[str, Any]
) -> str | None:
    field_state = (
        semantic_event.get("conditioningState", {})
        .get("cursorContext", {})
        .get("fieldState")
    )
    if field_state == "unpopulated_prompt":
        return ""
    return logical_observation_value(record.get("before"))


def member_projection(
    ordinal: int | None,
    example: dict[str, Any] | None,
    event: dict[str, Any],
    raw_entry: dict[str, Any],
    semantic_event: dict[str, Any],
) -> dict[str, Any]:
    record = raw_entry["record"]
    selected_source, selected_observation, selected_checkpoint = (
        reducer_selected_observation(record, semantic_event)
    )
    audit = json.loads(event.get("auditSerialized", "{}"))
    reduction = semantic_event.get("reduction", {})
    return {
        "oneBasedExampleOrdinal": ordinal,
        "exampleID": example.get("exampleID") if example else None,
        "currentTargetEligibility": (
            "loss_bearing_example" if example else "semantic_history_only"
        ),
        "writeEventID": event["sourceEventID"],
        "beganAt": event["beganAt"],
        "availableAt": event["availableAt"],
        "currentLossTarget": target_text(example) if example else None,
        "currentTarget": example.get("target") if example else None,
        "operation": audit.get("operation"),
        "characterOffset": audit.get("characterOffset"),
        "removedContent": audit.get("removedContent", ""),
        "boundaryReason": audit.get("boundaryReason"),
        "application": audit.get("appName"),
        "windowTitle": audit.get("windowTitle"),
        "sourceRecordID": record.get("recordID"),
        "rawPath": raw_entry["rawPath"],
        "rawLine": raw_entry["rawLine"],
        "targetIdentity": identity_projection(record),
        "inputHints": record.get("inputHints", []),
        "inputEventCount": record.get("inputEventCount"),
        "beforeAXErrors": record.get("beforeAXErrors", []),
        "afterAXErrors": record.get("afterAXErrors", []),
        "before": observation_projection(record.get("before")),
        "beforeLogicalValue": logical_member_before(record, semantic_event),
        "selectedTerminalObservationSource": selected_source,
        "selectedTerminalCheckpointID": selected_checkpoint,
        "selectedTerminalObservation": observation_projection(selected_observation),
        "selectedTerminalLogicalValue": logical_observation_value(selected_observation),
        "semanticReduction": {
            "rule": reduction.get("rule"),
            "reason": reduction.get("reason"),
            "selectedObservationID": reduction.get("selectedObservationID"),
            "selectedObservationSource": reduction.get("selectedObservationSource"),
            "derivationObservationSource": semantic_event.get(
                "derivationObservationSource"
            ),
            "usedObservationCapturedAt": semantic_event.get(
                "usedObservationCapturedAt"
            ),
        },
    }


def serialized_destination(event: dict[str, Any]) -> tuple[Any, Any]:
    try:
        value = json.loads(event.get("serialized", "{}"))
    except json.JSONDecodeError:
        return None, None
    destination = value.get("destination")
    if not isinstance(destination, dict):
        return None, None
    return destination.get("application"), destination.get("window")


def event_review_projection(event: dict[str, Any]) -> dict[str, Any]:
    try:
        serialized = json.loads(event.get("serialized", "{}"))
    except json.JSONDecodeError:
        serialized = {}
    try:
        audit = json.loads(event.get("auditSerialized", "{}"))
    except json.JSONDecodeError:
        audit = {}
    content = audit.get("content")
    if not isinstance(content, str):
        content = serialized.get("content")
    return {
        "eventID": event.get("sourceEventID"),
        "kind": event.get("kind"),
        "beganAt": event.get("beganAt"),
        "availableAt": event.get("availableAt"),
        "destination": serialized.get("destination") or serialized.get("source"),
        "content": content,
        "boundaryReason": audit.get("boundaryReason"),
    }


def neighborhood_write_events(
    neighborhood: Neighborhood,
    examples: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    anchors = examples[neighborhood.first - 1 : neighborhood.last]
    event_by_id = {event["sourceEventID"]: event for event in events}
    first = event_by_id[anchors[0]["targetEventID"]]
    last = event_by_id[anchors[-1]["targetEventID"]]
    destination = serialized_destination(first)
    began = timestamp(first["beganAt"])
    closed = timestamp(last["availableAt"])
    return sorted(
        [
            event
            for event in events
            if event.get("kind") == "write"
            and event.get("sessionID") == first.get("sessionID")
            and serialized_destination(event) == destination
            and began <= timestamp(event["beganAt"])
            and timestamp(event["availableAt"]) <= closed
        ],
        key=lambda event: (event["beganAt"], event["sourceEventID"]),
    )


def intervening_events(
    events: list[dict[str, Any]],
    member_event_ids: set[str],
    began: dt.datetime,
    closed: dt.datetime,
) -> list[dict[str, Any]]:
    """Return causally available events inside a proposed episode interval."""
    return [
        event
        for event in events
        if event["sourceEventID"] not in member_event_ids
        and began <= timestamp(event["availableAt"]) <= closed
    ]


def continuity_evidence(members: list[dict[str, Any]]) -> dict[str, Any]:
    transitions = []
    for previous, following in zip(members, members[1:]):
        previous_value = previous.get("selectedTerminalLogicalValue")
        following_value = following.get("beforeLogicalValue")
        matches = (
            isinstance(previous_value, str)
            and isinstance(following_value, str)
            and previous_value == following_value
        )
        transitions.append(
            {
                "fromWriteEventID": previous["writeEventID"],
                "toWriteEventID": following["writeEventID"],
                "matchesExactly": matches,
                "fromObservationID": previous["selectedTerminalObservation"].get(
                    "observationID"
                ),
                "toObservationID": following["before"].get("observationID"),
                "fromValueSHA256": previous["selectedTerminalObservation"].get(
                    "valueSHA256"
                ),
                "toValueSHA256": following["before"].get("valueSHA256"),
            }
        )
    return {
        "continuousReplayableState": all(
            transition["matchesExactly"] for transition in transitions
        ),
        "transitions": transitions,
    }


def mechanical_gate_failures(
    *,
    continuous_replay: bool,
    exact_identity_stable: bool,
    intervening_reads: list[dict[str, Any]],
    overlapping_outside_writes: list[dict[str, Any]],
) -> list[str]:
    failures = []
    if not continuous_replay:
        failures.append("discontinuous_editable_state")
    if not exact_identity_stable:
        failures.append("logical_editable_identity_changed")
    if intervening_reads:
        failures.append("causally_available_read_inside_candidate")
    if overlapping_outside_writes:
        failures.append("outside_write_overlaps_candidate")
    return failures


def build_candidate(
    corpus_id: str,
    neighborhood: Neighborhood,
    examples: list[dict[str, Any]],
    events: list[dict[str, Any]],
    raw_records: dict[str, dict[str, Any]],
    semantic_events: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    anchor_examples = examples[neighborhood.first - 1 : neighborhood.last]
    if len(anchor_examples) != neighborhood.last - neighborhood.first + 1:
        raise ReviewError(f"neighborhood exceeds corpus: {neighborhood.label}")
    candidate_events = neighborhood_write_events(neighborhood, examples, events)
    example_by_event_id = {
        example["targetEventID"]: (index, example)
        for index, example in enumerate(examples, 1)
    }
    members = []
    member_event_ids: list[str] = []
    source_record_ids: list[str] = []
    for event in candidate_events:
        event_id = event["sourceEventID"]
        ordinal_and_example = example_by_event_id.get(event_id)
        ordinal = ordinal_and_example[0] if ordinal_and_example else None
        example = ordinal_and_example[1] if ordinal_and_example else None
        lineage = event.get("sourceRecordIDs", [])
        if len(lineage) != 1:
            raise ReviewError(
                f"v0 shadow review requires one raw attempt per member: {event_id}"
            )
        raw_entry = raw_records[lineage[0]]
        semantic_event = semantic_events[event["sessionID"]].get(event_id)
        if semantic_event is None:
            raise ReviewError(f"missing bound semantic event: {event_id}")
        members.append(
            member_projection(
                ordinal, example, event, raw_entry, semantic_event
            )
        )
        member_event_ids.append(event_id)
        source_record_ids.extend(lineage)

    first_member = members[0]
    last_member = members[-1]
    first_raw = raw_records[first_member["sourceRecordID"]]["record"]
    last_raw = raw_records[last_member["sourceRecordID"]]["record"]
    initial_observation = first_raw.get("before")
    if not isinstance(initial_observation, dict):
        raise ReviewError("first member has no complete BEFORE observation")
    initial_value, initial_source = logical_initial_value(
        anchor_examples[0], initial_observation
    )
    terminal_value = last_member["selectedTerminalLogicalValue"]
    net_edit = (
        minimal_edit(initial_value, terminal_value)
        if isinstance(terminal_value, str)
        else None
    )
    first_event = candidate_events[0]
    cursor_fidelity = first_event.get("cursorFidelity")
    cursor_fidelity = cursor_fidelity if isinstance(cursor_fidelity, dict) else {}
    initial_location = cursor_fidelity.get("initialCursorOffsetCharacters")
    initial_length = cursor_fidelity.get("initialSelectionLengthCharacters")
    coordinate_unit = "unicode_character_from_cursor_fidelity"
    cursor_context = anchor_examples[0].get("conditioningState", {}).get(
        "cursorContext", {}
    )
    selected_text = cursor_context.get("selectedText")
    if initial_source != "raw_before_value":
        initial_location = 0
        initial_length = 0
        selected_text = ""
        coordinate_unit = "logical_unpopulated_field"
    elif not isinstance(initial_location, int) or not isinstance(initial_length, int):
        initial_location = initial_observation.get("selectedRangeLocation")
        initial_length = initial_observation.get("selectedRangeLength")
        selected_text = (
            utf16_slice(initial_value, initial_location, initial_length)
            if isinstance(initial_location, int) and isinstance(initial_length, int)
            else None
        )
        coordinate_unit = "utf16_from_raw_accessibility_selection"
    numeric_selection_aligned = bool(
        net_edit is not None
        and isinstance(initial_location, int)
        and isinstance(initial_length, int)
        and (
            net_edit["characterOffset"]
            if coordinate_unit == "unicode_character_from_cursor_fidelity"
            else net_edit["utf16Offset"]
        )
        == initial_location
        and (
            len(net_edit["removedContent"])
            if coordinate_unit == "unicode_character_from_cursor_fidelity"
            else utf16_length(net_edit["removedContent"])
        )
        == initial_length
        and selected_text == net_edit["removedContent"]
        and bool(net_edit["content"])
    )
    semantic_anchor = semantic_anchor_diagnostic(
        initial_value, net_edit, cursor_context
    )
    semantic_anchor_aligned = bool(
        net_edit is not None
        and bool(net_edit["content"])
        and semantic_anchor["status"] == "proven"
    )
    if numeric_selection_aligned:
        alignment_status = "mechanically_representable_numeric_selection"
    elif semantic_anchor_aligned:
        alignment_status = "mechanically_representable_semantic_anchor"
    else:
        alignment_status = "not_mechanically_proven"
    alignment_proven = numeric_selection_aligned or semantic_anchor_aligned

    began = timestamp(first_member["beganAt"])
    closed = timestamp(last_member["availableAt"])
    member_set = set(member_event_ids)
    intervening = intervening_events(events, member_set, began, closed)
    intervening_reads = [event for event in intervening if event["kind"] == "read"]
    intervening_writes = [event for event in intervening if event["kind"] == "write"]
    overlapping_outside_writes = [
        event
        for event in events
        if event.get("kind") == "write"
        and event["sourceEventID"] not in member_set
        and timestamp(event["beganAt"]) <= closed
        and timestamp(event["availableAt"]) >= began
    ]
    identities = [member["targetIdentity"] for member in members]
    exact_identity_stable = all(identity == identities[0] for identity in identities)
    semantic_destination_stable = all(
        stable_destination(identity) == stable_destination(identities[0])
        for identity in identities
    )
    continuity = continuity_evidence(members)
    continuous_replay = continuity["continuousReplayableState"]
    gate_failures = mechanical_gate_failures(
        continuous_replay=continuous_replay,
        exact_identity_stable=exact_identity_stable,
        intervening_reads=intervening_reads,
        overlapping_outside_writes=overlapping_outside_writes,
    )
    if not alignment_proven:
        completion_status = "not_mechanically_proven"
    elif gate_failures:
        completion_status = "mechanical_alignment_gated_out"
    else:
        completion_status = alignment_status
    single_completion = alignment_proven and not gate_failures

    chronologically_sorted = sorted(
        events,
        key=lambda event: (
            event["availableAt"],
            event.get("beganAt") or event["availableAt"],
            event["sourceEventID"],
        ),
    )
    prior_events = [
        event for event in chronologically_sorted if timestamp(event["availableAt"]) < began
    ][-3:]
    following_events = [
        event for event in chronologically_sorted if timestamp(event["availableAt"]) > closed
    ][:5]
    candidate_material = {
        "corpusID": corpus_id,
        "label": neighborhood.label,
        "memberWriteEventIDs": member_event_ids,
    }
    candidate_id = "episode_candidate_" + hashlib.sha256(
        canonical_bytes(candidate_material)
    ).hexdigest()
    explicit_submission_boundary = last_member["boundaryReason"] in {
        "return_pressed",
        "submission_boundary",
    }
    return_observed = (
        "return" in last_member.get("inputHints", [])
        or last_member["selectedTerminalObservationSource"]
        == "pre_return_checkpoint"
    )
    if explicit_submission_boundary:
        closure_status = "objective_submission_observed"
    elif return_observed:
        closure_status = "return_observed_requires_surface_interpretation"
    else:
        closure_status = "ambiguous_requires_review"
    return {
        "schemaVersion": 2,
        "builderVersion": BUILDER_VERSION,
        "authority": "shadow_review_only",
        "candidateID": candidate_id,
        "label": neighborhood.label,
        "selectionCategory": neighborhood.category,
        "selectionRationale": neighborhood.rationale,
        "oneBasedExampleRange": {
            "first": neighborhood.first,
            "last": neighborhood.last,
        },
        "memberWriteEventIDs": member_event_ids,
        "sourceRecordIDs": source_record_ids,
        "beganAt": first_member["beganAt"],
        "candidateAvailableAt": last_member["availableAt"],
        "durationSeconds": (closed - began).total_seconds(),
        "initialConditioningState": anchor_examples[0].get("conditioningState"),
        "initialObservationSource": initial_source,
        "initialObservation": observation_projection(initial_observation),
        "finalObservationSource": last_member["selectedTerminalObservationSource"],
        "finalObservation": last_member["selectedTerminalObservation"],
        "members": members,
        "causalEvidence": {
            "interveningReadCount": len(intervening_reads),
            "interveningWriteCountOutsideCandidate": len(intervening_writes),
            "overlappingOutsideWriteCount": len(overlapping_outside_writes),
            "interveningEvents": [
                {
                    "eventID": event["sourceEventID"],
                    "kind": event["kind"],
                    "availableAt": event["availableAt"],
                    "serialized": event["serialized"],
                }
                for event in intervening
            ],
            "noCausallyAvailableReadDuringCandidate": not intervening_reads,
            "noOverlappingOutsideWrite": not overlapping_outside_writes,
            "overlappingOutsideWrites": [
                event_review_projection(event) for event in overlapping_outside_writes
            ],
        },
        "surfaceEvidence": {
            "exactTargetIdentityStable": exact_identity_stable,
            "semanticDestinationStable": semantic_destination_stable,
            "targetIdentities": identities,
        },
        "continuityEvidence": continuity,
        "mechanicalGates": {
            "passed": not gate_failures,
            "failures": gate_failures,
            "requiresContinuousReplayableState": True,
            "requiresExactLogicalEditableIdentity": True,
            "requiresNoCausallyAvailableRead": True,
            "requiresNoOverlappingOutsideWrite": True,
        },
        "closureContext": {
            "priorEvents": [event_review_projection(event) for event in prior_events],
            "followingEvents": [
                event_review_projection(event) for event in following_events
            ],
        },
        "singleCompletionDiagnostic": {
            "status": completion_status,
            "alignmentStatusBeforeGates": alignment_status,
            "numericSelectionAligned": numeric_selection_aligned,
            "semanticAnchorAligned": semantic_anchor_aligned,
            "semanticAnchor": semantic_anchor,
            "initialSelectionText": selected_text,
            "initialSelectionOffset": initial_location,
            "initialSelectionLength": initial_length,
            "initialSelectionCoordinateUnit": coordinate_unit,
            "netFieldEdit": net_edit,
            "proposedFinalizedTarget": (
                net_edit["content"] if alignment_proven and net_edit is not None else None
            ),
            "warning": (
                "Mechanical representability is not a merge or closure decision. "
                "A reviewer must still decide whether this is one thought, whether "
                "the neighborhood is complete, and whether authorship is proven. "
                "A semantic anchor does not erase or repair a disagreeing numeric "
                "Accessibility coordinate; both observations remain in the audit."
            ),
        },
        "closureEvidence": {
            "lastBoundaryReason": last_member["boundaryReason"],
            "lastSelectedObservationSource": last_member[
                "selectedTerminalObservationSource"
            ],
            "lastInputHints": last_member.get("inputHints", []),
            "returnObserved": return_observed,
            "objectiveSubmissionBoundary": explicit_submission_boundary,
            "status": closure_status,
        },
    }


def render_excerpt(value: str | None, maximum: int = 700) -> str:
    if value is None:
        return "[missing]"
    if len(value) <= maximum:
        return value
    half = maximum // 2
    return value[:half] + "\n… [excerpt clipped] …\n" + value[-half:]


def marked_transition_state(
    value: str, offset: int, changed_length: int, radius: int = 320
) -> str:
    start = max(0, offset - radius)
    end = min(len(value), offset + changed_length + radius)
    prefix = "…" if start else ""
    suffix = "…" if end < len(value) else ""
    local_offset = offset - start
    local_end = local_offset + changed_length
    excerpt = value[start:end]
    marker = excerpt[local_offset:local_end]
    if not marker:
        marker = "∅"
    return (
        prefix
        + excerpt[:local_offset]
        + "⟦"
        + marker
        + "⟧"
        + excerpt[local_end:]
        + suffix
    )


def member_transition(member: dict[str, Any]) -> dict[str, Any] | None:
    before = member.get("beforeLogicalValue")
    after = member.get("selectedTerminalLogicalValue")
    if not isinstance(before, str) or not isinstance(after, str):
        return None
    edit = minimal_edit(before, after)
    return {
        "edit": edit,
        "before": marked_transition_state(
            before, edit["characterOffset"], len(edit["removedContent"])
        ),
        "after": marked_transition_state(
            after, edit["characterOffset"], len(edit["content"])
        ),
    }


def event_markdown(event: dict[str, Any]) -> list[str]:
    destination = event.get("destination")
    destination_text = json.dumps(destination, ensure_ascii=False, sort_keys=True)
    content = render_excerpt(event.get("content"), maximum=260)
    return [
        f"- `{event.get('kind')}` available `{event.get('availableAt')}` "
        f"at `{destination_text}`",
        "",
        "  ```text",
        "  " + (content or "[no projected content]").replace("\n", "\n  "),
        "  ```",
    ]


def markdown(
    candidates: list[dict[str, Any]],
    proposals_by_label: dict[str, dict[str, Any]] | None = None,
) -> str:
    lines = [
        "# Phase 1 episode review — shadow mode",
        "",
        "This artifact is diagnostic only. It does not change semantic events, causal examples, packing, or loss.",
        "",
        "Adjudication question: **At the initial conditioning point, what single completion could have captured the intended output and made the subsequent editing trajectory largely unnecessary, using no information read later?**",
        "",
    ]
    for candidate in candidates:
        diagnostic = candidate["singleCompletionDiagnostic"]
        causal = candidate["causalEvidence"]
        closure = candidate["closureEvidence"]
        gates = candidate["mechanicalGates"]
        continuity = candidate["continuityEvidence"]
        lines.extend([
            f"## {candidate['label']}",
            "",
            f"- Candidate: `{candidate['candidateID']}`",
            f"- Selection category: `{candidate.get('selectionCategory') or 'ad_hoc'}`",
            f"- Selection rationale: {candidate.get('selectionRationale') or '[ad hoc neighborhood]'}",
            f"- Examples: {candidate['oneBasedExampleRange']['first']}–{candidate['oneBasedExampleRange']['last']}",
            f"- Duration: {candidate['durationSeconds']:.3f}s",
            f"- Intervening READs: {causal['interveningReadCount']}",
            f"- Stable semantic destination: {candidate['surfaceEvidence']['semanticDestinationStable']}",
            f"- Exact logical editable identity: {candidate['surfaceEvidence']['exactTargetIdentityStable']}",
            f"- Continuous raw replay: {continuity['continuousReplayableState']}",
            f"- Hard mechanical gates passed: {gates['passed']} (`{json.dumps(gates['failures'])}`)",
            f"- Closure: `{closure['status']}` (`{closure['lastBoundaryReason']}`)",
            f"- Single-completion diagnostic: `{diagnostic['status']}`",
            "",
            "### Current independent loss targets",
            "",
        ])
        for member in candidate["members"]:
            ordinal = member["oneBasedExampleOrdinal"]
            title = (
                f"Example {ordinal}"
                if ordinal is not None
                else "History-only semantic WRITE"
            )
            transition = member_transition(member)
            lines.extend([
                f"#### {title} — `{member['boundaryReason']}`",
                "",
                "```text",
                member["currentLossTarget"] or "[no independent loss target]",
                "```",
                f"Reducer-selected terminal: `{member['selectedTerminalObservationSource']}` "
                f"(`{member['semanticReduction']['selectedObservationID']}`; "
                f"reason `{member['semanticReduction']['reason']}`)",
                f"Raw: `{member['rawPath']}:{member['rawLine']}`",
                "",
            ])
            if transition is not None:
                lines.extend([
                    "Raw logical BEFORE (changed span marked):",
                    "",
                    "```text",
                    transition["before"],
                    "```",
                    "Raw reducer-selected terminal state (changed span marked):",
                    "",
                    "```text",
                    transition["after"],
                    "```",
                    "",
                ])
            if member["currentTarget"] is not None:
                lines.extend([
                    "Structured target (authorship/paste provenance):",
                    "",
                    "```json",
                    json.dumps(
                        member["currentTarget"],
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    ),
                    "```",
                    "",
                ])
        lines.extend([
            "### Mechanically reconstructed candidate completion",
            "",
            "```text",
            render_excerpt(diagnostic.get("proposedFinalizedTarget")),
            "```",
            "",
            "### Events immediately after the candidate",
            "",
        ])
        following = candidate["closureContext"]["followingEvents"]
        if following:
            for event in following:
                lines.extend(event_markdown(event))
        else:
            lines.append("[No later semantic event in the corpus.]\n")
        lines.extend([
            "### Review decision",
            "",
            "- [ ] correct closed substantive episode",
            "- [ ] merge with adjacent WRITEs",
            "- [ ] split into multiple thoughts",
            "- [ ] mechanical editing only; history without loss",
            "- [ ] not representable as one completion",
            "- [ ] ambiguous closure; defer",
            "",
        ])
        proposal = (proposals_by_label or {}).get(candidate["label"])
        if proposal is not None:
            finalized = proposal.get("finalizedTarget")
            lines.extend([
                "### Assistant proposal — pending human adjudication",
                "",
                f"- Decision: `{proposal['decision']}`",
                f"- Target policy: `{proposal['targetPolicy']}`",
                f"- Closure assessment: `{proposal['closureAssessment']}`",
                f"- Representable as one completion: `{proposal['representableAsSingleCompletion']}`",
                f"- Notes: {proposal['notes']}",
                "",
            ])
            if finalized is not None:
                lines.extend([
                    "Proposed structured target:",
                    "",
                    "```json",
                    json.dumps(
                        finalized, ensure_ascii=False, indent=2, sort_keys=True
                    ),
                    "```",
                    "",
                ])
    return "\n".join(lines) + "\n"


def load_proposals(
    path: Path,
    *,
    selection_id: str,
    candidate_labels: set[str],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    manifest = load_json(path)
    if manifest.get("selectionID") != selection_id:
        raise ReviewError("proposal selectionID does not match review selection")
    rows = manifest.get("proposals")
    if not isinstance(rows, list) or not rows:
        raise ReviewError("proposal file must contain non-empty proposals")
    by_label: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("label"), str):
            raise ReviewError("proposal rows require a label")
        label = row["label"]
        if label in by_label:
            raise ReviewError(f"duplicate proposal label: {label}")
        by_label[label] = row
    if set(by_label) != candidate_labels:
        missing = sorted(candidate_labels - set(by_label))
        extra = sorted(set(by_label) - candidate_labels)
        raise ReviewError(f"proposal labels differ; missing={missing}, extra={extra}")
    return manifest, by_label


def authored_target(content: str) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "resolvedContent": content,
        "segments": [{"type": "authored_text", "content": content}],
    }


def resolve_proposal(
    candidate: dict[str, Any], proposal: dict[str, Any]
) -> dict[str, Any]:
    policy = proposal.get("targetPolicy")
    finalized: dict[str, Any] | None
    if policy == "mechanically_reconstructed_candidate":
        content = candidate["singleCompletionDiagnostic"].get(
            "proposedFinalizedTarget"
        )
        if not isinstance(content, str) or not content:
            raise ReviewError(
                f"mechanical proposal has no reconstructed target: {candidate['label']}"
            )
        finalized = authored_target(content)
    elif policy == "current_single_structured_target":
        targets = [
            member["currentTarget"]
            for member in candidate["members"]
            if member["currentTarget"] is not None
        ]
        if len(targets) != 1:
            raise ReviewError(
                f"single-target proposal resolved {len(targets)} targets: {candidate['label']}"
            )
        finalized = targets[0]
    elif policy == "custom_structured_target":
        marker = proposal.get("proposedMarkerTarget")
        if not isinstance(marker, str) or marker.count("<|paste|>") != 1:
            raise ReviewError(
                f"custom structured target requires one paste marker: {candidate['label']}"
            )
        paste_segments = [
            segment
            for member in candidate["members"]
            for segment in (member.get("currentTarget") or {}).get("segments", [])
            if segment.get("type") == "paste"
        ]
        if len(paste_segments) != 1:
            raise ReviewError(
                f"custom structured target resolved {len(paste_segments)} paste segments: {candidate['label']}"
            )
        before, after = marker.split("<|paste|>")
        resolved = candidate["finalObservation"].get("value")
        if not isinstance(resolved, str) or not resolved:
            raise ReviewError(f"custom target lacks final field value: {candidate['label']}")
        finalized = {
            "schemaVersion": 1,
            "resolvedContent": resolved,
            "segments": [
                {"type": "authored_text", "content": before},
                paste_segments[0],
                {"type": "authored_text", "content": after},
            ],
        }
        if marker_target(finalized) != marker:
            raise ReviewError(f"custom marker target did not round-trip: {candidate['label']}")
    elif policy == "custom_authored_target_from_selected_terminal_without_ui_scaffold":
        marker = proposal.get("proposedMarkerTarget")
        if not isinstance(marker, str) or not marker:
            raise ReviewError(f"custom authored target is empty: {candidate['label']}")
        finalized = authored_target(marker)
    elif policy in {
        "none_for_combined_candidate",
        "none_until_initial_composition_member_is_included",
    }:
        finalized = None
    else:
        raise ReviewError(f"unknown target policy for {candidate['label']}: {policy}")
    return {
        "schemaVersion": 2,
        "candidateID": candidate["candidateID"],
        "label": candidate["label"],
        "memberWriteEventIDs": candidate["memberWriteEventIDs"],
        "status": "assistant_proposal_pending_human_adjudication",
        "decision": proposal["decision"],
        "targetPolicy": policy,
        "finalizedTarget": finalized,
        "closureAssessment": proposal["closureAssessment"],
        "representableAsSingleCompletion": proposal[
            "representableAsSingleCompletion"
        ],
        "notes": proposal["notes"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    selection_group = parser.add_mutually_exclusive_group(required=True)
    selection_group.add_argument(
        "--neighborhood",
        action="append",
        type=parse_neighborhood,
        help="repeatable LABEL=FIRST:LAST, using one-based inclusive example ordinals",
    )
    selection_group.add_argument(
        "--selection-file",
        type=Path,
        help="versioned JSON selection containing labeled review neighborhoods",
    )
    parser.add_argument(
        "--proposals-file",
        type=Path,
        help="assistant proposal sidecar bound to --selection-file; remains non-authoritative",
    )
    arguments = parser.parse_args()
    corpus_path = arguments.corpus.expanduser().resolve()
    output = arguments.output.expanduser().resolve()
    if output.exists():
        raise ReviewError(f"output already exists: {output}")
    project = Path(__file__).resolve().parent.parent
    manifest_path = corpus_path / "corpus.json"
    manifest = load_json(manifest_path)
    selection_manifest = None
    selection_path = None
    if arguments.selection_file is not None:
        selection_path = arguments.selection_file.expanduser().resolve()
        neighborhoods, selection_manifest = load_selection(
            selection_path, manifest["corpusID"]
        )
    else:
        neighborhoods = arguments.neighborhood or []
    if arguments.proposals_file is not None and selection_manifest is None:
        raise ReviewError("--proposals-file requires --selection-file")
    for name in ("examples.jsonl", "events.jsonl"):
        expected = manifest.get("artifactDigestsSHA256", {}).get(name)
        if not expected or sha256(corpus_path / name) != expected:
            raise ReviewError(f"corpus artifact changed: {name}")
    examples = load_jsonl(corpus_path / "examples.jsonl")
    events = load_jsonl(corpus_path / "events.jsonl")
    if [example.get("chronologicalOrdinal") for example in examples] != list(
        range(len(examples))
    ):
        raise ReviewError("examples are not in frozen chronological order")
    for neighborhood in neighborhoods:
        if neighborhood.last > len(examples):
            raise ReviewError(f"neighborhood exceeds corpus: {neighborhood.label}")
    event_by_id = {event["sourceEventID"]: event for event in events}
    selected_events = [
        event
        for neighborhood in neighborhoods
        for event in neighborhood_write_events(neighborhood, examples, events)
    ]
    needed_ids = {
        record_id
        for event in selected_events
        for record_id in event.get("sourceRecordIDs", [])
    }
    session_ids = {event["sessionID"] for event in selected_events}
    raw_sessions = discover_raw_sessions(project, session_ids)
    raw_records, raw_sources = load_raw_records(raw_sessions, needed_ids)
    semantic_events, semantic_sources = discover_semantic_sessions(
        project, manifest, session_ids
    )
    candidates = [
        build_candidate(
            manifest["corpusID"],
            neighborhood,
            examples,
            events,
            raw_records,
            semantic_events,
        )
        for neighborhood in neighborhoods
    ]
    proposal_manifest = None
    proposal_path = None
    proposals_by_label: dict[str, dict[str, Any]] = {}
    proposed_annotations: list[dict[str, Any]] = []
    if arguments.proposals_file is not None:
        proposal_path = arguments.proposals_file.expanduser().resolve()
        proposal_manifest, proposal_specs = load_proposals(
            proposal_path,
            selection_id=selection_manifest["selectionID"],
            candidate_labels={candidate["label"] for candidate in candidates},
        )
        proposed_annotations = [
            resolve_proposal(candidate, proposal_specs[candidate["label"]])
            for candidate in candidates
        ]
        proposals_by_label = {
            value["label"]: value for value in proposed_annotations
        }
    annotations = [
        {
            "schemaVersion": 2,
            "candidateID": candidate["candidateID"],
            "memberWriteEventIDs": candidate["memberWriteEventIDs"],
            "decision": "unreviewed",
            "finalMemberWriteEventIDs": None,
            "finalizedTarget": None,
            "closureReason": None,
            "representableAsSingleCompletion": None,
            "notes": "",
        }
        for candidate in candidates
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        write_jsonl(temporary / "episode-candidates.jsonl", candidates)
        write_jsonl(temporary / "annotations.jsonl", annotations)
        if proposed_annotations:
            write_jsonl(
                temporary / "proposed-annotations.jsonl", proposed_annotations
            )
        (temporary / "review.md").write_text(
            markdown(candidates, proposals_by_label), encoding="utf-8"
        )
        review_manifest = {
            "schemaVersion": 2,
            "builderVersion": BUILDER_VERSION,
            "status": "shadow_review_only_not_training_authority",
            "source": {
                "corpusID": manifest["corpusID"],
                "corpusPath": str(corpus_path.relative_to(project)),
                "corpusSHA256": sha256(manifest_path),
                "examplesSHA256": sha256(corpus_path / "examples.jsonl"),
                "eventsSHA256": sha256(corpus_path / "events.jsonl"),
                "rawSessions": raw_sources,
                "semanticSessions": semantic_sources,
                "selection": (
                    {
                        "selectionID": selection_manifest.get("selectionID"),
                        "path": str(selection_path.relative_to(project)),
                        "sha256": sha256(selection_path),
                    }
                    if selection_manifest is not None and selection_path is not None
                    else None
                ),
                "assistantProposals": (
                    {
                        "status": proposal_manifest.get("status"),
                        "path": str(proposal_path.relative_to(project)),
                        "sha256": sha256(proposal_path),
                    }
                    if proposal_manifest is not None and proposal_path is not None
                    else None
                ),
            },
            "neighborhoods": [
                {
                    "label": value.label,
                    "firstOneBasedExampleOrdinal": value.first,
                    "lastOneBasedExampleOrdinal": value.last,
                    "category": value.category,
                    "rationale": value.rationale,
                }
                for value in neighborhoods
            ],
            "rules": {
                "newReadIsHardMergeBoundary": True,
                "mechanicalRepresentabilityRequiresNumericOrUniqueSemanticAlignment": True,
                "semanticAlignmentPreservesNumericDisagreement": True,
                "terminalObservationMustMatchSemanticReducerProvenance": True,
                "continuousEditableReplayIsHardGate": True,
                "sameLogicalEditableIsHardGate": True,
                "interveningReadIsHardGate": True,
                "overlappingOutsideWriteIsHardGate": True,
                "mechanicalRepresentabilityIsNotEpisodeAuthority": True,
                "microWritesRemainUnchanged": True,
                "trainingArtifactsRemainUnchanged": True,
                "humanAnnotationsAreDevelopmentGoldNotProductionRequirement": True,
            },
            "counts": {
                "candidates": len(candidates),
                "memberWrites": sum(len(value["members"]) for value in candidates),
                "mechanicallyRepresentable": sum(
                    value["singleCompletionDiagnostic"]["status"].startswith(
                        "mechanically_representable_"
                    )
                    for value in candidates
                ),
                "objectiveClosures": sum(
                    value["closureEvidence"]["objectiveSubmissionBoundary"]
                    for value in candidates
                ),
                "mechanicallyGatedOut": sum(
                    value["singleCompletionDiagnostic"]["status"]
                    == "mechanical_alignment_gated_out"
                    for value in candidates
                ),
                "assistantProposalsPendingHumanAdjudication": len(
                    proposed_annotations
                ),
            },
            "artifactDigestsSHA256": {
                "episode-candidates.jsonl": sha256(
                    temporary / "episode-candidates.jsonl"
                ),
                "annotations.jsonl": sha256(temporary / "annotations.jsonl"),
                "review.md": sha256(temporary / "review.md"),
            },
        }
        if proposed_annotations:
            review_manifest["artifactDigestsSHA256"][
                "proposed-annotations.jsonl"
            ] = sha256(temporary / "proposed-annotations.jsonl")
        (temporary / "episode-review.json").write_bytes(
            canonical_bytes(review_manifest)
        )
        os.replace(temporary, output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    print(f"Wrote {len(candidates)} shadow episode neighborhoods to {output}")
    print("No semantic events, examples, packing, loss masks, or model results changed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError, ReviewError) as error:
        raise SystemExit(f"build-phase1-episode-review: {error}") from error

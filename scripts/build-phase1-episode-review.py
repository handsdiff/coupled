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


BUILDER_VERSION = "phase1-episode-v0-shadow"
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
    pieces: list[str] = []
    for segment in example.get("target", {}).get("segments", []):
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


def terminal_observation(record: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    after = record.get("after")
    if isinstance(after, dict) and isinstance(after.get("value"), str):
        return "terminal_after", after
    candidates: list[tuple[str, dict[str, Any]]] = []
    for field, label in (
        ("returnCheckpoints", "return_checkpoint"),
        ("pasteCheckpoints", "paste_checkpoint"),
        ("mutationCheckpoints", "mutation_checkpoint"),
    ):
        for checkpoint in record.get(field, []):
            if not isinstance(checkpoint, dict):
                continue
            observation = checkpoint.get("observation")
            if isinstance(observation, dict) and isinstance(
                observation.get("value"), str
            ):
                candidates.append((label, observation))
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: item[1].get("observedAt") or "",
    )


def logical_initial_value(
    example: dict[str, Any], observation: dict[str, Any]
) -> tuple[str, str]:
    cursor = example.get("conditioningState", {}).get("cursorContext", {})
    if cursor.get("fieldState") == "unpopulated_prompt":
        return "", "conditioning_field_state_unpopulated_prompt"
    value = observation.get("value")
    if not isinstance(value, str):
        raise ReviewError("initial raw observation has no string value")
    if observation.get("valueRepresentedPlaceholder") is True:
        return "", "accessibility_value_represented_placeholder"
    return value, "raw_before_value"


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


def member_projection(
    ordinal: int | None,
    example: dict[str, Any] | None,
    event: dict[str, Any],
    raw_entry: dict[str, Any],
) -> dict[str, Any]:
    record = raw_entry["record"]
    terminal = terminal_observation(record)
    audit = json.loads(event.get("auditSerialized", "{}"))
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
        "selectedTerminalObservationSource": terminal[0] if terminal else None,
        "selectedTerminalObservation": (
            observation_projection(terminal[1]) if terminal else None
        ),
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


def build_candidate(
    corpus_id: str,
    neighborhood: Neighborhood,
    examples: list[dict[str, Any]],
    events: list[dict[str, Any]],
    raw_records: dict[str, dict[str, Any]],
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
        members.append(member_projection(ordinal, example, event, raw_entry))
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
    terminal = terminal_observation(last_raw)
    terminal_value = terminal[1].get("value") if terminal else None
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
        completion_status = "mechanically_representable_numeric_selection"
    elif semantic_anchor_aligned:
        completion_status = "mechanically_representable_semantic_anchor"
    else:
        completion_status = "not_mechanically_proven"
    single_completion = numeric_selection_aligned or semantic_anchor_aligned

    began = timestamp(first_member["beganAt"])
    closed = timestamp(last_member["availableAt"])
    member_set = set(member_event_ids)
    intervening = intervening_events(events, member_set, began, closed)
    intervening_reads = [event for event in intervening if event["kind"] == "read"]
    intervening_writes = [event for event in intervening if event["kind"] == "write"]
    identities = [member["targetIdentity"] for member in members]
    exact_identity_stable = all(identity == identities[0] for identity in identities)
    semantic_destination_stable = all(
        stable_destination(identity) == stable_destination(identities[0])
        for identity in identities
    )
    candidate_material = {
        "corpusID": corpus_id,
        "label": neighborhood.label,
        "memberWriteEventIDs": member_event_ids,
    }
    candidate_id = "episode_candidate_" + hashlib.sha256(
        canonical_bytes(candidate_material)
    ).hexdigest()
    return {
        "schemaVersion": 1,
        "builderVersion": BUILDER_VERSION,
        "authority": "shadow_review_only",
        "candidateID": candidate_id,
        "label": neighborhood.label,
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
        "finalObservationSource": terminal[0] if terminal else None,
        "finalObservation": observation_projection(terminal[1]) if terminal else None,
        "members": members,
        "causalEvidence": {
            "interveningReadCount": len(intervening_reads),
            "interveningWriteCountOutsideCandidate": len(intervening_writes),
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
        },
        "surfaceEvidence": {
            "exactTargetIdentityStable": exact_identity_stable,
            "semanticDestinationStable": semantic_destination_stable,
            "targetIdentities": identities,
        },
        "singleCompletionDiagnostic": {
            "status": completion_status,
            "numericSelectionAligned": numeric_selection_aligned,
            "semanticAnchorAligned": semantic_anchor_aligned,
            "semanticAnchor": semantic_anchor,
            "initialSelectionText": selected_text,
            "initialSelectionOffset": initial_location,
            "initialSelectionLength": initial_length,
            "initialSelectionCoordinateUnit": coordinate_unit,
            "netFieldEdit": net_edit,
            "proposedFinalizedTarget": net_edit["content"] if single_completion else None,
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
            "objectiveSubmissionBoundary": last_member["boundaryReason"]
            in {"return_pressed", "submission_boundary"},
            "status": (
                "objective_submission_observed"
                if last_member["boundaryReason"]
                in {"return_pressed", "submission_boundary"}
                else "ambiguous_requires_review"
            ),
        },
    }


def render_excerpt(value: str | None, maximum: int = 700) -> str:
    if value is None:
        return "[missing]"
    if len(value) <= maximum:
        return value
    half = maximum // 2
    return value[:half] + "\n… [excerpt clipped] …\n" + value[-half:]


def markdown(candidates: list[dict[str, Any]]) -> str:
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
        lines.extend([
            f"## {candidate['label']}",
            "",
            f"- Candidate: `{candidate['candidateID']}`",
            f"- Examples: {candidate['oneBasedExampleRange']['first']}–{candidate['oneBasedExampleRange']['last']}",
            f"- Duration: {candidate['durationSeconds']:.3f}s",
            f"- Intervening READs: {causal['interveningReadCount']}",
            f"- Stable semantic destination: {candidate['surfaceEvidence']['semanticDestinationStable']}",
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
            lines.extend([
                f"#### {title} — `{member['boundaryReason']}`",
                "",
                "```text",
                member["currentLossTarget"] or "[no independent loss target]",
                "```",
                f"Raw: `{member['rawPath']}:{member['rawLine']}`",
                "",
            ])
        lines.extend([
            "### Mechanically reconstructed candidate completion",
            "",
            "```text",
            render_excerpt(diagnostic.get("proposedFinalizedTarget")),
            "```",
            "",
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
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--neighborhood",
        action="append",
        required=True,
        type=parse_neighborhood,
        help="repeatable LABEL=FIRST:LAST, using one-based inclusive example ordinals",
    )
    arguments = parser.parse_args()
    corpus_path = arguments.corpus.expanduser().resolve()
    output = arguments.output.expanduser().resolve()
    if output.exists():
        raise ReviewError(f"output already exists: {output}")
    project = Path(__file__).resolve().parent.parent
    manifest_path = corpus_path / "corpus.json"
    manifest = load_json(manifest_path)
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
    for neighborhood in arguments.neighborhood:
        if neighborhood.last > len(examples):
            raise ReviewError(f"neighborhood exceeds corpus: {neighborhood.label}")
    event_by_id = {event["sourceEventID"]: event for event in events}
    selected_events = [
        event
        for neighborhood in arguments.neighborhood
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
    candidates = [
        build_candidate(
            manifest["corpusID"],
            neighborhood,
            examples,
            events,
            raw_records,
        )
        for neighborhood in arguments.neighborhood
    ]
    annotations = [
        {
            "schemaVersion": 1,
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
        (temporary / "review.md").write_text(markdown(candidates), encoding="utf-8")
        review_manifest = {
            "schemaVersion": 1,
            "builderVersion": BUILDER_VERSION,
            "status": "shadow_review_only_not_training_authority",
            "source": {
                "corpusID": manifest["corpusID"],
                "corpusPath": str(corpus_path.relative_to(project)),
                "corpusSHA256": sha256(manifest_path),
                "examplesSHA256": sha256(corpus_path / "examples.jsonl"),
                "eventsSHA256": sha256(corpus_path / "events.jsonl"),
                "rawSessions": raw_sources,
            },
            "neighborhoods": [
                {
                    "label": value.label,
                    "firstOneBasedExampleOrdinal": value.first,
                    "lastOneBasedExampleOrdinal": value.last,
                }
                for value in arguments.neighborhood
            ],
            "rules": {
                "newReadIsHardMergeBoundary": True,
                "mechanicalRepresentabilityRequiresNumericOrUniqueSemanticAlignment": True,
                "semanticAlignmentPreservesNumericDisagreement": True,
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
            },
            "artifactDigestsSHA256": {
                "episode-candidates.jsonl": sha256(
                    temporary / "episode-candidates.jsonl"
                ),
                "annotations.jsonl": sha256(temporary / "annotations.jsonl"),
                "review.md": sha256(temporary / "review.md"),
            },
        }
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

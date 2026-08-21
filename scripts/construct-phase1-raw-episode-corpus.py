#!/usr/bin/env python3
"""Construct raw-authoritative Phase 1 composition episodes.

This compiler deliberately does not accept adjudications, regression fixtures,
event IDs, or target overrides. Its input is a complete raw-evidence primitive
projection plus the frozen causal micro-event corpus. Regression fixtures are
allowed only in the separate checker.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


EPISODE_VERSION = "phase1-raw-episode-v6"
CONVERSION_VERSION = "phase1-raw-episode-causal-v6"
MIN_PERSISTENT_CHARACTERS = 40
MIN_PERSISTENT_WORDS = 6
MIN_SUBMITTED_CHARACTERS = 4
MAX_INTERNAL_REVISION_GAP_SECONDS = 3.0
PROMPT_SCAFFOLDS = {
    "ask gemini", "do anything", "start writing...", "write a message…",
    "write a message...",
}


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object: {path}")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def timestamp(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def words(value: str) -> list[str]:
    return re.findall(r"[\w’']+", value, flags=re.UNICODE)


def stable_field_description(value: Any) -> str:
    """Remove volatile progress glyphs without weakening semantic identity."""
    text = str(value or "")
    text = re.sub(r"(?<=, )[\u2800-\u28ff]+(?= )", "", text)
    return re.sub(r"\s+", " ", text).strip()


def stable_destination(member: dict[str, Any]) -> tuple[Any, ...]:
    identity = member.get("targetIdentity") or {}
    return (
        identity.get("bundleIdentifier"),
        identity.get("windowTitle"),
        identity.get("role"),
        stable_field_description(identity.get("fieldDescription")),
        identity.get("fieldLabel"),
    )


def normalized_text(value: str) -> str:
    return "".join(
        character.lower() for character in value
        if not character.isspace() and character not in {"\u200b", "\ufeff"}
    )


def normalize_authored_content(value: str) -> str:
    value = re.sub(
        "\\n\u200b(?:\\t)?\\n(?:\u200b\\n)?-\\n\u200b ?\\n?", "\n\n", value
    )
    return value.strip("\n\r\u200b")


def utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def minimal_edit(before: str, after: str) -> dict[str, Any]:
    """Return the prefix/suffix-preserving net field transition."""
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
    return {
        "operation": "replace" if removed and inserted else (
            "delete" if removed else "insert"
        ),
        "characterOffset": prefix,
        "utf16Offset": utf16_length(before[:prefix]),
        "removedContent": removed,
        "content": inserted,
    }


def apply_edit(source: str, edit: dict[str, Any]) -> str | None:
    offset = edit.get("characterOffset")
    removed = edit.get("removedContent")
    inserted = edit.get("content")
    if not isinstance(offset, int) or offset < 0:
        return None
    if not isinstance(removed, str) or not isinstance(inserted, str):
        return None
    if source[offset:offset + len(removed)] != removed:
        return None
    return source[:offset] + inserted + source[offset + len(removed):]


def verified_single_member_alignment(
    episode: OpenEpisode,
    before: str,
    after: str,
    canonical: dict[str, Any],
) -> dict[str, Any] | None:
    """Preserve only a compiler-verified equivalent micro-WRITE alignment.

    The causal compiler admits a non-canonical alignment only when the reducer
    grounded it in ordered raw checkpoints and it is an equal-size edit which
    reconstructs the identical terminal field state. The episode constructor
    must not erase that evidence by recomputing the prefix-greedy diff. This
    narrow bridge applies only to a singleton; multi-WRITE compositions remain
    independently reconstructed from their complete initial/terminal states.
    """
    if len(episode.members) != 1:
        return None
    member = episode.members[0]
    target = member.get("currentTarget") or {}
    inserted = target.get("resolvedContent")
    candidate = {
        "operation": member.get("operation"),
        "characterOffset": member.get("characterOffset"),
        "removedContent": member.get("removedContent"),
        "content": inserted,
    }
    if (
        candidate["operation"] != canonical["operation"]
        or not isinstance(inserted, str)
        or not isinstance(candidate["removedContent"], str)
        or len(inserted) != len(canonical["content"])
        or len(candidate["removedContent"]) != len(canonical["removedContent"])
        or apply_edit(before, candidate) != after
    ):
        return None
    candidate["utf16Offset"] = utf16_length(
        before[:candidate["characterOffset"]]
    )
    return candidate


def serialized_content(event: dict[str, Any]) -> str:
    try:
        value = json.loads(event.get("serialized", "{}"))
    except json.JSONDecodeError:
        return ""
    content = value.get("content")
    return content if isinstance(content, str) else ""


def event_boundary(event: dict[str, Any]) -> str | None:
    try:
        return json.loads(event.get("auditSerialized", "{}")).get("boundaryReason")
    except json.JSONDecodeError:
        return None


def is_prompt_surface(member: dict[str, Any]) -> bool:
    identity = member.get("targetIdentity") or {}
    bundle = identity.get("bundleIdentifier", "")
    description = str(identity.get("fieldDescription") or "").lower()
    label = str(identity.get("fieldLabel") or "").lower()
    role = identity.get("role")
    if bundle == "com.openai.codex":
        return True
    if bundle == "com.microsoft.VSCode" and role == "AXTextField":
        return True
    if bundle == "com.google.Chrome":
        if role == "AXTextArea":
            return True
        return any(marker in description + " " + label for marker in (
            "prompt", "message", "post text", "reply", "search", "address",
        ))
    return False


def is_mechanical_destination(member: dict[str, Any], content: str) -> bool:
    identity = member.get("targetIdentity") or {}
    description = str(identity.get("fieldDescription") or "").lower()
    label = str(identity.get("fieldLabel") or "").lower()
    role = identity.get("role")
    address = role != "AXTextArea" and any(
        marker in description + " " + label
        for marker in ("address", "location", "search combo box")
    )
    url_like = bool(re.fullmatch(r"(?:https?://)?[^\s]+\.[^\s]+/?", content.strip()))
    return address or url_like


def field_state_continuous(left: dict[str, Any], right: dict[str, Any]) -> bool:
    terminal = left.get("selectedTerminalLogicalValue")
    before = right.get("beforeLogicalValue")
    return isinstance(terminal, str) and isinstance(before, str) and terminal == before


def prompt_epoch_reset_compatible(
    left: dict[str, Any], right: dict[str, Any]
) -> bool:
    """Recognize a same-prompt AX epoch reset without treating submission as one."""
    terminal = left.get("selectedTerminalLogicalValue")
    before = right.get("beforeLogicalValue")
    left_hints = set(left.get("inputHints", []))
    return (
        is_prompt_surface(left)
        and is_prompt_surface(right)
        and stable_destination(left) == stable_destination(right)
        and left.get("boundaryReason") == "write_delay_elapsed"
        and "return" not in left_hints
        and isinstance(terminal, str)
        and bool(terminal)
        and isinstance(before, str)
        and not before
    )


def episode_state_continuous(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return field_state_continuous(left, right) or prompt_epoch_reset_compatible(
        left, right
    )


def affected_region_compatible(
    episode_before: str,
    current_after: str,
    next_after: str,
    prompt_surface: bool,
) -> tuple[bool, dict[str, Any]]:
    current = minimal_edit(episode_before, current_after)
    local = minimal_edit(current_after, next_after)
    current_start = current["characterOffset"]
    current_end = current_start + len(current["content"])
    local_start = local["characterOffset"]
    local_end = local_start + max(len(local["removedContent"]), len(local["content"]))
    touches = local_start <= current_end + 1 and local_end >= max(0, current_start - 1)
    return prompt_surface or touches, {
        "episodeRegionBeforeNext": [current_start, current_end],
        "nextLocalRegion": [local_start, local_end],
        "promptSurfaceKeepsCompositionOpen": prompt_surface,
        "regionsTouchOrOverlap": touches,
    }


def same_element_internal_revision_continuation(
    episode: OpenEpisode,
    following: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Prove a short caret-relocation revision inside the active composition.

    An Accessibility snapshot can occasionally materialize the first character
    after a pointer boundary before the next raw WRITE's BEFORE observation. An
    exact left-AFTER == right-BEFORE requirement then splits one composition and
    makes the suffix look like an independent target. This gate does not guess
    from language. It requires the same retained AX element, a short pointer
    boundary, no paste/cut provenance, and a bridge edit wholly inside the
    already-authored net region. The episode target is still derived from its
    original BEFORE and final AFTER, so temporary edits receive no loss.
    """
    left = episode.last
    right = following["members"][0]
    before = episode.first.get("beforeLogicalValue")
    left_after = left.get("selectedTerminalLogicalValue")
    right_before = right.get("beforeLogicalValue")
    right_after = right.get("selectedTerminalLogicalValue")
    evidence: dict[str, Any] = {
        "policy": "same_ax_element_short_internal_revision_v1",
        "maximumGapSeconds": MAX_INTERNAL_REVISION_GAP_SECONDS,
    }
    if not all(
        isinstance(value, str)
        for value in (before, left_after, right_before, right_after)
    ):
        evidence["reason"] = "missing_complete_field_endpoint"
        return False, evidence

    gap = (timestamp(right["beganAt"]) - timestamp(left["availableAt"])).total_seconds()
    evidence["gapSeconds"] = gap
    left_identity = left.get("targetIdentity") or {}
    right_identity = right.get("targetIdentity") or {}
    left_hash = left_identity.get("elementHash")
    right_hash = right_identity.get("elementHash")
    same_element = left_hash is not None and left_hash == right_hash
    evidence["sameAXElementHash"] = same_element

    hints = {
        hint
        for member in [*episode.members, right]
        for hint in member.get("inputHints", [])
    }
    evidence["inputHints"] = sorted(hints)
    current = minimal_edit(before, left_after)
    bridge = minimal_edit(left_after, right_before)
    final = minimal_edit(before, right_after)
    current_start = current["characterOffset"]
    current_end = current_start + max(
        len(current["removedContent"]), len(current["content"])
    )
    bridge_start = bridge["characterOffset"]
    bridge_end = bridge_start + max(
        len(bridge["removedContent"]), len(bridge["content"])
    )
    bridge_inside_authored_region = (
        bridge_start <= current_end + 1
        and bridge_end >= max(0, current_start - 1)
    )
    final_region_continues_composition, region_evidence = affected_region_compatible(
        before, left_after, right_after, is_prompt_surface(left)
    )
    current_authored = normalize_authored_content(current["content"])
    current_substantive = (
        len(current_authored.strip()) >= MIN_PERSISTENT_CHARACTERS
        and len(words(current_authored)) >= MIN_PERSISTENT_WORDS
    )
    evidence.update({
        "currentNetEdit": current,
        "bridgeEdit": bridge,
        "finalNetEdit": final,
        "bridgeInsideAuthoredRegion": bridge_inside_authored_region,
        "finalRegion": region_evidence,
        "currentCompositionSubstantive": current_substantive,
    })
    proven = (
        stable_destination(left) == stable_destination(right)
        and same_element
        and left.get("boundaryReason") == "pointer_selection_boundary"
        and 0 <= gap <= MAX_INTERNAL_REVISION_GAP_SECONDS
        and "paste" not in hints
        and "cut" not in hints
        and current_substantive
        and bridge_inside_authored_region
        and final_region_continues_composition
        and left_after != right_before
    )
    evidence["reason"] = (
        "proven_same_element_internal_revision" if proven
        else "internal_revision_gate_failed"
    )
    return proven, evidence


def read_assessments(
    events: list[dict[str, Any]],
    session_id: str,
    onset: dt.datetime,
    lower: dt.datetime,
    upper: dt.datetime,
    current_completion: str,
    application: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    prior = {
        event.get("serialized") for event in events
        if event.get("sessionID") == session_id and event.get("kind") == "read"
        and timestamp(event["availableAt"]) < onset
    }
    between = [
        event for event in events
        if event.get("sessionID") == session_id and event.get("kind") == "read"
        and lower < timestamp(event["availableAt"]) < upper
    ]
    assessments: list[dict[str, Any]] = []
    novel: list[dict[str, Any]] = []
    normalized_completion = normalized_text(current_completion)
    for event in between:
        status = "novel_causally_available_read"
        if event.get("serialized") in prior:
            status = "exact_repeat_available_at_episode_onset"
        else:
            content = normalized_text(serialized_content(event))
            source = json.loads(event.get("serialized", "{}")).get("source", {})
            same_app = source.get("application") == application
            if (
                same_app and len(normalized_completion) >= 12
                and normalized_completion in content
            ):
                status = "self_derived_active_composition_read"
        assessment = {
            "eventID": event["sourceEventID"],
            "availableAt": event["availableAt"],
            "status": status,
        }
        assessments.append(assessment)
        if status == "novel_causally_available_read":
            novel.append(event)
    return assessments, novel


@dataclass
class OpenEpisode:
    primitives: list[dict[str, Any]]
    onset_partition_reason: str
    boundary_evidence: list[dict[str, Any]] = field(default_factory=list)
    read_assessments: list[dict[str, Any]] = field(default_factory=list)
    close_reason: str | None = None

    @property
    def members(self) -> list[dict[str, Any]]:
        return [primitive["members"][0] for primitive in self.primitives]

    @property
    def first(self) -> dict[str, Any]:
        return self.members[0]

    @property
    def last(self) -> dict[str, Any]:
        return self.members[-1]


def strict_fast_start(
    episode: OpenEpisode, raw_records: dict[str, dict[str, Any]]
) -> tuple[bool, dict[str, Any] | None]:
    first = episode.first
    ids = first.get("sourceRecordIDs", [])
    if len(ids) < 2 or first.get("beforeLogicalValue") is not None:
        return False, None
    initial = raw_records.get(ids[0], {})
    materialized = raw_records.get(ids[1], {})
    before = (materialized.get("before") or {}).get("value")
    first_hints = set(initial.get("inputHints", []))
    initial_identity = initial.get("targetIdentity")
    same_surface = initial.get("bundleIdentifier") == materialized.get("bundleIdentifier")
    if isinstance(initial_identity, dict):
        same_surface = same_surface and (
            stable_destination({"targetIdentity": initial_identity})
            == stable_destination({"targetIdentity": materialized.get("targetIdentity") or {}})
        )
    else:
        # The first callback is precisely the failed observation being repaired.
        # A target_changed boundary followed within one second by the known
        # prompt surface is the only accepted missing-identity case.
        same_surface = same_surface and initial.get("boundaryReason") == "target_changed"
    valid = (
        initial.get("inputEventCount") == 1
        and first_hints == {"typed"}
        and isinstance(before, str) and len(before) == 1
        and same_surface
        and not initial.get("pasteCheckpoints")
        and timestamp(materialized["beganAt"]) - timestamp(initial["beganAt"])
            < dt.timedelta(seconds=1)
    )
    if not valid:
        return False, None
    conditioning = json.loads(json.dumps(first.get("conditioningState") or {}))
    cursor = conditioning.setdefault("cursorContext", {})
    cursor.update({
        "fieldState": "unpopulated_prompt",
        "leftContext": "",
        "selectedText": "",
        "rightContext": "",
        "surfacePrompt": cursor.get("surfacePrompt") or "Do anything",
    })
    conditioning["captureSemantics"] = "strict_single_character_fast_start_recovery"
    conditioning["sourceObservationID"] = None
    conditioning["recoveryEvidence"] = {
        "failedFirstRecordID": ids[0],
        "materializedRecordID": ids[1],
        "materializedFirstCharacterSHA256": hashlib.sha256(before.encode()).hexdigest(),
    }
    return True, conditioning


def raw_paste_evidence(
    member: dict[str, Any], raw_records: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Reconstruct each keyboard paste from its synchronous raw checkpoint pair."""
    results: list[dict[str, Any]] = []
    for record_id in member.get("sourceRecordIDs", []):
        record = raw_records.get(record_id) or {}
        record_hints = set(record.get("inputHints", []))
        for checkpoint in record.get("pasteCheckpoints", []):
            pre = checkpoint.get("prePasteObservation") or {}
            post = checkpoint.get("observation") or {}
            clipboard = checkpoint.get("clipboardText")
            evidence = {
                "status": "unresolved",
                "reason": "raw_paste_checkpoint_incomplete",
                "checkpointID": checkpoint.get("checkpointID"),
                "clipboardSnapshotID": checkpoint.get("clipboardSnapshotID"),
                "clipboardChangeCount": checkpoint.get("clipboardChangeCount"),
                "sourceRecordID": record_id,
            }
            before = pre.get("value")
            after = post.get("value")
            complete = (
                isinstance(before, str)
                and isinstance(after, str)
                and isinstance(clipboard, str)
                and not pre.get("valueWasTruncated", False)
                and not post.get("valueWasTruncated", False)
                and not checkpoint.get("clipboardTextWasTruncated", False)
                and not checkpoint.get("prePasteAXErrors", [])
                and not checkpoint.get("axErrors", [])
            )
            if not complete:
                results.append(evidence)
                continue
            local = minimal_edit(before, after)
            insertion = local["content"]
            position = insertion.find(clipboard)
            exact = position >= 0 and insertion.find(clipboard, position + 1) < 0
            conditioned_clipboard = (record.get("conditioningState") or {}).get(
                "clipboard"
            ) or {}
            same_conditioned_clipboard = (
                checkpoint.get("clipboardSnapshotID")
                == conditioned_clipboard.get("snapshotID")
                and checkpoint.get("clipboardChangeCount")
                == conditioned_clipboard.get("changeCount")
            )
            pure_paste_input = (
                "paste" in record_hints
                and not record_hints.intersection({"typed", "delete", "cut"})
            )
            input_events = record.get("inputEvents") or []
            paste_event_index = next((
                index for index, event in enumerate(input_events)
                if event.get("eventTimestampNanoseconds")
                    == checkpoint.get("eventTimestampNanoseconds")
            ), None)
            immediate_undo = (
                paste_event_index is not None
                and paste_event_index + 1 < len(input_events)
                and input_events[paste_event_index + 1].get("hint") == "undo_redo"
            )
            undo_event = (
                input_events[paste_event_index + 1] if immediate_undo else None
            )
            undo_checkpoint = next((
                row for row in record.get("mutationCheckpoints", [])
                if undo_event is not None
                and row.get("eventTimestampNanoseconds")
                    == undo_event.get("eventTimestampNanoseconds")
            ), None)
            undo_observation = (undo_checkpoint or {}).get("observation") or {}
            undo_restores_pre_paste = (
                isinstance(undo_observation.get("value"), str)
                and undo_observation.get("value") == before
                and undo_observation.get("selectedRangeLocation")
                    == pre.get("selectedRangeLocation")
                and undo_observation.get("selectedRangeLength")
                    == pre.get("selectedRangeLength")
                and not undo_observation.get("valueWasTruncated", False)
                and not (undo_checkpoint or {}).get("axErrors", [])
            )
            terminal = member.get("selectedTerminalLogicalValue")
            member_before = member.get("beforeLogicalValue")
            terminal_edit = (
                minimal_edit(member_before, terminal)
                if isinstance(member_before, str) and isinstance(terminal, str)
                else None
            )
            payload_absent_from_terminal_edit = (
                isinstance(clipboard, str)
                and isinstance(terminal_edit, dict)
                and clipboard not in str(terminal_edit.get("content") or "")
            )
            subsequent_typed_events = (
                [
                    event for event in input_events[paste_event_index + 2:]
                    if event.get("hint") == "typed"
                ]
                if paste_event_index is not None else []
            )
            final_checkpoint = next((
                row for row in reversed(record.get("mutationCheckpoints", []))
                if isinstance((row.get("observation") or {}).get("value"), str)
            ), None)
            typed_trajectory_reaches_terminal = (
                bool(subsequent_typed_events)
                and isinstance(terminal, str)
                and (final_checkpoint or {}).get("observation", {}).get("value")
                    == terminal
                and bool((terminal_edit or {}).get("content"))
            )
            if (
                exact
                and immediate_undo
                and undo_restores_pre_paste
                and payload_absent_from_terminal_edit
                and typed_trajectory_reaches_terminal
            ):
                evidence.update({
                    "status": "canceled",
                    "reason": "raw_paste_immediately_undone_to_exact_pre_paste_state",
                    "resolvedContent": "",
                    "segments": [],
                    "localEdit": local,
                    "undoCheckpointID": undo_checkpoint.get("checkpointID"),
                    "undoObservationID": undo_observation.get("observationID"),
                    "undoEventTimestampNanoseconds": undo_event.get(
                        "eventTimestampNanoseconds"
                    ),
                    "subsequentTypedInputCount": len(subsequent_typed_events),
                    "terminalEdit": terminal_edit,
                })
            elif pure_paste_input and insertion:
                evidence.update({
                    "status": "proven",
                    "reason": (
                        "raw_pure_paste_exact_clipboard_span" if insertion == clipboard
                        else "raw_pure_paste_application_transformation"
                    ),
                    "resolvedContent": insertion,
                    "segments": [{
                        "type": "paste",
                        "content": insertion,
                        "clipboardSnapshotID": checkpoint.get("clipboardSnapshotID"),
                        "clipboardChangeCount": checkpoint.get("clipboardChangeCount"),
                        "pasteCheckpointID": checkpoint.get("checkpointID"),
                        "applicationFormattingObserved": insertion != clipboard,
                        "sourceClipboardTextSHA256": hashlib.sha256(
                            clipboard.encode()
                        ).hexdigest(),
                    }],
                    "localEdit": local,
                })
            elif (
                "paste" in record_hints
                and before
                and not after
                and same_conditioned_clipboard
            ):
                evidence.update({
                    "status": "proven",
                    "reason": "raw_grounded_paste_action_with_opaque_ax_epoch",
                    "resolvedContent": clipboard,
                    "segments": [{
                        "type": "paste",
                        "content": clipboard,
                        "clipboardSnapshotID": checkpoint.get(
                            "clipboardSnapshotID"
                        ),
                        "clipboardChangeCount": checkpoint.get(
                            "clipboardChangeCount"
                        ),
                        "pasteCheckpointID": checkpoint.get("checkpointID"),
                        "directSemanticInsertionObserved": False,
                        "deliverySemantics": (
                            "synchronous_cmd_v_with_opaque_post_paste_ax_epoch"
                        ),
                        "sourceClipboardTextSHA256": hashlib.sha256(
                            clipboard.encode()
                        ).hexdigest(),
                    }],
                    "localEdit": local,
                    "preValue": before,
                    "postValue": after,
                })
            elif exact:
                segments: list[dict[str, Any]] = []
                if position:
                    segments.append({
                        "type": "authored_text", "content": insertion[:position],
                    })
                segments.append({
                    "type": "paste",
                    "content": clipboard,
                    "clipboardSnapshotID": checkpoint.get("clipboardSnapshotID"),
                    "clipboardChangeCount": checkpoint.get("clipboardChangeCount"),
                    "pasteCheckpointID": checkpoint.get("checkpointID"),
                })
                suffix = insertion[position + len(clipboard):]
                if suffix:
                    segments.append({"type": "authored_text", "content": suffix})
                evidence.update({
                    "status": "proven",
                    "reason": "raw_pre_post_paste_exact_clipboard_span",
                    "resolvedContent": insertion,
                    "segments": segments,
                    "localEdit": local,
                })
            else:
                evidence.update({
                    "reason": "raw_mixed_paste_clipboard_span_not_exact",
                    "resolvedContent": insertion,
                    "localEdit": local,
                })
            results.append(evidence)
    return results


def structured_target(
    episode: OpenEpisode,
    before: str,
    after: str,
    raw_records: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
    edit = minimal_edit(before, after)
    aligned_edit = verified_single_member_alignment(
        episode, before, after, edit
    )
    if aligned_edit is not None:
        edit = aligned_edit
    selected_text = (
        episode.first.get("conditioningState", {})
        .get("cursorContext", {})
        .get("selectedText")
    )
    if selected_text == before and before:
        edit = {
            "operation": "replace",
            "characterOffset": 0,
            "removedContent": before,
            "content": after,
        }
    raw_content = edit["content"]
    content = normalize_authored_content(raw_content)
    paste_evidence = [
        evidence
        for member in episode.members
        if "paste" in set(member.get("inputHints", []))
        for evidence in raw_paste_evidence(member, raw_records)
    ]
    paste_signal_count = sum(
        "paste" in set(member.get("inputHints", [])) for member in episode.members
    )
    audit = {
        "netFieldEdit": edit,
        "pasteEvidenceCount": len(paste_evidence),
        "alignmentSource": (
            "compiler_verified_single_member_alignment"
            if aligned_edit is not None else "canonical_minimal_diff"
        ),
    }
    canceled_paste_evidence = [
        evidence for evidence in paste_evidence
        if evidence.get("status") == "canceled"
    ]
    active_paste_evidence = [
        evidence for evidence in paste_evidence
        if evidence.get("status") != "canceled"
    ]
    audit["canceledPasteEvidenceCount"] = len(canceled_paste_evidence)
    episode_hints = {
        hint for member in episode.members for hint in member.get("inputHints", [])
    }
    raw_paste_only_action = (
        paste_signal_count > 0
        and not episode_hints.intersection({"typed", "delete", "cut"})
        and len(paste_evidence) >= paste_signal_count
        and all(evidence.get("status") == "proven" for evidence in paste_evidence)
    )
    if raw_paste_only_action:
        content = "".join(
            normalize_authored_content(str(evidence.get("resolvedContent") or ""))
            for evidence in paste_evidence
        )
        audit["completionSource"] = "raw_local_paste_transitions"
    else:
        audit["completionSource"] = "initial_to_terminal_field_diff"
    if not content:
        return None, "no_surviving_authored_or_pasted_content", audit
    if not paste_signal_count or (
        canceled_paste_evidence
        and not active_paste_evidence
        and len(paste_evidence) >= paste_signal_count
    ):
        return {
            "schemaVersion": 1,
            "resolvedContent": content,
            "segments": [{"type": "authored_text", "content": content}],
        }, (
            "complete_diff_after_exactly_canceled_paste"
            if canceled_paste_evidence else "complete_initial_to_terminal_minimal_diff"
        ), audit
    if (
        len(paste_evidence) < paste_signal_count
        or any(
            evidence.get("status") != "proven"
            for evidence in active_paste_evidence
        )
    ):
        return {
            "schemaVersion": 1,
            "resolvedContent": content,
            "segments": [{"type": "unresolved_paste_transition", "content": content}],
        }, "complete_transition_unresolved_paste_authorship", audit
    evidence_insertions: list[
        tuple[str, str, list[dict[str, Any]], int | None]
    ] = []
    for evidence in active_paste_evidence:
        pasted = [row for row in evidence.get("segments", []) if row.get("type") == "paste"]
        if len(pasted) != 1 or not isinstance(pasted[0].get("content"), str):
            return {
                "schemaVersion": 1,
                "resolvedContent": content,
                "segments": [{"type": "unresolved_paste_transition", "content": content}],
            }, "paste_payload_not_uniquely_proven", audit
        full_insertion = evidence.get("resolvedContent")
        if not isinstance(full_insertion, str) or not full_insertion:
            full_insertion = pasted[0]["content"]
        paste_content = pasted[0]["content"]
        paste_position = full_insertion.find(paste_content)
        if (
            paste_position < 0
            or full_insertion.find(paste_content, paste_position + 1) >= 0
        ):
            return {
                "schemaVersion": 1,
                "resolvedContent": content,
                "segments": [{"type": "unresolved_paste_transition", "content": content}],
            }, "paste_payload_not_unique_in_proven_insertion", audit
        sentinel = "\ufdd0COUPLED_PASTE\ufdd1"
        marked = (
            full_insertion[:paste_position]
            + sentinel
            + full_insertion[paste_position + len(paste_content):]
        )
        normalized_marked = normalize_authored_content(marked)
        marker_position = normalized_marked.find(sentinel)
        if marker_position < 0:
            raise AssertionError("paste normalization removed sentinel")
        prefix = normalized_marked[:marker_position]
        suffix = normalized_marked[marker_position + len(sentinel):]
        normalized_paste = normalize_authored_content(paste_content)
        normalized_full = prefix + normalized_paste + suffix
        insertion_segments: list[dict[str, Any]] = []
        if prefix:
            insertion_segments.append({"type": "authored_text", "content": prefix})
        insertion_segments.append({
            key: value for key, value in pasted[0].items() if key != "content"
        } | {"historyContent": normalized_paste})
        if suffix:
            insertion_segments.append({"type": "authored_text", "content": suffix})
        local_edit = evidence.get("localEdit") or {}
        field_offset = local_edit.get("characterOffset")
        evidence_insertions.append((
            full_insertion,
            normalized_full,
            insertion_segments,
            field_offset if isinstance(field_offset, int) else None,
        ))
    segments: list[dict[str, Any]] = []
    cursor = 0
    placement_rules: list[str] = []
    for index, (
        raw_insertion,
        normalized_insertion,
        insertion_segments,
        field_offset,
    ) in enumerate(evidence_insertions):
        insertion_position = -1
        search_insertion = normalized_insertion

        # A synchronous pre/post-paste checkpoint supplies the insertion's
        # absolute field offset. Prefer that observed location to textual
        # search: the clipboard payload may legitimately already occur in the
        # authored text. The anchor is accepted only when both the raw field
        # slice and the normalized final transition round-trip exactly.
        if field_offset is not None:
            relative_offset = field_offset - edit["characterOffset"]
            if (
                relative_offset >= 0
                and raw_content[
                    relative_offset:relative_offset + len(raw_insertion)
                ] == raw_insertion
            ):
                sentinel = f"\ufdd0COUPLED_PASTE_{index}\ufdd1"
                marked_raw = (
                    raw_content[:relative_offset]
                    + sentinel
                    + raw_content[relative_offset + len(raw_insertion):]
                )
                normalized_marked = normalize_authored_content(marked_raw)
                marker_position = normalized_marked.find(sentinel)
                if marker_position >= 0:
                    anchored_round_trip = normalized_marked.replace(
                        sentinel, normalized_insertion, 1
                    )
                    if anchored_round_trip == content:
                        insertion_position = marker_position
                        placement_rules.append("raw_checkpoint_field_offset")

        # Older/application-transformed evidence may not map directly into the
        # final net region. Preserve the former conservative fallback only when
        # the normalized insertion is textually unique after prior pastes.
        if insertion_position < 0:
            insertion_position = content.find(search_insertion, cursor)
            if insertion_position < 0:
                search_insertion = normalized_insertion.strip("\n\r\u200b")
                insertion_position = content.find(search_insertion, cursor)
            if (
                insertion_position >= 0
                and content.find(search_insertion, insertion_position + 1) < 0
            ):
                placement_rules.append("unique_normalized_text_fallback")
            else:
                insertion_position = -1

        if insertion_position < cursor:
            return {
                "schemaVersion": 1,
                "resolvedContent": content,
                "segments": [{"type": "unresolved_paste_transition", "content": content}],
            }, "paste_checkpoint_offset_not_proven_in_final_region", audit
        position = insertion_position
        if position > cursor:
            segments.append({"type": "authored_text", "content": content[cursor:position]})
        segments.extend(insertion_segments)
        cursor = position + len(search_insertion)
    if cursor < len(content):
        segments.append({"type": "authored_text", "content": content[cursor:]})
    audit["pastePlacementRules"] = placement_rules
    return {
        "schemaVersion": 1, "resolvedContent": content, "segments": segments,
    }, "complete_diff_with_grounded_paste_segments", audit


def merge_authorship_segments(
    segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for segment in segments:
        if not segment:
            continue
        if (
            merged
            and segment.get("type") == "authored_text"
            and merged[-1].get("type") == "authored_text"
        ):
            merged[-1]["content"] = str(merged[-1].get("content") or "") + str(
                segment.get("content") or ""
            )
        else:
            merged.append(dict(segment))
    return merged


def opaque_paste_member_target(
    member: dict[str, Any], raw_records: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    evidence = [
        row for row in raw_paste_evidence(member, raw_records)
        if row.get("reason") == "raw_grounded_paste_action_with_opaque_ax_epoch"
    ]
    if len(evidence) != 1:
        return None, {"reason": "requires_exactly_one_grounded_opaque_paste"}
    row = evidence[0]
    before = member.get("beforeLogicalValue")
    pre = row.get("preValue")
    post = row.get("postValue")
    terminal = member.get("selectedTerminalLogicalValue")
    if not all(isinstance(value, str) for value in (before, pre, post, terminal)):
        return None, {"reason": "opaque_paste_epoch_endpoint_missing"}
    prefix = normalize_authored_content(minimal_edit(before, pre)["content"])
    suffix = normalize_authored_content(minimal_edit(post, terminal)["content"])
    paste = next(
        (segment for segment in row.get("segments", []) if segment.get("type") == "paste"),
        None,
    )
    if not isinstance(paste, dict) or not isinstance(paste.get("content"), str):
        return None, {"reason": "opaque_paste_payload_missing"}
    paste_content = normalize_authored_content(paste["content"])
    segments: list[dict[str, Any]] = []
    if prefix:
        segments.append({"type": "authored_text", "content": prefix})
    segments.append({
        key: value for key, value in paste.items() if key != "content"
    } | {"historyContent": paste_content})
    if suffix:
        segments.append({"type": "authored_text", "content": suffix})
    return {
        "schemaVersion": 1,
        "resolvedContent": prefix + paste_content + suffix,
        "segments": segments,
    }, {
        "reason": "grounded_opaque_paste_epoch_transcript",
        "pasteCheckpointID": row.get("checkpointID"),
        "prefixEdit": minimal_edit(before, pre),
        "suffixEdit": minimal_edit(post, terminal),
    }


def stitched_epoch_target(
    episode: OpenEpisode, raw_records: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
    """Compose only AX epochs separated by a specifically proven prompt reset."""
    groups: list[list[dict[str, Any]]] = [[episode.primitives[0]]]
    for primitive in episode.primitives[1:]:
        left = groups[-1][-1]["members"][0]
        right = primitive["members"][0]
        if prompt_epoch_reset_compatible(left, right):
            groups.append([primitive])
        else:
            groups[-1].append(primitive)
    if len(groups) == 1:
        opaque = [
            row
            for member in episode.members
            for row in raw_paste_evidence(member, raw_records)
            if row.get("reason") == "raw_grounded_paste_action_with_opaque_ax_epoch"
        ]
        if opaque and len(episode.members) == 1:
            target, audit = opaque_paste_member_target(
                episode.first, raw_records
            )
            return target, str(audit.get("reason")), {
                "completionSource": "locally_verified_ax_epoch_transcript",
                "epochCount": 1,
                "epochs": [{
                    "ordinal": 0,
                    "reason": audit.get("reason"),
                    "audit": audit,
                    "memberWriteEventIDs": [episode.first["writeEventID"]],
                }],
            }
        return structured_target(
            episode,
            episode.first.get("beforeLogicalValue"),
            episode.last.get("selectedTerminalLogicalValue"),
            raw_records,
        )

    combined: list[dict[str, Any]] = []
    epoch_audit: list[dict[str, Any]] = []
    for ordinal, primitives in enumerate(groups):
        subgroup = OpenEpisode(primitives, episode.onset_partition_reason)
        opaque = [
            row
            for member in subgroup.members
            for row in raw_paste_evidence(member, raw_records)
            if row.get("reason") == "raw_grounded_paste_action_with_opaque_ax_epoch"
        ]
        if opaque:
            if len(subgroup.members) != 1:
                return None, "opaque_paste_spans_multiple_micro_writes", {
                    "epochCount": len(groups), "failedEpoch": ordinal,
                }
            target, audit = opaque_paste_member_target(
                subgroup.first, raw_records
            )
            reason = audit.get("reason")
        else:
            before = subgroup.first.get("beforeLogicalValue")
            after = subgroup.last.get("selectedTerminalLogicalValue")
            if not isinstance(before, str) or not isinstance(after, str):
                return None, "stitched_epoch_endpoint_missing", {
                    "epochCount": len(groups), "failedEpoch": ordinal,
                }
            target, reason, audit = structured_target(
                subgroup, before, after, raw_records
            )
        if target is None:
            return None, "stitched_epoch_unreconstructible", {
                "epochCount": len(groups), "failedEpoch": ordinal,
                "failedReason": reason, "failedAudit": audit,
            }
        combined.extend(target.get("segments", []))
        epoch_audit.append({
            "ordinal": ordinal, "reason": reason, "audit": audit,
            "memberWriteEventIDs": [
                member["writeEventID"] for member in subgroup.members
            ],
        })
    combined = merge_authorship_segments(combined)
    resolved = "".join(
        str(segment.get("content") or segment.get("historyContent") or "")
        for segment in combined
    )
    return {
        "schemaVersion": 1,
        "resolvedContent": resolved,
        "segments": combined,
    }, "raw_prompt_ax_epochs_stitched", {
        "completionSource": "locally_verified_ax_epoch_transcript",
        "epochCount": len(groups),
        "epochs": epoch_audit,
    }


def next_events(
    events: list[dict[str, Any]], session_id: str, after: dt.datetime, limit: int = 5
) -> list[dict[str, Any]]:
    return sorted(
        [
            event for event in events
            if event.get("sessionID") == session_id
            and timestamp(event["availableAt"]) > after
        ],
        key=lambda row: (row["availableAt"], row.get("beganAt") or row["availableAt"]),
    )[:limit]


def classify_episode(
    episode: OpenEpisode,
    events: list[dict[str, Any]],
    event_by_id: dict[str, dict[str, Any]],
    raw_records: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    first, last = episode.first, episode.last
    session_id = event_by_id[first["writeEventID"]]["sessionID"]
    recovered, recovered_conditioning = strict_fast_start(episode, raw_records)
    prompt = is_prompt_surface(first)
    observed_before = first.get("beforeLogicalValue")
    scaffold_before = (
        isinstance(observed_before, str)
        and observed_before.strip().lower() in PROMPT_SCAFFOLDS
    )
    before = "" if recovered or scaffold_before else observed_before
    after = last.get("selectedTerminalLogicalValue")
    proven_revision_pairs = {
        tuple(boundary.get("between", []))
        for boundary in episode.boundary_evidence
        if boundary.get("decision") == "continue"
        and boundary.get("sameElementInternalRevision") is True
    }
    continuity = all(
        episode_state_continuous(left, right)
        or (left["writeEventID"], right["writeEventID"]) in proven_revision_pairs
        for left, right in zip(episode.members, episode.members[1:])
    )
    epoch_reset_count = sum(
        prompt_epoch_reset_compatible(left, right)
        for left, right in zip(episode.members, episode.members[1:])
    )
    grounded_opaque_paste_count = sum(
        row.get("reason") == "raw_grounded_paste_action_with_opaque_ax_epoch"
        for member in episode.members
        for row in raw_paste_evidence(member, raw_records)
    )
    internal_revision_count = len(proven_revision_pairs)
    same_destination = all(
        stable_destination(member) == stable_destination(first)
        for member in episode.members
    )
    if not isinstance(after, str):
        reconstruction_status = "unreconstructible_missing_endpoint"
        target = None
        reconstruction_reason = reconstruction_status
        reconstruction_audit: dict[str, Any] = {}
    elif not isinstance(before, str):
        terminal_content = normalize_authored_content(after)
        target = ({
            "schemaVersion": 1,
            "resolvedContent": terminal_content,
            "segments": [{"type": "authored_text", "content": terminal_content}],
        } if terminal_content else None)
        reconstruction_status = "reconstructed_terminal_without_onset"
        reconstruction_reason = "terminal_field_value_preserved_without_onset"
        reconstruction_audit = {}
    elif not continuity or not same_destination:
        reconstruction_status = "unreconstructible_state_or_destination_discontinuity"
        target = None
        reconstruction_reason = reconstruction_status
        reconstruction_audit = {}
    else:
        if epoch_reset_count or grounded_opaque_paste_count:
            target, reconstruction_reason, reconstruction_audit = (
                stitched_epoch_target(episode, raw_records)
            )
        else:
            target, reconstruction_reason, reconstruction_audit = structured_target(
                episode, before, after, raw_records
            )
        reconstruction_status = (
            "reconstructed" if target is not None else "reconstructed_no_surviving_output"
        )

    boundary = last.get("boundaryReason")
    hints = set(last.get("inputHints", []))
    candidate_available_at = last["availableAt"]
    if isinstance(last.get("submissionObservedAt"), str):
        candidate_available_at = max(
            candidate_available_at,
            last["submissionObservedAt"],
            key=timestamp,
        )
    following = next_events(events, session_id, timestamp(candidate_available_at))
    following_raw: list[dict[str, Any]] = []
    for event in following:
        for record_id in event.get("sourceRecordIDs", []):
            record = raw_records.get(record_id)
            if not isinstance(record, dict):
                continue
            following_raw.append({
                "eventID": event["sourceEventID"],
                "recordID": record_id,
                "recordType": record.get("recordType"),
                "capturedAt": record.get("capturedAt") or record.get("observedAt"),
                "triggerTypes": record.get("triggerTypes"),
                "screenshotRelativePath": record.get("screenshotRelativePath"),
                "screenshotSHA256": record.get("screenshotSHA256"),
                "postCaptureSurface": record.get("postCaptureSurface"),
            })
    submitted = boundary in {"return_pressed", "submission_boundary"} or (
        prompt and "return" in hints
    )
    x_post = (
        (first.get("targetIdentity") or {}).get("bundleIdentifier") == "com.google.Chrome"
        and "post text" in str(
            (first.get("targetIdentity") or {}).get("fieldDescription") or ""
        ).lower()
        and boundary == "pointer_selection_boundary"
        and any(event.get("kind") == "read" for event in following[:2])
    )
    boundary_to_continuation = (
        episode.boundary_evidence[-1] if episode.boundary_evidence else {}
    )
    novel_read_same_surface_continuation = (
        episode.close_reason == "novel_read"
        and boundary_to_continuation.get("sameLogicalDestination") is True
        and boundary_to_continuation.get("stateContinuous") is True
    )
    if submitted:
        closure_status = "closed_submission"
        closure_reason = "return_or_submission_boundary"
    elif x_post:
        closure_status = "closed_submission"
        closure_reason = "pointer_post_field_departure_with_raw_following_surface"
    elif novel_read_same_surface_continuation and prompt:
        closure_status = "closed_composition_region"
        closure_reason = "novel_causal_read_partition"
    elif episode.close_reason in {
        "novel_read", "destination_changed", "affected_region_changed",
        "prior_submission", "state_discontinuity",
    } and not prompt:
        closure_status = "closed_persistent_region"
        closure_reason = episode.close_reason
    elif episode.close_reason == "affected_region_changed" and prompt:
        closure_status = "closed_composition_region"
        closure_reason = "new_composition_after_navigation_boundary"
    else:
        closure_status = "open_or_abandoned"
        closure_reason = episode.close_reason or "no_structural_closure"

    conditioning = recovered_conditioning or json.loads(json.dumps(
        first.get("conditioningState") or {}
    ))
    if scaffold_before:
        cursor = conditioning.setdefault("cursorContext", {})
        cursor["fieldState"] = "unpopulated_prompt"
        cursor["leftContext"] = ""
        cursor["selectedText"] = ""
        cursor["rightContext"] = ""
        cursor["surfacePrompt"] = observed_before
        conditioning["logicalBaselinePolicy"] = "known_application_prompt_scaffold"
    cursor = (conditioning or {}).get("cursorContext", {})
    initial_field_state = cursor.get("fieldState")
    visible_before = "".join(
        str(cursor.get(key) or "")
        for key in ("leftContext", "selectedText", "rightContext")
    )
    empty_prompt_observed = (
        isinstance(observed_before, str)
        and not observed_before.replace("\u200b", "").replace("\ufeff", "").strip()
        and not visible_before.replace("\u200b", "").replace("\ufeff", "").strip()
    )
    entire_field_selected = (
        isinstance(observed_before, str)
        and bool(observed_before)
        and cursor.get("leftContext", "") == ""
        and cursor.get("rightContext", "") == ""
        and cursor.get("selectedText") == observed_before
    )
    onset_proven = bool(conditioning) and (
        not prompt
        or recovered
        or initial_field_state == "unpopulated_prompt"
        or empty_prompt_observed
        or entire_field_selected
        or episode.onset_partition_reason == "novel_read"
    )
    if (
        prompt and not onset_proven and isinstance(after, str)
        and reconstruction_status.startswith("reconstructed")
    ):
        terminal_content = normalize_authored_content(after)
        if terminal_content:
            target = {
                "schemaVersion": 1,
                "resolvedContent": terminal_content,
                "segments": [{
                    "type": "unresolved_authorship",
                    "content": terminal_content,
                }],
            }
            reconstruction_status = "reconstructed_terminal_without_proven_onset"
            reconstruction_reason = "terminal_field_value_preserved_without_onset"
    resolved = target.get("resolvedContent", "") if target else ""
    target_segments = target.get("segments", []) if target else []
    pure_paste = bool(target_segments) and all(
        segment.get("type") == "paste" for segment in target_segments
    )
    unresolved_paste = any(
        segment.get("type") == "unresolved_paste_transition"
        for segment in target_segments
    )
    unresolved_authorship = any(
        segment.get("type") == "unresolved_authorship"
        for segment in target_segments
    )
    authored_for_eligibility = "".join(
        str(segment.get("content") or "")
        for segment in target_segments
        if segment.get("type") == "authored_text"
    )
    submitted_substantive = (
        len(authored_for_eligibility.strip()) >= MIN_SUBMITTED_CHARACTERS
    )
    persistent_substantive = (
        len(authored_for_eligibility.strip()) >= MIN_PERSISTENT_CHARACTERS
        and len(words(authored_for_eligibility)) >= MIN_PERSISTENT_WORDS
    )
    mechanical = is_mechanical_destination(first, resolved)
    eligible = (
        reconstruction_status == "reconstructed"
        and closure_status.startswith("closed_")
        and onset_proven
        and not pure_paste
        and not unresolved_paste
        and not unresolved_authorship
        and not mechanical
        and (
            submitted_substantive
            if prompt and closure_status == "closed_submission"
            else persistent_substantive
        )
    )
    if eligible:
        loss_status = "eligible"
        loss_reason = "closed_reconstructed_substantive_authored_completion"
    elif target is None:
        loss_status = "ineligible"
        loss_reason = reconstruction_reason
    elif pure_paste:
        loss_status = "ineligible"
        loss_reason = "pure_paste_history_only"
    elif unresolved_paste:
        loss_status = "ineligible"
        loss_reason = "paste_authorship_unresolved"
    elif unresolved_authorship:
        loss_status = "ineligible"
        loss_reason = "prediction_time_onset_unavailable"
    elif mechanical:
        loss_status = "ineligible"
        loss_reason = "instrumental_navigation_or_url"
    elif not onset_proven:
        loss_status = "ineligible"
        loss_reason = "prediction_time_onset_unavailable"
    elif not closure_status.startswith("closed_"):
        loss_status = "ineligible"
        loss_reason = "composition_not_structurally_closed"
    else:
        loss_status = "ineligible"
        loss_reason = "authored_content_below_substantive_threshold"

    member_ids = [member["writeEventID"] for member in episode.members]
    candidate_id = "raw_episode_candidate_" + hashlib.sha256(
        json.dumps(member_ids, separators=(",", ":")).encode()
    ).hexdigest()
    candidate = {
        "schemaVersion": 4,
        "authority": "raw_authoritative_episode_state_machine",
        "candidateID": candidate_id,
        "label": candidate_id,
        "memberWriteEventIDs": member_ids,
        "sourceRecordIDs": sorted({
            source_id for member in episode.members
            for source_id in member.get("sourceRecordIDs", [])
        }),
        "beganAt": first["beganAt"],
        "candidateAvailableAt": candidate_available_at,
        "initialConditioningState": conditioning,
        "initialObservationSource": (
            "strict_single_character_fast_start_recovery" if recovered
            else episode.primitives[0].get("initialObservationSource")
        ),
        "onsetEvidence": {
            "policy": "raw_episode_onset_state_machine",
            "requiresProvenPromptOnset": prompt,
            "promptOnsetProven": onset_proven if prompt else None,
            "proofReason": (
                "strict_single_character_fast_start_recovery" if recovered
                else "known_application_prompt_scaffold" if scaffold_before
                else "empty_prompt_observed" if empty_prompt_observed
                else "entire_field_selected_for_replacement" if entire_field_selected
                else episode.onset_partition_reason
            ),
            "emptyPromptObserved": empty_prompt_observed,
            "entireFieldSelectedForReplacement": entire_field_selected,
        },
        "members": episode.members,
        "causalEvidence": {
            "interveningReadAssessments": episode.read_assessments,
            "novelInterveningReadCount": sum(
                row["status"] == "novel_causally_available_read"
                for row in episode.read_assessments
            ),
            "noNovelCausallyAvailableReadDuringCandidate": not any(
                row["status"] == "novel_causally_available_read"
                for row in episode.read_assessments
            ),
            "noOverlappingOutsideWrite": True,
        },
        "surfaceEvidence": {"logicalEditableIdentityStable": same_destination},
        "continuityEvidence": {
            "continuousReplayableState": continuity,
            "rawPromptAXEpochResetCount": epoch_reset_count,
            "groundedOpaquePasteEpochCount": grounded_opaque_paste_count,
            "sameElementInternalRevisionCount": internal_revision_count,
        },
        "mechanicalGates": {
            "passed": continuity and same_destination and not any(
                row["status"] == "novel_causally_available_read"
                for row in episode.read_assessments
            )
        },
        "singleCompletionDiagnostic": {
            "status": reconstruction_status,
            "netFieldEdit": reconstruction_audit.get("netFieldEdit"),
            "proposedFinalizedTarget": resolved if target else None,
        },
        "closureEvidence": {
            "status": closure_status,
            "lastBoundaryReason": boundary,
            "captureBoundaryReason": last.get("captureBoundaryReason"),
            "submissionObservedAt": last.get("submissionObservedAt"),
            "rawSubmissionObservation": last.get("submissionClosureEvidence"),
            "lastInputHints": sorted(hints),
            "returnObserved": "return" in hints,
            "objectiveSubmissionBoundary": submitted,
            "followingEvents": [event["sourceEventID"] for event in following],
            "followingRawObservations": following_raw,
        },
        "episodeStateMachine": {
            "onsetPartitionReason": episode.onset_partition_reason,
            "closeReason": episode.close_reason,
            "boundaryEvidence": episode.boundary_evidence,
            "affectedRegionPolicy": "net_region_overlap_or_prompt_surface",
            "internalRevisionPolicy": "same_ax_element_short_internal_revision_v1",
        },
    }
    decision = {
        "schemaVersion": 3,
        "label": candidate_id,
        "candidateID": candidate_id,
        "memberWriteEventIDs": member_ids,
        "decision": "closed_loss_episode" if eligible else (
            "closed_history_episode" if target is not None else "exclude_unresolved_episode"
        ),
        "finalizedTarget": target,
        "closureReason": closure_reason,
        "classificationProvenance": "raw_episode_state_machine_v2",
        "reconstructionStatus": reconstruction_status,
        "closureStatus": closure_status,
        "lossEligibility": loss_status,
        "reason": loss_reason,
        "minimumAuthoredCharacters": MIN_PERSISTENT_CHARACTERS,
        "minimumWords": MIN_PERSISTENT_WORDS,
        "minimumSubmittedAuthoredCharacters": MIN_SUBMITTED_CHARACTERS,
    }
    return candidate, decision


def assemble(
    corpus: Path,
    primitives_path: Path,
    output: Path,
    project: Path,
    projection: Any,
) -> dict[str, Any]:
    manifest = load_json(corpus / "corpus.json")
    primitive_manifest = load_json(primitives_path / "episode-review.json")
    source = primitive_manifest.get("source") or {}
    if source.get("corpusID") != manifest.get("corpusID"):
        raise ValueError("primitive evidence belongs to another corpus")
    if source.get("eventsSHA256") != sha256(corpus / "events.jsonl"):
        raise ValueError("primitive evidence event digest mismatch")
    primitives = load_jsonl(primitives_path / "episode-candidates.jsonl")
    events = load_jsonl(corpus / "events.jsonl")
    event_by_id = {event["sourceEventID"]: event for event in events}
    write_ids = {event["sourceEventID"] for event in events if event["kind"] == "write"}
    primitive_ids = [row["memberWriteEventIDs"][0] for row in primitives]
    if (
        len(primitives) != len(write_ids)
        or len(set(primitive_ids)) != len(primitive_ids)
        or set(primitive_ids) != write_ids
        or any(len(row.get("memberWriteEventIDs", [])) != 1 for row in primitives)
    ):
        raise ValueError("primitive evidence is not exact one-to-one WRITE coverage")

    raw_records: dict[str, dict[str, Any]] = {}
    raw_path_by_session: dict[str, Path] = {}
    for candidate in primitives:
        member = candidate["members"][0]
        event = event_by_id[member["writeEventID"]]
        path = (project / member["rawPath"]).resolve()
        existing = raw_path_by_session.setdefault(event["sessionID"], path)
        if existing != path:
            raise ValueError(f"session spans multiple raw journals: {event['sessionID']}")
    needed_by_path: dict[Path, set[str]] = {}
    for event in events:
        path = raw_path_by_session.get(event["sessionID"])
        if path is None:
            raise ValueError(f"session has no raw journal: {event['sessionID']}")
        needed_by_path.setdefault(path, set()).update(event.get("sourceRecordIDs", []))
    for path, needed in needed_by_path.items():
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                if row.get("recordID") in needed:
                    raw_records[row["recordID"]] = row
    missing_raw = set().union(*needed_by_path.values()) - set(raw_records)
    if missing_raw:
        raise ValueError(
            f"raw journals are missing {len(missing_raw)} referenced records; "
            f"first={sorted(missing_raw)[0]}"
        )

    by_session: dict[str, list[dict[str, Any]]] = {}
    for primitive in primitives:
        event = event_by_id[primitive["memberWriteEventIDs"][0]]
        by_session.setdefault(event["sessionID"], []).append(primitive)
    episodes: list[OpenEpisode] = []
    for session_id, rows in by_session.items():
        rows.sort(key=lambda row: (row["beganAt"], row["memberWriteEventIDs"][0]))
        current = OpenEpisode([rows[0]], "session_start")
        for following in rows[1:]:
            left = current.last
            right = following["members"][0]
            left_event = event_by_id[left["writeEventID"]]
            right_event = event_by_id[right["writeEventID"]]
            lower = timestamp(left["availableAt"])
            upper = timestamp(right["beganAt"])
            before = current.first.get("beforeLogicalValue")
            current_after = left.get("selectedTerminalLogicalValue")
            right_after = right.get("selectedTerminalLogicalValue")
            current_completion = ""
            if isinstance(before, str) and isinstance(current_after, str):
                current_completion = minimal_edit(before, current_after)["content"]
            assessments, novel = read_assessments(
                events, session_id, timestamp(current.first["beganAt"]), lower, upper,
                current_completion, left.get("application"),
            )
            same_destination = stable_destination(left) == stable_destination(right)
            exact_continuity = field_state_continuous(left, right)
            epoch_reset = prompt_epoch_reset_compatible(left, right)
            internal_revision, internal_revision_evidence = (
                same_element_internal_revision_continuation(current, following)
            )
            continuous = exact_continuity or epoch_reset or internal_revision
            region_ok = False
            region_evidence: dict[str, Any] = {}
            if all(isinstance(value, str) for value in (before, current_after, right_after)):
                region_ok, region_evidence = affected_region_compatible(
                    before, current_after, right_after, is_prompt_surface(left)
                )
                if (
                    is_prompt_surface(left)
                    and left.get("boundaryReason") == "selection_navigation"
                    and len(current_completion.strip()) >= MIN_PERSISTENT_CHARACTERS
                    and region_evidence.get("nextLocalRegion", [0])[0]
                        >= region_evidence.get("episodeRegionBeforeNext", [0, 0])[1]
                ):
                    region_ok = False
                    region_evidence["navigationBeganNewFrontierComposition"] = True
            elif (
                is_prompt_surface(left)
                and continuous
                and isinstance(current_after, str)
                and isinstance(right_after, str)
            ):
                region_ok = True
                region_evidence = {
                    "promptSurfaceKeepsUnknownOnsetCompositionOpen": True
                }
            if epoch_reset:
                region_ok = True
                region_evidence["rawPromptAXEpochResetContinuation"] = True
            if internal_revision:
                region_ok = True
                region_evidence["sameElementInternalRevisionContinuation"] = True
            submitted = left.get("boundaryReason") in {
                "return_pressed", "submission_boundary"
            } or (is_prompt_surface(left) and "return" in set(left.get("inputHints", [])))
            if same_destination and continuous and region_ok and not novel and not submitted:
                current.primitives.append(following)
                current.read_assessments.extend(assessments)
                current.boundary_evidence.append({
                    "between": [left["writeEventID"], right["writeEventID"]],
                    "decision": "continue",
                    "stateContinuous": continuous,
                    "exactFieldStateContinuous": exact_continuity,
                    "rawPromptAXEpochReset": epoch_reset,
                    "sameElementInternalRevision": internal_revision,
                    "internalRevisionEvidence": internal_revision_evidence,
                    "sameLogicalDestination": same_destination,
                    "readAssessments": assessments,
                    "affectedRegion": region_evidence,
                })
                continue
            if submitted:
                reason = "prior_submission"
            elif novel:
                reason = "novel_read"
            elif not same_destination:
                reason = "destination_changed"
            elif not continuous:
                reason = "state_discontinuity"
            else:
                reason = "affected_region_changed"
            current.close_reason = reason
            current.boundary_evidence.append({
                "between": [left["writeEventID"], right["writeEventID"]],
                "decision": "partition",
                "reason": reason,
                "stateContinuous": continuous,
                "exactFieldStateContinuous": exact_continuity,
                "rawPromptAXEpochReset": epoch_reset,
                "sameElementInternalRevision": internal_revision,
                "internalRevisionEvidence": internal_revision_evidence,
                "sameLogicalDestination": same_destination,
                "readAssessments": assessments,
                "affectedRegion": region_evidence,
            })
            episodes.append(current)
            current = OpenEpisode([following], reason)
        current.close_reason = "session_end"
        episodes.append(current)

    candidates: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    for episode in sorted(episodes, key=lambda value: value.first["beganAt"]):
        candidate, decision = classify_episode(
            episode, events, event_by_id, raw_records
        )
        candidates.append(candidate)
        decisions.append(decision)

    temporary = Path(tempfile.mkdtemp(prefix="raw-episode-v1-", dir=output.parent))
    adjudications_path = temporary / "episode-decisions.jsonl"
    candidates_path = temporary / "episode-candidates.jsonl"
    write_jsonl(adjudications_path, decisions)
    write_jsonl(candidates_path, candidates)
    projection.EPISODE_VERSION = EPISODE_VERSION
    projection.CONVERSION_VERSION = CONVERSION_VERSION
    artifact = projection.construct(corpus, adjudications_path, [candidates_path], output)
    # The projection copies its decisions and candidates' hashes. Preserve a
    # complete state-machine audit next to the model-facing corpus as well.
    write_jsonl(output / "raw-episode-candidates.jsonl", candidates)
    artifact = load_json(output / "corpus.json")
    artifact["artifactType"] = "phase1_raw_authoritative_episode_corpus"
    artifact["source"]["candidateEvidenceSHA256"] = {
        "raw-episode-candidates.jsonl": sha256(
            output / "raw-episode-candidates.jsonl"
        )
    }
    artifact["source"]["adjudicationsSHA256"] = sha256(
        output / "episode-adjudications.jsonl"
    )
    artifact["eligibility"].update({
        "episodePolicy": (
            "raw-authoritative state machine; complete lineage coverage; "
            "ambiguity withheld from loss"
        ),
        "automaticSubmittedShortMinimumEnabled": True,
        "reviewedConciseSubmissionsMayOverrideGeneralMinimum": False,
        "reviewedRegressionDecisionsOverrideLengthHeuristics": False,
        "unresolvedOrUnclosedMicroWritesAppearInModelHistory": False,
        "strictFastStartRecoveryEnabled": True,
        "knownPromptScaffoldsAreLogicalEmptyBaselines": True,
        "sameElementInternalRevisionContinuationEnabled": True,
    })
    artifact["rawEpisodeArchitecture"] = {
        "sourceAuthority": "immutable_raw_journals",
        "semanticPrimitives": str(primitives_path.relative_to(project)),
        "semanticPrimitivesSHA256": sha256(primitives_path / "episode-candidates.jsonl"),
        "productionConsumesRegressionFixture": False,
        "stateMachineVersion": EPISODE_VERSION,
        "separateStatuses": [
            "reconstructionStatus", "closureStatus", "lossEligibility",
        ],
    }
    artifact["artifactDigestsSHA256"]["raw-episode-candidates.jsonl"] = sha256(
        output / "raw-episode-candidates.jsonl"
    )
    (output / "corpus.json").write_text(
        json.dumps(artifact, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output / "dataset.json").write_text(
        json.dumps(artifact, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    shutil.rmtree(temporary)
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--primitives", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    project = Path(__file__).resolve().parent.parent
    projection = load_module(
        "phase1_episode_projection",
        project / "scripts/construct-phase1-closed-episode-corpus.py",
    )
    output = args.output.resolve()
    if output.exists():
        raise ValueError(f"output already exists: {output}")
    artifact = assemble(
        args.corpus.resolve(), args.primitives.resolve(), output, project, projection
    )
    print(json.dumps(artifact["counts"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

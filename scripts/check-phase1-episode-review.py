#!/usr/bin/env python3
"""Regression checks for the non-authoritative Phase 1 episode review layer."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


def load_builder() -> ModuleType:
    path = Path(__file__).with_name("build-phase1-episode-review.py")
    spec = importlib.util.spec_from_file_location("phase1_episode_review", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def event(
    event_id: str,
    kind: str,
    began_at: str,
    available_at: str,
    *,
    application: str = "Obsidian",
    window: str = "Data.md",
) -> dict[str, Any]:
    serialized = json.dumps(
        {
            "destination": {"application": application, "window": window},
        }
    )
    return {
        "sourceEventID": event_id,
        "kind": kind,
        "sessionID": "session",
        "beganAt": began_at,
        "availableAt": available_at,
        "serialized": serialized,
    }


def check_final_state_not_keystroke_concatenation(builder: ModuleType) -> None:
    initial = ""
    checkpoints = [
        "core idea involves solving",
        "core idea involves answering",
        "core idea involves answering the question",
    ]
    edit = builder.minimal_edit(initial, checkpoints[-1])
    assert edit["content"] == "core idea involves answering the question"
    assert "solving" not in edit["content"]


def check_unique_semantic_anchor(builder: ModuleType) -> None:
    # AX layout values contain newlines/zero-width scaffolding while the
    # separately queried semantic range presents the same text compactly.
    left = "a sufficiently long and unique semantic phrase before the caret"
    right = "a sufficiently long and unique semantic phrase after the caret"
    initial = f"prefix\n\u200b{left}\n\u200b\n\t{right}\nsuffix"
    offset = initial.index(right)
    while offset > 0 and (initial[offset - 1].isspace() or initial[offset - 1] == "\u200b"):
        offset -= 1
    final = initial[:offset] + "final authored thought" + initial[offset:]
    edit = builder.minimal_edit(initial, final)
    diagnostic = builder.semantic_anchor_diagnostic(
        initial,
        edit,
        {
            "leftContext": left,
            "selectedText": "",
            "rightContext": right,
        },
    )
    assert diagnostic["status"] == "proven", diagnostic

    duplicate = initial + initial
    duplicate_edit = builder.minimal_edit(
        duplicate, duplicate[:offset] + "x" + duplicate[offset:]
    )
    duplicate_diagnostic = builder.semantic_anchor_diagnostic(
        duplicate,
        duplicate_edit,
        {
            "leftContext": left,
            "selectedText": "",
            "rightContext": right,
        },
    )
    assert duplicate_diagnostic["status"] == "not_proven"
    assert duplicate_diagnostic["reason"] == "semantic_anchor_not_unique"

    wrong_place = len(initial)
    wrong_edit = builder.minimal_edit(initial, initial + "unrelated old-region edit")
    assert wrong_edit["characterOffset"] == wrong_place
    wrong_diagnostic = builder.semantic_anchor_diagnostic(
        initial,
        wrong_edit,
        {
            "leftContext": left,
            "selectedText": "",
            "rightContext": right,
        },
    )
    assert wrong_diagnostic["status"] == "not_proven"


def check_history_only_write_is_retained(builder: ModuleType) -> None:
    examples = [
        {"targetEventID": "write-1"},
        {"targetEventID": "write-3"},
    ]
    events = [
        event("write-1", "write", "2026-08-19T12:00:00Z", "2026-08-19T12:00:01Z"),
        event("write-2", "write", "2026-08-19T12:00:02Z", "2026-08-19T12:00:03Z"),
        event("write-3", "write", "2026-08-19T12:00:04Z", "2026-08-19T12:00:05Z"),
    ]
    neighborhood = builder.Neighborhood("fixture", 1, 2)
    selected = builder.neighborhood_write_events(neighborhood, examples, events)
    assert [value["sourceEventID"] for value in selected] == [
        "write-1",
        "write-2",
        "write-3",
    ]


def check_only_novel_read_is_causal_boundary(builder: ModuleType) -> None:
    events = [
        event("write-1", "write", "2026-08-19T12:00:00Z", "2026-08-19T12:00:01Z"),
        event("read-1", "read", "2026-08-19T12:00:02Z", "2026-08-19T12:00:03Z"),
        event("write-2", "write", "2026-08-19T12:00:04Z", "2026-08-19T12:00:05Z"),
    ]
    inside = builder.intervening_events(
        events,
        {"write-1", "write-2"},
        dt.datetime.fromisoformat("2026-08-19T12:00:00+00:00"),
        dt.datetime.fromisoformat("2026-08-19T12:00:05+00:00"),
    )
    assert [value["sourceEventID"] for value in inside] == ["read-1"]
    repeated = builder.read_novelty_evidence(
        inside,
        {"retainedHistory": [{"serialized": inside[0]["serialized"]}]},
    )
    assert not repeated["novelReads"]
    assert [value["sourceEventID"] for value in repeated["repeatedReads"]] == [
        "read-1"
    ]
    failures = builder.mechanical_gate_failures(
        continuous_replay=True,
        logical_identity_stable=True,
        novel_intervening_reads=repeated["novelReads"],
        overlapping_outside_writes=[],
    )
    assert failures == []

    novel = builder.read_novelty_evidence(
        inside,
        {"retainedHistory": [{"serialized": '{"kind":"read","content":"other"}'}]},
    )
    failures = builder.mechanical_gate_failures(
        continuous_replay=True,
        logical_identity_stable=True,
        novel_intervening_reads=novel["novelReads"],
        overlapping_outside_writes=[],
    )
    assert failures == ["novel_causally_available_read_inside_candidate"]


def check_reducer_selected_terminal_is_authoritative(builder: ModuleType) -> None:
    record = {
        "after": {
            "observationID": "after-empty",
            "observedAt": "2026-08-19T12:00:05Z",
            "value": "",
        },
        "returnCheckpoints": [
            {
                "checkpointID": "return-1",
                "observation": {
                    "observationID": "pre-return-authored",
                    "observedAt": "2026-08-19T12:00:04Z",
                    "value": "complete submitted prompt",
                },
            }
        ],
    }
    semantic = {
        "usedCheckpointID": "return-1",
        "reduction": {
            "selectedObservationID": "pre-return-authored",
            "selectedObservationSource": "pre_return_checkpoint",
        },
    }
    source, observation, checkpoint = builder.reducer_selected_observation(
        record, semantic
    )
    assert source == "pre_return_checkpoint"
    assert checkpoint == "return-1"
    assert observation["value"] == "complete submitted prompt"


def projected_observation(identifier: str, value: str) -> dict[str, Any]:
    return {
        "observationID": identifier,
        "value": value,
        "valueSHA256": identifier,
    }


def check_continuity_is_exact_gate(builder: ModuleType) -> None:
    continuous = [
        {
            "writeEventID": "one",
            "selectedTerminalLogicalValue": "same state",
            "selectedTerminalObservation": projected_observation("a", "same state"),
            "before": projected_observation("before-a", ""),
        },
        {
            "writeEventID": "two",
            "beforeLogicalValue": "same state",
            "selectedTerminalLogicalValue": "later state",
            "selectedTerminalObservation": projected_observation("b", "later state"),
            "before": projected_observation("before-b", "same state"),
        },
    ]
    assert builder.continuity_evidence(continuous)["continuousReplayableState"]
    continuous[1]["beforeLogicalValue"] = "different state"
    evidence = builder.continuity_evidence(continuous)
    assert not evidence["continuousReplayableState"]
    failures = builder.mechanical_gate_failures(
        continuous_replay=False,
        logical_identity_stable=True,
        novel_intervening_reads=[],
        overlapping_outside_writes=[{"sourceEventID": "other-write"}],
    )
    assert failures == [
        "discontinuous_editable_state",
        "outside_write_overlaps_candidate",
    ]


def check_deterministic_serialization(builder: ModuleType) -> None:
    assert builder.canonical_bytes({"b": 2, "a": 1}) == builder.canonical_bytes(
        {"a": 1, "b": 2}
    )


def check_development_selection(builder: ModuleType) -> None:
    project = Path(__file__).resolve().parent.parent
    path = project / "episode-review" / "phase1-episode-development-gold-v0.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    neighborhoods, loaded = builder.load_selection(path, manifest["corpusID"])
    assert loaded["selectionID"] == "phase1-episode-design-review-v1-20260819"
    assert len(neighborhoods) == 21
    assert len({value.label for value in neighborhoods}) == 21
    assert all(value.category and value.rationale for value in neighborhoods)
    categories = {value.category for value in neighborhoods}
    assert "submitted_single_write" in categories
    assert "within_composition_cursor_revision" in categories
    assert "causal_boundary_negative_control" in categories
    assert "independent_actions_negative_control" in categories
    assert "human_visible_model_missing_causal_boundary" in categories
    assert "causal_partition_design_case" in categories
    fragmented = next(
        value for value in neighborhoods if value.label == "chatgpt_fragmented_long"
    )
    assert fragmented.leading_write_event_ids == (
        "evt_2ee98457e931a7545fd2e5387e7f57f5365ff055131ca38ddab6363cac9e1a2d",
    )


def check_development_proposals(builder: ModuleType) -> None:
    project = Path(__file__).resolve().parent.parent
    selection_path = (
        project / "episode-review" / "phase1-episode-development-gold-v0.json"
    )
    proposal_path = (
        project
        / "episode-review"
        / "phase1-episode-development-gold-v0-proposals.json"
    )
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    labels = {value["label"] for value in selection["neighborhoods"]}
    loaded, by_label = builder.load_proposals(
        proposal_path,
        selection_id=selection["selectionID"],
        candidate_labels=labels,
    )
    assert (
        loaded["status"]
        == "assistant_episode_design_proposals_pending_human_adjudication"
    )
    assert set(by_label) == labels
    assert len(by_label) == 21
    assert any(
        value["decision"] == "merge_closed_episode"
        for value in proposal["proposals"]
    )
    assert any(
        value["decision"] == "split_into_independent_episodes"
        for value in proposal["proposals"]
    )
    assert any(
        value["decision"] == "defer_causal_ambiguity"
        for value in proposal["proposals"]
    )
    lookup = by_label["obsidian_fast_lookup_boundary"]
    assert lookup["visibilityAssessment"]["status"] == (
        "confirmed_human_visible_model_missing_information"
    )
    assert len(lookup["partitions"]) == 4
    post_lookup = by_label["obsidian_post_lookup_partition"]
    assert [
        (value["firstOneBasedExampleOrdinal"], value["lastOneBasedExampleOrdinal"])
        for value in post_lookup["partitions"]
    ] == [(123, 123), (124, 125)]
    assert by_label["obsidian_gtm_composition"]["decision"] == (
        "merge_closed_episode"
    )
    assert by_label["chatgpt_fragmented_long"]["decision"] == (
        "merge_closed_episode"
    )


def check_structured_paste_proposal(builder: ModuleType) -> None:
    paste = {
        "type": "paste",
        "clipboardSnapshotID": "clipboard",
        "pasteCheckpointID": "checkpoint",
    }
    candidate = {
        "candidateID": "candidate",
        "label": "mixed",
        "memberWriteEventIDs": ["one", "two"],
        "members": [
            {
                "currentTarget": {
                    "schemaVersion": 1,
                    "resolvedContent": "before payload",
                    "segments": [
                        {"type": "authored_text", "content": "before "},
                        paste,
                    ],
                }
            },
            {"currentTarget": None},
        ],
        "finalObservation": {"value": "before payload after"},
        "singleCompletionDiagnostic": {},
    }
    resolved = builder.resolve_proposal(
        candidate,
        {
            "decision": "merge_closed_episode",
            "targetPolicy": "custom_structured_target",
            "proposedMarkerTarget": "before <|paste|> after",
            "closureAssessment": "submitted",
            "representableAsSingleCompletion": True,
            "notes": "fixture",
        },
    )
    target = resolved["finalizedTarget"]
    assert target["resolvedContent"] == "before payload after"
    assert builder.marker_target(target) == "before <|paste|> after"
    assert target["segments"][1] == paste


def check_model_facing_projection(builder: ModuleType) -> None:
    query = json.dumps({"kind": "write_conditioning_state", "cursor": "here"})
    event = json.dumps({"kind": "read", "content": "visible evidence"})
    instruction = "Predict the completion."
    semantic = instruction + "\n" + event + "\n" + query
    example = {"exampleID": "example", "targetEventID": "target", "query": query}
    plan = {
        "exampleID": "example",
        "taskInstruction": instruction,
        "rightEdgeQuerySHA256": builder.hashlib.sha256(query.encode()).hexdigest(),
        "semanticModelInputSHA256": builder.hashlib.sha256(semantic.encode()).hexdigest(),
        "retainedContextBlocks": [
            {"contextBlockID": "read", "contentTruncated": False}
        ],
    }
    packed = {
        "exampleID": "example",
        "modelInputTokenCount": 3,
        "targetTokenCount": 1,
        "inputIDs": [1, 2, 3, 4],
        "modelInputTokenCountBeforePacking": 3,
        "sourceContextEventCount": 1,
        "droppedContextEventCount": 0,
        "partiallyRetainedContextEventCount": 0,
        "unusedModelInputTokenBudget": 5,
    }
    projection = builder.model_facing_projection(
        example,
        plan,
        packed,
        {"read": {"serialized": event, "availableAt": "2026-08-19T12:00:00Z"}},
    )
    assert projection["exactSemanticModelInput"] == semantic
    assert projection["retainedHistory"][0]["projection"]["content"] == (
        "visible evidence"
    )
    assert projection["focusTimeObservationAvailable"] is False


def check_partition_resolution(builder: ModuleType) -> None:
    candidate = {
        "candidateID": "partition",
        "label": "partition",
        "memberWriteEventIDs": ["123", "124", "125"],
        "members": [
            {
                "oneBasedExampleOrdinal": ordinal,
                "exampleID": f"example-{ordinal}",
                "writeEventID": str(ordinal),
                "beganAt": f"2026-08-19T12:00:{ordinal - 120:02d}Z",
                "availableAt": f"2026-08-19T12:00:{ordinal - 119:02d}Z",
                "beforeLogicalValue": "" if ordinal == 123 else str(ordinal - 1),
                "selectedTerminalLogicalValue": str(ordinal),
                "before": {
                    "observationID": f"before-{ordinal}",
                    "valueSHA256": f"before-hash-{ordinal}",
                },
                "selectedTerminalObservation": {
                    "observationID": f"after-{ordinal}",
                    "valueSHA256": f"after-hash-{ordinal}",
                },
                "targetIdentity": {
                    "bundleIdentifier": "md.obsidian",
                    "windowTitle": "note",
                    "role": "AXTextArea",
                },
                "currentTarget": builder.authored_target(str(ordinal)),
            }
            for ordinal in (123, 124, 125)
        ],
        "oneBasedExampleRange": {"first": 123, "last": 125},
        "finalObservation": {"value": "unused"},
        "causalEvidence": {"interveningEvents": []},
        "singleCompletionDiagnostic": {},
    }
    proposal = {
        "decision": "partition_candidate",
        "targetPolicy": "none_for_combined_candidate",
        "closureAssessment": "fixture",
        "representableAsSingleCompletion": False,
        "notes": "fixture",
        "partitions": [
            {
                "firstOneBasedExampleOrdinal": 123,
                "lastOneBasedExampleOrdinal": 123,
                "decision": "history_only",
                "targetPolicy": "none_missing_information",
                "representableAsSingleCompletion": False,
                "notes": "missing",
            },
            {
                "firstOneBasedExampleOrdinal": 124,
                "lastOneBasedExampleOrdinal": 125,
                "decision": "merge_closed_episode",
                "targetPolicy": "custom_authored_target",
                "proposedMarkerTarget": "124 perhaps not",
                "representableAsSingleCompletion": True,
                "notes": "merge",
            },
        ],
    }
    resolved = builder.resolve_proposal(candidate, proposal)
    assert resolved["finalizedTarget"] is None
    assert len(resolved["partitions"]) == 2
    assert resolved["partitions"][0]["finalizedTarget"] is None
    assert (
        resolved["partitions"][1]["finalizedTarget"]["resolvedContent"]
        == "124 perhaps not"
    )
    assert resolved["partitions"][1]["partitionEvidence"] == {
        "memberWriteEventIDs": ["124", "125"],
        "continuousReplayableState": True,
        "exactLogicalEditableIdentity": True,
        "interveningReadCount": 0,
    }


def main() -> int:
    builder = load_builder()
    check_final_state_not_keystroke_concatenation(builder)
    check_unique_semantic_anchor(builder)
    check_history_only_write_is_retained(builder)
    check_only_novel_read_is_causal_boundary(builder)
    check_reducer_selected_terminal_is_authoritative(builder)
    check_continuity_is_exact_gate(builder)
    check_deterministic_serialization(builder)
    check_development_selection(builder)
    check_development_proposals(builder)
    check_structured_paste_proposal(builder)
    check_model_facing_projection(builder)
    check_partition_resolution(builder)
    print("Phase 1 episode-review checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

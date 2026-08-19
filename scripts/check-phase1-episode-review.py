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


def check_read_is_hard_causal_boundary(builder: ModuleType) -> None:
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
    failures = builder.mechanical_gate_failures(
        continuous_replay=True,
        exact_identity_stable=True,
        intervening_reads=inside,
        overlapping_outside_writes=[],
    )
    assert failures == ["causally_available_read_inside_candidate"]


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
        exact_identity_stable=True,
        intervening_reads=[],
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
    assert loaded["selectionID"] == "phase1-episode-development-gold-v0-20260819"
    assert len(neighborhoods) == 20
    assert len({value.label for value in neighborhoods}) == 20
    assert all(value.category and value.rationale for value in neighborhoods)
    categories = {value.category for value in neighborhoods}
    assert "submitted_single_write" in categories
    assert "within_composition_cursor_revision" in categories
    assert "causal_boundary_negative_control" in categories
    assert "independent_actions_negative_control" in categories


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
    assert loaded["status"] == "assistant_proposals_pending_human_adjudication"
    assert set(by_label) == labels
    assert len(by_label) == 20
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


def main() -> int:
    builder = load_builder()
    check_final_state_not_keystroke_concatenation(builder)
    check_unique_semantic_anchor(builder)
    check_history_only_write_is_retained(builder)
    check_read_is_hard_causal_boundary(builder)
    check_reducer_selected_terminal_is_authoritative(builder)
    check_continuity_is_exact_gate(builder)
    check_deterministic_serialization(builder)
    check_development_selection(builder)
    check_development_proposals(builder)
    check_structured_paste_proposal(builder)
    print("Phase 1 episode-review checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

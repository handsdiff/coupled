#!/usr/bin/env python3
"""Small deterministic checks for the raw-authoritative episode reducer."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location("phase1_raw_episode_check", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    project = Path(__file__).resolve().parent.parent
    reducer_path = project / "scripts/construct-phase1-raw-episode-corpus.py"
    reducer = load_module(reducer_path)

    assert reducer.minimal_edit("hello world", "hello brave world") == {
        "operation": "insert",
        "characterOffset": 6,
        "utf16Offset": 6,
        "removedContent": "",
        "content": "brave ",
    }
    assert reducer.normalize_authored_content(
        "first\n\u200b\n-\n\u200b \nsecond"
    ) == "first\n\nsecond"

    # Prefix-greedy minimal diffing rotates an insertion when the inserted
    # text and unchanged suffix share a boundary character: inserting
    # ``live `` before ``latest`` appears canonically as ``ive l``. The
    # upstream reducer/compiler has already grounded the human alignment in
    # ordered raw checkpoints. A singleton closed episode must preserve that
    # equal-size, exactly reconstructing completion.
    boundary_before = "update after reviewing the latest trace"
    boundary_after = "update after reviewing the live latest trace"
    boundary_member = {
        "writeEventID": "range-aligned-boundary",
        "operation": "insert",
        "characterOffset": 27,
        "removedContent": "",
        "currentTarget": {
            "resolvedContent": "live ",
            "segments": [{"type": "authored_text", "content": "live "}],
        },
        "beforeLogicalValue": boundary_before,
        "selectedTerminalLogicalValue": boundary_after,
        "conditioningState": {"cursorContext": {"selectedText": ""}},
        "inputHints": ["typed"],
    }
    boundary_episode = reducer.OpenEpisode(
        [{"members": [boundary_member]}], "session_start"
    )
    target, reason, audit = reducer.structured_target(
        boundary_episode, boundary_before, boundary_after, {}
    )
    assert reducer.minimal_edit(boundary_before, boundary_after)["content"] == "ive l"
    assert reason == "complete_initial_to_terminal_minimal_diff"
    assert target["resolvedContent"] == "live "
    assert audit["alignmentSource"] == "compiler_verified_single_member_alignment"

    # A pointer relocation may let one character materialize between the
    # previous terminal observation and the next synchronous BEFORE. When the
    # exact same AX element, timing, and affected region prove that this is an
    # internal revision, preserve one finalized composition rather than
    # supervising the suffix as an independent thought.
    base = "older note\n"
    provisional = (
        "training costs are amortized over model use, but this blocks people "
        "who cannot pay for inference"
    )
    revised_before = (
        "training costs are amortized over model use, but this blocks people "
        "who a"
    )
    finalized = (
        "training costs are amortized over model use, but this blocks people "
        "who aren't using the model enough for amortization"
    )
    identity = {
        "bundleIdentifier": "md.obsidian",
        "processIdentifier": 1,
        "elementHash": 77,
        "role": "AXTextArea",
        "windowTitle": "GTM - Notes - Obsidian",
        "fieldDescription": "",
        "fieldLabel": "",
    }
    revision_left = {
        "writeEventID": "revision-left",
        "beganAt": "2026-08-18T17:25:00.000Z",
        "availableAt": "2026-08-18T17:26:00.000Z",
        "boundaryReason": "pointer_selection_boundary",
        "inputHints": ["typed", "delete"],
        "beforeLogicalValue": base,
        "selectedTerminalLogicalValue": base + provisional,
        "targetIdentity": identity,
        "conditioningState": {"cursorContext": {"selectedText": ""}},
    }
    revision_right = {
        "writeEventID": "revision-right",
        "beganAt": "2026-08-18T17:26:01.500Z",
        "availableAt": "2026-08-18T17:26:20.000Z",
        "boundaryReason": "write_delay_elapsed",
        "inputHints": ["typed", "delete"],
        "beforeLogicalValue": base + revised_before,
        "selectedTerminalLogicalValue": base + finalized,
        "targetIdentity": identity,
        "conditioningState": {"cursorContext": {"selectedText": ""}},
    }
    revision_episode = reducer.OpenEpisode(
        [{"members": [revision_left]}], "novel_read"
    )
    proven, evidence = reducer.same_element_internal_revision_continuation(
        revision_episode, {"members": [revision_right]}
    )
    assert proven is True
    assert evidence["reason"] == "proven_same_element_internal_revision"
    combined_episode = reducer.OpenEpisode(
        [{"members": [revision_left]}, {"members": [revision_right]}],
        "novel_read",
        boundary_evidence=[{
            "between": ["revision-left", "revision-right"],
            "decision": "continue",
            "sameElementInternalRevision": True,
        }],
    )
    target, reason, _ = reducer.structured_target(
        combined_episode, base, base + finalized, {}
    )
    assert reason == "complete_initial_to_terminal_minimal_diff"
    assert target["resolvedContent"] == finalized

    revision_right["targetIdentity"] = identity | {"elementHash": 78}
    proven, _ = reducer.same_element_internal_revision_continuation(
        revision_episode, {"members": [revision_right]}
    )
    assert proven is False
    revision_right["targetIdentity"] = identity
    revision_right["beganAt"] = "2026-08-18T17:26:03.001Z"
    proven, _ = reducer.same_element_internal_revision_continuation(
        revision_episode, {"members": [revision_right]}
    )
    assert proven is False

    record_id = "raw-paste"
    raw_records = {
        record_id: {
            "recordID": record_id,
            "inputHints": ["typed", "paste"],
            "pasteCheckpoints": [{
                "checkpointID": "paste-1",
                "clipboardSnapshotID": "clipboard-1",
                "clipboardChangeCount": 7,
                "clipboardText": "copied",
                "clipboardTextWasTruncated": False,
                "prePasteAXErrors": [],
                "axErrors": [],
                "prePasteObservation": {
                    "value": "before ", "valueWasTruncated": False,
                },
                "observation": {
                    "value": "before copied", "valueWasTruncated": False,
                },
            }],
        }
    }
    member = {
        "writeEventID": "write-1",
        "sourceRecordIDs": [record_id],
        "inputHints": ["typed", "paste"],
        "beforeLogicalValue": "",
        "selectedTerminalLogicalValue": "before copied after",
        "conditioningState": {"cursorContext": {"selectedText": ""}},
    }
    episode = reducer.OpenEpisode(
        [{"members": [member]}], "session_start"
    )
    target, reason, audit = reducer.structured_target(
        episode, "", "before copied after", raw_records
    )
    assert reason == "complete_diff_with_grounded_paste_segments"
    assert audit["completionSource"] == "initial_to_terminal_field_diff"
    assert target["resolvedContent"] == "before copied after"
    assert [segment["type"] for segment in target["segments"]] == [
        "authored_text", "paste", "authored_text",
    ]
    assert target["segments"][1]["historyContent"] == "copied"

    # The same text may have been authored earlier in the field. The
    # synchronous paste checkpoint's field offset—not global string
    # uniqueness—must identify the pasted occurrence.
    raw_records[record_id]["pasteCheckpoints"][0]["prePasteObservation"][
        "value"
    ] = "copied was authored; pasted: "
    raw_records[record_id]["pasteCheckpoints"][0]["observation"][
        "value"
    ] = "copied was authored; pasted: copied"
    member["selectedTerminalLogicalValue"] = (
        "copied was authored; pasted: copied after"
    )
    target, reason, audit = reducer.structured_target(
        episode,
        "",
        "copied was authored; pasted: copied after",
        raw_records,
    )
    assert reason == "complete_diff_with_grounded_paste_segments"
    assert audit["pastePlacementRules"] == ["raw_checkpoint_field_offset"]
    assert [segment["type"] for segment in target["segments"]] == [
        "authored_text", "paste", "authored_text",
    ]
    assert target["segments"][0]["content"] == "copied was authored; pasted: "
    assert target["segments"][1]["historyContent"] == "copied"
    assert target["segments"][2]["content"] == " after"

    canceled_id = "raw-canceled-paste"
    canceled_before = "existing "
    canceled_after = "existing authored completion"
    canceled_raw = {
        "recordID": canceled_id,
        "inputHints": ["paste", "undo_redo", "typed"],
        "inputEvents": [
            {"eventTimestampNanoseconds": 100, "hint": "paste"},
            {"eventTimestampNanoseconds": 200, "hint": "undo_redo"},
            {"eventTimestampNanoseconds": 300, "hint": "typed"},
        ],
        "pasteCheckpoints": [{
            "checkpointID": "canceled-paste-1",
            "eventTimestampNanoseconds": 100,
            "clipboardSnapshotID": "canceled-clipboard",
            "clipboardChangeCount": 9,
            "clipboardText": "wrong payload",
            "clipboardTextWasTruncated": False,
            "prePasteAXErrors": [],
            "axErrors": [],
            "prePasteObservation": {
                "value": canceled_before,
                "valueWasTruncated": False,
                "selectedRangeLocation": 9,
                "selectedRangeLength": 0,
            },
            "observation": {
                "value": "existing wrong payload",
                "valueWasTruncated": False,
            },
        }],
        "mutationCheckpoints": [
            {
                "checkpointID": "undo-checkpoint",
                "eventTimestampNanoseconds": 200,
                "axErrors": [],
                "observation": {
                    "observationID": "undo-observation",
                    "value": canceled_before,
                    "valueWasTruncated": False,
                    "selectedRangeLocation": 9,
                    "selectedRangeLength": 0,
                },
            },
            {
                "checkpointID": "typed-checkpoint",
                "eventTimestampNanoseconds": 300,
                "axErrors": [],
                "observation": {
                    "value": canceled_after,
                    "valueWasTruncated": False,
                },
            },
        ],
    }
    canceled_member = {
        "writeEventID": "canceled-write",
        "sourceRecordIDs": [canceled_id],
        "inputHints": ["paste", "undo_redo", "typed"],
        "beforeLogicalValue": canceled_before,
        "selectedTerminalLogicalValue": canceled_after,
        "conditioningState": {"cursorContext": {"selectedText": ""}},
    }
    canceled_episode = reducer.OpenEpisode(
        [{"members": [canceled_member]}], "session_start"
    )
    target, reason, audit = reducer.structured_target(
        canceled_episode,
        canceled_before,
        canceled_after,
        {canceled_id: canceled_raw},
    )
    assert reason == "complete_diff_after_exactly_canceled_paste"
    assert audit["canceledPasteEvidenceCount"] == 1
    assert target["resolvedContent"] == "authored completion"
    assert target["segments"] == [
        {"type": "authored_text", "content": "authored completion"}
    ]

    raw_records[record_id]["inputHints"] = ["paste"]
    raw_records[record_id]["pasteCheckpoints"][0]["clipboardText"] = "copied"
    raw_records[record_id]["pasteCheckpoints"][0]["prePasteObservation"]["value"] = "old"
    raw_records[record_id]["pasteCheckpoints"][0]["observation"]["value"] = "old•copied"
    member["inputHints"] = ["paste"]
    member["selectedTerminalLogicalValue"] = "unrelated AX epoch"
    target, _, audit = reducer.structured_target(
        episode, "old document", "unrelated AX epoch", raw_records
    )
    assert audit["completionSource"] == "raw_local_paste_transitions"
    assert target["resolvedContent"] == "•copied"
    assert [segment["type"] for segment in target["segments"]] == ["paste"]

    left = {
        "writeEventID": "epoch-left",
        "sourceRecordIDs": ["epoch-left-raw"],
        "inputHints": ["typed"],
        "boundaryReason": "write_delay_elapsed",
        "beforeLogicalValue": "",
        "selectedTerminalLogicalValue": 'prefix "merge_cl',
        "targetIdentity": {
            "bundleIdentifier": "com.microsoft.VSCode",
            "windowTitle": "checkpoint.md",
            "role": "AXTextField",
            "fieldDescription": "Terminal 1, ⠏ coupled Use help",
            "fieldLabel": "",
        },
        "conditioningState": {"cursorContext": {"selectedText": ""}},
    }
    right = {
        "writeEventID": "epoch-right",
        "sourceRecordIDs": ["epoch-right-raw"],
        "inputHints": ["typed", "paste", "delete"],
        "boundaryReason": "write_delay_elapsed",
        "beforeLogicalValue": "",
        "selectedTerminalLogicalValue": '"',
        "targetIdentity": {
            **left["targetIdentity"],
            "fieldDescription": "Terminal 1, ⠸ coupled Use help",
        },
        "conditioningState": {
            "cursorContext": {"selectedText": ""},
            "clipboard": {
                "snapshotID": "opaque-clipboard",
                "changeCount": 12,
            },
        },
    }
    raw_records.update({
        "epoch-left-raw": {"recordID": "epoch-left-raw"},
        "epoch-right-raw": {
            "recordID": "epoch-right-raw",
            "inputHints": ["typed", "paste", "delete"],
            "conditioningState": right["conditioningState"],
            "pasteCheckpoints": [{
                "checkpointID": "opaque-paste",
                "clipboardSnapshotID": "opaque-clipboard",
                "clipboardChangeCount": 12,
                "clipboardText": "copied payload",
                "clipboardTextWasTruncated": False,
                "prePasteAXErrors": [],
                "axErrors": [],
                "prePasteObservation": {
                    "value": 'osed_episode". note "',
                    "valueWasTruncated": False,
                },
                "observation": {"value": "", "valueWasTruncated": False},
            }],
        },
    })
    assert reducer.stable_destination(left) == reducer.stable_destination(right)
    assert reducer.prompt_epoch_reset_compatible(left, right)
    epoch_episode = reducer.OpenEpisode(
        [{"members": [left]}, {"members": [right]}], "session_start"
    )
    target, reason, audit = reducer.stitched_epoch_target(
        epoch_episode, raw_records
    )
    assert reason == "raw_prompt_ax_epochs_stitched"
    assert audit["epochCount"] == 2
    assert target["resolvedContent"] == (
        'prefix "merge_closed_episode". note "copied payload"'
    )
    assert [row["type"] for row in target["segments"]] == [
        "authored_text", "paste", "authored_text",
    ]
    assert target["segments"][1]["historyContent"] == "copied payload"
    single_target, single_reason, single_audit = reducer.stitched_epoch_target(
        reducer.OpenEpisode([{"members": [right]}], "session_start"),
        raw_records,
    )
    assert single_reason == "grounded_opaque_paste_epoch_transcript"
    assert single_audit["completionSource"] == (
        "locally_verified_ax_epoch_transcript"
    )
    assert single_target["resolvedContent"] == (
        'osed_episode". note "copied payload"'
    )

    fixture = json.loads(
        (project / "episode-review/phase1-episode-regressions-v4.json").read_text()
    )
    production = reducer_path.read_text()
    forbidden = {
        value
        for row in fixture["neighborhoods"]
        for value in [row["label"], *row["writeEventIDs"]]
    }
    assert not [value for value in forbidden if value in production]

    print(json.dumps({
        "status": "passed",
        "checks": 11,
        "productionOracleIdentities": 0,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

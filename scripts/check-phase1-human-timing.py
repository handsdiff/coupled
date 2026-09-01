#!/usr/bin/env python3
"""No-network regression checks for reconstructed human timing."""

from __future__ import annotations

from datetime import datetime, timezone

from phase1_human_timing import (
    Example,
    Gate,
    SemanticEvent,
    real_time_suggestion_pass,
    reconstruct_record,
)


def at(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def raw_record(*events: tuple[float, str, bool]) -> dict:
    return {
        "inputEvents": [
            {
                "observedAt": at(when).isoformat(),
                "hint": hint,
                "mutationCapable": mutating,
            }
            for when, hint, mutating in events
        ]
    }


def example(*, began: float = 10, context: tuple[str, ...] = ("read",)) -> Example:
    return Example(
        ordinal=51,
        example_id="example-51",
        session_id="session",
        target_episode_id="episode",
        began_at=at(began),
        available_at=at(20),
        source_record_ids=("target",),
        context_event_ids=context,
        target_text="hello world",
    )


def event(
    event_id: str,
    kind: str,
    record: str,
    *,
    ready: float,
    closure: str | None = None,
) -> SemanticEvent:
    return SemanticEvent(
        event_id,
        None,
        kind,
        "session",
        None,
        at(ready),
        (record,),
        closure,
    )


def read_record(last_activity: float, observed: float) -> dict:
    return {
        "recordID": "read-record",
        "recordType": "screen_ocr_observation",
        "lastActivityAt": at(last_activity).isoformat(),
        "observedAt": at(observed).isoformat(),
    }


def main() -> None:
    raw = {
        "target": {
            "recordID": "target",
            **raw_record(
                (10, "typed", True),
                (15, "typed", True),
                (16, "return", False),
            ),
        },
        "read-record": read_record(2, 8),
        # This raw motor observation is intentionally not a semantic event.
        "move": {
            "recordID": "move",
            "recordType": "read_candidate_suppression",
            "lastActivityAt": at(9).isoformat(),
        },
    }
    semantic = {"read": event("read", "read", "read-record", ready=5)}
    row = reconstruct_record(
        example(), semantic, raw, {}, 3, Gate()
    )
    assert row["humanStartAt"].endswith("02.000Z")
    assert row["modelSampleAt"].endswith("05.000Z")
    assert row["modelStartAt"].endswith("08.000Z")
    assert row["humanFinishedAt"].endswith("15.000Z")
    assert row["humanWorkflowCompletedAt"].endswith("16.000Z")
    assert row["writeAvailableAt"].endswith("20.000Z")
    assert row["humanTimeSeconds"] == 13.0
    assert row["modelStartToHumanFinishSeconds"] == 7.0
    assert row["sampleRelationToWriting"] == "before_writing"
    assert row["writeSettlementLagSeconds"] == 5.0

    # A non-semantic focus or pointer observation at t=9 does not replace the
    # material READ anchor at t=2.
    row = reconstruct_record(
        example(), semantic, raw, {}, 3, Gate()
    )
    assert row["humanStartAt"].endswith("02.000Z")

    # A genuinely new canonical READ replaces the earlier material anchor even
    # when the target starts before the three-second sample delay elapses.
    newer_raw = {
        **raw,
        "new-read": {
            "recordID": "new-read",
            "recordType": "screen_ocr_observation",
            "lastActivityAt": at(9).isoformat(),
            "observedAt": at(11).isoformat(),
        },
    }
    newer_events = {
        **semantic,
        "new-read": event("new-read", "read", "new-read", ready=10),
    }
    row = reconstruct_record(
        example(context=("read", "new-read")), newer_events, newer_raw, {}, 3, Gate()
    )
    assert row["humanStartAt"].endswith("09.000Z")
    assert row["modelSampleAt"].endswith("12.000Z")
    assert row["sampleRelationToWriting"] == "during_writing"
    assert row["timingReconstructable"] is True

    # A submitted prior WRITE can anchor the next thought at submission rather
    # than at its earlier final character.
    prior_raw = {
        **raw,
        "prior-write": {
            "recordID": "prior-write",
            **raw_record((2, "typed", True), (3, "return", False)),
        },
    }
    prior_events = {
        "prior": event(
            "prior",
            "write",
            "prior-write",
            ready=4,
            closure="closed_submission",
        )
    }
    row = reconstruct_record(
        example(context=("prior",)), prior_events, prior_raw, {}, 3, Gate()
    )
    assert row["humanStartAt"].endswith("03.000Z")
    assert row["humanStartKind"] == "write"

    unknown = reconstruct_record(
        example(context=()), {}, raw, {}, 3, Gate()
    )
    assert unknown["disposition"] == "timing_unknown_no_material_anchor"
    assert unknown["timingReconstructable"] is False

    eligible = {
        "timingReconstructable": True,
        "realTimeUtilityEligible": True,
        "modelStartToHumanFinishSeconds": 10.0,
        "estimatedSuggestionInteractionSeconds": 2.0,
        "minimumNetSavingsSeconds": 1.0,
    }
    assert real_time_suggestion_pass(
        eligible, 3.0, semantic_pass=True, substantive=True
    )
    assert not real_time_suggestion_pass(
        eligible, 8.0, semantic_pass=True, substantive=True
    )
    # Typing onset is deliberately absent from the real-time formula. A model
    # may finish after typing begins and still save time before completion.
    assert real_time_suggestion_pass(
        eligible, 3.0, semantic_pass=True, substantive=True
    )
    assert not real_time_suggestion_pass(
        eligible, 3.0, semantic_pass=False, substantive=True
    )
    print("phase1 human timing checks passed")


if __name__ == "__main__":
    main()

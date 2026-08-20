#!/usr/bin/env python3
"""Audit a Phase 1 closed-episode corpus and its raw semantic lineage."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected object")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def words(value: str) -> list[str]:
    return re.findall(r"[\w’']+", value, flags=re.UNICODE)


def event_destination(event: dict[str, Any]) -> tuple[Any, Any]:
    payload = json.loads(event.get("serialized", "{}"))
    destination = payload.get("destination") or {}
    return destination.get("application"), destination.get("window")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--expect-target-count", type=int)
    parser.add_argument("--expect-target-substring", action="append", default=[])
    parser.add_argument("--reject-target-prefix", action="append", default=[])
    args = parser.parse_args()

    root = args.corpus.resolve()
    manifest = load_json(root / "corpus.json")
    if manifest.get("artifactType") not in {
        "phase1_episode_corpus", "phase1_raw_authoritative_episode_corpus",
    }:
        raise ValueError("not an episode corpus")
    episode_version = manifest.get("episodeVersion")
    conversion_version = manifest.get("conversionVersion")
    raw_authoritative = (
        manifest.get("artifactType") == "phase1_raw_authoritative_episode_corpus"
    )
    if raw_authoritative:
        if episode_version not in {
            "phase1-raw-episode-v1", "phase1-raw-episode-v2",
            "phase1-raw-episode-v3",
            "phase1-raw-episode-v4",
            "phase1-raw-episode-v5",
            "phase1-raw-episode-v6",
        }:
            raise ValueError("audit requires a supported raw episode version")
        if conversion_version not in {
            "phase1-raw-episode-causal-v1", "phase1-raw-episode-causal-v2",
            "phase1-raw-episode-causal-v3",
            "phase1-raw-episode-causal-v4",
            "phase1-raw-episode-causal-v5",
            "phase1-raw-episode-causal-v6",
        }:
            raise ValueError("audit requires a supported raw causal version")
        architecture = manifest.get("rawEpisodeArchitecture") or {}
        assert architecture.get("sourceAuthority") == "immutable_raw_journals"
        assert architecture.get("productionConsumesRegressionFixture") is False
    elif (
        episode_version not in {"phase1-episode-v4", "phase1-episode-v5"}
        or conversion_version not in {
            "phase1-episode-causal-v4", "phase1-episode-causal-v5"
        }
    ):
        raise ValueError("audit requires a supported Phase 1 episode corpus")
    objective = manifest.get("objective") or {}
    assert objective.get("predictionUnit") == "closed_composition_episode"
    assert objective.get("microWritesReceiveLoss") is False
    assert objective.get("microWritesAppearInModelHistory") is False
    assert objective.get("modelFacingWritesAreClosedEpisodes") is True
    eligibility = manifest.get("eligibility") or {}
    assert eligibility.get("minimumTrimmedAuthoredCharacters") == 40
    assert eligibility.get("minimumAuthoredWords") == 6
    assert eligibility.get("minimumSubmittedAuthoredCharacters") == 4
    assert eligibility.get("automaticSubmittedShortMinimumEnabled") is raw_authoritative
    assert eligibility.get("reviewedConciseSubmissionsMayOverrideGeneralMinimum") is not raw_authoritative
    assert eligibility.get("reviewedRegressionDecisionsOverrideLengthHeuristics") is not raw_authoritative
    assert eligibility.get("groundedPasteActionBypassesMinimumAuthoredContent") is False
    assert eligibility.get("closedButBelowThresholdRemainsHistoryOnly") is True
    assert eligibility.get("resolvedTransitionsWithoutTargetEligibilityRemainHistoryOnly") is True
    assert eligibility.get("unresolvedOrUnclosedMicroWritesAppearInModelHistory") is not raw_authoritative

    for name, expected in manifest.get("artifactDigestsSHA256", {}).items():
        actual = digest(root / name)
        if actual != expected:
            raise ValueError(f"artifact digest mismatch for {name}: {actual} != {expected}")

    source = Path(manifest["source"]["path"])
    for name, expected in manifest["source"]["digestsSHA256"].items():
        actual = digest(source / name)
        if actual != expected:
            raise ValueError(f"source digest mismatch for {name}: {actual} != {expected}")

    source_events = load_jsonl(source / "events.jsonl")
    source_event_by_id = {row["sourceEventID"]: row for row in source_events}
    source_write_ids = {
        row["sourceEventID"] for row in source_events if row.get("kind") == "write"
    }
    events = load_jsonl(root / "events.jsonl")
    blocks = load_jsonl(root / "context-blocks.jsonl")
    examples = load_jsonl(root / "examples.jsonl")
    adjudications = load_jsonl(root / "episode-adjudications.jsonl")
    adjudication_by_candidate = {
        row["candidateID"]: row for row in adjudications
    }
    if raw_authoritative and episode_version in {
        "phase1-raw-episode-v2", "phase1-raw-episode-v3",
        "phase1-raw-episode-v4",
        "phase1-raw-episode-v5",
        "phase1-raw-episode-v6",
    }:
        raw_candidates = load_jsonl(root / "raw-episode-candidates.jsonl")
        for candidate in raw_candidates:
            adjudication = adjudication_by_candidate[candidate["candidateID"]]
            state_machine = candidate.get("episodeStateMachine") or {}
            closure_evidence = candidate.get("closureEvidence") or {}
            if (
                state_machine.get("closeReason") == "session_end"
                and closure_evidence.get("objectiveSubmissionBoundary") is not True
            ):
                assert adjudication.get("decision") != "closed_loss_episode"
                assert adjudication.get("closureStatus") == "open_or_abandoned"
            if adjudication.get("closureReason") == "novel_causal_read_partition":
                boundary = (state_machine.get("boundaryEvidence") or [])[-1]
                assert boundary.get("reason") == "novel_read"
                assert boundary.get("sameLogicalDestination") is True
                assert boundary.get("stateContinuous") is True
    event_by_id = {row["sourceEventID"]: row for row in events}
    block_by_id = {row["contextBlockID"]: row for row in blocks}
    assert len(event_by_id) == len(events)
    assert len(block_by_id) == len(blocks)

    covered: set[str] = set()
    for row in adjudications:
        members = row["memberWriteEventIDs"]
        assert members and not covered.intersection(members)
        covered.update(members)
    assert covered == source_write_ids

    closed_members: set[str] = set()
    write_events = [row for row in events if row.get("kind") == "write"]
    for event in write_events:
        assert event.get("episodeID")
        members = event.get("memberWriteEventIDs")
        assert isinstance(members, list) and members
        assert not closed_members.intersection(members)
        closed_members.update(members)
        assert set(members).issubset(source_write_ids)
        compact = json.loads(event["serialized"])
        audit = json.loads(event["auditSerialized"])
        assert compact["operation"] == "closed_composition_episode"
        assert audit["episodeVersion"] == episode_version
        assert audit["memberWriteEventIDs"] == members
        assert audit["resolvedCompletion"] == "".join(
            segment["content"] for segment in compact["authorshipSegments"]
        )
        for segment in compact["authorshipSegments"]:
            if segment.get("type") == "paste":
                assert isinstance(segment.get("content"), str) and segment["content"]
            else:
                assert segment.get("type") in {
                    "authored_text", "unresolved_paste_transition",
                    "unresolved_authorship",
                }

    target_texts: list[str] = []
    loss_members: set[str] = set()
    for ordinal, example in enumerate(examples):
        assert example["chronologicalOrdinal"] == ordinal
        assert example["conversionVersion"] == conversion_version
        assert example["targetUnitType"] == "closed_composition_episode"
        target_event = event_by_id[example["targetEventID"]]
        assert target_event["kind"] == "write"
        assert example["episode"]["memberWriteEventIDs"] == target_event["memberWriteEventIDs"]
        assert not loss_members.intersection(target_event["memberWriteEventIDs"])
        loss_members.update(target_event["memberWriteEventIDs"])

        context_ids = example["contextEventIDs"]
        assert not set(context_ids).intersection(source_write_ids)
        assert example["targetEventID"] not in context_ids
        assert all(value in event_by_id for value in context_ids)
        began = instant(example["targetBeganAt"])
        assert all(instant(event_by_id[value]["availableAt"]) < began for value in context_ids)
        expected_context = "\n".join(
            block_by_id[value]["serialized"] for value in example["contextBlockIDs"]
        )
        assert example["context"] == expected_context
        assert example["modelInput"] == (
            example["query"] if not expected_context
            else expected_context + "\n" + example["query"]
        )
        for value in context_ids:
            if event_by_id[value]["kind"] == "write":
                assert event_by_id[value].get("episodeID")

        target = example["target"]
        onset = example["episode"].get("onsetEvidence") or {}
        if onset.get("requiresProvenPromptOnset"):
            assert onset.get("promptOnsetProven") is True
        if onset.get("requiresProvenPromptOnset") and not raw_authoritative:
            cursor = example["conditioningState"].get("cursorContext") or {}
            visible = "".join(
                str(cursor.get(key) or "")
                for key in ("leftContext", "selectedText", "rightContext")
            )
            if onset.get("emptyOrUnpopulatedPromptProven"):
                normalized_visible = visible.replace("\u200b", "").replace(
                    "\ufeff", ""
                ).strip().lower()
                if onset.get("initialObservationSource") == "known_application_prompt_scaffold":
                    assert normalized_visible in {
                        "ask gemini", "do anything", "start writing...",
                        "write a message…", "write a message...",
                    }
                else:
                    assert not normalized_visible
            else:
                assert onset.get("causalPartitionAfterPriorCompositionProven") is True
                assert onset.get("proofReason") in {
                    "prior_submission",
                    "novel_causal_read_after_prior_surface_write",
                    "outside_write_after_prior_surface_write",
                }
                member_ids = example["episode"]["memberWriteEventIDs"]
                first_member = source_event_by_id[member_ids[0]]
                previous_id = onset.get("previousSameSurfaceWriteEventID")
                previous = source_event_by_id[previous_id]
                same_surface_prior = [
                    event for event in source_events
                    if event.get("kind") == "write"
                    and event.get("sessionID") == first_member.get("sessionID")
                    and event_destination(event) == event_destination(first_member)
                    and instant(event["availableAt"]) < began
                ]
                immediate_previous = max(
                    same_surface_prior,
                    key=lambda event: (event["availableAt"], event["sourceEventID"]),
                )
                assert immediate_previous["sourceEventID"] == previous_id
                boundary_ids = onset.get("boundaryEventIDs") or []
                assert boundary_ids
                boundary_events = [source_event_by_id[value] for value in boundary_ids]
                if onset["proofReason"] == "prior_submission":
                    assert boundary_ids == [previous_id]
                    audit = json.loads(previous.get("auditSerialized", "{}"))
                    assert audit.get("boundaryReason") in {
                        "return_pressed", "submission_boundary"
                    }
                elif onset["proofReason"] == "novel_causal_read_after_prior_surface_write":
                    assert all(
                        instant(previous["availableAt"])
                        < instant(event["availableAt"])
                        < began
                        for event in boundary_events
                    )
                    assert all(event.get("kind") == "read" for event in boundary_events)
                    old_reads = {
                        event.get("serialized") for event in source_events
                        if event.get("kind") == "read"
                        and instant(event["availableAt"]) <= instant(previous["beganAt"])
                    }
                    assert all(event.get("serialized") not in old_reads for event in boundary_events)
                else:
                    assert all(
                        instant(previous["availableAt"])
                        < instant(event["availableAt"])
                        < began
                        for event in boundary_events
                    )
                    assert all(event.get("kind") == "write" for event in boundary_events)
                    assert all(
                        event_destination(event) != event_destination(first_member)
                        for event in boundary_events
                    )
        text = target["resolvedContent"]
        assert isinstance(text, str) and text
        assert "\u200b\n-\n\u200b" not in text
        authored = "".join(
            segment.get("content", "")
            for segment in target["segments"]
            if segment.get("type") == "authored_text"
        ).strip()
        adjudication = adjudication_by_candidate[example["episode"]["candidateID"]]
        closure = adjudication.get("closureReason")
        reviewed = adjudication.get("classificationProvenance") == "reviewed_regression_fixture"
        if raw_authoritative:
            assert adjudication.get("lossEligibility") == "eligible"
            if not (len(authored) >= 40 and len(words(authored)) >= 6):
                assert adjudication.get("closureStatus") == "closed_submission"
                assert len(authored) >= 4
        else:
            assert reviewed or (len(authored) >= 40 and len(words(authored)) >= 6)
        assert all(
            segment.get("type") != "unresolved_paste_transition"
            for segment in target["segments"]
        )
        for segment in target["segments"]:
            if segment.get("type") == "paste":
                forbidden = {
                    "content", "payload", "resolvedContent", "clipboardContent",
                    "historyContent",
                }
                assert not forbidden.intersection(segment)
        target_texts.append(text)

    if args.expect_target_count is not None:
        assert len(examples) == args.expect_target_count
    for fragment in args.expect_target_substring:
        assert any(fragment in text for text in target_texts), fragment
    for prefix in args.reject_target_prefix:
        assert not any(text.startswith(prefix) for text in target_texts), prefix

    counts = manifest["counts"]
    assert counts["convertedEvents"] == len(events)
    assert counts["closedEpisodeEvents"] == len(write_events)
    assert counts["examples"] == len(examples)
    assert counts["sourceWrites"] == len(source_write_ids)
    print(json.dumps({
        "status": "passed",
        "closedEpisodeEvents": len(write_events),
        "lossBearingEpisodes": len(examples),
        "multiWriteLossEpisodes": sum(row["episode"]["memberCount"] > 1 for row in examples),
        "sourceMicroWrites": len(source_write_ids),
        "sourceMicroWritesInModelHistory": 0,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

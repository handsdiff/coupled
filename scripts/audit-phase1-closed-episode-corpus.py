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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--expect-target-count", type=int)
    parser.add_argument("--expect-target-substring", action="append", default=[])
    args = parser.parse_args()

    root = args.corpus.resolve()
    manifest = load_json(root / "corpus.json")
    if manifest.get("artifactType") != "phase1_episode_corpus":
        raise ValueError("not an episode corpus")
    if manifest.get("conversionVersion") != "phase1-episode-causal-v2":
        raise ValueError("audit requires phase1-episode-causal-v2")
    if manifest.get("episodeVersion") != "phase1-episode-v2":
        raise ValueError("audit requires phase1-episode-v2")
    objective = manifest.get("objective") or {}
    assert objective.get("predictionUnit") == "closed_composition_episode"
    assert objective.get("microWritesReceiveLoss") is False
    assert objective.get("microWritesAppearInModelHistory") is False
    assert objective.get("modelFacingWritesAreClosedEpisodes") is True
    eligibility = manifest.get("eligibility") or {}
    assert eligibility.get("minimumTrimmedAuthoredCharacters") == 40
    assert eligibility.get("minimumAuthoredWords") == 6
    assert eligibility.get("groundedPasteActionBypassesMinimumAuthoredContent") is False
    assert eligibility.get("closedButBelowThresholdRemainsHistoryOnly") is True
    assert eligibility.get("unresolvedOrUnclosedMicroWritesAppearInModelHistory") is False

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
    source_write_ids = {
        row["sourceEventID"] for row in source_events if row.get("kind") == "write"
    }
    events = load_jsonl(root / "events.jsonl")
    blocks = load_jsonl(root / "context-blocks.jsonl")
    examples = load_jsonl(root / "examples.jsonl")
    adjudications = load_jsonl(root / "episode-adjudications.jsonl")
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
        assert audit["episodeVersion"] == "phase1-episode-v2"
        assert audit["memberWriteEventIDs"] == members
        assert audit["resolvedCompletion"] == "".join(
            segment["content"] for segment in compact["authorshipSegments"]
        )
        for segment in compact["authorshipSegments"]:
            if segment.get("type") == "paste":
                assert isinstance(segment.get("content"), str) and segment["content"]

    target_texts: list[str] = []
    loss_members: set[str] = set()
    for ordinal, example in enumerate(examples):
        assert example["chronologicalOrdinal"] == ordinal
        assert example["conversionVersion"] == "phase1-episode-causal-v2"
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
        text = target["resolvedContent"]
        assert isinstance(text, str) and text
        assert "\u200b\n-\n\u200b" not in text
        authored = "".join(
            segment.get("content", "")
            for segment in target["segments"]
            if segment.get("type") == "authored_text"
        ).strip()
        assert len(authored) >= 40 and len(words(authored)) >= 6
        for segment in target["segments"]:
            if segment.get("type") == "paste":
                forbidden = {"content", "payload", "resolvedContent", "clipboardContent"}
                assert not forbidden.intersection(segment)
        target_texts.append(text)

    if args.expect_target_count is not None:
        assert len(examples) == args.expect_target_count
    for fragment in args.expect_target_substring:
        assert any(fragment in text for text in target_texts), fragment

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

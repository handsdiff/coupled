#!/usr/bin/env python3
"""Audit lineage, causality, masking inputs, and blind cases in an episode corpus."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path


def rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    args = parser.parse_args()
    manifest = json.loads((args.corpus / "corpus.json").read_text())
    events = rows(args.corpus / "events.jsonl")
    examples = rows(args.corpus / "examples.jsonl")
    event_by_id = {row["sourceEventID"]: row for row in events}
    assert manifest["conversionVersion"] == "phase1-episode-causal-v1"
    assert manifest["objective"]["predictionUnit"] == "closed_composition_episode"
    assert manifest["objective"]["microWritesReceiveLoss"] is False
    assert manifest["counts"]["examples"] == len(examples)
    claimed: set[str] = set()
    labels: dict[str, dict] = {}
    for ordinal, example in enumerate(examples):
        assert example["chronologicalOrdinal"] == ordinal
        assert example["targetUnitType"] == "closed_composition_episode"
        episode = example["episode"]
        members = episode["memberWriteEventIDs"]
        assert members and not (claimed & set(members))
        claimed.update(members)
        assert all(value in event_by_id for value in members)
        assert not (set(members) & set(example["contextEventIDs"]))
        began = instant(example["targetBeganAt"])
        assert all(instant(event_by_id[value]["availableAt"]) < began
                   for value in example["contextEventIDs"])
        target = example["target"]
        assert isinstance(target["resolvedContent"], str) and target["resolvedContent"]
        for segment in target["segments"]:
            if segment["type"] == "paste":
                assert "content" not in segment
                assert segment.get("clipboardSnapshotID")
                assert segment.get("pasteCheckpointID")
        label = episode.get("adjudicationLabel")
        if label:
            labels[label] = example
    for label in ("obsidian_best_examples", "code_post_submission"):
        assert labels[label]["episode"]["memberCount"] > 1
    assert "(131) feels materially stronger" in labels["obsidian_best_examples"]["target"]["resolvedContent"]
    assert labels["code_post_submission"]["target"]["resolvedContent"].endswith(
        "post-submission trigger to clean up some other gaps."
    )
    print(
        f"Episode corpus audit passed: {len(examples)} targets; "
        f"{manifest['counts']['multiWriteEpisodes']} multi-WRITE episodes; "
        f"{len(claimed)} loss-bearing member WRITEs."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

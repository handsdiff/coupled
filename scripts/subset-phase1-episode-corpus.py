#!/usr/bin/env python3
"""Create a deterministic tiny episode corpus for mechanical provider smoke tests."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path


def rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def write_rows(path: Path, values: list[dict]) -> None:
    path.write_text("".join(json.dumps(x, ensure_ascii=False, sort_keys=True) + "\n" for x in values), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--adjudication-label", required=True)
    parser.add_argument("--include-shortest-paste", action="store_true")
    parser.add_argument("--include-longest-ordinary", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError(f"output exists: {args.output}")
    source_manifest = json.loads((args.input / "corpus.json").read_text())
    examples = rows(args.input / "examples.jsonl")
    selected = [
        row for row in examples
        if (
            row.get("episode", {}).get("label")
            or row.get("episode", {}).get("adjudicationLabel")
        ) == args.adjudication_label
    ]
    if len(selected) != 1:
        raise ValueError("adjudication label must select exactly one example")
    if args.include_shortest_paste:
        pastes = [
            row for row in examples
            if any(segment.get("type") == "paste" for segment in row["target"]["segments"])
            and row not in selected
        ]
        if not pastes:
            raise ValueError("source has no separate paste example")
        selected.append(min(pastes, key=lambda row: (len(row["target"]["resolvedContent"]), row["exampleID"])))
    if args.include_longest_ordinary:
        ordinary = [
            row for row in examples
            if not any(segment.get("type") == "paste" for segment in row["target"]["segments"])
            and row not in selected
        ]
        if not ordinary:
            raise ValueError("source has no additional ordinary example")
        selected.append(max(ordinary, key=lambda row: (len(row["target"]["resolvedContent"]), row["exampleID"])))
    selected.sort(key=lambda row: (row["targetBeganAt"], row["exampleID"]))
    for ordinal, row in enumerate(selected):
        row["chronologicalOrdinal"] = ordinal
        row["experimentBlockID"] = "block-0001"
    args.output.mkdir(parents=True)
    for name in ("events.jsonl", "context-blocks.jsonl", "gaps.jsonl", "privacy-policy.json"):
        if (args.input / name).exists():
            shutil.copy2(args.input / name, args.output / name)
    write_rows(args.output / "examples.jsonl", selected)
    corpus_id = "episode_smoke_" + hashlib.sha256(
        json.dumps([x["exampleID"] for x in selected], sort_keys=True).encode()
    ).hexdigest()
    artifact_names = ["events.jsonl", "context-blocks.jsonl", "examples.jsonl"]
    for name in ("gaps.jsonl", "privacy-policy.json"):
        if (args.output / name).exists():
            artifact_names.append(name)
    manifest = {
        **source_manifest,
        "artifactType": "phase1_episode_corpus",
        "corpusID": corpus_id,
        "sessionID": corpus_id,
        "sourceCorpusID": source_manifest["corpusID"],
        "counts": {**source_manifest["counts"], "examples": len(selected)},
        "smokeSubset": {
            "schemaVersion": 1,
            "policy": "named_multi_write_episode_plus_shortest_grounded_paste_plus_longest_ordinary",
            "exampleIDs": [x["exampleID"] for x in selected],
        },
        "artifactDigestsSHA256": {
            name: digest(args.output / name) for name in artifact_names
        },
    }
    write_json(args.output / "corpus.json", manifest)
    write_json(args.output / "dataset.json", manifest)
    print(f"Wrote {len(selected)}-example episode smoke corpus to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

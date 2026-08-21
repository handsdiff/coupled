#!/usr/bin/env python3
"""Repair a score JSONL after two resume processes raced on one output.

The original rows are preserved byte-for-byte.  The active score stream is
reduced to its earliest ordered protocol prefix, and the manifest records the
duplicate provider work and its frozen-rate cost.  This is deliberately a
one-purpose recovery tool, not part of normal experiment execution.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as destination:
        destination.write(payload)
        temporary = Path(destination.name)
    os.replace(temporary, path)


def atomic_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as destination:
        for value in values:
            destination.write(json.dumps(value, separators=(",", ":")) + "\n")
        temporary = Path(destination.name)
    os.replace(temporary, path)


def main() -> int:
    arguments = parse_arguments()
    output = arguments.output.resolve()
    scores_path = output / "scores.jsonl"
    manifest_path = output / "stability.json"
    evidence_path = output / "scores.concurrent-resume-race.jsonl"
    if evidence_path.exists():
        raise SystemExit(f"recovery evidence already exists: {evidence_path}")

    original_hash = sha256(scores_path)
    shutil.copy2(scores_path, evidence_path)
    if sha256(evidence_path) != original_hash:
        raise SystemExit("failed to preserve the raced score stream byte-for-byte")

    plan = json.loads(arguments.plan.read_text())
    blocks = {
        value["blockID"]: value
        for value in load_jsonl(arguments.corpus / "episode-blocks.jsonl")
    }
    expected_ids = [
        example_id
        for block_id in plan["protocol"]["evaluationBlockIDsAfterUpdate"]
        for example_id in blocks[block_id]["exampleIDs"]
    ]
    expected_set = set(expected_ids)
    rows = load_jsonl(scores_path)
    unexpected = [value["exampleID"] for value in rows if value["exampleID"] not in expected_set]
    if unexpected:
        raise SystemExit(f"score stream contains unexpected example IDs: {unexpected}")

    by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for value in rows:
        by_id[value["exampleID"]].append(value)

    canonical: list[dict[str, Any]] = []
    for example_id in expected_ids:
        candidates = by_id.get(example_id, [])
        if not candidates:
            break
        canonical.append(candidates[0])

    canonical_ids = {value["exampleID"] for value in canonical}
    beyond_prefix = sorted(set(by_id) - canonical_ids)
    if beyond_prefix:
        raise SystemExit(
            "race produced rows beyond a missing protocol position; preserve and "
            f"review manually: {beyond_prefix}"
        )

    duplicate_rows = [
        value
        for example_id in expected_ids
        for value in by_id.get(example_id, [])[1:]
    ]
    duplicate_cost = sum(
        (
            Decimal(value["estimatedProviderCostUSDAtFrozenRates"])
            for value in duplicate_rows
        ),
        Decimal(0),
    )
    atomic_jsonl(scores_path, canonical)

    manifest = json.loads(manifest_path.read_text())
    manifest.pop("inflightOperation", None)
    manifest.pop("activeUpdate", None)
    manifest["status"] = "recovered_from_concurrent_resume"
    recovery = {
        "kind": "concurrent_resume_race_recovery",
        "recoveredAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "originalScoreRows": len(rows),
        "canonicalScoreRows": len(canonical),
        "duplicateScoreRows": len(duplicate_rows),
        "duplicateExampleIDs": [value["exampleID"] for value in duplicate_rows],
        "duplicateGenerationCostUSDAtFrozenRates": str(duplicate_cost),
        "originalScoresSHA256": original_hash,
        "canonicalScoresSHA256": sha256(scores_path),
        "preservedEvidence": evidence_path.name,
        "preservedEvidenceSHA256": sha256(evidence_path),
        "selectionRule": "earliest_physical_row_per_example_in_protocol_order",
    }
    manifest.setdefault("recoveryEvents", []).append(recovery)
    manifest.setdefault("concurrentResumeRecoveries", []).append(recovery)
    counts = manifest.setdefault("counts", {})
    counts["evaluationScores"] = len(canonical)
    counts["samples"] = counts.get("baseProbes", 0) + len(canonical)
    atomic_json(manifest_path, manifest)
    print(json.dumps(recovery, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

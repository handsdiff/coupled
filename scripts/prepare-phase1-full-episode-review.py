#!/usr/bin/env python3
"""Create exhaustive singleton plus plausible multi-WRITE review selections."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def logical_destination(example: dict[str, Any]) -> tuple[Any, ...]:
    destination = example.get("conditioningState", {}).get("destination", {})
    bundle = destination.get("bundleIdentifier")
    return (
        bundle,
        destination.get("role"),
        destination.get("fieldDescription"),
        destination.get("fieldLabel"),
        destination.get("windowTitle") if bundle == "md.obsidian" else None,
    )


def event_destination(event: dict[str, Any]) -> tuple[Any, Any]:
    value = json.loads(event.get("serialized", "{}"))
    destination = value.get("destination") or {}
    return destination.get("application"), destination.get("window")


def event_audit(event: dict[str, Any]) -> dict[str, Any]:
    return json.loads(event.get("auditSerialized", "{}"))


def is_prompt_submission_surface(event: dict[str, Any]) -> bool:
    """Narrowly identify the prompt surfaces validated by the current traces."""
    audit = event_audit(event)
    application = str(audit.get("appName") or "").lower()
    window = str(audit.get("windowTitle") or "").lower()
    if application == "chatgpt":
        return True
    return application == "google chrome" and any(
        marker in window for marker in ("chatgpt", "claude", "gemini")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--prior-candidates", action="append", type=Path, default=[])
    parser.add_argument(
        "--base-selection",
        type=Path,
        help="reuse an already frozen exhaustive selection before adding onset probes",
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--automatic-gap-seconds", type=float, default=30.0)
    args = parser.parse_args()
    corpus = args.corpus.resolve()
    manifest = load_json(corpus / "corpus.json")
    examples = load_jsonl(corpus / "examples.jsonl")
    events = load_jsonl(corpus / "events.jsonl")
    event_by_id = {row["sourceEventID"]: row for row in events}
    ordinal_by_event = {
        row["targetEventID"]: row["chronologicalOrdinal"] + 1 for row in examples
    }
    neighborhoods: list[dict[str, Any]] = []

    if args.base_selection:
        base = load_json(args.base_selection.resolve())
        if base.get("corpusID") != manifest["corpusID"]:
            raise ValueError("base selection belongs to a different corpus")
        rows = base.get("neighborhoods")
        if not isinstance(rows, list) or not rows:
            raise ValueError("base selection has no neighborhoods")
        neighborhoods.extend(rows)

    # Rebind previously reviewed neighborhoods to the replayed corpus by stable
    # event lineage. Newly recovered eligible WRITEs inside their bounds are
    # included automatically when the candidate is rebuilt.
    for path in ([] if args.base_selection else args.prior_candidates):
        for candidate in load_jsonl(path.resolve()):
            eligible = [
                ordinal_by_event[value]
                for value in candidate.get("memberWriteEventIDs", [])
                if value in ordinal_by_event
            ]
            if not eligible:
                continue
            member_set = set(candidate.get("memberWriteEventIDs", []))
            first_began = examples[min(eligible) - 1]["targetBeganAt"]
            leading = [
                value for value in member_set
                if value in event_by_id
                and value not in ordinal_by_event
                and (event_by_id[value].get("beganAt") or event_by_id[value]["availableAt"])
                    < first_began
            ]
            neighborhoods.append({
                "label": "reviewed_" + candidate["label"],
                "firstOneBasedExampleOrdinal": min(eligible),
                "lastOneBasedExampleOrdinal": max(eligible),
                "category": "replayed_prior_review",
                "rationale": (
                    "Rebuild previously reviewed bounds from stable WRITE lineage "
                    "under the current semantic reducer."
                ),
                **({"leadingWriteEventIDs": sorted(leading)} if leading else {}),
            })

    # Every eligible micro-WRITE receives its own closure diagnostic. This is
    # what makes an unreviewed-singleton fallback impossible downstream.
    if not args.base_selection:
        for ordinal, example in enumerate(examples, 1):
            neighborhoods.append({
                "label": f"singleton_{ordinal:04d}",
                "firstOneBasedExampleOrdinal": ordinal,
                "lastOneBasedExampleOrdinal": ordinal,
                "category": "exhaustive_singleton_closure",
                "rationale": "Determine whether this micro-WRITE is independently closed and substantive.",
            })

    # Plausible automatic composition runs are only proposals. The review
    # builder will independently test field continuity, causal READ novelty,
    # outside WRITEs, and single-completion reconstruction.
    runs: list[tuple[int, int]] = []
    start = 0
    for index in range(1, len(examples)):
        previous = examples[index - 1]
        current = examples[index]
        previous_event = event_by_id[previous["targetEventID"]]
        gap = (
            instant(current["targetBeganAt"])
            - instant(previous_event["availableAt"])
        ).total_seconds()
        joins = (
            gap <= args.automatic_gap_seconds
            and logical_destination(previous) == logical_destination(current)
            and previous.get("sourceSessionOrdinal") == current.get("sourceSessionOrdinal")
        )
        if not joins:
            if index - start >= 2:
                runs.append((start + 1, index))
            start = index
    if len(examples) - start >= 2:
        runs.append((start + 1, len(examples)))
    automatic_subruns: list[tuple[int, int]] = []
    for run_first, run_last in ([] if args.base_selection else runs):
        for first in range(run_first, run_last):
            for last in range(first + 1, run_last + 1):
                automatic_subruns.append((first, last))
                neighborhoods.append({
                    "label": f"automatic_run_{first:04d}_{last:04d}",
                    "firstOneBasedExampleOrdinal": first,
                    "lastOneBasedExampleOrdinal": last,
                    "category": "automatic_same_editable_composition_proposal",
                    "rationale": (
                        f"Contiguous subrun of same-logical-editable WRITEs separated by at most "
                        f"{args.automatic_gap_seconds:g} seconds; evidence gates remain authoritative."
                    ),
                })

    # Eligible targets are not a sufficient candidate universe: the opening of
    # a submitted prompt can be a history-only micro-WRITE.  For every eligible
    # prompt-surface endpoint, propose every earlier same-window WRITE as a
    # possible onset.  The review builder then selects all intervening WRITEs
    # and independently enforces raw state continuity, stable editable identity,
    # causal READ novelty, and outside-WRITE gates.  We deliberately impose no
    # pause threshold here; quiet time is not evidence that a thought ended.
    ordinal_by_event = {
        example["targetEventID"]: ordinal
        for ordinal, example in enumerate(examples, 1)
    }
    writes_by_surface: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for event in events:
        if event.get("kind") != "write":
            continue
        key = (event.get("sessionID"), *event_destination(event))
        writes_by_surface.setdefault(key, []).append(event)
    onset_probe_count = 0
    for writes in writes_by_surface.values():
        writes.sort(key=lambda event: (event["beganAt"], event["sourceEventID"]))
        for end_index, endpoint in enumerate(writes):
            ordinal = ordinal_by_event.get(endpoint["sourceEventID"])
            if ordinal is None or not is_prompt_submission_surface(endpoint):
                continue
            for start_index in range(end_index):
                onset = writes[start_index]
                neighborhoods.append({
                    "label": (
                        f"submission_onset_{ordinal:04d}_"
                        f"{onset['sourceEventID'].removeprefix('evt_')[:12]}"
                    ),
                    "firstOneBasedExampleOrdinal": ordinal,
                    "lastOneBasedExampleOrdinal": ordinal,
                    "leadingWriteEventIDs": [onset["sourceEventID"]],
                    "category": "all_write_prompt_onset_probe",
                    "rationale": (
                        "Test whether an earlier semantic WRITE, including a history-only "
                        "WRITE, is the true onset of this submitted composition."
                    ),
                })
                onset_probe_count += 1

    labels = [row["label"] for row in neighborhoods]
    if len(labels) != len(set(labels)):
        raise ValueError("generated duplicate selection labels")
    artifact = {
        "schemaVersion": 1,
        "selectionID": "phase1-full-episode-review-v3",
        "corpusID": manifest["corpusID"],
        "status": "exhaustive_shadow_episode_selection",
        "automaticGapSeconds": args.automatic_gap_seconds,
        "counts": {
            "eligibleMicroWrites": len(examples),
            "priorReviewedNeighborhoods": sum(
                row["category"] == "replayed_prior_review" for row in neighborhoods
            ),
            "singletonNeighborhoods": sum(
                row["category"] == "exhaustive_singleton_closure"
                for row in neighborhoods
            ),
            "automaticRunNeighborhoods": sum(
                row["category"] == "automatic_same_editable_composition_proposal"
                for row in neighborhoods
            ),
            "submissionOnsetProbeNeighborhoods": onset_probe_count,
            "totalNeighborhoods": len(neighborhoods),
        },
        "neighborhoods": neighborhoods,
    }
    if args.output.exists():
        raise ValueError(f"output exists: {args.output}")
    args.output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(artifact["counts"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

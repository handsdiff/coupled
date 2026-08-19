#!/usr/bin/env python3
"""Project a Phase 1 micro-WRITE corpus into closed composition episodes.

Semantic READ/WRITE events remain unchanged and continue to form later history.
Only examples are replaced: reviewed groups become one loss-bearing episode,
reviewed history-only/ambiguous groups receive no loss, and unaffected examples
remain explicit one-member episodes pending broader adjudication.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterable


EPISODE_VERSION = "phase1-episode-v1"
CONVERSION_VERSION = "phase1-episode-causal-v1"


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(canonical_bytes(value)).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected an object")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_number}: expected an object")
            rows.append(row)
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("wb") as handle:
        for row in rows:
            handle.write(canonical_bytes(row))


def reviewed_units(annotations: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], set[str]]:
    units: list[dict[str, Any]] = []
    covered: set[str] = set()
    for annotation in annotations:
        members = annotation.get("memberWriteEventIDs")
        if not isinstance(members, list) or not members or any(not isinstance(x, str) for x in members):
            raise ValueError(f"invalid member lineage for {annotation.get('label')}")
        overlap = covered.intersection(members)
        # Overlap is permitted only for the explicitly reviewed partition pair.
        if overlap and annotation.get("decision") != "partition_candidate":
            raise ValueError(f"overlapping adjudications for {sorted(overlap)}")
        covered.update(members)
        decision = annotation.get("decision")
        if decision in {"merge_closed_episode", "keep_single_closed_episode"}:
            target = annotation.get("finalizedTarget")
            if not isinstance(target, dict):
                raise ValueError(f"closed episode has no target: {annotation.get('label')}")
            units.append({
                "memberWriteEventIDs": members,
                "target": target,
                "decision": decision,
                "adjudicationLabel": annotation.get("label"),
                "closureAssessment": annotation.get("closureAssessment"),
                "adjudicationCandidateID": annotation.get("candidateID"),
            })
        elif decision == "split_into_independent_episodes":
            for member in members:
                units.append({
                    "memberWriteEventIDs": [member],
                    "target": None,
                    "decision": "reviewed_independent_singleton",
                    "adjudicationLabel": annotation.get("label"),
                    "adjudicationCandidateID": annotation.get("candidateID"),
                })
        elif decision in {"defer_causal_ambiguity", "partition_history_only_sequence"}:
            pass
        elif decision == "partition_candidate":
            for partition in annotation.get("partitions", []):
                part_members = partition.get("partitionEvidence", {}).get("memberWriteEventIDs")
                if not isinstance(part_members, list) or not part_members:
                    raise ValueError(f"partition lacks lineage: {annotation.get('label')}")
                if partition.get("decision") == "merge_closed_episode":
                    target = partition.get("finalizedTarget")
                    if not isinstance(target, dict):
                        raise ValueError(f"merged partition lacks target: {annotation.get('label')}")
                    units.append({
                        "memberWriteEventIDs": part_members,
                        "target": target,
                        "decision": "merge_closed_episode",
                        "adjudicationLabel": annotation.get("label"),
                        "adjudicationCandidateID": annotation.get("candidateID"),
                        "partition": {
                            "first": partition.get("firstOneBasedExampleOrdinal"),
                            "last": partition.get("lastOneBasedExampleOrdinal"),
                        },
                    })
        else:
            raise ValueError(f"unsupported adjudication decision: {decision}")
    return units, covered


def model_target(target: dict[str, Any]) -> dict[str, Any]:
    """Keep resolved payload for audit, but never place it in a paste target span."""
    result = json.loads(json.dumps(target))
    segments = result.get("segments")
    if not isinstance(segments, list):
        raise ValueError("episode target has no structured segments")
    for segment in segments:
        if isinstance(segment, dict) and segment.get("type") == "paste":
            for key in ("content", "payload", "resolvedContent", "clipboardContent"):
                segment.pop(key, None)
    return result


def serialize_query(conditioning: dict[str, Any]) -> str:
    destination = dict(conditioning.get("destination") or {})
    destination.pop("processIdentifier", None)
    cursor = conditioning.get("cursorContext") or {}
    if cursor.get("source") == "accessibility_string_for_range":
        model_cursor = {
            key: cursor[key]
            for key in ("schemaVersion", "fieldState", "leftContext", "selectedText", "rightContext", "surfacePrompt")
            if key in cursor
        }
    else:
        model_cursor = cursor
    query: dict[str, Any] = {
        "schemaVersion": 3 if conditioning.get("clipboard") is not None else 2,
        "kind": "write_conditioning_state",
        "destination": destination,
        "cursorContext": model_cursor,
    }
    clipboard = conditioning.get("clipboard")
    if isinstance(clipboard, dict):
        query["clipboard"] = {
            "changeCount": clipboard.get("changeCount"),
            "content": clipboard.get("text"),
            "contentWasTruncated": clipboard.get("textWasTruncated"),
        }
    return canonical_json(query)


def construct(
    source: Path,
    annotations_path: Path | None,
    output: Path,
    candidate_paths: list[Path] | None = None,
) -> dict[str, Any]:
    manifest = load_json(source / "corpus.json")
    if manifest.get("conversionVersion") != "phase1-causal-v14":
        raise ValueError("episode construction requires a phase1-causal-v14 corpus")
    events = load_jsonl(source / "events.jsonl")
    examples = load_jsonl(source / "examples.jsonl")
    event_by_id = {row["sourceEventID"]: row for row in events}
    context_blocks = load_jsonl(source / "context-blocks.jsonl")
    context_by_id = {row["contextBlockID"]: row for row in context_blocks}
    example_by_target = {row["targetEventID"]: row for row in examples}
    if len(example_by_target) != len(examples):
        raise ValueError("micro corpus contains duplicate target event IDs")

    annotations: list[dict[str, Any]] = []
    if annotations_path is not None:
        annotations = load_jsonl(annotations_path)
    candidate_by_id: dict[str, dict[str, Any]] = {}
    for candidate_path in candidate_paths or []:
        for candidate in load_jsonl(candidate_path):
            candidate_id = candidate.get("candidateID")
            if not isinstance(candidate_id, str) or candidate_id in candidate_by_id:
                raise ValueError(f"invalid or duplicate candidate evidence: {candidate_id}")
            candidate_by_id[candidate_id] = candidate
    units, covered = reviewed_units(annotations)
    unknown = covered - set(event_by_id)
    if unknown:
        raise ValueError(f"adjudication references unknown events: {sorted(unknown)}")

    # Explicitly retain every unaffected eligible target as a one-member episode.
    # Its status makes the absence of human episode adjudication inspectable.
    for example in examples:
        event_id = example["targetEventID"]
        if event_id not in covered:
            units.append({
                "memberWriteEventIDs": [event_id],
                "target": example["target"],
                "decision": "unreviewed_singleton_closed_baseline",
            })

    episode_examples: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    claimed: set[str] = set()
    for unit in units:
        member_ids = unit["memberWriteEventIDs"]
        duplicate = claimed.intersection(member_ids)
        if duplicate:
            # The broad 123-125 candidate overlaps its narrower accepted partition;
            # history-only members are covered but never emitted as a second target.
            if unit["decision"] != "merge_closed_episode":
                continue
            raise ValueError(f"event belongs to multiple loss units: {sorted(duplicate)}")
        claimed.update(member_ids)
        member_events = [event_by_id[value] for value in member_ids]
        member_examples = [example_by_target[value] for value in member_ids if value in example_by_target]
        if not member_examples:
            exclusions.append({
                "schemaVersion": 1,
                "episodeVersion": EPISODE_VERSION,
                "reason": "episode_has_no_eligible_micro_member",
                "memberWriteEventIDs": member_ids,
                "decision": unit["decision"],
            })
            continue
        first = min(member_examples, key=lambda row: (row["targetBeganAt"], row["exampleID"]))
        target = model_target(unit.get("target") or first["target"])
        began_at = min(event["beganAt"] for event in member_events)
        available_at = max(event["availableAt"] for event in member_events)
        episode_id = stable_id("episode_", {
            "corpusID": manifest["corpusID"],
            "memberWriteEventIDs": member_ids,
        })
        example_id = stable_id("example_", {"episodeID": episode_id})
        source_record_ids = sorted({
            value
            for event in member_events
            for value in event.get("sourceRecordIDs", [])
            if isinstance(value, str)
        })
        candidate = candidate_by_id.get(unit.get("adjudicationCandidateID"))
        conditioning_state = (
            candidate.get("initialConditioningState")
            if isinstance(candidate, dict) else first.get("conditioningState")
        )
        if not isinstance(conditioning_state, dict):
            raise ValueError(f"episode has no initial conditioning state: {episode_id}")
        query = serialize_query(conditioning_state) if candidate is not None else first["query"]
        context_block_ids = []
        for block_id in first.get("contextBlockIDs", first.get("contextEventIDs", [])):
            if block_id in member_ids:
                continue
            block = context_by_id[block_id]
            if block.get("contextBlockType") == "semantic_event":
                available = event_by_id[block_id]["availableAt"]
                if available >= began_at:
                    continue
            context_block_ids.append(block_id)
        serialized_blocks = [context_by_id[value]["serialized"] for value in context_block_ids]
        context = "\n".join(serialized_blocks)
        model_input = query if not context else context + "\n" + query
        episode_examples.append({
            **first,
            "schemaVersion": 11,
            "conversionVersion": CONVERSION_VERSION,
            "exampleID": example_id,
            "targetEventID": member_ids[0],
            "targetUnitID": episode_id,
            "targetUnitType": "closed_composition_episode",
            "targetBeganAt": began_at,
            "targetAvailableAt": available_at,
            "conditioningState": conditioning_state,
            "query": query,
            "contextBlockIDs": context_block_ids,
            "contextEventIDs": [
                value for value in context_block_ids
                if context_by_id[value].get("contextBlockType") == "semantic_event"
            ],
            "context": context,
            "modelInput": model_input,
            "target": target,
            "targetSourceRecordIDs": source_record_ids,
            "sourceRecordIDs": sorted(set(first.get("contextSourceRecordIDs", [])) | set(source_record_ids)),
            "episode": {
                "schemaVersion": 1,
                "episodeVersion": EPISODE_VERSION,
                "memberWriteEventIDs": member_ids,
                "memberCount": len(member_ids),
                "decision": unit["decision"],
                **({"adjudicationLabel": unit["adjudicationLabel"]} if unit.get("adjudicationLabel") else {}),
                **({"adjudicationCandidateID": unit["adjudicationCandidateID"]} if unit.get("adjudicationCandidateID") else {}),
                **({"conditioningSource": "candidate_episode_onset"} if candidate is not None else {"conditioningSource": "source_micro_example"}),
                **({"closureAssessment": unit["closureAssessment"]} if unit.get("closureAssessment") else {}),
                **({"partition": unit["partition"]} if unit.get("partition") else {}),
            },
            "targetMetadata": {
                "episodeVersion": EPISODE_VERSION,
                "memberWriteEventIDs": member_ids,
                "microWriteCount": len(member_ids),
                "availableAt": available_at,
                "decision": unit["decision"],
            },
        })

    episode_examples.sort(key=lambda row: (row["targetBeganAt"], row["exampleID"]))
    for ordinal, row in enumerate(episode_examples):
        row["chronologicalOrdinal"] = ordinal
        row["experimentBlockID"] = f"block-{ordinal // 50 + 1:04d}"

    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        for name in ("events.jsonl", "context-blocks.jsonl", "gaps.jsonl", "privacy-policy.json"):
            if (source / name).exists():
                shutil.copy2(source / name, temporary / name)
        write_jsonl(temporary / "examples.jsonl", episode_examples)
        write_jsonl(temporary / "episode-target-exclusions.jsonl", exclusions)
        write_jsonl(temporary / "episode-adjudications.jsonl", annotations)
        source_hashes = {
            name: sha256(source / name)
            for name in ("corpus.json", "events.jsonl", "examples.jsonl")
        }
        artifact = {
            "schemaVersion": 1,
            "artifactType": "phase1_episode_corpus",
            "assemblerVersion": manifest.get("assemblerVersion"),
            "conversionVersion": CONVERSION_VERSION,
            "episodeVersion": EPISODE_VERSION,
            "corpusID": stable_id("episode_corpus_", {
                "sourceCorpusID": manifest["corpusID"],
                "sourceHashes": source_hashes,
                "annotationSHA256": sha256(annotations_path) if annotations_path else None,
                "candidateEvidenceSHA256": [sha256(path) for path in candidate_paths or []],
                "episodeVersion": EPISODE_VERSION,
            }),
            "sessionID": None,
            "sourceCorpusID": manifest["corpusID"],
            "source": {
                "path": str(source.resolve()),
                "digestsSHA256": source_hashes,
                **({"adjudicationsSHA256": sha256(annotations_path)} if annotations_path else {}),
                "candidateEvidenceSHA256": {
                    str(path.resolve()): sha256(path) for path in candidate_paths or []
                },
            },
            "serialization": manifest["serialization"],
            "objective": {
                **manifest["objective"],
                "predictionUnit": "closed_composition_episode",
                "microWritesReceiveLoss": False,
                "intermediateMicroWritesRemainInHistory": True,
            },
            "eligibility": {
                **manifest["eligibility"],
                "episodePolicy": (
                    "human-adjudicated reviewed neighborhoods; explicit one-member "
                    "closed baseline elsewhere"
                ),
            },
            "timing": manifest["timing"],
            "counts": {
                "convertedEvents": len(events),
                "sourceMicroExamples": len(examples),
                "examples": len(episode_examples),
                "multiWriteEpisodes": sum(row["episode"]["memberCount"] > 1 for row in episode_examples),
                "microWritesAbsorbedIntoMultiWriteEpisodes": sum(
                    row["episode"]["memberCount"] for row in episode_examples if row["episode"]["memberCount"] > 1
                ),
                "reviewedHistoryOnlyMicroWrites": len(covered - claimed),
                "episodeTargetExclusions": len(exclusions),
            },
        }
        artifact["sessionID"] = artifact["corpusID"]
        write_jsonl(temporary / "episode-blocks.jsonl", [
            {
                "blockID": f"block-{index // 50 + 1:04d}",
                "exampleIDs": [x["exampleID"] for x in episode_examples[index:index + 50]],
                "exampleCount": len(episode_examples[index:index + 50]),
            }
            for index in range(0, len(episode_examples), 50)
        ])
        artifact["artifactDigestsSHA256"] = {
            name: sha256(temporary / name)
            for name in (
                "events.jsonl", "examples.jsonl", "episode-target-exclusions.jsonl",
                "episode-adjudications.jsonl", "episode-blocks.jsonl",
            )
        }
        (temporary / "corpus.json").write_bytes(canonical_bytes(artifact))
        (temporary / "dataset.json").write_bytes(canonical_bytes(artifact))
        if output.exists():
            raise ValueError(f"output already exists: {output}")
        temporary.rename(output)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--adjudications", action="append", type=Path)
    parser.add_argument("--candidate-evidence", action="append", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    adjudications = args.adjudications or []
    if len(adjudications) > 1:
        combined = args.output.resolve().parent / f".{args.output.name}.combined-adjudications.jsonl"
        with combined.open("wb") as handle:
            for path in adjudications:
                data = path.resolve().read_bytes()
                handle.write(data)
                if data and not data.endswith(b"\n"):
                    handle.write(b"\n")
        adjudications_path = combined
    else:
        adjudications_path = adjudications[0].resolve() if adjudications else None
    try:
        artifact = construct(
            args.corpus.resolve(), adjudications_path, args.output.resolve(),
            [path.resolve() for path in (args.candidate_evidence or [])],
        )
    finally:
        if len(adjudications) > 1 and adjudications_path.exists():
            adjudications_path.unlink()
    print(json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

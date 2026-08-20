#!/usr/bin/env python3
"""Deterministically assemble compatible Phase 1 sessions into one corpus."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterable


ASSEMBLER_VERSION = "phase1-corpus-v2"
PRIVACY_POLICY_VERSION = "phase1-context-privacy-v1"
RAW_EPISODE_EXPERIMENT_ADAPTER_VERSION = (
    "phase1-raw-episode-experiment-adapter-v1"
)
SUPPORTED_RAW_EPISODE_EXPERIMENT_CONTRACT = {
    "artifactType": "phase1_raw_authoritative_episode_corpus",
    "schemaVersion": 2,
    "episodeVersion": "phase1-raw-episode-v6",
    "conversionVersion": "phase1-raw-episode-causal-v6",
}


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode()


def canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


PRIVACY_REDACTION_SERIALIZED = canonical_json({
    "kind": "write",
    "privacy": "sensitive_content_redacted",
})


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(json_bytes(value)).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected a JSON object")
            rows.append(value)
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("wb") as handle:
        for row in rows:
            handle.write(json_bytes(row))


def contract(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "conversionVersion": manifest.get("conversionVersion"),
        "serialization": manifest.get("serialization"),
        "objective": manifest.get("objective"),
        "eligibility": manifest.get("eligibility"),
        "timing": manifest.get("timing"),
        "reducerVersion": manifest.get("source", {}).get("reducerVersion"),
    }


def load_session(path: Path) -> dict[str, Any]:
    required = ("dataset.json", "events.jsonl", "examples.jsonl")
    for name in required:
        if not (path / name).is_file():
            raise ValueError(f"{path}: missing {name}")
    manifest = load_json(path / "dataset.json")
    if manifest.get("conversionVersion") != "phase1-causal-v14":
        raise ValueError(f"{path}: corpus assembly requires phase1-causal-v14")
    session_id = manifest.get("sessionID")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError(f"{path}: missing sessionID")
    events = load_jsonl(path / "events.jsonl")
    examples = load_jsonl(path / "examples.jsonl")
    if manifest.get("counts", {}).get("convertedEvents") != len(events):
        raise ValueError(f"{path}: event count disagrees with dataset.json")
    if manifest.get("counts", {}).get("examples") != len(examples):
        raise ValueError(f"{path}: example count disagrees with dataset.json")
    event_ids = [event.get("sourceEventID") for event in events]
    if any(not isinstance(value, str) for value in event_ids) or len(set(event_ids)) != len(events):
        raise ValueError(f"{path}: invalid or duplicate event IDs")
    event_by_id = dict(zip(event_ids, events))
    for example in examples:
        if example.get("sessionID") != session_id:
            raise ValueError(f"{path}: example sessionID disagrees")
        context_ids = example.get("contextEventIDs")
        if not isinstance(context_ids, list) or any(value not in event_by_id for value in context_ids):
            raise ValueError(f"{path}: example has invalid context lineage")
        blocks = [event_by_id[value]["serialized"] for value in context_ids]
        context_text = "\n".join(blocks)
        query = example.get("query")
        if not isinstance(query, str):
            raise ValueError(f"{path}: example query is not text")
        model_input = query if not context_text else context_text + "\n" + query
        if example.get("context") != context_text or example.get("modelInput") != model_input:
            raise ValueError(f"{path}: example context is inconsistent")
    digest_names = required + (
        "target-exclusions.jsonl",
        "context-exclusions.jsonl",
        "rejections.jsonl",
    )
    digests = {
        name: sha256(path / name)
        for name in digest_names
        if (path / name).is_file()
    }
    all_times = [
        value
        for event in events
        for value in (event.get("beganAt"), event.get("availableAt"))
        if isinstance(value, str)
    ] + [
        example["targetBeganAt"]
        for example in examples
        if isinstance(example.get("targetBeganAt"), str)
    ]
    if not all_times:
        raise ValueError(f"{path}: no semantic timestamps")
    return {
        "path": path,
        "manifest": manifest,
        "sessionID": session_id,
        "events": events,
        "eventByID": event_by_id,
        "examples": examples,
        "digests": digests,
        "firstAt": min(all_times),
        "lastAt": max(all_times),
    }


def load_privacy_policy(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    value = load_json(path.expanduser().resolve())
    if not (
        value.get("schemaVersion") == 1
        and value.get("policyVersion") == PRIVACY_POLICY_VERSION
        and isinstance(value.get("events"), list)
    ):
        raise ValueError(f"{path}: unsupported privacy policy")
    result: dict[str, dict[str, str]] = {}
    for ordinal, entry in enumerate(value["events"]):
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: privacy event {ordinal} is not an object")
        event_id = entry.get("sourceEventID")
        reason = entry.get("reason")
        group_id = entry.get("groupID")
        if not (
            isinstance(event_id, str) and event_id
            and isinstance(reason, str) and reason
            and (group_id is None or isinstance(group_id, str))
            and event_id not in result
        ):
            raise ValueError(f"{path}: privacy event {ordinal} is invalid or duplicated")
        result[event_id] = {
            "reason": reason,
            **({"groupID": group_id} if group_id is not None else {}),
        }
    return result


def assemble(
    input_paths: list[Path],
    output: Path,
    block_size: int = 50,
    privacy_policy_path: Path | None = None,
) -> dict[str, Any]:
    if len(input_paths) < 1:
        raise ValueError("at least one --input is required")
    if block_size <= 0:
        raise ValueError("block size must be positive")
    if output.exists():
        raise ValueError(f"output already exists: {output}; use a fresh directory")

    privacy_policy = load_privacy_policy(privacy_policy_path)
    sessions = [load_session(path.expanduser().resolve()) for path in input_paths]
    session_ids = [session["sessionID"] for session in sessions]
    if len(set(session_ids)) != len(session_ids):
        raise ValueError("input sessions contain duplicate sessionID values")
    baseline_contract = contract(sessions[0]["manifest"])
    for session in sessions[1:]:
        if contract(session["manifest"]) != baseline_contract:
            raise ValueError(
                f"{session['path']}: dataset contract differs from the first session"
            )
    for previous, current in zip(sessions, sessions[1:]):
        if previous["lastAt"] >= current["firstAt"]:
            raise ValueError(
                "input sessions must be supplied in non-overlapping chronological order"
            )

    corpus_sources = [
        {
            "ordinal": index,
            "sessionID": session["sessionID"],
            "firstSemanticAt": session["firstAt"],
            "lastSemanticAt": session["lastAt"],
            "digestsSHA256": session["digests"],
        }
        for index, session in enumerate(sessions)
    ]
    corpus_id = stable_id(
        "corpus_",
        {
            "assemblerVersion": ASSEMBLER_VERSION,
            "sources": corpus_sources,
            "privacyPolicy": privacy_policy,
        },
    )

    semantic_events: list[dict[str, Any]] = []
    context_blocks: list[dict[str, Any]] = []
    context_by_id: dict[str, dict[str, Any]] = {}
    source_prefixes: list[list[str]] = []
    preceding_block_ids: list[str] = []
    gaps: list[dict[str, Any]] = []
    seen_event_ids: set[str] = set()
    for index, session in enumerate(sessions):
        source_prefixes.append(list(preceding_block_ids))
        for event in session["events"]:
            event_id = event["sourceEventID"]
            if event_id in seen_event_ids:
                raise ValueError(f"duplicate event ID across sessions: {event_id}")
            seen_event_ids.add(event_id)
            corpus_event = {**event, "corpusID": corpus_id, "sourceSessionOrdinal": index}
            semantic_events.append(corpus_event)
            privacy = privacy_policy.get(event_id)
            block = {
                "contextBlockID": event_id,
                "contextBlockType": "semantic_event",
                "sessionID": session["sessionID"],
                "availableAt": event.get("availableAt"),
                "serialized": (
                    PRIVACY_REDACTION_SERIALIZED if privacy is not None
                    else event["serialized"]
                ),
                "sourceEventID": event_id,
            }
            if privacy is not None:
                block["privacyRedaction"] = {
                    "policyVersion": PRIVACY_POLICY_VERSION,
                    "reason": privacy["reason"],
                    **(
                        {"groupID": privacy["groupID"]}
                        if "groupID" in privacy else {}
                    ),
                    "sourceSerializedSHA256": hashlib.sha256(
                        event["serialized"].encode()
                    ).hexdigest(),
                }
            context_blocks.append(block)
            context_by_id[event_id] = block
            preceding_block_ids.append(event_id)
        if index + 1 < len(sessions):
            following = sessions[index + 1]
            gap_payload = {
                "kind": "coverage_gap",
                "coverage": "unknown",
                "fromSessionID": session["sessionID"],
                "toSessionID": following["sessionID"],
            }
            gap_id = stable_id(
                "gap_",
                {
                    "corpusID": corpus_id,
                    "ordinal": index,
                    "from": session["sessionID"],
                    "to": following["sessionID"],
                },
            )
            gap = {
                "contextBlockID": gap_id,
                "contextBlockType": "coverage_gap",
                "coverage": "unknown",
                "afterAt": session["lastAt"],
                "beforeAt": following["firstAt"],
                "fromSessionID": session["sessionID"],
                "toSessionID": following["sessionID"],
                "serialized": canonical_json(gap_payload),
            }
            gaps.append(gap)
            context_blocks.append(gap)
            context_by_id[gap_id] = gap
            preceding_block_ids.append(gap_id)

    unknown_private_ids = set(privacy_policy) - seen_event_ids
    if unknown_private_ids:
        raise ValueError(
            "privacy policy references unknown event IDs: "
            + ", ".join(sorted(unknown_private_ids))
        )
    eligible_target_ids = {
        example["targetEventID"]
        for session in sessions
        for example in session["examples"]
    }
    private_target_ids = set(privacy_policy) & eligible_target_ids
    privacy_policy_artifact = {
        "schemaVersion": 1,
        "policyVersion": PRIVACY_POLICY_VERSION,
        "events": [
            {
                "sourceEventID": event_id,
                **privacy_policy[event_id],
            }
            for event_id in sorted(privacy_policy)
        ],
    }

    assembled_examples: list[dict[str, Any]] = []
    seen_example_ids: set[str] = set()
    for session_index, session in enumerate(sessions):
        prefix = source_prefixes[session_index]
        for source_example in session["examples"]:
            if source_example.get("targetEventID") in privacy_policy:
                continue
            example_id = source_example.get("exampleID")
            if not isinstance(example_id, str) or example_id in seen_example_ids:
                raise ValueError(f"invalid or duplicate example ID: {example_id}")
            seen_example_ids.add(example_id)
            local_ids = source_example["contextEventIDs"]
            context_ids = prefix + local_ids
            serialized_blocks = [context_by_id[value]["serialized"] for value in context_ids]
            context_text = "\n".join(serialized_blocks)
            query = source_example["query"]
            model_input = query if not context_text else context_text + "\n" + query
            assembled_examples.append({
                **source_example,
                "corpusID": corpus_id,
                "sourceSessionOrdinal": session_index,
                "contextBlockIDs": context_ids,
                "contextEventIDs": [
                    value for value in context_ids
                    if context_by_id[value]["contextBlockType"] == "semantic_event"
                ],
                "context": context_text,
                "modelInput": model_input,
            })

    assembled_examples.sort(
        key=lambda value: (
            value["targetBeganAt"], value["sourceSessionOrdinal"], value["exampleID"]
        )
    )
    blocks: list[dict[str, Any]] = []
    for index, example in enumerate(assembled_examples):
        block_ordinal = index // block_size
        block_id = f"block-{block_ordinal + 1:04d}"
        example["chronologicalOrdinal"] = index
        example["experimentBlockID"] = block_id
        if block_ordinal == len(blocks):
            blocks.append({
                "blockID": block_id,
                "ordinal": block_ordinal,
                "firstExampleOrdinal": index,
                "exampleIDs": [],
            })
        blocks[block_ordinal]["exampleIDs"].append(example["exampleID"])
        blocks[block_ordinal]["lastExampleOrdinal"] = index
    for block in blocks:
        block["exampleCount"] = len(block["exampleIDs"])
        block["firstTargetBeganAt"] = assembled_examples[
            block["firstExampleOrdinal"]
        ]["targetBeganAt"]
        block["lastTargetBeganAt"] = assembled_examples[
            block["lastExampleOrdinal"]
        ]["targetBeganAt"]

    temporary_parent = output.parent
    temporary_parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=temporary_parent))
    try:
        write_jsonl(temporary / "events.jsonl", semantic_events)
        write_jsonl(temporary / "context-blocks.jsonl", context_blocks)
        write_jsonl(temporary / "examples.jsonl", assembled_examples)
        write_jsonl(temporary / "gaps.jsonl", gaps)
        (temporary / "privacy-policy.json").write_bytes(
            json_bytes(privacy_policy_artifact)
        )
        for name in ("target-exclusions.jsonl", "context-exclusions.jsonl", "rejections.jsonl"):
            rows = []
            for ordinal, session in enumerate(sessions):
                for row in load_jsonl(session["path"] / name):
                    rows.append({**row, "sourceSessionOrdinal": ordinal})
            if name == "target-exclusions.jsonl":
                event_ordinal = {
                    event["sourceEventID"]: event["sourceSessionOrdinal"]
                    for event in semantic_events
                }
                for event_id in sorted(private_target_ids):
                    privacy = privacy_policy[event_id]
                    rows.append({
                        "schemaVersion": 1,
                        "reason": "privacy_policy_excluded_target",
                        "sourceEventID": event_id,
                        "sourceSessionOrdinal": event_ordinal[event_id],
                        "privacyReason": privacy["reason"],
                        **(
                            {"privacyGroupID": privacy["groupID"]}
                            if "groupID" in privacy else {}
                        ),
                    })
            write_jsonl(temporary / name, rows)

        manifest = {
            "schemaVersion": 1,
            "artifactType": "phase1_multi_session_corpus",
            "assemblerVersion": ASSEMBLER_VERSION,
            "corpusID": corpus_id,
            # Compatibility alias for the existing tokenizer boundary.
            "sessionID": corpus_id,
            **baseline_contract,
            "coverage": {
                "semanticEventKinds": ["read", "write"],
                "structuralGapIsSemanticEvent": False,
                "betweenSessionPolicy": "explicit_unknown_coverage_gap",
                "gapCount": len(gaps),
            },
            "privacy": {
                "policyVersion": PRIVACY_POLICY_VERSION,
                "policyArtifact": "privacy-policy.json",
                "modelFacingPolicy": "redact_context_and_exclude_target",
                "redactionSerialized": PRIVACY_REDACTION_SERIALIZED,
                "redactedEventCount": len(privacy_policy),
                "excludedTargetCount": len(private_target_ids),
                "rawAndSemanticEvidenceModified": False,
            },
            "blocking": {
                "policy": "fixed_chronological_example_count",
                "blockSize": block_size,
                "blockCount": len(blocks),
                "blocks": blocks,
            },
            "sources": corpus_sources,
            "counts": {
                "sessions": len(sessions),
                "convertedEvents": len(semantic_events),
                "contextBlocks": len(context_blocks),
                "coverageGaps": len(gaps),
                "examples": len(assembled_examples),
                "targetExclusions": len(private_target_ids) + sum(
                    session["manifest"].get("counts", {}).get("targetExclusions", 0)
                    for session in sessions
                ),
                "contextExclusions": sum(
                    session["manifest"].get("counts", {}).get("contextExclusions", 0)
                    for session in sessions
                ),
                "rejections": sum(
                    session["manifest"].get("counts", {}).get("rejections", 0)
                    for session in sessions
                ),
            },
        }
        manifest["artifactDigestsSHA256"] = {
            name: sha256(temporary / name)
            for name in (
                "events.jsonl", "context-blocks.jsonl", "examples.jsonl", "gaps.jsonl",
                "privacy-policy.json",
                "target-exclusions.jsonl", "context-exclusions.jsonl", "rejections.jsonl",
            )
        }
        (temporary / "corpus.json").write_bytes(json_bytes(manifest))
        # dataset.json lets existing tokenizer tooling consume this artifact,
        # while artifactType prevents it being confused with one causal compile.
        (temporary / "dataset.json").write_bytes(json_bytes(manifest))
        os.replace(temporary, output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return manifest


def audit(directory: Path) -> dict[str, Any]:
    directory = directory.expanduser().resolve()
    manifest = load_json(directory / "corpus.json")
    multi_session = (
        manifest.get("artifactType") == "phase1_multi_session_corpus"
        and manifest.get("assemblerVersion") in {"phase1-corpus-v1", ASSEMBLER_VERSION}
        and manifest.get("conversionVersion") == "phase1-causal-v14"
    )
    raw_episode = all(
        manifest.get(key) == value
        for key, value in SUPPORTED_RAW_EPISODE_EXPERIMENT_CONTRACT.items()
    ) and (
        manifest.get("rawEpisodeArchitecture", {}).get(
            "productionConsumesRegressionFixture"
        ) is False
        and manifest.get("rawEpisodeArchitecture", {}).get("sourceAuthority")
        == "immutable_raw_journals"
    )
    if not (multi_session or raw_episode):
        raise ValueError("unsupported Phase 1 corpus manifest")
    if load_json(directory / "dataset.json") != manifest:
        raise ValueError("dataset.json compatibility manifest disagrees with corpus.json")
    for name, expected in manifest.get("artifactDigestsSHA256", {}).items():
        if sha256(directory / name) != expected:
            raise ValueError(f"artifact digest mismatch: {name}")
    events = load_jsonl(directory / "events.jsonl")
    blocks = load_jsonl(directory / "context-blocks.jsonl")
    gaps = load_jsonl(directory / "gaps.jsonl")
    examples = load_jsonl(directory / "examples.jsonl")
    privacy_policy_artifact = (
        load_json(directory / "privacy-policy.json")
        if manifest.get("assemblerVersion") == ASSEMBLER_VERSION else None
    )
    event_by_id = {event.get("sourceEventID"): event for event in events}
    block_by_id = {block.get("contextBlockID"): block for block in blocks}
    if len(event_by_id) != len(events) or None in event_by_id:
        raise ValueError("semantic event IDs are invalid or duplicated")
    if len(block_by_id) != len(blocks) or None in block_by_id:
        raise ValueError("context block IDs are invalid or duplicated")
    if any(event.get("kind") not in {"read", "write"} for event in events):
        raise ValueError("corpus semantic event ontology is not READ/WRITE-only")
    semantic_blocks = {
        block_id: block for block_id, block in block_by_id.items()
        if block.get("contextBlockType") == "semantic_event"
    }
    gap_blocks = {
        block_id: block for block_id, block in block_by_id.items()
        if block.get("contextBlockType") == "coverage_gap"
    }
    if set(semantic_blocks) != set(event_by_id):
        raise ValueError("semantic events and context blocks disagree")
    redacted_count = 0
    for event_id, event in event_by_id.items():
        block = semantic_blocks[event_id]
        privacy = block.get("privacyRedaction")
        if privacy is None:
            if block.get("serialized") != event.get("serialized"):
                raise ValueError(f"semantic serialization disagrees: {event_id}")
            continue
        redacted_count += 1
        if not (
            block.get("serialized") == PRIVACY_REDACTION_SERIALIZED
            and privacy.get("policyVersion") == PRIVACY_POLICY_VERSION
            and isinstance(privacy.get("reason"), str)
            and privacy.get("sourceSerializedSHA256")
            == hashlib.sha256(event["serialized"].encode()).hexdigest()
        ):
            raise ValueError(f"privacy redaction disagrees: {event_id}")
    if {gap.get("contextBlockID") for gap in gaps} != set(gap_blocks):
        raise ValueError("coverage gaps and context blocks disagree")

    prior_time: str | None = None
    seen_examples: set[str] = set()
    expected_block_members: dict[str, list[str]] = {}
    for ordinal, example in enumerate(examples):
        example_id = example.get("exampleID")
        if not isinstance(example_id, str) or example_id in seen_examples:
            raise ValueError("example IDs are invalid or duplicated")
        seen_examples.add(example_id)
        if example.get("chronologicalOrdinal") != ordinal:
            raise ValueError(f"example {example_id} chronological ordinal disagrees")
        began_at = example.get("targetBeganAt")
        if not isinstance(began_at, str) or (prior_time is not None and began_at < prior_time):
            raise ValueError("examples are not chronological")
        prior_time = began_at
        target_id = example.get("targetEventID")
        if target_id not in event_by_id or event_by_id[target_id].get("kind") != "write":
            raise ValueError(f"example {example_id} target is not a semantic WRITE")
        context_ids = example.get("contextBlockIDs")
        if not isinstance(context_ids, list) or any(value not in block_by_id for value in context_ids):
            raise ValueError(f"example {example_id} has invalid context blocks")
        semantic_ids = [
            value for value in context_ids
            if block_by_id[value].get("contextBlockType") == "semantic_event"
        ]
        if semantic_ids != example.get("contextEventIDs"):
            raise ValueError(f"example {example_id} semantic lineage disagrees")
        context_text = "\n".join(block_by_id[value]["serialized"] for value in context_ids)
        model_input = example["query"] if not context_text else context_text + "\n" + example["query"]
        if example.get("context") != context_text or example.get("modelInput") != model_input:
            raise ValueError(f"example {example_id} model input disagrees with lineage")
        block_id = example.get("experimentBlockID")
        if not isinstance(block_id, str):
            raise ValueError(f"example {example_id} has no experiment block")
        expected_block_members.setdefault(block_id, []).append(example_id)
    if raw_episode:
        declared_blocks = load_jsonl(directory / "episode-blocks.jsonl")
        if not declared_blocks:
            raise ValueError("raw episode corpus has no experiment blocks")
        expected_block_ids = [
            f"block-{ordinal:04d}"
            for ordinal in range(1, len(declared_blocks) + 1)
        ]
        if [block.get("blockID") for block in declared_blocks] != expected_block_ids:
            raise ValueError("raw episode block IDs are not contiguous")
        for ordinal, block in enumerate(declared_blocks):
            example_ids = block.get("exampleIDs")
            if not isinstance(example_ids, list) or not example_ids:
                raise ValueError("raw episode block has no examples")
            if block.get("exampleCount") != len(example_ids):
                raise ValueError("raw episode block count disagrees")
            if len(example_ids) > 50 or (
                ordinal < len(declared_blocks) - 1 and len(example_ids) != 50
            ):
                raise ValueError("raw episode blocks do not follow the frozen size-50 rule")
    else:
        declared_blocks = manifest.get("blocking", {}).get("blocks", [])
    if [block.get("blockID") for block in declared_blocks] != list(expected_block_members):
        raise ValueError("experiment block order disagrees")
    for block in declared_blocks:
        if block.get("exampleIDs") != expected_block_members[block["blockID"]]:
            raise ValueError(f"experiment block membership disagrees: {block['blockID']}")
    counts = manifest.get("counts", {})
    expected_counts = {
        "convertedEvents": len(events),
        "examples": len(examples),
    }
    if multi_session:
        expected_counts.update({
            "contextBlocks": len(blocks),
            "coverageGaps": len(gaps),
        })
    for key, value in expected_counts.items():
        if counts.get(key) != value:
            raise ValueError(f"manifest count disagrees: {key}")
    if manifest.get("assemblerVersion") == ASSEMBLER_VERSION:
        declared_private = {
            entry.get("sourceEventID"): entry
            for entry in privacy_policy_artifact.get("events", [])
        }
        if not (
            manifest.get("privacy", {}).get("policyVersion") == PRIVACY_POLICY_VERSION
            and manifest.get("privacy", {}).get("redactedEventCount") == redacted_count
            and manifest.get("privacy", {}).get("policyArtifact") == "privacy-policy.json"
            and set(declared_private)
            == {
                event_id for event_id, block in semantic_blocks.items()
                if block.get("privacyRedaction") is not None
            }
        ):
            raise ValueError("privacy manifest disagrees")
        for event_id, entry in declared_private.items():
            redaction = semantic_blocks[event_id]["privacyRedaction"]
            if not (
                entry.get("reason") == redaction.get("reason")
                and entry.get("groupID") == redaction.get("groupID")
            ):
                raise ValueError(f"privacy policy lineage disagrees: {event_id}")
    elif redacted_count:
        raise ValueError("legacy corpus contains privacy redactions")
    if raw_episode:
        # The frozen episode artifact remains byte-for-byte immutable. Expose
        # its separately hashed episode-block ledger only in the in-memory
        # experiment contract consumed by provider-neutral runners.
        manifest = {
            **manifest,
            "blocking": {
                "blockSize": 50,
                "blockCount": len(declared_blocks),
                "blocks": declared_blocks,
            },
            "experimentAdapter": {
                "version": RAW_EPISODE_EXPERIMENT_ADAPTER_VERSION,
                "sourceArtifactType": manifest["artifactType"],
                "episodeBlocksSHA256": sha256(directory / "episode-blocks.jsonl"),
                "semanticInterpretationChanged": False,
            },
        }
    return manifest

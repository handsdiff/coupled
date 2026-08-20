#!/usr/bin/env python3
"""Provider-neutral chronological Phase 1 experiment contract and mock runner."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from phase1_corpus import audit as audit_corpus
from phase1_training_contract import (
    PackedDataset,
    TrainingContractError,
    adapt_dataset_to_tinker,
    sha256,
    validate_packed_dataset,
)


RUNNER_VERSION = "phase1-prequential-v3"
ARM_FROZEN_QWEN = "frozen_qwen3.5_9b_base"
ARM_FROZEN_FRONTIER = "frozen_gpt_5.6_sol_xhigh"
ARM_PERSONALIZED_QWEN = "personalized_qwen3.5_9b_base"
ARMS = (ARM_FROZEN_QWEN, ARM_FROZEN_FRONTIER, ARM_PERSONALIZED_QWEN)
PASTE_MARKER = "<|paste|>"
QWEN_GENERATION_CONTRACT = {
    "temperature": 0.6,
    "seed": 17,
}


def prospective_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return blocks scored after the first training-only warm-up block."""

    if len(blocks) < 2:
        raise TrainingContractError(
            "prospective evaluation requires one warm-up and one evaluation block"
        )
    return blocks[1:]


def prospective_example_ids(blocks: list[dict[str, Any]]) -> list[str]:
    return [
        example_id
        for block in prospective_blocks(blocks)
        for example_id in block["exampleIDs"]
    ]


def update_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return blocks followed by a useful personalized checkpoint.

    The terminal block is scored but never trained: no later block consumes
    that checkpoint, so the update would add cost without experimental value.
    """

    if len(blocks) < 2:
        raise TrainingContractError(
            "prequential training requires one warm-up and one terminal scored block"
        )
    return blocks[:-1]
TINKER_TRAINING_CONTRACT = {
    "algorithm": "lora",
    "rank": 32,
    "trainAttention": True,
    "trainMLP": True,
    "trainUnembedding": True,
    "seed": 17,
    "batchExamplesPerForwardBackward": 1,
    "epochsPerCumulativeUpdate": 1,
    "checkpointTTLSeconds": 7 * 24 * 60 * 60,
    "optimizer": {
        "type": "adam",
        "learningRate": 0.0002,
        "beta1": 0.9,
        "beta2": 0.95,
        "epsilon": 1e-12,
        "weightDecay": 0.0,
        "gradientClipNorm": 1.0,
    },
    "exampleOrder": {
        "version": "phase1-prequential-order-v1",
        "algorithm": "sha256_ascending_then_example_id",
        "material": "phase1-prequential:{seed}:{updateOrdinal}:{exampleID}",
    },
}


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode()


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise TrainingContractError(f"{path} contains a non-object")
                rows.append(value)
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("wb") as handle:
        for row in rows:
            handle.write(canonical_bytes(row))


def target_text(target: dict[str, Any]) -> str:
    """Render the tokenizer-independent Phase 1 completion target."""

    pieces: list[str] = []
    for segment in target.get("segments", []):
        kind = segment.get("type")
        if kind == "authored_text" and isinstance(segment.get("content"), str):
            pieces.append(segment["content"])
        elif kind == "paste":
            pieces.append(PASTE_MARKER)
        else:
            raise TrainingContractError(f"unsupported target segment: {kind}")
    result = "".join(pieces)
    if not result:
        raise TrainingContractError("Phase 1 target is empty")
    return result


def semantic_model_input(
    corpus_directory: Path,
    example: dict[str, Any],
    context_plan: dict[str, Any],
) -> str:
    """Reconstruct and verify the shared semantic input for non-Qwen arms.

    The packed Qwen IDs and this text are two projections of the same frozen
    context plan.  Known private events must occur only through their explicit
    serialized redaction overrides.
    """

    blocks = {
        value["contextBlockID"]: value
        for value in load_jsonl(corpus_directory / "context-blocks.jsonl")
    }
    serialized: list[str] = []
    for retained in context_plan["retainedContextBlocks"]:
        text = retained.get("serializedOverride")
        if text is None:
            text = blocks[retained["contextBlockID"]]["serialized"]
        serialized.append(text)
    context = "\n".join(serialized)
    body = example["query"] if not context else context + "\n" + example["query"]
    value = context_plan["taskInstruction"] + "\n" + body
    if hashlib.sha256(value.encode()).hexdigest() != context_plan[
        "semanticModelInputSHA256"
    ]:
        raise TrainingContractError("semantic model input digest disagrees")

    events = {
        event["sourceEventID"]: event
        for event in load_jsonl(corpus_directory / "events.jsonl")
    }
    privacy = json.loads((corpus_directory / "privacy-policy.json").read_text())
    for entry in privacy["events"]:
        event_id = entry["sourceEventID"]
        if events[event_id]["serialized"] in value:
            raise TrainingContractError(
                f"unredacted private context survived for {event_id}"
            )
    return value


@dataclass(frozen=True)
class Score:
    prediction: str
    weighted_nll_sum: float | None
    weighted_token_count: int | None
    latency_seconds: float
    cost_usd: float
    finish_reason: str


class ExperimentBackend(Protocol):
    name: str

    def score(
        self,
        arm: str,
        example: dict[str, Any],
        packed_row: dict[str, Any],
        context_plan: dict[str, Any],
        checkpoint_id: str | None,
    ) -> Score: ...

    def update(
        self,
        prior_checkpoint_id: str | None,
        cumulative_rows: list[dict[str, Any]],
        update_ordinal: int,
        epochs: int,
    ) -> dict[str, Any]: ...


class DeterministicMockBackend:
    """No-network backend for proving ordering, masks, and lineage only."""

    name = "deterministic_mock_no_model_calls"

    def score(
        self,
        arm: str,
        example: dict[str, Any],
        packed_row: dict[str, Any],
        context_plan: dict[str, Any],
        checkpoint_id: str | None,
    ) -> Score:
        material = {
            "arm": arm,
            "exampleID": example["exampleID"],
            "plan": context_plan["semanticModelInputSHA256"],
            "checkpoint": checkpoint_id,
        }
        digest = canonical_sha256(material)
        target_count = packed_row["targetTokenCount"]
        # Deterministic non-evidence numbers exercise macro and micro plumbing.
        per_token = 4.0 + int(digest[:4], 16) / 65535
        return Score(
            prediction=f"<mock:{digest[:16]}>",
            weighted_nll_sum=per_token * target_count,
            weighted_token_count=target_count,
            latency_seconds=0.0,
            cost_usd=0.0,
            finish_reason="mock",
        )

    def update(
        self,
        prior_checkpoint_id: str | None,
        cumulative_rows: list[dict[str, Any]],
        update_ordinal: int,
        epochs: int,
    ) -> dict[str, Any]:
        checkpoint_id = "mock_checkpoint_" + canonical_sha256({
            "parent": prior_checkpoint_id,
            "ordinal": update_ordinal,
            "epochs": epochs,
            "examples": [row["exampleID"] for row in cumulative_rows],
        })[:24]
        return {
            "checkpointID": checkpoint_id,
            "optimizerStateID": "mock_optimizer_" + checkpoint_id.removeprefix("mock_checkpoint_"),
            "submittedPositions": epochs * sum(len(row["inputIDs"]) - 1 for row in cumulative_rows),
            "lossBearingTokenPresentations": epochs * sum(row["targetTokenCount"] for row in cumulative_rows),
            "costUSD": 0.0,
        }


def validate_inputs(
    corpus_directory: Path, packed_directory: Path
) -> tuple[dict[str, Any], list[dict[str, Any]], PackedDataset, dict[str, dict[str, Any]]]:
    corpus = audit_corpus(corpus_directory)
    examples = load_jsonl(corpus_directory / "examples.jsonl")
    packed = validate_packed_dataset(packed_directory)
    source = packed.manifest.get("source", {})
    if source.get("sessionID") != corpus.get("corpusID"):
        raise TrainingContractError("packed dataset is not derived from this corpus")
    source_digests = source.get("digestsSHA256", {})
    expected_source = {
        "dataset.json": sha256(corpus_directory / "dataset.json"),
        "examples.jsonl": sha256(corpus_directory / "examples.jsonl"),
        "events.jsonl": sha256(corpus_directory / "events.jsonl"),
        **(
            {
                "context-blocks.jsonl": sha256(corpus_directory / "context-blocks.jsonl"),
                "privacy-policy.json": sha256(corpus_directory / "privacy-policy.json"),
            }
            if packed.manifest.get("packerVersion") == "phase1-token-pack-v7" else {}
        ),
    }
    if source_digests != expected_source:
        raise TrainingContractError("packed source digests do not match the corpus")
    plans_path = packed_directory / "context-plans.jsonl"
    expected_plan_digest = packed.manifest.get("artifactDigestsSHA256", {}).get(
        "context-plans.jsonl"
    )
    if not expected_plan_digest or sha256(plans_path) != expected_plan_digest:
        raise TrainingContractError("shared semantic context plans are missing or changed")
    plans = load_jsonl(plans_path)
    plan_by_id = {plan.get("exampleID"): plan for plan in plans}
    if len(plan_by_id) != len(plans) or len(plans) != len(examples):
        raise TrainingContractError("shared context plan IDs do not match the corpus")
    if [row["exampleID"] for row in packed.rows] != [row["exampleID"] for row in examples]:
        raise TrainingContractError("corpus and packed example order differs")
    adapt_dataset_to_tinker(packed)  # Exhaustive causal-shift invariant.
    return corpus, examples, packed, plan_by_id


def run_mock_experiment(
    corpus_directory: Path,
    packed_directory: Path,
    output: Path,
    epochs_per_update: int = 1,
) -> dict[str, Any]:
    if epochs_per_update <= 0:
        raise TrainingContractError("epochs per update must be positive")
    if output.exists():
        raise TrainingContractError(f"output already exists: {output}")
    corpus, examples, packed, plan_by_id = validate_inputs(
        corpus_directory.resolve(), packed_directory.resolve()
    )
    packed_by_id = {row["exampleID"]: row for row in packed.rows}
    blocks = corpus["blocking"]["blocks"]
    backend: ExperimentBackend = DeterministicMockBackend()
    results: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    current_checkpoint: str | None = None
    scored_personalized: set[str] = set()
    cumulative_ids: list[str] = []
    presentations = {example["exampleID"]: 0 for example in examples}

    evaluation_block_ids = {block["blockID"] for block in prospective_blocks(blocks)}
    update_block_ids = {block["blockID"] for block in update_blocks(blocks)}
    for block in blocks:
        block_id = block["blockID"]
        block_ids = block["exampleIDs"]
        if block_id in evaluation_block_ids:
            for arm in ARMS:
                arm_checkpoint = current_checkpoint if arm == ARM_PERSONALIZED_QWEN else None
                for example_id in block_ids:
                    example = examples[next(
                        index for index, value in enumerate(examples)
                        if value["exampleID"] == example_id
                    )]
                    packed_row = packed_by_id[example_id]
                    score = backend.score(
                        arm, example, packed_row, plan_by_id[example_id], arm_checkpoint
                    )
                    results.append({
                        "schemaVersion": 1,
                        "runnerVersion": RUNNER_VERSION,
                        "blockID": block_id,
                        "arm": arm,
                        "exampleID": example_id,
                        "targetEventID": example["targetEventID"],
                        "target": example["target"],
                        "prediction": score.prediction,
                        "weightedNLLSum": score.weighted_nll_sum,
                        "weightedTokenCount": score.weighted_token_count,
                        "latencySeconds": score.latency_seconds,
                        "costUSD": score.cost_usd,
                        "finishReason": score.finish_reason,
                        "checkpointID": arm_checkpoint,
                        "semanticContextPlanSHA256": plan_by_id[example_id][
                            "semanticModelInputSHA256"
                        ],
                        "application": example.get("conditioningState", {})
                            .get("destination", {}).get("appName"),
                    })
                    if arm == ARM_PERSONALIZED_QWEN:
                        scored_personalized.add(example_id)
        if block_id in evaluation_block_ids and not set(block_ids).issubset(scored_personalized):
            raise AssertionError("personalized update attempted before complete block scoring")
        cumulative_ids.extend(block_ids)
        if block_id not in update_block_ids:
            continue
        cumulative_rows = [packed_by_id[value] for value in cumulative_ids]
        update = backend.update(
            current_checkpoint, cumulative_rows, len(updates) + 1, epochs_per_update
        )
        for example_id in cumulative_ids:
            presentations[example_id] += epochs_per_update
        update_record = {
            "schemaVersion": 1,
            "runnerVersion": RUNNER_VERSION,
            "afterBlockID": block_id,
            "parentCheckpointID": current_checkpoint,
            "trainingPolicy": "warm_start_then_train_full_cumulative_corpus",
            "epochsOverCumulativeCorpus": epochs_per_update,
            "cumulativeExampleIDs": list(cumulative_ids),
            "cumulativeExampleCount": len(cumulative_ids),
            "perExamplePresentationsAfterUpdate": {
                value: presentations[value] for value in cumulative_ids
            },
            **update,
        }
        updates.append(update_record)
        current_checkpoint = update["checkpointID"]

    summaries = []
    for arm in ARMS:
        rows = [row for row in results if row["arm"] == arm]
        nll_rows = [row for row in rows if row["weightedNLLSum"] is not None]
        token_count = sum(row["weightedTokenCount"] for row in nll_rows)
        summaries.append({
            "arm": arm,
            "examples": len(rows),
            "macroExampleAverageNLL": sum(
                row["weightedNLLSum"] / row["weightedTokenCount"] for row in nll_rows
            ) / len(nll_rows),
            "microTargetTokenNLL": sum(row["weightedNLLSum"] for row in nll_rows)
            / token_count,
            "weightedTokens": token_count,
            "costUSD": sum(row["costUSD"] for row in rows),
        })

    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        write_jsonl(temporary / "scores.jsonl", results)
        write_jsonl(temporary / "updates.jsonl", updates)
        manifest = {
            "schemaVersion": 1,
            "runnerVersion": RUNNER_VERSION,
            "status": "passed_mock_only_not_scientific_evidence",
            "backend": backend.name,
            "corpus": {
                "corpusID": corpus["corpusID"],
                "corpusSHA256": sha256(corpus_directory / "corpus.json"),
                "examplesSHA256": sha256(corpus_directory / "examples.jsonl"),
            },
            "packing": {
                "packingSHA256": sha256(packed_directory / "packing.json"),
                "packedExamplesSHA256": sha256(packed_directory / "packed-examples.jsonl"),
                "contextPlansSHA256": sha256(packed_directory / "context-plans.jsonl"),
            },
            "protocol": {
                "arms": list(ARMS),
                "blockCount": len(blocks),
                "warmupBlockID": blocks[0]["blockID"],
                "warmupBlocksAreTrainingOnly": True,
                "prospectiveEvaluationBlockIDs": [
                    block["blockID"] for block in prospective_blocks(blocks)
                ],
                "scoreCompleteBlockBeforeUpdate": True,
                "personalizedUpdatePolicy": (
                    "warm_start_then_train_full_cumulative_corpus_except_terminal_block"
                ),
                "terminalBlockReceivesPostScoreUpdate": False,
                "qwenGenerationContract": QWEN_GENERATION_CONTRACT,
                "epochsPerUpdate": epochs_per_update,
                "frozenArmsNeverUpdate": True,
                "contextPlanSharedAcrossArms": True,
            },
            "counts": {
                "examples": len(examples),
                "scores": len(results),
                "updates": len(updates),
                "lossBearingTargetTokenOccurrences": sum(
                    row["targetTokenCount"] for row in packed.rows
                ),
                "lossBearingTokenPresentationsAcrossUpdates": sum(
                    update["lossBearingTokenPresentations"] for update in updates
                ),
                "submittedTrainingPositionsAcrossUpdates": sum(
                    update["submittedPositions"] for update in updates
                ),
            },
            "finalPerExamplePresentations": presentations,
            "summaries": summaries,
        }
        manifest["artifactDigestsSHA256"] = {
            "scores.jsonl": sha256(temporary / "scores.jsonl"),
            "updates.jsonl": sha256(temporary / "updates.jsonl"),
        }
        (temporary / "experiment.json").write_bytes(canonical_bytes(manifest))
        os.replace(temporary, output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return manifest


def audit_mock_experiment(
    directory: Path, corpus_directory: Path, packed_directory: Path
) -> dict[str, Any]:
    directory = directory.expanduser().resolve()
    manifest = json.loads((directory / "experiment.json").read_text(encoding="utf-8"))
    if not (
        manifest.get("runnerVersion") == RUNNER_VERSION
        and manifest.get("status") == "passed_mock_only_not_scientific_evidence"
        and manifest.get("backend") == DeterministicMockBackend.name
        and manifest.get("protocol", {}).get("qwenGenerationContract")
        == QWEN_GENERATION_CONTRACT
        and manifest.get("protocol", {}).get(
            "terminalBlockReceivesPostScoreUpdate"
        ) is False
        and manifest.get("protocol", {}).get("personalizedUpdatePolicy")
        == "warm_start_then_train_full_cumulative_corpus_except_terminal_block"
    ):
        raise TrainingContractError("not a supported passing mock experiment")
    for name, expected in manifest.get("artifactDigestsSHA256", {}).items():
        if sha256(directory / name) != expected:
            raise TrainingContractError(f"experiment artifact digest mismatch: {name}")
    corpus, examples, packed, plan_by_id = validate_inputs(
        corpus_directory.expanduser().resolve(), packed_directory.expanduser().resolve()
    )
    if not (
        manifest["corpus"]["corpusID"] == corpus["corpusID"]
        and manifest["corpus"]["corpusSHA256"] == sha256(corpus_directory / "corpus.json")
        and manifest["packing"]["packingSHA256"] == sha256(packed_directory / "packing.json")
    ):
        raise TrainingContractError("experiment source binding disagrees")
    example_by_id = {value["exampleID"]: value for value in examples}
    packed_by_id = {value["exampleID"]: value for value in packed.rows}
    scores = load_jsonl(directory / "scores.jsonl")
    updates = load_jsonl(directory / "updates.jsonl")
    expected_score_keys = []
    for block in prospective_blocks(corpus["blocking"]["blocks"]):
        for arm in ARMS:
            expected_score_keys.extend(
                (block["blockID"], arm, example_id)
                for example_id in block["exampleIDs"]
            )
    actual_score_keys = [
        (row.get("blockID"), row.get("arm"), row.get("exampleID")) for row in scores
    ]
    if actual_score_keys != expected_score_keys:
        raise TrainingContractError("score order or coverage violates the block protocol")
    checkpoint_after_block: dict[str, str] = {}
    for score in scores:
        example_id = score["exampleID"]
        example = example_by_id[example_id]
        packed_row = packed_by_id[example_id]
        if score.get("target") != example.get("target"):
            raise TrainingContractError(f"score target changed: {example_id}")
        if score.get("semanticContextPlanSHA256") != plan_by_id[example_id].get(
            "semanticModelInputSHA256"
        ):
            raise TrainingContractError(f"score context plan changed: {example_id}")
        if score.get("weightedTokenCount") != packed_row["targetTokenCount"]:
            raise TrainingContractError(f"score loss-bearing count changed: {example_id}")
        if score["arm"] != ARM_PERSONALIZED_QWEN and score.get("checkpointID") is not None:
            raise TrainingContractError("a frozen arm received a checkpoint")
    cumulative_ids: list[str] = []
    presentations = {value["exampleID"]: 0 for value in examples}
    parent: str | None = None
    epochs = manifest["protocol"]["epochsPerUpdate"]
    all_blocks = corpus["blocking"]["blocks"]
    training_blocks = update_blocks(all_blocks)
    for block, update in zip(training_blocks, updates, strict=True):
        cumulative_ids.extend(block["exampleIDs"])
        if not (
            update.get("afterBlockID") == block["blockID"]
            and update.get("parentCheckpointID") == parent
            and update.get("cumulativeExampleIDs") == cumulative_ids
        ):
            raise TrainingContractError("update lineage or cumulative membership disagrees")
        for example_id in cumulative_ids:
            presentations[example_id] += epochs
        if update.get("perExamplePresentationsAfterUpdate") != {
            value: presentations[value] for value in cumulative_ids
        }:
            raise TrainingContractError("per-example presentation accounting disagrees")
        checkpoint = update.get("checkpointID")
        if not isinstance(checkpoint, str):
            raise TrainingContractError("update has no checkpoint")
        checkpoint_after_block[block["blockID"]] = checkpoint
        parent = checkpoint
    if len(updates) != len(training_blocks):
        raise TrainingContractError("update count differs from nonterminal block count")
    prior_checkpoint: str | None = checkpoint_after_block[all_blocks[0]["blockID"]]
    for block in prospective_blocks(all_blocks):
        personalized = [
            row for row in scores
            if row["blockID"] == block["blockID"] and row["arm"] == ARM_PERSONALIZED_QWEN
        ]
        if any(row.get("checkpointID") != prior_checkpoint for row in personalized):
            raise TrainingContractError("personalized block used the wrong prior checkpoint")
        if block["blockID"] in checkpoint_after_block:
            prior_checkpoint = checkpoint_after_block[block["blockID"]]
    if manifest.get("finalPerExamplePresentations") != presentations:
        raise TrainingContractError("final presentation accounting disagrees")
    if manifest.get("counts", {}).get("scores") != len(scores):
        raise TrainingContractError("score count disagrees")
    return manifest

#!/usr/bin/env python3
"""Prepare a minimal native-loss Inkling free-generation stability plan."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from phase1_experiment import canonical_bytes
from phase1_inkling import (
    GENERATION_CONTRACT,
    INKLING_CONTRACT_VERSION,
    INKLING_MODEL,
    REASONING_CONDITIONS,
    TRAINING_CONTRACT,
    InklingContractError,
    load_experiment_blocks,
    load_jsonl,
    sha256,
)
from phase1_training_contract import git_revision


PLAN_VERSION = "phase1-inkling-native-loss-prequential-plan-v2"
TRAIN_BLOCKS = 4
PROBES_PER_APPLICATION = 1
TRAIN_PER_MILLION = Decimal("1.73")
PREFILL_PER_MILLION = Decimal("0.58")
SAMPLE_PER_MILLION = Decimal("1.44")
CHECKPOINT_RESERVE_USD = Decimal("1.50")
MINIMUM_AUTOMATIC_VALIDITY_RATE = Decimal("0.98")
MAXIMUM_GENERATION_RETRIES = 1
MAXIMUM_TRAINING_BLOCK_RESTARTS = 1
PRICING_AS_OF = "2026-08-20"


def iso8601() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def money(tokens: int, rate: Decimal) -> Decimal:
    return Decimal(tokens) * rate / Decimal(1_000_000)


def money_string(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.000001")))


def application(example: dict[str, Any]) -> str:
    query = json.loads(example["query"])
    return str(query["destination"]["appName"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--semantic-pack", required=True, type=Path)
    parser.add_argument("--inkling-pack", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--tinker-project-id", required=True)
    parser.add_argument("--hard-ceiling-usd", required=True, type=Decimal)
    arguments = parser.parse_args()

    output = arguments.output.expanduser().resolve()
    if output.exists():
        raise InklingContractError(f"stability plan already exists: {output}")
    if arguments.hard_ceiling_usd <= 0:
        raise InklingContractError("hard ceiling must be positive")
    if set(REASONING_CONDITIONS) != {"reasoning_off"}:
        raise InklingContractError("stability probe must be reasoning-off only")

    project = Path(__file__).resolve().parent.parent
    corpus_path = arguments.corpus.expanduser().resolve()
    semantic_pack = arguments.semantic_pack.expanduser().resolve()
    pack_path = arguments.inkling_pack.expanduser().resolve()
    corpus = json.loads((corpus_path / "corpus.json").read_text(encoding="utf-8"))
    packing = json.loads((pack_path / "packing.json").read_text(encoding="utf-8"))
    blocks = load_experiment_blocks(corpus_path)
    if len(blocks) <= TRAIN_BLOCKS:
        raise InklingContractError("corpus lacks a never-trained probe block")

    examples = {
        value["exampleID"]: value
        for value in load_jsonl(corpus_path / "examples.jsonl")
    }
    rows = load_jsonl(pack_path / "reasoning_off-packed-examples.jsonl")
    by_id = {value["exampleID"]: value for value in rows}
    expected_ids = [value for block in blocks for value in block["exampleIDs"]]
    if [value["exampleID"] for value in rows] != expected_ids:
        raise InklingContractError("Inkling pack order differs from corpus")

    training_blocks = blocks[:TRAIN_BLOCKS]
    training_ids = [value for block in training_blocks for value in block["exampleIDs"]]
    probe_candidates: dict[str, list[str]] = {}
    for example_id in training_blocks[0]["exampleIDs"]:
        probe_candidates.setdefault(application(examples[example_id]), []).append(
            example_id
        )
    if set(probe_candidates) != {"ChatGPT", "Code", "Google Chrome", "Obsidian"}:
        raise InklingContractError("training blocks lack an expected application")
    probe_ids: list[str] = []
    for app in sorted(probe_candidates):
        candidates = sorted(
            probe_candidates[app],
            key=lambda value: (by_id[value]["modelInputTokenCount"], value),
        )
        if len(candidates) < PROBES_PER_APPLICATION:
            raise InklingContractError(f"insufficient {app} stability probes")
        probe_ids.extend(candidates[:PROBES_PER_APPLICATION])

    training_positions = sum(len(by_id[value]["inputIDs"]) - 1 for value in training_ids)
    training_loss_tokens = sum(by_id[value]["targetTokenCount"] for value in training_ids)
    evaluation_blocks = blocks[1 : TRAIN_BLOCKS + 1]
    base_probe_calls = len(probe_ids)
    evaluation_calls = sum(len(value["exampleIDs"]) for value in evaluation_blocks)
    sample_calls = base_probe_calls + evaluation_calls
    sample_prefill = sum(
        by_id[value]["modelInputTokenCount"] for value in probe_ids
    ) + sum(
        by_id[value]["modelInputTokenCount"]
        for block in evaluation_blocks
        for value in block["exampleIDs"]
    )
    sample_ceiling = sample_calls * GENERATION_CONTRACT["maximumTokensByCondition"][
        "reasoning_off"
    ]
    base_costs = {
        "trainingUSD": money(training_positions, TRAIN_PER_MILLION),
        "generationPrefillUSD": money(sample_prefill, PREFILL_PER_MILLION),
        "sampleCeilingUSD": money(sample_ceiling, SAMPLE_PER_MILLION),
        "checkpointReserveUSD": CHECKPOINT_RESERVE_USD,
    }
    training_block_positions = [
        sum(len(by_id[value]["inputIDs"]) - 1 for value in block["exampleIDs"])
        for block in training_blocks
    ]
    generation_ids = [
        *probe_ids,
        *[
            example_id
            for block in evaluation_blocks
            for example_id in block["exampleIDs"]
        ],
    ]
    maximum_generation_prompt = max(
        by_id[value]["modelInputTokenCount"] for value in generation_ids
    )
    recovery_costs = {
        "maximumGenerationRetryReserveUSD": money(
            maximum_generation_prompt, PREFILL_PER_MILLION
        )
        + money(
            GENERATION_CONTRACT["maximumTokensByCondition"]["reasoning_off"],
            SAMPLE_PER_MILLION,
        ),
        "maximumTrainingBlockRestartReserveUSD": money(
            max(training_block_positions), TRAIN_PER_MILLION
        ),
    }
    projected_without_recovery = sum(base_costs.values(), Decimal(0))
    projected = projected_without_recovery + sum(recovery_costs.values(), Decimal(0))
    if projected > arguments.hard_ceiling_usd:
        raise InklingContractError("projected stability probe exceeds hard ceiling")

    implementation_files = [
        "scripts/phase1_experiment.py",
        "scripts/phase1_inkling.py",
        "scripts/phase1_tinker_overfit_contract.py",
        "scripts/phase1_training_contract.py",
        "scripts/prepare-phase1-inkling-stability.py",
        "scripts/run-phase1-inkling-prequential.py",
        "scripts/run-phase1-inkling-stability.py",
        "scripts/inkling-requirements.txt",
    ]
    plan = {
        "schemaVersion": 1,
        "planVersion": PLAN_VERSION,
        "status": "review_only_not_authorization",
        "createdAt": iso8601(),
        "purpose": "validate_native_loss_repair_and_collect_full_personalized_prequential_comparison",
        "causalChange": {
            "changed": "custom_partial_response_loss_to_native_full_response_loss",
            "heldFixed": [
                "reasoning_off",
                "same_corpus_and_chronology",
                "optimizer_batch_size_10",
                "micro_normalized_batch_loss",
                "rank_32_attention_mlp_unembedding_lora",
                "adam_2e-4_and_existing_optimizer_parameters",
                "one_pass_append_only_updates",
                "temperature_0.6_seed_17_max_512",
            ],
        },
        "provider": {
            "projectID": arguments.tinker_project_id,
            "model": INKLING_MODEL,
            "reasoningConditions": REASONING_CONDITIONS,
            "generationContract": GENERATION_CONTRACT,
            "trainingContract": TRAINING_CONTRACT,
        },
        "protocol": {
            "trainingBlockIDs": [value["blockID"] for value in training_blocks],
            "trainingExampleCountsAfterStage": [0, 50, 100, 150, 200],
            "evaluationBlockIDsAfterUpdate": [
                value["blockID"] for value in evaluation_blocks
            ],
            "evaluationExampleCountsAfterUpdate": [
                len(value["exampleIDs"]) for value in evaluation_blocks
            ],
            "probeSource": "first_training_block_only_never_scored",
            "probeExampleIDs": probe_ids,
            "probesPerApplication": PROBES_PER_APPLICATION,
            "baseProbeCalls": base_probe_calls,
            "personalizedEvaluationCalls": evaluation_calls,
            "freeGenerationStages": 1 + len(evaluation_blocks),
            "validityGate": "strict_base_gate_then_block_level_structural_validity_review",
            "minimumAutomaticValidityRate": str(MINIMUM_AUTOMATIC_VALIDITY_RATE),
            "failurePolicy": "complete_block_then_pause_before_next_training_update_on_material_deterioration",
            "invalidGenerationScoring": "zero_in_all_174_holistic_and_deterministic_comparisons",
            "recoveryContract": {
                "maximumAutomaticGenerationRetriesTotal": MAXIMUM_GENERATION_RETRIES,
                "maximumAutomaticTrainingBlockRestartsTotal": MAXIMUM_TRAINING_BLOCK_RESTARTS,
                "generationRecovery": "resume_committed_rows_retry_nonmutating_generation_once",
                "trainingRecovery": "restore_parent_optimizer_checkpoint_restart_whole_block_once",
                "costAccounting": "planned_base_plus_abandoned_attempt_maximums_within_hard_ceiling",
            },
            "targetLikelihoodCalls": 0,
            "frozenDuplicateArm": False,
            "reasoningOnArm": False,
            "terminalTrainingBlock": blocks[TRAIN_BLOCKS - 1]["blockID"],
        },
        "operations": {
            "trainedExamples": len(training_ids),
            "trainingCalls": sum(
                len(value["exampleIDs"]) // TRAINING_CONTRACT["optimizerBatchExamples"]
                for value in training_blocks
            ),
            "trainingSubmittedPositions": training_positions,
            "nativeLossTokenPresentations": training_loss_tokens,
            "sampleCalls": sample_calls,
            "baseProbeCalls": base_probe_calls,
            "personalizedEvaluationCalls": evaluation_calls,
            "generationPrefillTokens": sample_prefill,
            "sampledTokenCeiling": sample_ceiling,
            "samplerCheckpointSaves": len(training_blocks),
            "optimizerStateCheckpointSaves": len(training_blocks),
        },
        "pricing": {
            "asOf": PRICING_AS_OF,
            "perMillionUSD": {
                "training": str(TRAIN_PER_MILLION),
                "prefill": str(PREFILL_PER_MILLION),
                "sample": str(SAMPLE_PER_MILLION),
            },
            "projectedUSD": {
                **{key: money_string(value) for key, value in base_costs.items()},
                **{key: money_string(value) for key, value in recovery_costs.items()},
                "totalBeforeRecoveryReserve": money_string(projected_without_recovery),
                "totalIncludingReserve": money_string(projected),
            },
            "hardCeilingUSD": money_string(arguments.hard_ceiling_usd),
        },
        "source": {
            "corpusDirectory": str(corpus_path),
            "corpusID": corpus["corpusID"],
            "corpusSHA256": sha256(corpus_path / "corpus.json"),
            "examplesSHA256": sha256(corpus_path / "examples.jsonl"),
            "semanticPackDirectory": str(semantic_pack),
            "semanticPackingSHA256": sha256(semantic_pack / "packing.json"),
            "inklingPackDirectory": str(pack_path),
            "inklingPackingSHA256": sha256(pack_path / "packing.json"),
            "inklingRowsSHA256": sha256(
                pack_path / "reasoning_off-packed-examples.jsonl"
            ),
            "inklingContractVersion": INKLING_CONTRACT_VERSION,
            "inklingPackerVersion": packing["packerVersion"],
        },
        "implementation": {
            "codeRevision": git_revision(project),
            "fileDigestsSHA256": {
                value: sha256(project / value) for value in implementation_files
            },
        },
    }
    plan["planID"] = "inkling_stability_" + hashlib.sha256(
        canonical_bytes(plan)
    ).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(canonical_bytes(plan))
    print(json.dumps(plan, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

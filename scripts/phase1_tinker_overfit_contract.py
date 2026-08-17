#!/usr/bin/env python3
"""Local construction and validation for the Phase 1 Tinker overfit plan.

This module may import the pinned Tinker SDK to construct ``Datum`` objects,
but it deliberately has no service-client, authentication, network, training,
sampling, or checkpoint code.  It turns the provider-neutral causal-shift
contract into the exact SDK payload shape and freezes a bounded execution plan
for a later, separately authorized runner.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from phase1_training_contract import TrainingContractError, TinkerDatumContract


CONTRACT_SCHEMA_VERSION = 1
PINNED_TINKER_SDK_VERSION = "0.25.0"
BASE_MODEL = "Qwen/Qwen3.5-9B-Base"
TRAINING_SEED = 17
LORA_RANK = 32
EPOCHS = 20
BATCH_EXAMPLES = 1
CHECKPOINT_TTL_SECONDS = 7 * 24 * 60 * 60

OPTIMIZER = {
    "type": "adam",
    "learningRate": 0.0002,
    "beta1": 0.9,
    "beta2": 0.95,
    "epsilon": 1e-12,
    "weightDecay": 0.0,
    "gradientClipNorm": 1.0,
}

PRICING_AS_OF = "2026-08-17"
TRAINING_PRICE_PER_MILLION_USD = Decimal("1.463")
PREFILL_PRICE_PER_MILLION_USD = Decimal("0.66")
SAMPLE_PRICE_PER_MILLION_USD = Decimal("1.995")
CHECKPOINT_AND_STORAGE_RESERVE_USD = Decimal("1.00")
HARD_MAXIMUM_PROJECTED_COST_USD = Decimal("20.00")


@dataclass(frozen=True)
class SDKDatumValidation:
    example_id: str
    length: int
    weighted_positions: int
    model_input_sha256: str
    target_tokens_sha256: str
    weights_sha256: str


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256((encoded + "\n").encode()).hexdigest()


def deterministic_epoch_order(
    example_ids: list[str], epoch: int, seed: int = TRAINING_SEED
) -> list[str]:
    """Return a runtime-independent deterministic shuffle.

    Hash sorting avoids relying on a particular Python PRNG implementation.
    The epoch is one-indexed in both the manifest and the later runner.
    """

    if epoch <= 0:
        raise TrainingContractError("epoch must be positive")
    if len(set(example_ids)) != len(example_ids):
        raise TrainingContractError("training example IDs must be unique")

    def key(example_id: str) -> tuple[bytes, str]:
        material = f"phase1-tinker-smoke:{seed}:{epoch}:{example_id}".encode()
        return hashlib.sha256(material).digest(), example_id

    return sorted(example_ids, key=key)


def build_and_validate_sdk_datums(
    contracts: list[TinkerDatumContract],
) -> tuple[list[Any], list[SDKDatumValidation], str]:
    """Construct real Tinker Datums and exhaustively round-trip their payloads."""

    try:
        import numpy as np
        import tinker
    except ImportError as error:
        raise TrainingContractError(
            "SDK Datum validation requires scripts/tinker-requirements.txt"
        ) from error

    sdk_version = importlib.metadata.version("tinker")
    if sdk_version != PINNED_TINKER_SDK_VERSION:
        raise TrainingContractError(
            f"Tinker SDK {sdk_version} does not match pin {PINNED_TINKER_SDK_VERSION}"
        )

    datums: list[Any] = []
    validations: list[SDKDatumValidation] = []
    for contract in contracts:
        target_array = np.asarray(contract.target_tokens, dtype=np.int64)
        weight_array = np.asarray(contract.weights, dtype=np.float32)
        datum = tinker.Datum(
            model_input=tinker.ModelInput.from_ints(
                tokens=contract.model_input_token_ids
            ),
            loss_fn_inputs={
                "target_tokens": tinker.TensorData.from_numpy(target_array),
                "weights": tinker.TensorData.from_numpy(weight_array),
            },
        )

        actual_input = datum.model_input.to_ints()
        actual_targets = datum.loss_fn_inputs["target_tokens"].to_numpy()
        actual_weights = datum.loss_fn_inputs["weights"].to_numpy()
        if actual_input != contract.model_input_token_ids:
            raise TrainingContractError(
                f"{contract.example_id} SDK ModelInput changed token IDs"
            )
        if actual_targets.dtype != np.dtype("int64") or actual_targets.tolist() != (
            contract.target_tokens
        ):
            raise TrainingContractError(
                f"{contract.example_id} SDK target TensorData changed tokens or dtype"
            )
        if actual_weights.dtype != np.dtype("float32") or actual_weights.tolist() != (
            contract.weights
        ):
            raise TrainingContractError(
                f"{contract.example_id} SDK weight TensorData changed values or dtype"
            )
        if not (
            len(actual_input)
            == len(actual_targets)
            == len(actual_weights)
            == contract.length
        ):
            raise TrainingContractError(
                f"{contract.example_id} SDK Datum arrays differ in length"
            )
        weighted_positions = int(np.count_nonzero(actual_weights))
        if weighted_positions != contract.weighted_positions:
            raise TrainingContractError(
                f"{contract.example_id} SDK Datum changed the loss mask"
            )
        validations.append(
            SDKDatumValidation(
                example_id=contract.example_id,
                length=contract.length,
                weighted_positions=weighted_positions,
                model_input_sha256=canonical_sha256(actual_input),
                target_tokens_sha256=canonical_sha256(actual_targets.tolist()),
                weights_sha256=canonical_sha256(actual_weights.tolist()),
            )
        )
        datums.append(datum)

    return datums, validations, sdk_version


def select_reload_examples(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Select small normal/paste and longest cases for checkpoint reload parity."""

    paste_rows = [row for row in rows if row["pasteActionCount"] > 0]
    ordinary_rows = [row for row in rows if row["pasteActionCount"] == 0]
    if not paste_rows or not ordinary_rows:
        raise TrainingContractError(
            "reload validation requires both ordinary and paste examples"
        )
    selected = [
        min(ordinary_rows, key=lambda row: (len(row["inputIDs"]), row["exampleID"])),
        min(paste_rows, key=lambda row: (len(row["inputIDs"]), row["exampleID"])),
        max(rows, key=lambda row: (len(row["inputIDs"]), row["exampleID"])),
    ]
    if len({row["exampleID"] for row in selected}) != 3:
        raise TrainingContractError("reload validation selection is not distinct")
    return selected


def build_execution_plan(
    rows: list[dict[str, Any]], contracts: list[TinkerDatumContract]
) -> dict[str, Any]:
    if len(rows) != len(contracts) or not rows:
        raise TrainingContractError("rows and shifted Datum contracts must align")
    contract_by_id = {contract.example_id: contract for contract in contracts}
    if len(contract_by_id) != len(contracts):
        raise TrainingContractError("shifted Datum IDs must be unique")
    if [row["exampleID"] for row in rows] != [
        contract.example_id for contract in contracts
    ]:
        raise TrainingContractError("row and shifted Datum order differs")

    example_ids = [contract.example_id for contract in contracts]
    epoch_orders = []
    for epoch in range(1, EPOCHS + 1):
        ordered_ids = deterministic_epoch_order(example_ids, epoch)
        epoch_orders.append(
            {
                "epoch": epoch,
                "exampleOrderSHA256": canonical_sha256(ordered_ids),
                "firstExampleID": ordered_ids[0],
                "lastExampleID": ordered_ids[-1],
            }
        )

    training_tokens_per_epoch = sum(contract.length for contract in contracts)
    training_tokens = training_tokens_per_epoch * EPOCHS
    reload_rows = select_reload_examples(rows)

    # Full-sequence logprobs include the target and establish weighted NLL.
    full_sequence_tokens = sum(len(row["inputIDs"]) for row in rows)
    reload_logprob_tokens = sum(len(row["inputIDs"]) for row in reload_rows)

    # Greedy generation receives only causal model input, never the target.
    all_generation_prefill = sum(row["modelInputTokenCount"] for row in rows)
    reload_generation_prefill = sum(
        row["modelInputTokenCount"] for row in reload_rows
    )
    generation_output_ceiling = sum(
        row["targetTokenCount"] + 2 for row in rows
    ) + sum(row["targetTokenCount"] + 2 for row in reload_rows)

    prefill_tokens = (
        full_sequence_tokens  # base weighted NLL
        + full_sequence_tokens  # trained weighted NLL
        + reload_logprob_tokens  # reloaded-state parity
        + all_generation_prefill  # trained exact-generation audit
        + reload_generation_prefill  # reloaded-state generation parity
    )

    training_cost = (
        Decimal(training_tokens) * TRAINING_PRICE_PER_MILLION_USD
        / Decimal(1_000_000)
    )
    prefill_cost = (
        Decimal(prefill_tokens) * PREFILL_PRICE_PER_MILLION_USD
        / Decimal(1_000_000)
    )
    sampling_cost = (
        Decimal(generation_output_ceiling) * SAMPLE_PRICE_PER_MILLION_USD
        / Decimal(1_000_000)
    )
    projected_with_reserve = (
        training_cost
        + prefill_cost
        + sampling_cost
        + CHECKPOINT_AND_STORAGE_RESERVE_USD
    )
    if projected_with_reserve > HARD_MAXIMUM_PROJECTED_COST_USD:
        raise TrainingContractError("frozen plan exceeds its projected cost ceiling")

    return {
        "purpose": "mechanical_training_harness_validation_not_phase1_hypothesis_evidence",
        "model": {
            "baseModel": BASE_MODEL,
            "method": "lora",
            "rank": LORA_RANK,
            "seed": TRAINING_SEED,
            "trainMLP": True,
            "trainAttention": True,
            "trainUnembedding": True,
        },
        "training": {
            "epochs": EPOCHS,
            "examplesPerEpoch": len(contracts),
            "batchExamples": BATCH_EXAMPLES,
            "optimizerSteps": len(contracts) * EPOCHS,
            "lossFunction": "cross_entropy",
            "optimizer": OPTIMIZER,
            "ordering": {
                "algorithm": "sha256_sort",
                "seed": TRAINING_SEED,
                "epochOrders": epoch_orders,
            },
            "automaticRetryOrExtraEpochsAllowed": False,
        },
        "evaluation": {
            "baseline": "weighted_token_nll_from_base_sampling_client_compute_logprobs",
            "final": "weighted_token_nll_from_trained_sampler_compute_logprobs",
            "generation": {
                "examples": "all",
                "temperature": 0.0,
                "maximumTokens": "targetTokenCount_plus_2",
                "expected": "exact_target_tokens_and_eos_termination",
                "pasteRequirement": "all_grounded_paste_examples_generate_the_exact_five_token_marker",
            },
            "acceptance": {
                "maximumFinalToBaselineWeightedNLLRatio": 0.35,
                "exactGreedyTargetRate": 1.0,
                "eosTerminationRate": 1.0,
                "automaticRetryOnFailure": False,
            },
        },
        "checkpoints": {
            "ttlSeconds": CHECKPOINT_TTL_SECONDS,
            "required": [
                "trained_sampler_weights",
                "full_optimizer_state",
                "sampler_weights_resaved_after_optimizer_state_reload",
            ],
            "reloadVerification": {
                "exampleIDs": [row["exampleID"] for row in reload_rows],
                "requiresExactWeightedLogprobParity": True,
                "requiresExactGreedyTokenAndStopReasonParity": True,
            },
        },
        "operationCeilings": {
            "training": {
                "forwardBackwardCalls": len(contracts) * EPOCHS,
                "optimizerSteps": len(contracts) * EPOCHS,
                "submittedTokens": training_tokens,
            },
            "evaluation": {
                "computeLogprobCalls": len(rows) * 2 + len(reload_rows),
                "sampleCalls": len(rows) + len(reload_rows),
                "prefillTokens": prefill_tokens,
                "maximumSampledTokens": generation_output_ceiling,
            },
            "checkpointSaves": 3,
            "automaticRetries": 0,
        },
        "costCeiling": {
            "currency": "USD",
            "pricingAsOf": PRICING_AS_OF,
            "trainingPricePerMillionTokens": str(
                TRAINING_PRICE_PER_MILLION_USD
            ),
            "prefillPricePerMillionTokens": str(PREFILL_PRICE_PER_MILLION_USD),
            "samplingPricePerMillionTokens": str(SAMPLE_PRICE_PER_MILLION_USD),
            "trainingProjected": str(training_cost.quantize(Decimal("0.000001"))),
            "prefillProjected": str(prefill_cost.quantize(Decimal("0.000001"))),
            "samplingProjected": str(sampling_cost.quantize(Decimal("0.000001"))),
            "checkpointAndStorageReserve": str(
                CHECKPOINT_AND_STORAGE_RESERVE_USD
            ),
            "projectedIncludingReserve": str(
                projected_with_reserve.quantize(Decimal("0.000001"))
            ),
            "hardMaximumProjected": str(HARD_MAXIMUM_PROJECTED_COST_USD),
            "requiresPriceReverificationBeforeExecution": True,
            "actualBillingMustBeRecorded": True,
        },
    }

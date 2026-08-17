#!/usr/bin/env python3
"""Deterministic checks for the local-only Tinker overfit contract."""

from __future__ import annotations

from phase1_tinker_overfit_contract import (
    EPOCHS,
    build_and_validate_sdk_datums,
    build_execution_plan,
    deterministic_epoch_order,
)
from phase1_training_contract import TinkerDatumContract


def contract(example_id: str, length: int, weighted: int) -> TinkerDatumContract:
    tokens = list(range(length))
    return TinkerDatumContract(
        example_id=example_id,
        model_input_token_ids=tokens,
        target_tokens=[value + 1 for value in tokens],
        weights=[0.0] * (length - weighted) + [1.0] * weighted,
        weighted_positions=weighted,
    )


contracts = [
    contract("ordinary-short", 4, 2),
    contract("paste-short", 6, 3),
    contract("ordinary-long", 9, 2),
]
rows = [
    {
        "exampleID": "ordinary-short",
        "inputIDs": [0, 1, 2, 3, 4],
        "modelInputTokenCount": 3,
        "targetTokenCount": 2,
        "pasteActionCount": 0,
    },
    {
        "exampleID": "paste-short",
        "inputIDs": [0, 1, 2, 3, 4, 5, 6],
        "modelInputTokenCount": 4,
        "targetTokenCount": 3,
        "pasteActionCount": 1,
    },
    {
        "exampleID": "ordinary-long",
        "inputIDs": list(range(10)),
        "modelInputTokenCount": 8,
        "targetTokenCount": 2,
        "pasteActionCount": 0,
    },
]

datums, validations, version = build_and_validate_sdk_datums(contracts)
assert version == "0.25.0"
assert len(datums) == len(validations) == 3
assert [datum.model_input.to_ints() for datum in datums] == [
    item.model_input_token_ids for item in contracts
]
assert sum(item.weighted_positions for item in validations) == 7

first = deterministic_epoch_order([item.example_id for item in contracts], 1)
second = deterministic_epoch_order([item.example_id for item in contracts], 1)
assert first == second
assert set(first) == {item.example_id for item in contracts}

plan = build_execution_plan(rows, contracts)
assert plan["training"]["epochs"] == EPOCHS
assert plan["training"]["automaticRetryOrExtraEpochsAllowed"] is False
assert plan["operationCeilings"]["training"]["submittedTokens"] == (
    sum(item.length for item in contracts) * EPOCHS
)
assert plan["operationCeilings"]["automaticRetries"] == 0
assert len(plan["training"]["ordering"]["epochOrders"]) == EPOCHS
assert plan["costCeiling"]["requiresPriceReverificationBeforeExecution"] is True
assert float(plan["costCeiling"]["projectedIncludingReserve"]) < float(
    plan["costCeiling"]["hardMaximumProjected"]
)
assert plan["checkpoints"]["reloadVerification"]["exampleIDs"] == [
    "ordinary-short",
    "paste-short",
    "ordinary-long",
]

print("Phase 1 Tinker overfit-contract checks passed")

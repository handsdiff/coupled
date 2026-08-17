#!/usr/bin/env python3
"""Small deterministic checks for the Phase 1 causal-shift adapter."""

from __future__ import annotations

from phase1_training_contract import (
    TrainingContractError,
    adapt_row_to_tinker,
)


def row(labels: list[int]) -> dict[str, object]:
    return {
        "exampleID": "synthetic-example",
        "inputIDs": [10, 11, 12, 13],
        "labels": labels,
        "modelInputTokenCount": 2,
        "targetTokenCount": 2,
    }


datum = adapt_row_to_tinker(row([-100, -100, 12, 13]))
assert datum.model_input_token_ids == [10, 11, 12]
assert datum.target_tokens == [11, 12, 13]
assert datum.weights == [0.0, 1.0, 1.0]
assert datum.weighted_positions == 2

try:
    adapt_row_to_tinker(row([-100, -100, 99, 13]))
except TrainingContractError as error:
    assert "causal shift mismatch" in str(error)
else:
    raise AssertionError("adapter accepted a wrong loss-bearing target")

bad_count = row([-100, -100, 12, 13])
bad_count["targetTokenCount"] = 1
try:
    adapt_row_to_tinker(bad_count)
except TrainingContractError as error:
    assert "targetTokenCount" in str(error)
else:
    raise AssertionError("adapter accepted an incorrect target-token count")

print("Phase 1 training-contract checks passed")

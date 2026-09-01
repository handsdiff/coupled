#!/usr/bin/env python3
"""No-network regression checks for the Qwen 35B native loss and runner rules."""

from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from phase1_qwen35_native import MODEL_SPECS, RENDERER_CONTRACT
from phase1_training_contract import TrainingContractError, adapt_row_to_tinker


PROJECT = Path(__file__).resolve().parent.parent
RUNNER_PATH = PROJECT / "scripts/run-phase1-qwen35-experiment.py"
SPEC = importlib.util.spec_from_file_location("phase1_qwen35_runner", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUNNER
SPEC.loader.exec_module(RUNNER)

assert RENDERER_CONTRACT == "coupled/qwen35-model-specific-exact-completion-v2"
assert MODEL_SPECS["qwen35_base"]["rendererStrategy"] == "raw_completion_eos"
assert MODEL_SPECS["qwen35_base"]["officialRecommendedRenderers"] == ["role_colon"]
assert (
    MODEL_SPECS["qwen36_hybrid"]["rendererStrategy"]
    == "qwen3_5_disable_thinking_exact_content"
)
assert "qwen3_5_disable_thinking" in MODEL_SPECS["qwen36_hybrid"][
    "officialRecommendedRenderers"
]
assert (
    MODEL_SPECS["qwen35_base"]["renderer"]
    != MODEL_SPECS["qwen36_hybrid"]["renderer"]
)


def native_row(labels: list[int]) -> dict[str, object]:
    return {
        "exampleID": "exact-leading-space",
        "inputIDs": [100, 101, 102, 248046],
        "labels": labels,
        "modelInputTokenCount": 2,
        "targetTokenCount": 2,
    }


contract = adapt_row_to_tinker(native_row([-100, -100, 102, 248046]))
assert contract.model_input_token_ids == [100, 101, 102]
assert contract.target_tokens == [101, 102, 248046]
assert contract.weights == [0.0, 1.0, 1.0]
assert contract.weighted_positions == 2

for wrong in (
    [-100, -100, 102, 248044],  # tokenizer EOS instead of native terminator
    [-100, 101, 102, 248046],  # loss leaks into prompt/native envelope
):
    try:
        adapt_row_to_tinker(native_row(wrong))
    except TrainingContractError:
        pass
    else:
        raise AssertionError(f"accepted invalid native labels: {wrong}")

blocks = [
    {"blockID": f"block-{index}", "exampleIDs": [f"e{index}a", f"e{index}b"]}
    for index in range(1, 4)
]
assert RUNNER.expected_updates(blocks) == ["block-1", "block-2"]
assert RUNNER.expected_scores(blocks) == [
    ("block-2", "frozen", "e2a"),
    ("block-2", "frozen", "e2b"),
    ("block-2", "personalized", "e2a"),
    ("block-2", "personalized", "e2b"),
    ("block-3", "frozen", "e3a"),
    ("block-3", "frozen", "e3b"),
    ("block-3", "personalized", "e3a"),
    ("block-3", "personalized", "e3b"),
]
assert RUNNER.deterministic_order(["b", "a", "c"], 2, 17) == RUNNER.deterministic_order(
    ["c", "b", "a"], 2, 17
)

plan = {
    "tinker": {
        "projectedCostUSD": {"conservativeUncachedIncludingReserve": "1.000000"}
    }
}
manifest = {
    "inflightOperation": {
        "kind": "score_generation_and_optional_nll",
        "modelKey": "qwen35_base",
        "blockID": "block-2",
        "arm": "frozen",
        "exampleID": "e2a",
        "maximumReplayCostUSD": "0.100000",
    },
    "abandonedAttempts": [],
}
scores = {"qwen35_base": [], "qwen36_hybrid": []}
updates = {"qwen35_base": [], "qwen36_hybrid": []}
assert RUNNER.recover_interruption(
    manifest=manifest,
    scores_by_model=scores,
    updates_by_model=updates,
    plan=plan,
    maximum_usd=Decimal("1.10"),
)
assert manifest["abandonedAttempts"][0]["kind"] == "score_retry"
assert manifest["budget"]["maximumProjectedUSD"] == "1.100000"

manifest = {
    "activeUpdate": {
        "modelKey": "qwen36_hybrid",
        "updateOrdinal": 3,
        "maximumReplayCostUSD": "0.250000",
    },
    "abandonedAttempts": [],
}
try:
    RUNNER.recover_interruption(
        manifest=manifest,
        scores_by_model=scores,
        updates_by_model=updates,
        plan=plan,
        maximum_usd=Decimal("1.20"),
    )
except TrainingContractError as error:
    assert "exceeds authorized" in str(error)
else:
    raise AssertionError("training restart exceeded the hard ceiling")

# Sampling APIs may omit the terminating token.  The runner must rely on both
# returned IDs and stop reason, then add only a local parsing terminator.
assert RUNNER.normalize_stop_reason(SimpleNamespace(value="stop")) == "stop"

print("Phase 1 Qwen 35B contract checks passed")

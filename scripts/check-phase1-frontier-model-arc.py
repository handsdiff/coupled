#!/usr/bin/env python3
"""No-network checks for the Phase 1 frontier-model arc contract."""

from __future__ import annotations

from phase1_frontier_model_arc import MODEL_SPECS, add_prediction_metrics, model_spec, summarize_scores


def main() -> int:
    if [value["key"] for value in MODEL_SPECS] != [
        "gpt-5-high", "gpt-5.2-xhigh", "gpt-5.3-xhigh", "gpt-5.4-xhigh", "gpt-5.5-xhigh"
    ]:
        raise AssertionError("frontier-model order changed")
    if model_spec("gpt-5-high")["reasoningEffort"] != "high":
        raise AssertionError("GPT-5 reasoning effort changed")
    if any(
        value["reasoningEffort"] != "xhigh" for value in MODEL_SPECS[1:]
    ):
        raise AssertionError("later frontier reasoning effort changed")
    row = add_prediction_metrics({
        "target": "hello",
        "prediction": "hell",
        "pasteActionCount": 0,
        "latencySeconds": 2.0,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 3,
            "total_tokens": 13,
            "output_tokens_details": {"reasoning_tokens": 2},
        },
    })
    summary = summarize_scores([row, {**row, "latencySeconds": 4.0}])
    if not (
        summary["examples"] == 2
        and summary["latency"]["medianSeconds"] == 3.0
        and summary["latency"]["meanSeconds"] == 3.0
        and summary["usage"]["input_tokens"] == 20
        and summary["usage"]["reasoning_tokens"] == 4
        and summary["generatedCompletion"]["correctPrefix"]["totalCharacters"] == 8
    ):
        raise AssertionError("frontier-model summary changed")
    print("Phase 1 frontier-model arc checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Shared contract for the GPT-5.6 Sol Phase 1 history-window ablation."""

from __future__ import annotations

import statistics
from decimal import Decimal
from pathlib import Path
from typing import Any

from phase1_frontier_model_arc import (
    add_prediction_metrics,
    atomic_json,
    load_jsonl,
    percentile,
    sha256,
)
from phase1_prediction_metrics import summarize_prediction_metrics


ABLATION_VERSION = "phase1-gpt56-context-window-v2"
PLAN_VERSION = "phase1-gpt56-context-window-plan-v2"
RUNNER_VERSION = "phase1-gpt56-context-window-runner-v2"
AUDIT_VERSION = "phase1-gpt56-context-window-audit-v2"
MODEL = {
    "route": "chatgpt/gpt-5.6-sol",
    "requestedModel": "gpt-5.6-sol",
    "reasoningEffort": "xhigh",
}
WINDOWS = {
    "32k": 32768,
    "128k": 131072,
}
API_EQUIVALENT_PRICES_PER_MILLION_USD = {
    "input": "5.00",
    "cachedInput": "0.50",
    "output": "30.00",
}
PRICING_AS_OF = "2026-08-20"
PRICING_URL = "https://developers.openai.com/api/docs/models/gpt-5.6-sol"


class ContextWindowError(RuntimeError):
    pass


def api_equivalent_cost(rows: list[dict[str, Any]]) -> dict[str, Any]:
    input_tokens = sum(int(value.get("usage", {}).get("input_tokens") or 0) for value in rows)
    output_tokens = sum(int(value.get("usage", {}).get("output_tokens") or 0) for value in rows)
    input_cost = Decimal(input_tokens) * Decimal("5.00") / Decimal(1_000_000)
    output_cost = Decimal(output_tokens) * Decimal("30.00") / Decimal(1_000_000)
    return {
        "billingMode": "api_equivalent_not_subscription_charge",
        "pricingAsOf": PRICING_AS_OF,
        "pricingSource": PRICING_URL,
        "pricesPerMillionUSD": API_EQUIVALENT_PRICES_PER_MILLION_USD,
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "inputUSD": str(input_cost.quantize(Decimal("0.000001"))),
        "outputUSD": str(output_cost.quantize(Decimal("0.000001"))),
        "totalUSD": str((input_cost + output_cost).quantize(Decimal("0.000001"))),
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    normalized = [add_prediction_metrics(value) if "predictionMetrics" not in value else value for value in rows]
    latencies = [float(value["latencySeconds"]) for value in normalized]
    reasoning_tokens = sum(
        int(value.get("usage", {}).get("output_tokens_details", {}).get("reasoning_tokens") or 0)
        for value in normalized
    )
    return {
        "examples": len(normalized),
        "generatedCompletion": summarize_prediction_metrics(
            [value["predictionMetrics"] for value in normalized]
        ),
        "latency": {
            "medianSeconds": statistics.median(latencies),
            "meanSeconds": statistics.mean(latencies),
            "p90Seconds": percentile(latencies, 0.90),
            "p95Seconds": percentile(latencies, 0.95),
            "minimumSeconds": min(latencies),
            "maximumSeconds": max(latencies),
            "totalSeconds": sum(latencies),
        },
        "reasoningTokens": reasoning_tokens,
        "apiEquivalentCost": api_equivalent_cost(normalized),
    }


__all__ = [
    "ABLATION_VERSION",
    "AUDIT_VERSION",
    "ContextWindowError",
    "MODEL",
    "PLAN_VERSION",
    "RUNNER_VERSION",
    "WINDOWS",
    "api_equivalent_cost",
    "atomic_json",
    "load_jsonl",
    "sha256",
    "summarize",
]

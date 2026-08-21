#!/usr/bin/env python3
"""Shared contract for the Phase 1 frozen frontier-model arc."""

from __future__ import annotations

import hashlib
import json
import os
import statistics
from pathlib import Path
from typing import Any

from phase1_experiment import canonical_bytes
from phase1_prediction_metrics import score_prediction, summarize_prediction_metrics


ARC_VERSION = "phase1-frontier-model-arc-v1"
PLAN_VERSION = "phase1-frontier-model-arc-plan-v1"
RUNNER_VERSION = "phase1-frontier-model-arc-runner-v1"
AUDIT_VERSION = "phase1-frontier-model-arc-audit-v1"
MODEL_SPECS = (
    {
        "key": "gpt-5-high",
        "route": "chatgpt/gpt-5",
        "requestedModel": "gpt-5",
        "reasoningEffort": "high",
    },
    {
        "key": "gpt-5.2-xhigh",
        "route": "chatgpt/gpt-5.2",
        "requestedModel": "gpt-5.2",
        "reasoningEffort": "xhigh",
    },
    {
        "key": "gpt-5.3-xhigh",
        "route": "chatgpt/gpt-5.3",
        "requestedModel": "gpt-5.3",
        "reasoningEffort": "xhigh",
    },
    {
        "key": "gpt-5.4-xhigh",
        "route": "chatgpt/gpt-5.4",
        "requestedModel": "gpt-5.4",
        "reasoningEffort": "xhigh",
    },
    {
        "key": "gpt-5.5-xhigh",
        "route": "chatgpt/gpt-5.5",
        "requestedModel": "gpt-5.5",
        "reasoningEffort": "xhigh",
    },
)


class FrontierArcError(RuntimeError):
    pass


def model_spec(key: str) -> dict[str, str]:
    matches = [value for value in MODEL_SPECS if value["key"] == key]
    if len(matches) != 1:
        raise FrontierArcError(f"unknown model key: {key}")
    return dict(matches[0])


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    with path.open("ab") as handle:
        handle.write(canonical_bytes(value))
        handle.flush()
        os.fsync(handle.fileno())


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(canonical_bytes(value))
    os.replace(temporary, path)


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = quantile * (len(ordered) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def summarize_scores(rows: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = [float(value["latencySeconds"]) for value in rows]
    metric_rows = [value["predictionMetrics"] for value in rows]
    usage_keys = ("input_tokens", "output_tokens", "total_tokens")
    usage = {
        key: sum(int(row.get("usage", {}).get(key) or 0) for row in rows)
        for key in usage_keys
    }
    usage["reasoning_tokens"] = sum(
        int(row.get("usage", {}).get("output_tokens_details", {}).get("reasoning_tokens") or 0)
        for row in rows
    )
    return {
        "examples": len(rows),
        "generatedCompletion": summarize_prediction_metrics(metric_rows),
        "latency": {
            "observations": len(latencies),
            "medianSeconds": statistics.median(latencies) if latencies else None,
            "meanSeconds": statistics.mean(latencies) if latencies else None,
            "p90Seconds": percentile(latencies, 0.90),
            "p95Seconds": percentile(latencies, 0.95),
            "minimumSeconds": min(latencies) if latencies else None,
            "maximumSeconds": max(latencies) if latencies else None,
            "totalSeconds": sum(latencies),
        },
        "usage": usage,
    }


def add_prediction_metrics(record: dict[str, Any]) -> dict[str, Any]:
    return {
        **record,
        "predictionMetrics": score_prediction(
            record["target"],
            record["prediction"],
            target_paste_actions=int(record["pasteActionCount"]),
        ),
    }

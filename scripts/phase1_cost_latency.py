#!/usr/bin/env python3
"""Cost and latency accounting for the Phase 1 provider experiment."""

from __future__ import annotations

import csv
import io
from decimal import Decimal
from typing import Any


COST_LATENCY_VERSION = "phase1-cost-latency-v1"
MILLION = Decimal(1_000_000)


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        raise ValueError("cannot calculate a percentile of no values")
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def latency_summary(rows: list[dict[str, Any]], semantics: str) -> dict[str, Any]:
    values = [float(row["latencySeconds"]) for row in rows]
    if not values:
        return {"observations": 0, "semantics": semantics}
    return {
        "observations": len(values),
        "semantics": semantics,
        "totalSeconds": sum(values),
        "meanSeconds": sum(values) / len(values),
        "medianSeconds": percentile(values, 0.5),
        "p90Seconds": percentile(values, 0.9),
        "p95Seconds": percentile(values, 0.95),
        "minimumSeconds": min(values),
        "maximumSeconds": max(values),
    }


def priced_tokens(tokens: int, rate_per_million: Decimal) -> Decimal:
    return Decimal(tokens) * rate_per_million / MILLION


def decimal_string(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.000001")))


def tinker_arm_cost(
    rows: list[dict[str, Any]],
    *,
    prefill_rate: Decimal,
    sample_rate: Decimal,
) -> dict[str, Any]:
    nll_prefill = sum(int(row["fullSequenceTokenCount"]) for row in rows)
    generation_prefill = sum(int(row["modelInputTokenCount"]) for row in rows)
    sampled = sum(len(row["predictionTokenIDs"]) for row in rows)
    nll_cost = priced_tokens(nll_prefill, prefill_rate)
    generation_prefill_cost = priced_tokens(generation_prefill, prefill_rate)
    sampled_cost = priced_tokens(sampled, sample_rate)
    generation_cost = generation_prefill_cost + sampled_cost
    combined_cost = nll_cost + generation_cost
    return {
        "pricingBasis": "provider_plan_frozen_rates",
        "examples": len(rows),
        "tokens": {
            "targetLikelihoodPrefill": nll_prefill,
            "generationPrefill": generation_prefill,
            "sampledObserved": sampled,
        },
        "estimatedUSD": {
            "targetLikelihood": decimal_string(nll_cost),
            "generationPrefill": decimal_string(generation_prefill_cost),
            "generationSampled": decimal_string(sampled_cost),
            "generationOnly": decimal_string(generation_cost),
            "combinedScoringAndGeneration": decimal_string(combined_cost),
            "combinedMeanPerExample": decimal_string(combined_cost / len(rows)),
            "generationOnlyMeanPerExample": decimal_string(generation_cost / len(rows)),
        },
    }


def build_cost_latency_report(
    *,
    frontier_rows: list[dict[str, Any]],
    frozen_qwen_rows: list[dict[str, Any]],
    personalized_qwen_rows: list[dict[str, Any]],
    updates: list[dict[str, Any]],
    frontier_manifest: dict[str, Any],
    tinker_manifest: dict[str, Any],
    provider_plan: dict[str, Any] | None,
    verified_tinker_charge_usd: Decimal | None,
) -> dict[str, Any]:
    tinker_score_rows = frozen_qwen_rows + personalized_qwen_rows
    observed_prefill = sum(
        int(row["fullSequenceTokenCount"]) + int(row["modelInputTokenCount"])
        for row in tinker_score_rows
    )
    observed_sampled = sum(len(row["predictionTokenIDs"]) for row in tinker_score_rows)
    observed_training_positions = sum(
        int(row["submittedPositions"]) for row in updates
    )
    recorded_usage = tinker_manifest.get("usage") or {}
    for field, observed in (
        ("prefillTokens", observed_prefill),
        ("sampledTokensObserved", observed_sampled),
        ("trainingPositions", observed_training_positions),
    ):
        if field in recorded_usage and int(recorded_usage[field]) != observed:
            raise ValueError(f"Tinker {field} differs from operation records")

    arms: dict[str, Any] = {
        "frozen_gpt_5.6_sol_xhigh": {
            "latency": latency_summary(
                frontier_rows,
                "one_subscription_responses_generation_request_including_reasoning",
            ),
            "cost": {
                "billingBasis": "existing_chatgpt_monthly_subscription",
                "experimentSpecificChargeUSD": None,
                "perQueryCostUSD": None,
                "reason": "subscription usage was not separately metered or attributable",
                "usage": frontier_manifest.get("summary", {}).get("usage"),
            },
        },
        "frozen_qwen3.5_9b_base": {
            "latency": latency_summary(
                frozen_qwen_rows,
                "combined_sequential_target_logprob_scoring_and_generation",
            ),
            "cost": None,
        },
        "personalized_qwen3.5_9b_base": {
            "latency": latency_summary(
                personalized_qwen_rows,
                "combined_sequential_target_logprob_scoring_and_generation",
            ),
            "cost": None,
        },
    }

    rate_contract = None
    if provider_plan is not None:
        prices = provider_plan["tinker"]["pricesPerMillionUSD"]
        prefill_rate = Decimal(prices["prefill"])
        sample_rate = Decimal(prices["sample"])
        training_rate = Decimal(prices["training"])
        rate_contract = {
            "pricingAsOf": provider_plan["tinker"]["pricingAsOf"],
            "pricingSource": provider_plan["tinker"]["pricingSource"],
            "pricesPerMillionUSD": prices,
        }
        arms["frozen_qwen3.5_9b_base"]["cost"] = tinker_arm_cost(
            frozen_qwen_rows,
            prefill_rate=prefill_rate,
            sample_rate=sample_rate,
        )
        arms["personalized_qwen3.5_9b_base"]["cost"] = tinker_arm_cost(
            personalized_qwen_rows,
            prefill_rate=prefill_rate,
            sample_rate=sample_rate,
        )
        training_positions = observed_training_positions
        training_estimate = priced_tokens(training_positions, training_rate)
    else:
        training_positions = observed_training_positions
        training_estimate = None

    tinker_observed_seconds = sum(
        float(row["latencySeconds"]) for row in tinker_score_rows + updates
    )
    report = {
        "schemaVersion": 1,
        "reportVersion": COST_LATENCY_VERSION,
        "comparability": {
            "generationLatencyDirectlyComparableAcrossArms": False,
            "reason": (
                "GPT rows time generation requests, while Tinker rows combine target-"
                "likelihood scoring and generation; existing evidence cannot separate them"
            ),
            "futureInstrumentation": (
                "record target-likelihood and generation request latency independently"
            ),
        },
        "arms": arms,
        "personalizationTraining": {
            "latency": latency_summary(
                updates,
                "cumulative_training_update_including_sampler_and_optimizer_checkpoint_saves",
            ),
            "updates": len(updates),
            "submittedPositions": training_positions,
            "estimatedUSDAtFrozenRate": (
                decimal_string(training_estimate) if training_estimate is not None else None
            ),
        },
        "tinkerAggregate": {
            "observedOperationWallClockSeconds": tinker_observed_seconds,
            "frozenRateContract": rate_contract,
            "frozenRateEstimate": tinker_manifest.get("estimatedCost"),
            "verifiedFinalChargeUSD": (
                decimal_string(verified_tinker_charge_usd)
                if verified_tinker_charge_usd is not None
                else None
            ),
            "verifiedChargeProvenance": (
                "user_reported_provider_total" if verified_tinker_charge_usd is not None else None
            ),
            "actualCostAttributablePerArmOrQuery": False,
        },
    }
    return report


def cost_latency_csv(report: dict[str, Any]) -> str:
    columns = [
        "component",
        "observations",
        "latency_semantics",
        "total_seconds",
        "mean_seconds",
        "median_seconds",
        "p90_seconds",
        "p95_seconds",
        "estimated_combined_cost_usd",
        "estimated_generation_only_cost_usd",
        "estimated_mean_cost_per_example_usd",
        "estimated_generation_only_mean_per_example_usd",
        "actual_attributable_cost_usd",
    ]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for arm, value in report["arms"].items():
        latency = value["latency"]
        cost = value.get("cost") or {}
        estimated = cost.get("estimatedUSD") or {}
        writer.writerow({
            "component": arm,
            "observations": latency.get("observations"),
            "latency_semantics": latency.get("semantics"),
            "total_seconds": latency.get("totalSeconds"),
            "mean_seconds": latency.get("meanSeconds"),
            "median_seconds": latency.get("medianSeconds"),
            "p90_seconds": latency.get("p90Seconds"),
            "p95_seconds": latency.get("p95Seconds"),
            "estimated_combined_cost_usd": estimated.get(
                "combinedScoringAndGeneration"
            ),
            "estimated_generation_only_cost_usd": estimated.get("generationOnly"),
            "estimated_mean_cost_per_example_usd": estimated.get(
                "combinedMeanPerExample"
            ),
            "estimated_generation_only_mean_per_example_usd": estimated.get(
                "generationOnlyMeanPerExample"
            ),
            "actual_attributable_cost_usd": cost.get("experimentSpecificChargeUSD"),
        })
    training = report["personalizationTraining"]
    latency = training["latency"]
    writer.writerow({
        "component": "personalization_training",
        "observations": latency.get("observations"),
        "latency_semantics": latency.get("semantics"),
        "total_seconds": latency.get("totalSeconds"),
        "mean_seconds": latency.get("meanSeconds"),
        "median_seconds": latency.get("medianSeconds"),
        "p90_seconds": latency.get("p90Seconds"),
        "p95_seconds": latency.get("p95Seconds"),
        "estimated_combined_cost_usd": training.get("estimatedUSDAtFrozenRate"),
    })
    aggregate = report["tinkerAggregate"]
    writer.writerow({
        "component": "tinker_aggregate_all_operations",
        "observations": sum(
            value["latency"]["observations"]
            for name, value in report["arms"].items()
            if name != "frozen_gpt_5.6_sol_xhigh"
        ) + training["latency"]["observations"],
        "latency_semantics": "sum_of_recorded_scoring_generation_and_update_wall_clock",
        "total_seconds": aggregate["observedOperationWallClockSeconds"],
        "estimated_combined_cost_usd": (
            (aggregate.get("frozenRateEstimate") or {}).get(
                "subtotalBeforeCheckpointStorage"
            )
        ),
        "actual_attributable_cost_usd": aggregate.get("verifiedFinalChargeUSD"),
    })
    return output.getvalue()

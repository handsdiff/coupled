#!/usr/bin/env python3
"""Cost and latency accounting for the Phase 1 provider experiment."""

from __future__ import annotations

import csv
import io
from decimal import Decimal
from typing import Any


COST_LATENCY_VERSION = "phase1-cost-latency-v3"
MILLION = Decimal(1_000_000)
OPENAI_API_EQUIVALENT_PRICE_CONTRACT = {
    "model": "gpt-5.6-sol",
    "pricingAsOf": "2026-08-19",
    "pricingSource": "https://developers.openai.com/api/docs/models/gpt-5.6-sol",
    "pricesPerMillionUSD": {
        "input": "5.00",
        "cachedInput": "0.50",
        "cacheWrite": "6.25",
        "output": "30.00",
    },
    "highContextThresholdInputTokens": 272_000,
    "highContextMultipliers": {"input": "2.0", "output": "1.5"},
}


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        raise ValueError("cannot calculate a percentile of no values")
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def latency_summary(
    rows: list[dict[str, Any]], semantics: str, *, field: str = "latencySeconds"
) -> dict[str, Any]:
    values = [float(row[field]) for row in rows]
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


def openai_api_equivalent_query_cost(row: dict[str, Any]) -> dict[str, Any]:
    usage = row.get("usage") or {}
    details = usage.get("input_tokens_details") or {}
    input_tokens = int(usage.get("input_tokens") or 0)
    cached_tokens = int(details.get("cached_tokens") or 0)
    cache_write_tokens = int(details.get("cache_write_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    uncached_tokens = input_tokens - cached_tokens - cache_write_tokens
    if uncached_tokens < 0:
        raise ValueError("OpenAI input-token detail exceeds total input tokens")

    prices = OPENAI_API_EQUIVALENT_PRICE_CONTRACT["pricesPerMillionUSD"]
    high_context = (
        input_tokens
        > OPENAI_API_EQUIVALENT_PRICE_CONTRACT["highContextThresholdInputTokens"]
    )
    input_multiplier = Decimal(
        OPENAI_API_EQUIVALENT_PRICE_CONTRACT["highContextMultipliers"]["input"]
        if high_context
        else "1"
    )
    output_multiplier = Decimal(
        OPENAI_API_EQUIVALENT_PRICE_CONTRACT["highContextMultipliers"]["output"]
        if high_context
        else "1"
    )
    uncached_cost = priced_tokens(
        uncached_tokens, Decimal(prices["input"]) * input_multiplier
    )
    cached_cost = priced_tokens(
        cached_tokens, Decimal(prices["cachedInput"]) * input_multiplier
    )
    cache_write_cost = priced_tokens(
        cache_write_tokens, Decimal(prices["cacheWrite"]) * input_multiplier
    )
    output_cost = priced_tokens(
        output_tokens, Decimal(prices["output"]) * output_multiplier
    )
    total = uncached_cost + cached_cost + cache_write_cost + output_cost
    return {
        "pricingBasis": "api_equivalent_frozen_official_rates",
        "highContextPricingApplied": high_context,
        "tokens": {
            "inputTotal": input_tokens,
            "inputUncached": uncached_tokens,
            "inputCached": cached_tokens,
            "inputCacheWrite": cache_write_tokens,
            "outputIncludingReasoning": output_tokens,
        },
        "estimatedUSD": {
            "inputUncached": decimal_string(uncached_cost),
            "inputCached": decimal_string(cached_cost),
            "inputCacheWrite": decimal_string(cache_write_cost),
            "outputIncludingReasoning": decimal_string(output_cost),
            "total": decimal_string(total),
        },
    }


def summarize_openai_api_equivalent_cost(rows: list[dict[str, Any]]) -> dict[str, Any]:
    queries = [openai_api_equivalent_query_cost(row) for row in rows]
    total = sum(
        (Decimal(value["estimatedUSD"]["total"]) for value in queries),
        Decimal(0),
    )
    return {
        "pricingBasis": "api_equivalent_frozen_official_rates",
        "priceContract": OPENAI_API_EQUIVALENT_PRICE_CONTRACT,
        "queries": len(rows),
        "highContextQueries": sum(value["highContextPricingApplied"] for value in queries),
        "estimatedUSD": {
            "total": decimal_string(total),
            "meanPerQuery": decimal_string(total / len(rows)),
        },
    }


def summarize_openai_usage(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "inputTokens": sum(int((row.get("usage") or {}).get("input_tokens") or 0) for row in rows),
        "cachedInputTokens": sum(
            int(
                ((row.get("usage") or {}).get("input_tokens_details") or {}).get(
                    "cached_tokens"
                )
                or 0
            )
            for row in rows
        ),
        "outputTokens": sum(
            int((row.get("usage") or {}).get("output_tokens") or 0) for row in rows
        ),
        "reasoningTokens": sum(
            int(
                ((row.get("usage") or {}).get("output_tokens_details") or {}).get(
                    "reasoning_tokens"
                )
                or 0
            )
            for row in rows
        ),
    }


def tinker_latency(rows: list[dict[str, Any]]) -> dict[str, Any]:
    has_components = all(
        "generationLatencySeconds" in row
        and "targetLikelihoodLatencySeconds" in row
        for row in rows
    )
    if not has_components:
        return {
            "primary": latency_summary(
                rows,
                "combined_sequential_target_logprob_scoring_and_generation",
            ),
            "componentsAvailable": False,
        }
    return {
        "primary": latency_summary(
            rows,
            "generation_sampling_request_only",
            field="generationLatencySeconds",
        ),
        "componentsAvailable": True,
        "targetLikelihood": latency_summary(
            rows,
            "target_logprob_scoring_request_only",
            field="targetLikelihoodLatencySeconds",
        ),
        "combined": latency_summary(
            rows,
            "combined_sequential_target_logprob_scoring_and_generation",
        ),
    }


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


def benchmark_arms(
    *,
    frontier_rows: list[dict[str, Any]],
    frozen_qwen_rows: list[dict[str, Any]],
    personalized_qwen_rows: list[dict[str, Any]],
    prefill_rate: Decimal | None,
    sample_rate: Decimal | None,
) -> dict[str, Any]:
    frozen_latency = tinker_latency(frozen_qwen_rows)
    personalized_latency = tinker_latency(personalized_qwen_rows)
    arms: dict[str, Any] = {
        "frozen_gpt_5.6_sol_xhigh": {
            "latency": latency_summary(
                frontier_rows,
                "one_subscription_responses_generation_request_including_reasoning",
            ),
            "cost": {
                "billingBasis": "existing_chatgpt_monthly_subscription",
                "experimentSpecificChargeUSD": None,
                "reason": "subscription usage was not separately metered or attributable",
                "usage": summarize_openai_usage(frontier_rows),
                "apiEquivalent": summarize_openai_api_equivalent_cost(frontier_rows),
            },
        },
        "frozen_qwen3.5_9b_base": {
            "latency": frozen_latency["primary"],
            "latencyComponents": frozen_latency,
            "cost": None,
        },
        "personalized_qwen3.5_9b_base": {
            "latency": personalized_latency["primary"],
            "latencyComponents": personalized_latency,
            "cost": None,
        },
    }
    if prefill_rate is not None and sample_rate is not None:
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
    return arms


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
    evaluation_example_ids: set[str] | None = None,
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

    rate_contract = None
    prefill_rate = None
    sample_rate = None
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
        training_positions = observed_training_positions
        training_estimate = priced_tokens(training_positions, training_rate)
    else:
        training_positions = observed_training_positions
        training_estimate = None

    all_executed_arms = benchmark_arms(
        frontier_rows=frontier_rows,
        frozen_qwen_rows=frozen_qwen_rows,
        personalized_qwen_rows=personalized_qwen_rows,
        prefill_rate=prefill_rate,
        sample_rate=sample_rate,
    )
    if evaluation_example_ids is None:
        evaluation_arms = all_executed_arms
        evaluation_count = len(frontier_rows)
        excluded_count = 0
    else:
        frontier_evaluation = [
            row for row in frontier_rows if row["exampleID"] in evaluation_example_ids
        ]
        frozen_evaluation = [
            row for row in frozen_qwen_rows if row["exampleID"] in evaluation_example_ids
        ]
        personalized_evaluation = [
            row
            for row in personalized_qwen_rows
            if row["exampleID"] in evaluation_example_ids
        ]
        if not (
            len(frontier_evaluation)
            == len(frozen_evaluation)
            == len(personalized_evaluation)
            == len(evaluation_example_ids)
        ):
            raise ValueError("cost/latency evaluation coverage differs across arms")
        evaluation_arms = benchmark_arms(
            frontier_rows=frontier_evaluation,
            frozen_qwen_rows=frozen_evaluation,
            personalized_qwen_rows=personalized_evaluation,
            prefill_rate=prefill_rate,
            sample_rate=sample_rate,
        )
        evaluation_count = len(evaluation_example_ids)
        excluded_count = len(frontier_rows) - evaluation_count

    frozen_latency = evaluation_arms["frozen_qwen3.5_9b_base"]["latencyComponents"]
    personalized_latency = evaluation_arms[
        "personalized_qwen3.5_9b_base"
    ]["latencyComponents"]

    tinker_observed_seconds = sum(
        float(row["latencySeconds"]) for row in tinker_score_rows + updates
    )
    report = {
        "schemaVersion": 1,
        "reportVersion": COST_LATENCY_VERSION,
        "headlineScope": {
            "kind": "prospective_evaluation_after_warmup",
            "examples": evaluation_count,
            "warmupExamplesExcluded": excluded_count,
        },
        "comparability": {
            "generationLatencyDirectlyComparableAcrossArms": (
                frozen_latency["componentsAvailable"]
                and personalized_latency["componentsAvailable"]
            ),
            "reason": (
                "historical Tinker rows combine target-likelihood scoring and generation"
                if not frozen_latency["componentsAvailable"]
                else "all primary arm latency summaries time generation requests"
            ),
            "futureInstrumentation": (
                "record target-likelihood and generation request latency independently"
            ),
        },
        "arms": evaluation_arms,
        "allExecutedArms": all_executed_arms,
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
        api_equivalent = cost.get("apiEquivalent") or {}
        if api_equivalent:
            estimated = {
                "combinedScoringAndGeneration": api_equivalent["estimatedUSD"]["total"],
                "generationOnly": api_equivalent["estimatedUSD"]["total"],
                "combinedMeanPerExample": api_equivalent["estimatedUSD"]["meanPerQuery"],
                "generationOnlyMeanPerExample": api_equivalent["estimatedUSD"][
                    "meanPerQuery"
                ],
            }
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
            for name, value in report["allExecutedArms"].items()
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

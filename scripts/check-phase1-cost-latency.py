#!/usr/bin/env python3
"""No-network checks for Phase 1 cost and latency accounting."""

from decimal import Decimal

from phase1_cost_latency import (
    build_cost_latency_report,
    cost_latency_csv,
    openai_api_equivalent_query_cost,
    percentile,
)


def main() -> int:
    if not (
        percentile([1.0, 2.0, 3.0], 0.5) == 2.0
        and percentile([1.0, 2.0], 0.5) == 1.5
    ):
        raise AssertionError("latency percentile contract failed")

    frontier = [{
        "latencySeconds": 2.0,
        "usage": {
            "input_tokens": 100_000,
            "input_tokens_details": {
                "cached_tokens": 10_000,
                "cache_write_tokens": 0,
            },
            "output_tokens": 10_000,
        },
    }]
    common = {
        "fullSequenceTokenCount": 1_000_000,
        "modelInputTokenCount": 500_000,
        "predictionTokenIDs": [1] * 10,
        "targetLikelihoodLatencySeconds": 1.0,
        "generationLatencySeconds": 2.0,
    }
    frozen = [{**common, "latencySeconds": 4.0}]
    personalized = [{**common, "latencySeconds": 6.0}]
    updates = [{"latencySeconds": 8.0, "submittedPositions": 2_000_000}]
    plan = {
        "tinker": {
            "pricesPerMillionUSD": {
                "prefill": "0.5",
                "sample": "1.0",
                "training": "2.0",
            },
            "pricingAsOf": "fixture",
            "pricingSource": "fixture",
        }
    }
    report = build_cost_latency_report(
        frontier_rows=frontier,
        frozen_qwen_rows=frozen,
        personalized_qwen_rows=personalized,
        updates=updates,
        frontier_manifest={"summary": {"usage": {"total_tokens": 1}}},
        tinker_manifest={
            "estimatedCost": {"fixture": True},
            "usage": {
                "prefillTokens": 3_000_000,
                "sampledTokensObserved": 20,
                "trainingPositions": 2_000_000,
            },
        },
        provider_plan=plan,
        verified_tinker_charge_usd=Decimal("3.25"),
    )
    frozen_cost = report["arms"]["frozen_qwen3.5_9b_base"]["cost"]["estimatedUSD"]
    frontier_cost = openai_api_equivalent_query_cost(frontier[0])
    high_context_cost = openai_api_equivalent_query_cost({
        "usage": {
            "input_tokens": 300_000,
            "input_tokens_details": {
                "cached_tokens": 0,
                "cache_write_tokens": 0,
            },
            "output_tokens": 10_000,
        }
    })
    if not (
        frozen_cost["targetLikelihood"] == "0.500000"
        and frozen_cost["generationOnly"] == "0.250010"
        and report["personalizationTraining"]["estimatedUSDAtFrozenRate"]
        == "4.000000"
        and report["tinkerAggregate"]["verifiedFinalChargeUSD"] == "3.250000"
        and report["comparability"]["generationLatencyDirectlyComparableAcrossArms"]
        and report["arms"]["frozen_qwen3.5_9b_base"]["latency"][
            "meanSeconds"
        ] == 2.0
        and frontier_cost["estimatedUSD"]["total"] == "0.755000"
        and high_context_cost["highContextPricingApplied"]
        and high_context_cost["estimatedUSD"]["total"] == "3.450000"
        and report["arms"]["frozen_gpt_5.6_sol_xhigh"]["cost"][
            "apiEquivalent"
        ]["estimatedUSD"]["total"] == "0.755000"
        and "personalization_training" in cost_latency_csv(report)
        and "tinker_aggregate_all_operations" in cost_latency_csv(report)
    ):
        raise AssertionError("cost accounting contract failed")
    print("Phase 1 cost/latency checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

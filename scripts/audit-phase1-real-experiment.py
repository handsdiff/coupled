#!/usr/bin/env python3
"""Audit and summarize the three-arm Phase 1 developmental experiment."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import tempfile
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from phase1_experiment import (
    ARM_FROZEN_FRONTIER,
    ARM_FROZEN_QWEN,
    ARM_PERSONALIZED_QWEN,
    canonical_bytes,
    load_jsonl,
    target_text,
    validate_inputs,
    write_jsonl,
)
from phase1_prediction_metrics import (
    METRIC_CONTRACT_VERSION,
    PASTE_MARKER,
    score_prediction,
    summarize_prediction_metrics,
)
from phase1_cost_latency import (
    COST_LATENCY_VERSION,
    build_cost_latency_report,
    cost_latency_csv,
    openai_api_equivalent_query_cost,
    tinker_arm_cost,
)
from phase1_training_contract import TrainingContractError, sha256


AUDIT_VERSION = "phase1-real-experiment-audit-v6"


def target_profile(example: dict[str, Any]) -> dict[str, Any]:
    segments = example["target"].get("segments", [])
    paste_actions = sum(segment.get("type") == "paste" for segment in segments)
    has_authored_text = any(
        segment.get("type") == "authored_text" and bool(segment.get("content"))
        for segment in segments
    )
    if not paste_actions:
        stratum = "authored_only"
    elif has_authored_text:
        stratum = "mixed_authored_and_paste"
    else:
        stratum = "paste_only"
    return {"pasteActionCount": paste_actions, "targetStratum": stratum}


def arm_summary(
    rows: list[dict[str, Any]], prediction_metrics: list[dict[str, Any]]
) -> dict[str, Any]:
    weighted = sum(int(row.get("weightedTokenCount") or 0) for row in rows)
    nll_sum = sum(float(row.get("weightedNLLSum") or 0.0) for row in rows)
    return {
        "examples": len(rows),
        "generatedCompletion": summarize_prediction_metrics(prediction_metrics),
        "legacyProviderSequenceMatcher": {
            "meanCharacterSimilarity": sum(
                float(row["characterSimilarity"]) for row in rows
            ) / len(rows),
        },
        "weightedTokens": weighted or None,
        "microTargetTokenNLL": nll_sum / weighted if weighted else None,
        "macroExampleAverageNLL": (
            sum(float(row["weightedNLLSum"]) / int(row["weightedTokenCount"]) for row in rows)
            / len(rows)
            if weighted
            else None
        ),
        "latencySeconds": sum(float(row["latencySeconds"]) for row in rows),
    }


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--packed", required=True, type=Path)
    parser.add_argument("--frontier", required=True, type=Path)
    parser.add_argument("--tinker", required=True, type=Path)
    parser.add_argument("--provider-plan", type=Path)
    parser.add_argument("--verified-tinker-charge-usd", type=Decimal)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    corpus_path = arguments.corpus.expanduser().resolve()
    packed_path = arguments.packed.expanduser().resolve()
    frontier_path = arguments.frontier.expanduser().resolve()
    tinker_path = arguments.tinker.expanduser().resolve()
    provider_plan_path = (
        arguments.provider_plan.expanduser().resolve()
        if arguments.provider_plan is not None
        else None
    )
    output = arguments.output.expanduser().resolve()
    if output.exists():
        raise TrainingContractError(f"output already exists: {output}")
    corpus, examples, _, plans = validate_inputs(corpus_path, packed_path)
    example_ids = [row["exampleID"] for row in examples]
    example_by_id = {row["exampleID"]: row for row in examples}
    profile_by_id = {
        row["exampleID"]: target_profile(row)
        for row in examples
    }
    blocks = corpus["blocking"]["blocks"]
    if len(blocks) < 2:
        raise TrainingContractError("prospective evaluation requires a warm-up block")
    warmup_example_ids = list(blocks[0]["exampleIDs"])
    evaluation_example_ids = [
        example_id for block in blocks[1:] for example_id in block["exampleIDs"]
    ]
    evaluation_example_id_set = set(evaluation_example_ids)

    frontier_manifest = json.loads((frontier_path / "frontier.json").read_text())
    frontier_scores = load_jsonl(frontier_path / "scores.jsonl")
    tinker_manifest = json.loads((tinker_path / "tinker.json").read_text())
    tinker_scores = load_jsonl(tinker_path / "scores.jsonl")
    updates = load_jsonl(tinker_path / "updates.jsonl")
    provider_plan = None
    if provider_plan_path is not None:
        provider_plan = json.loads(provider_plan_path.read_text())
        plan_digest = sha256(provider_plan_path)
        if not (
            tinker_manifest.get("source", {}).get("providerPlanSHA256") == plan_digest
            and frontier_manifest.get("source", {}).get("providerPlanSHA256")
            == plan_digest
        ):
            raise TrainingContractError("provider plan lineage differs")
    if (
        arguments.verified_tinker_charge_usd is not None
        and arguments.verified_tinker_charge_usd < 0
    ):
        raise TrainingContractError("verified Tinker charge cannot be negative")
    if not (
        frontier_manifest.get("status") == "complete"
        and tinker_manifest.get("status") == "complete"
        and frontier_manifest.get("artifactDigestsSHA256", {}).get("scores.jsonl")
        == sha256(frontier_path / "scores.jsonl")
        and tinker_manifest.get("artifactDigestsSHA256", {}).get("scores.jsonl")
        == sha256(tinker_path / "scores.jsonl")
        and tinker_manifest.get("artifactDigestsSHA256", {}).get("updates.jsonl")
        == sha256(tinker_path / "updates.jsonl")
    ):
        raise TrainingContractError("provider artifacts are incomplete or changed")
    for manifest in (frontier_manifest, tinker_manifest):
        if not (
            manifest.get("source", {}).get("corpusSHA256")
            == sha256(corpus_path / "corpus.json")
            and manifest.get("source", {}).get("packingSHA256")
            == sha256(packed_path / "packing.json")
        ):
            raise TrainingContractError("provider artifact lineage differs")
    scored_example_ids = [row["exampleID"] for row in frontier_scores]
    if scored_example_ids not in (example_ids, evaluation_example_ids):
        raise TrainingContractError("frontier score coverage/order differs")
    scored_example_id_set = set(scored_example_ids)

    by_arm: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in frontier_scores + tinker_scores:
        arm = row["arm"]
        example_id = row["exampleID"]
        if example_id in by_arm[arm]:
            raise TrainingContractError("duplicate arm/example score")
        expected = target_text(example_by_id[example_id]["target"])
        if row["target"] != expected:
            raise TrainingContractError("provider score target differs from frozen corpus")
        if row.get("pasteActionCount") != profile_by_id[example_id]["pasteActionCount"]:
            raise TrainingContractError("provider paste count differs from frozen corpus")
        if arm == ARM_FROZEN_FRONTIER and row.get("semanticModelInputSHA256") != plans[
            example_id
        ]["semanticModelInputSHA256"]:
            raise TrainingContractError("frontier score used a different context plan")
        by_arm[arm][example_id] = row
    for arm in (ARM_FROZEN_QWEN, ARM_FROZEN_FRONTIER, ARM_PERSONALIZED_QWEN):
        if set(by_arm[arm]) != scored_example_id_set:
            raise TrainingContractError(f"incomplete score coverage for {arm}")

    prediction_metrics_by_arm = {
        arm: {
            example_id: score_prediction(
                by_arm[arm][example_id]["target"],
                by_arm[arm][example_id]["prediction"],
                target_paste_actions=profile_by_id[example_id]["pasteActionCount"],
            )
            for example_id in scored_example_ids
        }
        for arm in (ARM_FROZEN_QWEN, ARM_FROZEN_FRONTIER, ARM_PERSONALIZED_QWEN)
    }

    update_by_block = {row["afterBlockID"]: row for row in updates}
    if list(update_by_block) != [row["blockID"] for row in blocks]:
        raise TrainingContractError("update blocks differ from frozen protocol")
    for ordinal, block in enumerate(blocks):
        update = update_by_block[block["blockID"]]
        scored_block_ids = [
            example_id for example_id in block["exampleIDs"]
            if example_id in scored_example_id_set
        ]
        score_times = [
            by_arm[arm][example_id]["completedAt"]
            for arm in (ARM_FROZEN_QWEN, ARM_FROZEN_FRONTIER, ARM_PERSONALIZED_QWEN)
            for example_id in scored_block_ids
        ]
        if score_times and max(score_times) >= update["completedAt"]:
            raise TrainingContractError("block update preceded a score")
        expected_checkpoint = updates[ordinal - 1]["samplerCheckpointPath"] if ordinal else None
        for example_id in scored_block_ids:
            if by_arm[ARM_PERSONALIZED_QWEN][example_id].get("checkpointID") != expected_checkpoint:
                raise TrainingContractError("personalized score used the wrong checkpoint")

    comparisons = []
    tinker_prices = (
        provider_plan.get("tinker", {}).get("pricesPerMillionUSD")
        if provider_plan is not None
        else None
    )
    prefill_rate = Decimal(tinker_prices["prefill"]) if tinker_prices else None
    sample_rate = Decimal(tinker_prices["sample"]) if tinker_prices else None
    for example in examples:
        example_id = example["exampleID"]
        if example_id not in scored_example_id_set:
            continue
        frozen = by_arm[ARM_FROZEN_QWEN][example_id]
        personalized = by_arm[ARM_PERSONALIZED_QWEN][example_id]
        frontier = by_arm[ARM_FROZEN_FRONTIER][example_id]
        comparisons.append({
            "exampleID": example_id,
            "blockID": example["experimentBlockID"],
            "evaluationRole": (
                "prospective_evaluation"
                if example_id in evaluation_example_id_set
                else "warmup_excluded_from_headline"
            ),
            "application": frozen.get("application"),
            "target": frozen["target"],
            "pasteActionCount": frozen["pasteActionCount"],
            "targetStratum": profile_by_id[example_id]["targetStratum"],
            "frozenQwen": {
                "prediction": frozen["prediction"],
                "meanNLL": frozen["meanNLL"],
                "latencySeconds": frozen["latencySeconds"],
                "latencySemantics": (
                    "combined_sequential_target_logprob_scoring_and_generation"
                ),
                "estimatedCost": (
                    tinker_arm_cost(
                        [frozen],
                        prefill_rate=prefill_rate,
                        sample_rate=sample_rate,
                    )
                    if prefill_rate is not None and sample_rate is not None
                    else None
                ),
                "predictionMetrics": prediction_metrics_by_arm[
                    ARM_FROZEN_QWEN
                ][example_id],
                "legacySequenceMatcherCharacterSimilarity": frozen[
                    "characterSimilarity"
                ],
            },
            "frontier": {
                "prediction": frontier["prediction"],
                "latencySeconds": frontier["latencySeconds"],
                "latencySemantics": (
                    "one_subscription_responses_generation_request_including_reasoning"
                ),
                "usage": frontier.get("usage"),
                "experimentSpecificCostUSD": None,
                "costReason": "subscription usage was not separately attributable",
                "apiEquivalentCost": openai_api_equivalent_query_cost(frontier),
                "predictionMetrics": prediction_metrics_by_arm[
                    ARM_FROZEN_FRONTIER
                ][example_id],
                "legacySequenceMatcherCharacterSimilarity": frontier[
                    "characterSimilarity"
                ],
            },
            "personalizedQwen": {
                "prediction": personalized["prediction"],
                "meanNLL": personalized["meanNLL"],
                "latencySeconds": personalized["latencySeconds"],
                "latencySemantics": (
                    "combined_sequential_target_logprob_scoring_and_generation"
                ),
                "estimatedCost": (
                    tinker_arm_cost(
                        [personalized],
                        prefill_rate=prefill_rate,
                        sample_rate=sample_rate,
                    )
                    if prefill_rate is not None and sample_rate is not None
                    else None
                ),
                "predictionMetrics": prediction_metrics_by_arm[
                    ARM_PERSONALIZED_QWEN
                ][example_id],
                "legacySequenceMatcherCharacterSimilarity": personalized[
                    "characterSimilarity"
                ],
                "checkpointID": personalized["checkpointID"],
            },
            "personalizedBitsSavedVersusFrozen": (
                frozen["weightedNLLSum"] - personalized["weightedNLLSum"]
            ) / math.log(2),
        })

    evaluation_comparisons = [
        row for row in comparisons if row["evaluationRole"] == "prospective_evaluation"
    ]
    summaries = {
        arm: arm_summary(
            [by_arm[arm][example_id] for example_id in evaluation_example_ids],
            [
                prediction_metrics_by_arm[arm][example_id]
                for example_id in evaluation_example_ids
            ],
        )
        for arm in (ARM_FROZEN_QWEN, ARM_FROZEN_FRONTIER, ARM_PERSONALIZED_QWEN)
    }
    all_scored_operational_summaries = {
        arm: arm_summary(
            [by_arm[arm][example_id] for example_id in scored_example_ids],
            [
                prediction_metrics_by_arm[arm][example_id]
                for example_id in scored_example_ids
            ],
        )
        for arm in (ARM_FROZEN_QWEN, ARM_FROZEN_FRONTIER, ARM_PERSONALIZED_QWEN)
    }
    generated_completion_strata = {
        arm: {
            stratum: summarize_prediction_metrics([
                prediction_metrics_by_arm[arm][example_id]
                for example_id in evaluation_example_ids
                if profile_by_id[example_id]["targetStratum"] == stratum
            ])
            for stratum in (
                "authored_only",
                "mixed_authored_and_paste",
                "paste_only",
            )
        }
        for arm in (ARM_FROZEN_QWEN, ARM_FROZEN_FRONTIER, ARM_PERSONALIZED_QWEN)
    }
    cost_latency = build_cost_latency_report(
        frontier_rows=[
            by_arm[ARM_FROZEN_FRONTIER][value] for value in scored_example_ids
        ],
        frozen_qwen_rows=[
            by_arm[ARM_FROZEN_QWEN][value] for value in scored_example_ids
        ],
        personalized_qwen_rows=[
            by_arm[ARM_PERSONALIZED_QWEN][value] for value in scored_example_ids
        ],
        updates=updates,
        frontier_manifest=frontier_manifest,
        tinker_manifest=tinker_manifest,
        provider_plan=provider_plan,
        verified_tinker_charge_usd=arguments.verified_tinker_charge_usd,
        evaluation_example_ids=evaluation_example_id_set,
    )
    block_summaries = []
    for block in blocks:
        ids = [
            value for value in block["exampleIDs"] if value in scored_example_id_set
        ]
        bits = (
            sum(
                by_arm[ARM_FROZEN_QWEN][value]["weightedNLLSum"]
                - by_arm[ARM_PERSONALIZED_QWEN][value]["weightedNLLSum"]
                for value in ids
            ) / math.log(2)
            if ids
            else None
        )
        block_summaries.append({
            "blockID": block["blockID"],
            "examples": len(block["exampleIDs"]),
            "providerScoredExamples": len(ids),
            "evaluationRole": (
                "warmup_excluded_from_headline"
                if block is blocks[0]
                else "prospective_evaluation"
            ),
            "precedingTrainingExamples": sum(
                len(value["exampleIDs"])
                for value in blocks[: blocks.index(block)]
            ),
            "personalizedBitsSavedVersusFrozen": bits,
            "arms": (
                {
                    arm: arm_summary(
                        [by_arm[arm][value] for value in ids],
                        [prediction_metrics_by_arm[arm][value] for value in ids],
                    )
                    for arm in (
                        ARM_FROZEN_QWEN,
                        ARM_FROZEN_FRONTIER,
                        ARM_PERSONALIZED_QWEN,
                    )
                }
                if ids
                else None
            ),
        })

    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        write_jsonl(temporary / "comparisons.jsonl", comparisons)
        write_jsonl(
            temporary / "evaluation-comparisons.jsonl", evaluation_comparisons
        )
        (temporary / "cost-latency.json").write_bytes(canonical_bytes(cost_latency))
        (temporary / "cost-latency.csv").write_text(
            cost_latency_csv(cost_latency), encoding="utf-8"
        )
        manifest = {
            "schemaVersion": 4,
            "auditVersion": AUDIT_VERSION,
            "status": "passed_developmental_not_thesis_conclusion",
            "source": {
                "corpusID": corpus["corpusID"],
                "corpusSHA256": sha256(corpus_path / "corpus.json"),
                "packingSHA256": sha256(packed_path / "packing.json"),
                "frontierManifestSHA256": sha256(frontier_path / "frontier.json"),
                "frontierScoresSHA256": sha256(frontier_path / "scores.jsonl"),
                "tinkerManifestSHA256": sha256(tinker_path / "tinker.json"),
                "tinkerScoresSHA256": sha256(tinker_path / "scores.jsonl"),
                "tinkerUpdatesSHA256": sha256(tinker_path / "updates.jsonl"),
                "auditImplementationSHA256": sha256(Path(__file__).resolve()),
                "predictionMetricImplementationSHA256": sha256(
                    Path(__file__).with_name("phase1_prediction_metrics.py")
                ),
                "costLatencyImplementationSHA256": sha256(
                    Path(__file__).with_name("phase1_cost_latency.py")
                ),
                "providerPlanSHA256": (
                    sha256(provider_plan_path) if provider_plan_path is not None else None
                ),
            },
            "protocol": {
                "providerScoredExamples": len(scored_example_ids),
                "warmupProviderScored": bool(
                    scored_example_id_set.intersection(warmup_example_ids)
                ),
                "warmupExamples": len(warmup_example_ids),
                "prospectiveEvaluationExamples": len(evaluation_example_ids),
                "blocks": len(blocks),
                "warmupBlockID": blocks[0]["blockID"],
                "prospectiveEvaluationBlockIDs": [
                    block["blockID"] for block in blocks[1:]
                ],
                "headlineEvaluationRule": (
                    "exclude the first block because the personalized arm has no prior "
                    "personal training and is identical to the frozen base"
                ),
                "scoreCompleteBlockBeforeUpdate": True,
                "frontierHasComparableTokenNLL": False,
                "personalizedUpdatePolicy": "warm_start_then_train_full_cumulative_corpus",
            },
            "generatedCompletionMetricContract": {
                "version": METRIC_CONTRACT_VERSION,
                "characterUnit": "unicode_code_point",
                "unicodeNormalization": "none",
                "normalizedExactMatch": "strip_surrounding_unicode_whitespace_only",
                "characterSimilarity": {
                    "distance": "unit_cost_levenshtein",
                    "macro": "mean_of_per_example_one_minus_distance_over_max_length",
                    "micro": "one_minus_summed_distance_over_summed_max_length",
                },
                "correctPrefix": "exact_raw_longest_common_prefix",
                "pasteAction": {
                    "representation": PASTE_MARKER,
                    "matching": "per_example_minimum_of_target_and_prediction_marker_counts",
                },
                "targetStrata": [
                    "authored_only",
                    "mixed_authored_and_paste",
                    "paste_only",
                ],
                "primaryCrossModelMetrics": [
                    "exact_match",
                    "surrounding_whitespace_normalized_exact_match",
                    "correct_prefix",
                    "macro_and_micro_normalized_levenshtein_similarity",
                    "paste_action_precision_and_recall",
                ],
                "personalizationPrimaryMetric": "paired_prequential_target_token_nll",
            },
            "costLatencyReportVersion": COST_LATENCY_VERSION,
            "summaries": summaries,
            "allScoredOperationalSummaries": all_scored_operational_summaries,
            "generatedCompletionStrata": generated_completion_strata,
            "blockSummaries": block_summaries,
            "personalizedCumulativeBitsSavedVersusFrozen": sum(
                row["personalizedBitsSavedVersusFrozen"]
                for row in evaluation_comparisons
            ),
            "providerUsage": {
                "frontier": frontier_manifest.get("summary", {}).get("usage"),
                "tinker": tinker_manifest.get("usage"),
                "tinkerEstimatedCost": tinker_manifest.get("estimatedCost"),
            },
        }
        manifest["artifactDigestsSHA256"] = {
            "comparisons.jsonl": sha256(temporary / "comparisons.jsonl"),
            "evaluation-comparisons.jsonl": sha256(
                temporary / "evaluation-comparisons.jsonl"
            ),
            "cost-latency.json": sha256(temporary / "cost-latency.json"),
            "cost-latency.csv": sha256(temporary / "cost-latency.csv"),
        }
        (temporary / "experiment.json").write_bytes(canonical_bytes(manifest))
        os.replace(temporary, output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    print(f"Phase 1 real experiment audit passed: {output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, KeyError, ValueError, TrainingContractError) as error:
        raise SystemExit(f"audit-phase1-real-experiment: {error}")

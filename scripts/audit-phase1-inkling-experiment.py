#!/usr/bin/env python3
"""Audit the completed four-arm Inkling-Small Phase 1 experiment."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from decimal import Decimal
from pathlib import Path
from typing import Any

from phase1_experiment import canonical_bytes, target_text
from phase1_inkling import (
    ARM_NAMES,
    GENERATION_CONTRACT,
    INKLING_AUDIT_VERSION,
    INKLING_MODEL,
    INKLING_RUNNER_VERSION,
    REASONING_CONDITIONS,
    InklingContractError,
    load_experiment_blocks,
    load_jsonl,
    sha256,
)
from phase1_prediction_metrics import score_prediction, summarize_prediction_metrics


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = quantile * (len(ordered) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def expected_scores(blocks: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    return [
        (block["blockID"], ARM_NAMES[condition][kind], example_id)
        for block in blocks[1:]
        for condition in REASONING_CONDITIONS
        for kind in ("frozen", "personalized")
        for example_id in block["exampleIDs"]
    ]


def expected_updates(blocks: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [
        (block["blockID"], condition)
        for block in blocks[:-1]
        for condition in REASONING_CONDITIONS
    ]


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    weighted = sum(value["weightedTokenCount"] for value in rows)
    latencies = [float(value["latencySeconds"]) for value in rows]
    generation = [float(value["generationLatencySeconds"]) for value in rows]
    nll = [float(value["targetLikelihoodLatencySeconds"]) for value in rows]
    accepted = [
        value for value in rows if value["generationEligibleForEvaluation"]
    ]
    return {
        "examples": len(rows),
        "generationEligibleExamples": len(accepted),
        "generationExcludedExamples": len(rows) - len(accepted),
        "generationDispositions": {
            disposition: sum(
                value["generationDisposition"] == disposition for value in rows
            )
            for disposition in sorted(
                {value["generationDisposition"] for value in rows}
            )
        },
        "macroExampleAverageNLL": statistics.mean(value["meanNLL"] for value in rows),
        "microTargetTokenNLL": sum(value["weightedNLLSum"] for value in rows) / weighted,
        "weightedTokens": weighted,
        "generatedCompletion": (
            summarize_prediction_metrics(
                [value["predictionMetrics"] for value in accepted]
            )
            if accepted
            else None
        ),
        "reasoning": {
            "nonemptyResponses": sum(bool(value["reasoning"]) for value in rows),
            "characters": sum(len(value["reasoning"]) for value in rows),
        },
        "latency": {
            "medianSeconds": statistics.median(latencies),
            "meanSeconds": statistics.mean(latencies),
            "p90Seconds": percentile(latencies, 0.90),
            "generationMedianSeconds": statistics.median(generation),
            "generationMeanSeconds": statistics.mean(generation),
            "targetLikelihoodMedianSeconds": statistics.median(nll),
            "targetLikelihoodMeanSeconds": statistics.mean(nll),
            "totalSeconds": sum(latencies),
        },
        "estimatedProviderCostUSDAtFrozenRates": str(
            sum(
                (Decimal(value["estimatedProviderCostUSDAtFrozenRates"]) for value in rows),
                Decimal(0),
            ).quantize(Decimal("0.000001"))
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--inkling-pack", required=True, type=Path)
    parser.add_argument("--provider-plan", required=True, type=Path)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    output = arguments.output.expanduser().resolve()
    if output.exists():
        raise InklingContractError(f"output already exists: {output}")
    corpus_path = arguments.corpus.expanduser().resolve()
    pack_path = arguments.inkling_pack.expanduser().resolve()
    plan_path = arguments.provider_plan.expanduser().resolve()
    run_path = arguments.run.expanduser().resolve()
    corpus = json.loads((corpus_path / "corpus.json").read_text(encoding="utf-8"))
    examples = load_jsonl(corpus_path / "examples.jsonl")
    example_by_id = {value["exampleID"]: value for value in examples}
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    packing = json.loads((pack_path / "packing.json").read_text(encoding="utf-8"))
    manifest_path = run_path / "inkling.json"
    scores_path = run_path / "scores.jsonl"
    updates_path = run_path / "updates.jsonl"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    scores = load_jsonl(scores_path)
    updates = load_jsonl(updates_path)
    blocks = load_experiment_blocks(corpus_path)
    expected_score_rows = expected_scores(blocks)
    expected_update_rows = expected_updates(blocks)
    if not (
        manifest.get("status") == "complete"
        and manifest.get("runnerVersion") == INKLING_RUNNER_VERSION
        and manifest.get("provider", {}).get("model") == INKLING_MODEL
        and manifest.get("source", {}).get("corpusSHA256")
        == sha256(corpus_path / "corpus.json")
        and manifest.get("source", {}).get("inklingPackingSHA256")
        == sha256(pack_path / "packing.json")
        and manifest.get("source", {}).get("providerPlanSHA256") == sha256(plan_path)
        and manifest.get("artifactDigestsSHA256", {}).get("scores.jsonl")
        == sha256(scores_path)
        and manifest.get("artifactDigestsSHA256", {}).get("updates.jsonl")
        == sha256(updates_path)
        and len(scores) == len(expected_score_rows)
        and len(updates) == len(expected_update_rows)
    ):
        raise InklingContractError("completed Inkling lineage or counts differ")
    observed_scores = [
        (value["blockID"], value["arm"], value["exampleID"]) for value in scores
    ]
    if observed_scores != expected_score_rows:
        raise InklingContractError("Inkling score order differs")
    observed_updates = [(value["afterBlockID"], value["condition"]) for value in updates]
    if observed_updates != expected_update_rows:
        raise InklingContractError("Inkling update order differs")

    rows_by_condition = {
        condition: {
            value["exampleID"]: value
            for value in load_jsonl(pack_path / f"{condition}-packed-examples.jsonl")
        }
        for condition in REASONING_CONDITIONS
    }
    latest_checkpoint: dict[str, str] = {}
    update_by_block_condition: dict[tuple[str, str], dict[str, Any]] = {}
    for update in updates:
        condition = update["condition"]
        if update["effort"] != REASONING_CONDITIONS[condition]:
            raise InklingContractError("update effort differs")
        prior = latest_checkpoint.get(condition)
        block = next(
            value for value in blocks if value["blockID"] == update["afterBlockID"]
        )
        batch_size = plan["protocol"]["trainingContract"][
            "optimizerBatchExamples"
        ]
        expected_batch_sizes = [
            min(batch_size, len(block["exampleIDs"]) - start)
            for start in range(0, len(block["exampleIDs"]), batch_size)
        ]
        if update["parentOptimizerStatePath"] != prior:
            raise InklingContractError("optimizer checkpoint chain differs")
        if not (
            update.get("optimizerBatchSizes") == expected_batch_sizes
            and update.get("trainingCalls") == len(expected_batch_sizes)
            and update.get("optimizerSteps") == len(expected_batch_sizes)
            and update.get("lossReduction")
            == plan["protocol"]["trainingContract"]["lossReduction"]
        ):
            raise InklingContractError("optimizer batching or loss reduction differs")
        latest_checkpoint[condition] = update["optimizerStatePath"]
        update_by_block_condition[(update["afterBlockID"], condition)] = update
    if set(latest_checkpoint) != set(REASONING_CONDITIONS):
        raise InklingContractError("one personalized Inkling chain is absent")

    prior_block_by_block = {
        blocks[index]["blockID"]: blocks[index - 1]["blockID"]
        for index in range(1, len(blocks))
    }
    for score in scores:
        condition = score["condition"]
        row = rows_by_condition[condition][score["exampleID"]]
        expected_target = target_text(example_by_id[score["exampleID"]]["target"])
        valid_final = (
            score.get("responseParse", {}).get("status") == "parsed"
            and bool(score["prediction"])
        )
        expected_disposition = (
            "accepted"
            if valid_final
            else (
                GENERATION_CONTRACT["tokenCapWithoutValidFinalDisposition"]
                if score["stopReason"] == "length"
                else GENERATION_CONTRACT["missingFinalDisposition"]
            )
        )
        expected_metrics = (
            score_prediction(
                expected_target,
                score["prediction"],
                target_paste_actions=row["pasteActionCount"],
            )
            if valid_final
            else None
        )
        kind = "personalized" if score["arm"].startswith("personalized_") else "frozen"
        expected_arm = ARM_NAMES[condition][kind]
        prior_update = update_by_block_condition[
            (prior_block_by_block[score["blockID"]], condition)
        ]
        if not (
            score["arm"] == expected_arm
            and score["effort"] == REASONING_CONDITIONS[condition]
            and score["target"] == expected_target
            and score["semanticModelInputSHA256"] == row["semanticModelInputSHA256"]
            and score["weightedTokenCount"] == row["targetTokenCount"]
            and score["predictionMetrics"] == expected_metrics
            and score["generationDisposition"] == expected_disposition
            and score["generationEligibleForEvaluation"] == valid_final
            and score["generationTokenCeiling"]
            == GENERATION_CONTRACT["maximumTokensByCondition"][condition]
            and (
                score["checkpointID"] is None
                if kind == "frozen"
                else score["checkpointID"] == prior_update["samplerCheckpointPath"]
            )
        ):
            raise InklingContractError(f"score audit failed: {score['exampleID']}")

    summaries = {
        arm: summarize([value for value in scores if value["arm"] == arm])
        for condition in REASONING_CONDITIONS
        for arm in ARM_NAMES[condition].values()
    }
    audit = {
        "schemaVersion": 1,
        "auditVersion": INKLING_AUDIT_VERSION,
        "status": "passed",
        "source": {
            "corpusSHA256": sha256(corpus_path / "corpus.json"),
            "inklingPackingSHA256": sha256(pack_path / "packing.json"),
            "providerPlanSHA256": sha256(plan_path),
            "runManifestSHA256": sha256(manifest_path),
            "scoresSHA256": sha256(scores_path),
            "updatesSHA256": sha256(updates_path),
        },
        "protocol": {
            "model": INKLING_MODEL,
            "reasoningConditions": REASONING_CONDITIONS,
            "scoreRows": len(scores),
            "updates": len(updates),
            "terminalBlockUpdated": False,
            "semanticInputsIdenticalAcrossConditions": all(
                rows_by_condition["reasoning_off"][value]["semanticModelInputSHA256"]
                == rows_by_condition["reasoning_on"][value]["semanticModelInputSHA256"]
                for value in rows_by_condition["reasoning_off"]
            ),
            "trainingContract": plan["protocol"]["trainingContract"],
            "generationContract": plan["protocol"]["generationContract"],
        },
        "summaries": summaries,
        "usage": manifest["usage"],
        "estimatedCost": manifest["estimatedCost"],
        "actualProviderCostUSD": None,
        "packing": {
            "counts": packing["counts"],
            "lossMask": packing["renderer"]["lossMask"],
        },
    }
    output.mkdir(parents=True)
    (output / "audit.json").write_bytes(canonical_bytes(audit))
    with (output / "models.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "arm",
                "examples",
                "generation_eligible_examples",
                "generation_excluded_examples",
                "micro_target_token_nll",
                "macro_character_similarity",
                "exact_match_rate",
                "correct_prefix_mean_characters",
                "median_latency_seconds",
                "mean_latency_seconds",
                "estimated_query_cost_usd",
                "reasoning_nonempty_responses",
            ],
        )
        writer.writeheader()
        for arm, summary in summaries.items():
            generated = summary["generatedCompletion"]
            writer.writerow({
                "arm": arm,
                "examples": summary["examples"],
                "generation_eligible_examples": summary[
                    "generationEligibleExamples"
                ],
                "generation_excluded_examples": summary[
                    "generationExcludedExamples"
                ],
                "micro_target_token_nll": summary["microTargetTokenNLL"],
                "macro_character_similarity": (
                    None
                    if generated is None
                    else generated["macroNormalizedLevenshteinSimilarity"]
                ),
                "exact_match_rate": (
                    None if generated is None else generated["exactMatchRate"]
                ),
                "correct_prefix_mean_characters": (
                    None
                    if generated is None
                    else generated["correctPrefix"]["meanCharactersPerExample"]
                ),
                "median_latency_seconds": summary["latency"]["medianSeconds"],
                "mean_latency_seconds": summary["latency"]["meanSeconds"],
                "estimated_query_cost_usd": summary["estimatedProviderCostUSDAtFrozenRates"],
                "reasoning_nonempty_responses": summary["reasoning"]["nonemptyResponses"],
            })
    print(json.dumps(audit, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

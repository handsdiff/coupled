#!/usr/bin/env python3
"""Audit the native-loss Inkling prequential stability/comparison run."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
from decimal import Decimal
from pathlib import Path
from typing import Any

from phase1_prediction_metrics import score_prediction, summarize_prediction_metrics


class AuditError(RuntimeError):
    pass


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def target_text(target: dict[str, Any]) -> str:
    return "".join(
        segment.get("content", "") if segment["type"] == "authored_text" else "<|paste|>"
        for segment in target["segments"]
    )


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    rank = quantile * (len(ordered) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = [float(value["generationLatencySeconds"]) for value in rows]
    accepted = [value for value in rows if value["accepted"]]
    return {
        "examples": len(rows),
        "accepted": len(accepted),
        "invalid": len(rows) - len(accepted),
        "validityRate": len(accepted) / len(rows),
        "generatedCompletion": summarize_prediction_metrics(
            [value["predictionMetrics"] for value in rows]
        ),
        "generatedCompletionConditionalOnValid": (
            summarize_prediction_metrics(
                [value["validOnlyPredictionMetrics"] for value in accepted]
            )
            if accepted
            else None
        ),
        "generationLatency": {
            "medianSeconds": statistics.median(latencies),
            "meanSeconds": statistics.mean(latencies),
            "p90Seconds": percentile(latencies, 0.90),
            "totalSeconds": sum(latencies),
        },
        "estimatedGenerationCostUSDAtFrozenRates": str(
            sum(
                (Decimal(value["estimatedProviderCostUSDAtFrozenRates"]) for value in rows),
                Decimal(0),
            )
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--inkling-pack", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    corpus_path = arguments.corpus.resolve()
    pack_path = arguments.inkling_pack.resolve()
    plan_path = arguments.plan.resolve()
    run_path = arguments.run.resolve()
    output = arguments.output.resolve()
    if output.exists():
        raise AuditError(f"output already exists: {output}")

    plan = json.loads(plan_path.read_text())
    manifest_path = run_path / "stability.json"
    manifest = json.loads(manifest_path.read_text())
    scores_path = run_path / "scores.jsonl"
    sentinels_path = run_path / "base-sentinels.jsonl"
    updates_path = run_path / "updates.jsonl"
    batches_path = run_path / "training-batches.jsonl"
    scores = load_jsonl(scores_path)
    sentinels = load_jsonl(sentinels_path)
    updates = load_jsonl(updates_path)
    batches = load_jsonl(batches_path)
    blocks = {
        value["blockID"]: value
        for value in load_jsonl(corpus_path / "episode-blocks.jsonl")
    }
    examples = {
        value["exampleID"]: value
        for value in load_jsonl(corpus_path / "examples.jsonl")
    }
    packed = {
        value["exampleID"]: value
        for value in load_jsonl(pack_path / "reasoning_off-packed-examples.jsonl")
    }

    expected_score_ids = [
        example_id
        for block_id in plan["protocol"]["evaluationBlockIDsAfterUpdate"]
        for example_id in blocks[block_id]["exampleIDs"]
    ]
    if [value["exampleID"] for value in scores] != expected_score_ids:
        raise AuditError("scores are not the exact ordered 174-example protocol")
    if [value["exampleID"] for value in sentinels] != plan["protocol"]["probeExampleIDs"]:
        raise AuditError("base probes differ from the frozen plan")
    if [value["updateOrdinal"] for value in updates] != [1, 2, 3, 4]:
        raise AuditError("updates are not the exact four-stage protocol")
    if len(batches) != 20:
        raise AuditError("expected exactly 20 optimizer batch records")

    if not (
        manifest["status"] == "complete_with_generation_deterioration"
        and manifest["planSHA256"] == sha256(plan_path)
        and manifest["counts"]["evaluationScores"] == 174
        and manifest["counts"]["completedUpdates"] == 4
        and manifest["counts"]["trainingBatchRecords"] == 20
    ):
        raise AuditError("completed manifest status or counts differ")
    for filename, path in (
        ("base-sentinels.jsonl", sentinels_path),
        ("scores.jsonl", scores_path),
        ("updates.jsonl", updates_path),
        ("training-batches.jsonl", batches_path),
    ):
        if manifest["artifactDigestsSHA256"][filename] != sha256(path):
            raise AuditError(f"artifact digest differs: {filename}")

    latest_optimizer: str | None = None
    for ordinal, update in enumerate(updates, 1):
        block_id = plan["protocol"]["trainingBlockIDs"][ordinal - 1]
        update_batches = [value for value in batches if value["updateOrdinal"] == ordinal]
        if not (
            update["afterBlockID"] == block_id
            and update["parentOptimizerStatePath"] == latest_optimizer
            and update["trainedExamplesThisUpdate"] == 50
            and update["cumulativeTrainedExamples"] == ordinal * 50
            and update["optimizerSteps"] == 5
            and len(update_batches) == 5
            and [value["batchPosition"] for value in update_batches] == [1, 2, 3, 4, 5]
            and sorted(
                example_id
                for value in update_batches
                for example_id in value["exampleIDs"]
            )
            == sorted(blocks[block_id]["exampleIDs"])
        ):
            raise AuditError(f"training update {ordinal} differs from its block")
        latest_optimizer = update["optimizerStatePath"]

    position = 0
    stage_summaries: list[dict[str, Any]] = []
    for stage, block_id in enumerate(
        plan["protocol"]["evaluationBlockIDsAfterUpdate"], 1
    ):
        count = len(blocks[block_id]["exampleIDs"])
        stage_rows = scores[position : position + count]
        expected_checkpoint = updates[stage - 1]["samplerCheckpointPath"]
        for row in stage_rows:
            example_id = row["exampleID"]
            expected_target = target_text(examples[example_id]["target"])
            accepted = bool(row["accepted"])
            expected_metrics = score_prediction(
                expected_target,
                row["prediction"] if accepted else "",
                target_paste_actions=packed[example_id]["pasteActionCount"],
            )
            expected_valid_metrics = (
                score_prediction(
                    expected_target,
                    row["prediction"],
                    target_paste_actions=packed[example_id]["pasteActionCount"],
                )
                if accepted
                else None
            )
            if not (
                row["stage"] == stage
                and row["trainedExamples"] == stage * 50
                and row["blockID"] == block_id
                and row["checkpointID"] == expected_checkpoint
                and row["target"] == expected_target
                and row["semanticModelInputSHA256"]
                == packed[example_id]["semanticModelInputSHA256"]
                and row["predictionMetrics"] == expected_metrics
                and row["validOnlyPredictionMetrics"] == expected_valid_metrics
                and row["generationEligibleForEvaluation"] == accepted
            ):
                raise AuditError(f"score contract differs: {example_id}")
        stage_summaries.append(
            {"stage": stage, "blockID": block_id, "trainedExamples": stage * 50, **summarize(stage_rows)}
        )
        position += count

    recovery_rows = manifest.get("concurrentResumeRecoveries", [])
    recovery_cost = sum(
        (Decimal(value["duplicateGenerationCostUSDAtFrozenRates"]) for value in recovery_rows),
        Decimal(0),
    )
    for recovery in recovery_rows:
        evidence = run_path / recovery["preservedEvidence"]
        if sha256(evidence) != recovery["preservedEvidenceSHA256"]:
            raise AuditError("preserved concurrent-resume evidence digest differs")
    completed_training = sum(
        (Decimal(value["estimatedTrainingCostUSDAtFrozenRate"]) for value in updates),
        Decimal(0),
    )
    completed_generation = sum(
        (
            Decimal(value["estimatedProviderCostUSDAtFrozenRates"])
            for value in [*sentinels, *scores]
        ),
        Decimal(0),
    )
    total_cost = completed_training + completed_generation + recovery_cost
    if total_cost > Decimal(manifest["authorization"]["maximumUSD"]):
        raise AuditError("frozen-rate total exceeds the authorized hard ceiling")

    overall = summarize(scores)
    audit = {
        "schemaVersion": 1,
        "auditVersion": "phase1-inkling-native-loss-stability-audit-v1",
        "status": "passed",
        "source": {
            "planSHA256": sha256(plan_path),
            "runManifestSHA256": sha256(manifest_path),
            "scoresSHA256": sha256(scores_path),
            "updatesSHA256": sha256(updates_path),
            "trainingBatchesSHA256": sha256(batches_path),
        },
        "protocol": {
            "model": manifest["provider"]["model"],
            "reasoning": "off",
            "trainingExamples": 200,
            "evaluationExamples": 174,
            "incrementalUpdates": 4,
            "optimizerSteps": 20,
            "terminalBlockUpdated": True,
            "trainingContract": manifest["provider"]["trainingContract"],
            "generationContract": manifest["provider"]["generationContract"],
        },
        "training": {
            "updates": [
                {
                    "updateOrdinal": value["updateOrdinal"],
                    "cumulativeTrainedExamples": value["cumulativeTrainedExamples"],
                    "meanPreUpdateNLL": value["meanPreUpdateNLL"],
                    "nativeLossTokenPresentations": value["nativeLossTokenPresentations"],
                    "latencySeconds": value["latencySeconds"],
                }
                for value in updates
            ]
        },
        "evaluation": {"overall": overall, "byStage": stage_summaries},
        "cost": {
            "completedTrainingUSDAtFrozenRate": str(completed_training),
            "canonicalGenerationUSDAtFrozenRates": str(completed_generation),
            "duplicateRaceGenerationUSDAtFrozenRates": str(recovery_cost),
            "totalUSDAtFrozenRates": str(total_cost),
            "authorizedHardCeilingUSD": manifest["authorization"]["maximumUSD"],
            "actualProviderCostUSD": None,
        },
        "recovery": {
            "concurrentResumeRaceRecovered": bool(recovery_rows),
            "recoveries": recovery_rows,
            "abandonedAttempts": manifest.get("abandonedAttempts", []),
        },
    }
    output.mkdir(parents=True)
    (output / "audit.json").write_text(
        json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    with (output / "stages.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "stage",
                "trained_examples",
                "examples",
                "accepted",
                "validity_rate",
                "exact_match_rate",
                "macro_character_similarity",
                "median_generation_latency_seconds",
                "mean_generation_latency_seconds",
            ],
        )
        writer.writeheader()
        for value in stage_summaries:
            metrics = value["generatedCompletion"]
            writer.writerow(
                {
                    "stage": value["stage"],
                    "trained_examples": value["trainedExamples"],
                    "examples": value["examples"],
                    "accepted": value["accepted"],
                    "validity_rate": value["validityRate"],
                    "exact_match_rate": metrics["exactMatchRate"],
                    "macro_character_similarity": metrics[
                        "macroNormalizedLevenshteinSimilarity"
                    ],
                    "median_generation_latency_seconds": value["generationLatency"][
                        "medianSeconds"
                    ],
                    "mean_generation_latency_seconds": value["generationLatency"][
                        "meanSeconds"
                    ],
                }
            )
    print(json.dumps(audit, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Audit and summarize all frozen Phase 1 frontier-model arc outputs."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from phase1_experiment import prospective_example_ids, validate_inputs
from phase1_frontier_model_arc import (
    ARC_VERSION,
    AUDIT_VERSION,
    MODEL_SPECS,
    PLAN_VERSION,
    FrontierArcError,
    atomic_json,
    load_jsonl,
    sha256,
    summarize_scores,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--packed", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--preflight", required=True, type=Path)
    parser.add_argument("--runs", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    corpus_path = arguments.corpus.expanduser().resolve()
    packed_path = arguments.packed.expanduser().resolve()
    plan_path = arguments.plan.expanduser().resolve()
    preflight_path = arguments.preflight.expanduser().resolve()
    runs_path = arguments.runs.expanduser().resolve()
    output = arguments.output.expanduser().resolve()
    if output.exists():
        raise FrontierArcError(f"output already exists: {output}")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    if plan.get("planVersion") != PLAN_VERSION or plan.get("arcVersion") != ARC_VERSION:
        raise FrontierArcError("unsupported frontier-arc plan")
    if preflight.get("planSHA256") != sha256(plan_path):
        raise FrontierArcError("preflight is not bound to this plan")
    preflight_by_key = {
        value["model"]["key"]: value for value in preflight.get("results", [])
    }
    if set(preflight_by_key) != {value["key"] for value in MODEL_SPECS}:
        raise FrontierArcError("preflight does not cover every requested model")
    corpus, _, _, _ = validate_inputs(corpus_path, packed_path)
    expected_ids = prospective_example_ids(corpus["blocking"]["blocks"])
    summaries: dict[str, dict] = {}
    sources: dict[str, dict] = {}
    unavailable: dict[str, dict] = {}
    for spec in MODEL_SPECS:
        preflight_row = preflight_by_key[spec["key"]]
        if preflight_row.get("status") != "passed":
            unavailable[spec["key"]] = {
                "model": spec,
                "disposition": "unavailable_through_requested_chatgpt_subscription_pathway",
                "providerPreflightStatus": preflight_row.get("status"),
                "providerError": preflight_row.get("error"),
            }
            continue
        directory = runs_path / spec["key"]
        manifest_path = directory / "model.json"
        scores_path = directory / "scores.jsonl"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        scores = load_jsonl(scores_path)
        if not (
            manifest.get("status") == "complete"
            and manifest.get("model") == spec
            and manifest.get("source", {}).get("planSHA256") == sha256(plan_path)
            and manifest.get("source", {}).get("preflightSHA256")
            == sha256(preflight_path)
            and manifest.get("source", {}).get("corpusSHA256") == sha256(corpus_path / "corpus.json")
            and manifest.get("artifactDigestsSHA256", {}).get("scores.jsonl") == sha256(scores_path)
            and [value.get("exampleID") for value in scores] == expected_ids
            and all(value.get("responseModel") == spec["requestedModel"] for value in scores)
        ):
            raise FrontierArcError(f"model output failed lineage audit: {spec['key']}")
        summaries[spec["key"]] = summarize_scores(scores)
        sources[spec["key"]] = {
            "directory": str(directory),
            "manifestSHA256": sha256(manifest_path),
            "scoresSHA256": sha256(scores_path),
        }
    comparator = plan["existingComparator"]
    comparator_path = Path(comparator["directory"])
    comparator_scores = load_jsonl(comparator_path / "scores.jsonl")
    if not (
        sha256(comparator_path / "frontier.json") == comparator["manifestSHA256"]
        and sha256(comparator_path / "scores.jsonl") == comparator["scoresSHA256"]
        and [value.get("exampleID") for value in comparator_scores] == expected_ids
    ):
        raise FrontierArcError("existing GPT-5.6 comparator changed")
    normalized_comparator = []
    for row in comparator_scores:
        from phase1_frontier_model_arc import add_prediction_metrics
        normalized_comparator.append(add_prediction_metrics({
            **row,
            "pasteActionCount": int(row.get("pasteActionCount") or 0),
        }))
    summaries[comparator["key"]] = summarize_scores(normalized_comparator)
    sources[comparator["key"]] = {
        "directory": str(comparator_path),
        "manifestSHA256": comparator["manifestSHA256"],
        "scoresSHA256": comparator["scoresSHA256"],
        "reusedExistingArtifact": True,
    }
    output.mkdir(parents=True)
    report = {
        "schemaVersion": 1,
        "auditVersion": AUDIT_VERSION,
        "arcVersion": ARC_VERSION,
        "status": "passed_with_unavailable_requested_models" if unavailable else "passed",
        "examplesPerModel": len(expected_ids),
        "totalNewDatasetSubscriptionCalls": len(expected_ids)
        * (len(MODEL_SPECS) - len(unavailable)),
        "sameSemanticInputsAcrossModels": True,
        "source": {
            "corpusID": corpus["corpusID"],
            "corpusSHA256": sha256(corpus_path / "corpus.json"),
            "packingSHA256": sha256(packed_path / "packing.json"),
            "planSHA256": sha256(plan_path),
            "preflightSHA256": sha256(preflight_path),
            "models": sources,
        },
        "summaries": summaries,
        "unavailableModels": unavailable,
    }
    atomic_json(output / "arc.json", report)
    with (output / "models.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "model", "examples", "exact_matches", "macro_similarity",
            "micro_similarity", "mean_prefix_characters", "median_latency_seconds",
            "mean_latency_seconds", "input_tokens", "output_tokens", "reasoning_tokens",
        ])
        for spec in MODEL_SPECS:
            key = spec["key"]
            if key in unavailable:
                writer.writerow([key, 0, "", "", "", "", "", "", "", "", ""])
                continue
            summary = summaries[key]
            generated = summary["generatedCompletion"]
            writer.writerow([
                key, summary["examples"], generated["exactMatches"],
                generated["macroNormalizedLevenshteinSimilarity"],
                generated["microNormalizedLevenshteinSimilarity"],
                generated["correctPrefix"]["meanCharactersPerExample"],
                summary["latency"]["medianSeconds"], summary["latency"]["meanSeconds"],
                summary["usage"]["input_tokens"], summary["usage"]["output_tokens"],
                summary["usage"]["reasoning_tokens"],
            ])
        key = comparator["key"]
        summary = summaries[key]
        generated = summary["generatedCompletion"]
        writer.writerow([
            key, summary["examples"], generated["exactMatches"],
            generated["macroNormalizedLevenshteinSimilarity"],
            generated["microNormalizedLevenshteinSimilarity"],
            generated["correctPrefix"]["meanCharactersPerExample"],
            summary["latency"]["medianSeconds"], summary["latency"]["meanSeconds"],
            summary["usage"]["input_tokens"], summary["usage"]["output_tokens"],
            summary["usage"]["reasoning_tokens"],
        ])
    report["artifactDigestsSHA256"] = {"models.csv": sha256(output / "models.csv")}
    atomic_json(output / "arc.json", report)
    print(f"Frontier model arc audit passed: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

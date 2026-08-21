#!/usr/bin/env python3
"""Audit GPT-5.6 Sol xhigh across reused 32K and new 128K history packs."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from phase1_context_window_ablation import (
    ABLATION_VERSION,
    AUDIT_VERSION,
    MODEL,
    PLAN_VERSION,
    RUNNER_VERSION,
    WINDOWS,
    ContextWindowError,
    atomic_json,
    load_jsonl,
    sha256,
    summarize,
)
from phase1_inkling import load_experiment_blocks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--runs", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    output = arguments.output.expanduser().resolve()
    if output.exists():
        raise ContextWindowError(f"output already exists: {output}")
    corpus_path = arguments.corpus.expanduser().resolve()
    plan_path = arguments.plan.expanduser().resolve()
    runs_path = arguments.runs.expanduser().resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if not (
        plan.get("planVersion") == PLAN_VERSION
        and plan.get("ablationVersion") == ABLATION_VERSION
        and plan.get("model") == MODEL
    ):
        raise ContextWindowError("unsupported context-window plan")
    packs = {
        key: Path(value["directory"])
        for key, value in plan["source"]["packs"].items()
    }
    corpus = json.loads((corpus_path / "corpus.json").read_text(encoding="utf-8"))
    blocks = load_experiment_blocks(corpus_path)
    expected_ids = [value for block in blocks[1:] for value in block["exampleIDs"]]
    plans_by_window = {
        key: {
            value["exampleID"]: value
            for value in load_jsonl(pack / "semantic-examples.jsonl")
        }
        for key, pack in packs.items()
    }
    for example_id in expected_ids:
        prior_ids: list[str] = []
        query_hash = None
        instruction = None
        for key in WINDOWS:
            value = plans_by_window[key][example_id]
            ids = [item["contextBlockID"] for item in value["retainedContextBlocks"]]
            if prior_ids and ids[-len(prior_ids) :] != prior_ids:
                raise ContextWindowError(
                    f"retained history is not nested for {example_id}: {key}"
                )
            if query_hash is not None and value["rightEdgeQuerySHA256"] != query_hash:
                raise ContextWindowError("conditioning query changed across windows")
            if instruction is not None and value["taskInstruction"] != instruction:
                raise ContextWindowError("task instruction changed across windows")
            prior_ids = ids
            query_hash = value["rightEdgeQuerySHA256"]
            instruction = value["taskInstruction"]

    sources = {}
    summaries = {}
    all_scores = {}
    for key in WINDOWS:
        if key == "32k":
            directory = Path(plan["existing32KComparator"]["directory"])
            manifest_path = directory / "frontier.json"
            scores_path = directory / "scores.jsonl"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            scores = load_jsonl(scores_path)
            if not (
                sha256(manifest_path) == plan["existing32KComparator"]["manifestSHA256"]
                and sha256(scores_path) == plan["existing32KComparator"]["scoresSHA256"]
                and manifest.get("source", {}).get("packingSHA256")
                == plan["source"]["reference32KPackingSHA256"]
            ):
                raise ContextWindowError("32K comparator changed")
        else:
            directory = runs_path / key
            manifest_path = directory / "window.json"
            scores_path = directory / "scores.jsonl"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            scores = load_jsonl(scores_path)
            if not (
                manifest.get("status") == "complete"
                and manifest.get("runnerVersion") == RUNNER_VERSION
                and manifest.get("windowKey") == key
                and manifest.get("inputTokenBudget") == WINDOWS[key]
                and manifest.get("model") == MODEL
                and manifest.get("source", {}).get("planSHA256") == sha256(plan_path)
                and manifest.get("source", {}).get("packingSHA256")
                == plan["source"]["packs"][key]["packingSHA256"]
                and manifest.get("artifactDigestsSHA256", {}).get("scores.jsonl")
                == sha256(scores_path)
            ):
                raise ContextWindowError(f"{key} run lineage differs")
        if not (
            [value.get("exampleID") for value in scores] == expected_ids
            and all(value.get("responseModel") == MODEL["requestedModel"] for value in scores)
            and all(value.get("requestedReasoningEffort") == MODEL["reasoningEffort"] for value in scores)
        ):
            raise ContextWindowError(f"{key} scores differ from protocol")
        all_scores[key] = scores
        summaries[key] = summarize(scores)
        sources[key] = {
            "directory": str(directory),
            "manifestSHA256": sha256(manifest_path),
            "scoresSHA256": sha256(scores_path),
            "packingSHA256": plan["source"]["packs"][key]["packingSHA256"],
            "reusedExistingArtifact": key == "32k",
        }

    output.mkdir(parents=True)
    report = {
        "schemaVersion": 1,
        "auditVersion": AUDIT_VERSION,
        "ablationVersion": ABLATION_VERSION,
        "status": "passed",
        "model": MODEL,
        "examplesPerWindow": len(expected_ids),
        "sameTargetsQueriesInstructionAndExampleOrder": True,
        "retainedHistoryNestedMonotonically": True,
        "windowBudgetsMeasuredInFrozen32KComparatorTokenizer": True,
        "actualProviderInputUsageRecordedPerQuery": True,
        "source": {
            "corpusID": corpus["corpusID"],
            "corpusSHA256": sha256(corpus_path / "corpus.json"),
            "planSHA256": sha256(plan_path),
            "windows": sources,
        },
        "summaries": summaries,
    }
    atomic_json(output / "context-windows.json", report)
    with (output / "context-windows.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "window", "input_token_budget", "examples", "exact_matches",
            "macro_similarity", "micro_similarity", "mean_prefix_characters",
            "median_latency_seconds", "mean_latency_seconds", "input_tokens",
            "output_tokens", "reasoning_tokens", "api_equivalent_cost_usd",
        ])
        for key, budget in WINDOWS.items():
            summary = summaries[key]
            generated = summary["generatedCompletion"]
            cost = summary["apiEquivalentCost"]
            writer.writerow([
                key, budget, summary["examples"], generated["exactMatches"],
                generated["macroNormalizedLevenshteinSimilarity"],
                generated["microNormalizedLevenshteinSimilarity"],
                generated["correctPrefix"]["meanCharactersPerExample"],
                summary["latency"]["medianSeconds"], summary["latency"]["meanSeconds"],
                cost["inputTokens"], cost["outputTokens"], summary["reasoningTokens"],
                cost["totalUSD"],
            ])
    report["artifactDigestsSHA256"] = {
        "context-windows.csv": sha256(output / "context-windows.csv")
    }
    atomic_json(output / "context-windows.json", report)
    print(f"Context-window audit passed: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

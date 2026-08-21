#!/usr/bin/env python3
"""Run one resumable GPT-5.6 Sol context-window arm via ChatGPT subscription."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import time
import traceback
from pathlib import Path
from typing import Any

from phase1_context_window_ablation import (
    ABLATION_VERSION,
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
from phase1_frontier_model_arc import add_prediction_metrics, append_jsonl
from phase1_subscription_responses import request_completion, require_loopback_url
from phase1_training_contract import git_revision, git_worktree_dirty


def iso8601() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--packed", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--window-key", required=True, choices=["8k", "16k", "64k"])
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--endpoint", default="http://127.0.0.1:4000/v1/responses")
    parser.add_argument("--maximum-calls", required=True, type=int)
    parser.add_argument("--confirm-personal-data-transfer", action="store_true")
    parser.add_argument("--confirm-subscription-usage", action="store_true")
    parser.add_argument("--execute", action="store_true")
    arguments = parser.parse_args()
    for value, flag in (
        (arguments.confirm_personal_data_transfer, "--confirm-personal-data-transfer"),
        (arguments.confirm_subscription_usage, "--confirm-subscription-usage"),
        (arguments.execute, "--execute"),
    ):
        if not value:
            parser.error(f"{flag} is required")
    return arguments


def implementation_record(plan: dict[str, Any]) -> dict[str, Any]:
    project = Path(__file__).resolve().parent.parent
    return {
        "codeRevision": git_revision(project),
        "workingTreeDirtyAtStart": git_worktree_dirty(project),
        "fileDigestsSHA256": {
            relative: sha256(project / relative)
            for relative in plan["implementation"]["fileDigestsSHA256"]
        },
    }


def run() -> int:
    arguments = parse_arguments()
    corpus_path = arguments.corpus.expanduser().resolve()
    packed_path = arguments.packed.expanduser().resolve()
    plan_path = arguments.plan.expanduser().resolve()
    output = arguments.output.expanduser().resolve()
    endpoint = require_loopback_url(arguments.endpoint)
    if sha256(plan_path) != arguments.plan_sha256:
        raise ContextWindowError("plan SHA-256 differs from authorization")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if not (
        plan.get("planVersion") == PLAN_VERSION
        and plan.get("ablationVersion") == ABLATION_VERSION
        and plan.get("status") == "local_plan_only_no_authentication_or_data_transfer"
        and plan.get("model") == MODEL
        and plan.get("source", {}).get("packs", {}).get(arguments.window_key, {}).get("directory")
        == str(packed_path)
    ):
        raise ContextWindowError("plan contract differs")
    project = Path(__file__).resolve().parent.parent
    for relative, expected in plan["implementation"]["fileDigestsSHA256"].items():
        if sha256(project / relative) != expected:
            raise ContextWindowError(f"planned implementation changed: {relative}")
    pack_record = plan["source"]["packs"][arguments.window_key]
    if not (
        pack_record["inputTokenBudget"] == WINDOWS[arguments.window_key]
        and pack_record["packingSHA256"] == sha256(packed_path / "packing.json")
        and pack_record["semanticExamplesSHA256"] == sha256(packed_path / "semantic-examples.jsonl")
    ):
        raise ContextWindowError("window pack changed")
    corpus = json.loads((corpus_path / "corpus.json").read_text(encoding="utf-8"))
    blocks = load_experiment_blocks(corpus_path)
    expected_ids = [value for block in blocks[1:] for value in block["exampleIDs"]]
    all_rows = load_jsonl(packed_path / "semantic-examples.jsonl")
    examples = [value for value in all_rows if value["exampleID"] in set(expected_ids)]
    if [value["exampleID"] for value in examples] != expected_ids:
        raise ContextWindowError("prospective example order differs")
    if arguments.maximum_calls != len(examples):
        raise ContextWindowError(f"--maximum-calls must equal {len(examples)}")
    current = implementation_record(plan)
    if current["workingTreeDirtyAtStart"]:
        raise ContextWindowError("context-window execution requires a clean worktree")
    manifest_path = output / "window.json"
    scores_path = output / "scores.jsonl"
    if output.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("inflightOperation"):
            raise ContextWindowError("uncertain subscription call cannot be replayed")
        if not (
            manifest.get("implementation") == current
            and manifest.get("source", {}).get("planSHA256") == arguments.plan_sha256
            and manifest.get("windowKey") == arguments.window_key
        ):
            raise ContextWindowError("existing output lineage differs")
        scores = load_jsonl(scores_path) if scores_path.exists() else []
    else:
        output.mkdir(parents=True)
        scores = []
        manifest = {
            "schemaVersion": 1,
            "runnerVersion": RUNNER_VERSION,
            "ablationVersion": ABLATION_VERSION,
            "status": "initialized",
            "startedAt": iso8601(),
            "implementation": current,
            "source": {
                "corpusID": corpus["corpusID"],
                "corpusSHA256": sha256(corpus_path / "corpus.json"),
                "packingSHA256": sha256(packed_path / "packing.json"),
                "semanticExamplesSHA256": sha256(packed_path / "semantic-examples.jsonl"),
                "planSHA256": arguments.plan_sha256,
            },
            "model": MODEL,
            "windowKey": arguments.window_key,
            "inputTokenBudget": WINDOWS[arguments.window_key],
            "transport": {
                "type": "loopback_litellm_chatgpt_subscription_responses",
                "endpoint": endpoint,
                "openAIAPIKeyFallbackAllowed": False,
            },
            "counts": {"completedCalls": 0, "expectedCalls": len(examples)},
        }
        atomic_json(manifest_path, manifest)
    if [value.get("exampleID") for value in scores] != expected_ids[: len(scores)]:
        raise ContextWindowError("scores are not an ordered prefix")
    if manifest.get("status") == "complete":
        if len(scores) != len(examples):
            raise ContextWindowError("complete arm lacks scores")
        return 0
    manifest["status"] = "running"
    atomic_json(manifest_path, manifest)
    for ordinal, example in enumerate(examples[len(scores) :], len(scores) + 1):
        example_id = example["exampleID"]
        model_input = example["semanticModelInput"]
        target = example["target"]
        manifest["inflightOperation"] = {
            "ordinal": ordinal,
            "exampleID": example_id,
            "replayAllowedAutomatically": False,
        }
        atomic_json(manifest_path, manifest)
        started = time.monotonic()
        prediction, response = request_completion(
            endpoint,
            model_input,
            local_proxy_key=os.environ.get("LITELLM_PROXY_KEY"),
            model=MODEL["route"],
            reasoning_effort=MODEL["reasoningEffort"],
        )
        latency = time.monotonic() - started
        if response.get("model") != MODEL["requestedModel"]:
            raise ContextWindowError("provider resolved an unexpected model")
        record = add_prediction_metrics({
            "schemaVersion": 1,
            "runnerVersion": RUNNER_VERSION,
            "ordinal": ordinal,
            "blockID": example["experimentBlockID"],
            "windowKey": arguments.window_key,
            "inputTokenBudget": WINDOWS[arguments.window_key],
            "requestedModel": MODEL["requestedModel"],
            "requestedReasoningEffort": MODEL["reasoningEffort"],
            "exampleID": example_id,
            "targetEventID": example["targetEventID"],
            "application": example.get("application"),
            "semanticModelInputSHA256": example["semanticModelInputSHA256"],
            "semanticModelInputUTF8Bytes": len(model_input.encode()),
            "canonicalPackingTokenCount": example["canonicalPackingTokenCount"],
            "retainedContextBlockCount": len(example["retainedContextBlocks"]),
            "target": target,
            "pasteActionCount": example["pasteActionCount"],
            "prediction": prediction,
            "predictionSHA256": hashlib.sha256(prediction.encode()).hexdigest(),
            "latencySeconds": latency,
            "responseID": response.get("id"),
            "responseModel": response.get("model"),
            "usage": response.get("usage") or {},
            "completedAt": iso8601(),
        })
        append_jsonl(scores_path, record)
        scores.append(record)
        manifest.pop("inflightOperation", None)
        manifest["counts"]["completedCalls"] = len(scores)
        manifest["lastCompletedExampleID"] = example_id
        atomic_json(manifest_path, manifest)
        print(
            f"gpt56-window {arguments.window_key} {ordinal:03d}/{len(examples)} "
            f"exact={record['predictionMetrics']['exactMatch']} latency={latency:.2f}s",
            flush=True,
        )
    manifest["status"] = "complete"
    manifest["completedAt"] = iso8601()
    manifest["summary"] = summarize(scores)
    manifest["artifactDigestsSHA256"] = {"scores.jsonl": sha256(scores_path)}
    atomic_json(manifest_path, manifest)
    return 0


def main() -> int:
    try:
        return run()
    except KeyboardInterrupt:
        raise SystemExit("run-phase1-context-window-ablation: interrupted")
    except Exception as error:
        try:
            arguments = parse_arguments()
            manifest_path = arguments.output.expanduser().resolve() / "window.json"
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["status"] = "interrupted"
                manifest["interruptedAt"] = iso8601()
                manifest["failure"] = {
                    "type": type(error).__name__,
                    "message": str(error),
                    "traceback": traceback.format_exc(),
                }
                atomic_json(manifest_path, manifest)
        finally:
            raise


if __name__ == "__main__":
    raise SystemExit(main())

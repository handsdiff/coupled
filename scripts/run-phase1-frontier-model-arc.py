#!/usr/bin/env python3
"""Run one resumable model arm from the frozen Phase 1 frontier-model arc."""

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

from phase1_experiment import prospective_example_ids, semantic_model_input, target_text, validate_inputs
from phase1_frontier_model_arc import (
    ARC_VERSION,
    PLAN_VERSION,
    RUNNER_VERSION,
    FrontierArcError,
    add_prediction_metrics,
    append_jsonl,
    atomic_json,
    load_jsonl,
    model_spec,
    sha256,
    summarize_scores,
)
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
    parser.add_argument("--preflight", required=True, type=Path)
    parser.add_argument("--preflight-sha256", required=True)
    parser.add_argument("--model-key", required=True)
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


def validate_plan(path: Path, digest: str, corpus: Path, packed: Path, key: str) -> tuple[dict[str, Any], dict[str, str]]:
    if sha256(path) != digest:
        raise FrontierArcError("plan SHA-256 differs from authorization")
    plan = json.loads(path.read_text(encoding="utf-8"))
    if not (
        plan.get("planVersion") == PLAN_VERSION
        and plan.get("arcVersion") == ARC_VERSION
        and plan.get("status") == "local_plan_only_no_authentication_or_data_transfer"
        and plan.get("transport", {}).get("openAIAPIKeyFallbackAllowed") is False
    ):
        raise FrontierArcError("unsupported model-arc plan")
    project = Path(__file__).resolve().parent.parent
    for relative, expected in plan["implementation"]["fileDigestsSHA256"].items():
        if sha256(project / relative) != expected:
            raise FrontierArcError(f"model-arc implementation changed: {relative}")
    expected_source = {
        "corpusSHA256": sha256(corpus / "corpus.json"),
        "examplesSHA256": sha256(corpus / "examples.jsonl"),
        "packingSHA256": sha256(packed / "packing.json"),
        "packedExamplesSHA256": sha256(packed / "packed-examples.jsonl"),
        "contextPlansSHA256": sha256(packed / "context-plans.jsonl"),
    }
    for name, expected in expected_source.items():
        if plan["source"].get(name) != expected:
            raise FrontierArcError(f"model-arc source changed: {name}")
    spec = model_spec(key)
    if spec not in plan["models"]:
        raise FrontierArcError("requested model is absent from plan")
    return plan, spec


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
    preflight_path = arguments.preflight.expanduser().resolve()
    output = arguments.output.expanduser().resolve()
    endpoint = require_loopback_url(arguments.endpoint)
    plan, spec = validate_plan(
        plan_path, arguments.plan_sha256, corpus_path, packed_path, arguments.model_key
    )
    if sha256(preflight_path) != arguments.preflight_sha256:
        raise FrontierArcError("preflight SHA-256 differs from authorization")
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    preflight_rows = [
        value for value in preflight.get("results", []) if value.get("model") == spec
    ]
    if not (
        preflight.get("planSHA256") == arguments.plan_sha256
        and len(preflight_rows) == 1
        and preflight_rows[0].get("status") == "passed"
        and preflight_rows[0].get("responseModel") == spec["requestedModel"]
    ):
        raise FrontierArcError("model did not pass the bound authenticated preflight")
    corpus, all_examples, _, plans = validate_inputs(corpus_path, packed_path)
    expected_ids = prospective_example_ids(corpus["blocking"]["blocks"])
    examples = [value for value in all_examples if value["exampleID"] in set(expected_ids)]
    if [value["exampleID"] for value in examples] != expected_ids:
        raise FrontierArcError("prospective model-arc order differs")
    if arguments.maximum_calls != len(examples):
        raise FrontierArcError(f"maximum calls must equal {len(examples)}")
    current_implementation = implementation_record(plan)
    if current_implementation["workingTreeDirtyAtStart"]:
        raise FrontierArcError("model-arc execution requires a clean working tree")
    manifest_path = output / "model.json"
    scores_path = output / "scores.jsonl"
    if output.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("inflightOperation"):
            raise FrontierArcError("uncertain subscription call cannot be replayed automatically")
        if not (
            manifest.get("implementation") == current_implementation
            and manifest.get("source", {}).get("planSHA256") == arguments.plan_sha256
            and manifest.get("source", {}).get("preflightSHA256")
            == arguments.preflight_sha256
            and manifest.get("model") == spec
        ):
            raise FrontierArcError("existing model-arc output has different lineage")
        scores = load_jsonl(scores_path) if scores_path.exists() else []
    else:
        output.mkdir(parents=True)
        scores = []
        manifest = {
            "schemaVersion": 1,
            "runnerVersion": RUNNER_VERSION,
            "arcVersion": ARC_VERSION,
            "status": "initialized",
            "startedAt": iso8601(),
            "implementation": current_implementation,
            "source": {
                "corpusID": corpus["corpusID"],
                "corpusSHA256": sha256(corpus_path / "corpus.json"),
                "packingSHA256": sha256(packed_path / "packing.json"),
                "planSHA256": arguments.plan_sha256,
                "preflightSHA256": arguments.preflight_sha256,
            },
            "model": spec,
            "transport": {
                "endpoint": endpoint,
                "type": "loopback_litellm_chatgpt_subscription_responses",
                "openAIAPIKeyFallbackAllowed": False,
            },
            "authorization": {
                "personalDataTransferConfirmed": True,
                "subscriptionUsageConfirmed": True,
                "maximumLogicalCalls": len(examples),
            },
            "counts": {"completedCalls": 0, "expectedCalls": len(examples)},
        }
        atomic_json(manifest_path, manifest)
    if [value.get("exampleID") for value in scores] != expected_ids[: len(scores)]:
        raise FrontierArcError("model-arc scores are not an ordered prefix")
    if manifest.get("status") == "complete":
        if len(scores) != len(examples):
            raise FrontierArcError("complete model arm lacks scores")
        print(f"Model arm already complete: {output}")
        return 0
    manifest["status"] = "running"
    atomic_json(manifest_path, manifest)
    for ordinal, example in enumerate(examples[len(scores):], len(scores) + 1):
        example_id = example["exampleID"]
        model_input = semantic_model_input(corpus_path, example, plans[example_id])
        target = target_text(example["target"])
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
            model=spec["route"],
            reasoning_effort=spec["reasoningEffort"],
        )
        latency = time.monotonic() - started
        if response.get("model") != spec["requestedModel"]:
            raise FrontierArcError(
                f"provider resolved {response.get('model')!r}, expected {spec['requestedModel']!r}"
            )
        record = add_prediction_metrics({
            "schemaVersion": 1,
            "runnerVersion": RUNNER_VERSION,
            "ordinal": ordinal,
            "blockID": example["experimentBlockID"],
            "modelKey": spec["key"],
            "requestedModel": spec["requestedModel"],
            "requestedReasoningEffort": spec["reasoningEffort"],
            "exampleID": example_id,
            "targetEventID": example["targetEventID"],
            "application": example.get("conditioningState", {}).get("destination", {}).get("appName"),
            "semanticModelInputSHA256": plans[example_id]["semanticModelInputSHA256"],
            "semanticModelInputUTF8Bytes": len(model_input.encode()),
            "target": target,
            "pasteActionCount": sum(
                segment.get("type") == "paste"
                for segment in example["target"].get("segments", [])
            ),
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
            f"frontier-arc {spec['key']} {ordinal:03d}/{len(examples)} "
            f"exact={record['predictionMetrics']['exactMatch']} latency={latency:.2f}s",
            flush=True,
        )
    manifest["status"] = "complete"
    manifest["completedAt"] = iso8601()
    manifest["summary"] = summarize_scores(scores)
    manifest["artifactDigestsSHA256"] = {"scores.jsonl": sha256(scores_path)}
    atomic_json(manifest_path, manifest)
    print(f"Frontier model arm complete: {output}")
    return 0


def main() -> int:
    try:
        return run()
    except KeyboardInterrupt:
        raise SystemExit("run-phase1-frontier-model-arc: interrupted")
    except Exception as error:
        try:
            arguments = parse_arguments()
            manifest_path = arguments.output.expanduser().resolve() / "model.json"
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["status"] = "interrupted"
                manifest["interruptedAt"] = iso8601()
                manifest["failure"] = {
                    "type": type(error).__name__,
                    "message": str(error),
                    "traceback": traceback.format_exc(),
                    "rawProviderErrorPersisted": False,
                }
                atomic_json(manifest_path, manifest)
        except Exception:
            pass
        raise SystemExit(f"run-phase1-frontier-model-arc: {type(error).__name__}: {error}")


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run the resumable GPT-5.6-sol arm of the frozen Phase 1 experiment."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import time
import traceback
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from phase1_experiment import (
    ARM_FROZEN_FRONTIER,
    RUNNER_VERSION,
    canonical_bytes,
    load_jsonl,
    semantic_model_input,
    target_text,
    validate_inputs,
)
from phase1_subscription_responses import (
    MODEL,
    REASONING_EFFORT,
    SubscriptionResponseError,
    request_completion,
)
from phase1_training_contract import (
    TrainingContractError,
    git_revision,
    git_worktree_dirty,
    sha256,
)


FRONTIER_RUNNER_VERSION = "phase1-frontier-arm-v1"
EXPECTED_PLAN_VERSION = "phase1-provider-plan-v3"


def iso8601() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(canonical_bytes(value))
    os.replace(temporary, path)


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    with path.open("ab") as handle:
        handle.write(canonical_bytes(value))
        handle.flush()
        os.fsync(handle.fileno())


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--packed", required=True, type=Path)
    parser.add_argument("--provider-plan", required=True, type=Path)
    parser.add_argument("--provider-plan-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--endpoint", default="http://127.0.0.1:4000/v1/responses")
    parser.add_argument("--maximum-calls", required=True, type=int)
    parser.add_argument("--confirm-personal-data-transfer", action="store_true")
    parser.add_argument("--confirm-subscription-usage", action="store_true")
    parser.add_argument("--execute", action="store_true")
    arguments = parser.parse_args()
    for enabled, flag in (
        (arguments.confirm_personal_data_transfer, "--confirm-personal-data-transfer"),
        (arguments.confirm_subscription_usage, "--confirm-subscription-usage"),
        (arguments.execute, "--execute"),
    ):
        if not enabled:
            parser.error(f"{flag} is required")
    return arguments


def validate_plan(
    path: Path,
    expected_digest: str,
    corpus_path: Path,
    packed_path: Path,
    example_count: int,
) -> tuple[dict[str, Any], str]:
    actual = sha256(path)
    if actual != expected_digest:
        raise TrainingContractError("provider plan SHA-256 differs from approval")
    plan = json.loads(path.read_text(encoding="utf-8"))
    if plan.get("planVersion") != EXPECTED_PLAN_VERSION:
        raise TrainingContractError("unsupported provider plan version")
    project = Path(__file__).resolve().parent.parent
    for relative, expected in plan.get("implementation", {}).get(
        "fileDigestsSHA256", {}
    ).items():
        current = project / relative
        if not current.is_file() or sha256(current) != expected:
            raise TrainingContractError(
                f"provider plan implementation changed: {relative}"
            )
    source = plan.get("source", {})
    expected_source = {
        "corpusSHA256": sha256(corpus_path / "corpus.json"),
        "examplesSHA256": sha256(corpus_path / "examples.jsonl"),
        "packingSHA256": sha256(packed_path / "packing.json"),
        "packedExamplesSHA256": sha256(packed_path / "packed-examples.jsonl"),
        "contextPlansSHA256": sha256(packed_path / "context-plans.jsonl"),
    }
    for key, value in expected_source.items():
        if source.get(key) != value:
            raise TrainingContractError(f"provider plan source changed: {key}")
    frontier = plan.get("openai", {})
    if not (
        frontier.get("transport") == "litellm_chatgpt_subscription"
        and frontier.get("providerModelRoute") == MODEL
        and frontier.get("model") == "gpt-5.6-sol"
        and frontier.get("reasoningEffort") == REASONING_EFFORT
        and frontier.get("openAIAPIKeyFallbackAllowed") is False
        and frontier.get("operations", {}).get("responseCalls") == example_count
    ):
        raise TrainingContractError("provider plan does not match frontier contract")
    return plan, actual


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


def validate_resume_implementation(
    manifest: dict[str, Any], current: dict[str, Any]
) -> None:
    if current["workingTreeDirtyAtStart"]:
        raise TrainingContractError("frontier resume requires a clean working tree")
    if manifest.get("implementation") != current:
        raise TrainingContractError(
            "frontier resume implementation or Git revision changed"
        )


def create_manifest(
    output: Path,
    corpus: dict[str, Any],
    corpus_path: Path,
    packed_path: Path,
    plan_path: Path,
    plan_digest: str,
    plan: dict[str, Any],
    examples: list[dict[str, Any]],
) -> dict[str, Any]:
    output.mkdir(parents=True)
    manifest = {
        "schemaVersion": 1,
        "runnerVersion": FRONTIER_RUNNER_VERSION,
        "phase1ProtocolVersion": RUNNER_VERSION,
        "status": "initialized",
        "startedAt": iso8601(),
        "implementation": implementation_record(plan),
        "source": {
            "corpusID": corpus["corpusID"],
            "corpusSHA256": sha256(corpus_path / "corpus.json"),
            "examplesSHA256": sha256(corpus_path / "examples.jsonl"),
            "packingSHA256": sha256(packed_path / "packing.json"),
            "packedExamplesSHA256": sha256(packed_path / "packed-examples.jsonl"),
            "contextPlansSHA256": sha256(packed_path / "context-plans.jsonl"),
            "providerPlanPath": str(plan_path),
            "providerPlanSHA256": plan_digest,
        },
        "provider": {
            "arm": ARM_FROZEN_FRONTIER,
            "route": MODEL,
            "requestedReasoningEffort": REASONING_EFFORT,
            "transport": "loopback_litellm_chatgpt_subscription",
            "openAIAPIKeyFallbackAllowed": False,
        },
        "authorization": {
            "personalDataTransferConfirmed": True,
            "subscriptionUsageConfirmed": True,
            "maximumLogicalCalls": len(examples),
        },
        "expectedExampleIDs": [value["exampleID"] for value in examples],
        "counts": {"completedCalls": 0, "expectedCalls": len(examples)},
    }
    atomic_json(output / "frontier.json", manifest)
    return manifest


def load_or_create(
    output: Path,
    corpus: dict[str, Any],
    corpus_path: Path,
    packed_path: Path,
    plan_path: Path,
    plan_digest: str,
    plan: dict[str, Any],
    examples: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest_path = output / "frontier.json"
    scores_path = output / "scores.jsonl"
    if not output.exists():
        return (
            create_manifest(
                output, corpus, corpus_path, packed_path, plan_path, plan_digest, plan,
                examples
            ),
            [],
        )
    if not manifest_path.is_file():
        raise TrainingContractError("existing output lacks frontier.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    current_implementation = implementation_record(plan)
    validate_resume_implementation(manifest, current_implementation)
    if not (
        manifest.get("runnerVersion") == FRONTIER_RUNNER_VERSION
        and manifest.get("source", {}).get("providerPlanSHA256") == plan_digest
        and manifest.get("source", {}).get("corpusSHA256")
        == sha256(corpus_path / "corpus.json")
        and manifest.get("source", {}).get("packingSHA256")
        == sha256(packed_path / "packing.json")
    ):
        raise TrainingContractError("existing frontier output has different lineage")
    scores = load_jsonl(scores_path) if scores_path.exists() else []
    expected = [value["exampleID"] for value in examples]
    observed = [value.get("exampleID") for value in scores]
    if observed != expected[: len(observed)] or len(scores) > len(examples):
        raise TrainingContractError("frontier scores are not an ordered corpus prefix")
    if manifest.get("status") == "complete" and len(scores) != len(examples):
        raise TrainingContractError("complete frontier manifest lacks scores")
    return manifest, scores


def finish_summary(scores: list[dict[str, Any]]) -> dict[str, Any]:
    usage_keys = ("input_tokens", "output_tokens", "total_tokens")
    usage = {
        key: sum(int(row.get("usage", {}).get(key) or 0) for row in scores)
        for key in usage_keys
    }
    reasoning = sum(
        int(row.get("usage", {}).get("output_tokens_details", {}).get("reasoning_tokens") or 0)
        for row in scores
    )
    return {
        "examples": len(scores),
        "exactMatches": sum(bool(row["exactMatch"]) for row in scores),
        "normalizedExactMatches": sum(bool(row["normalizedExactMatch"]) for row in scores),
        "meanCharacterSimilarity": sum(row["characterSimilarity"] for row in scores)
        / len(scores),
        "latencySeconds": sum(row["latencySeconds"] for row in scores),
        "usage": {**usage, "reasoning_tokens": reasoning},
    }


def run() -> int:
    arguments = parse_arguments()
    corpus_path = arguments.corpus.expanduser().resolve()
    packed_path = arguments.packed.expanduser().resolve()
    plan_path = arguments.provider_plan.expanduser().resolve()
    output = arguments.output.expanduser().resolve()
    corpus, examples, _, plans = validate_inputs(corpus_path, packed_path)
    if arguments.maximum_calls != len(examples):
        raise TrainingContractError(
            f"--maximum-calls must exactly equal frozen example count {len(examples)}"
        )
    plan, plan_digest = validate_plan(
        plan_path,
        arguments.provider_plan_sha256,
        corpus_path,
        packed_path,
        len(examples),
    )
    if git_worktree_dirty(Path(__file__).resolve().parent.parent):
        raise TrainingContractError("frontier execution requires a clean working tree")
    manifest, scores = load_or_create(
        output, corpus, corpus_path, packed_path, plan_path, plan_digest, plan, examples
    )
    manifest["status"] = "running"
    manifest["resumedAt"] = iso8601() if scores else None
    atomic_json(output / "frontier.json", manifest)
    scores_path = output / "scores.jsonl"

    for ordinal, example in enumerate(examples[len(scores) :], len(scores) + 1):
        example_id = example["exampleID"]
        model_input = semantic_model_input(corpus_path, example, plans[example_id])
        expected = target_text(example["target"])
        started = time.monotonic()
        prediction, response = request_completion(
            arguments.endpoint,
            model_input,
            local_proxy_key=os.environ.get("LITELLM_PROXY_KEY"),
        )
        latency = time.monotonic() - started
        record = {
            "schemaVersion": 1,
            "runnerVersion": FRONTIER_RUNNER_VERSION,
            "ordinal": ordinal,
            "blockID": example["experimentBlockID"],
            "arm": ARM_FROZEN_FRONTIER,
            "exampleID": example_id,
            "targetEventID": example["targetEventID"],
            "application": example.get("conditioningState", {})
            .get("destination", {})
            .get("appName"),
            "semanticModelInputSHA256": plans[example_id]["semanticModelInputSHA256"],
            "semanticModelInputUTF8Bytes": len(model_input.encode()),
            "target": expected,
            "pasteActionCount": sum(
                segment.get("type") == "paste"
                for segment in example["target"].get("segments", [])
            ),
            "prediction": prediction,
            "predictionSHA256": hashlib.sha256(prediction.encode()).hexdigest(),
            "exactMatch": prediction == expected,
            "normalizedExactMatch": prediction.strip() == expected.strip(),
            "characterSimilarity": SequenceMatcher(None, expected, prediction).ratio(),
            "latencySeconds": latency,
            "responseID": response.get("id"),
            "responseModel": response.get("model"),
            "requestedReasoningEffort": REASONING_EFFORT,
            "usage": response.get("usage") or {},
            "completedAt": iso8601(),
        }
        if record["responseModel"] != "gpt-5.6-sol":
            raise TrainingContractError("frontier provider resolved an unexpected model")
        append_jsonl(scores_path, record)
        scores.append(record)
        manifest["counts"]["completedCalls"] = len(scores)
        manifest["lastCompletedExampleID"] = example_id
        atomic_json(output / "frontier.json", manifest)
        print(
            f"frontier {ordinal:03d}/{len(examples)} block={record['blockID']} "
            f"exact={record['exactMatch']} latency={latency:.2f}s",
            flush=True,
        )

    manifest["status"] = "complete"
    manifest["completedAt"] = iso8601()
    manifest["summary"] = finish_summary(scores)
    manifest["artifactDigestsSHA256"] = {"scores.jsonl": sha256(scores_path)}
    atomic_json(output / "frontier.json", manifest)
    print(f"Frontier arm complete: {output}", flush=True)
    return 0


def main() -> int:
    try:
        return run()
    except KeyboardInterrupt:
        raise SystemExit("run-phase1-frontier-arm: interrupted; rerun the same command")
    except Exception as error:
        try:
            arguments = parse_arguments()
            output = arguments.output.expanduser().resolve()
            manifest_path = output / "frontier.json"
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
        raise SystemExit(f"run-phase1-frontier-arm: {type(error).__name__}: {error}")


if __name__ == "__main__":
    raise SystemExit(main())

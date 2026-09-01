#!/usr/bin/env python3
"""Run the paired GPT-5.6 combined-conditioning diagnostic."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import statistics
import time
import traceback
from pathlib import Path
from typing import Any

from phase1_frontier_model_arc import add_prediction_metrics, append_jsonl
from phase1_subscription_responses import (
    MODEL,
    REASONING_EFFORT,
    request_completion,
    require_loopback_url,
)
from phase1_training_contract import git_revision, git_worktree_dirty, sha256


RUNNER_VERSION = "phase1-conditioning-diagnostic-runner-v2"
EXPECTED_DIAGNOSTIC_VERSION = "phase1-conditioning-diagnostic-v6"


class DiagnosticRunError(RuntimeError):
    pass


def iso8601() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(canonical_bytes(value))
    os.replace(temporary, path)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = [float(row["latencySeconds"]) for row in rows]
    usage = [row.get("usage") or {} for row in rows]
    return {
        "examples": len(rows),
        "generatedCompletion": {
            "exactMatches": sum(
                bool(row["predictionMetrics"]["exactMatch"]) for row in rows
            ),
            "macroNormalizedLevenshteinSimilarity": statistics.fmean(
                float(row["predictionMetrics"]["normalizedLevenshteinSimilarity"])
                for row in rows
            ),
            "emptyPredictions": sum(row["prediction"] == "" for row in rows),
        },
        "latency": {
            "meanSeconds": statistics.fmean(latencies),
            "medianSeconds": statistics.median(latencies),
            "minimumSeconds": min(latencies),
            "maximumSeconds": max(latencies),
            "totalSeconds": sum(latencies),
        },
        "usage": {
            "inputTokens": sum(int(value.get("input_tokens") or 0) for value in usage),
            "outputTokens": sum(int(value.get("output_tokens") or 0) for value in usage),
            "reasoningTokens": sum(
                int((value.get("output_tokens_details") or {}).get("reasoning_tokens") or 0)
                for value in usage
            ),
        },
    }


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diagnostic", required=True, type=Path)
    parser.add_argument("--required-predecessor", required=True, type=Path)
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


def run() -> int:
    arguments = parse_arguments()
    project = Path(__file__).resolve().parent.parent
    diagnostic = arguments.diagnostic.expanduser().resolve()
    predecessor = arguments.required_predecessor.expanduser().resolve()
    output = arguments.output.expanduser().resolve()
    endpoint = require_loopback_url(arguments.endpoint)

    diagnostic_manifest_path = diagnostic / "manifest.json"
    examples_path = diagnostic / "examples.jsonl"
    diagnostic_manifest = json.loads(diagnostic_manifest_path.read_text(encoding="utf-8"))
    examples = load_jsonl(examples_path)
    predecessor_manifest = json.loads(predecessor.read_text(encoding="utf-8"))
    if not (
        diagnostic_manifest.get("diagnosticVersion") == EXPECTED_DIAGNOSTIC_VERSION
        and diagnostic_manifest.get("status")
        == "local_context_variants_complete_no_new_provider_calls"
        and diagnostic_manifest.get("artifactDigestsSHA256", {}).get("examples.jsonl")
        == sha256(examples_path)
    ):
        raise DiagnosticRunError(
            f"diagnostic artifact is not the audited {EXPECTED_DIAGNOSTIC_VERSION} input"
        )
    if not (
        predecessor_manifest.get("status") == "complete"
        and predecessor_manifest.get("completedWindows") == ["128k"]
        and predecessor_manifest.get("auditSHA256")
    ):
        raise DiagnosticRunError("required 128K predecessor and audit are not complete")
    if arguments.maximum_calls != len(examples):
        raise DiagnosticRunError(f"--maximum-calls must equal {len(examples)}")
    for example in examples:
        paste_action_count = example.get("pasteActionCount")
        if not (
            isinstance(paste_action_count, int)
            and not isinstance(paste_action_count, bool)
            and paste_action_count >= 0
        ):
            raise DiagnosticRunError(
                f"{example.get('exampleID')} has no valid structured paste-action count"
            )
        # Exercise the exact local scoring boundary for every example before
        # the first provider call. This prevents schema omissions from
        # consuming a subscription response that cannot be recorded.
        add_prediction_metrics({
            "target": example["target"],
            "prediction": "",
            "pasteActionCount": paste_action_count,
        })

    manifest_path = output / "run.json"
    scores_path = output / "scores.jsonl"
    implementation = {
        "codeRevision": git_revision(project),
        "workingTreeDirtyAtStart": git_worktree_dirty(project),
        "fileDigestsSHA256": {
            "scripts/run-phase1-conditioning-diagnostic.py": sha256(Path(__file__)),
            "scripts/phase1_subscription_responses.py": sha256(
                project / "scripts/phase1_subscription_responses.py"
            ),
        },
    }
    if output.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("inflightOperation"):
            raise DiagnosticRunError("uncertain subscription call cannot be replayed")
        if not (
            manifest.get("source", {}).get("examplesSHA256") == sha256(examples_path)
            and manifest.get("source", {}).get("predecessorSHA256") == sha256(predecessor)
            and manifest.get("implementation", {}).get("fileDigestsSHA256")
            == implementation["fileDigestsSHA256"]
        ):
            raise DiagnosticRunError("existing output lineage differs")
        scores = load_jsonl(scores_path) if scores_path.exists() else []
    else:
        output.mkdir(parents=True)
        scores = []
        manifest = {
            "schemaVersion": 1,
            "runnerVersion": RUNNER_VERSION,
            "status": "initialized",
            "startedAt": iso8601(),
            "implementation": implementation,
            "source": {
                "diagnostic": str(diagnostic),
                "diagnosticManifestSHA256": sha256(diagnostic_manifest_path),
                "examplesSHA256": sha256(examples_path),
                "predecessor": str(predecessor),
                "predecessorSHA256": sha256(predecessor),
            },
            "variant": "combined",
            "provider": {
                "route": MODEL,
                "requestedModel": "gpt-5.6-sol",
                "reasoningEffort": REASONING_EFFORT,
                "transport": "loopback_litellm_chatgpt_subscription_responses",
                "endpoint": endpoint,
                "openAIAPIKeyFallbackAllowed": False,
            },
            "counts": {"completedCalls": 0, "expectedCalls": len(examples)},
        }
        atomic_json(manifest_path, manifest)
    expected_ids = [row["exampleID"] for row in examples]
    if [row.get("exampleID") for row in scores] != expected_ids[: len(scores)]:
        raise DiagnosticRunError("scores are not an ordered prefix")
    if manifest.get("status") == "complete":
        if len(scores) != len(examples):
            raise DiagnosticRunError("complete run lacks scores")
        return 0

    manifest["status"] = "running"
    atomic_json(manifest_path, manifest)
    for ordinal, example in enumerate(examples[len(scores) :], len(scores) + 1):
        model_input = example["variants"]["combined"]["semanticModelInput"]
        manifest["inflightOperation"] = {
            "ordinal": ordinal,
            "exampleID": example["exampleID"],
            "replayAllowedAutomatically": False,
        }
        atomic_json(manifest_path, manifest)
        started = time.monotonic()
        prediction, response = request_completion(
            endpoint,
            model_input,
            local_proxy_key=os.environ.get("LITELLM_PROXY_KEY"),
            model=MODEL,
            reasoning_effort=REASONING_EFFORT,
        )
        latency = time.monotonic() - started
        if response.get("model") != "gpt-5.6-sol":
            raise DiagnosticRunError("provider resolved an unexpected model")
        record = add_prediction_metrics({
            "schemaVersion": 1,
            "runnerVersion": RUNNER_VERSION,
            "ordinal": ordinal,
            "sampleOrdinal": example["sampleOrdinal"],
            "corpusOrdinal": example["corpusOrdinal"],
            "variant": "combined",
            "requestedModel": "gpt-5.6-sol",
            "requestedReasoningEffort": REASONING_EFFORT,
            "exampleID": example["exampleID"],
            "targetEventID": example["targetEventID"],
            "application": example["application"],
            "semanticModelInputSHA256": example["variants"]["combined"]["semanticModelInputSHA256"],
            "semanticModelInputUTF8Bytes": len(model_input.encode()),
            "canonicalPackingTokenCount": example["variants"]["combined"]["packing"]["modelInputTokenCount"],
            "target": example["target"],
            "pasteActionCount": int(example["pasteActionCount"]),
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
        manifest["lastCompletedExampleID"] = example["exampleID"]
        atomic_json(manifest_path, manifest)
        print(
            f"conditioning {ordinal:02d}/{len(examples)} "
            f"similarity={record['predictionMetrics']['normalizedLevenshteinSimilarity']:.3f} "
            f"latency={latency:.2f}s",
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
        raise SystemExit("run-phase1-conditioning-diagnostic: interrupted")
    except Exception as error:
        try:
            arguments = parse_arguments()
            manifest_path = arguments.output.expanduser().resolve() / "run.json"
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

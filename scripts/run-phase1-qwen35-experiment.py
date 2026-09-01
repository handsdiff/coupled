#!/usr/bin/env python3
"""Run the resumable two-model Phase 1 Qwen 35B experiment on Tinker.

This file contains the paid execution path, but it cannot run without the
reviewed plan digest, explicit transfer/project/price confirmations, a dollar
ceiling, and ``--execute``.  The local preparation and audit scripts never
import or instantiate ``tinker.ServiceClient``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.metadata
import json
import math
import os
import time
import traceback
import uuid
from dataclasses import dataclass
from decimal import Decimal
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from phase1_experiment import canonical_bytes, target_text, validate_inputs
from phase1_qwen35_native import (
    GENERATION_TOKEN_CEILING,
    MODEL_SPECS,
    NATIVE_PACK_VERSION,
    RENDERER_CONTRACT,
    audit_native_pack,
    build_native_rows_with_runtime,
    canonical_sha256,
    load_jsonl,
    native_runtime_from_tokenizer,
    vocabulary_sha256,
)
from phase1_tinker_overfit_contract import (
    PINNED_TINKER_SDK_VERSION,
    build_and_validate_sdk_datums,
)
from phase1_training_contract import (
    IGNORE_LABEL,
    TrainingContractError,
    adapt_row_to_tinker,
    git_revision,
    git_worktree_dirty,
    sha256,
)


RUNNER_VERSION = "phase1-qwen35-tinker-prequential-v2"
EXPECTED_PLAN_VERSION = "phase1-qwen35-provider-plan-v2"
ARM_FROZEN = "frozen"
ARM_PERSONALIZED = "personalized"
MODEL_ORDER = ("qwen35_base", "qwen36_hybrid")
MAXIMUM_SCORE_RETRIES_PER_MODEL = 1
MAXIMUM_TRAINING_BLOCK_RESTARTS_PER_MODEL = 1


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


def load_api_key(path: Path) -> str:
    content = path.expanduser().read_text(encoding="utf-8").strip()
    if not content:
        raise TrainingContractError("Tinker API key file is empty")
    if "\n" not in content and "=" not in content:
        return content
    values: dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        if "=" in line:
            name, value = line.split("=", 1)
            values[name.strip()] = value.strip().strip("\"'")
    if not values.get("TINKER_API_KEY"):
        raise TrainingContractError("env file does not define TINKER_API_KEY")
    return values["TINKER_API_KEY"]


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, type=Path)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--shared-pack", required=True, type=Path)
    parser.add_argument("--provider-plan-sha256", required=True)
    parser.add_argument("--paid-preflight", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--dedicated-private-project-id", required=True)
    parser.add_argument("--maximum-usd", required=True, type=Decimal)
    parser.add_argument("--confirm-dedicated-private-project", action="store_true")
    parser.add_argument("--confirm-personal-data-transfer", action="store_true")
    parser.add_argument("--confirm-current-prices", action="store_true")
    parser.add_argument("--execute", action="store_true")
    arguments = parser.parse_args()
    for enabled, flag in (
        (arguments.confirm_dedicated_private_project, "--confirm-dedicated-private-project"),
        (arguments.confirm_personal_data_transfer, "--confirm-personal-data-transfer"),
        (arguments.confirm_current_prices, "--confirm-current-prices"),
        (arguments.execute, "--execute"),
    ):
        if not enabled:
            parser.error(f"{flag} is required")
    try:
        arguments.dedicated_private_project_id = str(
            uuid.UUID(arguments.dedicated_private_project_id)
        )
    except ValueError as error:
        parser.error(f"invalid project UUID: {error}")
    if (
        arguments.maximum_usd <= 0
        or arguments.maximum_usd.quantize(Decimal("0.01"))
        != arguments.maximum_usd
    ):
        parser.error("--maximum-usd must be a positive reviewed dollar ceiling")
    return arguments


def money(tokens: int, rate: str) -> Decimal:
    return Decimal(tokens) * Decimal(rate) / Decimal(1_000_000)


def money_string(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.000001")))


def deterministic_order(example_ids: list[str], update_ordinal: int, seed: int) -> list[str]:
    if len(set(example_ids)) != len(example_ids):
        raise TrainingContractError("training IDs are not unique")
    return sorted(
        example_ids,
        key=lambda value: (
            hashlib.sha256(
                f"phase1-prequential-new-block:{seed}:{update_ordinal}:{value}".encode()
            ).digest(),
            value,
        ),
    )


def expected_scores(blocks: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    return [
        (block["blockID"], arm, example_id)
        for block in blocks[1:]
        for arm in (ARM_FROZEN, ARM_PERSONALIZED)
        for example_id in block["exampleIDs"]
    ]


def expected_updates(blocks: list[dict[str, Any]]) -> list[str]:
    return [block["blockID"] for block in blocks[:-1]]


def weighted_nll(row: dict[str, Any], logprobs: list[float | None]) -> tuple[float, int]:
    if len(logprobs) != len(row["labels"]):
        raise TrainingContractError("Tinker logprob length differs from native sequence")
    total = 0.0
    count = 0
    for label, logprob in zip(row["labels"], logprobs, strict=True):
        if label == IGNORE_LABEL:
            continue
        if logprob is None or not math.isfinite(float(logprob)):
            raise TrainingContractError("Tinker returned a nonfinite weighted logprob")
        total -= float(logprob)
        count += 1
    if count != row["targetTokenCount"]:
        raise TrainingContractError("weighted logprob count differs from target count")
    return total, count


def normalize_stop_reason(value: Any) -> str:
    return str(getattr(value, "value", value))


def parsed_prediction(runtime: Any, token_ids: list[int]) -> tuple[str, str]:
    from tinker_cookbook.renderers import get_text_content

    message, termination = runtime.renderer.parse_response(token_ids)
    return get_text_content(message), str(getattr(termination, "value", termination))


@dataclass
class Usage:
    likelihood_calls: int = 0
    generation_calls: int = 0
    likelihood_prefill_tokens: int = 0
    generation_prefill_tokens: int = 0
    sampled_tokens_observed: int = 0
    training_calls: int = 0
    optimizer_steps: int = 0
    training_positions: int = 0
    checkpoint_saves: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "likelihoodCalls": self.likelihood_calls,
            "generationCalls": self.generation_calls,
            "likelihoodPrefillTokens": self.likelihood_prefill_tokens,
            "generationPrefillTokens": self.generation_prefill_tokens,
            "sampledTokensObserved": self.sampled_tokens_observed,
            "trainingCalls": self.training_calls,
            "optimizerSteps": self.optimizer_steps,
            "trainingPositions": self.training_positions,
            "checkpointSaves": self.checkpoint_saves,
        }


def recompute_usage(
    scores: list[dict[str, Any]], updates: list[dict[str, Any]]
) -> Usage:
    usage = Usage()
    for score in scores:
        if score.get("heldoutNLLCollected"):
            usage.likelihood_calls += 1
            usage.likelihood_prefill_tokens += score["fullSequenceTokenCount"]
        usage.generation_calls += 1
        usage.generation_prefill_tokens += score["modelInputTokenCount"]
        usage.sampled_tokens_observed += len(score["predictionTokenIDs"])
    for update in updates:
        usage.training_calls += update["trainingCalls"]
        usage.optimizer_steps += update["optimizerSteps"]
        usage.training_positions += update["submittedPositions"]
        usage.checkpoint_saves += 2
    return usage


def estimated_usage_cost(usage: Usage, plan: dict[str, Any]) -> dict[str, str]:
    rates = plan["tinker"]["pricesPerMillionUSD"]
    training = money(usage.training_positions, rates["train"])
    prefill = money(
        usage.likelihood_prefill_tokens + usage.generation_prefill_tokens,
        rates["prefill"],
    )
    sampling = money(usage.sampled_tokens_observed, rates["sample"])
    return {
        "trainingAtFrozenRate": money_string(training),
        "prefillAtUncachedFrozenRate": money_string(prefill),
        "samplingAtFrozenRate": money_string(sampling),
        "subtotalBeforeCheckpointStorage": money_string(training + prefill + sampling),
    }


def score_maximum_cost(row: dict[str, Any], plan: dict[str, Any]) -> Decimal:
    rates = plan["tinker"]["pricesPerMillionUSD"]
    prefill = row["modelInputTokenCount"]
    if plan["experiment"]["heldoutTargetNLLCollected"]:
        prefill += row["fullSequenceTokenCount"]
    return money(prefill, rates["prefill"]) + money(
        GENERATION_TOKEN_CEILING, rates["sample"]
    )


def block_training_maximum_cost(
    rows: dict[str, dict[str, Any]], block: dict[str, Any], plan: dict[str, Any]
) -> Decimal:
    return money(
        sum(len(rows[value]["inputIDs"]) - 1 for value in block["exampleIDs"]),
        plan["tinker"]["pricesPerMillionUSD"]["train"],
    )


def abandoned_maximum(manifest: dict[str, Any]) -> Decimal:
    return sum(
        (
            Decimal(value["maximumEstimatedProviderCostUSD"])
            for value in manifest.get("abandonedAttempts", [])
        ),
        Decimal(0),
    )


def ensure_budget(
    manifest: dict[str, Any], plan: dict[str, Any], maximum_usd: Decimal
) -> None:
    planned = Decimal(
        plan["tinker"]["projectedCostUSD"]["conservativeUncachedIncludingReserve"]
    )
    projected = planned + abandoned_maximum(manifest)
    if projected > maximum_usd:
        raise TrainingContractError(
            f"run maximum ${projected} exceeds authorized ${maximum_usd}"
        )
    manifest["budget"] = {
        "plannedConservativeUSD": money_string(planned),
        "abandonedAttemptMaximumUSD": money_string(abandoned_maximum(manifest)),
        "maximumProjectedUSD": money_string(projected),
        "authorizedCeilingUSD": str(maximum_usd),
    }


def implementation_record(plan: dict[str, Any]) -> dict[str, Any]:
    project = Path(__file__).resolve().parent.parent
    files = plan.get("implementation", {}).get("fileDigestsSHA256", {})
    return {
        "codeRevision": git_revision(project),
        "workingTreeDirtyAtStart": git_worktree_dirty(project),
        "fileDigestsSHA256": {
            relative: sha256(project / relative) for relative in files
        },
        "packageVersions": {
            package: importlib.metadata.version(package)
            for package in ("tinker", "tinker-cookbook", "transformers", "tokenizers")
        },
    }


def validate_plan_and_sources(
    *,
    experiment: Path,
    corpus: Path,
    shared_pack: Path,
    expected_digest: str,
    project_id: str,
    maximum_usd: Decimal,
    paid_preflight: Path,
) -> dict[str, Any]:
    plan_path = experiment / "provider-plan.json"
    if sha256(plan_path) != expected_digest:
        raise TrainingContractError("provider plan SHA-256 differs from approval")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if not (
        plan.get("planVersion") == EXPECTED_PLAN_VERSION
        and plan.get("status")
        == "local_preflight_complete_no_provider_calls_authorization_required"
        and plan.get("training", {}).get("rendererContract") == RENDERER_CONTRACT
        and plan.get("training", {}).get("renderersByModel")
        == {key: MODEL_SPECS[key]["renderer"] for key in MODEL_ORDER}
        and plan.get("training", {}).get("rendererReduction") == "none"
        and plan.get("experiment", {}).get("terminalBlockReceivesUpdate") is False
        and plan.get("tinker", {}).get("projectID") == project_id
    ):
        raise TrainingContractError("provider plan differs from runner contract")
    source = plan["source"]
    expected = {
        "corpusSHA256": sha256(corpus / "corpus.json"),
        "examplesSHA256": sha256(corpus / "examples.jsonl"),
        "sharedPackingSHA256": sha256(shared_pack / "packing.json"),
        "sharedContextPlansSHA256": sha256(shared_pack / "context-plans.jsonl"),
    }
    for key, digest in expected.items():
        if source.get(key) != digest:
            raise TrainingContractError(f"provider-plan source changed: {key}")
    implementation = implementation_record(plan)
    if implementation["workingTreeDirtyAtStart"]:
        raise TrainingContractError("paid execution requires a clean worktree")
    if implementation["codeRevision"] != plan.get("implementation", {}).get("codeRevision"):
        raise TrainingContractError("paid runner Git revision changed")
    if implementation["fileDigestsSHA256"] != plan.get("implementation", {}).get(
        "fileDigestsSHA256"
    ):
        raise TrainingContractError("paid runner implementation files changed")
    if implementation["packageVersions"] != plan.get("implementation", {}).get(
        "packageVersions"
    ):
        raise TrainingContractError("paid runner dependency versions changed")
    ensure_budget({}, plan, maximum_usd)
    preflight_path = paid_preflight.expanduser().resolve() / "preflight.json"
    if not preflight_path.is_file():
        raise TrainingContractError("full execution requires the paid preflight artifact")
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    if not (
        preflight.get("status") == "complete_go"
        and preflight.get("providerPlanSHA256") == expected_digest
        and preflight.get("implementation") == plan.get("implementation")
        and preflight.get("provider", {}).get("projectID") == project_id
        and set(preflight.get("models", {})) == set(MODEL_ORDER)
        and all(
            preflight["models"][value].get("status") == "passed"
            for value in MODEL_ORDER
        )
    ):
        raise TrainingContractError("paid preflight did not pass for both models")
    preflight_directory = preflight_path.parent
    for relative, digest in preflight.get("artifactDigestsSHA256", {}).items():
        artifact = preflight_directory / relative
        if not artifact.is_file() or sha256(artifact) != digest:
            raise TrainingContractError(f"paid preflight artifact changed: {relative}")
    if not preflight.get("artifactDigestsSHA256"):
        raise TrainingContractError("paid preflight lacks artifact digests")
    return plan


def validate_remote_tokenizer(
    *,
    model_key: str,
    local_manifest: dict[str, Any],
    local_rows: list[dict[str, Any]],
    remote_tokenizer: Any,
    corpus_directory: Path,
    shared_pack_directory: Path,
) -> Any:
    """Prove the remote tokenizer reproduces every frozen tokenized example.

    A vocabulary-size check is not enough.  We compare the complete token→ID
    mapping, special IDs, paste encoding, and re-render each exact prompt/target
    sequence using the Tinker-returned tokenizer before any personal sequence is
    submitted to training or sampling.
    """
    if not (
        len(remote_tokenizer) == local_manifest["tokenizerLength"]
        and vocabulary_sha256(remote_tokenizer)
        == local_manifest["tokenizerVocabularySHA256"]
        and remote_tokenizer.eos_token_id == local_manifest["tokenizerEOSID"]
        and remote_tokenizer.pad_token_id == local_manifest["tokenizerPadID"]
        and remote_tokenizer.encode("<|paste|>", add_special_tokens=False)
        == local_manifest["pasteMarkerTokenIDs"]
    ):
        raise TrainingContractError(f"{model_key} remote tokenizer metadata differs")
    runtime = native_runtime_from_tokenizer(model_key, remote_tokenizer)
    if not (
        runtime.renderer_name == local_manifest["renderer"]
        and runtime.renderer_strategy == local_manifest["rendererStrategy"]
        and runtime.response_terminator_token_id
        == local_manifest["responseTerminatorTokenID"]
        and runtime.expected_parse_termination
        == local_manifest["expectedParseTermination"]
    ):
        raise TrainingContractError(f"{model_key} remote renderer contract differs")
    remote_rows, _ = build_native_rows_with_runtime(
        corpus_directory=corpus_directory,
        shared_pack_directory=shared_pack_directory,
        model_key=model_key,
        runtime=runtime,
    )
    if canonical_sha256(remote_rows) != canonical_sha256(local_rows):
        raise TrainingContractError(
            f"{model_key} Tinker-returned tokenizer changed frozen native rows"
        )
    return runtime


def recover_interruption(
    *,
    manifest: dict[str, Any],
    scores_by_model: dict[str, list[dict[str, Any]]],
    updates_by_model: dict[str, list[dict[str, Any]]],
    plan: dict[str, Any],
    maximum_usd: Decimal,
) -> bool:
    active = manifest.get("activeUpdate")
    inflight = manifest.get("inflightOperation")
    if active:
        model_key = active["modelKey"]
        kind = "training_block_restart"
        identity = {
            "modelKey": model_key,
            "updateOrdinal": active["updateOrdinal"],
        }
        committed = any(
            value["updateOrdinal"] == active["updateOrdinal"]
            for value in updates_by_model[model_key]
        )
        maximum_cost = Decimal(active["maximumReplayCostUSD"])
        limit = MAXIMUM_TRAINING_BLOCK_RESTARTS_PER_MODEL
    elif inflight and inflight.get("kind") == "score_generation_and_optional_nll":
        model_key = inflight["modelKey"]
        kind = "score_retry"
        identity = {
            "modelKey": model_key,
            "blockID": inflight["blockID"],
            "arm": inflight["arm"],
            "exampleID": inflight["exampleID"],
        }
        committed = any(
            all(value.get(key) == expected for key, expected in identity.items() if key != "modelKey")
            for value in scores_by_model[model_key]
        )
        maximum_cost = Decimal(inflight["maximumReplayCostUSD"])
        limit = MAXIMUM_SCORE_RETRIES_PER_MODEL
    elif inflight:
        # Checkpoint saves occur while activeUpdate is present and are handled
        # by the whole-block restart branch above.  No other mutating operation
        # may be replayed implicitly.
        raise TrainingContractError(
            f"unsupported interrupted operation: {inflight.get('kind')}"
        )
    else:
        return False

    if committed:
        manifest.setdefault("recoveryEvents", []).append(
            {
                "kind": "stale_marker_after_committed_operation",
                "operationKind": kind,
                "identity": identity,
                "recoveredAt": iso8601(),
            }
        )
    else:
        prior = [
            value
            for value in manifest.get("abandonedAttempts", [])
            if value.get("kind") == kind and value.get("identity", {}).get("modelKey") == model_key
        ]
        if len(prior) >= limit or any(value.get("identity") == identity for value in prior):
            raise TrainingContractError(f"automatic {kind} allowance exhausted")
        manifest.setdefault("abandonedAttempts", []).append(
            {
                "attemptID": str(uuid.uuid4()),
                "kind": kind,
                "identity": identity,
                "abandonedAt": iso8601(),
                "maximumEstimatedProviderCostUSD": money_string(maximum_cost),
                "activeUpdate": active,
                "inflightOperation": inflight,
                "resolution": (
                    "retry_nonmutating_score_once"
                    if kind == "score_retry"
                    else "restore_parent_optimizer_state_and_restart_whole_block_once"
                ),
            }
        )
        ensure_budget(manifest, plan, maximum_usd)
    failure = manifest.pop("failure", None)
    interrupted_at = manifest.pop("interruptedAt", None)
    manifest.pop("activeUpdate", None)
    manifest.pop("inflightOperation", None)
    manifest.setdefault("recoveryEvents", []).append(
        {
            "kind": "resume_after_interruption",
            "operationKind": kind,
            "identity": identity,
            "failure": failure,
            "interruptedAt": interrupted_at,
            "resumedAt": iso8601(),
        }
    )
    manifest["status"] = "recovering"
    return True


def score_example(
    *,
    sampler: Any,
    runtime: Any,
    tinker: Any,
    model_key: str,
    arm: str,
    block_id: str,
    example: dict[str, Any],
    row: dict[str, Any],
    checkpoint_path: str | None,
    include_nll: bool,
    usage: Usage,
) -> dict[str, Any]:
    target = target_text(example["target"])
    started = time.monotonic()
    nll_latency: float | None = None
    nll_sum: float | None = None
    weighted: int | None = None
    logprobs_digest: str | None = None
    if include_nll:
        request_started = time.monotonic()
        usage.likelihood_calls += 1
        usage.likelihood_prefill_tokens += row["fullSequenceTokenCount"]
        logprobs = sampler.compute_logprobs(
            tinker.ModelInput.from_ints(tokens=row["inputIDs"])
        ).result()
        nll_latency = time.monotonic() - request_started
        nll_sum, weighted = weighted_nll(row, logprobs)
        logprobs_digest = hashlib.sha256(
            canonical_bytes(
                [
                    value
                    for label, value in zip(row["labels"], logprobs, strict=True)
                    if label != IGNORE_LABEL
                ]
            )
        ).hexdigest()

    prompt_ids = row["inputIDs"][: row["modelInputTokenCount"]]
    generation_started = time.monotonic()
    usage.generation_calls += 1
    usage.generation_prefill_tokens += len(prompt_ids)
    response = sampler.sample(
        prompt=tinker.ModelInput.from_ints(tokens=prompt_ids),
        num_samples=1,
        sampling_params=tinker.SamplingParams(
            max_tokens=GENERATION_TOKEN_CEILING,
            temperature=0.6,
            seed=17,
            stop=row["generationStopTokenIDs"],
        ),
    ).result()
    generation_latency = time.monotonic() - generation_started
    if len(response.sequences) != 1:
        raise TrainingContractError("Tinker returned an unexpected sample count")
    sequence = response.sequences[0]
    observed = list(sequence.tokens)
    usage.sampled_tokens_observed += len(observed)
    stop_reason = normalize_stop_reason(sequence.stop_reason)
    parse_ids = observed
    locally_added_terminator = False
    if (
        (not parse_ids or parse_ids[-1] != runtime.response_terminator_token_id)
        and stop_reason in {"stop", "stop_sequence", "eos"}
    ):
        # Some sampling APIs report a matched stop without returning its token.
        # Add it only to the local renderer parser; preserve provider token IDs
        # and billable sampled-token accounting exactly as returned.
        parse_ids = [*parse_ids, runtime.response_terminator_token_id]
        locally_added_terminator = True
    prediction, parse_termination = parsed_prediction(runtime, parse_ids)
    return {
        "schemaVersion": 1,
        "runnerVersion": RUNNER_VERSION,
        "modelKey": model_key,
        "model": MODEL_SPECS[model_key]["model"],
        "blockID": block_id,
        "arm": arm,
        "exampleID": example["exampleID"],
        "targetEventID": example["targetEventID"],
        "application": example.get("conditioningState", {})
        .get("destination", {})
        .get("appName"),
        "checkpointPath": checkpoint_path,
        "modelInputTokenCount": row["modelInputTokenCount"],
        "fullSequenceTokenCount": row["fullSequenceTokenCount"],
        "targetTokenCount": row["targetTokenCount"],
        "heldoutNLLCollected": include_nll,
        "weightedNLLSum": nll_sum,
        "weightedTokenCount": weighted,
        "meanNLL": None if nll_sum is None or weighted is None else nll_sum / weighted,
        "weightedLogprobsSHA256": logprobs_digest,
        "target": target,
        "pasteActionCount": row["pasteActionCount"],
        "prediction": prediction,
        "predictionTokenIDs": observed,
        "generationTemperature": 0.6,
        "generationSeed": 17,
        "samplingStopReason": stop_reason,
        "rendererParseTermination": parse_termination,
        "expectedRendererParseTermination": runtime.expected_parse_termination,
        "terminatorAddedForLocalParsing": locally_added_terminator,
        "exactMatch": prediction == target,
        "normalizedExactMatch": prediction.strip() == target.strip(),
        "characterSimilarity": SequenceMatcher(None, target, prediction).ratio(),
        "latencyInstrumentationVersion": "tinker-qwen35-split-requests-v1",
        "targetLikelihoodLatencySeconds": nll_latency,
        "generationLatencySeconds": generation_latency,
        "totalLatencySeconds": time.monotonic() - started,
        "completedAt": iso8601(),
    }


def initialize_or_resume(
    *,
    output: Path,
    plan: dict[str, Any],
    plan_digest: str,
    maximum_usd: Decimal,
    project_id: str,
    paid_preflight_sha256: str,
) -> tuple[
    dict[str, Any],
    dict[str, list[dict[str, Any]]],
    dict[str, list[dict[str, Any]]],
]:
    manifest_path = output / "experiment.json"
    if not output.exists():
        output.mkdir(parents=True)
        for model_key in MODEL_ORDER:
            (output / model_key).mkdir()
        manifest: dict[str, Any] = {
            "schemaVersion": 1,
            "runnerVersion": RUNNER_VERSION,
            "status": "initialized",
            "startedAt": iso8601(),
            "providerPlanSHA256": plan_digest,
            "paidPreflightSHA256": paid_preflight_sha256,
            "implementation": implementation_record(plan),
            "authorization": {
                "maximumUSD": str(maximum_usd),
                "personalDataTransferConfirmed": True,
                "dedicatedPrivateProjectConfirmed": True,
                "currentPricesConfirmed": True,
            },
            "provider": {
                "projectID": project_id,
                "models": [MODEL_SPECS[value]["model"] for value in MODEL_ORDER],
            },
            "counts": {},
            "abandonedAttempts": [],
            "recoveryEvents": [],
        }
        ensure_budget(manifest, plan, maximum_usd)
        atomic_json(manifest_path, manifest)
        return manifest, {key: [] for key in MODEL_ORDER}, {key: [] for key in MODEL_ORDER}

    if not manifest_path.is_file():
        raise TrainingContractError("existing output lacks experiment.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not (
        manifest.get("runnerVersion") == RUNNER_VERSION
        and manifest.get("providerPlanSHA256") == plan_digest
        and manifest.get("paidPreflightSHA256") == paid_preflight_sha256
        and manifest.get("authorization", {}).get("maximumUSD") == str(maximum_usd)
        and manifest.get("implementation") == implementation_record(plan)
    ):
        raise TrainingContractError("resume implementation, plan, or authorization changed")
    if manifest.get("status") == "complete":
        return manifest, {
            key: load_jsonl(output / key / "scores.jsonl") for key in MODEL_ORDER
        }, {
            key: load_jsonl(output / key / "updates.jsonl") for key in MODEL_ORDER
        }
    scores = {
        key: (
            load_jsonl(output / key / "scores.jsonl")
            if (output / key / "scores.jsonl").is_file()
            else []
        )
        for key in MODEL_ORDER
    }
    updates = {
        key: (
            load_jsonl(output / key / "updates.jsonl")
            if (output / key / "updates.jsonl").is_file()
            else []
        )
        for key in MODEL_ORDER
    }
    if recover_interruption(
        manifest=manifest,
        scores_by_model=scores,
        updates_by_model=updates,
        plan=plan,
        maximum_usd=maximum_usd,
    ):
        atomic_json(manifest_path, manifest)
    return manifest, scores, updates


def validate_protocol_prefixes(
    *,
    blocks: list[dict[str, Any]],
    scores_by_model: dict[str, list[dict[str, Any]]],
    updates_by_model: dict[str, list[dict[str, Any]]],
) -> None:
    score_plan = expected_scores(blocks)
    update_plan = expected_updates(blocks)
    for model_key in MODEL_ORDER:
        observed_scores = [
            (value["blockID"], value["arm"], value["exampleID"])
            for value in scores_by_model[model_key]
        ]
        if observed_scores != score_plan[: len(observed_scores)]:
            raise TrainingContractError(f"{model_key} scores are not a protocol prefix")
        observed_updates = [
            value["afterBlockID"] for value in updates_by_model[model_key]
        ]
        if observed_updates != update_plan[: len(observed_updates)]:
            raise TrainingContractError(f"{model_key} updates are not a protocol prefix")


def summaries(scores: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for arm in (ARM_FROZEN, ARM_PERSONALIZED):
        rows = [value for value in scores if value["arm"] == arm]
        with_nll = [value for value in rows if value["heldoutNLLCollected"]]
        weighted = sum(value["weightedTokenCount"] for value in with_nll)
        result.append(
            {
                "arm": arm,
                "examples": len(rows),
                "exactMatches": sum(value["exactMatch"] for value in rows),
                "normalizedExactMatches": sum(
                    value["normalizedExactMatch"] for value in rows
                ),
                "meanCharacterSimilarity": sum(
                    value["characterSimilarity"] for value in rows
                )
                / len(rows),
                "microTargetTokenNLL": (
                    None
                    if not with_nll
                    else sum(value["weightedNLLSum"] for value in with_nll) / weighted
                ),
                "weightedNLLTokens": weighted,
            }
        )
    return result


def run() -> int:
    arguments = parse_arguments()
    project = Path(__file__).resolve().parent.parent
    experiment = arguments.experiment.expanduser().resolve()
    corpus_path = arguments.corpus.expanduser().resolve()
    shared_pack_path = arguments.shared_pack.expanduser().resolve()
    output = arguments.output.expanduser().resolve()
    plan = validate_plan_and_sources(
        experiment=experiment,
        corpus=corpus_path,
        shared_pack=shared_pack_path,
        expected_digest=arguments.provider_plan_sha256,
        project_id=arguments.dedicated_private_project_id,
        maximum_usd=arguments.maximum_usd,
        paid_preflight=arguments.paid_preflight,
    )
    plan_digest = sha256(experiment / "provider-plan.json")
    corpus, examples, _, _ = validate_inputs(corpus_path, shared_pack_path)
    blocks = corpus["blocking"]["blocks"]
    examples_by_id = {value["exampleID"]: value for value in examples}
    if len(examples_by_id) != 450 or len(blocks) != 9:
        raise TrainingContractError("Qwen 35B runner requires the frozen 450-example corpus")

    native_manifests: dict[str, dict[str, Any]] = {}
    rows_by_model: dict[str, dict[str, dict[str, Any]]] = {}
    datums_by_model: dict[str, dict[str, Any]] = {}
    contracts_by_model: dict[str, dict[str, Any]] = {}
    for model_key in MODEL_ORDER:
        native_manifest, rows = audit_native_pack(experiment / model_key)
        native_manifests[model_key] = native_manifest
        rows_by_model[model_key] = {value["exampleID"]: value for value in rows}
        contracts = [adapt_row_to_tinker(value) for value in rows]
        datums, _, sdk_version = build_and_validate_sdk_datums(contracts)
        if sdk_version != PINNED_TINKER_SDK_VERSION:
            raise TrainingContractError("Tinker SDK version differs from local preflight")
        datums_by_model[model_key] = {
            contract.example_id: datum
            for contract, datum in zip(contracts, datums, strict=True)
        }
        contracts_by_model[model_key] = {
            value.example_id: value for value in contracts
        }

    manifest, scores_by_model, updates_by_model = initialize_or_resume(
        output=output,
        plan=plan,
        plan_digest=plan_digest,
        maximum_usd=arguments.maximum_usd,
        project_id=arguments.dedicated_private_project_id,
        paid_preflight_sha256=sha256(
            arguments.paid_preflight.expanduser().resolve() / "preflight.json"
        ),
    )
    if manifest.get("status") == "complete":
        return 0
    validate_protocol_prefixes(
        blocks=blocks,
        scores_by_model=scores_by_model,
        updates_by_model=updates_by_model,
    )
    os.environ["TINKER_API_KEY"] = load_api_key(arguments.env_file)
    try:
        import tinker
    except ImportError as error:
        raise TrainingContractError("Tinker SDK is unavailable") from error
    if importlib.metadata.version("tinker") != PINNED_TINKER_SDK_VERSION:
        raise TrainingContractError("Tinker SDK version changed")

    service = tinker.ServiceClient(
        project_id=arguments.dedicated_private_project_id,
        user_metadata={
            "purpose": "phase1-qwen35-initialization-comparison",
            "provider_plan_sha256": plan_digest,
        },
    )
    capabilities = service.get_server_capabilities()
    capability_by_model = {
        value.model_name: value for value in capabilities.supported_models
    }
    manifest["status"] = "running"
    manifest["provider"]["sessionID"] = service.holder.get_session_id()
    atomic_json(output / "experiment.json", manifest)

    for model_key in MODEL_ORDER:
        model = MODEL_SPECS[model_key]["model"]
        capability = capability_by_model.get(model)
        if capability is None or (capability.max_context_length or 0) < native_manifests[
            model_key
        ]["counts"]["maximumFullSequenceTokens"]:
            raise TrainingContractError(f"{model_key} server context/model differs")
        base_sampler = service.create_sampling_client(base_model=model)
        remote_tokenizer = base_sampler.get_tokenizer()
        runtime = validate_remote_tokenizer(
            model_key=model_key,
            local_manifest=native_manifests[model_key],
            local_rows=list(rows_by_model[model_key].values()),
            remote_tokenizer=remote_tokenizer,
            corpus_directory=corpus_path,
            shared_pack_directory=shared_pack_path,
        )
        manifest.setdefault("remotePreflight", {})[model_key] = {
            "status": "passed_before_personal_data_submission",
            "maximumContextLength": capability.max_context_length,
            "tokenizerVocabularySHA256": vocabulary_sha256(remote_tokenizer),
            "responseTerminatorTokenID": runtime.response_terminator_token_id,
            "tinkerServedModelRevision": "not_exposed_by_server_capabilities",
            "allFrozenSequencesExactRerender": True,
            "completedAt": iso8601(),
        }
        atomic_json(output / "experiment.json", manifest)

        rows = rows_by_model[model_key]
        contracts = contracts_by_model[model_key]
        datums = datums_by_model[model_key]
        scores = scores_by_model[model_key]
        updates = updates_by_model[model_key]
        scores_path = output / model_key / "scores.jsonl"
        updates_path = output / model_key / "updates.jsonl"
        training_steps_path = output / model_key / "training-steps.jsonl"
        usage = recompute_usage(scores, updates)
        if updates:
            training_client = service.create_training_client_from_state_with_optimizer(
                updates[-1]["optimizerStatePath"],
                base_model=model,
                user_metadata={"purpose": f"phase1-{model_key}-resume"},
            )
        else:
            contract = plan["training"]
            training_client = service.create_lora_training_client(
                base_model=model,
                rank=contract["rank"],
                seed=contract["seed"],
                train_mlp=contract["trainMLP"],
                train_attn=contract["trainAttention"],
                train_unembed=contract["trainUnembedding"],
                user_metadata={"purpose": f"phase1-{model_key}-incremental"},
            )
        optimizer_contract = plan["training"]["optimizer"]
        optimizer = tinker.AdamParams(
            learning_rate=optimizer_contract["learningRate"],
            beta1=optimizer_contract["beta1"],
            beta2=optimizer_contract["beta2"],
            eps=optimizer_contract["epsilon"],
            weight_decay=optimizer_contract["weightDecay"],
            grad_clip_norm=optimizer_contract["gradientClipNorm"],
        )

        for block_ordinal, block in enumerate(blocks, 1):
            if block_ordinal > 1:
                checkpoint = updates[block_ordinal - 2]["samplerCheckpointPath"]
                personalized_sampler = service.create_sampling_client(model_path=checkpoint)
                for arm, sampler in (
                    (ARM_FROZEN, base_sampler),
                    (ARM_PERSONALIZED, personalized_sampler),
                ):
                    for example_id in block["exampleIDs"]:
                        identity = (block["blockID"], arm, example_id)
                        observed = {
                            (value["blockID"], value["arm"], value["exampleID"])
                            for value in scores
                        }
                        if identity in observed:
                            continue
                        row = rows[example_id]
                        manifest["inflightOperation"] = {
                            "kind": "score_generation_and_optional_nll",
                            "modelKey": model_key,
                            "blockID": block["blockID"],
                            "arm": arm,
                            "exampleID": example_id,
                            "maximumReplayCostUSD": money_string(
                                score_maximum_cost(row, plan)
                            ),
                        }
                        atomic_json(output / "experiment.json", manifest)
                        score = score_example(
                            sampler=sampler,
                            runtime=runtime,
                            tinker=tinker,
                            model_key=model_key,
                            arm=arm,
                            block_id=block["blockID"],
                            example=examples_by_id[example_id],
                            row=row,
                            checkpoint_path=(
                                checkpoint if arm == ARM_PERSONALIZED else None
                            ),
                            include_nll=plan["experiment"][
                                "heldoutTargetNLLCollected"
                            ],
                            usage=usage,
                        )
                        append_jsonl(scores_path, score)
                        scores.append(score)
                        manifest.pop("inflightOperation", None)
                        manifest.setdefault("counts", {}).setdefault(model_key, {})[
                            "completedScores"
                        ] = len(scores)
                        manifest.setdefault("usage", {})[model_key] = usage.as_dict()
                        atomic_json(output / "experiment.json", manifest)
                        print(
                            f"{model_key} score {len(scores):03d}/800 "
                            f"block={block['blockID']} arm={arm}",
                            flush=True,
                        )

            if block_ordinal == len(blocks) or len(updates) >= block_ordinal:
                continue
            expected_score_prefix = expected_scores(blocks)[:
                2 * sum(len(value["exampleIDs"]) for value in blocks[1:block_ordinal])
            ]
            observed_score_prefix = [
                (value["blockID"], value["arm"], value["exampleID"])
                for value in scores
            ]
            if observed_score_prefix != expected_score_prefix:
                raise TrainingContractError("attempted update before complete block scoring")

            order = deterministic_order(
                list(block["exampleIDs"]), block_ordinal, plan["training"]["seed"]
            )
            maximum_replay = block_training_maximum_cost(rows, block, plan)
            attempt = 1 + sum(
                value.get("kind") == "training_block_restart"
                and value.get("identity", {}).get("modelKey") == model_key
                and value.get("identity", {}).get("updateOrdinal") == block_ordinal
                for value in manifest.get("abandonedAttempts", [])
            )
            parent = updates[-1] if updates else None
            manifest["activeUpdate"] = {
                "modelKey": model_key,
                "updateOrdinal": block_ordinal,
                "afterBlockID": block["blockID"],
                "attemptOrdinal": attempt,
                "parentOptimizerStatePath": (
                    None if parent is None else parent["optimizerStatePath"]
                ),
                "totalSteps": len(order),
                "completedStepsNotCheckpointed": 0,
                "maximumReplayCostUSD": money_string(maximum_replay),
            }
            atomic_json(output / "experiment.json", manifest)
            update_started = time.monotonic()
            update_nll = 0.0
            update_weighted = 0
            submitted_positions = 0
            for position, example_id in enumerate(order, 1):
                datum = datums[example_id]
                contract = contracts[example_id]
                manifest["inflightOperation"] = {
                    "kind": "training_step",
                    "modelKey": model_key,
                    "updateOrdinal": block_ordinal,
                    "position": position,
                    "exampleID": example_id,
                    "replayPolicy": "restart_whole_block_from_parent_checkpoint",
                }
                atomic_json(output / "experiment.json", manifest)
                # Tinker's async guide recommends enqueueing the optimizer step
                # immediately after forward/backward.  The server preserves
                # client operation order; resolving both futures afterwards
                # avoids an unnecessary round-trip while retaining batch-1 SGD.
                forward_future = training_client.forward_backward(
                    [datum], "cross_entropy"
                )
                optimizer_future = training_client.optim_step(optimizer)
                forward = forward_future.result()
                optimizer_future.result()
                logprobs = forward.loss_fn_outputs[0]["logprobs"].tolist()
                if len(logprobs) != contract.length:
                    raise TrainingContractError("training logprob length changed")
                nll = -sum(
                    float(logprob) * weight
                    for logprob, weight in zip(
                        logprobs, contract.weights, strict=True
                    )
                )
                step = {
                    "modelKey": model_key,
                    "updateOrdinal": block_ordinal,
                    "trainingAttemptOrdinal": attempt,
                    "position": position,
                    "exampleID": example_id,
                    "submittedPositions": contract.length,
                    "lossBearingTokens": contract.weighted_positions,
                    "weightedNLLSum": nll,
                    "meanPreUpdateNLL": nll / contract.weighted_positions,
                    "completedAt": iso8601(),
                }
                append_jsonl(training_steps_path, step)
                update_nll += nll
                update_weighted += contract.weighted_positions
                submitted_positions += contract.length
                usage.training_calls += 1
                usage.optimizer_steps += 1
                usage.training_positions += contract.length
                manifest.pop("inflightOperation", None)
                manifest["activeUpdate"]["completedStepsNotCheckpointed"] = position
                manifest.setdefault("usage", {})[model_key] = usage.as_dict()
                atomic_json(output / "experiment.json", manifest)
                if position % 10 == 0 or position == len(order):
                    print(
                        f"{model_key} update {block_ordinal}/8 "
                        f"step={position}/{len(order)}",
                        flush=True,
                    )

            prefix = (
                f"phase1-{model_key}-{corpus['corpusID'][:12]}-"
                f"block-{block_ordinal:02d}-attempt-{attempt:02d}"
            )
            manifest["inflightOperation"] = {
                "kind": "save_checkpoints",
                "modelKey": model_key,
                "updateOrdinal": block_ordinal,
                "replayPolicy": "restart_whole_block_from_parent_checkpoint",
            }
            atomic_json(output / "experiment.json", manifest)
            sampler_path = training_client.save_weights_for_sampler(
                f"{prefix}-sampler",
                ttl_seconds=plan["training"]["checkpointTTLSeconds"],
            ).result().path
            state_path = training_client.save_state(
                f"{prefix}-optimizer-state",
                ttl_seconds=plan["training"]["checkpointTTLSeconds"],
            ).result().path
            usage.checkpoint_saves += 2
            update = {
                "schemaVersion": 1,
                "runnerVersion": RUNNER_VERSION,
                "modelKey": model_key,
                "updateOrdinal": block_ordinal,
                "trainingAttemptOrdinal": attempt,
                "afterBlockID": block["blockID"],
                "parentOptimizerStatePath": (
                    None if parent is None else parent["optimizerStatePath"]
                ),
                "samplerCheckpointPath": sampler_path,
                "optimizerStatePath": state_path,
                "checkpointTTLSeconds": plan["training"]["checkpointTTLSeconds"],
                "trainedExamplesThisUpdate": len(order),
                "cumulativeTrainedExamples": block_ordinal * 50,
                "exampleOrderSHA256": hashlib.sha256(canonical_bytes(order)).hexdigest(),
                "trainingCalls": len(order),
                "optimizerSteps": len(order),
                "submittedPositions": submitted_positions,
                "lossBearingTokenPresentations": update_weighted,
                "weightedNLLSum": update_nll,
                "meanPreUpdateNLL": update_nll / update_weighted,
                "latencySeconds": time.monotonic() - update_started,
                "completedAt": iso8601(),
            }
            append_jsonl(updates_path, update)
            updates.append(update)
            manifest.pop("inflightOperation", None)
            manifest.pop("activeUpdate", None)
            manifest.setdefault("counts", {}).setdefault(model_key, {})[
                "completedUpdates"
            ] = len(updates)
            manifest.setdefault("usage", {})[model_key] = usage.as_dict()
            atomic_json(output / "experiment.json", manifest)

        model_expected = plan["tinker"]["models"][model_key]["operations"]
        audit = {
            "scores": len(scores) == model_expected["generationCalls"],
            "likelihoodCalls": usage.likelihood_calls
            == model_expected["likelihoodCalls"],
            "generationCalls": usage.generation_calls
            == model_expected["generationCalls"],
            "updates": len(updates) == model_expected["updates"],
            "optimizerSteps": usage.optimizer_steps
            == model_expected["optimizerSteps"],
            "samplerAndOptimizerCheckpoints": usage.checkpoint_saves
            == 2 * model_expected["updates"],
        }
        manifest.setdefault("modelResults", {})[model_key] = {
            "status": "complete" if all(audit.values()) else "failed_usage_audit",
            "operationAudit": audit,
            "usage": usage.as_dict(),
            "estimatedCost": estimated_usage_cost(usage, plan),
            "summaries": summaries(scores),
            "artifactDigestsSHA256": {
                "scores.jsonl": sha256(scores_path),
                "updates.jsonl": sha256(updates_path),
                "training-steps.jsonl": sha256(training_steps_path),
            },
            "completedAt": iso8601(),
        }
        if not all(audit.values()):
            raise TrainingContractError(f"{model_key} usage audit failed")
        atomic_json(output / "experiment.json", manifest)

    manifest["status"] = "complete"
    manifest["completedAt"] = iso8601()
    ensure_budget(manifest, plan, arguments.maximum_usd)
    atomic_json(output / "experiment.json", manifest)
    print(f"Qwen 35B experiment complete: {output}", flush=True)
    return 0


def main() -> int:
    try:
        return run()
    except KeyboardInterrupt:
        raise SystemExit(
            "run-phase1-qwen35-experiment: interrupted; rerun the exact command"
        )
    except Exception as error:
        try:
            arguments = parse_arguments()
            manifest_path = arguments.output.expanduser().resolve() / "experiment.json"
            if manifest_path.is_file():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["status"] = "interrupted"
                manifest["interruptedAt"] = iso8601()
                manifest["failure"] = {
                    "type": type(error).__name__,
                    "message": str(error),
                    "traceback": traceback.format_exc(),
                    "apiKeyPersisted": False,
                    "rawProviderErrorPersisted": False,
                }
                atomic_json(manifest_path, manifest)
        except Exception:
            pass
        raise SystemExit(
            f"run-phase1-qwen35-experiment: {type(error).__name__}: {error}"
        )


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run the separately authorized native-renderer/tiny-overfit Qwen 35B gate."""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
import sys
import time
import traceback
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from phase1_experiment import canonical_bytes, validate_inputs
from phase1_qwen35_native import MODEL_SPECS, audit_native_pack
from phase1_tinker_overfit_contract import (
    PINNED_TINKER_SDK_VERSION,
    build_and_validate_sdk_datums,
)
from phase1_training_contract import (
    TrainingContractError,
    adapt_row_to_tinker,
    git_revision,
    git_worktree_dirty,
    sha256,
)


PROJECT = Path(__file__).resolve().parent.parent
RUNNER_PATH = PROJECT / "scripts/run-phase1-qwen35-experiment.py"
SPEC = importlib.util.spec_from_file_location("phase1_qwen35_runner", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUNNER
SPEC.loader.exec_module(RUNNER)

PREFLIGHT_VERSION = "phase1-qwen35-paid-preflight-v1"


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, type=Path)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--shared-pack", required=True, type=Path)
    parser.add_argument("--provider-plan-sha256", required=True)
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
    if arguments.maximum_usd <= 0:
        parser.error("--maximum-usd must be positive")
    return arguments


def validate(
    arguments: argparse.Namespace,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    experiment = arguments.experiment.expanduser().resolve()
    plan_path = experiment / "provider-plan.json"
    if sha256(plan_path) != arguments.provider_plan_sha256:
        raise TrainingContractError("provider plan SHA-256 differs from approval")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if not (
        plan.get("planVersion") == RUNNER.EXPECTED_PLAN_VERSION
        and plan.get("tinker", {}).get("projectID")
        == arguments.dedicated_private_project_id
        and plan.get("authorizationBoundary", {}).get("paidOperationsAuthorized")
        is False
    ):
        raise TrainingContractError("paid-preflight plan differs")
    projected = Decimal(
        plan["paidPreflightRequiredAfterAuthorization"][
            "projectedConservativeTotalUSD"
        ]
    )
    if projected > arguments.maximum_usd:
        raise TrainingContractError(
            f"preflight projection ${projected} exceeds authorized ${arguments.maximum_usd}"
        )
    if git_worktree_dirty(PROJECT):
        raise TrainingContractError("paid preflight requires a clean worktree")
    implementation = plan["implementation"]
    if implementation["codeRevision"] != git_revision(PROJECT):
        raise TrainingContractError("paid-preflight Git revision changed")
    for relative, digest in implementation["fileDigestsSHA256"].items():
        if sha256(PROJECT / relative) != digest:
            raise TrainingContractError(f"paid-preflight file changed: {relative}")
    if implementation["packageVersions"] != {
        package: importlib.metadata.version(package)
        for package in ("tinker", "tinker-cookbook", "transformers", "tokenizers")
    }:
        raise TrainingContractError("paid-preflight dependencies changed")
    corpus, examples, _, _ = validate_inputs(
        arguments.corpus.expanduser().resolve(),
        arguments.shared_pack.expanduser().resolve(),
    )
    if corpus["corpusID"] != plan["source"]["corpusID"]:
        raise TrainingContractError("paid-preflight corpus changed")
    return plan, corpus, examples


def main_run() -> int:
    arguments = parse_arguments()
    plan, corpus, examples = validate(arguments)
    experiment = arguments.experiment.expanduser().resolve()
    corpus_path = arguments.corpus.expanduser().resolve()
    shared_pack_path = arguments.shared_pack.expanduser().resolve()
    output = arguments.output.expanduser().resolve()
    manifest_path = output / "preflight.json"
    if output.exists():
        if not manifest_path.is_file():
            raise TrainingContractError("existing preflight output lacks manifest")
        prior = json.loads(manifest_path.read_text(encoding="utf-8"))
        if prior.get("status") == "complete_go":
            return 0
        raise TrainingContractError(
            "interrupted paid preflight is fail-closed; review before authorizing a retry"
        )
    output.mkdir(parents=True)
    manifest: dict[str, Any] = {
        "schemaVersion": 1,
        "preflightVersion": PREFLIGHT_VERSION,
        "status": "initialized",
        "startedAt": RUNNER.iso8601(),
        "providerPlanSHA256": arguments.provider_plan_sha256,
        "implementation": plan["implementation"],
        "authorization": {
            "maximumUSD": str(arguments.maximum_usd),
            "personalDataTransferConfirmed": True,
            "dedicatedPrivateProjectConfirmed": True,
            "currentPricesConfirmed": True,
        },
        "provider": {
            "projectID": arguments.dedicated_private_project_id,
            "models": [MODEL_SPECS[value]["model"] for value in RUNNER.MODEL_ORDER],
        },
        "models": {},
    }
    RUNNER.atomic_json(manifest_path, manifest)
    examples_by_id = {value["exampleID"]: value for value in examples}
    os.environ["TINKER_API_KEY"] = RUNNER.load_api_key(arguments.env_file)
    import tinker

    if importlib.metadata.version("tinker") != PINNED_TINKER_SDK_VERSION:
        raise TrainingContractError("Tinker SDK version changed")
    service = tinker.ServiceClient(
        project_id=arguments.dedicated_private_project_id,
        user_metadata={
            "purpose": "phase1-qwen35-paid-native-loss-preflight",
            "provider_plan_sha256": arguments.provider_plan_sha256,
        },
    )
    capabilities = {
        value.model_name: value for value in service.get_server_capabilities().supported_models
    }
    manifest["status"] = "running"
    manifest["providerSessionID"] = service.holder.get_session_id()
    RUNNER.atomic_json(manifest_path, manifest)

    for model_key in RUNNER.MODEL_ORDER:
        if manifest["models"].get(model_key, {}).get("status") == "passed":
            continue
        model = MODEL_SPECS[model_key]["model"]
        native_manifest, native_rows = audit_native_pack(experiment / model_key)
        rows = {value["exampleID"]: value for value in native_rows}
        capability = capabilities.get(model)
        if capability is None or (capability.max_context_length or 0) < native_manifest[
            "counts"
        ]["maximumFullSequenceTokens"]:
            raise TrainingContractError(f"{model_key} capability changed")
        base_sampler = service.create_sampling_client(base_model=model)
        runtime = RUNNER.validate_remote_tokenizer(
            model_key=model_key,
            local_manifest=native_manifest,
            local_rows=native_rows,
            remote_tokenizer=base_sampler.get_tokenizer(),
            corpus_directory=corpus_path,
            shared_pack_directory=shared_pack_path,
        )
        specification = plan["paidPreflightRequiredAfterAuthorization"]["models"][
            model_key
        ]
        probe_ids = specification["exampleIDs"]
        model_directory = output / model_key
        model_directory.mkdir()
        base_scores: list[dict[str, Any]] = []
        usage = RUNNER.Usage()
        for example_id in probe_ids:
            score = RUNNER.score_example(
                sampler=base_sampler,
                runtime=runtime,
                tinker=tinker,
                model_key=model_key,
                arm="preflight_base",
                block_id="preflight",
                example=examples_by_id[example_id],
                row=rows[example_id],
                checkpoint_path=None,
                include_nll=False,
                usage=usage,
            )
            RUNNER.append_jsonl(model_directory / "base-scores.jsonl", score)
            base_scores.append(score)

        training = plan["training"]
        client = service.create_lora_training_client(
            base_model=model,
            rank=training["rank"],
            seed=training["seed"],
            train_mlp=training["trainMLP"],
            train_attn=training["trainAttention"],
            train_unembed=training["trainUnembedding"],
            user_metadata={"purpose": f"phase1-{model_key}-tiny-overfit"},
        )
        optimizer_spec = training["optimizer"]
        optimizer = tinker.AdamParams(
            learning_rate=optimizer_spec["learningRate"],
            beta1=optimizer_spec["beta1"],
            beta2=optimizer_spec["beta2"],
            eps=optimizer_spec["epsilon"],
            weight_decay=optimizer_spec["weightDecay"],
            grad_clip_norm=optimizer_spec["gradientClipNorm"],
        )
        contracts = {value: adapt_row_to_tinker(rows[value]) for value in probe_ids}
        datums_list, _, _ = build_and_validate_sdk_datums(list(contracts.values()))
        datums = dict(zip(probe_ids, datums_list, strict=True))
        training_records: list[dict[str, Any]] = []
        for epoch in range(1, specification["epochs"] + 1):
            order = RUNNER.deterministic_order(probe_ids, epoch, training["seed"])
            for position, example_id in enumerate(order, 1):
                contract = contracts[example_id]
                forward_future = client.forward_backward(
                    [datums[example_id]], "cross_entropy"
                )
                optimizer_future = client.optim_step(optimizer)
                forward = forward_future.result()
                optimizer_future.result()
                logprobs = forward.loss_fn_outputs[0]["logprobs"].tolist()
                if len(logprobs) != contract.length:
                    raise TrainingContractError("preflight training logprob length changed")
                nll = -sum(
                    float(logprob) * weight
                    for logprob, weight in zip(
                        logprobs, contract.weights, strict=True
                    )
                )
                weighted = contract.weighted_positions
                record = {
                    "epoch": epoch,
                    "position": position,
                    "exampleID": example_id,
                    "weightedNLLSum": nll,
                    "weightedTokenCount": weighted,
                    "meanPreUpdateNLL": nll / weighted,
                    "completedAt": RUNNER.iso8601(),
                }
                RUNNER.append_jsonl(model_directory / "training.jsonl", record)
                training_records.append(record)
        prefix = f"phase1-{model_key}-native-loss-preflight"
        sampler_path = client.save_weights_for_sampler(
            f"{prefix}-sampler", ttl_seconds=training["checkpointTTLSeconds"]
        ).result().path
        state_path = client.save_state(
            f"{prefix}-optimizer-state", ttl_seconds=training["checkpointTTLSeconds"]
        ).result().path
        personalized_sampler = service.create_sampling_client(model_path=sampler_path)
        after_scores: list[dict[str, Any]] = []
        for example_id in probe_ids:
            score = RUNNER.score_example(
                sampler=personalized_sampler,
                runtime=runtime,
                tinker=tinker,
                model_key=model_key,
                arm="preflight_personalized",
                block_id="preflight",
                example=examples_by_id[example_id],
                row=rows[example_id],
                checkpoint_path=sampler_path,
                include_nll=False,
                usage=usage,
            )
            RUNNER.append_jsonl(model_directory / "personalized-scores.jsonl", score)
            after_scores.append(score)
        before_similarity = sum(value["characterSimilarity"] for value in base_scores) / len(
            base_scores
        )
        after_similarity = sum(value["characterSimilarity"] for value in after_scores) / len(
            after_scores
        )
        clean_after = sum(
            value["rendererParseTermination"] == "stop_sequence"
            for value in after_scores
        )
        exact_after = sum(value["exactMatch"] for value in after_scores)
        passed = clean_after == len(after_scores) and (
            exact_after >= 1 or after_similarity >= before_similarity + 0.20
        )
        manifest["models"][model_key] = {
            "status": "passed" if passed else "failed",
            "probeExampleIDs": probe_ids,
            "baseMeanCharacterSimilarity": before_similarity,
            "personalizedMeanCharacterSimilarity": after_similarity,
            "personalizedCleanNativeTerminations": clean_after,
            "personalizedExactMatches": exact_after,
            "samplerCheckpointPath": sampler_path,
            "optimizerStatePath": state_path,
            "trainingCalls": len(training_records),
            "completedAt": RUNNER.iso8601(),
        }
        RUNNER.atomic_json(manifest_path, manifest)
        if not passed:
            manifest["status"] = "complete_no_go"
            manifest["completedAt"] = RUNNER.iso8601()
            RUNNER.atomic_json(manifest_path, manifest)
            return 2

    manifest["status"] = "complete_go"
    manifest["completedAt"] = RUNNER.iso8601()
    manifest["artifactDigestsSHA256"] = {
        str(path.relative_to(output)): sha256(path)
        for path in sorted(output.rglob("*.jsonl"))
    }
    RUNNER.atomic_json(manifest_path, manifest)
    print(f"Qwen 35B paid preflight passed: {output}")
    return 0


def main() -> int:
    try:
        return main_run()
    except Exception as error:
        try:
            arguments = parse_arguments()
            manifest_path = arguments.output.expanduser().resolve() / "preflight.json"
            if manifest_path.is_file():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["status"] = "interrupted_fail_closed"
                manifest["interruptedAt"] = RUNNER.iso8601()
                manifest["failure"] = {
                    "type": type(error).__name__,
                    "message": str(error),
                    "traceback": traceback.format_exc(),
                    "automaticRetryAllowed": False,
                }
                RUNNER.atomic_json(manifest_path, manifest)
        except Exception:
            pass
        raise SystemExit(
            f"run-phase1-qwen35-preflight: {type(error).__name__}: {error}"
        )


if __name__ == "__main__":
    raise SystemExit(main())

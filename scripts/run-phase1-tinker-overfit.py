#!/usr/bin/env python3
"""Execute the explicitly approved, manifest-bound Phase 1 Tinker smoke run.

Unlike the local preparer, this command transmits the frozen Run 8 tokenized
examples to Tinker and creates paid training/evaluation/checkpoint operations.
It therefore requires the exact approved plan digest, explicit personal-data
transfer and price confirmations, the dedicated private project, and the exact
reviewed USD ceiling. It has no retry or extra-epoch policy above the SDK's
provider-managed transport recovery.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.metadata
import json
import math
import os
import sys
import traceback
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from phase1_tinker_overfit_contract import (
    BASE_MODEL,
    BATCH_EXAMPLES,
    CHECKPOINT_TTL_SECONDS,
    EPOCHS,
    HARD_MAXIMUM_PROJECTED_COST_USD,
    LORA_RANK,
    OPTIMIZER,
    PINNED_TINKER_SDK_VERSION,
    PREFILL_PRICE_PER_MILLION_USD,
    PRICING_AS_OF,
    SAMPLE_PRICE_PER_MILLION_USD,
    TRAINING_PRICE_PER_MILLION_USD,
    TRAINING_SEED,
    build_and_validate_sdk_datums,
    build_execution_plan,
    canonical_sha256,
    deterministic_epoch_order,
)
from phase1_training_contract import (
    IGNORE_LABEL,
    TrainingContractError,
    adapt_dataset_to_tinker,
    git_revision,
    git_worktree_dirty,
    load_json,
    sha256,
    validate_packed_dataset,
)


RUN_SCHEMA_VERSION = 1


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso8601(value: dt.datetime | None = None) -> str:
    return (value or utc_now()).isoformat().replace("+00:00", "Z")


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_api_key(path: Path) -> str:
    try:
        content = path.expanduser().read_text(encoding="utf-8").strip()
    except OSError as error:
        raise TrainingContractError(f"cannot read API key file: {error}") from error
    if not content:
        raise TrainingContractError("API key file is empty")
    if "\n" not in content and "=" not in content:
        return content
    values: dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip("\"'")
    key = values.get("TINKER_API_KEY")
    if not key:
        raise TrainingContractError(
            "API key file must be a raw key or define TINKER_API_KEY"
        )
    return key


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--approved-plan", required=True, type=Path)
    parser.add_argument("--approved-plan-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--dedicated-private-project-id", required=True)
    parser.add_argument("--maximum-usd", required=True, type=Decimal)
    parser.add_argument("--confirm-dedicated-private-project", action="store_true")
    parser.add_argument("--confirm-personal-data-transfer", action="store_true")
    parser.add_argument("--confirm-current-prices", action="store_true")
    parser.add_argument("--execute", action="store_true")
    arguments = parser.parse_args()
    required_flags = [
        (arguments.confirm_dedicated_private_project, "--confirm-dedicated-private-project"),
        (arguments.confirm_personal_data_transfer, "--confirm-personal-data-transfer"),
        (arguments.confirm_current_prices, "--confirm-current-prices"),
        (arguments.execute, "--execute"),
    ]
    for present, name in required_flags:
        if not present:
            parser.error(f"{name} is required")
    try:
        arguments.dedicated_private_project_id = str(
            uuid.UUID(arguments.dedicated_private_project_id)
        )
    except ValueError as error:
        parser.error(f"invalid dedicated project UUID: {error}")
    if arguments.maximum_usd != HARD_MAXIMUM_PROJECTED_COST_USD:
        parser.error(
            f"--maximum-usd must exactly equal reviewed ceiling {HARD_MAXIMUM_PROJECTED_COST_USD}"
        )
    return arguments


def validate_approved_plan(
    plan_path: Path,
    approved_digest: str,
    dataset: Any,
    contracts: list[Any],
    project_id: str,
) -> tuple[dict[str, Any], str]:
    plan_path = plan_path.expanduser().resolve()
    actual_digest = sha256(plan_path)
    if actual_digest != approved_digest:
        raise TrainingContractError("approved-plan SHA-256 does not match")
    plan = load_json(plan_path)
    if (
        plan.get("status") != "passed"
        or plan.get("test") != "phase1_tinker_overfit_local_sdk_preparation"
        or plan.get("scope")
        != "local_sdk_datum_construction_and_execution_plan_only"
    ):
        raise TrainingContractError("approved plan is not a passing preparation")
    if plan.get("project", {}).get("projectID") != project_id:
        raise TrainingContractError("approved plan uses a different project")
    source = plan.get("source", {})
    if (
        source.get("packingSHA256") != sha256(dataset.directory / "packing.json")
        or source.get("packedExamplesSHA256")
        != sha256(dataset.directory / "packed-examples.jsonl")
        or source.get("modelRepository") != BASE_MODEL
    ):
        raise TrainingContractError("approved plan does not bind this frozen pack")
    for relative_name, expected in plan.get("implementation", {}).get(
        "fileDigestsSHA256", {}
    ).items():
        current = Path(__file__).resolve().parent.parent / relative_name
        if not current.is_file() or sha256(current) != expected:
            raise TrainingContractError(
                f"approved implementation dependency changed: {relative_name}"
            )
    current_execution_plan = build_execution_plan(dataset.rows, contracts)
    if current_execution_plan != plan.get("executionPlan"):
        raise TrainingContractError("current execution contract differs from approved plan")
    cost = current_execution_plan["costCeiling"]
    if Decimal(cost["projectedIncludingReserve"]) > HARD_MAXIMUM_PROJECTED_COST_USD:
        raise TrainingContractError("approved projection exceeds maximum USD")
    if (
        cost["pricingAsOf"] != PRICING_AS_OF
        or Decimal(cost["trainingPricePerMillionTokens"])
        != TRAINING_PRICE_PER_MILLION_USD
        or Decimal(cost["prefillPricePerMillionTokens"])
        != PREFILL_PRICE_PER_MILLION_USD
        or Decimal(cost["samplingPricePerMillionTokens"])
        != SAMPLE_PRICE_PER_MILLION_USD
    ):
        raise TrainingContractError("approved pricing differs from frozen constants")
    return plan, actual_digest


@dataclass
class OperationBudget:
    ceilings: dict[str, Any]
    forward_backward_calls: int = 0
    optimizer_steps: int = 0
    training_tokens: int = 0
    compute_logprob_calls: int = 0
    sample_calls: int = 0
    prefill_tokens: int = 0
    sampled_tokens_reserved: int = 0
    sampled_tokens_observed: int = 0
    checkpoint_saves: int = 0

    def reserve_training(self, tokens: int) -> None:
        ceiling = self.ceilings["training"]
        proposed = (
            self.forward_backward_calls + 1,
            self.optimizer_steps + 1,
            self.training_tokens + tokens,
        )
        maximum = (
            ceiling["forwardBackwardCalls"],
            ceiling["optimizerSteps"],
            ceiling["submittedTokens"],
        )
        if any(value > limit for value, limit in zip(proposed, maximum, strict=True)):
            raise TrainingContractError("training operation would exceed approved ceiling")
        self.forward_backward_calls, self.optimizer_steps, self.training_tokens = proposed

    def reserve_logprobs(self, tokens: int) -> None:
        evaluation = self.ceilings["evaluation"]
        if self.compute_logprob_calls + 1 > evaluation["computeLogprobCalls"]:
            raise TrainingContractError("logprob call would exceed approved ceiling")
        if self.prefill_tokens + tokens > evaluation["prefillTokens"]:
            raise TrainingContractError("prefill would exceed approved ceiling")
        self.compute_logprob_calls += 1
        self.prefill_tokens += tokens

    def reserve_sample(self, prefill_tokens: int, maximum_tokens: int) -> None:
        evaluation = self.ceilings["evaluation"]
        if self.sample_calls + 1 > evaluation["sampleCalls"]:
            raise TrainingContractError("sample call would exceed approved ceiling")
        if self.prefill_tokens + prefill_tokens > evaluation["prefillTokens"]:
            raise TrainingContractError("sample prefill would exceed approved ceiling")
        if (
            self.sampled_tokens_reserved + maximum_tokens
            > evaluation["maximumSampledTokens"]
        ):
            raise TrainingContractError("sampling would exceed approved token ceiling")
        self.sample_calls += 1
        self.prefill_tokens += prefill_tokens
        self.sampled_tokens_reserved += maximum_tokens

    def reserve_checkpoint(self) -> None:
        if self.checkpoint_saves + 1 > self.ceilings["checkpointSaves"]:
            raise TrainingContractError("checkpoint would exceed approved ceiling")
        self.checkpoint_saves += 1

    def as_dict(self) -> dict[str, int]:
        return {
            "forwardBackwardCallsReserved": self.forward_backward_calls,
            "optimizerStepsReserved": self.optimizer_steps,
            "trainingTokensReserved": self.training_tokens,
            "computeLogprobCallsReserved": self.compute_logprob_calls,
            "sampleCallsReserved": self.sample_calls,
            "prefillTokensReserved": self.prefill_tokens,
            "sampledTokensReserved": self.sampled_tokens_reserved,
            "sampledTokensObserved": self.sampled_tokens_observed,
            "checkpointSavesReserved": self.checkpoint_saves,
        }


@dataclass
class RunJournal:
    path: Path
    report: dict[str, Any]
    budget: OperationBudget
    stages: list[dict[str, Any]] = field(default_factory=list)

    def stage(self, name: str, status: str, **details: Any) -> None:
        self.stages.append(
            {"at": iso8601(), "name": name, "status": status, **details}
        )
        self.flush()

    def flush(self) -> None:
        self.report["updatedAt"] = iso8601()
        self.report["operationUsage"] = self.budget.as_dict()
        self.report["stages"] = self.stages
        atomic_write_json(self.path, self.report)


def weighted_nll(
    row: dict[str, Any], logprobs: list[float | None]
) -> tuple[float, int]:
    labels = row["labels"]
    if len(logprobs) != len(labels):
        raise TrainingContractError(
            f"{row['exampleID']} logprob length differs from packed sequence"
        )
    total = 0.0
    count = 0
    for index, (label, logprob) in enumerate(zip(labels, logprobs, strict=True)):
        if label == IGNORE_LABEL:
            continue
        if logprob is None or not math.isfinite(logprob):
            raise TrainingContractError(
                f"{row['exampleID']} lacks a finite weighted logprob at {index}"
            )
        total -= logprob
        count += 1
    if count != row["targetTokenCount"] or count <= 0:
        raise TrainingContractError("weighted logprob count differs from target count")
    return total, count


def evaluate_nll(
    sampling_client: Any,
    rows: list[dict[str, Any]],
    budget: OperationBudget,
    tinker: Any,
) -> dict[str, Any]:
    total_nll = 0.0
    total_tokens = 0
    examples = []
    for row in rows:
        token_ids = row["inputIDs"]
        budget.reserve_logprobs(len(token_ids))
        logprobs = sampling_client.compute_logprobs(
            tinker.ModelInput.from_ints(tokens=token_ids)
        ).result()
        example_nll, token_count = weighted_nll(row, logprobs)
        total_nll += example_nll
        total_tokens += token_count
        examples.append(
            {
                "exampleID": row["exampleID"],
                "weightedTokens": token_count,
                "meanNLL": example_nll / token_count,
                "weightedLogprobsSHA256": canonical_sha256(
                    [
                        logprob
                        for label, logprob in zip(
                            row["labels"], logprobs, strict=True
                        )
                        if label != IGNORE_LABEL
                    ]
                ),
            }
        )
    return {
        "examples": len(rows),
        "weightedTokens": total_tokens,
        "meanNLL": total_nll / total_tokens,
        "perExample": examples,
    }


def normalize_stop_reason(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw)


def evaluate_generation(
    sampling_client: Any,
    rows: list[dict[str, Any]],
    budget: OperationBudget,
    tinker: Any,
    eos_token_id: int,
    paste_marker_ids: list[int],
) -> dict[str, Any]:
    results = []
    exact = 0
    eos_terminated = 0
    paste_exact = 0
    paste_total = 0
    for row in rows:
        prompt_ids = row["inputIDs"][: row["modelInputTokenCount"]]
        expected = row["inputIDs"][row["modelInputTokenCount"] :]
        if expected[-1] != eos_token_id:
            raise TrainingContractError("generation target lacks terminal EOS")
        maximum_tokens = row["targetTokenCount"] + 2
        budget.reserve_sample(len(prompt_ids), maximum_tokens)
        response = sampling_client.sample(
            prompt=tinker.ModelInput.from_ints(tokens=prompt_ids),
            num_samples=1,
            sampling_params=tinker.SamplingParams(
                max_tokens=maximum_tokens,
                temperature=0.0,
                seed=TRAINING_SEED,
                stop=[eos_token_id],
            ),
        ).result()
        if len(response.sequences) != 1:
            raise TrainingContractError("sampling returned an unexpected sequence count")
        sequence = response.sequences[0]
        observed = list(sequence.tokens)
        budget.sampled_tokens_observed += len(observed)
        stop_reason = normalize_stop_reason(sequence.stop_reason)
        included_eos = observed == expected
        omitted_stopping_eos = observed == expected[:-1] and stop_reason == "stop"
        is_exact = included_eos or omitted_stopping_eos
        has_eos_termination = included_eos or omitted_stopping_eos
        exact += int(is_exact)
        eos_terminated += int(has_eos_termination)

        paste_count = row["pasteActionCount"]
        if paste_count:
            paste_total += 1
            expected_without_eos = expected[:-1]
            observed_without_eos = observed[:-1] if included_eos else observed
            expected_marker_positions = [
                index
                for index in range(
                    0, len(expected_without_eos) - len(paste_marker_ids) + 1
                )
                if expected_without_eos[index : index + len(paste_marker_ids)]
                == paste_marker_ids
            ]
            observed_markers_exact = all(
                observed_without_eos[index : index + len(paste_marker_ids)]
                == paste_marker_ids
                for index in expected_marker_positions
            ) and len(expected_marker_positions) == paste_count
            paste_exact += int(observed_markers_exact and is_exact)

        results.append(
            {
                "exampleID": row["exampleID"],
                "expectedTokenCount": len(expected),
                "observedTokenCount": len(observed),
                "observedTokenIDs": observed,
                "observedSHA256": canonical_sha256(observed),
                "stopReason": stop_reason,
                "exactTarget": is_exact,
                "eosTerminated": has_eos_termination,
                "pasteActions": paste_count,
            }
        )
    count = len(rows)
    return {
        "examples": count,
        "exactTargets": exact,
        "exactTargetRate": exact / count,
        "eosTerminations": eos_terminated,
        "eosTerminationRate": eos_terminated / count,
        "pasteExamples": paste_total,
        "exactPasteExamples": paste_exact,
        "results": results,
    }


def compare_reload_nll(
    trained: dict[str, Any], reloaded: dict[str, Any]
) -> dict[str, Any]:
    trained_by_id = {item["exampleID"]: item for item in trained["perExample"]}
    differences = []
    for item in reloaded["perExample"]:
        prior = trained_by_id[item["exampleID"]]
        differences.append(abs(item["meanNLL"] - prior["meanNLL"]))
    maximum = max(differences, default=0.0)
    return {
        "maximumAbsoluteMeanNLLDifference": maximum,
        "exactSHA256Parity": all(
            item["weightedLogprobsSHA256"]
            == trained_by_id[item["exampleID"]]["weightedLogprobsSHA256"]
            for item in reloaded["perExample"]
        ),
        "passed": maximum == 0.0,
    }


def compare_reload_generation(
    trained: dict[str, Any], reloaded: dict[str, Any]
) -> dict[str, Any]:
    trained_by_id = {item["exampleID"]: item for item in trained["results"]}
    passed = all(
        item["observedTokenIDs"]
        == trained_by_id[item["exampleID"]]["observedTokenIDs"]
        and item["stopReason"] == trained_by_id[item["exampleID"]]["stopReason"]
        for item in reloaded["results"]
    )
    return {"exactTokenAndStopReasonParity": passed, "passed": passed}


def estimate_actual_cost(budget: OperationBudget) -> dict[str, str]:
    training = (
        Decimal(budget.training_tokens)
        * TRAINING_PRICE_PER_MILLION_USD
        / Decimal(1_000_000)
    )
    # This is conservatively uncached. The later billing audit can lower it.
    prefill = (
        Decimal(budget.prefill_tokens)
        * PREFILL_PRICE_PER_MILLION_USD
        / Decimal(1_000_000)
    )
    sampling = (
        Decimal(budget.sampled_tokens_observed)
        * SAMPLE_PRICE_PER_MILLION_USD
        / Decimal(1_000_000)
    )
    return {
        "trainingAtFrozenRate": str(training.quantize(Decimal("0.000001"))),
        "prefillAtUncachedFrozenRate": str(prefill.quantize(Decimal("0.000001"))),
        "samplingAtFrozenRate": str(sampling.quantize(Decimal("0.000001"))),
        "subtotalBeforeCheckpointStorage": str(
            (training + prefill + sampling).quantize(Decimal("0.000001"))
        ),
    }


def billing_snapshot(service_client: Any, session_id: str, started_at: dt.datetime) -> dict[str, Any]:
    floor_start = started_at.replace(minute=0, second=0, microsecond=0)
    floor_end = utc_now().replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
    try:
        response = service_client.create_rest_client().get_billing_usage(
            floor_start, floor_end
        ).result()
    except Exception as error:
        return {
            "status": "unavailable_or_not_yet_posted",
            "errorType": type(error).__name__,
            "rawErrorPersisted": False,
            "usageLagExpected": True,
        }
    events = [event for event in response.data if event.session_id == session_id]
    return {
        "status": "observed" if events else "pending_usage_lag",
        "usageLagExpected": not events,
        "events": [event.model_dump(mode="json") for event in events],
        "sessionMetadata": (
            response.sessions[session_id].model_dump(mode="json")
            if session_id in response.sessions
            else None
        ),
    }


def run() -> int:
    arguments = parse_arguments()
    output = arguments.output.expanduser().resolve()
    if output.exists():
        raise TrainingContractError(f"output already exists: {output}")

    dataset = validate_packed_dataset(arguments.input)
    contracts = adapt_dataset_to_tinker(dataset)
    plan, plan_digest = validate_approved_plan(
        arguments.approved_plan,
        arguments.approved_plan_sha256,
        dataset,
        contracts,
        arguments.dedicated_private_project_id,
    )
    sdk_datums, _, sdk_version = build_and_validate_sdk_datums(contracts)
    if sdk_version != PINNED_TINKER_SDK_VERSION:
        raise TrainingContractError("SDK version changed after Datum validation")

    try:
        import tinker
    except ImportError as error:
        raise TrainingContractError("Tinker SDK is unavailable") from error

    project_directory = Path(__file__).resolve().parent.parent
    started_at = utc_now()
    run_id = f"phase1-run8-overfit-{started_at.strftime('%Y%m%dT%H%M%SZ')}"
    budget = OperationBudget(plan["executionPlan"]["operationCeilings"])
    report: dict[str, Any] = {
        "schemaVersion": RUN_SCHEMA_VERSION,
        "test": "phase1_tinker_run8_mechanical_overfit",
        "scope": "mechanical_training_harness_only_not_phase1_hypothesis_evidence",
        "status": "initializing",
        "runID": run_id,
        "startedAt": iso8601(started_at),
        "implementation": {
            "codeRevision": git_revision(project_directory),
            "workingTreeDirtyAtStart": git_worktree_dirty(project_directory),
            "runnerSHA256": sha256(Path(__file__).resolve()),
            "tinkerSDKVersion": importlib.metadata.version("tinker"),
        },
        "authorization": {
            "approvedPlanPath": str(arguments.approved_plan.expanduser().resolve()),
            "approvedPlanSHA256": plan_digest,
            "personalDataTransferConfirmed": True,
            "dedicatedPrivateProjectConfirmed": True,
            "currentPricesConfirmed": True,
            "maximumUSD": str(arguments.maximum_usd),
            "authorizedAt": iso8601(),
        },
        "source": plan["source"],
        "project": plan["project"],
        "frozenExecutionPlan": plan["executionPlan"],
        "providerRetryPolicy": {
            "applicationLevelRetries": 0,
            "extraEpochs": 0,
            "sdkTransportRecovery": "provider_managed_not_counted_as_new_logical_operation",
        },
        "checkpoints": {},
        "evaluations": {},
        "training": {"epochs": []},
    }
    journal = RunJournal(output, report, budget)
    journal.flush()

    api_key = load_api_key(arguments.env_file)
    os.environ["TINKER_API_KEY"] = api_key
    service_client = tinker.ServiceClient(
        project_id=arguments.dedicated_private_project_id,
        user_metadata={
            "purpose": "phase1-run8-mechanical-overfit",
            "run_id": run_id,
            "approved_plan_sha256": plan_digest,
        },
    )
    capabilities = service_client.get_server_capabilities()
    supported = [
        model for model in capabilities.supported_models if model.model_name == BASE_MODEL
    ]
    if len(supported) != 1 or (supported[0].max_context_length or 0) < (
        dataset.maximum_sequence_length
    ):
        raise TrainingContractError("server model/context no longer matches approved plan")
    session_id = service_client.holder.get_session_id()
    report["provider"] = {
        "sessionID": session_id,
        "model": BASE_MODEL,
        "maximumContextLength": supported[0].max_context_length,
        "serverModelRevision": None,
        "serverModelRevisionStatus": "unverified_not_exposed_by_tinker_api",
    }
    journal.stage("authenticated_capability_gate", "passed")

    print(
        f"Computing base-model weighted NLL for {len(dataset.rows)} examples...",
        flush=True,
    )
    base_sampler = service_client.create_sampling_client(base_model=BASE_MODEL)
    baseline = evaluate_nll(base_sampler, dataset.rows, budget, tinker)
    report["evaluations"]["baseline"] = baseline
    journal.stage(
        "baseline_weighted_nll",
        "passed",
        meanNLL=baseline["meanNLL"],
        weightedTokens=baseline["weightedTokens"],
    )
    print(f"Baseline weighted NLL: {baseline['meanNLL']:.6f}", flush=True)

    print("Creating rank-32 LoRA training client...", flush=True)
    training_client = service_client.create_lora_training_client(
        base_model=BASE_MODEL,
        rank=LORA_RANK,
        seed=TRAINING_SEED,
        train_mlp=True,
        train_attn=True,
        train_unembed=True,
        user_metadata={"run_id": run_id, "purpose": "mechanical-overfit"},
    )
    training_info = training_client.get_info()
    report["training"]["providerModelInfo"] = training_info.model_dump(mode="json")
    contract_by_id = {contract.example_id: contract for contract in contracts}
    datum_by_id = {
        contract.example_id: datum
        for contract, datum in zip(contracts, sdk_datums, strict=True)
    }
    optimizer = tinker.AdamParams(
        learning_rate=OPTIMIZER["learningRate"],
        beta1=OPTIMIZER["beta1"],
        beta2=OPTIMIZER["beta2"],
        eps=OPTIMIZER["epsilon"],
        weight_decay=OPTIMIZER["weightDecay"],
        grad_clip_norm=OPTIMIZER["gradientClipNorm"],
    )

    for epoch in range(1, EPOCHS + 1):
        order = deterministic_epoch_order(list(datum_by_id), epoch)
        epoch_nll = 0.0
        epoch_weighted = 0
        metric_sums: dict[str, float] = {}
        for example_id in order:
            contract = contract_by_id[example_id]
            budget.reserve_training(contract.length)
            forward = training_client.forward_backward(
                [datum_by_id[example_id]], "cross_entropy"
            ).result()
            logprobs = forward.loss_fn_outputs[0]["logprobs"].tolist()
            if len(logprobs) != contract.length:
                raise TrainingContractError("training logprobs differ from Datum length")
            local_nll = -sum(
                float(logprob) * weight
                for logprob, weight in zip(
                    logprobs, contract.weights, strict=True
                )
            )
            epoch_nll += local_nll
            epoch_weighted += contract.weighted_positions
            for key, value in forward.metrics.items():
                metric_sums[key] = metric_sums.get(key, 0.0) + float(value)
            training_client.optim_step(optimizer).result()
        epoch_record = {
            "epoch": epoch,
            "exampleOrderSHA256": canonical_sha256(order),
            "weightedTokens": epoch_weighted,
            "meanPreUpdateNLL": epoch_nll / epoch_weighted,
            "meanProviderMetricsAcrossSteps": {
                key: value / len(order) for key, value in metric_sums.items()
            },
        }
        report["training"]["epochs"].append(epoch_record)
        journal.stage(
            f"training_epoch_{epoch}",
            "passed",
            meanPreUpdateNLL=epoch_record["meanPreUpdateNLL"],
        )
        print(
            f"Epoch {epoch:02d}/{EPOCHS}: pre-update weighted NLL "
            f"{epoch_record['meanPreUpdateNLL']:.6f}",
            flush=True,
        )

    checkpoint_prefix = run_id
    budget.reserve_checkpoint()
    sampler_path = training_client.save_weights_for_sampler(
        f"{checkpoint_prefix}-sampler",
        ttl_seconds=CHECKPOINT_TTL_SECONDS,
    ).result().path
    budget.reserve_checkpoint()
    state_path = training_client.save_state(
        f"{checkpoint_prefix}-optimizer-state",
        ttl_seconds=CHECKPOINT_TTL_SECONDS,
    ).result().path
    report["checkpoints"].update(
        {
            "trainedSampler": sampler_path,
            "optimizerState": state_path,
            "ttlSeconds": CHECKPOINT_TTL_SECONDS,
        }
    )
    journal.stage("trained_sampler_and_optimizer_state_saved", "passed")
    print("Saved sampler weights and full optimizer state.", flush=True)

    trained_sampler = service_client.create_sampling_client(model_path=sampler_path)
    print("Computing trained weighted NLL...", flush=True)
    final_nll = evaluate_nll(trained_sampler, dataset.rows, budget, tinker)
    report["evaluations"]["trainedNLL"] = final_nll
    nll_ratio = final_nll["meanNLL"] / baseline["meanNLL"]
    report["evaluations"]["finalToBaselineNLLRatio"] = nll_ratio
    journal.stage("trained_weighted_nll", "passed", meanNLL=final_nll["meanNLL"], ratio=nll_ratio)
    print(
        f"Trained weighted NLL: {final_nll['meanNLL']:.6f} "
        f"(ratio {nll_ratio:.6f})",
        flush=True,
    )

    print(
        f"Running exact greedy generation on all {len(dataset.rows)} targets...",
        flush=True,
    )
    trained_generation = evaluate_generation(
        trained_sampler,
        dataset.rows,
        budget,
        tinker,
        dataset.eos_token_id,
        dataset.paste_marker_token_ids,
    )
    report["evaluations"]["trainedGeneration"] = trained_generation
    journal.stage(
        "trained_exact_generation",
        "completed",
        exactTargets=trained_generation["exactTargets"],
        eosTerminations=trained_generation["eosTerminations"],
    )

    reload_ids = plan["executionPlan"]["checkpoints"]["reloadVerification"][
        "exampleIDs"
    ]
    reload_rows = [
        next(row for row in dataset.rows if row["exampleID"] == example_id)
        for example_id in reload_ids
    ]
    reloaded_training = service_client.create_training_client_from_state_with_optimizer(
        state_path,
        base_model=BASE_MODEL,
        user_metadata={"run_id": run_id, "purpose": "optimizer-reload-verification"},
    )
    budget.reserve_checkpoint()
    reloaded_sampler_path = reloaded_training.save_weights_for_sampler(
        f"{checkpoint_prefix}-reloaded-sampler",
        ttl_seconds=CHECKPOINT_TTL_SECONDS,
    ).result().path
    report["checkpoints"]["reloadedSampler"] = reloaded_sampler_path
    reloaded_sampler = service_client.create_sampling_client(
        model_path=reloaded_sampler_path
    )
    trained_subset_nll = {
        "perExample": [
            item
            for item in final_nll["perExample"]
            if item["exampleID"] in set(reload_ids)
        ]
    }
    reloaded_nll = evaluate_nll(reloaded_sampler, reload_rows, budget, tinker)
    reload_nll_parity = compare_reload_nll(trained_subset_nll, reloaded_nll)
    trained_subset_generation = {
        "results": [
            item
            for item in trained_generation["results"]
            if item["exampleID"] in set(reload_ids)
        ]
    }
    reloaded_generation = evaluate_generation(
        reloaded_sampler,
        reload_rows,
        budget,
        tinker,
        dataset.eos_token_id,
        dataset.paste_marker_token_ids,
    )
    reload_generation_parity = compare_reload_generation(
        trained_subset_generation, reloaded_generation
    )
    report["evaluations"]["reload"] = {
        "nll": reloaded_nll,
        "generation": reloaded_generation,
        "nllParity": reload_nll_parity,
        "generationParity": reload_generation_parity,
    }
    journal.stage(
        "optimizer_state_reload",
        "passed" if reload_nll_parity["passed"] and reload_generation_parity["passed"] else "failed",
    )

    acceptance = plan["executionPlan"]["evaluation"]["acceptance"]
    checks = {
        "nllRatio": nll_ratio <= acceptance["maximumFinalToBaselineWeightedNLLRatio"],
        "exactGreedyTargets": trained_generation["exactTargetRate"]
        >= acceptance["exactGreedyTargetRate"],
        "eosTermination": trained_generation["eosTerminationRate"]
        >= acceptance["eosTerminationRate"],
        "allPasteExamplesExact": trained_generation["exactPasteExamples"]
        == trained_generation["pasteExamples"],
        "optimizerReloadNLLParity": reload_nll_parity["passed"],
        "optimizerReloadGenerationParity": reload_generation_parity["passed"],
        "operationCeilingsExact": budget.as_dict()
        == {
            "forwardBackwardCallsReserved": plan["executionPlan"]["operationCeilings"]["training"]["forwardBackwardCalls"],
            "optimizerStepsReserved": plan["executionPlan"]["operationCeilings"]["training"]["optimizerSteps"],
            "trainingTokensReserved": plan["executionPlan"]["operationCeilings"]["training"]["submittedTokens"],
            "computeLogprobCallsReserved": plan["executionPlan"]["operationCeilings"]["evaluation"]["computeLogprobCalls"],
            "sampleCallsReserved": plan["executionPlan"]["operationCeilings"]["evaluation"]["sampleCalls"],
            "prefillTokensReserved": plan["executionPlan"]["operationCeilings"]["evaluation"]["prefillTokens"],
            "sampledTokensReserved": plan["executionPlan"]["operationCeilings"]["evaluation"]["maximumSampledTokens"],
            "sampledTokensObserved": budget.sampled_tokens_observed,
            "checkpointSavesReserved": plan["executionPlan"]["operationCeilings"]["checkpointSaves"],
        },
    }
    report["acceptance"] = {"checks": checks, "passed": all(checks.values())}
    report["cost"] = {
        "estimatedFromLogicalOperations": estimate_actual_cost(budget),
        "hardMaximumUSD": str(HARD_MAXIMUM_PROJECTED_COST_USD),
        "checkpointAndStorageExcludedFromEstimate": True,
    }
    report["billingAPI"] = billing_snapshot(service_client, session_id, started_at)
    report["completedAt"] = iso8601()
    report["status"] = "passed" if report["acceptance"]["passed"] else "failed_acceptance"
    journal.stage("final_acceptance", report["status"], checks=checks)
    journal.flush()
    print(
        f"Smoke run status: {report['status']}; report: {output}",
        flush=True,
    )
    return 0 if report["acceptance"]["passed"] else 2


def main() -> int:
    try:
        return run()
    except KeyboardInterrupt:
        raise SystemExit("run-phase1-tinker-overfit: interrupted")
    except Exception as error:
        output = None
        try:
            arguments = parse_arguments()
            output = arguments.output.expanduser().resolve()
            if output.exists():
                report = load_json(output)
                report["status"] = "failed_execution"
                report["failedAt"] = iso8601()
                report["failure"] = {
                    "type": type(error).__name__,
                    "message": str(error),
                    "traceback": traceback.format_exc(),
                    "apiKeyPersisted": False,
                }
                atomic_write_json(output, report)
        except Exception:
            pass
        raise SystemExit(
            f"run-phase1-tinker-overfit: {type(error).__name__}: {error}"
            + (f"; inspect {output}" if output else "")
        )


if __name__ == "__main__":
    raise SystemExit(main())

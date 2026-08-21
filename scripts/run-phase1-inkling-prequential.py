#!/usr/bin/env python3
"""Run the four-arm continual Inkling-Small Phase 1 experiment on Tinker."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.metadata
import json
import math
import os
import struct
import time
import traceback
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from phase1_experiment import canonical_bytes, target_text
from phase1_inkling import (
    ARM_NAMES,
    GENERATION_CONTRACT,
    INKLING_MODEL,
    INKLING_PLAN_VERSION,
    INKLING_RUNNER_VERSION,
    REASONING_CONDITIONS,
    TRAINING_CONTRACT,
    InklingContractError,
    append_jsonl,
    atomic_json,
    load_jsonl,
    load_experiment_blocks,
    load_semantic_inputs,
    parse_completion,
    sha256,
)
from phase1_prediction_metrics import score_prediction
from phase1_tinker_overfit_contract import build_and_validate_sdk_datums
from phase1_training_contract import (
    IGNORE_LABEL,
    TinkerDatumContract,
    git_revision,
    git_worktree_dirty,
)


@dataclass
class Usage:
    nll_calls: int = 0
    sample_calls: int = 0
    prefill_tokens: int = 0
    sampled_tokens_reserved: int = 0
    sampled_tokens_observed: int = 0
    training_calls: int = 0
    optimizer_steps: int = 0
    training_positions: int = 0
    checkpoint_saves: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "nllCalls": self.nll_calls,
            "sampleCalls": self.sample_calls,
            "prefillTokens": self.prefill_tokens,
            "sampledTokensReserved": self.sampled_tokens_reserved,
            "sampledTokensObserved": self.sampled_tokens_observed,
            "trainingCalls": self.training_calls,
            "optimizerSteps": self.optimizer_steps,
            "trainingPositions": self.training_positions,
            "checkpointSaves": self.checkpoint_saves,
        }


def iso8601() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def load_api_key(path: Path) -> str:
    content = path.expanduser().read_text(encoding="utf-8").strip()
    if not content:
        raise InklingContractError("Tinker API key file is empty")
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
        raise InklingContractError("env file does not define TINKER_API_KEY")
    return values["TINKER_API_KEY"]


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--semantic-pack", required=True, type=Path)
    parser.add_argument("--inkling-pack", required=True, type=Path)
    parser.add_argument("--provider-plan", required=True, type=Path)
    parser.add_argument("--provider-plan-sha256", required=True)
    parser.add_argument("--preflight", required=True, type=Path)
    parser.add_argument("--preflight-sha256", required=True)
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


def deterministic_order(example_ids: list[str], update_ordinal: int) -> list[str]:
    return sorted(
        example_ids,
        key=lambda value: (
            hashlib.sha256(
                (
                    "phase1-prequential-new-block:"
                    f"{TRAINING_CONTRACT['seed']}:{update_ordinal}:{value}"
                ).encode()
            ).digest(),
            value,
        ),
    )


def datum_contract(row: dict[str, Any]) -> TinkerDatumContract:
    input_ids = row["inputIDs"]
    labels = row["labels"]
    model_input = input_ids[:-1]
    targets = input_ids[1:]
    weights = [0.0 if label == IGNORE_LABEL else 1.0 for label in labels[1:]]
    for position, (label, target, weight) in enumerate(
        zip(labels[1:], targets, weights, strict=True)
    ):
        if weight and label != target:
            raise InklingContractError(
                f"{row['exampleID']} shifted label mismatch at {position}"
            )
    weighted = sum(value != 0.0 for value in weights)
    if weighted != row["targetTokenCount"]:
        raise InklingContractError("shifted Inkling loss count changed")
    return TinkerDatumContract(
        example_id=row["exampleID"],
        model_input_token_ids=model_input,
        target_tokens=targets,
        weights=weights,
        weighted_positions=weighted,
    )


def optimizer_batches(example_ids: list[str]) -> list[list[str]]:
    size = TRAINING_CONTRACT["optimizerBatchExamples"]
    if type(size) is not int or size <= 0:
        raise InklingContractError("optimizer batch size must be a positive integer")
    batches = [example_ids[start : start + size] for start in range(0, len(example_ids), size)]
    if not TRAINING_CONTRACT["partialFinalBatchAllowed"] and any(
        len(value) != size for value in batches
    ):
        raise InklingContractError("partial optimizer batch is forbidden")
    return batches


def micro_normalized_batch_contracts(
    contracts: list[TinkerDatumContract],
) -> tuple[list[TinkerDatumContract], int, float]:
    if not contracts:
        raise InklingContractError("optimizer batch may not be empty")
    target_tokens = sum(value.weighted_positions for value in contracts)
    if target_tokens <= 0:
        raise InklingContractError("optimizer batch has no loss-bearing tokens")
    # Tinker receives float32 weights. Quantize locally so the SDK round-trip
    # check compares the exact values that will reach the provider.
    token_weight = struct.unpack("!f", struct.pack("!f", 1.0 / target_tokens))[0]
    normalized = [
        TinkerDatumContract(
            example_id=value.example_id,
            model_input_token_ids=value.model_input_token_ids,
            target_tokens=value.target_tokens,
            weights=[token_weight if weight else 0.0 for weight in value.weights],
            weighted_positions=value.weighted_positions,
        )
        for value in contracts
    ]
    observed = sum(sum(item.weights) for item in normalized)
    if not math.isclose(observed, 1.0, rel_tol=1e-6, abs_tol=1e-6):
        raise InklingContractError("micro-normalized batch weights do not sum to one")
    return normalized, target_tokens, token_weight


def generation_ceiling(condition: str) -> int:
    try:
        value = GENERATION_CONTRACT["maximumTokensByCondition"][condition]
    except KeyError as error:
        raise InklingContractError(f"missing generation ceiling for {condition}") from error
    if type(value) is not int or value <= 0:
        raise InklingContractError("generation ceiling must be a positive integer")
    return value


def generation_disposition(
    *, stop_reason: str, parsed: dict[str, Any]
) -> tuple[str, bool]:
    valid_final = parsed.get("status") == "parsed" and bool(parsed.get("prediction"))
    if valid_final:
        return "accepted", True
    if stop_reason == "length":
        return GENERATION_CONTRACT["tokenCapWithoutValidFinalDisposition"], False
    return GENERATION_CONTRACT["missingFinalDisposition"], False


def weighted_nll(row: dict[str, Any], logprobs: list[float | None]) -> tuple[float, int]:
    if len(logprobs) != len(row["labels"]):
        raise InklingContractError("Tinker logprobs differ from the full sequence")
    total = 0.0
    count = 0
    for label, logprob in zip(row["labels"], logprobs, strict=True):
        if label == IGNORE_LABEL:
            continue
        if logprob is None or not math.isfinite(float(logprob)):
            raise InklingContractError("Tinker returned a nonfinite target logprob")
        total -= float(logprob)
        count += 1
    if count != row["targetTokenCount"]:
        raise InklingContractError("weighted NLL count changed")
    return total, count


def expected_score_sequence(blocks: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    return [
        (block["blockID"], ARM_NAMES[condition][kind], example_id)
        for block in blocks[1:]
        for condition in REASONING_CONDITIONS
        for kind in ("frozen", "personalized")
        for example_id in block["exampleIDs"]
    ]


def expected_update_sequence(blocks: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [
        (block["blockID"], condition)
        for block in blocks[:-1]
        for condition in REASONING_CONDITIONS
    ]


def implementation_record(plan: dict[str, Any]) -> dict[str, Any]:
    project = Path(__file__).resolve().parent.parent
    return {
        "codeRevision": git_revision(project),
        "workingTreeDirtyAtStart": git_worktree_dirty(project),
        "fileDigestsSHA256": {
            relative: sha256(project / relative)
            for relative in plan["implementation"]["fileDigestsSHA256"]
        },
        "tinkerSDKVersion": importlib.metadata.version("tinker"),
        "tmlRenderersVersion": importlib.metadata.version("tml-renderers"),
        "torchVersion": importlib.metadata.version("torch"),
    }


def validate_plan(
    arguments: argparse.Namespace,
    corpus_path: Path,
    pack_path: Path,
) -> tuple[dict[str, Any], str]:
    plan_path = arguments.provider_plan.expanduser().resolve()
    digest = sha256(plan_path)
    if digest != arguments.provider_plan_sha256:
        raise InklingContractError("provider plan hash differs from authorization")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if not (
        plan.get("planVersion") == INKLING_PLAN_VERSION
        and plan.get("status") == "authorized_for_execution"
        and plan.get("tinker", {}).get("model") == INKLING_MODEL
        and plan.get("protocol", {}).get("reasoningConditions")
        == REASONING_CONDITIONS
        and plan.get("protocol", {}).get("generationContract")
        == GENERATION_CONTRACT
        and plan.get("protocol", {}).get("trainingContract")
        == TRAINING_CONTRACT
        and Decimal(plan["tinker"]["hardExecutionCeilingUSD"])
        == arguments.maximum_usd
        and plan["tinker"]["projectedCostWithinHardCeiling"] is True
        and plan["tinker"]["projectID"]
        == arguments.dedicated_private_project_id
    ):
        raise InklingContractError("provider plan differs from the frozen contract")
    if Decimal(plan["tinker"]["projectedCostUSD"]["totalIncludingReserve"]) > arguments.maximum_usd:
        raise InklingContractError("projected Inkling cost exceeds the ceiling")
    expected_source = {
        "corpusSHA256": sha256(corpus_path / "corpus.json"),
        "examplesSHA256": sha256(corpus_path / "examples.jsonl"),
        "episodeBlocksSHA256": sha256(corpus_path / "episode-blocks.jsonl"),
        "inklingPackingSHA256": sha256(pack_path / "packing.json"),
    }
    for key, expected in expected_source.items():
        if plan["source"].get(key) != expected:
            raise InklingContractError(f"provider plan source changed: {key}")
    project = Path(__file__).resolve().parent.parent
    for relative, expected in plan["implementation"]["fileDigestsSHA256"].items():
        if not (project / relative).is_file() or sha256(project / relative) != expected:
            raise InklingContractError(f"planned implementation changed: {relative}")
    preflight_path = arguments.preflight.expanduser().resolve()
    if sha256(preflight_path) != arguments.preflight_sha256:
        raise InklingContractError("preflight hash changed")
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    if not (
        preflight.get("status") == "passed"
        and preflight.get("model") == INKLING_MODEL
        and preflight.get("projectID") == arguments.dedicated_private_project_id
        and preflight.get("source", {}).get("providerPlanSHA256") == digest
        and preflight.get("source", {}).get("inklingPackingSHA256")
        == sha256(pack_path / "packing.json")
    ):
        raise InklingContractError("authenticated Inkling preflight is not valid")
    return plan, digest


def cost_string(tokens: int, rate: str) -> str:
    return str(
        (Decimal(tokens) * Decimal(rate) / Decimal(1_000_000)).quantize(
            Decimal("0.000001")
        )
    )


def score_example(
    *,
    sampling_client: Any,
    tinker: Any,
    condition: str,
    arm: str,
    block_id: str,
    example: dict[str, Any],
    row: dict[str, Any],
    semantic_input: str,
    checkpoint_id: str | None,
    usage: Usage,
    prices: dict[str, str],
) -> dict[str, Any]:
    started = time.monotonic()
    nll_started = time.monotonic()
    usage.nll_calls += 1
    usage.prefill_tokens += len(row["inputIDs"])
    logprobs = sampling_client.compute_logprobs(
        tinker.ModelInput.from_ints(tokens=row["inputIDs"])
    ).result()
    nll_latency = time.monotonic() - nll_started
    nll_sum, weighted = weighted_nll(row, logprobs)
    prompt_ids = row["inputIDs"][: row["modelInputTokenCount"]]
    generation_started = time.monotonic()
    usage.sample_calls += 1
    usage.prefill_tokens += len(prompt_ids)
    maximum_tokens = generation_ceiling(condition)
    usage.sampled_tokens_reserved += maximum_tokens
    response = sampling_client.sample(
        prompt=tinker.ModelInput.from_ints(tokens=prompt_ids),
        num_samples=1,
        sampling_params=tinker.SamplingParams(
            max_tokens=maximum_tokens,
            temperature=GENERATION_CONTRACT["temperature"],
            seed=GENERATION_CONTRACT["seed"],
            stop=row["stopTokenIDs"],
        ),
    ).result()
    generation_latency = time.monotonic() - generation_started
    if len(response.sequences) != 1:
        raise InklingContractError("Tinker returned an unexpected sample count")
    sequence = response.sequences[0]
    observed = [int(value) for value in sequence.tokens]
    usage.sampled_tokens_observed += len(observed)
    parsed = parse_completion(
        semantic_input=semantic_input,
        effort=REASONING_CONDITIONS[condition],
        token_ids=observed,
    )
    expected = target_text(example["target"])
    prediction = parsed["prediction"]
    stop_reason = str(getattr(sequence.stop_reason, "value", sequence.stop_reason))
    disposition, generation_eligible = generation_disposition(
        stop_reason=stop_reason, parsed=parsed
    )
    prefill = len(row["inputIDs"]) + len(prompt_ids)
    query_cost = Decimal(cost_string(prefill, prices["prefill"])) + Decimal(
        cost_string(len(observed), prices["sample"])
    )
    return {
        "schemaVersion": 1,
        "runnerVersion": INKLING_RUNNER_VERSION,
        "blockID": block_id,
        "condition": condition,
        "effort": REASONING_CONDITIONS[condition],
        "arm": arm,
        "exampleID": example["exampleID"],
        "targetEventID": example["targetEventID"],
        "application": example.get("conditioningState", {})
        .get("destination", {})
        .get("appName"),
        "checkpointID": checkpoint_id,
        "semanticModelInputSHA256": row["semanticModelInputSHA256"],
        "modelInputTokenCount": row["modelInputTokenCount"],
        "fullSequenceTokenCount": len(row["inputIDs"]),
        "weightedNLLSum": nll_sum,
        "weightedTokenCount": weighted,
        "meanNLL": nll_sum / weighted,
        "target": expected,
        "pasteActionCount": row["pasteActionCount"],
        "prediction": prediction,
        "predictionTokenIDs": observed,
        "reasoning": parsed["reasoning"],
        "responseParse": parsed,
        "generationTemperature": GENERATION_CONTRACT["temperature"],
        "generationSeed": GENERATION_CONTRACT["seed"],
        "generationTokenCeiling": maximum_tokens,
        "stopReason": stop_reason,
        "generationDisposition": disposition,
        "generationEligibleForEvaluation": generation_eligible,
        "predictionMetrics": (
            score_prediction(
                expected, prediction, target_paste_actions=row["pasteActionCount"]
            )
            if generation_eligible
            else None
        ),
        "targetLikelihoodLatencySeconds": nll_latency,
        "generationLatencySeconds": generation_latency,
        "latencySeconds": time.monotonic() - started,
        "estimatedProviderCostUSDAtFrozenRates": str(
            query_cost.quantize(Decimal("0.000001"))
        ),
        "completedAt": iso8601(),
    }


def recompute_usage(scores: list[dict[str, Any]], updates: list[dict[str, Any]]) -> Usage:
    usage = Usage()
    for value in scores:
        usage.nll_calls += 1
        usage.sample_calls += 1
        usage.prefill_tokens += value["fullSequenceTokenCount"]
        usage.prefill_tokens += value["modelInputTokenCount"]
        usage.sampled_tokens_reserved += value["generationTokenCeiling"]
        usage.sampled_tokens_observed += len(value["predictionTokenIDs"])
    for value in updates:
        usage.training_calls += value["trainingCalls"]
        usage.optimizer_steps += value["optimizerSteps"]
        usage.training_positions += value["submittedPositions"]
        usage.checkpoint_saves += 2
    return usage


def run() -> int:
    arguments = parse_arguments()
    corpus_path = arguments.corpus.expanduser().resolve()
    semantic_pack = arguments.semantic_pack.expanduser().resolve()
    pack_path = arguments.inkling_pack.expanduser().resolve()
    output = arguments.output.expanduser().resolve()
    corpus = json.loads((corpus_path / "corpus.json").read_text(encoding="utf-8"))
    examples = load_jsonl(corpus_path / "examples.jsonl")
    example_by_id = {value["exampleID"]: value for value in examples}
    semantic_by_id = load_semantic_inputs(corpus_path, semantic_pack)
    packing = json.loads((pack_path / "packing.json").read_text(encoding="utf-8"))
    rows_by_condition = {
        condition: load_jsonl(pack_path / f"{condition}-packed-examples.jsonl")
        for condition in REASONING_CONDITIONS
    }
    row_by_condition = {
        condition: {value["exampleID"]: value for value in rows}
        for condition, rows in rows_by_condition.items()
    }
    plan, plan_digest = validate_plan(arguments, corpus_path, pack_path)

    contracts_by_condition: dict[str, dict[str, TinkerDatumContract]] = {}
    for condition, rows in rows_by_condition.items():
        contracts = [datum_contract(value) for value in rows]
        contracts_by_condition[condition] = {
            value.example_id: value for value in contracts
        }

    try:
        import tinker
    except ImportError as error:
        raise InklingContractError("Tinker SDK is unavailable") from error

    manifest_path = output / "inkling.json"
    scores_path = output / "scores.jsonl"
    updates_path = output / "updates.jsonl"
    if not output.exists():
        if git_worktree_dirty(Path(__file__).resolve().parent.parent):
            raise InklingContractError("Inkling execution requires a clean worktree")
        output.mkdir(parents=True)
        manifest: dict[str, Any] = {
            "schemaVersion": 1,
            "runnerVersion": INKLING_RUNNER_VERSION,
            "status": "initialized",
            "startedAt": iso8601(),
            "implementation": implementation_record(plan),
            "source": {
                "corpusID": corpus["corpusID"],
                "corpusSHA256": sha256(corpus_path / "corpus.json"),
                "inklingPackingSHA256": sha256(pack_path / "packing.json"),
                "providerPlanSHA256": plan_digest,
                "preflightSHA256": arguments.preflight_sha256,
            },
            "authorization": {
                "personalDataTransferConfirmed": True,
                "dedicatedPrivateProjectConfirmed": True,
                "currentPricesConfirmed": True,
                "maximumUSD": str(arguments.maximum_usd),
            },
            "provider": {
                "projectID": arguments.dedicated_private_project_id,
                "model": INKLING_MODEL,
                "reasoningConditions": REASONING_CONDITIONS,
                "generationContract": GENERATION_CONTRACT,
                "trainingContract": TRAINING_CONTRACT,
            },
            "counts": {"completedScores": 0, "completedUpdates": 0},
        }
        atomic_json(manifest_path, manifest)
        scores: list[dict[str, Any]] = []
        updates: list[dict[str, Any]] = []
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        current = implementation_record(plan)
        if current["workingTreeDirtyAtStart"] or manifest.get("implementation") != current:
            raise InklingContractError("resume implementation or revision changed")
        if manifest.get("inflightOperation") or manifest.get("activeUpdate"):
            raise InklingContractError("uncertain paid work cannot be replayed")
        if not (
            manifest.get("source", {}).get("providerPlanSHA256") == plan_digest
            and manifest.get("source", {}).get("preflightSHA256")
            == arguments.preflight_sha256
        ):
            raise InklingContractError("resume lineage changed")
        scores = load_jsonl(scores_path) if scores_path.exists() else []
        updates = load_jsonl(updates_path) if updates_path.exists() else []

    blocks = load_experiment_blocks(corpus_path)
    expected_scores = expected_score_sequence(blocks)
    observed_scores = [
        (value["blockID"], value["arm"], value["exampleID"]) for value in scores
    ]
    if observed_scores != expected_scores[: len(observed_scores)]:
        raise InklingContractError("scores are not an ordered protocol prefix")
    expected_updates = expected_update_sequence(blocks)
    observed_updates = [(value["afterBlockID"], value["condition"]) for value in updates]
    if observed_updates != expected_updates[: len(observed_updates)]:
        raise InklingContractError("updates are not an ordered protocol prefix")
    usage = recompute_usage(scores, updates)

    os.environ["TINKER_API_KEY"] = load_api_key(arguments.env_file)
    service = tinker.ServiceClient(
        project_id=arguments.dedicated_private_project_id,
        user_metadata={
            "purpose": "phase1-inkling-four-arm-prequential",
            "provider_plan_sha256": plan_digest,
        },
    )
    supported = [
        value
        for value in service.get_server_capabilities().supported_models
        if value.model_name == INKLING_MODEL
    ]
    maximum_sequence = max(
        value["maximumSequenceTokens"] for value in packing["counts"].values()
    )
    if len(supported) != 1 or (supported[0].max_context_length or 0) < maximum_sequence:
        raise InklingContractError("Tinker Inkling model/context changed")
    base_sampler = service.create_sampling_client(base_model=INKLING_MODEL)
    latest_update: dict[str, dict[str, Any]] = {}
    for update in updates:
        latest_update[update["condition"]] = update
    training_clients: dict[str, Any] = {}
    for condition in REASONING_CONDITIONS:
        prior = latest_update.get(condition)
        if prior:
            training_clients[condition] = service.create_training_client_from_state_with_optimizer(
                prior["optimizerStatePath"],
                base_model=INKLING_MODEL,
                user_metadata={"purpose": f"phase1-inkling-resume-{condition}"},
            )
        else:
            training_clients[condition] = service.create_lora_training_client(
                base_model=INKLING_MODEL,
                rank=TRAINING_CONTRACT["rank"],
                seed=TRAINING_CONTRACT["seed"],
                train_mlp=TRAINING_CONTRACT["trainMLP"],
                train_attn=TRAINING_CONTRACT["trainAttention"],
                train_unembed=TRAINING_CONTRACT["trainUnembedding"],
                user_metadata={"purpose": f"phase1-inkling-{condition}"},
            )
    optimizer_contract = TRAINING_CONTRACT["optimizer"]
    optimizer = tinker.AdamParams(
        learning_rate=optimizer_contract["learningRate"],
        beta1=optimizer_contract["beta1"],
        beta2=optimizer_contract["beta2"],
        eps=optimizer_contract["epsilon"],
        weight_decay=optimizer_contract["weightDecay"],
        grad_clip_norm=optimizer_contract["gradientClipNorm"],
    )
    manifest["status"] = "running"
    manifest["provider"].update({
        "sessionID": service.holder.get_session_id(),
        "maximumContextLength": supported[0].max_context_length,
    })
    atomic_json(manifest_path, manifest)

    prices = plan["tinker"]["pricesPerMillionUSD"]
    for block_ordinal, block in enumerate(blocks, 1):
        if block_ordinal > 1:
            for condition in REASONING_CONDITIONS:
                prior = latest_update[condition]
                personalized_sampler = service.create_sampling_client(
                    model_path=prior["samplerCheckpointPath"]
                )
                for kind, sampler in (
                    ("frozen", base_sampler),
                    ("personalized", personalized_sampler),
                ):
                    arm = ARM_NAMES[condition][kind]
                    for example_id in block["exampleIDs"]:
                        key = (block["blockID"], arm, example_id)
                        if key in observed_scores:
                            continue
                        row = row_by_condition[condition][example_id]
                        manifest["inflightOperation"] = {
                            "kind": "score_nll_and_generation",
                            "blockID": block["blockID"],
                            "condition": condition,
                            "arm": arm,
                            "exampleID": example_id,
                            "replayAllowed": False,
                        }
                        atomic_json(manifest_path, manifest)
                        score = score_example(
                            sampling_client=sampler,
                            tinker=tinker,
                            condition=condition,
                            arm=arm,
                            block_id=block["blockID"],
                            example=example_by_id[example_id],
                            row=row,
                            semantic_input=semantic_by_id[example_id],
                            checkpoint_id=(
                                None if kind == "frozen" else prior["samplerCheckpointPath"]
                            ),
                            usage=usage,
                            prices=prices,
                        )
                        append_jsonl(scores_path, score)
                        scores.append(score)
                        observed_scores.append(key)
                        manifest.pop("inflightOperation", None)
                        manifest["counts"]["completedScores"] = len(scores)
                        manifest["usage"] = usage.as_dict()
                        atomic_json(manifest_path, manifest)
                        print(
                            f"inkling-score {len(scores):03d}/{len(expected_scores)} "
                            f"condition={condition} arm={kind} nll={score['meanNLL']:.4f}",
                            flush=True,
                        )

        if block_ordinal == len(blocks):
            continue
        score_prefix_length = 4 * sum(len(value["exampleIDs"]) for value in blocks[1:block_ordinal])
        if observed_scores != expected_scores[:score_prefix_length]:
            raise InklingContractError("attempted update before complete block scoring")
        for condition in REASONING_CONDITIONS:
            update_key = (block["blockID"], condition)
            if update_key in observed_updates:
                continue
            client = training_clients[condition]
            ids = list(block["exampleIDs"])
            order = deterministic_order(ids, block_ordinal)
            batches = optimizer_batches(order)
            contracts = contracts_by_condition[condition]
            training_nll = 0.0
            training_weighted = 0
            update_started = time.monotonic()
            manifest["activeUpdate"] = {
                "condition": condition,
                "ordinal": block_ordinal,
                "totalSteps": len(batches),
                "totalExamples": len(order),
                "completedStepsNotCheckpointed": 0,
                "replayAllowed": False,
            }
            atomic_json(manifest_path, manifest)
            for position, batch_ids in enumerate(batches, 1):
                raw_contracts = [contracts[value] for value in batch_ids]
                normalized_contracts, batch_target_tokens, token_weight = (
                    micro_normalized_batch_contracts(raw_contracts)
                )
                batch_datums, _, _ = build_and_validate_sdk_datums(
                    normalized_contracts
                )
                manifest["inflightOperation"] = {
                    "kind": "micro_batch_forward_backward_and_optimizer_step",
                    "condition": condition,
                    "updateOrdinal": block_ordinal,
                    "position": position,
                    "exampleIDs": batch_ids,
                    "examples": len(batch_ids),
                    "lossBearingTargetTokens": batch_target_tokens,
                    "perTargetTokenWeight": token_weight,
                    "submittedPositions": sum(value.length for value in raw_contracts),
                    "replayAllowed": False,
                }
                atomic_json(manifest_path, manifest)
                result = client.forward_backward(batch_datums, "cross_entropy").result()
                if len(result.loss_fn_outputs) != len(raw_contracts):
                    raise InklingContractError("training result batch size changed")
                for raw_contract, output in zip(
                    raw_contracts, result.loss_fn_outputs, strict=True
                ):
                    logprobs = output["logprobs"].tolist()
                    if len(logprobs) != raw_contract.length:
                        raise InklingContractError("training logprob length changed")
                    training_nll -= sum(
                        float(logprob)
                        for logprob, weight in zip(
                            logprobs, raw_contract.weights, strict=True
                        )
                        if weight
                    )
                    training_weighted += raw_contract.weighted_positions
                client.optim_step(optimizer).result()
                usage.training_calls += 1
                usage.optimizer_steps += 1
                usage.training_positions += sum(
                    value.length for value in raw_contracts
                )
                manifest.pop("inflightOperation", None)
                manifest["activeUpdate"]["completedStepsNotCheckpointed"] = position
                manifest["usage"] = usage.as_dict()
                atomic_json(manifest_path, manifest)
            prefix = (
                f"phase1-inkling-{condition}-{corpus['corpusID'][:12]}-"
                f"block-{block_ordinal:02d}"
            )
            manifest["inflightOperation"] = {
                "kind": "save_sampler_checkpoint",
                "condition": condition,
                "updateOrdinal": block_ordinal,
                "replayAllowed": False,
            }
            atomic_json(manifest_path, manifest)
            sampler_path = client.save_weights_for_sampler(
                f"{prefix}-sampler",
                ttl_seconds=TRAINING_CONTRACT["checkpointTTLSeconds"],
            ).result().path
            manifest["inflightOperation"] = {
                "kind": "save_optimizer_state",
                "condition": condition,
                "updateOrdinal": block_ordinal,
                "replayAllowed": False,
            }
            atomic_json(manifest_path, manifest)
            state_path = client.save_state(
                f"{prefix}-optimizer-state",
                ttl_seconds=TRAINING_CONTRACT["checkpointTTLSeconds"],
            ).result().path
            usage.checkpoint_saves += 2
            prior = latest_update.get(condition)
            submitted = sum(contracts[value].length for value in order)
            update = {
                "schemaVersion": 1,
                "runnerVersion": INKLING_RUNNER_VERSION,
                "condition": condition,
                "effort": REASONING_CONDITIONS[condition],
                "updateOrdinal": block_ordinal,
                "afterBlockID": block["blockID"],
                "parentOptimizerStatePath": None if prior is None else prior["optimizerStatePath"],
                "samplerCheckpointPath": sampler_path,
                "optimizerStatePath": state_path,
                "checkpointTTLSeconds": TRAINING_CONTRACT["checkpointTTLSeconds"],
                "trainingPolicy": "warm_start_then_train_new_block_only_except_terminal_block",
                "epochsOverNewBlock": 1,
                "trainedExampleCount": len(ids),
                "trainedExampleIDsSHA256": hashlib.sha256(canonical_bytes(ids)).hexdigest(),
                "exampleOrderSHA256": hashlib.sha256(canonical_bytes(order)).hexdigest(),
                "optimizerBatchExamples": TRAINING_CONTRACT["optimizerBatchExamples"],
                "optimizerBatchSizes": [len(value) for value in batches],
                "lossReduction": TRAINING_CONTRACT["lossReduction"],
                "trainingCalls": len(batches),
                "optimizerSteps": len(batches),
                "submittedPositions": submitted,
                "lossBearingTokenPresentations": training_weighted,
                "meanPreUpdateNLL": training_nll / training_weighted,
                "latencySeconds": time.monotonic() - update_started,
                "estimatedTrainingCostUSDAtFrozenRate": cost_string(
                    submitted, prices["training"]
                ),
                "completedAt": iso8601(),
            }
            append_jsonl(updates_path, update)
            updates.append(update)
            observed_updates.append(update_key)
            latest_update[condition] = update
            manifest.pop("inflightOperation", None)
            manifest.pop("activeUpdate", None)
            manifest["counts"]["completedUpdates"] = len(updates)
            manifest["usage"] = usage.as_dict()
            atomic_json(manifest_path, manifest)
            print(
                f"inkling-update {block_ordinal}/{len(blocks)-1} "
                f"condition={condition} steps={len(batches)} examples={len(order)}",
                flush=True,
            )

    if observed_scores != expected_scores or observed_updates != expected_updates:
        raise InklingContractError("Inkling protocol ended incomplete")
    actual = {
        "trainingAtFrozenRate": cost_string(usage.training_positions, prices["training"]),
        "prefillAtUncachedFrozenRate": cost_string(usage.prefill_tokens, prices["prefill"]),
        "samplingAtFrozenRate": cost_string(usage.sampled_tokens_observed, prices["sample"]),
    }
    actual["subtotalBeforeCheckpointStorage"] = str(
        sum((Decimal(value) for value in actual.values()), Decimal(0)).quantize(
            Decimal("0.000001")
        )
    )
    manifest["status"] = "complete"
    manifest["completedAt"] = iso8601()
    manifest["usage"] = usage.as_dict()
    manifest["estimatedCost"] = actual
    manifest["finalCheckpoints"] = {
        condition: {
            "samplerCheckpointPath": latest_update[condition]["samplerCheckpointPath"],
            "optimizerStatePath": latest_update[condition]["optimizerStatePath"],
        }
        for condition in REASONING_CONDITIONS
    }
    manifest["artifactDigestsSHA256"] = {
        "scores.jsonl": sha256(scores_path),
        "updates.jsonl": sha256(updates_path),
    }
    atomic_json(manifest_path, manifest)
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    try:
        return run()
    except BaseException as error:
        try:
            arguments = parse_arguments()
            path = arguments.output.expanduser().resolve() / "inkling.json"
            if path.exists():
                manifest = json.loads(path.read_text(encoding="utf-8"))
                manifest["status"] = "interrupted"
                manifest["interruptedAt"] = iso8601()
                manifest["failure"] = {
                    "type": type(error).__name__,
                    "message": str(error),
                    "traceback": traceback.format_exc(),
                }
                atomic_json(path, manifest)
        finally:
            raise


if __name__ == "__main__":
    raise SystemExit(main())

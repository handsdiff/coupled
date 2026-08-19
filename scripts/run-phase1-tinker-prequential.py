#!/usr/bin/env python3
"""Run the resumable Tinker arms of the frozen Phase 1 experiment."""

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

from phase1_experiment import (
    ARM_FROZEN_QWEN,
    ARM_PERSONALIZED_QWEN,
    RUNNER_VERSION,
    TINKER_TRAINING_CONTRACT,
    canonical_bytes,
    load_jsonl,
    prospective_blocks,
    prospective_example_ids,
    target_text,
    validate_inputs,
)
from phase1_tinker_overfit_contract import (
    BASE_MODEL,
    PINNED_TINKER_SDK_VERSION,
    build_and_validate_sdk_datums,
)
from phase1_training_contract import (
    IGNORE_LABEL,
    TrainingContractError,
    adapt_dataset_to_tinker,
    compare_pack_tokenizers,
    git_revision,
    git_worktree_dirty,
    load_local_frozen_tokenizer,
    sha256,
)


TINKER_RUNNER_VERSION = "phase1-tinker-prequential-v3"
EXPECTED_PLAN_VERSION = "phase1-provider-plan-v4"
GENERATION_TOKEN_CEILING = 512
EPOCHS_PER_UPDATE = 1


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
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--packed", required=True, type=Path)
    parser.add_argument("--provider-plan", required=True, type=Path)
    parser.add_argument("--provider-plan-sha256", required=True)
    parser.add_argument("--frontier-output", required=True, type=Path)
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
    if arguments.maximum_usd != Decimal("40.00"):
        parser.error("--maximum-usd must exactly equal the reviewed $40.00 ceiling")
    return arguments


def estimated_cost(usage: Usage, plan: dict[str, Any]) -> dict[str, str]:
    prices = plan["tinker"]["pricesPerMillionUSD"]
    training = (
        Decimal(usage.training_positions)
        * Decimal(prices["training"])
        / Decimal(1_000_000)
    )
    prefill = (
        Decimal(usage.prefill_tokens)
        * Decimal(prices["prefill"])
        / Decimal(1_000_000)
    )
    sample = (
        Decimal(usage.sampled_tokens_observed)
        * Decimal(prices["sample"])
        / Decimal(1_000_000)
    )
    return {
        "trainingAtFrozenRate": str(training.quantize(Decimal("0.000001"))),
        "prefillAtUncachedFrozenRate": str(prefill.quantize(Decimal("0.000001"))),
        "samplingAtFrozenRate": str(sample.quantize(Decimal("0.000001"))),
        "subtotalBeforeCheckpointStorage": str(
            (training + prefill + sample).quantize(Decimal("0.000001"))
        ),
    }


def validate_plan(
    path: Path,
    expected_digest: str,
    corpus_path: Path,
    packed_path: Path,
    project_id: str,
) -> tuple[dict[str, Any], str]:
    actual = sha256(path)
    if actual != expected_digest:
        raise TrainingContractError("provider plan SHA-256 differs from approval")
    plan = json.loads(path.read_text(encoding="utf-8"))
    if plan.get("planVersion") != EXPECTED_PLAN_VERSION:
        raise TrainingContractError("unsupported provider plan version")
    project = Path(__file__).resolve().parent.parent
    for relative, expected_digest in plan.get("implementation", {}).get(
        "fileDigestsSHA256", {}
    ).items():
        current = project / relative
        if not current.is_file() or sha256(current) != expected_digest:
            raise TrainingContractError(
                f"provider plan implementation changed: {relative}"
            )
    source = plan.get("source", {})
    expected = {
        "corpusSHA256": sha256(corpus_path / "corpus.json"),
        "examplesSHA256": sha256(corpus_path / "examples.jsonl"),
        "packingSHA256": sha256(packed_path / "packing.json"),
        "packedExamplesSHA256": sha256(packed_path / "packed-examples.jsonl"),
        "contextPlansSHA256": sha256(packed_path / "context-plans.jsonl"),
    }
    for key, value in expected.items():
        if source.get(key) != value:
            raise TrainingContractError(f"provider plan source changed: {key}")
    tinker_plan = plan.get("tinker", {})
    if not (
        tinker_plan.get("projectID") == project_id
        and tinker_plan.get("model") == BASE_MODEL
        and plan.get("protocol", {}).get("qwenGenerationTokenCeilingPerExample")
        == GENERATION_TOKEN_CEILING
        and plan.get("protocol", {}).get("scoreCompleteBlockBeforeUpdate") is True
        and plan.get("protocol", {}).get("personalizedUpdatePolicy")
        == "warm_start_then_train_full_cumulative_corpus"
        and tinker_plan.get("trainingContract") == TINKER_TRAINING_CONTRACT
        and tinker_plan.get("hardExecutionCeilingUSD") == "40.00"
        and tinker_plan.get("interruptionPolicy", {}).get(
            "partialUpdateReplayAllowedUnderThisPlan"
        ) is False
        and tinker_plan.get("interruptionPolicy", {}).get(
            "inFlightOperationReplayAllowedUnderThisPlan"
        ) is False
    ):
        raise TrainingContractError("provider plan does not match Tinker contract")
    projected = Decimal(tinker_plan["projectedCostUSD"]["totalIncludingReserve"])
    if projected > Decimal("40.00"):
        raise TrainingContractError("provider plan exceeds the hard cost ceiling")
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
        "tinkerSDKVersion": importlib.metadata.version("tinker"),
    }


def validate_resume_state(
    manifest: dict[str, Any], current_implementation: dict[str, Any]
) -> None:
    if current_implementation["workingTreeDirtyAtStart"]:
        raise TrainingContractError("Tinker resume requires a clean working tree")
    if manifest.get("implementation") != current_implementation:
        raise TrainingContractError(
            "Tinker resume implementation or Git revision changed"
        )
    if manifest.get("inflightOperation"):
        raise TrainingContractError(
            "an in-flight paid operation cannot be replayed under this plan"
        )
    if manifest.get("activeUpdate"):
        raise TrainingContractError(
            "a partially executed paid update cannot be replayed under the $40 plan"
        )


def validate_frontier(
    directory: Path,
    corpus_path: Path,
    packed_path: Path,
    example_ids: list[str],
) -> dict[str, Any]:
    manifest = json.loads((directory / "frontier.json").read_text(encoding="utf-8"))
    scores = load_jsonl(directory / "scores.jsonl")
    if not (
        manifest.get("status") == "complete"
        and manifest.get("source", {}).get("corpusSHA256")
        == sha256(corpus_path / "corpus.json")
        and manifest.get("source", {}).get("packingSHA256")
        == sha256(packed_path / "packing.json")
        and manifest.get("artifactDigestsSHA256", {}).get("scores.jsonl")
        == sha256(directory / "scores.jsonl")
        and [row.get("exampleID") for row in scores] == example_ids
    ):
        raise TrainingContractError("frontier arm is not complete for this corpus")
    return manifest


def deterministic_order(example_ids: list[str], update_ordinal: int) -> list[str]:
    if len(set(example_ids)) != len(example_ids):
        raise TrainingContractError("cumulative training IDs are not unique")
    return sorted(
        example_ids,
        key=lambda value: (
            hashlib.sha256(
                (
                    "phase1-prequential:"
                    f"{TINKER_TRAINING_CONTRACT['seed']}:{update_ordinal}:{value}"
                ).encode()
            ).digest(),
            value,
        ),
    )


def weighted_nll(row: dict[str, Any], logprobs: list[float | None]) -> tuple[float, int]:
    if len(logprobs) != len(row["labels"]):
        raise TrainingContractError("Tinker logprob length differs from packed sequence")
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


def score_example(
    sampling_client: Any,
    tokenizer: Any,
    tinker: Any,
    arm: str,
    block_id: str,
    example: dict[str, Any],
    row: dict[str, Any],
    checkpoint_id: str | None,
    usage: Usage,
) -> dict[str, Any]:
    started = time.monotonic()
    likelihood_started = time.monotonic()
    usage.nll_calls += 1
    usage.prefill_tokens += len(row["inputIDs"])
    logprobs = sampling_client.compute_logprobs(
        tinker.ModelInput.from_ints(tokens=row["inputIDs"])
    ).result()
    target_likelihood_latency = time.monotonic() - likelihood_started
    nll_sum, weighted = weighted_nll(row, logprobs)

    prompt_ids = row["inputIDs"][: row["modelInputTokenCount"]]
    generation_started = time.monotonic()
    usage.sample_calls += 1
    usage.prefill_tokens += len(prompt_ids)
    usage.sampled_tokens_reserved += GENERATION_TOKEN_CEILING
    response = sampling_client.sample(
        prompt=tinker.ModelInput.from_ints(tokens=prompt_ids),
        num_samples=1,
        sampling_params=tinker.SamplingParams(
            max_tokens=GENERATION_TOKEN_CEILING,
            temperature=0.0,
            seed=TINKER_TRAINING_CONTRACT["seed"],
            stop=[row["eosTokenID"]],
        ),
    ).result()
    generation_latency = time.monotonic() - generation_started
    if len(response.sequences) != 1:
        raise TrainingContractError("Tinker returned an unexpected sample count")
    sequence = response.sequences[0]
    observed = list(sequence.tokens)
    usage.sampled_tokens_observed += len(observed)
    without_eos = observed[:-1] if observed and observed[-1] == row["eosTokenID"] else observed
    prediction = tokenizer.decode(without_eos, skip_special_tokens=True)
    expected = target_text(example["target"])
    return {
        "schemaVersion": 2,
        "runnerVersion": TINKER_RUNNER_VERSION,
        "blockID": block_id,
        "arm": arm,
        "exampleID": example["exampleID"],
        "targetEventID": example["targetEventID"],
        "application": example.get("conditioningState", {})
        .get("destination", {})
        .get("appName"),
        "checkpointID": checkpoint_id,
        "modelInputTokenCount": row["modelInputTokenCount"],
        "weightedNLLSum": nll_sum,
        "weightedTokenCount": weighted,
        "meanNLL": nll_sum / weighted,
        "weightedLogprobsSHA256": hashlib.sha256(
            canonical_bytes([
                value
                for label, value in zip(row["labels"], logprobs, strict=True)
                if label != IGNORE_LABEL
            ])
        ).hexdigest(),
        "target": expected,
        "pasteActionCount": row["pasteActionCount"],
        "prediction": prediction,
        "predictionTokenIDs": observed,
        "stopReason": normalize_stop_reason(sequence.stop_reason),
        "exactMatch": prediction == expected,
        "normalizedExactMatch": prediction.strip() == expected.strip(),
        "characterSimilarity": SequenceMatcher(None, expected, prediction).ratio(),
        "latencyInstrumentationVersion": "tinker-score-latency-v2-split-requests",
        "targetLikelihoodLatencySeconds": target_likelihood_latency,
        "generationLatencySeconds": generation_latency,
        "latencySeconds": time.monotonic() - started,
        "completedAt": iso8601(),
    }


def expected_score_sequence(
    blocks: list[dict[str, Any]],
) -> list[tuple[str, str, str]]:
    return [
        (block["blockID"], arm, example_id)
        for block in prospective_blocks(blocks)
        for arm in (ARM_FROZEN_QWEN, ARM_PERSONALIZED_QWEN)
        for example_id in block["exampleIDs"]
    ]


def recompute_usage(
    scores: list[dict[str, Any]], updates: list[dict[str, Any]]
) -> Usage:
    usage = Usage()
    for score in scores:
        usage.nll_calls += 1
        usage.sample_calls += 1
        usage.prefill_tokens += score["fullSequenceTokenCount"]
        usage.prefill_tokens += score["modelInputTokenCount"]
        usage.sampled_tokens_reserved += GENERATION_TOKEN_CEILING
        usage.sampled_tokens_observed += len(score["predictionTokenIDs"])
    for update in updates:
        usage.training_calls += update["trainingCalls"]
        usage.optimizer_steps += update["optimizerSteps"]
        usage.training_positions += update["submittedPositions"]
        usage.checkpoint_saves += 2
    return usage


def summarize(scores: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries = []
    for arm in (ARM_FROZEN_QWEN, ARM_PERSONALIZED_QWEN):
        rows = [value for value in scores if value["arm"] == arm]
        weighted = sum(value["weightedTokenCount"] for value in rows)
        summaries.append({
            "arm": arm,
            "examples": len(rows),
            "exactMatches": sum(value["exactMatch"] for value in rows),
            "normalizedExactMatches": sum(value["normalizedExactMatch"] for value in rows),
            "macroExampleAverageNLL": sum(value["meanNLL"] for value in rows) / len(rows),
            "microTargetTokenNLL": sum(value["weightedNLLSum"] for value in rows) / weighted,
            "weightedTokens": weighted,
            "meanCharacterSimilarity": sum(value["characterSimilarity"] for value in rows)
            / len(rows),
        })
    return summaries


def run() -> int:
    arguments = parse_arguments()
    corpus_path = arguments.corpus.expanduser().resolve()
    packed_path = arguments.packed.expanduser().resolve()
    plan_path = arguments.provider_plan.expanduser().resolve()
    frontier_path = arguments.frontier_output.expanduser().resolve()
    output = arguments.output.expanduser().resolve()
    corpus, examples, packed, _ = validate_inputs(corpus_path, packed_path)
    example_ids = [value["exampleID"] for value in examples]
    evaluation_example_ids = prospective_example_ids(corpus["blocking"]["blocks"])
    packed_by_id = {value["exampleID"]: value for value in packed.rows}
    example_by_id = {value["exampleID"]: value for value in examples}
    plan, plan_digest = validate_plan(
        plan_path,
        arguments.provider_plan_sha256,
        corpus_path,
        packed_path,
        arguments.dedicated_private_project_id,
    )
    frontier = validate_frontier(
        frontier_path, corpus_path, packed_path, evaluation_example_ids
    )
    contracts = adapt_dataset_to_tinker(packed)
    datums, _, sdk_version = build_and_validate_sdk_datums(contracts)
    datum_by_id = {
        contract.example_id: datum
        for contract, datum in zip(contracts, datums, strict=True)
    }
    contract_by_id = {value.example_id: value for value in contracts}

    try:
        import tinker
    except ImportError as error:
        raise TrainingContractError("Tinker SDK is unavailable") from error
    if importlib.metadata.version("tinker") != PINNED_TINKER_SDK_VERSION:
        raise TrainingContractError("Tinker SDK version changed")

    manifest_path = output / "tinker.json"
    scores_path = output / "scores.jsonl"
    updates_path = output / "updates.jsonl"
    if not output.exists():
        if git_worktree_dirty(Path(__file__).resolve().parent.parent):
            raise TrainingContractError("Tinker execution requires a clean working tree")
        output.mkdir(parents=True)
        manifest: dict[str, Any] = {
            "schemaVersion": 1,
            "runnerVersion": TINKER_RUNNER_VERSION,
            "phase1ProtocolVersion": RUNNER_VERSION,
            "status": "initialized",
            "startedAt": iso8601(),
            "implementation": implementation_record(plan),
            "source": {
                "corpusID": corpus["corpusID"],
                "corpusSHA256": sha256(corpus_path / "corpus.json"),
                "packingSHA256": sha256(packed_path / "packing.json"),
                "providerPlanSHA256": plan_digest,
                "frontierManifestSHA256": sha256(frontier_path / "frontier.json"),
                "frontierScoresSHA256": sha256(frontier_path / "scores.jsonl"),
            },
            "authorization": {
                "personalDataTransferConfirmed": True,
                "dedicatedPrivateProjectConfirmed": True,
                "currentPricesConfirmed": True,
                "maximumUSD": str(arguments.maximum_usd),
            },
            "provider": {
                "projectID": arguments.dedicated_private_project_id,
                "model": BASE_MODEL,
                "trainingContract": plan["tinker"]["trainingContract"],
                "epochsPerCumulativeUpdate": EPOCHS_PER_UPDATE,
            },
            "counts": {"completedScores": 0, "completedUpdates": 0},
        }
        atomic_json(manifest_path, manifest)
        scores: list[dict[str, Any]] = []
        updates: list[dict[str, Any]] = []
    else:
        if not manifest_path.exists():
            raise TrainingContractError("existing Tinker output lacks tinker.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        current_implementation = implementation_record(plan)
        validate_resume_state(manifest, current_implementation)
        if not (
            manifest.get("runnerVersion") == TINKER_RUNNER_VERSION
            and manifest.get("source", {}).get("providerPlanSHA256") == plan_digest
            and manifest.get("source", {}).get("corpusSHA256")
            == sha256(corpus_path / "corpus.json")
            and manifest.get("source", {}).get("frontierScoresSHA256")
            == sha256(frontier_path / "scores.jsonl")
        ):
            raise TrainingContractError("existing Tinker output has different lineage")
        scores = load_jsonl(scores_path) if scores_path.exists() else []
        updates = load_jsonl(updates_path) if updates_path.exists() else []

    expected_scores = expected_score_sequence(corpus["blocking"]["blocks"])
    observed_scores = [
        (value["blockID"], value["arm"], value["exampleID"])
        for value in scores
    ]
    if observed_scores != expected_scores[: len(observed_scores)]:
        raise TrainingContractError("Tinker scores are not an ordered protocol prefix")
    expected_update_blocks = [
        value["blockID"] for value in corpus["blocking"]["blocks"]
    ]
    if [value["afterBlockID"] for value in updates] != expected_update_blocks[: len(updates)]:
        raise TrainingContractError("Tinker updates are not an ordered block prefix")
    usage = recompute_usage(scores, updates)

    os.environ["TINKER_API_KEY"] = load_api_key(arguments.env_file)
    service = tinker.ServiceClient(
        project_id=arguments.dedicated_private_project_id,
        user_metadata={
            "purpose": "phase1-initial-prequential-experiment",
            "provider_plan_sha256": plan_digest,
        },
    )
    capabilities = service.get_server_capabilities()
    supported = [value for value in capabilities.supported_models if value.model_name == BASE_MODEL]
    if len(supported) != 1 or (supported[0].max_context_length or 0) < packed.maximum_sequence_length:
        raise TrainingContractError("Tinker model/context differs from approved plan")
    base_sampler = service.create_sampling_client(base_model=BASE_MODEL)
    local_tokenizer, _ = load_local_frozen_tokenizer(packed)
    remote_tokenizer = base_sampler.get_tokenizer()
    tokenizer_comparison = compare_pack_tokenizers(packed, local_tokenizer, remote_tokenizer)
    if not tokenizer_comparison["compatible"]:
        raise TrainingContractError("Tinker tokenizer differs from frozen pack")

    if updates:
        prior_state = updates[-1]["optimizerStatePath"]
        training_client = service.create_training_client_from_state_with_optimizer(
            prior_state,
            base_model=BASE_MODEL,
            user_metadata={"purpose": "phase1-prequential-resume"},
        )
    else:
        training_contract = plan["tinker"]["trainingContract"]
        training_client = service.create_lora_training_client(
            base_model=BASE_MODEL,
            rank=training_contract["rank"],
            seed=training_contract["seed"],
            train_mlp=training_contract["trainMLP"],
            train_attn=training_contract["trainAttention"],
            train_unembed=training_contract["trainUnembedding"],
            user_metadata={"purpose": "phase1-initial-prequential-experiment"},
        )
    training_contract = plan["tinker"]["trainingContract"]
    optimizer_contract = training_contract["optimizer"]
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
        "tokenizerComparisonStatus": tokenizer_comparison["status"],
    })
    atomic_json(manifest_path, manifest)

    for block_ordinal, block in enumerate(corpus["blocking"]["blocks"], 1):
        prior_update = updates[block_ordinal - 2] if block_ordinal > 1 else None
        personalized_checkpoint = prior_update["samplerCheckpointPath"] if prior_update else None
        if block_ordinal > 1:
            personalized_sampler = service.create_sampling_client(
                model_path=personalized_checkpoint
            )
            for arm, sampler in (
                (ARM_FROZEN_QWEN, base_sampler),
                (ARM_PERSONALIZED_QWEN, personalized_sampler),
            ):
                for example_id in block["exampleIDs"]:
                    key = (block["blockID"], arm, example_id)
                    if key in observed_scores:
                        continue
                    row = packed_by_id[example_id]
                    manifest["inflightOperation"] = {
                        "kind": "score_nll_and_generation",
                        "blockID": block["blockID"],
                        "arm": arm,
                        "exampleID": example_id,
                        "replayAllowedUnderCurrentPlan": False,
                    }
                    atomic_json(manifest_path, manifest)
                    score = score_example(
                        sampler,
                        remote_tokenizer,
                        tinker,
                        arm,
                        block["blockID"],
                        example_by_id[example_id],
                        {**row, "eosTokenID": packed.eos_token_id},
                        personalized_checkpoint if arm == ARM_PERSONALIZED_QWEN else None,
                        usage,
                    )
                    score["fullSequenceTokenCount"] = len(row["inputIDs"])
                    append_jsonl(scores_path, score)
                    scores.append(score)
                    observed_scores.append(key)
                    manifest.pop("inflightOperation", None)
                    manifest["counts"]["completedScores"] = len(scores)
                    manifest["usage"] = usage.as_dict()
                    atomic_json(manifest_path, manifest)
                    print(
                        f"tinker-score {len(scores):03d}/{len(expected_scores)} "
                        f"block={block['blockID']} arm={arm} nll={score['meanNLL']:.4f}",
                        flush=True,
                    )

        if len(updates) >= block_ordinal:
            continue
        expected_prefix = expected_scores[: 2 * sum(
            len(value["exampleIDs"])
            for value in corpus["blocking"]["blocks"][1:block_ordinal]
        )]
        if observed_scores != expected_prefix:
            raise TrainingContractError("attempted update before complete block scoring")

        cumulative_ids = [
            example_id
            for value in corpus["blocking"]["blocks"][:block_ordinal]
            for example_id in value["exampleIDs"]
        ]
        order = deterministic_order(cumulative_ids, block_ordinal)
        training_nll = 0.0
        training_weighted = 0
        update_started = time.monotonic()
        manifest["activeUpdate"] = {
            "ordinal": block_ordinal,
            "completedStepsNotCheckpointed": 0,
            "totalSteps": len(order),
            "submittedPositionsNotCheckpointed": 0,
            "replayAllowedUnderCurrentPlan": False,
        }
        atomic_json(manifest_path, manifest)
        for position, example_id in enumerate(order, 1):
            contract = contract_by_id[example_id]
            manifest["inflightOperation"] = {
                "kind": "forward_backward_and_optimizer_step",
                "updateOrdinal": block_ordinal,
                "position": position,
                "exampleID": example_id,
                "submittedPositions": contract.length,
                "replayAllowedUnderCurrentPlan": False,
            }
            atomic_json(manifest_path, manifest)
            forward = training_client.forward_backward(
                [datum_by_id[example_id]], "cross_entropy"
            ).result()
            logprobs = forward.loss_fn_outputs[0]["logprobs"].tolist()
            if len(logprobs) != contract.length:
                raise TrainingContractError("training logprob length differs from Datum")
            training_nll -= sum(
                float(logprob) * weight
                for logprob, weight in zip(logprobs, contract.weights, strict=True)
            )
            training_weighted += contract.weighted_positions
            training_client.optim_step(optimizer).result()
            usage.training_calls += 1
            usage.optimizer_steps += 1
            usage.training_positions += contract.length
            manifest["activeUpdate"] = {
                "ordinal": block_ordinal,
                "completedStepsNotCheckpointed": position,
                "totalSteps": len(order),
                "submittedPositionsNotCheckpointed": sum(
                    contract_by_id[value].length for value in order[:position]
                ),
                "restartPolicy": "discard_partial_client_and_require_explicit_replay",
            }
            manifest["activeUpdate"]["restartPolicy"] = (
                "stop_and_require_a_new_cost-authorized_plan"
            )
            manifest.pop("inflightOperation", None)
            manifest["usage"] = usage.as_dict()
            atomic_json(manifest_path, manifest)
            if position % 10 == 0 or position == len(order):
                print(
                    f"tinker-update {block_ordinal}/4 step={position}/{len(order)}",
                    flush=True,
                )

        prefix = f"phase1-initial-{corpus['corpusID'][:16]}-block-{block_ordinal:02d}"
        manifest["inflightOperation"] = {
            "kind": "save_sampler_checkpoint",
            "updateOrdinal": block_ordinal,
            "replayAllowedUnderCurrentPlan": False,
        }
        atomic_json(manifest_path, manifest)
        sampler_path = training_client.save_weights_for_sampler(
            f"{prefix}-sampler",
            ttl_seconds=training_contract["checkpointTTLSeconds"],
        ).result().path
        manifest["activeUpdate"]["samplerCheckpointPath"] = sampler_path
        manifest["inflightOperation"] = {
            "kind": "save_optimizer_state",
            "updateOrdinal": block_ordinal,
            "replayAllowedUnderCurrentPlan": False,
        }
        atomic_json(manifest_path, manifest)
        state_path = training_client.save_state(
            f"{prefix}-optimizer-state",
            ttl_seconds=training_contract["checkpointTTLSeconds"],
        ).result().path
        usage.checkpoint_saves += 2
        update = {
            "schemaVersion": 1,
            "runnerVersion": TINKER_RUNNER_VERSION,
            "updateOrdinal": block_ordinal,
            "afterBlockID": block["blockID"],
            "parentOptimizerStatePath": updates[-1]["optimizerStatePath"] if updates else None,
            "samplerCheckpointPath": sampler_path,
            "optimizerStatePath": state_path,
            "checkpointTTLSeconds": training_contract["checkpointTTLSeconds"],
            "trainingPolicy": "warm_start_then_train_full_cumulative_corpus",
            "epochsOverCumulativeCorpus": EPOCHS_PER_UPDATE,
            "cumulativeExampleCount": len(cumulative_ids),
            "cumulativeExampleIDsSHA256": hashlib.sha256(canonical_bytes(cumulative_ids)).hexdigest(),
            "exampleOrderSHA256": hashlib.sha256(canonical_bytes(order)).hexdigest(),
            "trainingCalls": len(order),
            "optimizerSteps": len(order),
            "submittedPositions": sum(contract_by_id[value].length for value in order),
            "lossBearingTokenPresentations": training_weighted,
            "meanPreUpdateNLL": training_nll / training_weighted,
            "latencySeconds": time.monotonic() - update_started,
            "completedAt": iso8601(),
        }
        append_jsonl(updates_path, update)
        updates.append(update)
        manifest.pop("inflightOperation", None)
        manifest.pop("activeUpdate", None)
        manifest["counts"]["completedUpdates"] = len(updates)
        manifest["usage"] = usage.as_dict()
        atomic_json(manifest_path, manifest)
        print(
            f"tinker-update {block_ordinal}/4 saved nll={update['meanPreUpdateNLL']:.4f}",
            flush=True,
        )

    expected_plan_usage = plan["tinker"]["operations"]
    exact_usage = {
        "nllAndGenerationPrefill": usage.prefill_tokens
        == expected_plan_usage["totalPrefillTokens"],
        "trainingPositions": usage.training_positions
        == expected_plan_usage["trainingSubmittedPositions"],
        "lossPresentations": sum(value["lossBearingTokenPresentations"] for value in updates)
        == expected_plan_usage["lossBearingTokenPresentations"],
        "samplerCheckpointSaves": len(updates)
        == expected_plan_usage["samplerCheckpointSaves"],
        "optimizerCheckpointSaves": len(updates)
        == expected_plan_usage["optimizerStateCheckpointSaves"],
    }
    manifest["status"] = "complete" if all(exact_usage.values()) else "failed_usage_audit"
    manifest["completedAt"] = iso8601()
    manifest["usage"] = usage.as_dict()
    manifest["operationPlanAudit"] = exact_usage
    manifest["estimatedCost"] = estimated_cost(usage, plan)
    manifest["summaries"] = summarize(scores)
    manifest["artifactDigestsSHA256"] = {
        "scores.jsonl": sha256(scores_path),
        "updates.jsonl": sha256(updates_path),
    }
    atomic_json(manifest_path, manifest)
    print(f"Tinker arms {manifest['status']}: {output}", flush=True)
    return 0 if manifest["status"] == "complete" else 2


def main() -> int:
    try:
        return run()
    except KeyboardInterrupt:
        raise SystemExit("run-phase1-tinker-prequential: interrupted; rerun the same command")
    except Exception as error:
        try:
            arguments = parse_arguments()
            manifest_path = arguments.output.expanduser().resolve() / "tinker.json"
            if manifest_path.exists():
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
        raise SystemExit(f"run-phase1-tinker-prequential: {type(error).__name__}: {error}")


if __name__ == "__main__":
    raise SystemExit(main())

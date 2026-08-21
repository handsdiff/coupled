#!/usr/bin/env python3
"""Run a bounded two-condition Inkling training and generation smoke test."""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.metadata
import json
import os
import time
import traceback
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from phase1_experiment import canonical_bytes
from phase1_inkling import (
    GENERATION_CONTRACT,
    INKLING_MODEL,
    REASONING_CONDITIONS,
    TRAINING_CONTRACT,
    InklingContractError,
    atomic_json,
    load_jsonl,
    load_semantic_inputs,
    parse_completion,
    sha256,
)
from phase1_tinker_overfit_contract import build_and_validate_sdk_datums
from phase1_training_contract import git_revision, git_worktree_dirty

# The hyphenated production runner cannot be imported normally. Keeping these
# three small contract helpers here would risk drift, so load that committed
# module directly and bind its tested implementations.
import importlib.util
import sys


SMOKE_VERSION = "phase1-inkling-smoke-v1"
SMOKE_EXAMPLES_PER_CONDITION = 8
ABSOLUTE_SMOKE_CEILING_USD = Decimal("2.00")
TRAIN_PRICE_PER_MILLION = Decimal("1.73")
PREFILL_PRICE_PER_MILLION = Decimal("0.58")
SAMPLE_PRICE_PER_MILLION = Decimal("1.44")
CHECKPOINT_RESERVE_USD = Decimal("1.00")


def load_runner_module() -> Any:
    path = Path(__file__).resolve().with_name("run-phase1-inkling-prequential.py")
    specification = importlib.util.spec_from_file_location(
        "phase1_inkling_production_runner_for_smoke", path
    )
    if specification is None or specification.loader is None:
        raise InklingContractError(f"cannot load {path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


RUNNER = load_runner_module()


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


def money(tokens: int, rate: Decimal) -> Decimal:
    return Decimal(tokens) * rate / Decimal(1_000_000)


def selected_rows(pack_path: Path) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    selected_ids: list[str] | None = None
    for condition in REASONING_CONDITIONS:
        rows = load_jsonl(pack_path / f"{condition}-packed-examples.jsonl")
        by_length = sorted(rows, key=lambda row: (len(row["inputIDs"]), row["exampleID"]))
        if selected_ids is None:
            selected_ids = [
                value["exampleID"] for value in by_length[:SMOKE_EXAMPLES_PER_CONDITION]
            ]
        by_id = {value["exampleID"]: value for value in rows}
        result[condition] = [by_id[value] for value in selected_ids]
    if any(len(value) != SMOKE_EXAMPLES_PER_CONDITION for value in result.values()):
        raise InklingContractError("smoke selection is incomplete")
    return result


def projection(rows: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    training_positions = sum(
        len(row["inputIDs"]) - 1 for values in rows.values() for row in values
    )
    generation_prefill = sum(values[0]["modelInputTokenCount"] for values in rows.values())
    generation_ceiling = sum(
        GENERATION_CONTRACT["maximumTokensByCondition"][condition]
        for condition in REASONING_CONDITIONS
    )
    costs = {
        "trainingUSD": money(training_positions, TRAIN_PRICE_PER_MILLION),
        "prefillUSD": money(generation_prefill, PREFILL_PRICE_PER_MILLION),
        "sampleCeilingUSD": money(generation_ceiling, SAMPLE_PRICE_PER_MILLION),
        "checkpointReserveUSD": CHECKPOINT_RESERVE_USD,
    }
    return {
        "trainingPositions": training_positions,
        "generationPrefillTokens": generation_prefill,
        "generationTokenCeiling": generation_ceiling,
        "costUSD": {
            **{key: str(value.quantize(Decimal("0.000001"))) for key, value in costs.items()},
            "totalIncludingReserve": str(
                sum(costs.values(), Decimal(0)).quantize(Decimal("0.000001"))
            ),
        },
    }


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--semantic-pack", required=True, type=Path)
    parser.add_argument("--inkling-pack", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--dedicated-private-project-id", required=True)
    parser.add_argument("--maximum-usd", required=True, type=Decimal)
    parser.add_argument("--confirm-personal-data-transfer", action="store_true")
    parser.add_argument("--confirm-dedicated-private-project", action="store_true")
    parser.add_argument("--execute", action="store_true")
    arguments = parser.parse_args()
    if not all(
        (
            arguments.confirm_personal_data_transfer,
            arguments.confirm_dedicated_private_project,
            arguments.execute,
        )
    ):
        parser.error("both confirmations and --execute are required")
    arguments.dedicated_private_project_id = str(
        uuid.UUID(arguments.dedicated_private_project_id)
    )
    if not 0 < arguments.maximum_usd <= ABSOLUTE_SMOKE_CEILING_USD:
        parser.error(f"--maximum-usd must be in (0, {ABSOLUTE_SMOKE_CEILING_USD}]")
    return arguments


def run() -> int:
    arguments = parse_arguments()
    project = Path(__file__).resolve().parent.parent
    corpus_path = arguments.corpus.expanduser().resolve()
    semantic_pack_path = arguments.semantic_pack.expanduser().resolve()
    pack_path = arguments.inkling_pack.expanduser().resolve()
    output = arguments.output.expanduser().resolve()
    if output.exists():
        raise InklingContractError(f"smoke output already exists: {output}")
    if git_worktree_dirty(project):
        raise InklingContractError("authenticated smoke requires a clean worktree")
    packing = json.loads((pack_path / "packing.json").read_text(encoding="utf-8"))
    rows = selected_rows(pack_path)
    semantic_by_id = load_semantic_inputs(corpus_path, semantic_pack_path)
    projected = projection(rows)
    if Decimal(projected["costUSD"]["totalIncludingReserve"]) > arguments.maximum_usd:
        raise InklingContractError("projected smoke cost exceeds the authorized ceiling")

    output.mkdir(parents=True)
    manifest_path = output / "smoke.json"
    manifest: dict[str, Any] = {
        "schemaVersion": 1,
        "smokeVersion": SMOKE_VERSION,
        "status": "initialized",
        "startedAt": iso8601(),
        "purpose": "mechanical_provider_go_no_go_not_phase1_evidence",
        "implementation": {
            "codeRevision": git_revision(project),
            "runnerSHA256": sha256(Path(__file__).resolve()),
            "productionRunnerSHA256": sha256(
                Path(__file__).resolve().with_name("run-phase1-inkling-prequential.py")
            ),
            "tinkerSDKVersion": importlib.metadata.version("tinker"),
        },
        "source": {
            "corpusSHA256": sha256(corpus_path / "corpus.json"),
            "semanticPackingSHA256": sha256(semantic_pack_path / "packing.json"),
            "inklingPackingSHA256": sha256(pack_path / "packing.json"),
            "selectedExampleIDs": [value["exampleID"] for value in rows["reasoning_off"]],
        },
        "contract": {
            "model": INKLING_MODEL,
            "reasoningConditions": REASONING_CONDITIONS,
            "training": TRAINING_CONTRACT,
            "generation": GENERATION_CONTRACT,
        },
        "authorization": {
            "maximumUSD": str(arguments.maximum_usd),
            "absoluteSmokeCeilingUSD": str(ABSOLUTE_SMOKE_CEILING_USD),
            "personalDataTransferConfirmed": True,
            "dedicatedPrivateProjectConfirmed": True,
        },
        "projection": projected,
        "results": [],
    }
    atomic_json(manifest_path, manifest)

    os.environ["TINKER_API_KEY"] = load_api_key(arguments.env_file)
    try:
        import tinker
    except ImportError as error:
        raise InklingContractError("Tinker SDK unavailable") from error
    service = tinker.ServiceClient(
        project_id=arguments.dedicated_private_project_id,
        user_metadata={"purpose": "phase1-inkling-bounded-go-no-go-smoke"},
    )
    supported = [
        value
        for value in service.get_server_capabilities().supported_models
        if value.model_name == INKLING_MODEL
    ]
    required = max(
        value["maximumSequenceTokens"] for value in packing["counts"].values()
    )
    if len(supported) != 1 or (supported[0].max_context_length or 0) < required:
        raise InklingContractError("Inkling model or required context is unavailable")

    optimizer_contract = TRAINING_CONTRACT["optimizer"]
    optimizer = tinker.AdamParams(
        learning_rate=optimizer_contract["learningRate"],
        beta1=optimizer_contract["beta1"],
        beta2=optimizer_contract["beta2"],
        eps=optimizer_contract["epsilon"],
        weight_decay=optimizer_contract["weightDecay"],
        grad_clip_norm=optimizer_contract["gradientClipNorm"],
    )
    observed_training_positions = 0
    observed_sample_tokens = 0
    observed_prefill_tokens = 0
    for condition, condition_rows in rows.items():
        manifest["inflightOperation"] = {
            "condition": condition,
            "kind": "micro_batch_forward_backward_and_optimizer_step",
            "replayAllowed": False,
        }
        atomic_json(manifest_path, manifest)
        client = service.create_lora_training_client(
            base_model=INKLING_MODEL,
            rank=TRAINING_CONTRACT["rank"],
            seed=TRAINING_CONTRACT["seed"],
            train_mlp=TRAINING_CONTRACT["trainMLP"],
            train_attn=TRAINING_CONTRACT["trainAttention"],
            train_unembed=TRAINING_CONTRACT["trainUnembedding"],
            user_metadata={"purpose": f"phase1-inkling-smoke-{condition}"},
        )
        raw_contracts = [RUNNER.datum_contract(value) for value in condition_rows]
        normalized, target_tokens, token_weight = (
            RUNNER.micro_normalized_batch_contracts(raw_contracts)
        )
        datums, _, _ = build_and_validate_sdk_datums(normalized)
        started = time.monotonic()
        result = client.forward_backward(datums, "cross_entropy").result()
        if len(result.loss_fn_outputs) != len(datums):
            raise InklingContractError("smoke training result batch size changed")
        client.optim_step(optimizer).result()
        training_latency = time.monotonic() - started
        training_positions = sum(value.length for value in raw_contracts)
        observed_training_positions += training_positions

        prefix = f"phase1-inkling-smoke-{condition}-{uuid.uuid4().hex[:8]}"
        manifest["inflightOperation"] = {
            "condition": condition,
            "kind": "save_checkpoints",
            "replayAllowed": False,
        }
        atomic_json(manifest_path, manifest)
        sampler_path = client.save_weights_for_sampler(
            f"{prefix}-sampler",
            ttl_seconds=TRAINING_CONTRACT["checkpointTTLSeconds"],
        ).result().path
        state_path = client.save_state(
            f"{prefix}-optimizer-state",
            ttl_seconds=TRAINING_CONTRACT["checkpointTTLSeconds"],
        ).result().path

        sample_row = condition_rows[0]
        prompt_ids = sample_row["inputIDs"][: sample_row["modelInputTokenCount"]]
        maximum_tokens = RUNNER.generation_ceiling(condition)
        manifest["inflightOperation"] = {
            "condition": condition,
            "kind": "checkpoint_generation",
            "maximumTokens": maximum_tokens,
            "replayAllowed": False,
        }
        atomic_json(manifest_path, manifest)
        sampler = service.create_sampling_client(model_path=sampler_path)
        generation_started = time.monotonic()
        response = sampler.sample(
            prompt=tinker.ModelInput.from_ints(tokens=prompt_ids),
            num_samples=1,
            sampling_params=tinker.SamplingParams(
                max_tokens=maximum_tokens,
                temperature=GENERATION_CONTRACT["temperature"],
                seed=GENERATION_CONTRACT["seed"],
                stop=sample_row["stopTokenIDs"],
            ),
        ).result()
        generation_latency = time.monotonic() - generation_started
        if len(response.sequences) != 1:
            raise InklingContractError("smoke returned an unexpected sample count")
        sequence = response.sequences[0]
        tokens = [int(value) for value in sequence.tokens]
        stop_reason = str(getattr(sequence.stop_reason, "value", sequence.stop_reason))
        parsed = parse_completion(
            semantic_input=semantic_by_id[sample_row["exampleID"]],
            effort=REASONING_CONDITIONS[condition],
            token_ids=tokens,
        )
        disposition, eligible = RUNNER.generation_disposition(
            stop_reason=stop_reason, parsed=parsed
        )
        result_row = {
            "condition": condition,
            "training": {
                "examples": len(condition_rows),
                "submittedPositions": training_positions,
                "lossBearingTargetTokens": target_tokens,
                "perTargetTokenWeight": token_weight,
                "normalizedWeightSum": sum(sum(value.weights) for value in normalized),
                "optimizerSteps": 1,
                "latencySeconds": training_latency,
            },
            "checkpoints": {
                "sampler": sampler_path,
                "optimizerState": state_path,
            },
            "generation": {
                "maximumTokens": maximum_tokens,
                "observedTokens": len(tokens),
                "stopReason": stop_reason,
                "disposition": disposition,
                "eligibleForEvaluation": eligible,
                "prediction": parsed["prediction"],
                "reasoningCharacters": len(parsed["reasoning"]),
                "parseStatus": parsed["status"],
                "latencySeconds": generation_latency,
            },
        }
        manifest["results"].append(result_row)
        manifest.pop("inflightOperation", None)
        atomic_json(manifest_path, manifest)
        if not eligible:
            raise InklingContractError(
                f"{condition} generation failed closed: {disposition}"
            )
        observed_sample_tokens += len(tokens)
        observed_prefill_tokens += len(prompt_ids)

    estimated_actual = (
        money(observed_training_positions, TRAIN_PRICE_PER_MILLION)
        + money(observed_prefill_tokens, PREFILL_PRICE_PER_MILLION)
        + money(observed_sample_tokens, SAMPLE_PRICE_PER_MILLION)
    )
    manifest["status"] = "passed"
    manifest["completedAt"] = iso8601()
    manifest["verdict"] = "go"
    manifest["observed"] = {
        "trainingPositions": observed_training_positions,
        "generationPrefillTokens": observed_prefill_tokens,
        "sampledTokens": observed_sample_tokens,
        "estimatedProviderCostBeforeCheckpointStorageUSD": str(
            estimated_actual.quantize(Decimal("0.000001"))
        ),
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
            manifest_path = arguments.output.expanduser().resolve() / "smoke.json"
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["status"] = "failed_closed"
                manifest["verdict"] = "no_go"
                manifest["failedAt"] = iso8601()
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

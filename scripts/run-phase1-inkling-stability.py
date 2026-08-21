#!/usr/bin/env python3
"""Run the bounded native-loss Inkling free-generation stability probe."""

from __future__ import annotations

import argparse
import datetime as dt
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

from phase1_experiment import canonical_bytes
from phase1_inkling import (
    GENERATION_CONTRACT,
    INKLING_MODEL,
    REASONING_CONDITIONS,
    TRAINING_CONTRACT,
    InklingContractError,
    atomic_json,
    load_experiment_blocks,
    load_jsonl,
    load_semantic_inputs,
    parse_completion,
    sha256,
)
from phase1_tinker_overfit_contract import build_and_validate_sdk_datums
from phase1_training_contract import git_revision, git_worktree_dirty


RUNNER_VERSION = "phase1-inkling-native-loss-stability-v1"
PLAN_VERSION = "phase1-inkling-native-loss-stability-plan-v1"


def load_production_runner() -> Any:
    path = Path(__file__).resolve().with_name("run-phase1-inkling-prequential.py")
    specification = importlib.util.spec_from_file_location(
        "phase1_inkling_runner_for_stability", path
    )
    if specification is None or specification.loader is None:
        raise InklingContractError("cannot load Inkling production runner")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


PRODUCTION = load_production_runner()


def iso8601() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    with path.open("ab") as handle:
        handle.write(canonical_bytes(value))
        handle.flush()
        os.fsync(handle.fileno())


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
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    if not values.get("TINKER_API_KEY"):
        raise InklingContractError("env file does not define TINKER_API_KEY")
    return values["TINKER_API_KEY"]


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--semantic-pack", required=True, type=Path)
    parser.add_argument("--inkling-pack", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--dedicated-private-project-id", required=True)
    parser.add_argument("--maximum-usd", required=True, type=Decimal)
    parser.add_argument("--confirm-personal-data-transfer", action="store_true")
    parser.add_argument("--confirm-dedicated-private-project", action="store_true")
    parser.add_argument("--confirm-current-prices", action="store_true")
    parser.add_argument("--execute", action="store_true")
    arguments = parser.parse_args()
    for enabled, flag in (
        (arguments.confirm_personal_data_transfer, "--confirm-personal-data-transfer"),
        (
            arguments.confirm_dedicated_private_project,
            "--confirm-dedicated-private-project",
        ),
        (arguments.confirm_current_prices, "--confirm-current-prices"),
        (arguments.execute, "--execute"),
    ):
        if not enabled:
            parser.error(f"{flag} is required")
    arguments.dedicated_private_project_id = str(
        uuid.UUID(arguments.dedicated_private_project_id)
    )
    return arguments


def validate_plan(arguments: argparse.Namespace, project: Path) -> dict[str, Any]:
    plan_path = arguments.plan.expanduser().resolve()
    if sha256(plan_path) != arguments.plan_sha256:
        raise InklingContractError("stability plan digest differs")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if not (
        plan.get("planVersion") == PLAN_VERSION
        and plan.get("status") == "review_only_not_authorization"
        and plan["provider"]["model"] == INKLING_MODEL
        and plan["provider"]["reasoningConditions"] == {"reasoning_off": 0.0}
        and plan["provider"]["trainingContract"] == TRAINING_CONTRACT
        and plan["provider"]["generationContract"] == GENERATION_CONTRACT
        and plan["protocol"]["targetLikelihoodCalls"] == 0
        and plan["protocol"]["frozenDuplicateArm"] is False
        and plan["protocol"]["reasoningOnArm"] is False
    ):
        raise InklingContractError("stability plan contract differs")
    if Decimal(plan["pricing"]["hardCeilingUSD"]) != arguments.maximum_usd:
        raise InklingContractError("authorized ceiling differs from stability plan")
    if plan["provider"]["projectID"] != arguments.dedicated_private_project_id:
        raise InklingContractError("private project differs from stability plan")
    if plan["implementation"]["codeRevision"] != git_revision(project):
        raise InklingContractError("code revision differs from stability plan")
    for relative, expected in plan["implementation"]["fileDigestsSHA256"].items():
        if sha256(project / relative) != expected:
            raise InklingContractError(f"runtime file differs: {relative}")
    return plan


def probe_accepted(*, stop_reason: str, parsed: dict[str, Any]) -> bool:
    return bool(
        stop_reason == "stop"
        and parsed.get("status") == "parsed"
        and parsed.get("prediction")
    )


def run() -> int:
    arguments = parse_arguments()
    project = Path(__file__).resolve().parent.parent
    if git_worktree_dirty(project):
        raise InklingContractError("stability execution requires a clean worktree")
    plan = validate_plan(arguments, project)
    corpus_path = arguments.corpus.expanduser().resolve()
    semantic_pack = arguments.semantic_pack.expanduser().resolve()
    pack_path = arguments.inkling_pack.expanduser().resolve()
    output = arguments.output.expanduser().resolve()
    if output.exists():
        raise InklingContractError(f"stability output already exists: {output}")
    for path, expected in (
        (corpus_path / "corpus.json", plan["source"]["corpusSHA256"]),
        (semantic_pack / "packing.json", plan["source"]["semanticPackingSHA256"]),
        (pack_path / "packing.json", plan["source"]["inklingPackingSHA256"]),
        (
            pack_path / "reasoning_off-packed-examples.jsonl",
            plan["source"]["inklingRowsSHA256"],
        ),
    ):
        if sha256(path) != expected:
            raise InklingContractError(f"source artifact differs: {path}")

    rows = {
        value["exampleID"]: value
        for value in load_jsonl(pack_path / "reasoning_off-packed-examples.jsonl")
    }
    semantic = load_semantic_inputs(corpus_path, semantic_pack)
    examples = {
        value["exampleID"]: value
        for value in load_jsonl(corpus_path / "examples.jsonl")
    }
    applications = {
        example_id: json.loads(value["query"])["destination"]["appName"]
        for example_id, value in examples.items()
    }
    blocks = {
        value["blockID"]: value for value in load_experiment_blocks(corpus_path)
    }
    probe_ids = plan["protocol"]["probeExampleIDs"]
    output.mkdir(parents=True)
    manifest_path = output / "stability.json"
    samples_path = output / "samples.jsonl"
    updates_path = output / "updates.jsonl"
    manifest: dict[str, Any] = {
        "schemaVersion": 1,
        "runnerVersion": RUNNER_VERSION,
        "status": "initialized",
        "startedAt": iso8601(),
        "planSHA256": arguments.plan_sha256,
        "authorization": {
            "maximumUSD": str(arguments.maximum_usd),
            "personalDataTransferConfirmed": True,
            "dedicatedPrivateProjectConfirmed": True,
            "currentPricesConfirmed": True,
        },
        "provider": {
            "model": INKLING_MODEL,
            "projectID": arguments.dedicated_private_project_id,
            "trainingContract": TRAINING_CONTRACT,
            "generationContract": GENERATION_CONTRACT,
        },
        "counts": {"completedStages": 0, "completedUpdates": 0, "samples": 0},
        "stages": [],
        "updates": [],
    }
    atomic_json(manifest_path, manifest)

    os.environ["TINKER_API_KEY"] = load_api_key(arguments.env_file)
    try:
        import tinker
    except ImportError as error:
        raise InklingContractError("Tinker SDK unavailable") from error
    service = tinker.ServiceClient(
        project_id=arguments.dedicated_private_project_id,
        user_metadata={
            "purpose": "phase1-inkling-native-loss-stability",
            "plan_sha256": arguments.plan_sha256,
        },
    )
    supported = [
        value
        for value in service.get_server_capabilities().supported_models
        if value.model_name == INKLING_MODEL
    ]
    if len(supported) != 1 or (supported[0].max_context_length or 0) < max(
        len(value["inputIDs"]) for value in rows.values()
    ):
        raise InklingContractError("Inkling model/context unavailable")
    base_sampler = service.create_sampling_client(base_model=INKLING_MODEL)

    def sample_stage(*, stage: int, trained: int, sampler: Any) -> bool:
        valid = 0
        for example_id in probe_ids:
            row = rows[example_id]
            prompt = row["inputIDs"][: row["modelInputTokenCount"]]
            manifest["inflightOperation"] = {
                "kind": "free_generation_probe",
                "stage": stage,
                "exampleID": example_id,
                "replayAllowedAutomatically": False,
            }
            atomic_json(manifest_path, manifest)
            started = time.monotonic()
            response = sampler.sample(
                prompt=tinker.ModelInput.from_ints(tokens=prompt),
                num_samples=1,
                sampling_params=tinker.SamplingParams(
                    max_tokens=GENERATION_CONTRACT["maximumTokensByCondition"][
                        "reasoning_off"
                    ],
                    temperature=GENERATION_CONTRACT["temperature"],
                    seed=GENERATION_CONTRACT["seed"],
                    stop=row["stopTokenIDs"],
                ),
            ).result()
            if len(response.sequences) != 1:
                raise InklingContractError("probe returned an unexpected sample count")
            sequence = response.sequences[0]
            tokens = [int(value) for value in sequence.tokens]
            stop_reason = str(getattr(sequence.stop_reason, "value", sequence.stop_reason))
            parsed = parse_completion(
                semantic_input=semantic[example_id],
                effort=0.0,
                token_ids=tokens,
            )
            accepted = probe_accepted(stop_reason=stop_reason, parsed=parsed)
            valid += int(accepted)
            append_jsonl(
                samples_path,
                {
                    "stage": stage,
                    "trainedExamples": trained,
                    "exampleID": example_id,
                    "application": applications[example_id],
                    "accepted": accepted,
                    "stopReason": stop_reason,
                    "observedTokens": len(tokens),
                    "prediction": parsed.get("prediction", ""),
                    "parseStatus": parsed.get("status"),
                    "rawDecoded": parsed.get("rawDecoded"),
                    "latencySeconds": time.monotonic() - started,
                    "completedAt": iso8601(),
                },
            )
            manifest.pop("inflightOperation", None)
            manifest["counts"]["samples"] += 1
            atomic_json(manifest_path, manifest)
        stage_result = {
            "stage": stage,
            "trainedExamples": trained,
            "probes": len(probe_ids),
            "accepted": valid,
            "status": "passed" if valid == len(probe_ids) else "failed",
        }
        manifest["stages"].append(stage_result)
        manifest["counts"]["completedStages"] += 1
        atomic_json(manifest_path, manifest)
        return valid == len(probe_ids)

    manifest["status"] = "running"
    manifest["provider"]["sessionID"] = service.holder.get_session_id()
    atomic_json(manifest_path, manifest)
    if not sample_stage(stage=0, trained=0, sampler=base_sampler):
        raise InklingContractError("base Inkling failed the free-generation gate")

    # Do not create a training client until the frozen model proves that this
    # renderer/parser path can produce valid free generations. This keeps a
    # renderer failure from opening a paid training run at all.
    client = service.create_lora_training_client(
        base_model=INKLING_MODEL,
        rank=TRAINING_CONTRACT["rank"],
        seed=TRAINING_CONTRACT["seed"],
        train_mlp=TRAINING_CONTRACT["trainMLP"],
        train_attn=TRAINING_CONTRACT["trainAttention"],
        train_unembed=TRAINING_CONTRACT["trainUnembedding"],
        user_metadata={"purpose": "phase1-inkling-native-loss-stability"},
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

    trained = 0
    for update_ordinal, block_id in enumerate(
        plan["protocol"]["trainingBlockIDs"], 1
    ):
        block = blocks[block_id]
        order = PRODUCTION.deterministic_order(block["exampleIDs"], update_ordinal)
        batches = PRODUCTION.optimizer_batches(order)
        update_started = time.monotonic()
        submitted = 0
        loss_tokens = 0
        for batch_position, batch_ids in enumerate(batches, 1):
            raw_contracts = [PRODUCTION.datum_contract(rows[value]) for value in batch_ids]
            normalized, batch_loss_tokens, token_weight = (
                PRODUCTION.micro_normalized_batch_contracts(raw_contracts)
            )
            datums, _, _ = build_and_validate_sdk_datums(normalized)
            manifest["inflightOperation"] = {
                "kind": "training_batch",
                "updateOrdinal": update_ordinal,
                "batchPosition": batch_position,
                "exampleIDs": batch_ids,
                "replayAllowedAutomatically": False,
            }
            atomic_json(manifest_path, manifest)
            client.forward_backward(datums, "cross_entropy").result()
            client.optim_step(optimizer).result()
            submitted += sum(value.length for value in raw_contracts)
            loss_tokens += batch_loss_tokens
            manifest.pop("inflightOperation", None)
            atomic_json(manifest_path, manifest)

        prefix = f"phase1-inkling-native-loss-stability-{update_ordinal:02d}"
        manifest["inflightOperation"] = {
            "kind": "save_checkpoints",
            "updateOrdinal": update_ordinal,
            "replayAllowedAutomatically": False,
        }
        atomic_json(manifest_path, manifest)
        sampler_path = client.save_weights_for_sampler(
            f"{prefix}-sampler", ttl_seconds=TRAINING_CONTRACT["checkpointTTLSeconds"]
        ).result().path
        state_path = client.save_state(
            f"{prefix}-optimizer-state",
            ttl_seconds=TRAINING_CONTRACT["checkpointTTLSeconds"],
        ).result().path
        trained += len(order)
        update = {
            "updateOrdinal": update_ordinal,
            "afterBlockID": block_id,
            "trainedExamplesThisUpdate": len(order),
            "cumulativeTrainedExamples": trained,
            "optimizerSteps": len(batches),
            "submittedPositions": submitted,
            "nativeLossTokenPresentations": loss_tokens,
            "samplerCheckpointPath": sampler_path,
            "optimizerStatePath": state_path,
            "latencySeconds": time.monotonic() - update_started,
            "completedAt": iso8601(),
        }
        append_jsonl(updates_path, update)
        manifest["updates"].append(update)
        manifest["counts"]["completedUpdates"] += 1
        manifest.pop("inflightOperation", None)
        atomic_json(manifest_path, manifest)
        sampler = service.create_sampling_client(model_path=sampler_path)
        if not sample_stage(stage=update_ordinal, trained=trained, sampler=sampler):
            manifest["status"] = "complete_no_go"
            manifest["verdict"] = "native_loss_did_not_preserve_free_generation"
            manifest["completedAt"] = iso8601()
            atomic_json(manifest_path, manifest)
            print(json.dumps(manifest, indent=2))
            return 2

    manifest["status"] = "complete_go"
    manifest["verdict"] = "native_loss_preserved_free_generation_through_prior_collapse_point"
    manifest["completedAt"] = iso8601()
    manifest["artifactDigestsSHA256"] = {
        "samples.jsonl": sha256(samples_path),
        "updates.jsonl": sha256(updates_path),
    }
    atomic_json(manifest_path, manifest)
    print(json.dumps(manifest, indent=2))
    return 0


def main() -> int:
    try:
        return run()
    except BaseException as error:
        try:
            arguments = parse_arguments()
            manifest_path = arguments.output.expanduser().resolve() / "stability.json"
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["status"] = "failed_closed"
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

#!/usr/bin/env python3
"""Run the bounded native-loss Inkling free-generation stability probe."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
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

from phase1_experiment import canonical_bytes, target_text
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
from phase1_prediction_metrics import score_prediction


RUNNER_VERSION = "phase1-inkling-native-loss-prequential-v2"
PLAN_VERSION = "phase1-inkling-native-loss-prequential-plan-v2"
RUNNER_LOCK_VERSION = "exclusive-adjacent-flock-v1"


_OUTPUT_LOCK: tuple[Path, Any] | None = None


def acquire_output_lock(output: Path) -> Path:
    """Hold one process-wide nonblocking lock for an experiment output."""
    global _OUTPUT_LOCK
    lock_path = output.parent / f".{output.name}.runner.lock"
    if _OUTPUT_LOCK is not None and _OUTPUT_LOCK[0] == lock_path:
        return lock_path
    if _OUTPUT_LOCK is not None:
        _OUTPUT_LOCK[1].close()
        _OUTPUT_LOCK = None
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        handle.close()
        raise InklingContractError(
            f"another Inkling runner already owns output lock: {lock_path}"
        ) from error
    _OUTPUT_LOCK = (lock_path, handle)
    return lock_path


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
    parser.add_argument(
        "--authorize-continue-after-validity-review",
        action="append",
        default=[],
        metavar="BLOCK_ID",
    )
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
        and plan["protocol"]["trainingExampleCountsAfterStage"]
        == [0, 50, 100, 150, 200]
        and plan["protocol"]["evaluationExampleCountsAfterUpdate"]
        == [50, 50, 50, 24]
        and plan["protocol"]["probeSource"]
        == "first_training_block_only_never_scored"
        and plan["protocol"]["minimumAutomaticValidityRate"] == "0.98"
        and plan["protocol"]["invalidGenerationScoring"]
        == "zero_in_all_174_holistic_and_deterministic_comparisons"
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


def cost_string(tokens: int, rate: str) -> str:
    return str(
        (Decimal(tokens) * Decimal(rate) / Decimal(1_000_000)).quantize(
            Decimal("0.000001")
        )
    )


def abandoned_attempt_count(
    manifest: dict[str, Any], kind: str, identity: dict[str, Any]
) -> int:
    return sum(
        value.get("kind") == kind and value.get("identity") == identity
        for value in manifest.get("abandonedAttempts", [])
    )


def recover_interrupted_manifest(
    *,
    manifest: dict[str, Any],
    sentinels: list[dict[str, Any]],
    scores: list[dict[str, Any]],
    updates: list[dict[str, Any]],
    plan: dict[str, Any],
    maximum_usd: Decimal,
) -> bool:
    active = manifest.get("activeUpdate")
    inflight = manifest.get("inflightOperation")
    if active:
        kind = "training_block_restart"
        identity = {"updateOrdinal": active["updateOrdinal"]}
        maximum_cost = Decimal(active["maximumReplayCostUSD"])
        committed = any(
            value["updateOrdinal"] == active["updateOrdinal"] for value in updates
        )
    elif inflight and inflight.get("kind") == "free_generation":
        kind = "generation_retry"
        identity = {
            "purpose": inflight["purpose"],
            "stage": inflight["stage"],
            "exampleID": inflight["exampleID"],
        }
        maximum_cost = Decimal(inflight["maximumReplayCostUSD"])
        records = sentinels if identity["purpose"] == "base_renderer_gate" else scores
        committed = any(
            value["purpose"] == identity["purpose"]
            and value["stage"] == identity["stage"]
            and value["exampleID"] == identity["exampleID"]
            for value in records
        )
    elif inflight:
        raise InklingContractError(
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
        contract = plan["protocol"]["recoveryContract"]
        limit_key = (
            "maximumAutomaticGenerationRetriesTotal"
            if kind == "generation_retry"
            else "maximumAutomaticTrainingBlockRestartsTotal"
        )
        attempts = manifest.setdefault("abandonedAttempts", [])
        if sum(value["kind"] == kind for value in attempts) >= contract[limit_key]:
            raise InklingContractError(f"automatic {kind} allowance exhausted")
        if any(
            value["kind"] == kind and value["identity"] == identity
            for value in attempts
        ):
            raise InklingContractError(f"interrupted {kind} was already retried")
        projected = Decimal(
            plan["pricing"]["projectedUSD"]["totalBeforeRecoveryReserve"]
        ) + maximum_cost + sum(
            Decimal(value["maximumEstimatedProviderCostUSD"])
            for value in attempts
        )
        if projected > maximum_usd:
            raise InklingContractError("recovery would exceed the hard cost ceiling")
        attempts.append(
            {
                "attemptID": str(uuid.uuid4()),
                "kind": kind,
                "identity": identity,
                "abandonedAt": iso8601(),
                "maximumEstimatedProviderCostUSD": str(maximum_cost),
                "projectedRunMaximumAfterRecoveryUSD": str(projected),
                "activeUpdate": active,
                "inflightOperation": inflight,
                "resolution": contract[
                    "generationRecovery"
                    if kind == "generation_retry"
                    else "trainingRecovery"
                ],
            }
        )
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
    acquire_output_lock(output)
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
    expected_score_ids = [
        example_id
        for block_id in plan["protocol"]["evaluationBlockIDsAfterUpdate"]
        for example_id in blocks[block_id]["exampleIDs"]
    ]
    manifest_path = output / "stability.json"
    sentinels_path = output / "base-sentinels.jsonl"
    scores_path = output / "scores.jsonl"
    updates_path = output / "updates.jsonl"
    batches_path = output / "training-batches.jsonl"

    if not output.exists():
        output.mkdir(parents=True)
        manifest: dict[str, Any] = {
            "schemaVersion": 2,
            "runnerVersion": RUNNER_VERSION,
            "status": "initialized",
            "startedAt": iso8601(),
            "planSHA256": arguments.plan_sha256,
            "implementation": {
                "codeRevision": git_revision(project),
                "fileDigestsSHA256": plan["implementation"]["fileDigestsSHA256"],
            },
            "source": plan["source"],
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
                "recoveryContract": plan["protocol"]["recoveryContract"],
            },
            "counts": {},
            "stages": [],
            "updates": [],
            "abandonedAttempts": [],
            "validityReviewAuthorizations": [],
        }
        atomic_json(manifest_path, manifest)
        sentinels: list[dict[str, Any]] = []
        scores: list[dict[str, Any]] = []
        updates: list[dict[str, Any]] = []
        training_batches: list[dict[str, Any]] = []
    else:
        if not manifest_path.exists():
            raise InklingContractError("existing output lacks a stability manifest")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not (
            manifest.get("planSHA256") == arguments.plan_sha256
            and manifest.get("implementation", {}).get("codeRevision")
            == git_revision(project)
            and manifest.get("implementation", {}).get("fileDigestsSHA256")
            == plan["implementation"]["fileDigestsSHA256"]
        ):
            raise InklingContractError("resume implementation or lineage changed")
        sentinels = load_jsonl(sentinels_path) if sentinels_path.exists() else []
        scores = load_jsonl(scores_path) if scores_path.exists() else []
        updates = load_jsonl(updates_path) if updates_path.exists() else []
        training_batches = load_jsonl(batches_path) if batches_path.exists() else []
        if manifest.get("status", "").startswith("complete"):
            return 0
        if recover_interrupted_manifest(
            manifest=manifest,
            sentinels=sentinels,
            scores=scores,
            updates=updates,
            plan=plan,
            maximum_usd=arguments.maximum_usd,
        ):
            atomic_json(manifest_path, manifest)

    if [value["exampleID"] for value in sentinels] != probe_ids[: len(sentinels)]:
        raise InklingContractError("base probes are not an ordered protocol prefix")
    if [value["exampleID"] for value in scores] != expected_score_ids[: len(scores)]:
        raise InklingContractError("evaluation scores are not an ordered protocol prefix")
    if [value["updateOrdinal"] for value in updates] != list(
        range(1, len(updates) + 1)
    ):
        raise InklingContractError("updates are not an ordered protocol prefix")

    def refresh_manifest() -> None:
        stages: list[dict[str, Any]] = []
        if len(sentinels) == len(probe_ids):
            accepted = sum(value["accepted"] for value in sentinels)
            stages.append(
                {
                    "stage": 0,
                    "trainedExamples": 0,
                    "purpose": "base_renderer_gate",
                    "samples": len(sentinels),
                    "accepted": accepted,
                    "validityRate": accepted / len(sentinels),
                    "status": "passed" if accepted == len(sentinels) else "failed",
                }
            )
        position = 0
        threshold = float(plan["protocol"]["minimumAutomaticValidityRate"])
        for stage, block_id in enumerate(
            plan["protocol"]["evaluationBlockIDsAfterUpdate"], 1
        ):
            count = len(blocks[block_id]["exampleIDs"])
            stage_rows = scores[position : position + count]
            if len(stage_rows) != count:
                break
            accepted = sum(value["accepted"] for value in stage_rows)
            validity = accepted / count
            stages.append(
                {
                    "stage": stage,
                    "trainedExamples": stage * 50,
                    "purpose": "personalized_prequential_evaluation",
                    "blockID": block_id,
                    "samples": count,
                    "accepted": accepted,
                    "invalid": count - accepted,
                    "validityRate": validity,
                    "automaticContinuationEligible": validity >= threshold,
                    "status": "intact" if validity >= threshold else "material_deterioration",
                }
            )
            position += count
        manifest["stages"] = stages
        manifest["updates"] = updates
        manifest["counts"] = {
            "completedStages": len(stages),
            "completedUpdates": len(updates),
            "baseProbes": len(sentinels),
            "evaluationScores": len(scores),
            "samples": len(sentinels) + len(scores),
            "trainingBatchRecords": len(training_batches),
        }

    refresh_manifest()
    atomic_json(manifest_path, manifest)

    os.environ["TINKER_API_KEY"] = load_api_key(arguments.env_file)
    try:
        import tinker
    except ImportError as error:
        raise InklingContractError("Tinker SDK unavailable") from error
    service = tinker.ServiceClient(
        project_id=arguments.dedicated_private_project_id,
        user_metadata={
            "purpose": "phase1-inkling-native-loss-repair-and-prequential-comparison",
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
    manifest["status"] = "running"
    manifest["provider"]["sessionID"] = service.holder.get_session_id()
    atomic_json(manifest_path, manifest)

    def sample_examples(
        *,
        stage: int,
        trained: int,
        sampler: Any,
        example_ids: list[str],
        purpose: str,
        block_id: str | None,
        checkpoint_id: str | None,
        destination: Path,
        records: list[dict[str, Any]],
    ) -> None:
        completed = len(records) if purpose == "base_renderer_gate" else sum(
            len(blocks[value]["exampleIDs"])
            for value in plan["protocol"]["evaluationBlockIDsAfterUpdate"][: stage - 1]
        )
        existing_stage = (
            records[completed : completed + len(example_ids)]
            if purpose != "base_renderer_gate"
            else records
        )
        existing_ids = [value["exampleID"] for value in existing_stage]
        if existing_ids != example_ids[: len(existing_ids)]:
            raise InklingContractError("generation stage is not an ordered prefix")
        for example_id in example_ids[len(existing_ids) :]:
            row = rows[example_id]
            prompt = row["inputIDs"][: row["modelInputTokenCount"]]
            identity = {"purpose": purpose, "stage": stage, "exampleID": example_id}
            maximum_cost = Decimal(
                cost_string(len(prompt), plan["pricing"]["perMillionUSD"]["prefill"])
            ) + Decimal(
                cost_string(
                    GENERATION_CONTRACT["maximumTokensByCondition"]["reasoning_off"],
                    plan["pricing"]["perMillionUSD"]["sample"],
                )
            )
            attempt = 1 + abandoned_attempt_count(
                manifest, "generation_retry", identity
            )
            manifest["inflightOperation"] = {
                "kind": "free_generation",
                **identity,
                "attemptOrdinal": attempt,
                "maximumReplayCostUSD": str(maximum_cost),
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
                raise InklingContractError("generation returned an unexpected sample count")
            sequence = response.sequences[0]
            tokens = [int(value) for value in sequence.tokens]
            stop_reason = str(getattr(sequence.stop_reason, "value", sequence.stop_reason))
            parsed = parse_completion(
                semantic_input=semantic[example_id], effort=0.0, token_ids=tokens
            )
            accepted = probe_accepted(stop_reason=stop_reason, parsed=parsed)
            expected = target_text(examples[example_id]["target"])
            prediction = parsed.get("prediction", "")
            actual_cost = Decimal(
                cost_string(len(prompt), plan["pricing"]["perMillionUSD"]["prefill"])
            ) + Decimal(
                cost_string(len(tokens), plan["pricing"]["perMillionUSD"]["sample"])
            )
            record = {
                "schemaVersion": 2,
                "runnerVersion": RUNNER_VERSION,
                "purpose": purpose,
                "stage": stage,
                "trainedExamples": trained,
                "blockID": block_id,
                "arm": (
                    "frozen_inkling_small_reasoning_off"
                    if trained == 0
                    else "personalized_inkling_small_reasoning_off"
                ),
                "condition": "reasoning_off",
                "effort": 0.0,
                "checkpointID": checkpoint_id,
                "attemptOrdinal": attempt,
                "exampleID": example_id,
                "targetEventID": examples[example_id]["targetEventID"],
                "application": applications[example_id],
                "accepted": accepted,
                "stopReason": stop_reason,
                "predictionTokenIDs": tokens,
                "prediction": prediction,
                "target": expected,
                "pasteActionCount": row["pasteActionCount"],
                "semanticModelInputSHA256": row["semanticModelInputSHA256"],
                "modelInputTokenCount": row["modelInputTokenCount"],
                "fullSequenceTokenCount": len(row["inputIDs"]),
                "responseParse": parsed,
                "generationDisposition": (
                    "accepted"
                    if accepted
                    else (
                        GENERATION_CONTRACT["tokenCapWithoutValidFinalDisposition"]
                        if stop_reason == "length"
                        else GENERATION_CONTRACT["missingFinalDisposition"]
                    )
                ),
                "generationEligibleForEvaluation": accepted,
                "comparisonScorePolicy": "invalid_generation_scores_zero",
                "predictionMetrics": score_prediction(
                    expected,
                    prediction if accepted else "",
                    target_paste_actions=row["pasteActionCount"],
                ),
                "validOnlyPredictionMetrics": (
                    score_prediction(
                        expected,
                        prediction,
                        target_paste_actions=row["pasteActionCount"],
                    )
                    if accepted
                    else None
                ),
                "generationTemperature": GENERATION_CONTRACT["temperature"],
                "generationSeed": GENERATION_CONTRACT["seed"],
                "generationTokenCeiling": GENERATION_CONTRACT[
                    "maximumTokensByCondition"
                ]["reasoning_off"],
                "generationLatencySeconds": time.monotonic() - started,
                "estimatedProviderCostUSDAtFrozenRates": str(actual_cost),
                "completedAt": iso8601(),
            }
            append_jsonl(destination, record)
            records.append(record)
            manifest.pop("inflightOperation", None)
            refresh_manifest()
            atomic_json(manifest_path, manifest)

    sample_examples(
        stage=0,
        trained=0,
        sampler=base_sampler,
        example_ids=probe_ids,
        purpose="base_renderer_gate",
        block_id=None,
        checkpoint_id=None,
        destination=sentinels_path,
        records=sentinels,
    )
    refresh_manifest()
    if not all(value["accepted"] for value in sentinels):
        manifest["status"] = "complete_no_go"
        manifest["verdict"] = "base_renderer_gate_failed_before_training"
        manifest["completedAt"] = iso8601()
        atomic_json(manifest_path, manifest)
        return 2

    latest_update = updates[-1] if updates else None
    if latest_update:
        client = service.create_training_client_from_state_with_optimizer(
            latest_update["optimizerStatePath"],
            base_model=INKLING_MODEL,
            user_metadata={"purpose": "phase1-inkling-native-loss-resume"},
        )
    else:
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

    authorized_blocks = set(arguments.authorize_continue_after_validity_review)
    for update_ordinal, block_id in enumerate(plan["protocol"]["trainingBlockIDs"], 1):
        if update_ordinal > 1:
            prior_stage = manifest["stages"][update_ordinal - 1]
            if not prior_stage["automaticContinuationEligible"]:
                prior_block = prior_stage["blockID"]
                previously_authorized = any(
                    value["blockID"] == prior_block
                    for value in manifest["validityReviewAuthorizations"]
                )
                if prior_block in authorized_blocks and not previously_authorized:
                    manifest["validityReviewAuthorizations"].append(
                        {
                            "blockID": prior_block,
                            "validityRate": prior_stage["validityRate"],
                            "authorizedAt": iso8601(),
                        }
                    )
                    previously_authorized = True
                    atomic_json(manifest_path, manifest)
                if not previously_authorized:
                    manifest["status"] = "paused_for_validity_review"
                    manifest["pause"] = prior_stage
                    atomic_json(manifest_path, manifest)
                    return 3

        if update_ordinal <= len(updates):
            update = updates[update_ordinal - 1]
        else:
            block = blocks[block_id]
            order = PRODUCTION.deterministic_order(block["exampleIDs"], update_ordinal)
            batches = PRODUCTION.optimizer_batches(order)
            update_started = time.monotonic()
            submitted = sum(
                PRODUCTION.datum_contract(rows[value]).length for value in order
            )
            identity = {"updateOrdinal": update_ordinal}
            attempt = 1 + abandoned_attempt_count(
                manifest, "training_block_restart", identity
            )
            parent = updates[-1] if updates else None
            manifest["activeUpdate"] = {
                "updateOrdinal": update_ordinal,
                "afterBlockID": block_id,
                "parentOptimizerStatePath": (
                    None if parent is None else parent["optimizerStatePath"]
                ),
                "attemptOrdinal": attempt,
                "totalSteps": len(batches),
                "completedStepsNotCheckpointed": 0,
                "maximumReplayCostUSD": cost_string(
                    submitted, plan["pricing"]["perMillionUSD"]["training"]
                ),
            }
            atomic_json(manifest_path, manifest)
            update_nll = 0.0
            update_loss_tokens = 0
            for batch_position, batch_ids in enumerate(batches, 1):
                raw_contracts = [
                    PRODUCTION.datum_contract(rows[value]) for value in batch_ids
                ]
                normalized, batch_loss_tokens, token_weight = (
                    PRODUCTION.micro_normalized_batch_contracts(raw_contracts)
                )
                datums, _, _ = build_and_validate_sdk_datums(normalized)
                manifest["inflightOperation"] = {
                    "kind": "training_batch",
                    "updateOrdinal": update_ordinal,
                    "batchPosition": batch_position,
                    "exampleIDs": batch_ids,
                    "replayPolicy": "restart_whole_block_from_parent_checkpoint",
                }
                atomic_json(manifest_path, manifest)
                result = client.forward_backward(datums, "cross_entropy").result()
                if len(result.loss_fn_outputs) != len(raw_contracts):
                    raise InklingContractError("training result batch size changed")
                per_example = []
                batch_nll = 0.0
                for raw_contract, item in zip(
                    raw_contracts, result.loss_fn_outputs, strict=True
                ):
                    logprobs = item["logprobs"].tolist()
                    if len(logprobs) != raw_contract.length:
                        raise InklingContractError("training logprob length changed")
                    nll = -sum(
                        float(logprob)
                        for logprob, weight in zip(
                            logprobs, raw_contract.weights, strict=True
                        )
                        if weight
                    )
                    batch_nll += nll
                    per_example.append(
                        {
                            "exampleID": raw_contract.example_id,
                            "weightedNLLSum": nll,
                            "nativeLossTokenCount": raw_contract.weighted_positions,
                            "meanNLL": nll / raw_contract.weighted_positions,
                        }
                    )
                client.optim_step(optimizer).result()
                batch_record = {
                    "updateOrdinal": update_ordinal,
                    "trainingAttemptOrdinal": attempt,
                    "batchPosition": batch_position,
                    "exampleIDs": batch_ids,
                    "submittedPositions": sum(value.length for value in raw_contracts),
                    "nativeLossTokenCount": batch_loss_tokens,
                    "perNativeLossTokenWeight": token_weight,
                    "weightedNLLSum": batch_nll,
                    "meanPreUpdateNLL": batch_nll / batch_loss_tokens,
                    "perExample": per_example,
                    "completedAt": iso8601(),
                }
                append_jsonl(batches_path, batch_record)
                training_batches.append(batch_record)
                update_nll += batch_nll
                update_loss_tokens += batch_loss_tokens
                manifest.pop("inflightOperation", None)
                manifest["activeUpdate"]["completedStepsNotCheckpointed"] = batch_position
                refresh_manifest()
                atomic_json(manifest_path, manifest)

            prefix = (
                f"phase1-inkling-native-loss-stability-{update_ordinal:02d}-"
                f"attempt-{attempt:02d}"
            )
            manifest["inflightOperation"] = {
                "kind": "save_checkpoints",
                "updateOrdinal": update_ordinal,
                "replayPolicy": "restart_whole_block_from_parent_checkpoint",
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
            update = {
                "schemaVersion": 2,
                "runnerVersion": RUNNER_VERSION,
                "updateOrdinal": update_ordinal,
                "trainingAttemptOrdinal": attempt,
                "afterBlockID": block_id,
                "parentOptimizerStatePath": (
                    None if parent is None else parent["optimizerStatePath"]
                ),
                "samplerCheckpointPath": sampler_path,
                "optimizerStatePath": state_path,
                "trainedExamplesThisUpdate": len(order),
                "cumulativeTrainedExamples": update_ordinal * 50,
                "optimizerSteps": len(batches),
                "submittedPositions": submitted,
                "nativeLossTokenPresentations": update_loss_tokens,
                "weightedNLLSum": update_nll,
                "meanPreUpdateNLL": update_nll / update_loss_tokens,
                "estimatedTrainingCostUSDAtFrozenRate": cost_string(
                    submitted, plan["pricing"]["perMillionUSD"]["training"]
                ),
                "latencySeconds": time.monotonic() - update_started,
                "completedAt": iso8601(),
            }
            append_jsonl(updates_path, update)
            updates.append(update)
            manifest.pop("inflightOperation", None)
            manifest.pop("activeUpdate", None)
            refresh_manifest()
            atomic_json(manifest_path, manifest)

        evaluation_block_id = plan["protocol"]["evaluationBlockIDsAfterUpdate"][
            update_ordinal - 1
        ]
        sampler = service.create_sampling_client(
            model_path=update["samplerCheckpointPath"]
        )
        sample_examples(
            stage=update_ordinal,
            trained=update_ordinal * 50,
            sampler=sampler,
            example_ids=blocks[evaluation_block_id]["exampleIDs"],
            purpose="personalized_prequential_evaluation",
            block_id=evaluation_block_id,
            checkpoint_id=update["samplerCheckpointPath"],
            destination=scores_path,
            records=scores,
        )
        refresh_manifest()
        atomic_json(manifest_path, manifest)

    if len(scores) != len(expected_score_ids) or len(updates) != 4:
        raise InklingContractError("Inkling protocol ended incomplete")
    material = [
        value
        for value in manifest["stages"]
        if value.get("status") == "material_deterioration"
    ]
    abandoned_maximum = sum(
        (
            Decimal(value["maximumEstimatedProviderCostUSD"])
            for value in manifest.get("abandonedAttempts", [])
        ),
        Decimal(0),
    )
    manifest["status"] = (
        "complete_with_generation_deterioration" if material else "complete_go"
    )
    manifest["verdict"] = (
        "native_loss_run_complete_with_recorded_structural_failures"
        if material
        else "native_loss_preserved_structurally_valid_free_generation"
    )
    manifest["completedAt"] = iso8601()
    manifest["estimatedCost"] = {
        "completedTrainingAtFrozenRate": str(
            sum(
                Decimal(value["estimatedTrainingCostUSDAtFrozenRate"])
                for value in updates
            )
        ),
        "completedGenerationAtFrozenRate": str(
            sum(
                Decimal(value["estimatedProviderCostUSDAtFrozenRates"])
                for value in [*sentinels, *scores]
            )
        ),
        "abandonedAttemptMaximumUSD": str(abandoned_maximum),
    }
    manifest["finalCheckpoints"] = {
        "samplerCheckpointPath": updates[-1]["samplerCheckpointPath"],
        "optimizerStatePath": updates[-1]["optimizerStatePath"],
    }
    manifest["artifactDigestsSHA256"] = {
        "base-sentinels.jsonl": sha256(sentinels_path),
        "scores.jsonl": sha256(scores_path),
        "updates.jsonl": sha256(updates_path),
        "training-batches.jsonl": sha256(batches_path),
    }
    atomic_json(manifest_path, manifest)
    print(json.dumps(manifest, indent=2))
    return 0


def record_interruption(error: Exception) -> bool:
    arguments = parse_arguments()
    path = arguments.output.expanduser().resolve() / "stability.json"
    if not path.exists():
        return False
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["status"] = "interrupted"
    manifest["interruptedAt"] = iso8601()
    manifest["failure"] = {
        "type": type(error).__name__,
        "message": str(error),
        "traceback": traceback.format_exc(),
    }
    atomic_json(path, manifest)
    return bool(manifest.get("activeUpdate") or manifest.get("inflightOperation"))


def main() -> int:
    automatic_restarts = 0
    while True:
        try:
            return run()
        except Exception as error:
            recoverable = record_interruption(error)
            if recoverable and automatic_restarts < 2:
                automatic_restarts += 1
                print(
                    "inkling-stability recovery restarting from committed artifacts "
                    f"attempt={automatic_restarts}/2",
                    flush=True,
                )
                continue
            raise


if __name__ == "__main__":
    raise SystemExit(main())

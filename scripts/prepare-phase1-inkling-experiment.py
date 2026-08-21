#!/usr/bin/env python3
"""Prepare the four-arm Inkling-Small Phase 1 experiment plan locally."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from phase1_experiment import canonical_bytes
from phase1_inkling import (
    ARM_NAMES,
    GENERATION_CONTRACT,
    INKLING_MODEL,
    INKLING_PLAN_VERSION,
    REASONING_CONDITIONS,
    TRAINING_CONTRACT,
    InklingContractError,
    load_experiment_blocks,
    load_jsonl,
    sha256,
)


PREFILL_PER_MILLION = Decimal("0.58")
SAMPLE_PER_MILLION = Decimal("1.44")
TRAIN_PER_MILLION = Decimal("1.73")
CHECKPOINT_RESERVE = Decimal("2.00")
PRICING_AS_OF = "2026-08-20"
PRICING_URL = "https://tinker-docs.thinkingmachines.ai/tinker/models/"


def iso8601() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def money(tokens: int, rate: Decimal) -> Decimal:
    return Decimal(tokens) * rate / Decimal(1_000_000)


def decimal_string(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.000001")))


def shifted_positions(row: dict[str, Any]) -> int:
    return len(row["inputIDs"]) - 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--inkling-pack", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--tinker-project-id", required=True)
    parser.add_argument("--hard-ceiling-usd", type=Decimal)
    arguments = parser.parse_args()
    output = arguments.output.expanduser().resolve()
    if output.exists():
        raise InklingContractError(f"output already exists: {output}")
    if arguments.hard_ceiling_usd is not None and (
        arguments.hard_ceiling_usd <= 0
        or arguments.hard_ceiling_usd.quantize(Decimal("0.01"))
        != arguments.hard_ceiling_usd
    ):
        raise InklingContractError("hard ceiling must be a positive cent amount")

    corpus_path = arguments.corpus.expanduser().resolve()
    pack_path = arguments.inkling_pack.expanduser().resolve()
    corpus = json.loads((corpus_path / "corpus.json").read_text(encoding="utf-8"))
    packing = json.loads((pack_path / "packing.json").read_text(encoding="utf-8"))
    blocks = load_experiment_blocks(corpus_path)
    if len(blocks) < 2:
        raise InklingContractError("prequential experiment requires multiple blocks")
    rows_by_condition = {
        condition: load_jsonl(pack_path / f"{condition}-packed-examples.jsonl")
        for condition in REASONING_CONDITIONS
    }
    ids = [value for block in blocks for value in block["exampleIDs"]]
    for condition, rows in rows_by_condition.items():
        if [value["exampleID"] for value in rows] != ids:
            raise InklingContractError(f"{condition} pack differs from corpus order")

    evaluation_ids = {
        value for block in blocks[1:] for value in block["exampleIDs"]
    }
    update_ids_by_block = [block["exampleIDs"] for block in blocks[:-1]]
    operations: dict[str, Any] = {}
    total_prefill = 0
    total_sample_ceiling = 0
    total_training_positions = 0
    total_loss_presentations = 0
    updates: dict[str, list[dict[str, Any]]] = {}
    for condition, rows in rows_by_condition.items():
        by_id = {value["exampleID"]: value for value in rows}
        evaluation = [value for value in rows if value["exampleID"] in evaluation_ids]
        # Frozen and personalized each receive one NLL and one generation request.
        nll_prefill = 2 * sum(len(value["inputIDs"]) for value in evaluation)
        generation_prefill = 2 * sum(
            value["modelInputTokenCount"] for value in evaluation
        )
        sampled = (
            2 * len(evaluation) * GENERATION_CONTRACT["maximumTokens"]
        )
        condition_updates = []
        condition_training = 0
        condition_loss = 0
        for ordinal, (block, block_ids) in enumerate(
            zip(blocks[:-1], update_ids_by_block, strict=True), 1
        ):
            submitted = sum(shifted_positions(by_id[value]) for value in block_ids)
            loss = sum(by_id[value]["targetTokenCount"] for value in block_ids)
            condition_training += submitted
            condition_loss += loss
            condition_updates.append({
                "updateOrdinal": ordinal,
                "afterBlockID": block["blockID"],
                "trainedExamples": len(block_ids),
                "epochs": 1,
                "submittedPositions": submitted,
                "lossBearingTokenPresentations": loss,
            })
        operations[condition] = {
            "effort": REASONING_CONDITIONS[condition],
            "scoredExamplesPerArm": len(evaluation),
            "scoreArms": 2,
            "nllCalls": 2 * len(evaluation),
            "sampleCalls": 2 * len(evaluation),
            "nllPrefillTokens": nll_prefill,
            "generationPrefillTokens": generation_prefill,
            "sampledTokenCeiling": sampled,
            "trainingCalls": sum(len(value) for value in update_ids_by_block),
            "trainingSubmittedPositions": condition_training,
            "lossBearingTokenPresentations": condition_loss,
            "optimizerStateCheckpointSaves": len(blocks) - 1,
            "samplerCheckpointSaves": len(blocks) - 1,
        }
        updates[condition] = condition_updates
        total_prefill += nll_prefill + generation_prefill
        total_sample_ceiling += sampled
        total_training_positions += condition_training
        total_loss_presentations += condition_loss

    cost = {
        "prefillUSD": money(total_prefill, PREFILL_PER_MILLION),
        "sampleCeilingUSD": money(total_sample_ceiling, SAMPLE_PER_MILLION),
        "trainingUSD": money(total_training_positions, TRAIN_PER_MILLION),
        "checkpointReserveUSD": CHECKPOINT_RESERVE,
    }
    projected = sum(cost.values(), Decimal(0))
    ceiling = arguments.hard_ceiling_usd
    status = (
        "awaiting_explicit_cost_ceiling"
        if ceiling is None
        else (
            "authorized_for_execution"
            if projected <= ceiling
            else "blocked_projected_cost_exceeds_ceiling"
        )
    )
    project = Path(__file__).resolve().parent.parent
    implementation_paths = [
        Path(__file__).resolve(),
        project / "scripts/phase1_inkling.py",
        project / "scripts/prepare-phase1-inkling-pack.py",
        project / "scripts/preflight-phase1-inkling.py",
        project / "scripts/run-phase1-inkling-prequential.py",
        project / "scripts/audit-phase1-inkling-experiment.py",
        project / "scripts/check-phase1-inkling.py",
        project / "scripts/check-phase1-inkling-runner.py",
        project / "scripts/inkling-requirements.txt",
    ]
    missing = [path for path in implementation_paths if not path.is_file()]
    if missing:
        raise InklingContractError(f"missing planned implementation: {missing}")
    plan = {
        "schemaVersion": 1,
        "planVersion": INKLING_PLAN_VERSION,
        "createdAt": iso8601(),
        "status": status,
        "source": {
            "corpusDirectory": str(corpus_path),
            "corpusID": corpus["corpusID"],
            "corpusSHA256": sha256(corpus_path / "corpus.json"),
            "examplesSHA256": sha256(corpus_path / "examples.jsonl"),
            "episodeBlocksSHA256": sha256(corpus_path / "episode-blocks.jsonl"),
            "inklingPackDirectory": str(pack_path),
            "inklingPackingSHA256": sha256(pack_path / "packing.json"),
            "packedArtifactsSHA256": packing["artifactDigestsSHA256"],
        },
        "protocol": {
            "examples": len(ids),
            "warmupBlockID": blocks[0]["blockID"],
            "warmupExamples": len(blocks[0]["exampleIDs"]),
            "prospectiveEvaluationBlockIDs": [
                value["blockID"] for value in blocks[1:]
            ],
            "scoredExamplesPerArm": len(evaluation_ids),
            "scoreCompleteBlockBeforeUpdate": True,
            "warmupBlockIsTrainingOnly": True,
            "terminalBlockReceivesPostScoreUpdate": False,
            "personalizedUpdatePolicy": "warm_start_then_train_new_block_only_except_terminal_block",
            "arms": [
                ARM_NAMES[condition][kind]
                for condition in REASONING_CONDITIONS
                for kind in ("frozen", "personalized")
            ],
            "reasoningConditions": REASONING_CONDITIONS,
            "generationContract": GENERATION_CONTRACT,
            "trainingContract": TRAINING_CONTRACT,
            "updates": updates,
        },
        "tinker": {
            "projectID": arguments.tinker_project_id,
            "model": INKLING_MODEL,
            "maximumContextTokens": 65536,
            "operations": operations,
            "totals": {
                "prefillTokens": total_prefill,
                "sampledTokenCeiling": total_sample_ceiling,
                "trainingSubmittedPositions": total_training_positions,
                "lossBearingTokenPresentations": total_loss_presentations,
                "nllCalls": sum(value["nllCalls"] for value in operations.values()),
                "sampleCalls": sum(value["sampleCalls"] for value in operations.values()),
                "trainingCalls": sum(value["trainingCalls"] for value in operations.values()),
            },
            "pricesPerMillionUSD": {
                "prefill": str(PREFILL_PER_MILLION),
                "sample": str(SAMPLE_PER_MILLION),
                "training": str(TRAIN_PER_MILLION),
            },
            "pricingAsOf": PRICING_AS_OF,
            "pricingSource": PRICING_URL,
            "projectedCostUSD": {
                **{key: decimal_string(value) for key, value in cost.items()},
                "totalIncludingReserve": decimal_string(projected),
            },
            "hardExecutionCeilingUSD": None if ceiling is None else str(ceiling),
            "projectedCostWithinHardCeiling": (
                None if ceiling is None else projected <= ceiling
            ),
            "interruptionPolicy": {
                "inFlightOperationReplayAllowed": False,
                "partialUpdateReplayAllowed": False,
            },
        },
        "implementation": {
            "fileDigestsSHA256": {
                str(path.relative_to(project)): sha256(path)
                for path in implementation_paths
            }
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(canonical_bytes(plan))
    print(json.dumps(plan, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

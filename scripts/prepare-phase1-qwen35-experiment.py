#!/usr/bin/env python3
"""Build the no-provider-call plan for the Phase 1 35B Qwen comparison."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

from phase1_experiment import TINKER_TRAINING_CONTRACT, target_text, validate_inputs
from phase1_qwen35_native import (
    GENERATION_TOKEN_CEILING,
    MODEL_SPECS,
    NATIVE_PACK_VERSION,
    NATIVE_SCHEMA_VERSION,
    RENDERER_CONTRACT,
    build_native_rows,
    canonical_bytes,
    sha256,
    tokenizer_file_digests,
    package_versions,
    write_jsonl,
)
from phase1_training_contract import TrainingContractError, git_revision


PLAN_VERSION = "phase1-qwen35-provider-plan-v2"
PRICING_AS_OF = "2026-08-21"
PRICING_SOURCE = "https://tinker-docs.thinkingmachines.ai/tinker/models/"
PREFILL_PRICE = Decimal("0.54")
CACHED_PREFILL_PRICE = Decimal("0.108")
SAMPLE_PRICE = Decimal("1.335")
TRAIN_PRICE = Decimal("1.177")
CHECKPOINT_RESERVE_PER_MODEL = Decimal("2.00")
MAXIMUM_SCORE_RETRIES_PER_MODEL = 1
MAXIMUM_TRAINING_BLOCK_RESTARTS_PER_MODEL = 1


def iso8601() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def money(tokens: int, price: Decimal) -> Decimal:
    return Decimal(tokens) * price / Decimal(1_000_000)


def money_string(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.000001")))


def copy_tokenizer(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True)
    copied = 0
    for path in sorted(source.rglob("*")):
        if not path.is_file() or ".cache" in path.parts:
            continue
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied += 1
    if copied == 0 or not (destination / "tokenizer.json").is_file():
        raise TrainingContractError(f"no tokenizer files copied from {source}")


def model_cost_plan(
    rows: list[dict[str, Any]],
    blocks: list[dict[str, Any]],
    *,
    include_heldout_nll: bool,
) -> dict[str, Any]:
    rows_by_id = {row["exampleID"]: row for row in rows}
    evaluation_ids = [
        example_id for block in blocks[1:] for example_id in block["exampleIDs"]
    ]
    training_ids = [
        example_id for block in blocks[:-1] for example_id in block["exampleIDs"]
    ]
    evaluation = [rows_by_id[value] for value in evaluation_ids]
    training = [rows_by_id[value] for value in training_ids]

    # Both frozen and personalized arms generate on the same examples.  Held-out
    # likelihood is optional because the Phase 1 cross-model headline is the
    # generated completion evaluation; omitting it saves a second long prefill.
    nll_prefill = (
        2 * sum(row["fullSequenceTokenCount"] for row in evaluation)
        if include_heldout_nll
        else 0
    )
    generation_prefill = 2 * sum(
        row["modelInputTokenCount"] for row in evaluation
    )
    sample_ceiling = 2 * len(evaluation) * GENERATION_TOKEN_CEILING
    training_positions = sum(row["fullSequenceTokenCount"] - 1 for row in training)
    loss_presentations = sum(row["targetTokenCount"] for row in training)
    prefill = nll_prefill + generation_prefill
    costs = {
        "trainingUSD": money(training_positions, TRAIN_PRICE),
        "uncachedPrefillUSD": money(prefill, PREFILL_PRICE),
        "allCachedPrefillLowerBoundUSD": money(prefill, CACHED_PREFILL_PRICE),
        "maximumSamplingUSD": money(sample_ceiling, SAMPLE_PRICE),
        "checkpointReserveUSD": CHECKPOINT_RESERVE_PER_MODEL,
    }
    maximum_score_replay = max(
        (
            money(
                row["modelInputTokenCount"]
                + (row["fullSequenceTokenCount"] if include_heldout_nll else 0),
                PREFILL_PRICE,
            )
            + money(GENERATION_TOKEN_CEILING, SAMPLE_PRICE)
            for row in evaluation
        ),
        default=Decimal(0),
    )
    maximum_training_replay = max(
        (
            money(
                sum(rows_by_id[value]["fullSequenceTokenCount"] - 1 for value in block["exampleIDs"]),
                TRAIN_PRICE,
            )
            for block in blocks[:-1]
        ),
        default=Decimal(0),
    )
    recovery = {
        "maximumOneScoreRetryUSD": maximum_score_replay,
        "maximumOneTrainingBlockRestartUSD": maximum_training_replay,
    }
    conservative = (
        costs["trainingUSD"]
        + costs["uncachedPrefillUSD"]
        + costs["maximumSamplingUSD"]
        + costs["checkpointReserveUSD"]
        + sum(recovery.values(), Decimal(0))
    )
    cached_lower = (
        costs["trainingUSD"]
        + costs["allCachedPrefillLowerBoundUSD"]
        + costs["maximumSamplingUSD"]
        + costs["checkpointReserveUSD"]
        + sum(recovery.values(), Decimal(0))
    )
    return {
        "operations": {
            "frozenScores": len(evaluation),
            "personalizedScores": len(evaluation),
            "likelihoodCalls": 2 * len(evaluation) if include_heldout_nll else 0,
            "generationCalls": 2 * len(evaluation),
            "updates": len(blocks) - 1,
            "optimizerSteps": len(training),
            "samplerCheckpointSaves": len(blocks) - 1,
            "optimizerStateSaves": len(blocks) - 1,
        },
        "tokens": {
            "likelihoodPrefill": nll_prefill,
            "generationPrefill": generation_prefill,
            "totalPrefill": prefill,
            "maximumSampled": sample_ceiling,
            "trainingPositions": training_positions,
            "lossBearingTokenPresentations": loss_presentations,
        },
        "projectedCostUSD": {
            **{key: money_string(value) for key, value in costs.items()},
            **{key: money_string(value) for key, value in recovery.items()},
            "conservativeUncachedIncludingReserve": money_string(conservative),
            "allCachedLowerBoundIncludingReserve": money_string(cached_lower),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--shared-pack", required=True, type=Path)
    parser.add_argument("--qwen35-base-tokenizer", required=True, type=Path)
    parser.add_argument("--qwen36-hybrid-tokenizer", required=True, type=Path)
    parser.add_argument("--tinker-project-id", required=True)
    parser.add_argument(
        "--include-heldout-nll",
        action="store_true",
        help="also pay for full-sequence target likelihood on both arms",
    )
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    output = arguments.output.expanduser().resolve()
    if output.exists():
        raise TrainingContractError(f"output already exists: {output}")
    corpus_directory = arguments.corpus.expanduser().resolve()
    shared_pack_directory = arguments.shared_pack.expanduser().resolve()
    corpus, examples, _, _ = validate_inputs(corpus_directory, shared_pack_directory)
    examples_by_id = {value["exampleID"]: value for value in examples}
    blocks = corpus["blocking"]["blocks"]
    if len(examples) != 450 or len(blocks) != 9 or any(
        len(block["exampleIDs"]) != 50 for block in blocks
    ):
        raise TrainingContractError(
            "35B comparison requires exactly nine chronological blocks of 50"
        )

    tokenizer_sources = {
        "qwen35_base": arguments.qwen35_base_tokenizer.expanduser().resolve(),
        "qwen36_hybrid": arguments.qwen36_hybrid_tokenizer.expanduser().resolve(),
    }
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        model_plans = {}
        native_artifacts = {}
        preflight_by_model = {}
        for model_key in MODEL_SPECS:
            native_directory = temporary / model_key
            tokenizer_directory = native_directory / "tokenizer"
            copy_tokenizer(tokenizer_sources[model_key], tokenizer_directory)
            rows, metadata = build_native_rows(
                corpus_directory=corpus_directory,
                shared_pack_directory=shared_pack_directory,
                model_key=model_key,
                tokenizer_directory=tokenizer_directory,
            )
            rows_path = native_directory / "rendered-examples.jsonl"
            write_jsonl(rows_path, rows)
            native_manifest = {
                "schemaVersion": NATIVE_SCHEMA_VERSION,
                "nativePackVersion": NATIVE_PACK_VERSION,
                "createdAt": iso8601(),
                **metadata,
                "tokenizerFileDigestsSHA256": tokenizer_file_digests(
                    tokenizer_directory
                ),
                "artifactDigestsSHA256": {
                    "rendered-examples.jsonl": sha256(rows_path)
                },
            }
            (native_directory / "native-pack.json").write_bytes(
                canonical_bytes(native_manifest)
            )
            model_plans[model_key] = model_cost_plan(
                rows,
                blocks,
                include_heldout_nll=arguments.include_heldout_nll,
            )
            rows_by_id = {value["exampleID"]: value for value in rows}
            first_block_ids = blocks[0]["exampleIDs"]
            candidates_by_app: dict[str, list[str]] = {}
            for example_id in first_block_ids:
                app = (
                    examples_by_id[example_id]
                    .get("conditioningState", {})
                    .get("destination", {})
                    .get("appName")
                )
                candidates_by_app.setdefault(str(app), []).append(example_id)
            expected_apps = {"ChatGPT", "Code", "Google Chrome", "Obsidian"}
            if set(candidates_by_app) != expected_apps:
                raise TrainingContractError(
                    "first block lacks the four expected preflight applications"
                )
            preflight_by_app = {
                app: min(
                    candidates_by_app[app],
                    key=lambda value: (
                        rows_by_id[value]["fullSequenceTokenCount"],
                        value,
                    ),
                )
                for app in sorted(expected_apps)
            }
            paste_candidates = [
                value
                for value in first_block_ids
                if rows_by_id[value]["pasteActionCount"] > 0
            ]
            if not paste_candidates:
                raise TrainingContractError(
                    "first block lacks a grounded paste target for native-loss preflight"
                )
            paste_probe = min(
                paste_candidates,
                key=lambda value: (
                    rows_by_id[value]["fullSequenceTokenCount"],
                    value,
                ),
            )
            paste_app = (
                examples_by_id[paste_probe]
                .get("conditioningState", {})
                .get("destination", {})
                .get("appName")
            )
            preflight_by_app[str(paste_app)] = paste_probe
            preflight_ids = [preflight_by_app[app] for app in sorted(expected_apps)]
            preflight_epochs = 5
            preflight_training_positions = preflight_epochs * sum(
                rows_by_id[value]["fullSequenceTokenCount"] - 1
                for value in preflight_ids
            )
            preflight_prefill = 2 * sum(
                rows_by_id[value]["modelInputTokenCount"]
                for value in preflight_ids
            )
            preflight_sample_ceiling = 2 * len(preflight_ids) * GENERATION_TOKEN_CEILING
            preflight_cost = (
                money(preflight_training_positions, TRAIN_PRICE)
                + money(preflight_prefill, PREFILL_PRICE)
                + money(preflight_sample_ceiling, SAMPLE_PRICE)
                + Decimal("0.50")
            )
            preflight_by_model[model_key] = {
                "exampleIDs": preflight_ids,
                "applications": sorted(expected_apps),
                "groundedPasteProbeExampleID": paste_probe,
                "epochs": preflight_epochs,
                "trainingCalls": len(preflight_ids) * preflight_epochs,
                "trainingPositions": preflight_training_positions,
                "generationCalls": 2 * len(preflight_ids),
                "generationPrefillTokens": preflight_prefill,
                "sampledTokenCeiling": preflight_sample_ceiling,
                "checkpointReserveUSD": "0.500000",
                "projectedConservativeUSD": money_string(preflight_cost),
            }
            native_artifacts[model_key] = {
                "model": MODEL_SPECS[model_key]["model"],
                "localTokenizerRevision": MODEL_SPECS[model_key][
                    "localTokenizerRevision"
                ],
                "tinkerServedModelRevision": "not_exposed_by_server_capabilities",
                "rendererContract": RENDERER_CONTRACT,
                "renderer": metadata["renderer"],
                "rendererStrategy": metadata["rendererStrategy"],
                "officialRecommendedRenderers": metadata[
                    "officialRecommendedRenderers"
                ],
                "nativePackSHA256": sha256(native_directory / "native-pack.json"),
                "renderedExamplesSHA256": sha256(rows_path),
                "tokenizerVocabularySHA256": metadata[
                    "tokenizerVocabularySHA256"
                ],
                "tokenizerEOSID": metadata["tokenizerEOSID"],
                "responseTerminatorTokenID": metadata[
                    "responseTerminatorTokenID"
                ],
                "responseTerminatorText": metadata["responseTerminatorText"],
            }

        conservative_total = sum(
            Decimal(value["projectedCostUSD"][
                "conservativeUncachedIncludingReserve"
            ])
            for value in model_plans.values()
        )
        cached_lower_total = sum(
            Decimal(value["projectedCostUSD"][
                "allCachedLowerBoundIncludingReserve"
            ])
            for value in model_plans.values()
        )
        plan = {
            "schemaVersion": 1,
            "planVersion": PLAN_VERSION,
            "status": "local_preflight_complete_no_provider_calls_authorization_required",
            "createdAt": iso8601(),
            "experiment": {
                "description": (
                    "35B Base versus stronger next-generation hybrid initialization; "
                    "not a same-checkpoint post-training ablation"
                ),
                "corpusExamples": len(examples),
                "chronologicalBlocks": [
                    {
                        "blockID": block["blockID"],
                        "examples": len(block["exampleIDs"]),
                        "exampleIDsSHA256": hashlib_sha(block["exampleIDs"]),
                    }
                    for block in blocks
                ],
                "warmupBlock": blocks[0]["blockID"],
                "scoredBlocks": [block["blockID"] for block in blocks[1:]],
                "scoreBeforeUpdate": True,
                "trainOnlyNewlyScoredBlock": True,
                "terminalBlockReceivesUpdate": False,
                "frozenAndPersonalizedDeltaIsPrimaryWithinModelComparison": True,
                "crossModelNLLIsPrimary": False,
                "heldoutTargetNLLCollected": arguments.include_heldout_nll,
                "primaryEvaluation": "generated_completion_scorecard_and_holistic_judging",
                "commonSemanticContextPlan": True,
                "modelSpecificRendering": True,
                "crossModelTokenIDsExpectedIdentical": False,
            },
            "source": {
                "corpusID": corpus["corpusID"],
                "corpusSHA256": sha256(corpus_directory / "corpus.json"),
                "examplesSHA256": sha256(corpus_directory / "examples.jsonl"),
                "sharedPackingSHA256": sha256(
                    shared_pack_directory / "packing.json"
                ),
                "sharedContextPlansSHA256": sha256(
                    shared_pack_directory / "context-plans.jsonl"
                ),
            },
            "nativeArtifacts": native_artifacts,
            "training": {
                **TINKER_TRAINING_CONTRACT,
                "lossFunction": "cross_entropy",
                "rendererContract": RENDERER_CONTRACT,
                "renderersByModel": {
                    key: native_artifacts[key]["renderer"] for key in MODEL_SPECS
                },
                "rendererStrategiesByModel": {
                    key: native_artifacts[key]["rendererStrategy"]
                    for key in MODEL_SPECS
                },
                "trainOnWhat": "last_assistant_message",
                "rendererReduction": "none",
                "lossBearingTokens": {
                    "common": "exact authored content and literal <|paste|> tokens",
                    "qwen35_base": "one tokenizer EOS after exact raw continuation",
                    "qwen36_hybrid": "one native <|im_end|> response terminator",
                },
                "maskedTokens": {
                    "common": "complete causal semantic input",
                    "qwen35_base": "no injected chat envelope",
                    "qwen36_hybrid": "chat headers and injected non-thinking envelope",
                },
                "exactContentRendererEvidence": {
                    "upstreamStringNormalization": "content.strip()",
                    "affectedTargetsInFrozenCorpus": sum(
                        target_text(value["target"])
                        != target_text(value["target"]).strip()
                        for value in examples
                    ),
                    "leadingWhitespaceTargets": sum(
                        target_text(value["target"])
                        != target_text(value["target"]).lstrip()
                        for value in examples
                    ),
                    "trailingWhitespaceTargets": sum(
                        target_text(value["target"])
                        != target_text(value["target"]).rstrip()
                        for value in examples
                    ),
                    "resolution": (
                        "Base uses exact raw continuation; hybrid uses a namespaced "
                        "exact-content subclass of its compatible chat renderer"
                    ),
                },
            },
            "generation": {
                "temperature": 0.6,
                "seed": 17,
                "maximumTokens": GENERATION_TOKEN_CEILING,
                "stopSource": "renderer.get_stop_sequences",
                "parseSource": "renderer.parse_response",
            },
            "checkpointing": {
                "samplerWeightsAfterEveryCommittedUpdate": True,
                "optimizerStateAfterEveryCommittedUpdate": True,
                "resumeWithOptimizerState": True,
                "interruptedUpdateRestartsFromParentCheckpoint": True,
                "terminalBlockCheckpointNotCreated": True,
            },
            "recovery": {
                "maximumAutomaticScoreRetriesPerModel": MAXIMUM_SCORE_RETRIES_PER_MODEL,
                "maximumAutomaticTrainingBlockRestartsPerModel": MAXIMUM_TRAINING_BLOCK_RESTARTS_PER_MODEL,
                "scoreRecovery": "retry_one_nonmutating_score_and_charge_its_maximum",
                "trainingRecovery": "restore_parent_optimizer_state_and_restart_whole_block_once",
                "costAccounting": "planned_conservative_total_plus_abandoned_attempt_maximums",
            },
            "tinker": {
                "projectID": arguments.tinker_project_id,
                "pricingAsOf": PRICING_AS_OF,
                "pricingSource": PRICING_SOURCE,
                "pricesPerMillionUSD": {
                    "prefill": str(PREFILL_PRICE),
                    "cachedPrefill": str(CACHED_PREFILL_PRICE),
                    "sample": str(SAMPLE_PRICE),
                    "train": str(TRAIN_PRICE),
                },
                "models": model_plans,
                "projectedCostUSD": {
                    "conservativeUncachedIncludingReserve": money_string(
                        conservative_total
                    ),
                    "allCachedLowerBoundIncludingReserve": money_string(
                        cached_lower_total
                    ),
                },
            },
            "paidPreflightRequiredAfterAuthorization": {
                "frozenGenerationGatePerModel": True,
                "tinyOverfitFreeGenerationGatePerModel": True,
                "fullRunMayBeginOnlyAfterBothPass": True,
                "models": preflight_by_model,
                "projectedConservativeTotalUSD": money_string(
                    sum(
                        Decimal(value["projectedConservativeUSD"])
                        for value in preflight_by_model.values()
                    )
                ),
            },
            "authorizationBoundary": {
                "providerCallsMadeByThisPreparation": False,
                "personalDataTransmitted": False,
                "paidOperationsAuthorized": False,
            },
            "implementation": {
                "codeRevision": git_revision(Path(__file__).resolve().parent.parent),
                "fileDigestsSHA256": {
                    relative: sha256(Path(__file__).resolve().parent.parent / relative)
                    for relative in (
                        "scripts/phase1_experiment.py",
                        "scripts/phase1_training_contract.py",
                        "scripts/phase1_tinker_overfit_contract.py",
                        "scripts/phase1_qwen35_native.py",
                        "scripts/prepare-phase1-qwen35-experiment.py",
                        "scripts/audit-phase1-qwen35-experiment.py",
                        "scripts/run-phase1-qwen35-experiment.py",
                        "scripts/run-phase1-qwen35-preflight.py",
                        "scripts/qwen35-requirements.txt",
                    )
                },
                "packageVersions": package_versions(),
            },
        }
        (temporary / "provider-plan.json").write_bytes(canonical_bytes(plan))
        os.replace(temporary, output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    print(f"Prepared no-provider-call Qwen 35B plan at {output}")
    print(f"Conservative uncached projection: ${money_string(conservative_total)}")
    print(f"All-cached lower-bound projection: ${money_string(cached_lower_total)}")
    return 0


def hashlib_sha(value: Any) -> str:
    import hashlib

    return hashlib.sha256(canonical_bytes(value)).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())

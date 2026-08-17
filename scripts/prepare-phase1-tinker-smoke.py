#!/usr/bin/env python3
"""Locally validate a Phase 1 pack and its exact Tinker causal-shift contract.

This command contains no Tinker SDK, authentication, or network code. It does
not serialize token payloads. Its report is a local preflight for a separately
authorized remote tokenizer check and training run.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

from phase1_training_contract import (
    TrainingContractError,
    adapt_dataset_to_tinker,
    git_revision,
    git_worktree_dirty,
    sha256,
    token_ids_sha256,
    validate_local_frozen_tokenizer,
    validate_packed_dataset,
)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--training-price-per-million-usd",
        required=True,
        type=Decimal,
        help="Tinker training price snapshot used only for projection",
    )
    parser.add_argument(
        "--pricing-as-of",
        required=True,
        help="Date of the manually verified price snapshot (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--projected-epochs",
        type=int,
        nargs="+",
        default=[1, 10, 20],
    )
    arguments = parser.parse_args()
    if arguments.training_price_per_million_usd <= 0:
        parser.error("--training-price-per-million-usd must be positive")
    if any(epoch <= 0 for epoch in arguments.projected_epochs):
        parser.error("--projected-epochs values must be positive")
    return arguments


def main() -> int:
    arguments = parse_arguments()
    output = arguments.output.expanduser().resolve()
    if output.exists():
        raise TrainingContractError(f"output already exists: {output}")

    dataset = validate_packed_dataset(arguments.input)
    tokenizer_report = validate_local_frozen_tokenizer(dataset)
    datums = adapt_dataset_to_tinker(dataset)

    submitted_tokens = sum(datum.length for datum in datums)
    expected_submitted_tokens = sum(len(row["inputIDs"]) - 1 for row in dataset.rows)
    if submitted_tokens != expected_submitted_tokens:
        raise TrainingContractError("submitted-token count is not derived from shifted datums")
    weighted_positions = sum(datum.weighted_positions for datum in datums)
    maximum_datum_length = max(datum.length for datum in datums)
    minimum_datum_length = min(datum.length for datum in datums)
    price = arguments.training_price_per_million_usd
    per_epoch = Decimal(submitted_tokens) * price / Decimal(1_000_000)

    project_directory = Path(__file__).resolve().parent.parent
    implementation_files = [
        project_directory / "scripts" / "phase1_training_contract.py",
        Path(__file__).resolve(),
    ]
    report = {
        "schemaVersion": 1,
        "test": "phase1_tinker_local_preflight",
        "scope": "mechanical_training_harness_only",
        "implementation": {
            "codeRevision": git_revision(project_directory),
            "workingTreeDirty": git_worktree_dirty(project_directory),
            "fileDigestsSHA256": {
                str(path.relative_to(project_directory)): sha256(path)
                for path in implementation_files
            },
        },
        "source": {
            "packedDirectory": str(dataset.directory),
            "packingSHA256": sha256(dataset.directory / "packing.json"),
            "packedExamplesSHA256": sha256(
                dataset.directory / "packed-examples.jsonl"
            ),
            "packerVersion": dataset.manifest["packerVersion"],
            "modelRepository": dataset.manifest["tokenizer"]["repository"],
            "modelRevision": dataset.manifest["tokenizer"]["resolvedRevision"],
        },
        "localFrozenTokenizerValidation": tokenizer_report,
        "remoteTokenizerPreflight": {
            "status": "not_run_requires_separate_authorization",
            "datasetSubmissionRequired": False,
            "requiredChecks": [
                "server_model_repository_and_revision",
                "tokenizer_vocabulary_size",
                "eos_token_id",
                "paste_marker_encoding",
                "representative_encode_decode_results",
            ],
        },
        "causalShiftContract": {
            "modelInput": "packed.inputIDs[:-1]",
            "targetTokens": "packed.inputIDs[1:]",
            "weights": "1.0 iff packed.labels[i + 1] is loss-bearing; otherwise 0.0",
            "weightedInvariant": "targetTokens[i] == packed.labels[i + 1]",
            "status": "exhaustively_passed_for_every_position",
        },
        "tinkerDatumProjection": {
            "examples": len(datums),
            "submittedTokensPerEpoch": submitted_tokens,
            "weightedPositionsPerEpoch": weighted_positions,
            "unweightedPositionsPerEpoch": submitted_tokens - weighted_positions,
            "minimumDatumLength": minimum_datum_length,
            "maximumDatumLength": maximum_datum_length,
            "packTotalTokensBeforeShift": sum(
                len(row["inputIDs"]) for row in dataset.rows
            ),
            "positionsRemovedByShift": len(datums),
            "datumFingerprints": [
                {
                    "exampleID": datum.example_id,
                    "length": datum.length,
                    "weightedPositions": datum.weighted_positions,
                    "modelInputSHA256": token_ids_sha256(
                        datum.model_input_token_ids
                    ),
                    "targetTokensSHA256": token_ids_sha256(datum.target_tokens),
                    "weightsSHA256": token_ids_sha256(
                        [int(weight) for weight in datum.weights]
                    ),
                }
                for datum in datums
            ],
        },
        "costProjection": {
            "currency": "USD",
            "pricingAsOf": arguments.pricing_as_of,
            "trainingPricePerMillionSubmittedTokens": str(price),
            "submittedTokensPerEpoch": submitted_tokens,
            "trainingCostPerEpoch": str(per_epoch.quantize(Decimal("0.000001"))),
            "epochs": {
                str(epoch): str(
                    (per_epoch * Decimal(epoch)).quantize(Decimal("0.000001"))
                )
                for epoch in sorted(set(arguments.projected_epochs))
            },
            "excludes": ["evaluation", "sampling", "storage", "taxes"],
            "mustBeReverifiedBeforeRemoteRun": True,
        },
        "externalActionGate": {
            "authenticationPerformed": False,
            "networkAccessPerformed": False,
            "datasetTransmitted": False,
            "tinkerSDKImported": False,
            "requiredProjectPolicy": "dedicated_private_project_not_default_project",
            "samplerCheckpointRequired": True,
            "optimizerStateCheckpointRequired": True,
            "nextAction": "stop_for_review_before_authenticated_remote_preflight",
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        "Phase 1 Tinker local preflight passed: "
        f"{len(datums)} datums, {submitted_tokens} submitted tokens/epoch, "
        f"{weighted_positions} weighted positions, max length {maximum_datum_length}"
    )
    print(f"Report: {output}")
    print("Stopped before authentication, network access, or dataset transmission.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, KeyError, TrainingContractError) as error:
        raise SystemExit(f"prepare-phase1-tinker-smoke: {error}")

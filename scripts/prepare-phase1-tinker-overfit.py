#!/usr/bin/env python3
"""Prepare the exact SDK payload and bounded plan for a Tinker smoke run.

This command is intentionally local-only. It imports the pinned Tinker SDK and
constructs every real ``Datum`` in memory, but it contains no API-key loading,
``ServiceClient``, network, training, sampling, or checkpoint path. The output
is the artifact that must be reviewed before a separate execution gate exists.
"""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path
from typing import Any

from phase1_tinker_overfit_contract import (
    BASE_MODEL,
    CONTRACT_SCHEMA_VERSION,
    build_and_validate_sdk_datums,
    build_execution_plan,
)
from phase1_training_contract import (
    TrainingContractError,
    adapt_dataset_to_tinker,
    git_revision,
    git_worktree_dirty,
    load_json,
    sha256,
    validate_packed_dataset,
)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--remote-tokenizer-preflight", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--dedicated-private-project-id", required=True)
    parser.add_argument(
        "--confirm-dedicated-private-project",
        action="store_true",
        help="Attest that this is a private non-default Tinker project",
    )
    arguments = parser.parse_args()
    if not arguments.confirm_dedicated_private_project:
        parser.error("--confirm-dedicated-private-project is required")
    try:
        arguments.dedicated_private_project_id = str(
            uuid.UUID(arguments.dedicated_private_project_id)
        )
    except ValueError as error:
        parser.error(f"invalid dedicated project UUID: {error}")
    return arguments


def validate_remote_preflight(
    report: dict[str, Any],
    dataset: Any,
    project_id: str,
) -> None:
    if (
        report.get("status") != "passed"
        or report.get("test") != "phase1_tinker_remote_tokenizer_preflight"
        or report.get("scope") != "authenticated_tokenizer_only_no_dataset"
    ):
        raise TrainingContractError("remote tokenizer preflight did not pass")
    project = report.get("project", {})
    if (
        project.get("projectID") != project_id
        or project.get("dedicatedPrivateProjectAttested") is not True
        or project.get("defaultProjectAllowed") is not False
    ):
        raise TrainingContractError("remote preflight used a different project policy")
    source = report.get("source", {})
    if (
        source.get("modelRepository") != BASE_MODEL
        or source.get("packingSHA256")
        != sha256(dataset.directory / "packing.json")
        or source.get("packedExamplesSHA256")
        != sha256(dataset.directory / "packed-examples.jsonl")
    ):
        raise TrainingContractError("remote preflight does not bind this frozen pack")
    comparison = report.get("tokenizerComparison", {})
    if (
        comparison.get("status") != "passed"
        or comparison.get("compatible") is not True
        or comparison.get("completeVocabulary", {}).get("exact") is not True
        or comparison.get("packTokenIDs", {}).get("mismatchCount") != 0
        or comparison.get("packSequenceDecoding", {}).get("mismatchCount") != 0
        or comparison.get("specialTokensExact") is not True
        or comparison.get("pasteMarkerExact") is not True
    ):
        raise TrainingContractError("remote tokenizer comparison is incomplete")
    external = report.get("externalActions", {})
    if any(
        external.get(key) is not False
        for key in [
            "datumConstructed",
            "packedTokensTransmitted",
            "labelsTransmitted",
            "humanContentTransmitted",
            "samplingPerformed",
            "trainingPerformed",
            "checkpointCreated",
        ]
    ):
        raise TrainingContractError("remote preflight exceeded tokenizer-only scope")


def main() -> int:
    arguments = parse_arguments()
    output = arguments.output.expanduser().resolve()
    if output.exists():
        raise TrainingContractError(f"output already exists: {output}")

    dataset = validate_packed_dataset(arguments.input)
    contracts = adapt_dataset_to_tinker(dataset)
    remote_path = arguments.remote_tokenizer_preflight.expanduser().resolve()
    remote_report = load_json(remote_path)
    validate_remote_preflight(
        remote_report, dataset, arguments.dedicated_private_project_id
    )
    sdk_datums, sdk_validations, sdk_version = build_and_validate_sdk_datums(
        contracts
    )
    if len(sdk_datums) != len(dataset.rows):
        raise TrainingContractError("SDK Datum construction changed example count")
    execution_plan = build_execution_plan(dataset.rows, contracts)

    project_directory = Path(__file__).resolve().parent.parent
    implementation_files = [
        project_directory / "scripts" / "phase1_training_contract.py",
        project_directory / "scripts" / "phase1_tinker_overfit_contract.py",
        Path(__file__).resolve(),
        project_directory / "scripts" / "tinker-requirements.txt",
    ]
    report = {
        "schemaVersion": CONTRACT_SCHEMA_VERSION,
        "test": "phase1_tinker_overfit_local_sdk_preparation",
        "status": "passed",
        "scope": "local_sdk_datum_construction_and_execution_plan_only",
        "implementation": {
            "codeRevision": git_revision(project_directory),
            "workingTreeDirty": git_worktree_dirty(project_directory),
            "tinkerSDKVersion": sdk_version,
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
            "examples": len(dataset.rows),
            "modelRepository": dataset.manifest["tokenizer"]["repository"],
            "frozenHuggingFaceRevision": dataset.manifest["tokenizer"][
                "resolvedRevision"
            ],
        },
        "remoteTokenizerPreflight": {
            "path": str(remote_path),
            "sha256": sha256(remote_path),
            "status": "passed_and_bound_to_source",
            "serverModelWeightsRevision": None,
            "serverModelWeightsRevisionStatus": (
                "unverified_not_exposed_by_tinker_api"
            ),
        },
        "project": {
            "projectID": arguments.dedicated_private_project_id,
            "dedicatedPrivateProjectAttested": True,
            "defaultProjectAllowed": False,
        },
        "sdkDatumValidation": {
            "status": "all_real_sdk_objects_round_trip_exactly",
            "examples": len(sdk_validations),
            "submittedPositionsPerEpoch": sum(
                validation.length for validation in sdk_validations
            ),
            "weightedPositionsPerEpoch": sum(
                validation.weighted_positions for validation in sdk_validations
            ),
            "modelInputDType": "encoded_integer_tokens",
            "targetTensorDType": "int64",
            "weightTensorDType": "float32",
            "datums": [validation.__dict__ for validation in sdk_validations],
        },
        "executionPlan": execution_plan,
        "externalActionGate": {
            "apiKeyRead": False,
            "authenticationPerformed": False,
            "serviceClientConstructed": False,
            "networkAccessPerformed": False,
            "datasetTransmitted": False,
            "trainingPerformed": False,
            "samplingPerformed": False,
            "checkpointCreated": False,
            "executionPathPresentInThisCommand": False,
            "nextAction": (
                "review_this_exact_plan_then_separately_authorize_a_data_bearing_runner"
            ),
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        "Phase 1 Tinker SDK preparation passed: "
        f"{len(sdk_datums)} real Datums; "
        f"{execution_plan['operationCeilings']['training']['submittedTokens']} "
        "maximum training positions"
    )
    print(
        "Projected cost including reserve: $"
        f"{execution_plan['costCeiling']['projectedIncludingReserve']} "
        "(hard reviewed maximum $"
        f"{execution_plan['costCeiling']['hardMaximumProjected']})"
    )
    print(f"Plan: {output}")
    print("Stopped before API-key access, service construction, or data transfer.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, KeyError, TrainingContractError) as error:
        raise SystemExit(f"prepare-phase1-tinker-overfit: {error}")

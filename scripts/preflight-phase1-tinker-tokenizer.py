#!/usr/bin/env python3
"""Authenticated tokenizer-only Tinker preflight for a frozen Phase 1 pack.

This command creates a project-scoped Tinker session and base-model sampling
client solely to obtain the tokenizer Tinker associates with the model. It
does not create Datum objects, sample, train, save checkpoints, or transmit
any packed token, label, event, or human-content payload.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import sys
import uuid
from pathlib import Path

from phase1_training_contract import (
    TrainingContractError,
    compare_pack_tokenizers,
    git_revision,
    git_worktree_dirty,
    load_local_frozen_tokenizer,
    sha256,
    validate_packed_dataset,
)


PINNED_TINKER_SDK_VERSION = "0.25.0"


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--dedicated-private-project-id", required=True)
    parser.add_argument(
        "--confirm-dedicated-private-project",
        action="store_true",
        help="Attest that the supplied ID is not the Tinker Default project and has private grants",
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


def load_api_key(path: Path) -> tuple[str, str]:
    try:
        content = path.expanduser().read_text(encoding="utf-8").strip()
    except OSError as error:
        raise TrainingContractError(f"cannot read API key file: {error}") from error
    if not content:
        raise TrainingContractError("API key file is empty")
    if "\n" not in content and "=" not in content:
        return content, "raw_value"
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
    return key, "dotenv_assignment"


def main() -> int:
    arguments = parse_arguments()
    output = arguments.output.expanduser().resolve()
    if output.exists():
        raise TrainingContractError(f"output already exists: {output}")

    dataset = validate_packed_dataset(arguments.input)
    local_tokenizer, transformers_version = load_local_frozen_tokenizer(dataset)
    api_key, key_format = load_api_key(arguments.env_file)
    os.environ["TINKER_API_KEY"] = api_key

    try:
        import tinker
    except ImportError as error:
        raise TrainingContractError(
            "Tinker SDK is missing; install scripts/tinker-requirements.txt"
        ) from error
    sdk_version = importlib.metadata.version("tinker")
    if sdk_version != PINNED_TINKER_SDK_VERSION:
        raise TrainingContractError(
            f"Tinker SDK {sdk_version} does not match pin {PINNED_TINKER_SDK_VERSION}"
        )

    model_repository = dataset.manifest["tokenizer"]["repository"]
    project_id = arguments.dedicated_private_project_id
    service_client = tinker.ServiceClient(
        project_id=project_id,
        user_metadata={
            "purpose": "phase1-tokenizer-preflight",
            "dataset_transmission": "none",
        },
    )
    capabilities = service_client.get_server_capabilities()
    supported = [
        model
        for model in capabilities.supported_models
        if model.model_name == model_repository
    ]
    if len(supported) != 1:
        raise TrainingContractError(
            f"Tinker did not report exactly one supported {model_repository} model"
        )
    maximum_context = supported[0].max_context_length
    if maximum_context is None or maximum_context < dataset.maximum_sequence_length:
        raise TrainingContractError("Tinker model context is too short for the frozen pack")

    sampling_client = service_client.create_sampling_client(
        base_model=model_repository
    )
    reported_base_model = sampling_client.get_base_model()
    remote_tokenizer = sampling_client.get_tokenizer()
    comparison = compare_pack_tokenizers(
        dataset, local_tokenizer, remote_tokenizer
    )

    project_directory = Path(__file__).resolve().parent.parent
    implementation_files = [
        project_directory / "scripts" / "phase1_training_contract.py",
        Path(__file__).resolve(),
        project_directory / "scripts" / "tinker-requirements.txt",
    ]
    remote_tokenizer_commit = getattr(remote_tokenizer, "init_kwargs", {}).get(
        "_commit_hash"
    )
    report = {
        "schemaVersion": 1,
        "test": "phase1_tinker_remote_tokenizer_preflight",
        "scope": "authenticated_tokenizer_only_no_dataset",
        "status": comparison["status"],
        "implementation": {
            "codeRevision": git_revision(project_directory),
            "workingTreeDirty": git_worktree_dirty(project_directory),
            "fileDigestsSHA256": {
                str(path.relative_to(project_directory)): sha256(path)
                for path in implementation_files
            },
            "tinkerSDKVersion": sdk_version,
            "transformersVersion": transformers_version,
        },
        "source": {
            "packedDirectory": str(dataset.directory),
            "packingSHA256": sha256(dataset.directory / "packing.json"),
            "packedExamplesSHA256": sha256(
                dataset.directory / "packed-examples.jsonl"
            ),
            "modelRepository": model_repository,
            "frozenHuggingFaceRevision": dataset.manifest["tokenizer"][
                "resolvedRevision"
            ],
        },
        "project": {
            "projectID": project_id,
            "dedicatedPrivateProjectAttested": True,
            "projectGrantsProgrammaticallyVerified": False,
            "defaultProjectAllowed": False,
        },
        "tinkerModel": {
            "requestedBaseModel": model_repository,
            "reportedBaseModel": reported_base_model,
            "maximumContextLength": maximum_context,
            "serverModelRevision": None,
            "serverModelRevisionStatus": "unverified_not_exposed_by_tinker_api",
            "remoteTokenizerResolvedHuggingFaceRevision": remote_tokenizer_commit,
            "remoteTokenizerRevisionIsServerWeightsAttestation": False,
        },
        "tokenizerComparison": comparison,
        "externalActions": {
            "authenticated": True,
            "apiKeySourceFormat": key_format,
            "apiKeyPersistedInReport": False,
            "operations": [
                "get_server_capabilities",
                "create_project_scoped_session",
                "create_base_model_sampling_client",
                "get_base_model",
                "get_tokenizer",
            ],
            "datumConstructed": False,
            "packedTokensTransmitted": False,
            "labelsTransmitted": False,
            "humanContentTransmitted": False,
            "samplingPerformed": False,
            "trainingPerformed": False,
            "checkpointCreated": False,
            "nextAction": "stop_for_review_before_data_bearing_overfit",
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if not comparison["compatible"]:
        raise TrainingContractError(
            f"remote tokenizer differs from frozen pack; inspect {output}"
        )
    print(
        "Tinker remote tokenizer preflight passed: complete vocabulary and "
        f"all {comparison['packTokenIDs']['uniqueIDs']} used token IDs match"
    )
    print(f"Report: {output}")
    print("Stopped before Datum construction, sampling, training, or data transfer.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, KeyError, TrainingContractError) as error:
        raise SystemExit(f"preflight-phase1-tinker-tokenizer: {error}")

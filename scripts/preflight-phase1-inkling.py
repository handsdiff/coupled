#!/usr/bin/env python3
"""Authenticated Inkling-Small compatibility preflight with no dataset calls."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.metadata
import json
import os
import uuid
from pathlib import Path
from typing import Any

from phase1_experiment import canonical_bytes
from phase1_inkling import (
    INKLING_MODEL,
    InklingContractError,
    load_jsonl,
    renderer_components,
    sha256,
)


PREFLIGHT_VERSION = "phase1-inkling-preflight-v1"


def iso8601() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def load_api_key(path: Path) -> str:
    values: dict[str, str] = {}
    content = path.expanduser().read_text(encoding="utf-8").strip()
    if "\n" not in content and "=" not in content:
        return content
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


def decoder_value(tokenizer: Any, token_id: int, *, remote: bool) -> str:
    if remote:
        try:
            return tokenizer.decode([token_id], skip_special_tokens=False)
        except TypeError:
            return tokenizer.decode([token_id])
    return tokenizer.decode([token_id])


def mapping_hash(tokenizer: Any, token_ids: list[int], *, remote: bool) -> str:
    digest = hashlib.sha256()
    for token_id in token_ids:
        value = [token_id, decoder_value(tokenizer, token_id, remote=remote)]
        digest.update(
            (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        )
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inkling-pack", required=True, type=Path)
    parser.add_argument("--provider-plan", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--dedicated-private-project-id", required=True)
    parser.add_argument("--confirm-authenticated-metadata-only", action="store_true")
    parser.add_argument("--execute", action="store_true")
    arguments = parser.parse_args()
    if not arguments.confirm_authenticated_metadata_only or not arguments.execute:
        parser.error("authenticated metadata-only confirmation and --execute are required")
    project_id = str(uuid.UUID(arguments.dedicated_private_project_id))
    output = arguments.output.expanduser().resolve()
    if output.exists():
        raise InklingContractError(f"output already exists: {output}")
    pack_path = arguments.inkling_pack.expanduser().resolve()
    plan_path = arguments.provider_plan.expanduser().resolve()
    packing = json.loads((pack_path / "packing.json").read_text(encoding="utf-8"))
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if not (
        plan.get("status") in {
            "awaiting_explicit_cost_ceiling",
            "authorized_for_execution",
        }
        and plan.get("tinker", {}).get("model") == INKLING_MODEL
        and plan.get("tinker", {}).get("projectID") == project_id
        and plan.get("source", {}).get("inklingPackingSHA256")
        == sha256(pack_path / "packing.json")
    ):
        raise InklingContractError("preflight inputs differ from the local plan")

    token_ids: set[int] = set()
    for condition in packing["renderer"]["reasoningConditions"]:
        for row in load_jsonl(pack_path / f"{condition}-packed-examples.jsonl"):
            token_ids.update(row["inputIDs"])
    ordered_ids = sorted(token_ids)
    _, local_tokenizer, _, _ = renderer_components()
    os.environ["TINKER_API_KEY"] = load_api_key(arguments.env_file)
    try:
        import tinker
    except ImportError as error:
        raise InklingContractError("Tinker SDK unavailable") from error
    service = tinker.ServiceClient(
        project_id=project_id,
        user_metadata={"purpose": "phase1-inkling-metadata-only-preflight"},
    )
    supported = [
        value
        for value in service.get_server_capabilities().supported_models
        if value.model_name == INKLING_MODEL
    ]
    maximum = max(
        value["maximumSequenceTokens"] for value in packing["counts"].values()
    )
    if len(supported) != 1 or (supported[0].max_context_length or 0) < maximum:
        raise InklingContractError("Inkling-Small or required context is unavailable")
    sampler = service.create_sampling_client(base_model=INKLING_MODEL)
    server_base_model = sampler.get_base_model()
    if server_base_model != INKLING_MODEL:
        raise InklingContractError(
            f"server sampler resolved an unexpected base model: {server_base_model}"
        )
    tokenizer_resolution = "tinker_sampling_client"
    try:
        remote_tokenizer = sampler.get_tokenizer()
    except ModuleNotFoundError as error:
        # Tinker 0.25.0 delegates Inkling tokenizer loading to an optional
        # tml_tokenizers module that is not published as a standalone package.
        # The official Cookbook resolves the same server-reported model through
        # tml-renderers, which is also the renderer used to freeze this pack.
        if error.name != "tml_tokenizers":
            raise
        from tinker_cookbook.tokenizer_utils import get_tokenizer

        remote_tokenizer = get_tokenizer(server_base_model)
        tokenizer_resolution = "tinker_cookbook_from_server_reported_model"
    local_hash = mapping_hash(local_tokenizer, ordered_ids, remote=False)
    remote_hash = mapping_hash(remote_tokenizer, ordered_ids, remote=True)
    mismatches = [
        token_id
        for token_id in ordered_ids
        if decoder_value(local_tokenizer, token_id, remote=False)
        != decoder_value(remote_tokenizer, token_id, remote=True)
    ]
    if mismatches:
        raise InklingContractError(
            f"remote Inkling tokenizer differs for {len(mismatches)} used token IDs"
        )
    report = {
        "schemaVersion": 1,
        "preflightVersion": PREFLIGHT_VERSION,
        "status": "passed",
        "completedAt": iso8601(),
        "projectID": project_id,
        "model": INKLING_MODEL,
        "maximumContextLength": supported[0].max_context_length,
        "requiredMaximumSequenceLength": maximum,
        "tokenizer": {
            "serverReportedBaseModel": server_base_model,
            "resolution": tokenizer_resolution,
            "usedTokenIDsCompared": len(ordered_ids),
            "minimumUsedTokenID": min(ordered_ids),
            "maximumUsedTokenID": max(ordered_ids),
            "localMappingSHA256": local_hash,
            "remoteMappingSHA256": remote_hash,
            "compatible": True,
        },
        "runtime": {
            "tinkerSDKVersion": importlib.metadata.version("tinker"),
            "tinkerCookbookVersion": importlib.metadata.version("tinker-cookbook"),
            "tmlRenderersVersion": importlib.metadata.version("tml-renderers"),
            "torchVersion": importlib.metadata.version("torch"),
        },
        "providerOperations": {
            "capabilitiesRequests": 1,
            "samplingClientCreations": 1,
            "datasetExamplesSubmitted": 0,
            "nllCalls": 0,
            "sampleCalls": 0,
            "trainingCalls": 0,
            "checkpointSaves": 0,
        },
        "source": {
            "providerPlanSHA256": sha256(plan_path),
            "inklingPackingSHA256": sha256(pack_path / "packing.json"),
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(canonical_bytes(report))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

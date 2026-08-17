#!/usr/bin/env python3
"""Provider-neutral validation for tokenizer-packed Phase 1 training data.

This module deliberately has no training-provider dependency. It validates the
frozen pack and converts each aligned Hugging Face-style row into the shifted
causal representation expected by token-level training APIs such as Tinker.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


IGNORE_LABEL = -100
PACKER_VERSION = "phase1-token-pack-v4"
PACK_SCHEMA_VERSION = 4


class TrainingContractError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def token_ids_sha256(token_ids: list[int]) -> str:
    encoded = json.dumps(token_ids, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256((encoded + "\n").encode()).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TrainingContractError(f"cannot read JSON from {path}: {error}") from error
    if not isinstance(value, dict):
        raise TrainingContractError(f"{path} must contain a JSON object")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise TrainingContractError(
                        f"{path}:{line_number} is not a JSON object"
                    )
                rows.append(value)
    except (OSError, json.JSONDecodeError) as error:
        raise TrainingContractError(f"cannot read JSONL from {path}: {error}") from error
    return rows


def require_int_list(value: Any, location: str) -> list[int]:
    if not isinstance(value, list) or not all(type(item) is int for item in value):
        raise TrainingContractError(f"{location} must be an integer array")
    return value


@dataclass(frozen=True)
class PackedDataset:
    directory: Path
    manifest: dict[str, Any]
    rows: list[dict[str, Any]]
    eos_token_id: int
    pad_token_id: int
    paste_marker: str
    paste_marker_token_ids: list[int]
    maximum_sequence_length: int


@dataclass(frozen=True)
class TinkerDatumContract:
    """Provider-neutral mirror of one Tinker cross-entropy Datum.

    `model_input_token_ids` maps to `Datum.model_input`.
    `target_tokens` and `weights` map to `loss_fn_inputs`.
    """

    example_id: str
    model_input_token_ids: list[int]
    target_tokens: list[int]
    weights: list[float]
    weighted_positions: int

    @property
    def length(self) -> int:
        return len(self.model_input_token_ids)


def validate_packed_dataset(directory: Path) -> PackedDataset:
    directory = directory.expanduser().resolve()
    manifest_path = directory / "packing.json"
    rows_path = directory / "packed-examples.jsonl"
    tokenizer_path = directory / "tokenizer"
    if not manifest_path.is_file() or not rows_path.is_file() or not tokenizer_path.is_dir():
        raise TrainingContractError(f"{directory} is not a packed Phase 1 dataset")

    manifest = load_json(manifest_path)
    if (
        manifest.get("schemaVersion") != PACK_SCHEMA_VERSION
        or manifest.get("packerVersion") != PACKER_VERSION
    ):
        raise TrainingContractError(
            f"training contract requires {PACKER_VERSION} schema {PACK_SCHEMA_VERSION}"
        )
    expected_digest = manifest.get("artifactDigestsSHA256", {}).get(
        "packed-examples.jsonl"
    )
    if not isinstance(expected_digest, str) or sha256(rows_path) != expected_digest:
        raise TrainingContractError(
            "packed-examples.jsonl digest does not match packing.json"
        )

    tokenizer = manifest.get("tokenizer")
    if not isinstance(tokenizer, dict):
        raise TrainingContractError("packing.json is missing tokenizer metadata")
    eos_token_id = tokenizer.get("eosTokenID")
    pad_token_id = tokenizer.get("padTokenID")
    paste_marker = tokenizer.get("pasteMarker")
    paste_ids = require_int_list(
        tokenizer.get("pasteMarkerTokenIDs"), "tokenizer.pasteMarkerTokenIDs"
    )
    if type(eos_token_id) is not int or type(pad_token_id) is not int:
        raise TrainingContractError("packing.json must define integer EOS and padding IDs")
    if not isinstance(paste_marker, str) or not paste_marker or not paste_ids:
        raise TrainingContractError("packing.json must define a nonempty paste marker")
    if tokenizer.get("pasteMarkerTokenCount") != len(paste_ids):
        raise TrainingContractError("paste marker token count is inconsistent")

    saved_digests = tokenizer.get("savedFileDigestsSHA256")
    if not isinstance(saved_digests, dict) or not saved_digests:
        raise TrainingContractError("packing.json is missing saved tokenizer digests")
    for name, expected in saved_digests.items():
        if not isinstance(name, str) or not isinstance(expected, str):
            raise TrainingContractError("saved tokenizer digests are malformed")
        path = tokenizer_path / name
        if not path.is_file() or sha256(path) != expected:
            raise TrainingContractError(f"saved tokenizer file does not match: {name}")

    rows = load_jsonl(rows_path)
    expected_count = manifest.get("counts", {}).get("examples")
    if expected_count != len(rows) or not rows:
        raise TrainingContractError("packed example count does not match packing.json")

    maximum_sequence_length = 0
    example_ids: set[str] = set()
    total_model_input_tokens = 0
    total_target_tokens = 0
    total_paste_actions = 0
    for index, row in enumerate(rows):
        location = f"packed-examples.jsonl row {index + 1}"
        if (
            row.get("schemaVersion") != PACK_SCHEMA_VERSION
            or row.get("packerVersion") != PACKER_VERSION
        ):
            raise TrainingContractError(f"{location} has an incompatible schema")
        example_id = row.get("exampleID")
        if not isinstance(example_id, str) or not example_id or example_id in example_ids:
            raise TrainingContractError(f"{location} has a missing or duplicate exampleID")
        example_ids.add(example_id)

        input_ids = require_int_list(row.get("inputIDs"), f"{location}.inputIDs")
        attention = require_int_list(row.get("attentionMask"), f"{location}.attentionMask")
        labels = require_int_list(row.get("labels"), f"{location}.labels")
        if len(input_ids) < 2 or not (
            len(input_ids) == len(attention) == len(labels)
        ):
            raise TrainingContractError(
                f"{location} arrays must have equal lengths of at least two"
            )
        if any(value != 1 for value in attention):
            raise TrainingContractError(f"{location} must be unpadded before training")

        model_input_count = row.get("modelInputTokenCount")
        target_count = row.get("targetTokenCount")
        if type(model_input_count) is not int or type(target_count) is not int:
            raise TrainingContractError(f"{location} has invalid token counts")
        if (
            model_input_count <= 0
            or target_count <= 0
            or model_input_count + target_count != len(input_ids)
        ):
            raise TrainingContractError(f"{location} token counts do not cover the sequence")
        if labels[:model_input_count] != [IGNORE_LABEL] * model_input_count:
            raise TrainingContractError(f"{location} applies loss inside model input")
        if labels[model_input_count:] != input_ids[model_input_count:]:
            raise TrainingContractError(f"{location} target labels differ from token IDs")
        if labels[-1] != eos_token_id or input_ids[-1] != eos_token_id:
            raise TrainingContractError(f"{location} does not end in a loss-bearing EOS")
        if eos_token_id in labels[model_input_count:-1]:
            raise TrainingContractError(f"{location} contains an early loss-bearing EOS")
        if sum(label != IGNORE_LABEL for label in labels) != target_count:
            raise TrainingContractError(f"{location} target count differs from its mask")

        spans = row.get("targetSegmentTokenSpans")
        if not isinstance(spans, list) or not spans:
            raise TrainingContractError(f"{location} is missing target segment spans")
        segment_cursor = 0
        observed_pastes = 0
        target_without_eos = input_ids[model_input_count:-1]
        for segment_index, span in enumerate(spans):
            if not isinstance(span, dict):
                raise TrainingContractError(f"{location} has an invalid target segment")
            start = span.get("targetTokenStart")
            end = span.get("targetTokenEnd")
            segment_type = span.get("type")
            if (
                span.get("segmentIndex") != segment_index
                or type(start) is not int
                or type(end) is not int
                or start != segment_cursor
                or end <= start
                or end > len(target_without_eos)
                or segment_type not in {"authored_text", "paste"}
            ):
                raise TrainingContractError(f"{location} target spans are not contiguous")
            if segment_type == "paste":
                if target_without_eos[start:end] != paste_ids:
                    raise TrainingContractError(f"{location} paste span has wrong token IDs")
                observed_pastes += 1
            segment_cursor = end
        if segment_cursor != len(target_without_eos):
            raise TrainingContractError(f"{location} spans do not cover the target before EOS")
        paste_count = row.get("pasteActionCount")
        if type(paste_count) is not int or paste_count != observed_pastes:
            raise TrainingContractError(f"{location} paste action count is inconsistent")

        maximum_sequence_length = max(maximum_sequence_length, len(input_ids))
        total_model_input_tokens += model_input_count
        total_target_tokens += target_count
        total_paste_actions += paste_count

    counts = manifest.get("counts", {})
    expected_counts = {
        "modelInputTokens": total_model_input_tokens,
        "targetTokens": total_target_tokens,
        "pasteActions": total_paste_actions,
        "maximumPackedSequenceTokens": maximum_sequence_length,
    }
    for key, value in expected_counts.items():
        if counts.get(key) != value:
            raise TrainingContractError(f"packing.json count disagrees: {key}")
    required_capacity = (
        manifest.get("packing", {})
        .get("sequenceLengthContract", {})
        .get("requiredTrainerSequenceCapacity")
    )
    if required_capacity != maximum_sequence_length:
        raise TrainingContractError("packing sequence-length contract is inconsistent")

    return PackedDataset(
        directory=directory,
        manifest=manifest,
        rows=rows,
        eos_token_id=eos_token_id,
        pad_token_id=pad_token_id,
        paste_marker=paste_marker,
        paste_marker_token_ids=paste_ids,
        maximum_sequence_length=maximum_sequence_length,
    )


def adapt_row_to_tinker(row: dict[str, Any]) -> TinkerDatumContract:
    """Shift one aligned packed row and prove every causal label position."""

    example_id = row.get("exampleID")
    if not isinstance(example_id, str) or not example_id:
        raise TrainingContractError("cannot adapt row without an exampleID")
    input_ids = require_int_list(row.get("inputIDs"), f"{example_id}.inputIDs")
    labels = require_int_list(row.get("labels"), f"{example_id}.labels")
    if len(input_ids) < 2 or len(input_ids) != len(labels):
        raise TrainingContractError(f"{example_id} cannot be causally shifted")

    model_input = input_ids[:-1]
    target_tokens = input_ids[1:]
    shifted_labels = labels[1:]
    weights = [0.0 if label == IGNORE_LABEL else 1.0 for label in shifted_labels]
    if not (len(model_input) == len(target_tokens) == len(weights)):
        raise TrainingContractError(f"{example_id} shifted arrays have different lengths")

    weighted_positions = 0
    for index, (target, label, weight) in enumerate(
        zip(target_tokens, shifted_labels, weights, strict=True)
    ):
        if weight == 1.0:
            weighted_positions += 1
            if label == IGNORE_LABEL or target != label:
                raise TrainingContractError(
                    f"{example_id} causal shift mismatch at weighted position {index}"
                )
        elif weight == 0.0:
            if label != IGNORE_LABEL:
                raise TrainingContractError(
                    f"{example_id} omitted a loss-bearing position {index}"
                )
        else:
            raise TrainingContractError(f"{example_id} has a non-binary weight")
    if weighted_positions != row.get("targetTokenCount"):
        raise TrainingContractError(
            f"{example_id} shifted weights disagree with targetTokenCount"
        )
    model_input_count = row.get("modelInputTokenCount")
    if type(model_input_count) is int:
        weighted_indexes = [
            index for index, weight in enumerate(weights) if weight == 1.0
        ]
        expected_indexes = list(range(model_input_count - 1, len(weights)))
        if weighted_indexes != expected_indexes:
            raise TrainingContractError(
                f"{example_id} shifted loss does not begin at the causal boundary"
            )
    return TinkerDatumContract(
        example_id=example_id,
        model_input_token_ids=model_input,
        target_tokens=target_tokens,
        weights=weights,
        weighted_positions=weighted_positions,
    )


def adapt_dataset_to_tinker(dataset: PackedDataset) -> list[TinkerDatumContract]:
    datums = [adapt_row_to_tinker(row) for row in dataset.rows]
    if sum(datum.weighted_positions for datum in datums) != dataset.manifest["counts"][
        "targetTokens"
    ]:
        raise TrainingContractError("shifted dataset changed the number of weighted tokens")
    return datums


def validate_local_frozen_tokenizer(dataset: PackedDataset) -> dict[str, Any]:
    """Validate only the tokenizer files frozen beside the pack.

    This is intentionally not described as server compatibility. The remote
    model tokenizer requires a later authenticated preflight.
    """

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    try:
        import transformers
        from transformers import AutoTokenizer
    except ImportError as error:
        raise TrainingContractError(
            "local tokenizer validation requires transformers; use the tokenizer venv"
        ) from error

    tokenizer = AutoTokenizer.from_pretrained(
        dataset.directory / "tokenizer",
        local_files_only=True,
    )
    metadata = dataset.manifest["tokenizer"]
    if len(tokenizer) != metadata.get("savedVocabularySize"):
        raise TrainingContractError("local tokenizer length differs from the frozen manifest")
    if tokenizer.eos_token_id != dataset.eos_token_id:
        raise TrainingContractError("local tokenizer EOS differs from the frozen manifest")
    if tokenizer.pad_token_id != dataset.pad_token_id:
        raise TrainingContractError("local tokenizer padding differs from the frozen manifest")
    paste_ids = tokenizer.encode(dataset.paste_marker, add_special_tokens=False)
    if paste_ids != dataset.paste_marker_token_ids:
        raise TrainingContractError("local tokenizer changed the paste marker encoding")

    representative_strings = [
        "Phase 1 causal write prediction",
        "caf\u00e9 \U0001f642",
        dataset.paste_marker,
    ]
    representatives: list[dict[str, Any]] = []
    for text in representative_strings:
        token_ids = tokenizer.encode(text, add_special_tokens=False)
        decoded = tokenizer.decode(token_ids, skip_special_tokens=False)
        if decoded != text:
            raise TrainingContractError(
                f"local tokenizer failed representative round trip: {text!r}"
            )
        representatives.append(
            {
                "text": text,
                "tokenIDs": token_ids,
                "decoded": decoded,
                "roundTripExact": True,
            }
        )
    return {
        "scope": "local_frozen_tokenizer_only",
        "status": "passed",
        "transformersVersion": transformers.__version__,
        "tokenizerClass": type(tokenizer).__name__,
        "tokenizerLength": len(tokenizer),
        "baseVocabularySize": tokenizer.vocab_size,
        "eosTokenID": tokenizer.eos_token_id,
        "padTokenID": tokenizer.pad_token_id,
        "pasteMarkerTokenIDs": paste_ids,
        "representativeRoundTrips": representatives,
    }


def git_revision(project_directory: Path) -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_directory,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def git_worktree_dirty(project_directory: Path) -> bool | None:
    try:
        return bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=project_directory,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        return None

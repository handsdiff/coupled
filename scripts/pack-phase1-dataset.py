#!/usr/bin/env python3
"""Pack a compiled Phase 1 dataset with the selected model tokenizer.

The causal compiler deliberately stores text and structured target segments.
This script is the tokenizer-specific boundary: it left-truncates model input,
turns each proven paste action into a reserved marker encoded by the unchanged
tokenizer, appends one EOS, and constructs causal-LM labels. It never modifies
the compiled source dataset.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

try:
    import huggingface_hub
    import tokenizers
    import transformers
    from huggingface_hub import snapshot_download
    from transformers import AutoTokenizer
except ImportError as error:
    raise SystemExit(
        "Tokenizer dependencies are missing. Run:\n"
        "  uv venv .build/tokenizer-venv\n"
        "  uv pip install --python .build/tokenizer-venv/bin/python "
        "-r scripts/tokenizer-requirements.txt"
    ) from error


PACKER_VERSION = "phase1-token-pack-v2"
DEFAULT_TOKENIZER = "Qwen/Qwen3.5-9B-Base"
DEFAULT_PASTE_MARKER = "<|paste|>"
IGNORE_LABEL = -100
TOKENIZER_FILES = (
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.json",
    "merges.txt",
    "special_tokens_map.json",
    "added_tokens.json",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {error}") from error
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected a JSON object")
            rows.append(value)
    return rows


def require_compiled_dataset(
    source: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    manifest_path = source / "dataset.json"
    examples_path = source / "examples.jsonl"
    events_path = source / "events.jsonl"
    if not manifest_path.is_file() or not examples_path.is_file() or not events_path.is_file():
        raise ValueError(f"{source} is not a compiled Phase 1 dataset")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    examples = load_jsonl(examples_path)
    if manifest.get("conversionVersion") != "phase1-causal-v9":
        raise ValueError("packer requires conversionVersion phase1-causal-v9")
    if manifest.get("serialization", {}).get("targetFormat") != "structured_authorship_segments":
        raise ValueError("packer requires structured authorship targets")
    if manifest.get("counts", {}).get("examples") != len(examples):
        raise ValueError("compiled example count does not match dataset.json")
    events = load_jsonl(events_path)
    events_by_id = {event["sourceEventID"]: event for event in events}
    if len(events_by_id) != len(events):
        raise ValueError("compiled events contain duplicate sourceEventID values")
    return manifest, examples, events_by_id


def resolve_tokenizer_snapshot(
    repository: str, revision: str, local_files_only: bool
) -> tuple[Path, str]:
    snapshot = Path(
        snapshot_download(
            repo_id=repository,
            revision=revision,
            allow_patterns=list(TOKENIZER_FILES),
            local_files_only=local_files_only,
        )
    ).resolve()
    resolved_revision = snapshot.name
    if len(resolved_revision) != 40:
        raise ValueError(f"could not resolve tokenizer commit from {snapshot}")
    return snapshot, resolved_revision


def encode_plain_text(tokenizer: Any, text: str) -> list[int]:
    # split_special_tokens=True is set when loading this tokenizer. Therefore
    # marker-shaped human text and the reserved paste marker both use the
    # unchanged vocabulary. Only the loader-appended terminator becomes EOS.
    return list(tokenizer.encode(text, add_special_tokens=False))


def token_ids_sha256(token_ids: list[int]) -> str:
    return hashlib.sha256(json_bytes(token_ids)).hexdigest()


def verify_resolved_target_event(
    example: dict[str, Any], event: dict[str, Any]
) -> int:
    """Prove target paste payloads remain resolved in the historical WRITE."""
    serialized = json.loads(event["serialized"])
    target = example["target"]
    if serialized.get("content") != target.get("resolvedContent"):
        raise ValueError(f"example {example['exampleID']} resolved content disagrees with WRITE")
    target_segments = target.get("segments", [])
    if not any(segment.get("type") == "paste" for segment in target_segments):
        # Legacy sessions predate structured collector authorship. Their v9
        # targets are conservatively synthesized as authored text, so content
        # equality is the complete historical-preservation check available.
        return 0
    history_segments = serialized.get("authorshipSegments", [])
    if len(target_segments) != len(history_segments):
        raise ValueError(f"example {example['exampleID']} authorship segment count disagrees")
    preserved_pastes = 0
    for target_segment, history_segment in zip(target_segments, history_segments):
        if target_segment.get("type") != history_segment.get("type"):
            raise ValueError(f"example {example['exampleID']} authorship segment type disagrees")
        if target_segment.get("type") == "authored_text":
            if target_segment.get("content") != history_segment.get("content"):
                raise ValueError(f"example {example['exampleID']} authored segment disagrees")
        elif target_segment.get("type") == "paste":
            for key in ("clipboardSnapshotID", "pasteCheckpointID"):
                if target_segment.get(key) != history_segment.get(key):
                    raise ValueError(f"example {example['exampleID']} paste grounding disagrees")
            if not isinstance(history_segment.get("content"), str):
                raise ValueError(f"example {example['exampleID']} historical paste lost payload")
            if "content" in target_segment:
                raise ValueError(f"example {example['exampleID']} target paste contains payload")
            preserved_pastes += 1
    return preserved_pastes


def pack_target(
    segments: list[dict[str, Any]],
    plain_text_tokenizer: Any,
    paste_marker_token_ids: list[int],
    eos_token_id: int,
) -> tuple[list[int], list[dict[str, Any]], int]:
    target_ids: list[int] = []
    spans: list[dict[str, Any]] = []
    paste_count = 0
    for segment_index, segment in enumerate(segments):
        segment_type = segment.get("type")
        start = len(target_ids)
        if segment_type == "authored_text":
            content = segment.get("content")
            if not isinstance(content, str):
                raise ValueError(f"target segment {segment_index} has no string content")
            ids = encode_plain_text(plain_text_tokenizer, content)
            if eos_token_id in ids:
                raise ValueError("authored text unexpectedly encoded as structural EOS")
            target_ids.extend(ids)
        elif segment_type == "paste":
            if segment.get("content") is not None:
                raise ValueError("paste target segment must not contain pasted payload")
            if not segment.get("clipboardSnapshotID") or not segment.get("pasteCheckpointID"):
                raise ValueError("paste target segment is not grounded")
            target_ids.extend(paste_marker_token_ids)
            paste_count += 1
        else:
            raise ValueError(f"unknown target segment type: {segment_type!r}")
        spans.append(
            {
                "segmentIndex": segment_index,
                "type": segment_type,
                "targetTokenStart": start,
                "targetTokenEnd": len(target_ids),
            }
        )
    target_ids.append(eos_token_id)
    if not target_ids or target_ids[-1] != eos_token_id:
        raise AssertionError("target does not end in EOS")
    if target_ids.count(eos_token_id) != 1:
        raise ValueError("target must contain exactly one structural EOS")
    return target_ids, spans, paste_count


def validate_padded_batch(records: list[dict[str, Any]], pad_token_id: int) -> dict[str, Any]:
    sample = records[: min(4, len(records))]
    if not sample:
        return {"exampleCount": 0, "passed": True}
    maximum = max(len(record["inputIDs"]) for record in sample)
    for record in sample:
        amount = maximum - len(record["inputIDs"])
        padded_input = record["inputIDs"] + [pad_token_id] * amount
        padded_labels = record["labels"] + [IGNORE_LABEL] * amount
        attention = record["attentionMask"] + [0] * amount
        if not (len(padded_input) == len(padded_labels) == len(attention) == maximum):
            raise AssertionError("padded batch lengths disagree")
        if amount and (padded_labels[-amount:] != [IGNORE_LABEL] * amount):
            raise AssertionError("padding labels are not ignored")
        if amount and attention[-amount:] != [0] * amount:
            raise AssertionError("padding is not masked from attention")
    return {"exampleCount": len(sample), "paddedLength": maximum, "passed": True}


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("wb") as handle:
        for row in rows:
            handle.write(json_bytes(row))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="compiled Phase 1 dataset")
    parser.add_argument("--output", required=True, type=Path, help="fresh packed output directory")
    parser.add_argument("--tokenizer", default=DEFAULT_TOKENIZER)
    parser.add_argument("--revision", default="main")
    parser.add_argument("--input-token-budget", type=int, default=32768)
    parser.add_argument("--paste-marker", default=DEFAULT_PASTE_MARKER)
    parser.add_argument("--local-files-only", action="store_true")
    arguments = parser.parse_args()

    source = arguments.input.expanduser().resolve()
    output = arguments.output.expanduser().resolve()
    if arguments.input_token_budget <= 0:
        raise ValueError("--input-token-budget must be positive")
    if output.exists():
        raise ValueError(f"output already exists: {output}; use a fresh directory")

    source_manifest, examples, events_by_id = require_compiled_dataset(source)
    snapshot, resolved_revision = resolve_tokenizer_snapshot(
        arguments.tokenizer, arguments.revision, arguments.local_files_only
    )

    source_tokenizer = AutoTokenizer.from_pretrained(snapshot, use_fast=True)
    plain_text_tokenizer = AutoTokenizer.from_pretrained(
        snapshot, use_fast=True, split_special_tokens=True
    )
    original_vocabulary_size = len(source_tokenizer)
    eos_token_id = source_tokenizer.eos_token_id
    if eos_token_id is None:
        raise ValueError("selected tokenizer has no eos_token_id")
    if (
        len(plain_text_tokenizer) != original_vocabulary_size
        or plain_text_tokenizer.eos_token_id != eos_token_id
    ):
        raise ValueError("plain-text tokenizer does not preserve base vocabulary and EOS")
    paste_marker_token_ids = encode_plain_text(
        plain_text_tokenizer, arguments.paste_marker
    )
    if not paste_marker_token_ids:
        raise ValueError("paste marker encodes to no tokens")
    if eos_token_id in paste_marker_token_ids:
        raise ValueError("paste marker encoding contains structural EOS")

    temporary_parent = output.parent
    temporary_parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=temporary_parent))
    try:
        tokenizer_directory = temporary / "tokenizer"
        source_tokenizer.save_pretrained(tokenizer_directory)

        reloaded_source = AutoTokenizer.from_pretrained(tokenizer_directory, use_fast=True)
        reloaded_plain = AutoTokenizer.from_pretrained(
            tokenizer_directory, use_fast=True, split_special_tokens=True
        )
        if (
            len(reloaded_source) != original_vocabulary_size
            or len(reloaded_plain) != original_vocabulary_size
        ):
            raise AssertionError("saved tokenizer vocabulary size changed")
        if (
            reloaded_source.eos_token_id != eos_token_id
            or reloaded_plain.eos_token_id != eos_token_id
        ):
            raise AssertionError("saved tokenizer changed the EOS token ID")
        if encode_plain_text(reloaded_plain, arguments.paste_marker) != paste_marker_token_ids:
            raise AssertionError("saved tokenizer changed the paste marker encoding")
        literal_eos_ids = encode_plain_text(reloaded_plain, reloaded_plain.eos_token)
        if eos_token_id in literal_eos_ids:
            raise AssertionError("literal EOS-shaped human text became structural EOS")

        packed_records: list[dict[str, Any]] = []
        total_paste_actions = 0
        total_input_tokens = 0
        total_input_tokens_discarded = 0
        total_target_tokens = 0
        maximum_input_tokens_before_truncation = 0
        maximum_query_tokens = 0
        maximum_sequence_tokens = 0
        resolved_paste_payloads_preserved_in_history = 0
        for example in examples:
            model_input = example.get("modelInput")
            query = example.get("query")
            segments = example.get("target", {}).get("segments")
            if (
                not isinstance(model_input, str)
                or not isinstance(query, str)
                or not isinstance(segments, list)
            ):
                raise ValueError(f"example {example.get('exampleID')} has invalid input or target")
            target_event = events_by_id.get(example["targetEventID"])
            if target_event is None:
                raise ValueError(f"example {example['exampleID']} has no compiled target event")
            resolved_paste_payloads_preserved_in_history += verify_resolved_target_event(
                example, target_event
            )
            complete_input_ids = encode_plain_text(reloaded_plain, model_input)
            query_ids = encode_plain_text(reloaded_plain, query)
            if not query_ids:
                raise ValueError(f"example {example['exampleID']} has an empty conditioning query")
            if len(query_ids) > arguments.input_token_budget:
                raise ValueError(f"example {example['exampleID']} query exceeds input token budget")
            input_ids = complete_input_ids[-arguments.input_token_budget :]
            discarded = len(complete_input_ids) - len(input_ids)
            if input_ids[-len(query_ids) :] != query_ids:
                raise AssertionError("right-edge conditioning query was not preserved")
            target_ids, segment_spans, paste_count = pack_target(
                segments, reloaded_plain, paste_marker_token_ids, eos_token_id
            )
            combined_ids = input_ids + target_ids
            labels = [IGNORE_LABEL] * len(input_ids) + target_ids
            attention_mask = [1] * len(combined_ids)
            if not (len(combined_ids) == len(labels) == len(attention_mask)):
                raise AssertionError("packed sequence arrays have different lengths")
            if any(label != IGNORE_LABEL for label in labels[: len(input_ids)]):
                raise AssertionError("model-input tokens receive loss")
            if labels[len(input_ids) :] != target_ids:
                raise AssertionError("target labels do not match target tokens")
            if combined_ids[-1] != eos_token_id or labels[-1] != eos_token_id:
                raise AssertionError("EOS is absent from input or loss")
            if input_ids != complete_input_ids[-len(input_ids) :]:
                raise AssertionError("input truncation did not retain the right edge")

            record = {
                "schemaVersion": 2,
                "packerVersion": PACKER_VERSION,
                "exampleID": example["exampleID"],
                "sessionID": example["sessionID"],
                "targetEventID": example["targetEventID"],
                "inputIDs": combined_ids,
                "labels": labels,
                "attentionMask": attention_mask,
                "modelInputTokenCountBeforeTruncation": len(complete_input_ids),
                "modelInputTokenCount": len(input_ids),
                "discardedModelInputTokenCount": discarded,
                "rightEdgeQueryTokenCount": len(query_ids),
                "rightEdgeQueryTokenSHA256": token_ids_sha256(query_ids),
                "targetTokenCount": len(target_ids),
                "pasteActionCount": paste_count,
                "pasteMarkerTokenCount": len(paste_marker_token_ids),
                "targetSegmentTokenSpans": segment_spans,
            }
            packed_records.append(record)
            total_paste_actions += paste_count
            total_input_tokens += len(input_ids)
            total_input_tokens_discarded += discarded
            total_target_tokens += len(target_ids)
            maximum_input_tokens_before_truncation = max(
                maximum_input_tokens_before_truncation, len(complete_input_ids)
            )
            maximum_query_tokens = max(maximum_query_tokens, len(query_ids))
            maximum_sequence_tokens = max(maximum_sequence_tokens, len(combined_ids))

        padding_audit = validate_padded_batch(
            packed_records,
            eos_token_id
            if reloaded_source.pad_token_id is None
            else reloaded_source.pad_token_id,
        )
        packed_path = temporary / "packed-examples.jsonl"
        write_jsonl(packed_path, packed_records)

        tokenizer_digests = {
            str(path.relative_to(tokenizer_directory)): sha256(path)
            for path in sorted(tokenizer_directory.rglob("*"))
            if path.is_file()
        }
        manifest = {
            "schemaVersion": 2,
            "packerVersion": PACKER_VERSION,
            "createdAt": dt.datetime.now(dt.timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
            "source": {
                "directory": str(source),
                "conversionVersion": source_manifest["conversionVersion"],
                "sessionID": source_manifest["sessionID"],
                "digestsSHA256": {
                    "dataset.json": sha256(source / "dataset.json"),
                    "examples.jsonl": sha256(source / "examples.jsonl"),
                    "events.jsonl": sha256(source / "events.jsonl"),
                },
            },
            "tokenizer": {
                "repository": arguments.tokenizer,
                "requestedRevision": arguments.revision,
                "resolvedRevision": resolved_revision,
                "class": type(reloaded_source).__name__,
                "originalVocabularySize": original_vocabulary_size,
                "savedVocabularySize": len(reloaded_source),
                "pasteMarker": arguments.paste_marker,
                "pasteMarkerTokenIDs": paste_marker_token_ids,
                "pasteMarkerTokenCount": len(paste_marker_token_ids),
                "eosToken": reloaded_source.eos_token,
                "eosTokenID": eos_token_id,
                "padToken": reloaded_source.pad_token,
                "padTokenID": reloaded_source.pad_token_id,
                "savedFileDigestsSHA256": tokenizer_digests,
            },
            "runtime": {
                "python": sys.version.split()[0],
                "transformers": transformers.__version__,
                "tokenizers": tokenizers.__version__,
                "huggingfaceHub": huggingface_hub.__version__,
            },
            "packing": {
                "inputTokenBudget": arguments.input_token_budget,
                "inputTruncation": "left_truncate_retain_right_edge",
                "rightEdgeConditioningQueryPreserved": True,
                "ordinaryTextSpecialTokenHandling": "split_special_tokens",
                "automaticSpecialTokens": False,
                "targetConstruction": "authored_text_plus_reserved_paste_marker_string_plus_one_eos",
                "labelMask": {
                    "modelInput": IGNORE_LABEL,
                    "padding": IGNORE_LABEL,
                    "authoredTextReceivesLoss": True,
                    "pasteMarkerTokensReceiveLoss": True,
                    "eosReceivesLoss": True,
                    "pastedPayloadPresentInTarget": False,
                },
                "paddingSide": "right",
                "paddingAudit": padding_audit,
            },
            "counts": {
                "examples": len(packed_records),
                "pasteActions": total_paste_actions,
                "resolvedPastePayloadsPreservedInHistoricalWrites": (
                    resolved_paste_payloads_preserved_in_history
                ),
                "modelInputTokens": total_input_tokens,
                "discardedModelInputTokens": total_input_tokens_discarded,
                "targetTokens": total_target_tokens,
                "maximumModelInputTokensBeforeTruncation": maximum_input_tokens_before_truncation,
                "maximumRightEdgeQueryTokens": maximum_query_tokens,
                "maximumPackedSequenceTokens": maximum_sequence_tokens,
            },
        }
        manifest["artifactDigestsSHA256"] = {
            "packed-examples.jsonl": sha256(packed_path)
        }
        (temporary / "packing.json").write_bytes(json_bytes(manifest))
        os.replace(temporary, output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    print(f"Packed {len(examples)} examples into {output}")
    print(
        f"Tokenizer: {arguments.tokenizer}@{resolved_revision} "
        f"EOS={eos_token_id} PASTE_MARKER_TOKENS={paste_marker_token_ids}"
    )
    print(
        f"Paste actions: {total_paste_actions}; "
        f"discarded input tokens: {total_input_tokens_discarded}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f"pack-phase1-dataset: {error}", file=sys.stderr)
        raise SystemExit(1)

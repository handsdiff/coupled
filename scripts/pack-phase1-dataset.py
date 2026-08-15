#!/usr/bin/env python3
"""Pack a compiled Phase 1 dataset with the selected model tokenizer.

The causal compiler deliberately stores text and structured target segments.
This script is the tokenizer-specific boundary: it packs a suffix of complete
context-event blocks, turns each proven paste action into a reserved marker
encoded by the unchanged tokenizer, appends one EOS, and constructs causal-LM
labels. It never modifies the compiled source dataset.
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


PACKER_VERSION = "phase1-token-pack-v4"
DEFAULT_TOKENIZER = "Qwen/Qwen3.5-9B-Base"
DEFAULT_PASTE_MARKER = "<|paste|>"
CONTEXT_TRUNCATION_MARKER = "[...older event content truncated...]"
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
    if manifest.get("conversionVersion") not in {"phase1-causal-v11", "phase1-causal-v12"}:
        raise ValueError("packer requires conversionVersion phase1-causal-v11 or phase1-causal-v12")
    if manifest.get("serialization", {}).get("contextVersion") != 3:
        raise ValueError("packer requires model-facing contextVersion 3")
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


def canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def authorship_segment_suffix(
    segments: list[dict[str, Any]], retained_characters: int
) -> list[dict[str, str]]:
    remaining = retained_characters
    retained_reversed: list[dict[str, str]] = []
    for segment in reversed(segments):
        if remaining <= 0:
            break
        segment_characters = list(segment["content"])
        amount = min(remaining, len(segment_characters))
        if amount > 0:
            retained_reversed.append(
                {
                    "type": segment["type"],
                    "content": "".join(segment_characters[-amount:]),
                }
            )
            remaining -= amount
    return list(reversed(retained_reversed))


def truncate_oldest_event(
    serialized: str,
    maximum_tokens: int,
    tokenizer: Any,
) -> tuple[str, list[int], dict[str, Any]] | None:
    """Return valid JSON retaining one event's provenance-preserving text tail."""
    if maximum_tokens <= 0:
        return None
    value = json.loads(serialized)
    content = value.get("content")
    segments = value.get("authorshipSegments")
    if isinstance(content, str) and "authorshipSegments" not in value:
        representation = "content"
        resolved_content = content
    elif (
        "content" not in value
        and isinstance(segments, list)
        and segments
        and all(
            isinstance(segment, dict)
            and isinstance(segment.get("type"), str)
            and isinstance(segment.get("content"), str)
            for segment in segments
        )
    ):
        representation = "authorship_segments"
        resolved_content = "".join(segment["content"] for segment in segments)
    else:
        return None
    if not resolved_content:
        return None

    characters = list(resolved_content)

    def candidate(retained_characters: int) -> tuple[str, list[int]]:
        candidate_value = dict(value)
        candidate_value["contentTruncatedForPacking"] = True
        candidate_value["contentTruncationMarker"] = CONTEXT_TRUNCATION_MARKER
        suffix = "" if retained_characters == 0 else "".join(characters[-retained_characters:])
        if representation == "content":
            candidate_value["content"] = suffix
        else:
            candidate_value["authorshipSegments"] = authorship_segment_suffix(
                segments, retained_characters
            )
        text = canonical_json(candidate_value)
        return text, encode_plain_text(tokenizer, text + "\n")

    minimum_text, minimum_ids = candidate(0)
    if len(minimum_ids) > maximum_tokens:
        return None
    best_text, best_ids = minimum_text, minimum_ids
    low, high = 1, len(characters)
    while low <= high:
        middle = (low + high) // 2
        text, token_ids = candidate(middle)
        if len(token_ids) <= maximum_tokens:
            best_text, best_ids = text, token_ids
            low = middle + 1
        else:
            high = middle - 1
    retained_content = json.loads(best_text)
    if representation == "content":
        retained_character_count = len(list(retained_content["content"]))
    else:
        retained_character_count = sum(
            len(list(segment["content"]))
            for segment in retained_content["authorshipSegments"]
        )
    return best_text, best_ids, {
        "representation": representation,
        "originalContentCharacterCount": len(characters),
        "retainedContentCharacterCount": retained_character_count,
    }


def audit_truncation_implementations(tokenizer: Any) -> dict[str, Any]:
    """Exercise both legacy content and structured-authorship truncation paths."""
    structured_segments = [
        {"type": "authored_text", "content": "A" * 200},
        {"type": "paste", "content": "P" * 200},
        {"type": "authored_text", "content": "Z" * 200},
    ]
    fixtures = [
        {"kind": "read", "content": "R" * 600},
        {
            "kind": "write",
            "operation": "insert",
            "authorshipResolution": "resolved",
            "authorshipSegments": structured_segments,
        },
    ]
    results: dict[str, Any] = {}
    for fixture in fixtures:
        serialized = canonical_json(fixture)
        complete_count = len(encode_plain_text(tokenizer, serialized + "\n"))
        truncated = truncate_oldest_event(serialized, complete_count - 1, tokenizer)
        if truncated is None:
            raise AssertionError("truncation self-audit could not fit a content tail")
        text, token_ids, metadata = truncated
        packed = json.loads(text)
        if len(token_ids) > complete_count - 1:
            raise AssertionError("truncation self-audit exceeded its budget")
        if packed.get("contentTruncationMarker") != CONTEXT_TRUNCATION_MARKER:
            raise AssertionError("truncation self-audit lost its explicit marker")
        if metadata["representation"] == "content":
            retained = packed["content"]
            if not fixture["content"].endswith(retained):
                raise AssertionError("content truncation did not retain a suffix")
        else:
            if "content" in packed:
                raise AssertionError("structured truncation duplicated resolved content")
            retained_segments = packed["authorshipSegments"]
            retained = "".join(segment["content"] for segment in retained_segments)
            resolved = "".join(segment["content"] for segment in structured_segments)
            if not resolved.endswith(retained):
                raise AssertionError("structured truncation did not retain a text suffix")
            if retained_segments != authorship_segment_suffix(
                structured_segments, len(retained)
            ):
                raise AssertionError("structured truncation lost authorship boundaries")
        if not 0 < len(retained) < 600:
            raise AssertionError("truncation self-audit did not exercise a partial tail")
        results[metadata["representation"]] = {
            "completeTokenCount": complete_count,
            "packedTokenCount": len(token_ids),
            "retainedContentCharacterCount": len(retained),
        }
    return results


def pack_model_input(
    example: dict[str, Any],
    events_by_id: dict[str, dict[str, Any]],
    tokenizer: Any,
    token_budget: int,
) -> dict[str, Any]:
    query = example.get("query")
    context_event_ids = example.get("contextEventIDs")
    if not isinstance(query, str) or not isinstance(context_event_ids, list):
        raise ValueError(f"example {example.get('exampleID')} has invalid context lineage")
    query_ids = encode_plain_text(tokenizer, query)
    if not query_ids:
        raise ValueError(f"example {example['exampleID']} has an empty conditioning query")
    if len(query_ids) > token_budget:
        raise ValueError(f"example {example['exampleID']} query exceeds input token budget")

    blocks: list[dict[str, Any]] = []
    for event_id in context_event_ids:
        event = events_by_id.get(event_id)
        if event is None:
            raise ValueError(f"example {example['exampleID']} references missing event {event_id}")
        serialized = event.get("serialized")
        if not isinstance(serialized, str) or not isinstance(json.loads(serialized), dict):
            raise ValueError(f"compiled event {event_id} has invalid model serialization")
        blocks.append(
            {
                "eventID": event_id,
                "serialized": serialized,
                "tokenIDs": encode_plain_text(tokenizer, serialized + "\n"),
            }
        )
    expected_context = "\n".join(block["serialized"] for block in blocks)
    expected_model_input = query if not expected_context else expected_context + "\n" + query
    if example.get("context") != expected_context or example.get("modelInput") != expected_model_input:
        raise ValueError(f"example {example['exampleID']} context lineage is inconsistent")

    complete_token_count = len(query_ids) + sum(len(block["tokenIDs"]) for block in blocks)
    remaining = token_budget - len(query_ids)
    retained_reversed: list[dict[str, Any]] = []
    for block in reversed(blocks):
        block_ids = block["tokenIDs"]
        if len(block_ids) <= remaining:
            retained_reversed.append({**block, "contentTruncated": False})
            remaining -= len(block_ids)
            continue
        truncated = truncate_oldest_event(block["serialized"], remaining, tokenizer)
        if truncated is not None:
            truncated_text, truncated_ids, truncation = truncated
            retained_reversed.append(
                {
                    "eventID": block["eventID"],
                    "serialized": truncated_text,
                    "tokenIDs": truncated_ids,
                    "contentTruncated": True,
                    "truncation": truncation,
                }
            )
            remaining -= len(truncated_ids)
        break

    retained = list(reversed(retained_reversed))
    history_ids: list[int] = []
    spans: list[dict[str, Any]] = []
    for block in retained:
        start = len(history_ids)
        history_ids.extend(block["tokenIDs"])
        span = {
            "eventID": block["eventID"],
            "tokenStart": start,
            "tokenEnd": len(history_ids),
            "contentTruncated": block["contentTruncated"],
            "serializedSHA256": hashlib.sha256(block["serialized"].encode()).hexdigest(),
        }
        if block["contentTruncated"]:
            span["packedSerialized"] = block["serialized"]
            span["truncation"] = block["truncation"]
        spans.append(span)

    input_ids = history_ids + query_ids
    if len(input_ids) > token_budget:
        raise AssertionError("event-aware input exceeds token budget")
    if input_ids[-len(query_ids) :] != query_ids:
        raise AssertionError("right-edge conditioning query was not preserved")
    retained_ids = [span["eventID"] for span in spans]
    if retained_ids and context_event_ids[-len(retained_ids) :] != retained_ids:
        raise AssertionError("retained context events are not a chronological suffix")
    partial_count = sum(1 for span in spans if span["contentTruncated"])
    if partial_count > 1 or (partial_count and not spans[0]["contentTruncated"]):
        raise AssertionError("only the oldest retained event may be content-truncated")

    return {
        "inputIDs": input_ids,
        "queryIDs": query_ids,
        "completeTokenCount": complete_token_count,
        "historyTokenCount": len(history_ids),
        "unusedTokenBudget": token_budget - len(input_ids),
        "contextEventSpans": spans,
        "sourceContextEventCount": len(context_event_ids),
        "droppedContextEventCount": len(context_event_ids) - len(spans),
        "partiallyRetainedContextEventCount": partial_count,
    }


def verify_resolved_target_event(
    example: dict[str, Any], event: dict[str, Any]
) -> int:
    """Prove target paste payloads remain resolved in the historical WRITE."""
    serialized = json.loads(event["auditSerialized"])
    target = example["target"]
    if serialized.get("content") != target.get("resolvedContent"):
        raise ValueError(f"example {example['exampleID']} resolved content disagrees with WRITE")
    target_segments = target.get("segments", [])
    if not any(segment.get("type") == "paste" for segment in target_segments):
        # Legacy sessions predate structured collector authorship. Their
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
        truncation_self_audit = audit_truncation_implementations(reloaded_plain)

        packed_records: list[dict[str, Any]] = []
        total_paste_actions = 0
        total_input_tokens = 0
        total_input_tokens_discarded = 0
        total_target_tokens = 0
        maximum_input_tokens_before_truncation = 0
        maximum_query_tokens = 0
        maximum_sequence_tokens = 0
        maximum_unused_input_budget = 0
        total_dropped_context_events = 0
        total_partially_retained_context_events = 0
        resolved_paste_payloads_preserved_in_history = 0
        for example in examples:
            segments = example.get("target", {}).get("segments")
            if not isinstance(segments, list):
                raise ValueError(f"example {example.get('exampleID')} has invalid input or target")
            target_event = events_by_id.get(example["targetEventID"])
            if target_event is None:
                raise ValueError(f"example {example['exampleID']} has no compiled target event")
            resolved_paste_payloads_preserved_in_history += verify_resolved_target_event(
                example, target_event
            )
            packed_input = pack_model_input(
                example, events_by_id, reloaded_plain, arguments.input_token_budget
            )
            input_ids = packed_input["inputIDs"]
            query_ids = packed_input["queryIDs"]
            discarded = packed_input["completeTokenCount"] - len(input_ids)
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

            record = {
                "schemaVersion": 4,
                "packerVersion": PACKER_VERSION,
                "exampleID": example["exampleID"],
                "sessionID": example["sessionID"],
                "targetEventID": example["targetEventID"],
                "inputIDs": combined_ids,
                "labels": labels,
                "attentionMask": attention_mask,
                "modelInputTokenCountBeforePacking": packed_input["completeTokenCount"],
                "modelInputTokenCount": len(input_ids),
                "discardedModelInputTokenCount": discarded,
                "historyTokenCount": packed_input["historyTokenCount"],
                "rightEdgeQueryTokenCount": len(query_ids),
                "rightEdgeQueryTokenSHA256": token_ids_sha256(query_ids),
                "unusedModelInputTokenBudget": packed_input["unusedTokenBudget"],
                "sourceContextEventCount": packed_input["sourceContextEventCount"],
                "droppedContextEventCount": packed_input["droppedContextEventCount"],
                "partiallyRetainedContextEventCount": (
                    packed_input["partiallyRetainedContextEventCount"]
                ),
                "contextEventTokenSpans": packed_input["contextEventSpans"],
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
                maximum_input_tokens_before_truncation, packed_input["completeTokenCount"]
            )
            maximum_query_tokens = max(maximum_query_tokens, len(query_ids))
            maximum_sequence_tokens = max(maximum_sequence_tokens, len(combined_ids))
            maximum_unused_input_budget = max(
                maximum_unused_input_budget, packed_input["unusedTokenBudget"]
            )
            total_dropped_context_events += packed_input["droppedContextEventCount"]
            total_partially_retained_context_events += (
                packed_input["partiallyRetainedContextEventCount"]
            )

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
            "schemaVersion": 4,
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
                "sequenceLengthContract": {
                    "inputBudgetAppliesTo": "history_plus_conditioning_query_only",
                    "targetAppendedOutsideInputBudget": True,
                    "targetTruncationAllowed": False,
                    "requiredTrainerSequenceCapacity": maximum_sequence_tokens,
                },
                "inputTruncation": "newest_complete_event_blocks_then_explicit_oldest_event_tail",
                "rightEdgeConditioningQueryPreserved": True,
                "eventBlock": "one canonical context event followed by newline",
                "eventAware": True,
                "partialJSONAllowed": False,
                "oversizedOldestEventContent": "explicit_marker_plus_provenance_preserving_text_tail",
                "contextTruncationMarker": CONTEXT_TRUNCATION_MARKER,
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
                "truncationSelfAudit": truncation_self_audit,
            },
            "counts": {
                "examples": len(packed_records),
                "pasteActions": total_paste_actions,
                "resolvedPastePayloadsPreservedInHistoricalWrites": (
                    resolved_paste_payloads_preserved_in_history
                ),
                "modelInputTokens": total_input_tokens,
                "discardedModelInputTokens": total_input_tokens_discarded,
                "droppedContextEventsAcrossExamples": total_dropped_context_events,
                "partiallyRetainedContextEventsAcrossExamples": (
                    total_partially_retained_context_events
                ),
                "targetTokens": total_target_tokens,
                "maximumModelInputTokensBeforePacking": maximum_input_tokens_before_truncation,
                "maximumRightEdgeQueryTokens": maximum_query_tokens,
                "maximumPackedSequenceTokens": maximum_sequence_tokens,
                "maximumUnusedModelInputTokenBudget": maximum_unused_input_budget,
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

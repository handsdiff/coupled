#!/usr/bin/env python3
"""Audit a tokenizer-packed Phase 1 dataset without its source collector."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


IGNORE_LABEL = -100


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def token_ids_sha256(token_ids: list[int]) -> str:
    return hashlib.sha256(
        (json.dumps(token_ids, ensure_ascii=False, sort_keys=True) + "\n").encode()
    ).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    arguments = parser.parse_args()
    directory = arguments.dataset.expanduser().resolve()
    manifest = json.loads((directory / "packing.json").read_text(encoding="utf-8"))
    if manifest.get("schemaVersion") != 3 or manifest.get("packerVersion") != "phase1-token-pack-v3":
        raise ValueError("auditor requires phase1-token-pack-v3")
    packed_path = directory / "packed-examples.jsonl"
    expected = manifest["artifactDigestsSHA256"]["packed-examples.jsonl"]
    if sha256(packed_path) != expected:
        raise ValueError("packed-examples.jsonl digest does not match manifest")

    paste_marker_ids = manifest["tokenizer"]["pasteMarkerTokenIDs"]
    eos_id = manifest["tokenizer"]["eosTokenID"]
    if not paste_marker_ids or eos_id in paste_marker_ids:
        raise ValueError("paste marker encoding is empty or contains EOS")
    if manifest["tokenizer"]["pasteMarkerTokenCount"] != len(paste_marker_ids):
        raise ValueError("paste marker token count disagrees")
    if (
        manifest["tokenizer"]["originalVocabularySize"]
        != manifest["tokenizer"]["savedVocabularySize"]
    ):
        raise ValueError("saved tokenizer vocabulary was modified")
    rows = 0
    paste_actions = 0
    model_input_tokens = 0
    discarded_model_input_tokens = 0
    dropped_context_events = 0
    partially_retained_context_events = 0
    target_tokens = 0
    maximum_model_input_before_packing = 0
    maximum_query_tokens = 0
    maximum_sequence_tokens = 0
    maximum_unused_input_budget = 0
    with packed_path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("schemaVersion") != 3 or record.get("packerVersion") != manifest["packerVersion"]:
                raise ValueError(f"line {line_number}: packed record schema disagrees")
            inputs = record["inputIDs"]
            labels = record["labels"]
            attention = record["attentionMask"]
            input_count = record["modelInputTokenCount"]
            query_count = record["rightEdgeQueryTokenCount"]
            history_count = record["historyTokenCount"]
            target = inputs[input_count:]
            if not inputs or not (len(inputs) == len(labels) == len(attention)):
                raise ValueError(f"line {line_number}: inconsistent sequence arrays")
            if labels[:input_count] != [IGNORE_LABEL] * input_count:
                raise ValueError(f"line {line_number}: model input receives loss")
            if labels[input_count:] != target:
                raise ValueError(f"line {line_number}: target labels disagree")
            if query_count > input_count:
                raise ValueError(f"line {line_number}: right-edge query was truncated")
            if history_count + query_count != input_count:
                raise ValueError(f"line {line_number}: history/query token counts disagree")
            if not target:
                raise ValueError(f"line {line_number}: target is empty")
            query_ids = inputs[input_count - query_count : input_count]
            if token_ids_sha256(query_ids) != record["rightEdgeQueryTokenSHA256"]:
                raise ValueError(f"line {line_number}: right-edge query digest disagrees")
            spans = record["contextEventTokenSpans"]
            cursor = 0
            truncated_spans = 0
            for span_index, span in enumerate(spans):
                if span["tokenStart"] != cursor or span["tokenEnd"] <= cursor:
                    raise ValueError(f"line {line_number}: context event spans are not contiguous")
                cursor = span["tokenEnd"]
                if span["contentTruncated"]:
                    truncated_spans += 1
                    if span_index != 0:
                        raise ValueError(f"line {line_number}: non-oldest event was truncated")
                    packed = json.loads(span["packedSerialized"])
                    if packed.get("contentTruncatedForPacking") is not True:
                        raise ValueError(f"line {line_number}: truncated event lacks marker metadata")
                    if not str(packed.get("content", "")).startswith(
                        manifest["packing"]["contextTruncationMarker"]
                    ):
                        raise ValueError(f"line {line_number}: truncated content marker is absent")
                    digest = hashlib.sha256(span["packedSerialized"].encode()).hexdigest()
                    if digest != span["serializedSHA256"]:
                        raise ValueError(f"line {line_number}: truncated event digest disagrees")
                elif "packedSerialized" in span:
                    raise ValueError(f"line {line_number}: full event stores rewritten serialization")
            if cursor != history_count:
                raise ValueError(f"line {line_number}: event spans do not cover history")
            if truncated_spans != record["partiallyRetainedContextEventCount"]:
                raise ValueError(f"line {line_number}: truncated event count disagrees")
            if len(spans) + record["droppedContextEventCount"] != record["sourceContextEventCount"]:
                raise ValueError(f"line {line_number}: context event accounting disagrees")
            if input_count > manifest["packing"]["inputTokenBudget"]:
                raise ValueError(f"line {line_number}: model input exceeds budget")
            if record["unusedModelInputTokenBudget"] != (
                manifest["packing"]["inputTokenBudget"] - input_count
            ):
                raise ValueError(f"line {line_number}: unused budget disagrees")
            if record["discardedModelInputTokenCount"] != (
                record["modelInputTokenCountBeforePacking"] - input_count
            ):
                raise ValueError(f"line {line_number}: discarded token count disagrees")
            if record["targetTokenCount"] != len(target):
                raise ValueError(f"line {line_number}: target token count disagrees")
            if target[-1] != eos_id or target.count(eos_id) != 1:
                raise ValueError(f"line {line_number}: target EOS contract failed")
            observed_pastes = 0
            for span in record["targetSegmentTokenSpans"]:
                span_ids = target[span["targetTokenStart"] : span["targetTokenEnd"]]
                if span["type"] == "paste":
                    if span_ids != paste_marker_ids:
                        raise ValueError(f"line {line_number}: paste marker encoding disagrees")
                    observed_pastes += 1
            if observed_pastes != record["pasteActionCount"]:
                raise ValueError(f"line {line_number}: paste action count disagrees")
            rows += 1
            paste_actions += record["pasteActionCount"]
            model_input_tokens += input_count
            discarded_model_input_tokens += record["discardedModelInputTokenCount"]
            dropped_context_events += record["droppedContextEventCount"]
            partially_retained_context_events += record["partiallyRetainedContextEventCount"]
            target_tokens += len(target)
            maximum_model_input_before_packing = max(
                maximum_model_input_before_packing,
                record["modelInputTokenCountBeforePacking"],
            )
            maximum_query_tokens = max(maximum_query_tokens, query_count)
            maximum_sequence_tokens = max(maximum_sequence_tokens, len(inputs))
            maximum_unused_input_budget = max(
                maximum_unused_input_budget,
                record["unusedModelInputTokenBudget"],
            )

    if rows != manifest["counts"]["examples"]:
        raise ValueError("example count does not match manifest")
    if paste_actions != manifest["counts"]["pasteActions"]:
        raise ValueError("paste action count does not match manifest")
    expected_counts = {
        "modelInputTokens": model_input_tokens,
        "discardedModelInputTokens": discarded_model_input_tokens,
        "droppedContextEventsAcrossExamples": dropped_context_events,
        "partiallyRetainedContextEventsAcrossExamples": partially_retained_context_events,
        "targetTokens": target_tokens,
        "maximumModelInputTokensBeforePacking": maximum_model_input_before_packing,
        "maximumRightEdgeQueryTokens": maximum_query_tokens,
        "maximumPackedSequenceTokens": maximum_sequence_tokens,
        "maximumUnusedModelInputTokenBudget": maximum_unused_input_budget,
    }
    for key, value in expected_counts.items():
        if manifest["counts"].get(key) != value:
            raise ValueError(f"manifest count disagrees: {key}")
    for relative, digest in manifest["tokenizer"]["savedFileDigestsSHA256"].items():
        if sha256(directory / "tokenizer" / relative) != digest:
            raise ValueError(f"tokenizer digest mismatch: {relative}")
    print(
        f"Packed dataset audit passed: {rows} examples, "
        f"{paste_actions} paste actions, EOS={eos_id}, "
        f"PASTE_MARKER_TOKENS={paste_marker_ids}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        raise SystemExit(f"audit-phase1-packed: {error}")

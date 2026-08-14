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
    packed_path = directory / "packed-examples.jsonl"
    expected = manifest["artifactDigestsSHA256"]["packed-examples.jsonl"]
    if sha256(packed_path) != expected:
        raise ValueError("packed-examples.jsonl digest does not match manifest")

    paste_id = manifest["tokenizer"]["pasteTokenID"]
    eos_id = manifest["tokenizer"]["eosTokenID"]
    rows = 0
    paste_actions = 0
    with packed_path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            record = json.loads(line)
            inputs = record["inputIDs"]
            labels = record["labels"]
            attention = record["attentionMask"]
            input_count = record["modelInputTokenCount"]
            query_count = record["rightEdgeQueryTokenCount"]
            target = inputs[input_count:]
            if not inputs or not (len(inputs) == len(labels) == len(attention)):
                raise ValueError(f"line {line_number}: inconsistent sequence arrays")
            if labels[:input_count] != [IGNORE_LABEL] * input_count:
                raise ValueError(f"line {line_number}: model input receives loss")
            if labels[input_count:] != target:
                raise ValueError(f"line {line_number}: target labels disagree")
            if query_count > input_count:
                raise ValueError(f"line {line_number}: right-edge query was truncated")
            if not target:
                raise ValueError(f"line {line_number}: target is empty")
            query_ids = inputs[input_count - query_count : input_count]
            if token_ids_sha256(query_ids) != record["rightEdgeQueryTokenSHA256"]:
                raise ValueError(f"line {line_number}: right-edge query digest disagrees")
            if target[-1] != eos_id or target.count(eos_id) != 1:
                raise ValueError(f"line {line_number}: target EOS contract failed")
            if target.count(paste_id) != record["pasteActionCount"]:
                raise ValueError(f"line {line_number}: paste token count disagrees")
            for span in record["targetSegmentTokenSpans"]:
                span_ids = target[span["targetTokenStart"] : span["targetTokenEnd"]]
                if span["type"] == "paste" and span_ids != [paste_id]:
                    raise ValueError(f"line {line_number}: paste span is not atomic")
                if span["type"] == "authored_text" and paste_id in span_ids:
                    raise ValueError(f"line {line_number}: authored span contains paste ID")
            rows += 1
            paste_actions += record["pasteActionCount"]

    if rows != manifest["counts"]["examples"]:
        raise ValueError("example count does not match manifest")
    if paste_actions != manifest["counts"]["pasteActions"]:
        raise ValueError("paste action count does not match manifest")
    for relative, digest in manifest["tokenizer"]["savedFileDigestsSHA256"].items():
        if sha256(directory / "tokenizer" / relative) != digest:
            raise ValueError(f"tokenizer digest mismatch: {relative}")
    print(
        f"Packed dataset audit passed: {rows} examples, "
        f"{paste_actions} paste actions, EOS={eos_id}, PASTE={paste_id}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        raise SystemExit(f"audit-phase1-packed: {error}")

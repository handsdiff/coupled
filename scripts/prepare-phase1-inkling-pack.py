#!/usr/bin/env python3
"""Render the frozen Phase 1 semantic contexts with Inkling's native grammar."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
from typing import Any

from phase1_experiment import canonical_bytes, target_text
from phase1_inkling import (
    INKLING_MODEL,
    INKLING_PACK_VERSION,
    REASONING_CONDITIONS,
    InklingContractError,
    load_jsonl,
    render_training_row,
    sha256,
)


def iso8601() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def reconstruct_semantic_input(
    example: dict[str, Any],
    plan: dict[str, Any],
    context_by_id: dict[str, dict[str, Any]],
) -> str:
    serialized: list[str] = []
    for retained in plan["retainedContextBlocks"]:
        text = retained.get("serializedOverride")
        if text is None:
            text = context_by_id[retained["contextBlockID"]]["serialized"]
        if hashlib.sha256(text.encode()).hexdigest() != retained["serializedSHA256"]:
            raise InklingContractError("retained context block hash differs")
        serialized.append(text)
    context = "\n".join(serialized)
    body = example["query"] if not context else context + "\n" + example["query"]
    semantic = plan["taskInstruction"] + "\n" + body
    if hashlib.sha256(semantic.encode()).hexdigest() != plan["semanticModelInputSHA256"]:
        raise InklingContractError("semantic model input differs from the frozen plan")
    return semantic


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--semantic-pack", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    output = arguments.output.expanduser().resolve()
    if output.exists():
        raise InklingContractError(f"output already exists: {output}")
    corpus_path = arguments.corpus.expanduser().resolve()
    semantic_pack = arguments.semantic_pack.expanduser().resolve()
    corpus = json.loads((corpus_path / "corpus.json").read_text(encoding="utf-8"))
    qwen_packing = json.loads(
        (semantic_pack / "packing.json").read_text(encoding="utf-8")
    )
    examples = load_jsonl(corpus_path / "examples.jsonl")
    plans = load_jsonl(semantic_pack / "context-plans.jsonl")
    context_blocks = load_jsonl(corpus_path / "context-blocks.jsonl")
    plan_by_id = {value["exampleID"]: value for value in plans}
    context_by_id = {value["contextBlockID"]: value for value in context_blocks}
    if [value["exampleID"] for value in examples] != [value["exampleID"] for value in plans]:
        raise InklingContractError("corpus and semantic context plans are not aligned")

    output.mkdir(parents=True)
    paths = {
        condition: output / f"{condition}-packed-examples.jsonl"
        for condition in REASONING_CONDITIONS
    }
    counts: dict[str, Any] = {}
    for condition, effort in REASONING_CONDITIONS.items():
        maximum = 0
        model_input_total = 0
        full_total = 0
        target_total = 0
        with paths[condition].open("wb") as handle:
            for example in examples:
                semantic = reconstruct_semantic_input(
                    example, plan_by_id[example["exampleID"]], context_by_id
                )
                target = target_text(example["target"])
                rendered = render_training_row(
                    semantic_input=semantic, target=target, effort=effort
                )
                row = {
                    "schemaVersion": 1,
                    "packerVersion": INKLING_PACK_VERSION,
                    "condition": condition,
                    "effort": effort,
                    "exampleID": example["exampleID"],
                    "targetEventID": example["targetEventID"],
                    "experimentBlockID": example["experimentBlockID"],
                    "semanticModelInputSHA256": hashlib.sha256(semantic.encode()).hexdigest(),
                    "pasteActionCount": sum(
                        segment["type"] == "paste"
                        for segment in example["target"]["segments"]
                    ),
                    **rendered,
                }
                handle.write(canonical_bytes(row))
                maximum = max(maximum, len(row["inputIDs"]))
                model_input_total += row["modelInputTokenCount"]
                full_total += len(row["inputIDs"])
                target_total += row["targetTokenCount"]
            handle.flush()
            os.fsync(handle.fileno())
        counts[condition] = {
            "examples": len(examples),
            "maximumSequenceTokens": maximum,
            "modelInputTokens": model_input_total,
            "fullSequenceTokens": full_total,
            "lossBearingTargetTokens": target_total,
        }
        if maximum > 65536:
            raise InklingContractError(
                f"{condition} exceeds Inkling-Small's 64K Tinker context"
            )

    manifest = {
        "schemaVersion": 1,
        "packerVersion": INKLING_PACK_VERSION,
        "createdAt": iso8601(),
        "model": INKLING_MODEL,
        "renderer": {
            "grammar": "TMLv0",
            "tokenizer": "o200k_base_chat",
            "tmlRenderersVersion": importlib.metadata.version("tml-renderers"),
            "reasoningConditions": REASONING_CONDITIONS,
            "semanticInputPlacement": "single_user_message",
            "targetPlacement": "single_model_text_message_then_ModelEndSampling",
            "lossMask": {
                "semanticInput": False,
                "assistantEnvelope": True,
                "authoredAndPasteMarkerText": True,
                "endMessage": True,
                "modelEndSampling": True,
            },
        },
        "source": {
            "corpusDirectory": str(corpus_path),
            "corpusID": corpus["corpusID"],
            "corpusSHA256": sha256(corpus_path / "corpus.json"),
            "examplesSHA256": sha256(corpus_path / "examples.jsonl"),
            "semanticPackDirectory": str(semantic_pack),
            "semanticPackingSHA256": sha256(semantic_pack / "packing.json"),
            "semanticContextPlansSHA256": sha256(semantic_pack / "context-plans.jsonl"),
            "semanticInputIdentity": "exact_sha256_match_to_qwen_shared_context_plan",
            "qwenTokenizer": qwen_packing["tokenizer"]["repository"],
        },
        "counts": counts,
        "artifactDigestsSHA256": {
            path.name: sha256(path) for path in paths.values()
        },
    }
    (output / "packing.json").write_bytes(canonical_bytes(manifest))
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

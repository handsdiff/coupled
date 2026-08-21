#!/usr/bin/env python3
"""Stream event-aware semantic packs for the GPT-5.6 history-window ablation."""

from __future__ import annotations

import argparse
import gc
import datetime as dt
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from phase1_context_window_ablation import ABLATION_VERSION, WINDOWS, ContextWindowError, sha256
from phase1_experiment import canonical_bytes, target_text
from phase1_inkling import load_experiment_blocks, load_jsonl


PACK_VERSION = "phase1-gpt56-context-semantic-pack-v1"


def load_packer(project: Path):
    path = project / "scripts/pack-phase1-dataset.py"
    specification = importlib.util.spec_from_file_location("phase1_context_source_packer", path)
    if specification is None or specification.loader is None:
        raise ContextWindowError(f"cannot load {path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def pack_metadata(
    *,
    example: dict[str, Any],
    events_by_id: dict[str, dict[str, Any]],
    event_token_counts: dict[str, int],
    tokenizer: Any,
    instruction_token_count: int,
    token_budget: int,
    packer: Any,
) -> dict[str, Any]:
    query_ids = packer.encode_plain_text(tokenizer, example["query"])
    remaining = token_budget - instruction_token_count - len(query_ids)
    if remaining < 0:
        raise ContextWindowError(
            f"instruction plus query exceeds {token_budget}: {example['exampleID']}"
        )
    context_ids = example.get("contextBlockIDs", example.get("contextEventIDs"))
    retained_reversed = []
    complete = instruction_token_count + len(query_ids)
    for block_id in context_ids:
        complete += event_token_counts[block_id]
    for block_id in reversed(context_ids):
        count = event_token_counts[block_id]
        if count <= remaining:
            retained_reversed.append({
                "contextBlockID": block_id,
                "serializedOverride": None,
                "serializedSHA256": hashlib.sha256(
                    events_by_id[block_id]["serialized"].encode()
                ).hexdigest(),
                "contentTruncated": False,
                "tokenCount": count,
            })
            remaining -= count
            continue
        truncated = packer.truncate_oldest_event(
            events_by_id[block_id]["serialized"], remaining, tokenizer
        )
        if truncated is not None:
            text, token_ids, _ = truncated
            retained_reversed.append({
                "contextBlockID": block_id,
                "serializedOverride": text,
                "serializedSHA256": hashlib.sha256(text.encode()).hexdigest(),
                "contentTruncated": True,
                "tokenCount": len(token_ids),
            })
            remaining -= len(token_ids)
        break
    retained = list(reversed(retained_reversed))
    plans = [
        {key: value for key, value in item.items() if key != "tokenCount"}
        for item in retained
    ]
    packed_count = token_budget - remaining
    return {
        "completeTokenCount": complete,
        "packedTokenCount": packed_count,
        "retainedContextBlocks": plans,
        "sourceContextEventCount": len(context_ids),
        "droppedContextEventCount": len(context_ids) - len(plans),
        "partiallyRetainedContextEventCount": sum(
            bool(value["contentTruncated"]) for value in plans
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--reference-32k-pack", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    corpus_path = arguments.corpus.expanduser().resolve()
    reference_path = arguments.reference_32k_pack.expanduser().resolve()
    output = arguments.output.expanduser().resolve()
    if output.exists():
        raise ContextWindowError(f"output already exists: {output}")
    project = Path(__file__).resolve().parent.parent
    packer = load_packer(project)
    source_manifest = json.loads((corpus_path / "corpus.json").read_text(encoding="utf-8"))
    reference_manifest = json.loads((reference_path / "packing.json").read_text(encoding="utf-8"))
    if reference_manifest.get("packing", {}).get("inputTokenBudget") != WINDOWS["32k"]:
        raise ContextWindowError("reference pack is not the canonical 32K pack")
    reference_plans = {
        value["exampleID"]: value
        for value in load_jsonl(reference_path / "context-plans.jsonl")
    }
    context_blocks = load_jsonl(corpus_path / "context-blocks.jsonl")
    events_by_id = {value["contextBlockID"]: value for value in context_blocks}
    if len(events_by_id) != len(context_blocks):
        raise ContextWindowError("duplicate context blocks")
    tokenizer = packer.AutoTokenizer.from_pretrained(
        reference_path / "tokenizer", use_fast=True, split_special_tokens=True
    )
    task_instruction = reference_manifest["packing"]["taskInstruction"]
    instruction_token_count = len(
        packer.encode_plain_text(tokenizer, task_instruction + "\n")
    )
    event_token_counts = {
        block_id: len(
            packer.encode_plain_text(tokenizer, value["serialized"] + "\n")
        )
        for block_id, value in events_by_id.items()
    }
    blocks = load_experiment_blocks(corpus_path)
    expected_ids = [value for block in blocks[1:] for value in block["exampleIDs"]]
    expected_id_set = set(expected_ids)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    handles = {}
    counts = {
        key: {
            "examples": 0,
            "prospectiveExamples": 0,
            "modelInputTokens": 0,
            "droppedContextEventsAcrossExamples": 0,
            "partiallyRetainedContextEventsAcrossExamples": 0,
            "maximumModelInputTokens": 0,
            "maximumModelInputTokensBeforePacking": 0,
        }
        for key in WINDOWS
    }
    seen: list[str] = []
    try:
        for key in WINDOWS:
            directory = temporary / key
            directory.mkdir(parents=True)
            handles[key] = (directory / "semantic-examples.jsonl").open("wb")
        with (corpus_path / "examples.jsonl").open(encoding="utf-8") as source:
            for line in source:
                if not line.strip():
                    continue
                example = json.loads(line)
                example_id = example["exampleID"]
                seen.append(example_id)
                expected_context = "\n".join(
                    events_by_id[value]["serialized"]
                    for value in example.get("contextBlockIDs", example.get("contextEventIDs"))
                )
                if example["context"] != expected_context:
                    raise ContextWindowError(
                        f"compiled context lineage differs: {example_id}"
                    )
                del expected_context
                for key, budget in WINDOWS.items():
                    packed = pack_metadata(
                        example=example,
                        events_by_id=events_by_id,
                        event_token_counts=event_token_counts,
                        tokenizer=tokenizer,
                        instruction_token_count=instruction_token_count,
                        token_budget=budget,
                        packer=packer,
                    )
                    retained = []
                    serialized = []
                    for span in packed["retainedContextBlocks"]:
                        block_id = span["contextBlockID"]
                        text = span.get("serializedOverride")
                        if text is None:
                            text = events_by_id[block_id]["serialized"]
                        serialized.append(text)
                        retained.append(span)
                    context = "\n".join(serialized)
                    body = example["query"] if not context else context + "\n" + example["query"]
                    semantic_input = task_instruction + "\n" + body
                    semantic_hash = hashlib.sha256(semantic_input.encode()).hexdigest()
                    row = {
                        "schemaVersion": 1,
                        "packVersion": PACK_VERSION,
                        "windowKey": key,
                        "inputTokenBudget": budget,
                        "exampleID": example_id,
                        "targetEventID": example["targetEventID"],
                        "experimentBlockID": example["experimentBlockID"],
                        "application": example.get("conditioningState", {}).get("destination", {}).get("appName"),
                        "rightEdgeQuerySHA256": hashlib.sha256(example["query"].encode()).hexdigest(),
                        "taskInstruction": task_instruction,
                        "semanticModelInput": semantic_input,
                        "semanticModelInputSHA256": semantic_hash,
                        "canonicalPackingTokenCount": packed["packedTokenCount"],
                        "modelInputTokenCountBeforePacking": packed["completeTokenCount"],
                        "retainedContextBlocks": retained,
                        "sourceContextEventCount": packed["sourceContextEventCount"],
                        "droppedContextEventCount": packed["droppedContextEventCount"],
                        "partiallyRetainedContextEventCount": packed["partiallyRetainedContextEventCount"],
                        "target": target_text(example["target"]),
                        "pasteActionCount": sum(
                            segment.get("type") == "paste"
                            for segment in example["target"].get("segments", [])
                        ),
                    }
                    if key == "32k":
                        reference = reference_plans.get(example_id)
                        if not (
                            reference
                            and reference["semanticModelInputSHA256"] == semantic_hash
                            and reference["retainedContextBlocks"] == retained
                            and reference["qwenModelInputTokenCount"] == packed["packedTokenCount"]
                        ):
                            raise ContextWindowError(
                                f"streaming 32K pack differs from frozen reference: {example_id}"
                            )
                    handles[key].write(canonical_bytes(row))
                    count = counts[key]
                    count["examples"] += 1
                    count["prospectiveExamples"] += int(example_id in expected_id_set)
                    count["modelInputTokens"] += packed["packedTokenCount"]
                    count["droppedContextEventsAcrossExamples"] += packed["droppedContextEventCount"]
                    count["partiallyRetainedContextEventsAcrossExamples"] += packed["partiallyRetainedContextEventCount"]
                    count["maximumModelInputTokens"] = max(
                        count["maximumModelInputTokens"], packed["packedTokenCount"]
                    )
                    count["maximumModelInputTokensBeforePacking"] = max(
                        count["maximumModelInputTokensBeforePacking"], packed["completeTokenCount"]
                    )
                del example
                if len(seen) % 10 == 0:
                    gc.collect()
        if seen != list(reference_plans):
            raise ContextWindowError("streamed corpus order differs from reference plans")
        if not expected_id_set <= set(seen):
            raise ContextWindowError("prospective examples are missing")
        for handle in handles.values():
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
        handles.clear()
        for key, budget in WINDOWS.items():
            directory = temporary / key
            rows_path = directory / "semantic-examples.jsonl"
            manifest = {
                "schemaVersion": 1,
                "packVersion": PACK_VERSION,
                "ablationVersion": ABLATION_VERSION,
                "createdAt": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                "windowKey": key,
                "inputTokenBudget": budget,
                "source": {
                    "corpusID": source_manifest["corpusID"],
                    "corpusSHA256": sha256(corpus_path / "corpus.json"),
                    "examplesSHA256": sha256(corpus_path / "examples.jsonl"),
                    "contextBlocksSHA256": sha256(corpus_path / "context-blocks.jsonl"),
                    "reference32KPackingSHA256": sha256(reference_path / "packing.json"),
                    "reference32KContextPlansSHA256": sha256(reference_path / "context-plans.jsonl"),
                },
                "packing": {
                    "algorithm": "same_event_aware_suffix_and_oldest_event_tail_as_phase1-token-pack-v7",
                    "tokenizerRepository": reference_manifest["tokenizer"]["repository"],
                    "tokenizerResolvedRevision": reference_manifest["tokenizer"]["resolvedRevision"],
                    "taskInstruction": task_instruction,
                    "taskInstructionAndRightEdgeQueryPreserved": True,
                    "targetTruncationAllowed": False,
                    "onlyOldestRetainedEventMayBePartiallyTruncated": True,
                    "thirtyTwoKExactReferenceReproductionRequired": True,
                },
                "counts": counts[key],
                "artifactDigestsSHA256": {"semantic-examples.jsonl": sha256(rows_path)},
            }
            (directory / "packing.json").write_bytes(canonical_bytes(manifest))
        os.replace(temporary, output)
    except BaseException:
        for handle in handles.values():
            handle.close()
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    print(f"Wrote streaming context-window packs to {output}")
    print(json.dumps(counts, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

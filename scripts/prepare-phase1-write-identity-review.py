#!/usr/bin/env python3
"""Prepare a compact manual review of final episode-level WRITE identity."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


APPLICATIONS = ("ChatGPT", "Visual Studio Code", "Google Chrome", "Obsidian")


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected object")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def target_text(example: dict[str, Any]) -> str:
    pieces: list[str] = []
    for segment in example.get("target", {}).get("segments", []):
        pieces.append(
            "<|paste|>" if segment.get("type") == "paste"
            else str(segment.get("content") or "")
        )
    return "".join(pieces)


def quantiles(rows: list[dict[str, Any]], amount: int) -> list[dict[str, Any]]:
    if len(rows) <= amount:
        return rows
    indices = [round(index * (len(rows) - 1) / (amount - 1)) for index in range(amount)]
    return [rows[index] for index in indices]


def select_examples(examples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    by_app = {
        application: [
            row for row in examples
            if row.get("modelFacingDestination", {}).get("application") == application
        ]
        for application in APPLICATIONS
    }
    for application in APPLICATIONS:
        for row in quantiles(by_app[application], 7):
            selected[row["exampleID"]] = row

    terminal = [
        row for row in by_app["Visual Studio Code"]
        if row.get("modelFacingDestination", {}).get("surfaceKind")
        == "integrated_terminal"
    ]
    for mode in ("shell", "agent_cli", "unknown"):
        candidates = [
            row for row in terminal
            if row.get("modelFacingDestination", {}).get("interactionMode") == mode
        ]
        if candidates and not any(
            row.get("modelFacingDestination", {}).get("interactionMode") == mode
            for row in selected.values()
        ):
            selected[candidates[len(candidates) // 2]["exampleID"]] = candidates[
                len(candidates) // 2
            ]

    anchors = [
        row for row in examples
        if row.get("modelFacingDestination", {}).get("surfaceKind")
        in {"browser_address_bar", "integrated_terminal", "chat_prompt"}
        and row["exampleID"] not in selected
    ]
    if anchors:
        selected[anchors[len(anchors) // 2]["exampleID"]] = anchors[len(anchors) // 2]
    rows = sorted(selected.values(), key=lambda row: row["chronologicalOrdinal"])
    if len(rows) > 29:
        required = {
            next(
                row["exampleID"] for row in rows
                if row.get("modelFacingDestination", {}).get("interactionMode") == mode
            )
            for mode in ("shell", "agent_cli", "unknown")
            if any(
                row.get("modelFacingDestination", {}).get("interactionMode") == mode
                for row in rows
            )
        }
        removable = [row for row in rows if row["exampleID"] not in required]
        while len(rows) > 29 and removable:
            victim = removable.pop(len(removable) // 2)
            rows.remove(victim)
    return rows[:29]


def exact_model_input(
    example: dict[str, Any],
    plan: dict[str, Any],
    blocks: dict[str, dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    retained: list[dict[str, Any]] = []
    serialized: list[str] = []
    for item in plan.get("retainedContextBlocks", []):
        block = blocks[item["contextBlockID"]]
        value = item.get("serializedOverride") or block["serialized"]
        serialized.append(value)
        retained.append({
            "contextBlockID": item["contextBlockID"],
            "availableAt": block.get("availableAt"),
            "contentTruncated": bool(item.get("contentTruncated")),
            "serialized": value,
        })
    context = "\n".join(serialized)
    body = example["query"] if not context else context + "\n" + example["query"]
    result = plan["taskInstruction"] + "\n" + body
    if hashlib.sha256(result.encode()).hexdigest() != plan["semanticModelInputSHA256"]:
        raise ValueError(f"packed semantic input mismatch: {example['exampleID']}")
    return result, retained


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--packed", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    corpus = args.corpus.resolve()
    packed = args.packed.resolve()
    output = args.output.resolve()
    if output.exists():
        raise ValueError(f"output already exists: {output}")

    manifest = load_json(corpus / "corpus.json")
    packing = load_json(packed / "packing.json")
    if packing.get("source", {}).get("sessionID") != manifest.get("corpusID"):
        raise ValueError("pack does not belong to episode corpus")
    expected_digests = packing.get("source", {}).get("digestsSHA256", {})
    for name in ("examples.jsonl", "events.jsonl", "context-blocks.jsonl"):
        expected = expected_digests.get(name)
        if expected is not None and sha256(corpus / name) != expected:
            raise ValueError(f"packed source digest mismatch: {name}")
    examples = load_jsonl(corpus / "examples.jsonl")
    plans = {row["exampleID"]: row for row in load_jsonl(packed / "context-plans.jsonl")}
    packed_rows = {
        row["exampleID"]: row for row in load_jsonl(packed / "packed-examples.jsonl")
    }
    blocks = {
        row["contextBlockID"]: row
        for row in load_jsonl(corpus / "context-blocks.jsonl")
    }
    source_corpus = Path(manifest["source"]["path"])
    source_event_rows = load_jsonl(source_corpus / "events.jsonl")
    selected = select_examples(examples)
    review_rows: list[dict[str, Any]] = []
    for review_ordinal, example in enumerate(selected, 1):
        plan = plans[example["exampleID"]]
        packed_row = packed_rows[example["exampleID"]]
        exact_input, retained = exact_model_input(example, plan, blocks)
        derivation = example.get("destinationDerivation") or {}
        review_rows.append({
            "reviewOrdinal": review_ordinal,
            "exampleID": example["exampleID"],
            "chronologicalOrdinal": example["chronologicalOrdinal"],
            "targetBeganAt": example["targetBeganAt"],
            "modelFacingDestination": example.get("modelFacingDestination"),
            "logicalDestinationKey": example.get("logicalDestinationKey"),
            "rawDestination": derivation.get("originalDestination"),
            "classification": {
                "normalizerVersion": derivation.get("normalizerVersion"),
                "rule": derivation.get("rule"),
                "source": derivation.get("source"),
                "configuredTerminalAgentMapping": derivation.get(
                    "configuredTerminalAgentMapping"
                ),
            },
            "query": json.loads(example["query"]),
            "target": target_text(example),
            "targetStructured": example["target"],
            "episode": example.get("episode"),
            "modelInputTokenCount": packed_row["modelInputTokenCount"],
            "modelInputTokenCountBeforePacking": packed_row.get(
                "modelInputTokenCountBeforePacking"
            ),
            "droppedContextEventCount": packed_row.get("droppedContextEventCount"),
            "retainedHistory": retained,
            "exactSemanticModelInput": exact_input,
            "semanticModelInputSHA256": plan["semanticModelInputSHA256"],
        })

    comparisons_path = corpus / "destination-identity-comparison.jsonl"
    comparisons = load_jsonl(comparisons_path)
    changed = [
        row for row in comparisons if row.get("wouldChangeIdentityGate")
    ]
    terminal_rows: list[dict[str, Any]] = []
    terminal_events = [
        event for event in source_event_rows
        if event.get("kind") == "write"
        and event.get("modelFacingDestination", {}).get("surfaceKind")
        == "integrated_terminal"
    ]
    terminal_events.sort(key=lambda event: (event.get("beganAt", ""), event["sourceEventID"]))
    for ordinal, event in enumerate(terminal_events, 1):
        audit = json.loads(event["auditSerialized"])
        began_at = event.get("beganAt", "")
        derivation = event.get("destinationDerivation") or {}
        terminal_rows.append({
            "reviewOrdinal": ordinal,
            "sourceEventID": event["sourceEventID"],
            "beganAt": began_at,
            "modelFacingDestination": event.get("modelFacingDestination"),
            "logicalDestinationKey": event.get("logicalDestinationKey"),
            "rawDestination": derivation.get("originalDestination"),
            "classification": {
                "normalizerVersion": derivation.get("normalizerVersion"),
                "rule": derivation.get("rule"),
                "source": derivation.get("source"),
                "configuredTerminalAgentMapping": derivation.get(
                    "configuredTerminalAgentMapping"
                ),
            },
            "targetValidationOnly": audit.get("resolvedCompletion", audit.get("content", "")),
            "operation": audit.get("operation"),
            "authorshipResolution": audit.get("authorshipResolution"),
        })
    payload = {
        "schemaVersion": 1,
        "artifactType": "phase1_write_identity_review",
        "corpusID": manifest["corpusID"],
        "episodeVersion": manifest["episodeVersion"],
        "conversionVersion": manifest["conversionVersion"],
        "normalizerVersion": manifest.get("serialization", {}).get(
            "writeDestinationVersion"
        ),
        "configuredTerminalAgentProgramMappings": manifest.get(
            "writeDestination", {}
        ).get("configuredTerminalAgentProgramMappings", {}),
        "counts": {
            "reviewExamples": len(review_rows),
            "allEligibleEpisodes": len(examples),
            "identityComparisons": len(comparisons),
            "changedIdentityBoundaries": len(changed),
            "terminalWriteAudit": len(terminal_rows),
        },
        "examples": review_rows,
        "terminalWrites": terminal_rows,
        "changedIdentityBoundaries": changed,
    }
    output.mkdir(parents=True)
    (output / "review.json").write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    review_hash = sha256(output / "review.json")
    (output / "manifest.json").write_text(json.dumps({
        "schemaVersion": 1,
        "artifactType": "phase1_write_identity_review_manifest",
        "reviewSHA256": review_hash,
        "source": {
            "corpus": str(corpus),
            "corpusSHA256": sha256(corpus / "corpus.json"),
            "packed": str(packed),
            "packingSHA256": sha256(packed / "packing.json"),
        },
        "counts": payload["counts"],
    }, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload["counts"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

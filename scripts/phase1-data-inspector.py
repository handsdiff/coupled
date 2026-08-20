#!/usr/bin/env python3
"""Read-only local browser for Phase 1 corpus, packing, and result artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


PASTE_MARKER = "<|paste|>"


class InspectorError(RuntimeError):
    """Raised when the immutable artifact chain cannot be reconstructed."""


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise InspectorError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise InspectorError(f"expected a JSON object in {path}")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open() as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise InspectorError(
                        f"expected an object at {path}:{line_number}"
                    )
                rows.append(value)
    except (OSError, json.JSONDecodeError) as error:
        raise InspectorError(f"cannot read {path}: {error}") from error
    return rows


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def target_text(target: dict[str, Any]) -> str:
    pieces: list[str] = []
    for segment in target.get("segments", []):
        kind = segment.get("type")
        if kind == "authored_text":
            pieces.append(str(segment.get("content", "")))
        elif kind == "paste":
            pieces.append(PASTE_MARKER)
        else:
            pieces.append(str(segment.get("content", "")))
    return "".join(pieces)


def parsed_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def event_projection(serialized: str) -> dict[str, Any]:
    value = parsed_json(serialized)
    if not isinstance(value, dict):
        return {"kind": "unknown", "content": serialized, "raw": value}
    kind = str(value.get("kind", "unknown"))
    if kind == "read":
        source = value.get("source") if isinstance(value.get("source"), dict) else {}
        return {
            "kind": kind,
            "application": source.get("application"),
            "window": source.get("window"),
            "content": value.get("content", ""),
            "raw": value,
        }
    destination = (
        value.get("destination")
        if isinstance(value.get("destination"), dict)
        else {}
    )
    return {
        "kind": kind,
        "application": destination.get("application"),
        "window": destination.get("window"),
        "operation": value.get("operation"),
        "authorshipResolution": value.get("authorshipResolution"),
        "segments": value.get("authorshipSegments", []),
        "removedContent": value.get("removedContent"),
        "raw": value,
    }


@dataclass(frozen=True)
class ArtifactPaths:
    project: Path
    corpus: Path
    packed: Path
    results: Path
    holistic_review: Path | None


def preferred_candidate(paths: list[Path]) -> Path:
    if not paths:
        raise InspectorError("no matching artifact directory exists")
    return sorted(
        paths,
        key=lambda value: (
            "determinism" in value.name,
            -value.stat().st_mtime,
            value.name,
        ),
    )[0]


def discover_paths(
    project: Path,
    results_argument: Path | None,
    corpus_argument: Path | None,
    packed_argument: Path | None,
    holistic_review_argument: Path | None,
) -> ArtifactPaths:
    data = project / "coupled-data"
    if results_argument is not None:
        results = results_argument.expanduser().resolve()
    else:
        result_candidates = [
            value.parent
            for value in data.glob("phase1-*/experiment.json")
            if (value.parent / "comparisons.jsonl").is_file()
        ]
        results = preferred_candidate(result_candidates)
    experiment = load_json(results / "experiment.json")
    source = experiment.get("source", {})

    if corpus_argument is not None:
        corpus = corpus_argument.expanduser().resolve()
    else:
        expected_id = source.get("corpusID")
        expected_digest = source.get("corpusSHA256")
        candidates: list[Path] = []
        for manifest_path in data.glob("*/corpus.json"):
            try:
                manifest = load_json(manifest_path)
            except InspectorError:
                continue
            if expected_id and manifest.get("corpusID") != expected_id:
                continue
            if expected_digest and sha256(manifest_path) != expected_digest:
                continue
            candidates.append(manifest_path.parent)
        corpus = preferred_candidate(candidates)

    if packed_argument is not None:
        packed = packed_argument.expanduser().resolve()
    else:
        expected_packing = source.get("packingSHA256")
        candidates = []
        for packing_path in data.glob("*/packing.json"):
            if expected_packing and sha256(packing_path) != expected_packing:
                continue
            candidates.append(packing_path.parent)
        packed = preferred_candidate(candidates)

    if holistic_review_argument is not None:
        holistic_review = holistic_review_argument.expanduser().resolve()
        if not holistic_review.is_file():
            raise InspectorError(
                f"holistic review file does not exist: {holistic_review}"
            )
    else:
        expected_corpus_id = load_json(corpus / "corpus.json").get("corpusID")
        review_candidates: list[Path] = []
        for review_path in (project / "episode-review").glob("*holistic*.json"):
            try:
                review = load_json(review_path)
            except InspectorError:
                continue
            if review.get("source", {}).get("corpusID") == expected_corpus_id:
                review_candidates.append(review_path)
        holistic_review = (
            preferred_candidate(review_candidates) if review_candidates else None
        )

    required = {
        results / "experiment.json",
        results / "comparisons.jsonl",
        corpus / "corpus.json",
        corpus / "examples.jsonl",
        corpus / "events.jsonl",
        corpus / "context-blocks.jsonl",
        packed / "packing.json",
        packed / "context-plans.jsonl",
        packed / "packed-examples.jsonl",
    }
    missing = sorted(str(path) for path in required if not path.is_file())
    if missing:
        raise InspectorError("missing required artifacts:\n" + "\n".join(missing))
    return ArtifactPaths(
        project=project,
        corpus=corpus,
        packed=packed,
        results=results,
        holistic_review=holistic_review,
    )


def prediction_triples_sha256(comparisons: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for comparison in comparisons:
        row = {
            "exampleID": comparison.get("exampleID"),
            "frontier": comparison.get("frontier", {}).get("prediction"),
            "personalizedQwen": comparison.get("personalizedQwen", {}).get(
                "prediction"
            ),
            "target": comparison.get("target"),
        }
        serialized = json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest.update(serialized.encode())
        digest.update(b"\n")
    return digest.hexdigest()


class DatasetStore:
    def __init__(self, paths: ArtifactPaths):
        self.paths = paths
        self.experiment = load_json(paths.results / "experiment.json")
        self.corpus_manifest = load_json(paths.corpus / "corpus.json")
        self.packing_manifest = load_json(paths.packed / "packing.json")
        self.examples = load_jsonl(paths.corpus / "examples.jsonl")
        self.comparisons = load_jsonl(paths.results / "comparisons.jsonl")
        self.context_blocks = load_jsonl(paths.corpus / "context-blocks.jsonl")
        self.events = load_jsonl(paths.corpus / "events.jsonl")
        self.plans = load_jsonl(paths.packed / "context-plans.jsonl")
        self.packed = load_jsonl(paths.packed / "packed-examples.jsonl")
        self.holistic_review = (
            load_json(paths.holistic_review)
            if paths.holistic_review is not None
            else None
        )
        self.holistic_pass_ordinals: dict[str, set[int]] = {}

        self.example_by_id = self._index(self.examples, "exampleID", "examples")
        self.comparison_by_id = self._index(
            self.comparisons, "exampleID", "comparisons"
        )
        self.block_by_id = self._index(
            self.context_blocks, "contextBlockID", "context blocks"
        )
        self.event_by_id = self._index(self.events, "sourceEventID", "events")
        self.plan_by_id = self._index(self.plans, "exampleID", "context plans")
        self.packed_by_id = self._index(self.packed, "exampleID", "packed examples")
        self.scored_examples = [
            example
            for example in self.examples
            if example["exampleID"] in self.comparison_by_id
        ]
        self._validate()
        self.summaries = [
            self._summary(example) for example in self.scored_examples
        ]

    @staticmethod
    def _index(
        rows: list[dict[str, Any]], key: str, label: str
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            value = row.get(key)
            if not isinstance(value, str) or not value:
                raise InspectorError(f"{label} row has no {key}")
            if value in result:
                raise InspectorError(f"duplicate {key} in {label}: {value}")
            result[value] = row
        return result

    def _validate(self) -> None:
        scored_ids = [value["exampleID"] for value in self.scored_examples]
        unknown_comparisons = [
            value for value in self.comparison_by_id if value not in self.example_by_id
        ]
        if unknown_comparisons:
            raise InspectorError(
                f"comparisons reference {len(unknown_comparisons)} unknown examples"
            )
        for label, indexed in (
            ("context plan", self.plan_by_id),
            ("packed example", self.packed_by_id),
        ):
            missing = [value for value in scored_ids if value not in indexed]
            if missing:
                raise InspectorError(
                    f"{label} is missing {len(missing)} scored examples"
                )
        if len(scored_ids) != len(self.comparisons):
            raise InspectorError("comparison/scored-example counts disagree")
        for example_id in scored_ids:
            semantic = self.semantic_input(example_id)
            expected = self.plan_by_id[example_id].get("semanticModelInputSHA256")
            actual = hashlib.sha256(semantic.encode()).hexdigest()
            if expected != actual:
                raise InspectorError(
                    f"semantic input digest disagrees for {example_id}"
                )
            expected_target = target_text(self.example_by_id[example_id]["target"])
            if self.comparison_by_id[example_id].get("target") != expected_target:
                raise InspectorError(f"result target disagrees for {example_id}")
        self._validate_holistic_review()

    def _validate_holistic_review(self) -> None:
        review = self.holistic_review
        if review is None:
            return
        source = review.get("source", {})
        if source.get("corpusID") != self.corpus_manifest.get("corpusID"):
            raise InspectorError("holistic review corpus ID disagrees")
        actual_digest = prediction_triples_sha256(self.comparisons)
        if source.get("predictionTriplesSHA256") != actual_digest:
            raise InspectorError("holistic review prediction digest disagrees")
        scope = review.get("scope", {})
        first = scope.get("firstOneBasedExampleOrdinal")
        last = scope.get("lastOneBasedExampleOrdinal")
        if not isinstance(first, int) or not isinstance(last, int) or first > last:
            raise InspectorError("holistic review has an invalid ordinal scope")
        available = {
            int(example["chronologicalOrdinal"]) + 1
            for example in self.scored_examples
        }
        scoped_available = {
            ordinal for ordinal in available if first <= ordinal <= last
        }
        if scope.get("examples") != len(scoped_available):
            raise InspectorError("holistic review example scope disagrees")
        models = review.get("models")
        if not isinstance(models, dict):
            raise InspectorError("holistic review has no model labels")
        for model, value in models.items():
            if not isinstance(value, dict):
                raise InspectorError(f"holistic review model is invalid: {model}")
            ordinals = value.get("passOneBasedExampleOrdinals")
            if not isinstance(ordinals, list) or not all(
                isinstance(ordinal, int) for ordinal in ordinals
            ):
                raise InspectorError(f"holistic pass ordinals are invalid: {model}")
            if len(ordinals) != len(set(ordinals)):
                raise InspectorError(f"holistic pass ordinals are duplicated: {model}")
            if value.get("passCount") != len(ordinals):
                raise InspectorError(f"holistic pass count disagrees: {model}")
            outside = [
                ordinal
                for ordinal in ordinals
                if ordinal < first or ordinal > last or ordinal not in available
            ]
            if outside:
                raise InspectorError(
                    f"holistic pass ordinals are outside the corpus scope: {model}"
                )
            self.holistic_pass_ordinals[model] = set(ordinals)

    def semantic_input(self, example_id: str) -> str:
        example = self.example_by_id[example_id]
        plan = self.plan_by_id[example_id]
        serialized: list[str] = []
        for retained in plan.get("retainedContextBlocks", []):
            override = retained.get("serializedOverride")
            if override is not None:
                serialized.append(override)
            else:
                block_id = retained.get("contextBlockID")
                block = self.block_by_id.get(block_id)
                if block is None:
                    raise InspectorError(
                        f"missing retained context block {block_id} for {example_id}"
                    )
                serialized.append(block["serialized"])
        context = "\n".join(serialized)
        query = example["query"]
        body = query if not context else context + "\n" + query
        return plan["taskInstruction"] + "\n" + body

    def _summary(self, example: dict[str, Any]) -> dict[str, Any]:
        example_id = example["exampleID"]
        comparison = self.comparison_by_id[example_id]
        packed = self.packed_by_id[example_id]
        segments = example.get("target", {}).get("segments", [])
        segment_types = {value.get("type") for value in segments}
        if segment_types == {"paste"}:
            target_type = "paste"
        elif "paste" in segment_types:
            target_type = "mixed"
        else:
            target_type = "authored"
        target = comparison.get("target", "")
        conditioning = example.get("conditioningState", {})
        destination = conditioning.get("destination", {})
        personalized = comparison.get("personalizedQwen", {})
        frozen = comparison.get("frozenQwen", {})
        frontier = comparison.get("frontier", {})
        one_based_ordinal = int(example.get("chronologicalOrdinal")) + 1
        return {
            "exampleID": example_id,
            "ordinal": example.get("chronologicalOrdinal"),
            "blockID": example.get("experimentBlockID"),
            "application": comparison.get("application")
            or destination.get("appName"),
            "window": destination.get("windowTitle"),
            "target": target,
            "targetLength": len(target),
            "targetType": target_type,
            "pasteActionCount": packed.get("pasteActionCount", 0),
            "personalizedExact": personalized.get("prediction") == target,
            "frontierExact": frontier.get("prediction") == target,
            "frozenExact": frozen.get("prediction") == target,
            "personalizedHolistic": one_based_ordinal
            in self.holistic_pass_ordinals.get(
                "personalized_qwen3.5_9b_base", set()
            ),
            "frontierHolistic": one_based_ordinal
            in self.holistic_pass_ordinals.get(
                "frozen_gpt_5.6_sol_xhigh", set()
            ),
            "personalizedSimilarity": personalized.get("predictionMetrics", {}).get(
                "normalizedLevenshteinSimilarity",
                personalized.get("characterSimilarity"),
            ),
            "frontierSimilarity": frontier.get("predictionMetrics", {}).get(
                "normalizedLevenshteinSimilarity",
                frontier.get("characterSimilarity"),
            ),
            "frozenSimilarity": frozen.get("predictionMetrics", {}).get(
                "normalizedLevenshteinSimilarity",
                frozen.get("characterSimilarity"),
            ),
            "personalizedNLL": personalized.get("meanNLL"),
            "frozenNLL": frozen.get("meanNLL"),
            "bitsSaved": comparison.get("personalizedBitsSavedVersusFrozen"),
            "retainedEvents": len(
                self.plan_by_id[example_id].get("retainedContextBlocks", [])
            ),
            "droppedEvents": packed.get("droppedContextEventCount", 0),
        }

    def meta(self) -> dict[str, Any]:
        summaries = self.experiment.get("summaries", {})
        review = self.holistic_review or {}
        review_models = review.get("models", {})
        return {
            "examples": len(self.scored_examples),
            "applications": sorted(
                {value.get("application") for value in self.summaries if value.get("application")}
            ),
            "blocks": sorted(
                {value.get("blockID") for value in self.summaries if value.get("blockID")}
            ),
            "status": self.experiment.get("status"),
            "corpusID": self.corpus_manifest.get("corpusID"),
            "auditVersion": self.experiment.get("auditVersion"),
            "packerVersion": self.packing_manifest.get("packerVersion"),
            "summaries": summaries,
            "bitsSaved": self.experiment.get(
                "personalizedCumulativeBitsSavedVersusFrozen"
            ),
            "holisticReview": {
                "reviewID": review.get("reviewID"),
                "status": review.get("status"),
                "passBar": review.get("passBar"),
                "scope": review.get("scope"),
                "uncertaintyExamplesPerModel": review.get(
                    "uncertaintyExamplesPerModel"
                ),
                "personalizedPasses": review_models.get(
                    "personalized_qwen3.5_9b_base", {}
                ).get("passCount"),
                "frontierPasses": review_models.get(
                    "frozen_gpt_5.6_sol_xhigh", {}
                ).get("passCount"),
            },
            "paths": {
                "corpus": str(self.paths.corpus),
                "packed": str(self.paths.packed),
                "results": str(self.paths.results),
                "holisticReview": (
                    str(self.paths.holistic_review)
                    if self.paths.holistic_review is not None
                    else None
                ),
            },
        }

    def detail(self, example_id: str) -> dict[str, Any]:
        if example_id not in self.example_by_id:
            raise KeyError(example_id)
        example = self.example_by_id[example_id]
        comparison = self.comparison_by_id[example_id]
        plan = self.plan_by_id[example_id]
        packed = self.packed_by_id[example_id]
        retained_events = []
        for ordinal, retained in enumerate(plan.get("retainedContextBlocks", [])):
            block_id = retained["contextBlockID"]
            block = self.block_by_id[block_id]
            serialized = retained.get("serializedOverride") or block["serialized"]
            retained_events.append(
                {
                    "ordinal": ordinal,
                    "contextBlockID": block_id,
                    "availableAt": block.get("availableAt"),
                    "sessionID": block.get("sessionID"),
                    "contentTruncated": retained.get("contentTruncated", False),
                    "projection": event_projection(serialized),
                    "serialized": serialized,
                }
            )
        labels = packed.get("labels", [])
        target_count = int(packed.get("targetTokenCount", 0))
        packing_summary = {
            key: value
            for key, value in packed.items()
            if key not in {"inputIDs", "labels", "attentionMask"}
        }
        packing_summary["inputTokenCount"] = len(packed.get("inputIDs", []))
        packing_summary["maskedLabelCount"] = sum(
            1 for value in labels if value == -100
        )
        packing_summary["lossBearingLabelCount"] = sum(
            1 for value in labels if value != -100
        )
        packing_summary["targetLabels"] = labels[-target_count:] if target_count else []
        target_event = self.event_by_id.get(example.get("targetEventID"))
        target_event_projection = None
        if target_event is not None:
            target_event_projection = event_projection(target_event["serialized"])
        return {
            "summary": self._summary(example),
            "holisticReview": {
                "reviewID": (self.holistic_review or {}).get("reviewID"),
                "passBar": (self.holistic_review or {}).get("passBar"),
                "uncertaintyExamplesPerModel": (
                    self.holistic_review or {}
                ).get("uncertaintyExamplesPerModel"),
            },
            "taskInstruction": plan.get("taskInstruction"),
            "retainedEvents": retained_events,
            "query": parsed_json(example.get("query")),
            "querySerialized": example.get("query"),
            "conditioningState": example.get("conditioningState"),
            "target": example.get("target"),
            "targetText": target_text(example.get("target", {})),
            "targetEvent": target_event_projection,
            "targetMetadata": example.get("targetMetadata"),
            "targetMask": example.get("targetMask"),
            "comparison": comparison,
            "packing": packing_summary,
            "contextPlan": plan,
            "rawExample": example,
            "semanticInputSHA256": hashlib.sha256(
                self.semantic_input(example_id).encode()
            ).hexdigest(),
        }


HTML = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Coupled · Phase 1 Inspector</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0a0b0d;
      --panel: #111317;
      --panel-2: #171a20;
      --line: #282d36;
      --text: #e9edf3;
      --muted: #8d96a5;
      --read: #73b7ff;
      --write: #ffb86b;
      --query: #bd93f9;
      --target: #65d6a6;
      --bad: #ff6b7a;
      --good: #65d6a6;
      --accent: #e8ff72;
      --q-holistic: #73d7ff;
      --gpt-holistic: #c5a8ff;
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      --sans: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); font-family: var(--sans); overflow: hidden; }
    button, input, select { font: inherit; color: inherit; }
    button, select, input { border: 1px solid var(--line); background: var(--panel-2); border-radius: 8px; }
    button { cursor: pointer; padding: 7px 10px; }
    button:hover { border-color: #515a69; }
    header { height: 62px; border-bottom: 1px solid var(--line); display: flex; align-items: center; gap: 22px; padding: 0 18px; background: #0d0f12; }
    .brand { font-weight: 750; letter-spacing: -.02em; white-space: nowrap; }
    .brand span { color: var(--accent); }
    .stats { display: flex; gap: 18px; min-width: 0; }
    .stat { font-size: 12px; color: var(--muted); white-space: nowrap; }
    .stat strong { display: block; color: var(--text); font-size: 15px; }
    .paths { margin-left: auto; color: var(--muted); font: 11px var(--mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .layout { display: grid; grid-template-columns: 390px 1fr; grid-template-rows: minmax(0, 1fr); height: calc(100vh - 62px); }
    aside { border-right: 1px solid var(--line); min-width: 0; min-height: 0; display: flex; flex-direction: column; }
    .filters { padding: 12px; border-bottom: 1px solid var(--line); display: grid; gap: 8px; }
    .filters input { width: 100%; padding: 9px 11px; }
    .filter-row { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .filters select { width: 100%; padding: 7px; min-width: 0; }
    .result-count { color: var(--muted); font-size: 12px; }
    #example-list { overflow: auto; flex: 1; }
    .example-row { padding: 11px 12px; border-bottom: 1px solid #1c2027; cursor: pointer; }
    .example-row:hover { background: #15181d; }
    .example-row.selected { background: #1d2229; box-shadow: inset 3px 0 var(--accent); }
    .row-top { display: flex; align-items: center; gap: 6px; color: var(--muted); font-size: 11px; }
    .ordinal { font-family: var(--mono); color: #c0c7d2; }
    .target-preview { margin: 7px 0; line-height: 1.35; font: 12px var(--mono); overflow: hidden; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; }
    .row-bottom { display: flex; gap: 5px; align-items: center; color: var(--muted); font-size: 10px; }
    .badge { display: inline-flex; align-items: center; border: 1px solid var(--line); padding: 2px 6px; border-radius: 99px; font-size: 10px; color: var(--muted); }
    .badge.good { border-color: #315e4d; color: var(--good); background: #10271f; }
    .badge.bad { border-color: #6a303a; color: var(--bad); background: #281318; }
    .badge.q-holistic { border-color: #24576b; color: var(--q-holistic); background: #0e2530; }
    .badge.gpt-holistic { border-color: #574478; color: var(--gpt-holistic); background: #211932; }
    .badge.read { color: var(--read); }
    .badge.write { color: var(--write); }
    main { min-width: 0; min-height: 0; overflow: hidden; display: flex; flex-direction: column; }
    .detail-header { padding: 15px 20px 12px; border-bottom: 1px solid var(--line); background: #0e1013; }
    .detail-title { display: flex; align-items: flex-start; gap: 12px; }
    .detail-title h1 { margin: 0; font-size: 16px; line-height: 1.4; font-family: var(--mono); flex: 1; overflow-wrap: anywhere; }
    .subline { color: var(--muted); font-size: 11px; margin-top: 7px; display: flex; gap: 12px; flex-wrap: wrap; }
    .tabs { display: flex; gap: 4px; margin-top: 12px; }
    .tab { color: var(--muted); background: transparent; border-color: transparent; }
    .tab.active { background: var(--panel-2); border-color: var(--line); color: var(--text); }
    #detail { flex: 1 1 auto; min-height: 0; overflow: auto; padding: 18px 20px 80px; }
    .toolbar { position: sticky; top: -18px; z-index: 3; margin: -18px -20px 14px; padding: 10px 20px; background: rgba(10,11,13,.95); border-bottom: 1px solid var(--line); display: flex; gap: 8px; backdrop-filter: blur(8px); }
    .toolbar input { padding: 7px 9px; min-width: 260px; }
    .card { background: var(--panel); border: 1px solid var(--line); border-radius: 11px; margin-bottom: 12px; overflow: hidden; }
    .card-head { padding: 9px 12px; border-bottom: 1px solid var(--line); display: flex; gap: 8px; align-items: center; color: var(--muted); font-size: 11px; }
    .card-head strong { color: var(--text); }
    .card-body { padding: 12px; }
    pre { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; font: 12px/1.55 var(--mono); color: #d9dee7; }
    .event.read-event { border-left: 3px solid var(--read); }
    .event.write-event { border-left: 3px solid var(--write); }
    .event summary { list-style: none; cursor: pointer; padding: 10px 12px; display: flex; align-items: center; gap: 8px; }
    .event summary::-webkit-details-marker { display: none; }
    .event summary::before { content: "›"; color: var(--muted); transition: transform .12s; }
    .event[open] summary::before { transform: rotate(90deg); }
    .event .event-body { border-top: 1px solid var(--line); padding: 12px; }
    .event-index { color: var(--muted); font: 10px var(--mono); }
    .spacer { flex: 1; }
    .query-card { border-left: 3px solid var(--query); }
    .target-card { border-left: 3px solid var(--target); }
    .prediction-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
    .prediction { min-height: 180px; display: flex; flex-direction: column; }
    .prediction.exact { border-color: #34705a; box-shadow: inset 0 3px var(--good); }
    .prediction .card-body { flex: 1; max-height: 52vh; overflow: auto; }
    .metrics { display: flex; gap: 8px; flex-wrap: wrap; padding: 9px 12px; border-top: 1px solid var(--line); color: var(--muted); font: 10px var(--mono); }
    .json-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    .empty { color: var(--muted); text-align: center; padding: 80px 20px; }
    .hidden { display: none !important; }
    mark { background: #5b4d12; color: #fff3a4; }
    @media (max-width: 1000px) {
      .layout { grid-template-columns: 330px 1fr; }
      .prediction-grid, .json-grid { grid-template-columns: 1fr; }
      .stats .stat:nth-child(n+4) { display: none; }
    }
  </style>
</head>
<body>
  <header>
    <div class="brand"><span>Coupled</span> Phase 1 Inspector</div>
    <div class="stats" id="stats"></div>
    <div class="paths" id="paths"></div>
  </header>
  <div class="layout">
    <aside>
      <div class="filters">
        <input id="search" type="search" placeholder="Search targets, apps, IDs…">
        <div class="filter-row">
          <select id="block"><option value="">All blocks</option></select>
          <select id="app"><option value="">All applications</option></select>
        </div>
        <div class="filter-row">
          <select id="type">
            <option value="">All target types</option>
            <option value="authored">Authored</option>
            <option value="paste">Paste only</option>
            <option value="mixed">Mixed</option>
          </select>
          <select id="outcome">
            <option value="">All outcomes</option>
            <option value="personalized-exact">Personalized exact</option>
            <option value="frontier-exact">GPT exact</option>
            <option value="personalized-holistic">Qwen holistic pass</option>
            <option value="frontier-holistic">GPT holistic pass</option>
            <option value="either-holistic">Either holistic pass</option>
            <option value="helped">Personalization helped</option>
            <option value="hurt">Personalization hurt</option>
          </select>
        </div>
        <div class="filter-row">
          <select id="sort">
            <option value="chronology">Chronological</option>
            <option value="bits-desc">Bits saved ↓</option>
            <option value="bits-asc">Bits saved ↑</option>
            <option value="length-desc">Target length ↓</option>
            <option value="similarity-desc">Personalized similarity ↓</option>
          </select>
          <button id="random">Random example</button>
        </div>
        <div class="result-count" id="result-count"></div>
      </div>
      <div id="example-list"></div>
    </aside>
    <main>
      <div class="detail-header hidden" id="detail-header">
        <div class="detail-title">
          <h1 id="detail-target"></h1>
          <button id="previous" title="Previous filtered example (K)">↑</button>
          <button id="next" title="Next filtered example (J)">↓</button>
        </div>
        <div class="subline" id="detail-subline"></div>
        <div class="tabs">
          <button class="tab active" data-tab="stream">Causal stream</button>
          <button class="tab" data-tab="predictions">Predictions</button>
          <button class="tab" data-tab="conditioning">Conditioning</button>
          <button class="tab" data-tab="packing">Packing</button>
          <button class="tab" data-tab="raw">Raw JSON</button>
        </div>
      </div>
      <div id="detail"><div class="empty">Choose an example to inspect.</div></div>
    </main>
  </div>
  <script>
    const state = { meta: null, examples: [], filtered: [], selected: null, detail: null, tab: 'stream' };
    const $ = id => document.getElementById(id);
    const esc = value => String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
    const fmt = value => value == null ? '—' : Number(value).toFixed(3);
    const pct = value => value == null ? '—' : `${(Number(value) * 100).toFixed(1)}%`;
    const targetPreview = value => value === '' ? '∅ empty completion' : value;
    const json = value => JSON.stringify(value, null, 2);

    async function getJSON(path) {
      const response = await fetch(path, {cache: 'no-store'});
      if (!response.ok) throw new Error(await response.text());
      return response.json();
    }

    function fillSelect(id, values) {
      const select = $(id);
      values.forEach(value => select.insertAdjacentHTML('beforeend', `<option value="${esc(value)}">${esc(value)}</option>`));
    }

    function renderMeta() {
      const m = state.meta;
      const personalized = m.summaries['personalized_qwen3.5_9b_base'] || {};
      const frontier = m.summaries['frozen_gpt_5.6_sol_xhigh'] || {};
      const holistic = m.holisticReview || {};
      const holisticScope = holistic.scope?.examples ?? 150;
      const holisticScore = passes => `${passes}/${holisticScope} · ${((passes / holisticScope) * 100).toFixed(1)}%`;
      $('stats').innerHTML = `
        <div class="stat"><strong>${m.examples}</strong>examples</div>
        <div class="stat"><strong>${Number(m.bitsSaved || 0).toLocaleString(undefined,{maximumFractionDigits:1})}</strong>bits saved</div>
        <div class="stat"><strong>${fmt(personalized.microTargetTokenNLL)}</strong>personalized NLL</div>
        <div class="stat"><strong>${personalized.generatedCompletion?.exactMatches ?? personalized.exactMatches ?? 0}</strong>Qwen exact</div>
        <div class="stat"><strong>${frontier.generatedCompletion?.exactMatches ?? frontier.exactMatches ?? 0}</strong>GPT exact</div>
        ${holistic.personalizedPasses == null ? '' : `<div class="stat" title="Subjective strict reviewer pass; approximately ±${holistic.uncertaintyExamplesPerModel ?? 3} examples"><strong>${holisticScore(holistic.personalizedPasses)}</strong>Q holistic</div>`}
        ${holistic.frontierPasses == null ? '' : `<div class="stat" title="Subjective strict reviewer pass; approximately ±${holistic.uncertaintyExamplesPerModel ?? 3} examples"><strong>${holisticScore(holistic.frontierPasses)}</strong>GPT holistic</div>`}`;
      $('paths').textContent = m.paths.results;
      $('paths').title = `Corpus: ${m.paths.corpus}\nPacked: ${m.paths.packed}\nResults: ${m.paths.results}\nHolistic review: ${m.paths.holisticReview || 'none'}${holistic.passBar ? `\n\nReviewer bar: ${holistic.passBar}` : ''}`;
      fillSelect('block', m.blocks);
      fillSelect('app', m.applications);
    }

    function applyFilters() {
      const query = $('search').value.trim().toLowerCase();
      const block = $('block').value;
      const app = $('app').value;
      const type = $('type').value;
      const outcome = $('outcome').value;
      state.filtered = state.examples.filter(example => {
        if (block && example.blockID !== block) return false;
        if (app && example.application !== app) return false;
        if (type && example.targetType !== type) return false;
        if (query && !`${example.target} ${example.application} ${example.window} ${example.exampleID}`.toLowerCase().includes(query)) return false;
        if (outcome === 'personalized-exact' && !example.personalizedExact) return false;
        if (outcome === 'frontier-exact' && !example.frontierExact) return false;
        if (outcome === 'personalized-holistic' && !example.personalizedHolistic) return false;
        if (outcome === 'frontier-holistic' && !example.frontierHolistic) return false;
        if (outcome === 'either-holistic' && !(example.personalizedHolistic || example.frontierHolistic)) return false;
        if (outcome === 'helped' && !(example.bitsSaved > 0)) return false;
        if (outcome === 'hurt' && !(example.bitsSaved < 0)) return false;
        return true;
      });
      const sort = $('sort').value;
      state.filtered.sort((a,b) => {
        if (sort === 'bits-desc') return (b.bitsSaved ?? -Infinity) - (a.bitsSaved ?? -Infinity);
        if (sort === 'bits-asc') return (a.bitsSaved ?? Infinity) - (b.bitsSaved ?? Infinity);
        if (sort === 'length-desc') return b.targetLength - a.targetLength;
        if (sort === 'similarity-desc') return (b.personalizedSimilarity ?? -1) - (a.personalizedSimilarity ?? -1);
        return a.ordinal - b.ordinal;
      });
      renderList();
      if (
        state.selected &&
        state.filtered.length &&
        !state.filtered.some(example => example.exampleID === state.selected)
      ) {
        selectExample(state.filtered[0].exampleID);
      }
    }

    function renderList() {
      $('result-count').textContent = `${state.filtered.length} of ${state.examples.length} examples`;
      $('example-list').innerHTML = state.filtered.map(example => `
        <div class="example-row ${state.selected === example.exampleID ? 'selected' : ''}" data-id="${esc(example.exampleID)}">
          <div class="row-top"><span class="ordinal">#${example.ordinal + 1}</span><span>${esc(example.blockID)}</span><span>·</span><span>${esc(example.application)}</span></div>
          <div class="target-preview">${esc(targetPreview(example.target))}</div>
          <div class="row-bottom">
            <span class="badge">${esc(example.targetType)}</span>
            ${example.personalizedExact ? '<span class="badge good">Q exact</span>' : ''}
            ${example.frontierExact ? '<span class="badge good">GPT exact</span>' : ''}
            ${example.personalizedHolistic ? '<span class="badge q-holistic" title="Subjective strict reviewer pass">Q holistic</span>' : ''}
            ${example.frontierHolistic ? '<span class="badge gpt-holistic" title="Subjective strict reviewer pass">GPT holistic</span>' : ''}
            <span class="spacer"></span>
            <span>${example.bitsSaved == null ? '—' : `${example.bitsSaved.toFixed(1)} bits`}</span>
          </div>
        </div>`).join('');
      document.querySelectorAll('.example-row').forEach(row => row.addEventListener('click', () => selectExample(row.dataset.id)));
    }

    async function selectExample(id) {
      state.selected = id;
      history.replaceState(null, '', `#${encodeURIComponent(id)}`);
      renderList();
      $('detail').innerHTML = '<div class="empty">Loading exact context plan…</div>';
      state.detail = await getJSON(`/api/example?id=${encodeURIComponent(id)}`);
      renderDetailHeader();
      renderTab();
      document.querySelector('.example-row.selected')?.scrollIntoView({block:'nearest'});
    }

    function renderDetailHeader() {
      const d = state.detail;
      $('detail-header').classList.remove('hidden');
      $('detail-target').textContent = targetPreview(d.targetText);
      const s = d.summary;
      $('detail-subline').innerHTML = `
        <span>#${s.ordinal + 1}</span><span>${esc(s.blockID)}</span><span>${esc(s.application)}</span>
        <span>${esc(s.targetType)}</span><span>${s.targetLength} chars</span>
        ${s.personalizedHolistic ? '<span class="badge q-holistic">Q holistic pass</span>' : ''}
        ${s.frontierHolistic ? '<span class="badge gpt-holistic">GPT holistic pass</span>' : ''}
        <span>${d.retainedEvents.length} retained events</span><span>${s.droppedEvents} dropped</span>
        <span title="${esc(s.exampleID)}">${esc(s.exampleID.slice(-20))}</span>`;
    }

    function segmentText(segment) {
      const label = segment.type === 'paste' ? 'PASTE PAYLOAD' : (segment.type || 'TEXT').toUpperCase();
      return `<div class="card"><div class="card-head"><strong>${esc(label)}</strong></div><div class="card-body"><pre>${esc(segment.content || '')}</pre></div></div>`;
    }

    function eventContent(projection) {
      if (projection.kind === 'read') return `<pre>${esc(projection.content || '')}</pre>`;
      const segments = Array.isArray(projection.segments) ? projection.segments : [];
      if (!segments.length) return `<pre>[${esc(projection.operation || 'write')}]${projection.removedContent ? `\nremoved: ${esc(projection.removedContent)}` : ''}</pre>`;
      return segments.map(segment => `<div style="margin-bottom:10px"><span class="badge ${segment.type === 'paste' ? 'read' : 'write'}">${esc(segment.type)}</span><pre style="margin-top:6px">${esc(segment.content || '')}</pre></div>`).join('');
    }

    function renderStream() {
      const d = state.detail;
      const lastOpen = Math.max(0, d.retainedEvents.length - 4);
      return `
        <div class="toolbar">
          <input id="history-search" type="search" placeholder="Filter retained event text…">
          <button id="expand-events">Expand all</button><button id="collapse-events">Collapse all</button>
          <span class="spacer"></span><span class="badge">model-visible</span>
        </div>
        <div class="card"><div class="card-head"><strong>TASK INSTRUCTION</strong></div><div class="card-body"><pre>${esc(d.taskInstruction)}</pre></div></div>
        <div id="event-stream">
          ${d.retainedEvents.map((event,index) => {
            const p = event.projection;
            const haystack = json(p.raw).toLowerCase();
            return `<details class="card event ${p.kind === 'read' ? 'read-event' : 'write-event'}" data-search="${esc(haystack)}" ${index >= lastOpen ? 'open' : ''}>
              <summary><span class="event-index">${index + 1}/${d.retainedEvents.length}</span><span class="badge ${p.kind}">${esc(p.kind.toUpperCase())}</span><strong>${esc(p.application || 'Unknown app')}</strong><span>${esc(p.window || '')}</span>${event.contentTruncated ? '<span class="badge bad">oldest event truncated</span>' : ''}<span class="spacer"></span><span class="event-index">${esc(event.availableAt || '')}</span></summary>
              <div class="event-body">${eventContent(p)}</div>
            </details>`;
          }).join('')}
        </div>
        <div class="card query-card"><div class="card-head"><strong>CONDITIONING QUERY</strong><span>destination + cursor + clipboard</span></div><div class="card-body"><pre>${esc(json(d.query))}</pre></div></div>
        <div class="card target-card"><div class="card-head"><strong>LOSS TARGET</strong><span>target only; input is masked</span></div><div class="card-body"><pre>${esc(d.targetText)}</pre></div></div>
        <div style="margin-top:16px"><div class="card-head"><strong>OBSERVED WRITE SEGMENTS</strong></div>${(d.target.segments || []).map(segmentText).join('')}</div>`;
    }

    function predictionCard(name, row, nllAvailable, holisticPass, holisticClass) {
      const exact = row.prediction === state.detail.targetText;
      const metrics = row.predictionMetrics || {};
      const similarity = metrics.normalizedLevenshteinSimilarity ?? row.characterSimilarity;
      return `<div class="card prediction ${exact ? 'exact' : ''}">
        <div class="card-head"><strong>${esc(name)}</strong><span class="spacer"></span>${holisticPass ? `<span class="badge ${holisticClass}" title="Subjective strict reviewer pass">holistic pass</span>` : ''}${exact ? '<span class="badge good">exact</span>' : ''}</div>
        <div class="card-body"><pre>${esc(targetPreview(row.prediction))}</pre></div>
        <div class="metrics"><span>edit similarity ${pct(similarity)}</span><span>prefix ${metrics.correctPrefixCharacters ?? '—'} chars</span>${nllAvailable ? `<span>NLL ${fmt(row.meanNLL)}</span>` : ''}</div>
      </div>`;
    }

    function renderPredictions() {
      const c = state.detail.comparison;
      const s = state.detail.summary;
      const review = state.detail.holisticReview || {};
      return `<div class="card target-card"><div class="card-head"><strong>HUMAN TARGET</strong></div><div class="card-body"><pre>${esc(state.detail.targetText)}</pre></div></div>
        <div class="prediction-grid">
          ${predictionCard('Frozen Qwen3.5-9B', c.frozenQwen || {}, true, false, '')}
          ${predictionCard('Personalized Qwen3.5-9B', c.personalizedQwen || {}, true, s.personalizedHolistic, 'q-holistic')}
          ${predictionCard('GPT-5.6-sol xhigh', c.frontier || {}, false, s.frontierHolistic, 'gpt-holistic')}
        </div>
        ${review.passBar ? `<div class="card"><div class="card-head"><strong>HOLISTIC REVIEW BAR</strong><span class="spacer"></span><span>subjective · approximately ±${review.uncertaintyExamplesPerModel ?? 3} examples</span></div><div class="card-body"><pre>${esc(review.passBar)}</pre></div></div>` : ''}
        <div class="card"><div class="card-head"><strong>PAIRED PERSONALIZATION</strong></div><div class="card-body"><pre>Bits saved versus frozen: ${fmt(c.personalizedBitsSavedVersusFrozen)}\nPaste actions in target: ${c.pasteActionCount ?? 0}</pre></div></div>`;
    }

    function jsonCard(title, value) {
      return `<div class="card"><div class="card-head"><strong>${esc(title)}</strong></div><div class="card-body"><pre>${esc(json(value))}</pre></div></div>`;
    }

    function renderConditioning() {
      const c = state.detail.conditioningState || {};
      return `<div class="json-grid">
        ${jsonCard('Destination', c.destination)}
        ${jsonCard('Cursor context', c.cursorContext)}
        ${jsonCard('Clipboard', c.clipboard)}
        ${jsonCard('Capture semantics', {captureSemantics:c.captureSemantics,capturedAt:c.capturedAt,inputInterceptedAt:c.inputInterceptedAt,sourceObservationID:c.sourceObservationID})}
      </div>${jsonCard('Target metadata', state.detail.targetMetadata)}${jsonCard('Target mask', state.detail.targetMask)}`;
    }

    function renderPacking() {
      const p = state.detail.packing;
      return `<div class="stats" style="margin-bottom:14px">
        <div class="stat"><strong>${p.modelInputTokenCount}</strong>input tokens</div>
        <div class="stat"><strong>${p.targetTokenCount}</strong>target tokens</div>
        <div class="stat"><strong>${p.droppedContextEventCount}</strong>dropped events</div>
        <div class="stat"><strong>${p.partiallyRetainedContextEventCount}</strong>partial events</div>
        <div class="stat"><strong>${p.maskedLabelCount}</strong>masked labels</div>
        <div class="stat"><strong>${p.lossBearingLabelCount}</strong>loss labels</div>
      </div>${jsonCard('Packing record without full token arrays', p)}${jsonCard('Frozen context plan', state.detail.contextPlan)}`;
    }

    function renderRaw() {
      return `${jsonCard('Compiled example', state.detail.rawExample)}${jsonCard('Comparison result', state.detail.comparison)}${jsonCard('Target event projection', state.detail.targetEvent)}${jsonCard('Context plan', state.detail.contextPlan)}`;
    }

    function renderTab() {
      document.querySelectorAll('.tab').forEach(tab => tab.classList.toggle('active', tab.dataset.tab === state.tab));
      const renderers = {stream:renderStream,predictions:renderPredictions,conditioning:renderConditioning,packing:renderPacking,raw:renderRaw};
      $('detail').innerHTML = renderers[state.tab]();
      if (state.tab === 'stream') {
        $('expand-events').addEventListener('click', () => document.querySelectorAll('.event').forEach(value => value.open = true));
        $('collapse-events').addEventListener('click', () => document.querySelectorAll('.event').forEach(value => value.open = false));
        $('history-search').addEventListener('input', event => {
          const query = event.target.value.toLowerCase();
          document.querySelectorAll('.event').forEach(value => value.classList.toggle('hidden', query && !value.dataset.search.includes(query)));
        });
      }
    }

    function moveSelection(delta) {
      if (!state.filtered.length) return;
      let index = state.filtered.findIndex(value => value.exampleID === state.selected);
      index = index < 0 ? 0 : Math.max(0, Math.min(state.filtered.length - 1, index + delta));
      selectExample(state.filtered[index].exampleID);
    }

    async function boot() {
      [state.meta, state.examples] = await Promise.all([getJSON('/api/meta'), getJSON('/api/examples')]);
      renderMeta();
      applyFilters();
      const hash = decodeURIComponent(location.hash.slice(1));
      const initial = state.examples.some(value => value.exampleID === hash) ? hash : state.examples[0]?.exampleID;
      if (initial) selectExample(initial);
    }

    ['search','block','app','type','outcome','sort'].forEach(id => $(id).addEventListener(id === 'search' ? 'input' : 'change', applyFilters));
    $('random').addEventListener('click', () => state.filtered.length && selectExample(state.filtered[Math.floor(Math.random()*state.filtered.length)].exampleID));
    $('previous').addEventListener('click', () => moveSelection(-1));
    $('next').addEventListener('click', () => moveSelection(1));
    document.querySelectorAll('.tab').forEach(tab => tab.addEventListener('click', () => {state.tab = tab.dataset.tab; renderTab();}));
    document.addEventListener('keydown', event => {
      if (event.target.matches('input,select,textarea')) return;
      if (event.key === 'j' || event.key === 'ArrowDown') { event.preventDefault(); moveSelection(1); }
      if (event.key === 'k' || event.key === 'ArrowUp') { event.preventDefault(); moveSelection(-1); }
    });
    boot().catch(error => $('detail').innerHTML = `<div class="empty">${esc(error.message)}</div>`);
  </script>
</body>
</html>'''


class InspectorHandler(BaseHTTPRequestHandler):
    server: "InspectorServer"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path == "/":
                self._send(HTTPStatus.OK, HTML.encode(), "text/html; charset=utf-8")
            elif parsed.path == "/api/meta":
                self._json(self.server.store.meta())
            elif parsed.path == "/api/examples":
                self._json(self.server.store.summaries)
            elif parsed.path == "/api/example":
                query = urllib.parse.parse_qs(parsed.query)
                example_id = query.get("id", [None])[0]
                if not example_id:
                    self._error(HTTPStatus.BAD_REQUEST, "missing example id")
                    return
                try:
                    self._json(self.server.store.detail(example_id))
                except KeyError:
                    self._error(HTTPStatus.NOT_FOUND, "unknown example id")
            else:
                self._error(HTTPStatus.NOT_FOUND, "not found")
        except (BrokenPipeError, ConnectionResetError):
            return

    def _json(self, value: Any) -> None:
        self._send(
            HTTPStatus.OK,
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(),
            "application/json; charset=utf-8",
        )

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._send(
            status,
            json.dumps({"error": message}).encode(),
            "application/json; charset=utf-8",
        )

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "img-src 'self' data:; object-src 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string: str, *arguments: Any) -> None:
        if os.environ.get("COUPLED_INSPECTOR_HTTP_LOG") == "1":
            super().log_message(format_string, *arguments)


class InspectorServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], store: DatasetStore):
        super().__init__(address, InspectorHandler)
        self.store = store


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open a read-only localhost UI for a Phase 1 experiment."
    )
    parser.add_argument("--results", type=Path, help="results directory; latest by default")
    parser.add_argument("--corpus", type=Path, help="override discovered corpus directory")
    parser.add_argument("--packed", type=Path, help="override discovered packed directory")
    parser.add_argument(
        "--holistic-review",
        type=Path,
        help="override the optional subjective holistic-review labels",
    )
    parser.add_argument("--port", type=int, default=8765, help="localhost port (default: 8765)")
    parser.add_argument("--no-open", action="store_true", help="do not open the browser")
    parser.add_argument("--check", action="store_true", help="validate artifacts and exit")
    return parser.parse_args()


def main() -> None:
    arguments = parse_arguments()
    project = Path(__file__).resolve().parent.parent
    paths = discover_paths(
        project,
        arguments.results,
        arguments.corpus,
        arguments.packed,
        arguments.holistic_review,
    )
    store = DatasetStore(paths)
    if arguments.check:
        print(
            f"Phase 1 inspector validation passed: {len(store.scored_examples)} scored examples, "
            f"{len(store.context_blocks)} context blocks."
        )
        print(f"Corpus:  {paths.corpus}")
        print(f"Packed:  {paths.packed}")
        print(f"Results: {paths.results}")
        if paths.holistic_review is not None:
            print(f"Review:  {paths.holistic_review}")
        return

    if not 0 <= arguments.port <= 65535:
        raise InspectorError("port must be between 0 and 65535")
    server = InspectorServer(("127.0.0.1", arguments.port), store)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    print(f"Coupled Phase 1 Inspector: {url}")
    print(
        f"Loaded {len(store.scored_examples)} scored examples from {paths.results.name}"
    )
    print("The server is read-only and bound to localhost. Press Ctrl-C to stop.")
    if not arguments.no_open:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping inspector.")
    finally:
        server.server_close()


if __name__ == "__main__":
    try:
        main()
    except InspectorError as error:
        raise SystemExit(f"phase1-data-inspector: {error}") from error

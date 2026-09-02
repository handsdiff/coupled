#!/usr/bin/env python3
"""Read-only local browser for Phase 1 corpus, packing, and result artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


PASTE_MARKER = "<|paste|>"

INKLING_ARMS = (
    "frozen_inkling_small_reasoning_off",
    "frozen_inkling_small_reasoning_on",
    "personalized_inkling_small_reasoning_off",
    "personalized_inkling_small_reasoning_on",
)

INKLING_ARM_LABELS = {
    "frozen_inkling_small_reasoning_off": "Frozen Inkling · reasoning off",
    "frozen_inkling_small_reasoning_on": "Frozen Inkling · reasoning on",
    "personalized_inkling_small_reasoning_off": "Trained Inkling · reasoning off",
    "personalized_inkling_small_reasoning_on": "Trained Inkling · reasoning on",
}

COMPANION_ARM_LABELS = {
    "frozen_qwen3.5_9b_base": "Frozen Qwen3.5-9B Base",
    "personalized_qwen3.5_9b_base": "Trained Qwen3.5-9B Base",
    "frozen_gpt_5.6_sol_xhigh": "GPT-5.6-sol xhigh",
    "frozen_gpt_5.4_xhigh": "GPT-5.4 xhigh",
    "frozen_gpt_5.5_xhigh": "GPT-5.5 xhigh",
    "frozen_gpt_5.6_sol_xhigh_128k": "GPT-5.6-sol xhigh · 128K",
    "personalized_inkling_small_reasoning_off_native_v5": "Trained Inkling · native-loss v5",
}


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
    mode: str
    comparison_results: Path | None
    qwen_scores: Path | None
    gpt54_results: Path | None
    gpt55_results: Path | None
    gpt56_128k_results: Path | None
    gpt56_128k_packed: Path | None
    inkling_v5_results: Path | None
    comparison_holistic_review: Path | None
    suggestion_eligibility: Path | None


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
    comparison_results_argument: Path | None,
    qwen_scores_argument: Path | None,
    gpt54_results_argument: Path | None,
    gpt55_results_argument: Path | None,
    gpt56_128k_results_argument: Path | None,
    gpt56_128k_packed_argument: Path | None,
    inkling_v5_results_argument: Path | None,
    comparison_holistic_review_argument: Path | None,
    suggestion_eligibility_argument: Path | None,
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
    inkling_mode = (results / "inkling.json").is_file()
    experiment = load_json(
        results / ("inkling.json" if inkling_mode else "experiment.json")
    )
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
        expected_prediction_digest = (
            sha256(results / "scores.jsonl")
            if inkling_mode
            else prediction_triples_sha256(load_jsonl(results / "comparisons.jsonl"))
        )
        review_candidates: list[Path] = []
        for review_path in (project / "episode-review").glob("*holistic*.json"):
            try:
                review = load_json(review_path)
            except InspectorError:
                continue
            review_source = review.get("source", {})
            digest_matches = (
                review_source.get("inklingScoresSHA256") == expected_prediction_digest
                if inkling_mode
                else review_source.get("predictionTriplesSHA256")
                == expected_prediction_digest
            )
            if review_source.get("corpusID") == expected_corpus_id and digest_matches:
                review_candidates.append(review_path)
        holistic_review = (
            preferred_candidate(review_candidates) if review_candidates else None
        )

    required = {
        results / ("inkling.json" if inkling_mode else "experiment.json"),
        results / ("scores.jsonl" if inkling_mode else "comparisons.jsonl"),
        corpus / "corpus.json",
        corpus / "examples.jsonl",
        corpus / "events.jsonl",
        corpus / "context-blocks.jsonl",
        packed / "packing.json",
        packed / "context-plans.jsonl",
        packed / "packed-examples.jsonl",
    }
    missing = sorted(str(path) for path in required if not path.is_file())
    comparison_results = (
        comparison_results_argument.expanduser().resolve()
        if comparison_results_argument is not None
        else None
    )
    qwen_scores = (
        qwen_scores_argument.expanduser().resolve()
        if qwen_scores_argument is not None
        else None
    )
    gpt54_results = (
        gpt54_results_argument.expanduser().resolve()
        if gpt54_results_argument is not None
        else None
    )
    gpt55_results = (
        gpt55_results_argument.expanduser().resolve()
        if gpt55_results_argument is not None
        else None
    )
    gpt56_128k_results = (
        gpt56_128k_results_argument.expanduser().resolve()
        if gpt56_128k_results_argument is not None
        else None
    )
    gpt56_128k_packed = (
        gpt56_128k_packed_argument.expanduser().resolve()
        if gpt56_128k_packed_argument is not None
        else None
    )
    inkling_v5_results = (
        inkling_v5_results_argument.expanduser().resolve()
        if inkling_v5_results_argument is not None
        else None
    )
    comparison_holistic_review = (
        comparison_holistic_review_argument.expanduser().resolve()
        if comparison_holistic_review_argument is not None
        else None
    )
    suggestion_eligibility = (
        suggestion_eligibility_argument.expanduser().resolve()
        if suggestion_eligibility_argument is not None
        else None
    )
    optional_required = []
    if comparison_results is not None:
        optional_required.extend(
            [comparison_results / "experiment.json", comparison_results / "comparisons.jsonl"]
        )
    if qwen_scores is not None:
        optional_required.append(qwen_scores / "scores.jsonl")
    if gpt54_results is not None:
        optional_required.extend(
            [gpt54_results / "model.json", gpt54_results / "scores.jsonl"]
        )
    if gpt55_results is not None:
        optional_required.extend(
            [gpt55_results / "model.json", gpt55_results / "scores.jsonl"]
        )
    if (gpt56_128k_results is None) != (gpt56_128k_packed is None):
        raise InspectorError(
            "GPT-5.6 128K results and packed inputs must be supplied together"
        )
    if gpt56_128k_results is not None and gpt56_128k_packed is not None:
        optional_required.extend(
            [
                gpt56_128k_results / "window.json",
                gpt56_128k_results / "scores.jsonl",
                gpt56_128k_packed / "packing.json",
                gpt56_128k_packed / "semantic-examples.jsonl",
            ]
        )
    if inkling_v5_results is not None:
        optional_required.extend(
            [
                inkling_v5_results / "stability.json",
                inkling_v5_results / "scores.jsonl",
                inkling_v5_results / "updates.jsonl",
            ]
        )
    if comparison_holistic_review is not None:
        optional_required.append(comparison_holistic_review)
    if suggestion_eligibility is not None:
        optional_required.append(suggestion_eligibility)
    missing.extend(str(path) for path in optional_required if not path.is_file())
    if missing:
        raise InspectorError("missing required artifacts:\n" + "\n".join(missing))
    return ArtifactPaths(
        project=project,
        corpus=corpus,
        packed=packed,
        results=results,
        holistic_review=holistic_review,
        mode="inkling" if inkling_mode else "qwen-gpt",
        comparison_results=comparison_results,
        qwen_scores=qwen_scores,
        gpt54_results=gpt54_results,
        gpt55_results=gpt55_results,
        gpt56_128k_results=gpt56_128k_results,
        gpt56_128k_packed=gpt56_128k_packed,
        inkling_v5_results=inkling_v5_results,
        comparison_holistic_review=comparison_holistic_review,
        suggestion_eligibility=suggestion_eligibility,
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
        self.inkling_mode = paths.mode == "inkling"
        self.experiment = load_json(
            paths.results / ("inkling.json" if self.inkling_mode else "experiment.json")
        )
        self.corpus_manifest = load_json(paths.corpus / "corpus.json")
        self.packing_manifest = load_json(paths.packed / "packing.json")
        self.examples = load_jsonl(paths.corpus / "examples.jsonl")
        self.inkling_scores = (
            load_jsonl(paths.results / "scores.jsonl") if self.inkling_mode else []
        )
        self.comparisons = (
            self._group_inkling_scores(self.inkling_scores)
            if self.inkling_mode
            else load_jsonl(paths.results / "comparisons.jsonl")
        )
        self.updates = (
            load_jsonl(paths.results / "updates.jsonl")
            if self.inkling_mode and (paths.results / "updates.jsonl").is_file()
            else []
        )
        self.comparison_experiment = (
            load_json(paths.comparison_results / "experiment.json")
            if paths.comparison_results is not None
            else None
        )
        self.companion_comparisons = (
            load_jsonl(paths.comparison_results / "comparisons.jsonl")
            if paths.comparison_results is not None
            else []
        )
        self.qwen_score_rows = (
            load_jsonl(paths.qwen_scores / "scores.jsonl")
            if paths.qwen_scores is not None
            else []
        )
        self.gpt54_manifest = (
            load_json(paths.gpt54_results / "model.json")
            if paths.gpt54_results is not None
            else None
        )
        self.gpt54_score_rows = (
            load_jsonl(paths.gpt54_results / "scores.jsonl")
            if paths.gpt54_results is not None
            else []
        )
        self.gpt55_manifest = (
            load_json(paths.gpt55_results / "model.json")
            if paths.gpt55_results is not None
            else None
        )
        self.gpt55_score_rows = (
            load_jsonl(paths.gpt55_results / "scores.jsonl")
            if paths.gpt55_results is not None
            else []
        )
        self.gpt56_128k_manifest = (
            load_json(paths.gpt56_128k_results / "window.json")
            if paths.gpt56_128k_results is not None
            else None
        )
        self.gpt56_128k_score_rows = (
            load_jsonl(paths.gpt56_128k_results / "scores.jsonl")
            if paths.gpt56_128k_results is not None
            else []
        )
        self.gpt56_128k_packing_manifest = (
            load_json(paths.gpt56_128k_packed / "packing.json")
            if paths.gpt56_128k_packed is not None
            else None
        )
        self.gpt56_128k_semantic_rows = (
            load_jsonl(paths.gpt56_128k_packed / "semantic-examples.jsonl")
            if paths.gpt56_128k_packed is not None
            else []
        )
        self.gpt56_128k_semantic_by_id = self._index(
            self.gpt56_128k_semantic_rows,
            "exampleID",
            "GPT-5.6 128K semantic examples",
        )
        self.inkling_v5_manifest = (
            load_json(paths.inkling_v5_results / "stability.json")
            if paths.inkling_v5_results is not None
            else None
        )
        self.inkling_v5_score_rows = (
            load_jsonl(paths.inkling_v5_results / "scores.jsonl")
            if paths.inkling_v5_results is not None
            else []
        )
        self.inkling_v5_updates = (
            load_jsonl(paths.inkling_v5_results / "updates.jsonl")
            if paths.inkling_v5_results is not None
            else []
        )
        self.supplemental_holistic_review = (
            load_json(paths.comparison_holistic_review)
            if paths.comparison_holistic_review is not None
            else None
        )
        self.model_labels: dict[str, str] = {}
        if self.companion_comparisons:
            self.model_labels.update(
                {
                    key: value
                    for key, value in COMPANION_ARM_LABELS.items()
                    if key
                    not in {
                        "frozen_gpt_5.4_xhigh",
                        "frozen_gpt_5.6_sol_xhigh_128k",
                        "personalized_inkling_small_reasoning_off_native_v5",
                    }
                }
            )
        if self.gpt54_score_rows:
            self.model_labels["frozen_gpt_5.4_xhigh"] = COMPANION_ARM_LABELS[
                "frozen_gpt_5.4_xhigh"
            ]
        if self.gpt55_score_rows:
            self.model_labels["frozen_gpt_5.5_xhigh"] = COMPANION_ARM_LABELS[
                "frozen_gpt_5.5_xhigh"
            ]
        if self.gpt56_128k_score_rows:
            self.model_labels["frozen_gpt_5.6_sol_xhigh_128k"] = (
                COMPANION_ARM_LABELS["frozen_gpt_5.6_sol_xhigh_128k"]
            )
        if self.inkling_v5_score_rows:
            self.model_labels["personalized_inkling_small_reasoning_off_native_v5"] = (
                COMPANION_ARM_LABELS[
                    "personalized_inkling_small_reasoning_off_native_v5"
                ]
            )
        self.model_arms = tuple(self.model_labels)
        if self.inkling_mode:
            self._merge_companion_arms()
        self.context_blocks = load_jsonl(paths.corpus / "context-blocks.jsonl")
        self.events = load_jsonl(paths.corpus / "events.jsonl")
        self.plans = load_jsonl(paths.packed / "context-plans.jsonl")
        self.packed = load_jsonl(paths.packed / "packed-examples.jsonl")
        self.holistic_review = (
            load_json(paths.holistic_review)
            if paths.holistic_review is not None
            else None
        )
        self.suggestion_eligibility = (
            load_json(paths.suggestion_eligibility)
            if paths.suggestion_eligibility is not None
            else None
        )
        self.holistic_pass_ordinals: dict[str, set[int]] = {}
        self.score_eligible_ordinals: set[int] = set()
        self.suggestion_timing_by_ordinal: dict[int, dict[str, Any]] = {}
        self.minimum_net_savings_seconds = 0.0
        self.human_input_duration_by_ordinal: dict[int, float] = {}
        self.human_input_over_3_seconds_ordinals: set[int] = set()
        self.human_input_over_3_seconds_subset: dict[str, Any] = {}

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
    def _group_inkling_scores(
        scores: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for score in scores:
            example_id = score.get("exampleID")
            arm = score.get("arm")
            if not isinstance(example_id, str) or not isinstance(arm, str):
                raise InspectorError("Inkling score is missing exampleID or arm")
            row = grouped.setdefault(
                example_id,
                {
                    "exampleID": example_id,
                    "target": score.get("target"),
                    "application": score.get("application"),
                    "arms": {},
                },
            )
            if row["target"] != score.get("target"):
                raise InspectorError(f"Inkling targets disagree for {example_id}")
            if arm in row["arms"]:
                raise InspectorError(f"duplicate Inkling arm for {example_id}: {arm}")
            row["arms"][arm] = score
        for example_id, row in grouped.items():
            missing = [arm for arm in INKLING_ARMS if arm not in row["arms"]]
            if missing:
                raise InspectorError(
                    f"Inkling example {example_id} is missing arms: {', '.join(missing)}"
                )
        return list(grouped.values())

    def _merge_companion_arms(self) -> None:
        grouped_by_id = {row["exampleID"]: row for row in self.comparisons}
        qwen_by_key = {
            (row["exampleID"], row["arm"]): row for row in self.qwen_score_rows
        }
        for comparison in self.companion_comparisons:
            example_id = comparison["exampleID"]
            destination = grouped_by_id.get(example_id)
            if destination is None:
                raise InspectorError(
                    f"companion comparison references unknown example {example_id}"
                )
            mappings = {
                "frozen_qwen3.5_9b_base": comparison.get("frozenQwen", {}),
                "personalized_qwen3.5_9b_base": comparison.get(
                    "personalizedQwen", {}
                ),
                "frozen_gpt_5.6_sol_xhigh": comparison.get("frontier", {}),
            }
            for arm, source in mappings.items():
                normalized = dict(source)
                qwen_source = qwen_by_key.get((example_id, arm))
                if qwen_source is not None:
                    normalized["generationLatencySeconds"] = qwen_source.get(
                        "generationLatencySeconds"
                    )
                    normalized["targetLikelihoodLatencySeconds"] = qwen_source.get(
                        "targetLikelihoodLatencySeconds"
                    )
                else:
                    normalized["generationLatencySeconds"] = source.get(
                        "latencySeconds"
                    )
                if arm.startswith("frozen_gpt"):
                    normalized["estimatedProviderCostUSDAtFrozenRates"] = (
                        source.get("apiEquivalentCost", {})
                        .get("estimatedUSD", {})
                        .get("total")
                    )
                else:
                    normalized["estimatedProviderCostUSDAtFrozenRates"] = (
                        source.get("estimatedCost", {})
                        .get("estimatedUSD", {})
                        .get("combinedScoringAndGeneration")
                    )
                normalized["generationEligibleForEvaluation"] = True
                normalized["generationDisposition"] = "accepted"
                destination["arms"][arm] = normalized

        for source in self.gpt54_score_rows:
            example_id = source["exampleID"]
            destination = grouped_by_id.get(example_id)
            if destination is None:
                raise InspectorError(
                    f"GPT-5.4 score references unknown example {example_id}"
                )
            normalized = dict(source)
            usage = source.get("usage", {})
            estimated_cost = (
                float(usage.get("input_tokens", 0)) * 2.50 / 1_000_000
                + float(usage.get("output_tokens", 0)) * 15.00 / 1_000_000
            )
            normalized["generationLatencySeconds"] = source.get("latencySeconds")
            normalized["estimatedProviderCostUSDAtFrozenRates"] = estimated_cost
            normalized["generationEligibleForEvaluation"] = True
            normalized["generationDisposition"] = "accepted"
            destination["arms"]["frozen_gpt_5.4_xhigh"] = normalized

        for source in self.gpt55_score_rows:
            example_id = source["exampleID"]
            destination = grouped_by_id.get(example_id)
            if destination is None:
                raise InspectorError(
                    f"GPT-5.5 score references unknown example {example_id}"
                )
            normalized = dict(source)
            normalized["generationLatencySeconds"] = source.get("latencySeconds")
            # This was a ChatGPT-subscription run. There is no attributable
            # marginal invoice or frozen API rate for this route.
            normalized["estimatedProviderCostUSDAtFrozenRates"] = None
            normalized["generationEligibleForEvaluation"] = True
            normalized["generationDisposition"] = "accepted"
            destination["arms"]["frozen_gpt_5.5_xhigh"] = normalized

        for source in self.gpt56_128k_score_rows:
            example_id = source["exampleID"]
            destination = grouped_by_id.get(example_id)
            if destination is None:
                raise InspectorError(
                    f"GPT-5.6 128K score references unknown example {example_id}"
                )
            normalized = dict(source)
            usage = source.get("usage", {})
            estimated_cost = (
                float(usage.get("input_tokens", 0)) * 5.00 / 1_000_000
                + float(usage.get("output_tokens", 0)) * 30.00 / 1_000_000
            )
            normalized["generationLatencySeconds"] = source.get("latencySeconds")
            normalized["estimatedProviderCostUSDAtFrozenRates"] = estimated_cost
            normalized["generationEligibleForEvaluation"] = True
            normalized["generationDisposition"] = "accepted"
            normalized["inputContextLabel"] = "128K packed context"
            destination["arms"]["frozen_gpt_5.6_sol_xhigh_128k"] = normalized

        for source in self.inkling_v5_score_rows:
            example_id = source["exampleID"]
            destination = grouped_by_id.get(example_id)
            if destination is None:
                raise InspectorError(
                    f"Inkling native-loss v5 score references unknown example {example_id}"
                )
            normalized = dict(source)
            normalized["latencySeconds"] = source.get("generationLatencySeconds")
            destination["arms"][
                "personalized_inkling_small_reasoning_off_native_v5"
            ] = normalized

        for example_id, row in grouped_by_id.items():
            missing = [arm for arm in self.model_arms if arm not in row["arms"]]
            if missing:
                raise InspectorError(
                    f"unified example {example_id} is missing arms: {', '.join(missing)}"
                )
            row["arms"] = {arm: row["arms"][arm] for arm in self.model_arms}

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
        corpus_id = self.corpus_manifest.get("corpusID")
        for label, manifest in (
            ("companion comparison", self.comparison_experiment),
            ("GPT-5.4", self.gpt54_manifest),
            ("GPT-5.5", self.gpt55_manifest),
            ("GPT-5.6 128K", self.gpt56_128k_manifest),
        ):
            if manifest is not None and manifest.get("source", {}).get("corpusID") != corpus_id:
                raise InspectorError(f"{label} corpus ID disagrees")
        if (
            self.inkling_v5_manifest is not None
            and self.inkling_v5_manifest.get("source", {}).get("corpusID")
            != corpus_id
        ):
            raise InspectorError("Inkling native-loss v5 corpus ID disagrees")
        if (
            self.gpt56_128k_packing_manifest is not None
            and self.gpt56_128k_packing_manifest.get("source", {}).get("corpusID")
            != corpus_id
        ):
            raise InspectorError("GPT-5.6 128K packed-input corpus ID disagrees")
        if self.gpt56_128k_manifest is not None and self.paths.gpt56_128k_packed:
            semantic_path = self.paths.gpt56_128k_packed / "semantic-examples.jsonl"
            semantic_digest = sha256(semantic_path)
            if (
                self.gpt56_128k_manifest.get("source", {}).get(
                    "semanticExamplesSHA256"
                )
                != semantic_digest
                or self.gpt56_128k_packing_manifest.get(
                    "artifactDigestsSHA256", {}
                ).get("semantic-examples.jsonl")
                != semantic_digest
            ):
                raise InspectorError("GPT-5.6 128K semantic-input digest disagrees")
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
            if self.inkling_mode:
                for arm, score in self.comparison_by_id[example_id]["arms"].items():
                    score_digest = score.get("semanticModelInputSHA256")
                    arm_expected = expected
                    if arm == "frozen_gpt_5.6_sol_xhigh_128k":
                        semantic_row = self.gpt56_128k_semantic_by_id.get(example_id)
                        if semantic_row is None:
                            raise InspectorError(
                                f"GPT-5.6 128K packed input is missing {example_id}"
                            )
                        arm_expected = semantic_row.get("semanticModelInputSHA256")
                        if semantic_row.get("target") != expected_target:
                            raise InspectorError(
                                f"GPT-5.6 128K packed target disagrees for {example_id}"
                            )
                    if score_digest is not None and score_digest != arm_expected:
                        raise InspectorError(
                            f"model semantic input disagrees for {example_id}/{arm}"
                        )
        self._validate_holistic_review()
        self._validate_supplemental_holistic_review()
        self._validate_suggestion_eligibility()

    def _validate_suggestion_eligibility(self) -> None:
        artifact = self.suggestion_eligibility
        available = {
            int(example["chronologicalOrdinal"]) + 1: example
            for example in self.scored_examples
        }
        if artifact is None:
            self.score_eligible_ordinals = set(available)
            return
        source = artifact.get("source", {})
        if source.get("corpusID") != self.corpus_manifest.get("corpusID"):
            raise InspectorError("suggestion eligibility corpus ID disagrees")
        scope = artifact.get("scope", {})
        if scope.get("examples") != len(available):
            raise InspectorError("suggestion eligibility scope disagrees")
        records = artifact.get("records")
        if not isinstance(records, list) or len(records) != len(available):
            raise InspectorError("suggestion eligibility records disagree")
        estimate = artifact.get("estimate", {})
        reading_wpm = float(estimate.get("readingWordsPerMinute", 0))
        mental_seconds = float(estimate.get("mentalEvaluationSeconds", 0))
        shortcut_count = int(estimate.get("shortcutKeystrokes", 0))
        seconds_per_key = float(estimate.get("secondsPerShortcutKeystroke", 0))
        response_seconds = float(estimate.get("systemResponseSeconds", 0))
        minimum_savings = float(estimate.get("minimumNetSavingsSeconds", 0))
        if reading_wpm <= 0 or shortcut_count < 0 or seconds_per_key < 0:
            raise InspectorError("suggestion eligibility estimate is invalid")
        parsed: dict[int, dict[str, Any]] = {}
        eligible: set[int] = set()
        for record in records:
            if not isinstance(record, dict):
                raise InspectorError("suggestion eligibility record is invalid")
            ordinal = record.get("oneBasedExampleOrdinal")
            if not isinstance(ordinal, int) or ordinal not in available or ordinal in parsed:
                raise InspectorError("suggestion eligibility ordinal is invalid")
            example = available[ordinal]
            if record.get("exampleID") != example.get("exampleID"):
                raise InspectorError("suggestion eligibility example ID disagrees")
            words = len(re.findall(r"\S+", target_text(example.get("target", {}))))
            if record.get("targetWordCount") != words:
                raise InspectorError("suggestion eligibility word count disagrees")
            human_seconds = float(record.get("humanInputDurationSeconds"))
            expected_seconds = (
                60 * words / reading_wpm
                + mental_seconds
                + shortcut_count * seconds_per_key
                + response_seconds
            )
            recorded_seconds = float(record.get("estimatedSuggestionInteractionSeconds"))
            recorded_savings = float(record.get("estimatedNetSavingsSeconds"))
            if abs(recorded_seconds - expected_seconds) > 0.0015:
                raise InspectorError("suggestion interaction estimate disagrees")
            if abs(recorded_savings - (human_seconds - expected_seconds)) > 0.0015:
                raise InspectorError("suggestion net savings disagrees")
            expected_eligible = human_seconds - expected_seconds > minimum_savings
            if record.get("scoreEligible") is not expected_eligible:
                raise InspectorError("suggestion eligibility decision disagrees")
            parsed[ordinal] = record
            if expected_eligible:
                eligible.add(ordinal)
        if set(parsed) != set(available):
            raise InspectorError("suggestion eligibility coverage disagrees")
        if scope.get("scoreEligibleExamples") != len(eligible):
            raise InspectorError("suggestion eligible count disagrees")
        if scope.get("excludedExamples") != len(available) - len(eligible):
            raise InspectorError("suggestion excluded count disagrees")
        self.score_eligible_ordinals = eligible
        self.suggestion_timing_by_ordinal = parsed
        self.minimum_net_savings_seconds = minimum_savings

    def _real_time_holistic_pass(
        self,
        one_based_ordinal: int,
        arm: str,
        score: dict[str, Any],
    ) -> bool:
        if one_based_ordinal not in self.holistic_pass_ordinals.get(arm, set()):
            return False
        timing = self.suggestion_timing_by_ordinal.get(one_based_ordinal)
        latency = score.get("generationLatencySeconds")
        if timing is None or latency is None:
            return False
        human_seconds = float(timing["humanInputDurationSeconds"])
        interaction_seconds = float(
            timing["estimatedSuggestionInteractionSeconds"]
        )
        return (
            human_seconds - interaction_seconds - float(latency)
            > self.minimum_net_savings_seconds
        )

    def _validate_holistic_review(self) -> None:
        review = self.holistic_review
        if review is None:
            return
        source = review.get("source", {})
        if source.get("corpusID") != self.corpus_manifest.get("corpusID"):
            raise InspectorError("holistic review corpus ID disagrees")
        if self.inkling_mode:
            actual_digest = sha256(self.paths.results / "scores.jsonl")
            if source.get("inklingScoresSHA256") != actual_digest:
                raise InspectorError("holistic review Inkling score digest disagrees")
        else:
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

        subset = review.get("subsets", {}).get("humanInputOver3Seconds", {})
        if not subset:
            return
        threshold = subset.get("thresholdSeconds")
        durations = subset.get("durationSecondsByOneBasedExampleOrdinal")
        ordinals = subset.get("oneBasedExampleOrdinals")
        if not isinstance(threshold, (int, float)) or threshold != 3:
            raise InspectorError("human-input subset must use a three-second threshold")
        if not isinstance(durations, dict) or not isinstance(ordinals, list):
            raise InspectorError("human-input subset has invalid duration evidence")
        parsed_durations: dict[int, float] = {}
        for ordinal_text, duration in durations.items():
            try:
                ordinal = int(ordinal_text)
            except (TypeError, ValueError) as error:
                raise InspectorError("human-input duration ordinal is invalid") from error
            if ordinal not in scoped_available or not isinstance(duration, (int, float)):
                raise InspectorError("human-input duration evidence is outside the scope")
            parsed_durations[ordinal] = float(duration)
        if set(parsed_durations) != scoped_available:
            raise InspectorError("human-input duration evidence does not cover the scope")
        expected_long = {
            ordinal for ordinal, duration in parsed_durations.items()
            if duration > float(threshold)
        }
        if set(ordinals) != expected_long or subset.get("examples") != len(expected_long):
            raise InspectorError("human-input subset membership disagrees with durations")
        model_passes = subset.get("modelPasses", {})
        expected_qwen = len(
            expected_long
            & self.holistic_pass_ordinals.get("personalized_qwen3.5_9b_base", set())
        )
        expected_frontier = len(
            expected_long
            & self.holistic_pass_ordinals.get("frozen_gpt_5.6_sol_xhigh", set())
        )
        if (
            model_passes.get("personalized_qwen3.5_9b_base") != expected_qwen
            or model_passes.get("frozen_gpt_5.6_sol_xhigh") != expected_frontier
        ):
            raise InspectorError("human-input subset holistic pass counts disagree")
        self.human_input_duration_by_ordinal = parsed_durations
        self.human_input_over_3_seconds_ordinals = expected_long
        self.human_input_over_3_seconds_subset = subset

    def _validate_supplemental_holistic_review(self) -> None:
        review = self.supplemental_holistic_review
        if review is None:
            return
        source = review.get("source", {})
        if source.get("corpusID") != self.corpus_manifest.get("corpusID"):
            raise InspectorError("supplemental holistic review corpus ID disagrees")
        actual_digest = prediction_triples_sha256(self.companion_comparisons)
        if source.get("predictionTriplesSHA256") != actual_digest:
            raise InspectorError("supplemental holistic review prediction digest disagrees")
        for source_key, path, label in (
            ("gpt54ScoresSHA256", self.paths.gpt54_results, "GPT-5.4"),
            ("gpt55ScoresSHA256", self.paths.gpt55_results, "GPT-5.5"),
            (
                "gpt56_128kScoresSHA256",
                self.paths.gpt56_128k_results,
                "GPT-5.6 128K",
            ),
            (
                "inklingNativeV5ScoresSHA256",
                self.paths.inkling_v5_results,
                "Inkling native-loss v5",
            ),
        ):
            expected = source.get(source_key)
            if expected is None:
                continue
            if path is None or sha256(path / "scores.jsonl") != expected:
                raise InspectorError(
                    f"supplemental holistic review {label} score digest disagrees"
                )
        expected_128k_semantic = source.get("gpt56_128kSemanticExamplesSHA256")
        if expected_128k_semantic is not None:
            if (
                self.paths.gpt56_128k_packed is None
                or sha256(
                    self.paths.gpt56_128k_packed / "semantic-examples.jsonl"
                )
                != expected_128k_semantic
            ):
                raise InspectorError(
                    "supplemental holistic review GPT-5.6 128K semantic-input digest disagrees"
                )
        available = {
            int(example["chronologicalOrdinal"]) + 1 for example in self.scored_examples
        }
        for model, value in review.get("models", {}).items():
            ordinals = value.get("passOneBasedExampleOrdinals")
            if not isinstance(ordinals, list) or not all(
                isinstance(ordinal, int) and ordinal in available
                for ordinal in ordinals
            ):
                raise InspectorError(
                    f"supplemental holistic pass ordinals are invalid: {model}"
                )
            if value.get("passCount") != len(ordinals):
                raise InspectorError(
                    f"supplemental holistic pass count disagrees: {model}"
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
        one_based_ordinal = int(example.get("chronologicalOrdinal")) + 1
        score_eligible = one_based_ordinal in self.score_eligible_ordinals
        suggestion_timing = self.suggestion_timing_by_ordinal.get(
            one_based_ordinal, {}
        )
        if self.inkling_mode:
            arms: dict[str, Any] = {}
            for arm in self.model_arms:
                score = comparison["arms"][arm]
                metrics = score.get("predictionMetrics", {})
                semantic_holistic = one_based_ordinal in self.holistic_pass_ordinals.get(
                    arm, set()
                )
                arms[arm] = {
                    "prediction": score.get("prediction", ""),
                    "exact": bool(metrics.get("exactMatch")),
                    "holistic": semantic_holistic,
                    "realTimeHolistic": self._real_time_holistic_pass(
                        one_based_ordinal, arm, score
                    ),
                    "similarity": metrics.get("normalizedLevenshteinSimilarity"),
                    "generationEligible": score.get(
                        "generationEligibleForEvaluation", False
                    ),
                    "generationDisposition": score.get("generationDisposition"),
                    "latencySeconds": score.get("latencySeconds"),
                    "generationLatencySeconds": score.get(
                        "generationLatencySeconds"
                    ),
                    "costUSD": score.get(
                        "estimatedProviderCostUSDAtFrozenRates"
                    ),
                }
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
                "scoreEligible": score_eligible,
                "humanInputDurationSeconds": suggestion_timing.get(
                    "humanInputDurationSeconds"
                ),
                "estimatedSuggestionInteractionSeconds": suggestion_timing.get(
                    "estimatedSuggestionInteractionSeconds"
                ),
                "estimatedNetSavingsSeconds": suggestion_timing.get(
                    "estimatedNetSavingsSeconds"
                ),
                "pasteActionCount": packed.get("pasteActionCount", 0),
                "arms": arms,
                "retainedEvents": len(
                    self.plan_by_id[example_id].get("retainedContextBlocks", [])
                ),
                "droppedEvents": packed.get("droppedContextEventCount", 0),
            }
        personalized = comparison.get("personalizedQwen", {})
        frozen = comparison.get("frozenQwen", {})
        frontier = comparison.get("frontier", {})
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
            "scoreEligible": score_eligible,
            "estimatedSuggestionInteractionSeconds": suggestion_timing.get(
                "estimatedSuggestionInteractionSeconds"
            ),
            "estimatedNetSavingsSeconds": suggestion_timing.get(
                "estimatedNetSavingsSeconds"
            ),
            "pasteActionCount": packed.get("pasteActionCount", 0),
            "personalizedExact": personalized.get("prediction") == target,
            "frontierExact": frontier.get("prediction") == target,
            "frozenExact": frozen.get("prediction") == target,
            "personalizedHolistic": score_eligible and one_based_ordinal
            in self.holistic_pass_ordinals.get(
                "personalized_qwen3.5_9b_base", set()
            ),
            "frontierHolistic": score_eligible and one_based_ordinal
            in self.holistic_pass_ordinals.get(
                "frozen_gpt_5.6_sol_xhigh", set()
            ),
            "humanInputDurationSeconds": suggestion_timing.get(
                "humanInputDurationSeconds",
                self.human_input_duration_by_ordinal.get(one_based_ordinal),
            ),
            "humanInputOver3Seconds": one_based_ordinal
            in self.human_input_over_3_seconds_ordinals,
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
        if self.inkling_mode:
            review = self.holistic_review or {}
            review_models = review.get("models", {})
            training_cost_by_condition: dict[str, float] = {}
            for update in self.updates:
                condition = str(update.get("condition", ""))
                training_cost_by_condition[condition] = (
                    training_cost_by_condition.get(condition, 0.0)
                    + float(update.get("estimatedTrainingCostUSDAtFrozenRate", 0.0))
                )

            def median(values: list[float]) -> float | None:
                if not values:
                    return None
                ordered = sorted(values)
                middle = len(ordered) // 2
                if len(ordered) % 2:
                    return ordered[middle]
                return (ordered[middle - 1] + ordered[middle]) / 2

            arm_stats: dict[str, Any] = {}
            score_scope = len(self.score_eligible_ordinals)
            qwen_training_cost = 0.0
            if self.comparison_experiment is not None:
                qwen_training_cost = float(
                    self.comparison_experiment.get("providerUsage", {})
                    .get("tinkerEstimatedCost", {})
                    .get("trainingAtFrozenRate", 0.0)
                )
            for arm in self.model_arms:
                rows = [comparison["arms"][arm] for comparison in self.comparisons]
                generation_latencies = [
                    float(score["generationLatencySeconds"])
                    for score in rows
                    if score.get("generationLatencySeconds") is not None
                ]
                combined_latencies = [
                    float(score["latencySeconds"])
                    for score in rows
                    if score.get("latencySeconds") is not None
                ]
                evaluation_cost_values = [
                    float(score["estimatedProviderCostUSDAtFrozenRates"])
                    for score in rows
                    if score.get("estimatedProviderCostUSDAtFrozenRates") is not None
                ]
                evaluation_cost = (
                    sum(evaluation_cost_values)
                    if len(evaluation_cost_values) == len(rows)
                    else None
                )
                condition = "reasoning_on" if arm.endswith("reasoning_on") else "reasoning_off"
                if arm.startswith("personalized_inkling"):
                    if arm == "personalized_inkling_small_reasoning_off_native_v5":
                        training_cost = sum(
                            float(update.get("estimatedTrainingCostUSDAtFrozenRate", 0.0))
                            for update in self.inkling_v5_updates
                        )
                    else:
                        training_cost = training_cost_by_condition.get(condition, 0.0)
                elif arm == "personalized_qwen3.5_9b_base":
                    training_cost = qwen_training_cost
                else:
                    training_cost = 0.0
                holistic_passes = (
                    len(self.holistic_pass_ordinals[arm])
                    if arm in self.holistic_pass_ordinals
                    else None
                )
                real_time_holistic_passes = (
                    sum(
                        1
                        for summary in self.summaries
                        if summary.get("arms", {})
                        .get(arm, {})
                        .get("realTimeHolistic")
                    )
                    if arm in self.holistic_pass_ordinals
                    else None
                )
                arm_stats[arm] = {
                    "label": self.model_labels[arm],
                    "examples": len(rows),
                    "scoreEligibleExamples": score_scope,
                    "exactMatches": sum(
                        1 for score in rows
                        if score.get("predictionMetrics", {}).get("exactMatch")
                    ),
                    "macroNormalizedLevenshteinSimilarity": sum(
                        float(
                            score.get("predictionMetrics", {}).get(
                                "normalizedLevenshteinSimilarity", 0.0
                            )
                        )
                        for score in rows
                    )
                    / len(rows),
                    "holisticPasses": holistic_passes,
                    "realTimeHolisticPasses": real_time_holistic_passes,
                    "invalidOrTruncated": sum(
                        1 for score in rows
                        if not score.get("generationEligibleForEvaluation", False)
                    ),
                    "generationLatencyMeanSeconds": (
                        sum(generation_latencies) / len(generation_latencies)
                        if generation_latencies
                        else None
                    ),
                    "generationLatencyMedianSeconds": median(generation_latencies),
                    "combinedLatencyMeanSeconds": (
                        sum(combined_latencies) / len(combined_latencies)
                        if combined_latencies
                        else None
                    ),
                    "combinedLatencyMedianSeconds": median(combined_latencies),
                    "evaluationCostUSD": evaluation_cost,
                    "trainingCostUSD": training_cost,
                    "totalAttributedCostUSD": (
                        evaluation_cost + training_cost
                        if evaluation_cost is not None
                        else None
                    ),
                }
            return {
                "mode": "inkling",
                "examples": len(self.scored_examples),
                "applications": sorted(
                    {
                        value.get("application")
                        for value in self.summaries
                        if value.get("application")
                    }
                ),
                "blocks": sorted(
                    {
                        value.get("blockID")
                        for value in self.summaries
                        if value.get("blockID")
                    }
                ),
                "status": self.experiment.get("status"),
                "corpusID": self.corpus_manifest.get("corpusID"),
                "packerVersion": self.packing_manifest.get("packerVersion"),
                "arms": arm_stats,
                "holisticReview": {
                    "reviewID": review.get("reviewID"),
                    "status": review.get("status"),
                    "passBar": review.get("passBar"),
                    "scope": review.get("scope"),
                    "uncertaintyExamplesPerModel": review.get(
                        "uncertaintyExamplesPerModel"
                    ),
                    "scoringEligibility": {
                        "policyID": (self.suggestion_eligibility or {}).get(
                            "policyID"
                        ),
                        "examples": score_scope,
                        "excludedExamples": len(self.scored_examples) - score_scope,
                        "purpose": (self.suggestion_eligibility or {}).get(
                            "purpose"
                        ),
                        "estimate": (self.suggestion_eligibility or {}).get(
                            "estimate", {}
                        ),
                    },
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
                    "suggestionEligibility": (
                        str(self.paths.suggestion_eligibility)
                        if self.paths.suggestion_eligibility is not None
                        else None
                    ),
                    "comparisonResults": (
                        str(self.paths.comparison_results)
                        if self.paths.comparison_results is not None
                        else None
                    ),
                    "gpt54Results": (
                        str(self.paths.gpt54_results)
                        if self.paths.gpt54_results is not None
                        else None
                    ),
                    "gpt55Results": (
                        str(self.paths.gpt55_results)
                        if self.paths.gpt55_results is not None
                        else None
                    ),
                    "gpt56_128kResults": (
                        str(self.paths.gpt56_128k_results)
                        if self.paths.gpt56_128k_results is not None
                        else None
                    ),
                    "gpt56_128kPacked": (
                        str(self.paths.gpt56_128k_packed)
                        if self.paths.gpt56_128k_packed is not None
                        else None
                    ),
                    "inklingV5Results": (
                        str(self.paths.inkling_v5_results)
                        if self.paths.inkling_v5_results is not None
                        else None
                    ),
                },
            }
        summaries = self.experiment.get("summaries", {})
        review = self.holistic_review or {}
        review_models = review.get("models", {})
        duration_subset = self.human_input_over_3_seconds_subset
        duration_model_passes = duration_subset.get("modelPasses", {})
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
                ).get("passCount") if not self.suggestion_eligibility else len(
                    self.holistic_pass_ordinals.get(
                        "personalized_qwen3.5_9b_base", set()
                    ) & self.score_eligible_ordinals
                ),
                "frontierPasses": review_models.get(
                    "frozen_gpt_5.6_sol_xhigh", {}
                ).get("passCount") if not self.suggestion_eligibility else len(
                    self.holistic_pass_ordinals.get(
                        "frozen_gpt_5.6_sol_xhigh", set()
                    ) & self.score_eligible_ordinals
                ),
                "scoringEligibility": {
                    "policyID": (self.suggestion_eligibility or {}).get(
                        "policyID"
                    ),
                    "examples": len(self.score_eligible_ordinals),
                    "excludedExamples": len(self.scored_examples)
                    - len(self.score_eligible_ordinals),
                    "purpose": (self.suggestion_eligibility or {}).get(
                        "purpose"
                    ),
                    "estimate": (self.suggestion_eligibility or {}).get(
                        "estimate", {}
                    ),
                },
                "humanInputOver3Seconds": {
                    "thresholdSeconds": duration_subset.get("thresholdSeconds"),
                    "measurement": duration_subset.get("measurement"),
                    "examples": duration_subset.get("examples"),
                    "personalizedPasses": duration_model_passes.get(
                        "personalized_qwen3.5_9b_base"
                    ),
                    "frontierPasses": duration_model_passes.get(
                        "frozen_gpt_5.6_sol_xhigh"
                    ),
                },
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
                "suggestionEligibility": (
                    str(self.paths.suggestion_eligibility)
                    if self.paths.suggestion_eligibility is not None
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
        alternate_context_inputs: dict[str, Any] = {}
        gpt56_128k_semantic = self.gpt56_128k_semantic_by_id.get(example_id)
        if gpt56_128k_semantic is not None:
            alternate_context_inputs["frozen_gpt_5.6_sol_xhigh_128k"] = {
                "label": "GPT-5.6-sol xhigh · 128K",
                "inputTokenBudget": gpt56_128k_semantic.get("inputTokenBudget"),
                "canonicalPackingTokenCount": gpt56_128k_semantic.get(
                    "canonicalPackingTokenCount"
                ),
                "retainedContextBlockCount": len(
                    gpt56_128k_semantic.get("retainedContextBlocks", [])
                ),
                "semanticModelInputSHA256": gpt56_128k_semantic.get(
                    "semanticModelInputSHA256"
                ),
            }
        return {
            "summary": self._summary(example),
            "holisticReview": {
                "reviewID": (self.holistic_review or {}).get("reviewID"),
                "passBar": (self.holistic_review or {}).get("passBar"),
                "uncertaintyExamplesPerModel": (
                    self.holistic_review or {}
                ).get("uncertaintyExamplesPerModel"),
                "humanInputOver3Seconds": self.human_input_over_3_seconds_subset,
                "scoringEligibility": {
                    "policyID": (self.suggestion_eligibility or {}).get(
                        "policyID"
                    ),
                    "purpose": (self.suggestion_eligibility or {}).get(
                        "purpose"
                    ),
                    "estimate": (self.suggestion_eligibility or {}).get(
                        "estimate", {}
                    ),
                },
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
            "alternateContextInputs": alternate_context_inputs,
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
    .badge.inkling-off { border-color: #24576b; color: #73d7ff; background: #0e2530; }
    .badge.inkling-on { border-color: #725631; color: #ffc979; background: #2b2112; }
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
    .prediction-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    .summary-table-wrap { overflow: auto; }
    .summary-table { width: 100%; border-collapse: collapse; font: 11px/1.4 var(--mono); }
    .summary-table th, .summary-table td { padding: 10px 9px; text-align: right; border-bottom: 1px solid var(--line); white-space: nowrap; }
    .summary-table th:first-child, .summary-table td:first-child { text-align: left; position: sticky; left: 0; background: var(--panel); }
    .summary-table th { color: var(--muted); font-weight: 600; }
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
          <select id="duration">
            <option value="">All human input durations</option>
            <option value="over-3">Human input &gt;3s</option>
            <option value="at-most-3">Human input ≤3s</option>
          </select>
        </div>
        <div class="filter-row">
          <select id="outcome">
            <option value="">All outcomes</option>
            <option value="personalized-exact">Properly trained Qwen exact</option>
            <option value="frontier-exact">GPT exact</option>
            <option value="personalized-holistic">Qwen holistic pass</option>
            <option value="frontier-holistic">GPT holistic pass</option>
            <option value="either-holistic">Either holistic pass</option>
          </select>
          <select id="sort">
            <option value="chronology">Chronological</option>
            <option value="length-desc">Target length ↓</option>
            <option value="similarity-desc">Best similarity ↓</option>
          </select>
        </div>
        <button id="random">Random example</button>
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
          <button class="tab active" data-tab="summary">Model summary</button>
          <button class="tab" data-tab="stream">Causal stream</button>
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
    const state = { meta: null, examples: [], filtered: [], selected: null, detail: null, tab: 'summary' };
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
      if (m.mode === 'inkling') {
        const gate = m.holisticReview?.scoringEligibility || {};
        const scope = gate.examples ?? m.examples;
        const armEntries = Object.entries(m.arms || {});
        $('stats').innerHTML = `
          <div class="stat"><strong>${m.examples}</strong>examples</div>
          <div class="stat"><strong>${scope}</strong>scoreable suggestions</div>
          <div class="stat"><strong>${gate.excludedExamples ?? 0}</strong>too fast to score</div>
          <div class="stat"><strong>${armEntries.length}</strong>model arms</div>
          <div class="stat"><strong>${m.status}</strong>run status</div>`;
        $('paths').textContent = m.paths.results;
        $('paths').title = `Corpus: ${m.paths.corpus}\nSemantic context plan: ${m.paths.packed}\nResults: ${m.paths.results}\nWanted-suggestion review: ${m.paths.holisticReview || 'none'}\nSuggestion eligibility: ${m.paths.suggestionEligibility || 'none'}\n\nReviewer bar: ${m.holisticReview?.passBar || 'none'}`;
        fillSelect('block', m.blocks);
        fillSelect('app', m.applications);
        $('duration').innerHTML = '<option value="">All examples</option><option value="score-eligible">Score-eligible opportunities</option><option value="score-excluded">Too fast to score</option>';
        $('duration').disabled = false;
        $('outcome').innerHTML = '<option value="">All outcomes</option>'
          + armEntries.filter(([,value]) => value.realTimeHolisticPasses != null).map(([arm,value]) => `<option value="arm-realtime-holistic:${esc(arm)}">${esc(value.label)} real-time holy-shit pass</option>`).join('')
          + '<option value="any-realtime-holistic">Any real-time holy-shit pass</option>'
          + armEntries.filter(([,value]) => value.holisticPasses != null).map(([arm,value]) => `<option value="arm-holistic:${esc(arm)}">${esc(value.label)} semantic holy-shit pass (ignore speed)</option>`).join('')
          + '<option value="any-holistic">Any semantic holy-shit pass (ignore speed)</option>'
          + armEntries.map(([arm,value]) => `<option value="arm-exact:${esc(arm)}">${esc(value.label)} exact</option>`).join('');
        return;
      }
      const personalized = m.summaries['personalized_qwen3.5_9b_base'] || {};
      const frontier = m.summaries['frozen_gpt_5.6_sol_xhigh'] || {};
      const holistic = m.holisticReview || {};
      const holisticScope = holistic.scoringEligibility?.examples ?? holistic.scope?.examples ?? 150;
      const longInput = holistic.humanInputOver3Seconds || {};
      const holisticScore = passes => `${passes}/${holisticScope} · ${((passes / holisticScope) * 100).toFixed(1)}%`;
      const subsetScore = passes => `${passes}/${longInput.examples} · ${((passes / longInput.examples) * 100).toFixed(1)}%`;
      $('stats').innerHTML = `
        <div class="stat"><strong>${m.examples}</strong>examples</div>
        <div class="stat"><strong>${fmt(personalized.microTargetTokenNLL)}</strong>trained Qwen NLL</div>
        <div class="stat"><strong>${personalized.generatedCompletion?.exactMatches ?? personalized.exactMatches ?? 0}</strong>Qwen exact</div>
        <div class="stat"><strong>${frontier.generatedCompletion?.exactMatches ?? frontier.exactMatches ?? 0}</strong>GPT exact</div>
        ${holistic.personalizedPasses == null ? '' : `<div class="stat" title="Binary wanted-suggestion pass among score-eligible opportunities"><strong>${holisticScore(holistic.personalizedPasses)}</strong>Q wanted</div>`}
        ${holistic.frontierPasses == null ? '' : `<div class="stat" title="Binary wanted-suggestion pass among score-eligible opportunities"><strong>${holisticScore(holistic.frontierPasses)}</strong>GPT wanted</div>`}
        ${longInput.personalizedPasses == null ? '' : `<div class="stat" title="Strict semantic score among targets whose raw human input span exceeded ${longInput.thresholdSeconds}s"><strong>${subsetScore(longInput.personalizedPasses)}</strong>Q &gt;3s holistic</div>`}
        ${longInput.frontierPasses == null ? '' : `<div class="stat" title="Strict semantic score among targets whose raw human input span exceeded ${longInput.thresholdSeconds}s"><strong>${subsetScore(longInput.frontierPasses)}</strong>GPT &gt;3s holistic</div>`}`;
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
      const duration = $('duration').value;
      const outcome = $('outcome').value;
      state.filtered = state.examples.filter(example => {
        if (block && example.blockID !== block) return false;
        if (app && example.application !== app) return false;
        if (type && example.targetType !== type) return false;
        if (duration === 'over-3' && !example.humanInputOver3Seconds) return false;
        if (duration === 'at-most-3' && example.humanInputOver3Seconds) return false;
        if (duration === 'score-eligible' && !example.scoreEligible) return false;
        if (duration === 'score-excluded' && example.scoreEligible) return false;
        if (query && !`${example.target} ${example.application} ${example.window} ${example.exampleID}`.toLowerCase().includes(query)) return false;
        if (outcome === 'personalized-exact' && !example.personalizedExact) return false;
        if (outcome === 'frontier-exact' && !example.frontierExact) return false;
        if (outcome === 'personalized-holistic' && !example.personalizedHolistic) return false;
        if (outcome === 'frontier-holistic' && !example.frontierHolistic) return false;
        if (outcome === 'either-holistic' && !(example.personalizedHolistic || example.frontierHolistic)) return false;
        if (outcome.startsWith('arm-realtime-holistic:') && !example.arms?.[outcome.slice(22)]?.realTimeHolistic) return false;
        if (outcome.startsWith('arm-holistic:') && !example.arms?.[outcome.slice(13)]?.holistic) return false;
        if (outcome.startsWith('arm-exact:') && !example.arms?.[outcome.slice(10)]?.exact) return false;
        if (outcome === 'any-realtime-holistic' && !Object.values(example.arms || {}).some(value => value.realTimeHolistic)) return false;
        if (outcome === 'any-holistic' && !Object.values(example.arms || {}).some(value => value.holistic)) return false;
        return true;
      });
      const sort = $('sort').value;
      state.filtered.sort((a,b) => {
        if (sort === 'length-desc') return b.targetLength - a.targetLength;
        if (sort === 'similarity-desc') {
          const best = value => value.arms ? Math.max(...Object.values(value.arms).map(arm => arm.similarity ?? -1)) : (value.personalizedSimilarity ?? -1);
          return best(b) - best(a);
        }
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
            ${state.meta.mode === 'inkling' ? Object.entries(example.arms || {}).filter(([,value]) => value.realTimeHolistic).map(([arm]) => `<span class="badge good" title="Semantic pass delivered fast enough to be useful">${esc(state.meta.arms?.[arm]?.label || arm)} · real-time holy-shit</span>`).join('') : ''}
            ${state.meta.mode === 'inkling' ? Object.entries(example.arms || {}).filter(([,value]) => value.holistic && !value.realTimeHolistic).map(([arm]) => `<span class="badge ${arm.endsWith('reasoning_on') ? 'inkling-on' : 'inkling-off'}" title="Semantic pass ignoring generation speed">${esc(state.meta.arms?.[arm]?.label || arm)} · semantic-only</span>`).join('') : ''}
            ${example.personalizedExact ? '<span class="badge good">Q exact</span>' : ''}
            ${example.frontierExact ? '<span class="badge good">GPT exact</span>' : ''}
            ${example.personalizedHolistic ? '<span class="badge q-holistic" title="Subjective strict reviewer pass">Q holistic</span>' : ''}
            ${example.frontierHolistic ? '<span class="badge gpt-holistic" title="Subjective strict reviewer pass">GPT holistic</span>' : ''}
            ${example.scoreEligible ? '' : '<span class="badge bad" title="Estimated suggestion interaction would not save at least one second">too fast to score</span>'}
            ${example.humanInputDurationSeconds == null ? '' : `<span class="badge" title="Raw elapsed span from first to last mutation-capable human input across the closed episode">${example.humanInputDurationSeconds.toFixed(3)}s human</span>`}
            <span class="spacer"></span>
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
      const inklingBadges = state.meta.mode === 'inkling'
        ? Object.entries(s.arms || {}).filter(([,value]) => value.holistic).map(([arm,value]) => `<span class="badge ${value.realTimeHolistic ? 'good' : (arm.endsWith('reasoning_on') ? 'inkling-on' : 'inkling-off')}">${esc(state.meta.arms?.[arm]?.label || arm)} · ${value.realTimeHolistic ? 'real-time holy-shit' : 'semantic-only holy-shit'}</span>`).join('')
        : '';
      $('detail-subline').innerHTML = `
        <span>#${s.ordinal + 1}</span><span>${esc(s.blockID)}</span><span>${esc(s.application)}</span>
        <span>${esc(s.targetType)}</span><span>${s.targetLength} chars</span>
        ${inklingBadges}
        ${s.personalizedHolistic ? '<span class="badge q-holistic">Q wanted-suggestion pass</span>' : ''}
        ${s.frontierHolistic ? '<span class="badge gpt-holistic">GPT wanted-suggestion pass</span>' : ''}
        ${s.scoreEligible ? '<span class="badge good">score eligible</span>' : '<span class="badge bad">too fast to score</span>'}
        ${s.humanInputDurationSeconds == null ? '' : `<span class="badge" title="Raw elapsed span from first to last mutation-capable human input across the closed episode">${s.humanInputDurationSeconds.toFixed(3)}s human input${s.estimatedSuggestionInteractionSeconds == null ? '' : ` · ${s.estimatedSuggestionInteractionSeconds.toFixed(3)}s estimated suggestion · ${s.estimatedNetSavingsSeconds.toFixed(3)}s net`}</span>`}
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
      const alternateContexts = Object.values(d.alternateContextInputs || {});
      return `
        <div class="toolbar">
          <input id="history-search" type="search" placeholder="Filter retained event text…">
          <button id="expand-events">Expand all</button><button id="collapse-events">Collapse all</button>
          <span class="spacer"></span><span class="badge">model-visible</span>
        </div>
        ${alternateContexts.length ? `<div class="card"><div class="card-head"><strong>CONTEXT DISPLAY NOTE</strong></div><div class="card-body"><pre>This tab displays the canonical 32K causal stream. ${alternateContexts.map(value => `${value.label} used its separately validated packed context (${value.retainedContextBlockCount} retained blocks; budget ${value.inputTokenBudget} tokens).`).join('\n')}</pre></div></div>` : ''}
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

    function predictionCard(name, row, nllAvailable, holisticPass, realTimeHolisticPass, holisticClass) {
      const exact = row.prediction === state.detail.targetText;
      const metrics = row.predictionMetrics || {};
      const similarity = metrics.normalizedLevenshteinSimilarity ?? row.characterSimilarity;
      const eligible = row.generationEligibleForEvaluation;
      const disposition = row.generationDisposition;
      return `<div class="card prediction ${exact ? 'exact' : ''}">
        <div class="card-head"><strong>${esc(name)}</strong><span class="spacer"></span>${row.inputContextLabel ? `<span class="badge" title="This arm was validated against a distinct packed semantic input">${esc(row.inputContextLabel)}</span>` : ''}${eligible === false ? `<span class="badge bad">${esc(disposition || 'invalid')}</span>` : ''}${realTimeHolisticPass ? '<span class="badge good" title="Semantic pass delivered fast enough to beat the human write">real-time holy-shit</span>' : (holisticPass ? `<span class="badge ${holisticClass}" title="Semantic pass when generation speed is ignored">semantic-only holy-shit</span>` : '')}${exact ? '<span class="badge good">exact</span>' : ''}</div>
        <div class="card-body"><pre>${esc(targetPreview(row.prediction))}</pre></div>
        <div class="metrics"><span>edit similarity ${pct(similarity)}</span><span>prefix ${metrics.correctPrefixCharacters ?? '—'} chars</span>${nllAvailable ? `<span>NLL ${fmt(row.meanNLL)}</span>` : ''}${row.generationLatencySeconds == null ? '' : `<span>generation ${Number(row.generationLatencySeconds).toFixed(3)}s</span>`}${row.latencySeconds == null ? '' : `<span>combined ${Number(row.latencySeconds).toFixed(3)}s</span>`}${row.estimatedProviderCostUSDAtFrozenRates == null ? '' : `<span>eval $${Number(row.estimatedProviderCostUSDAtFrozenRates).toFixed(4)}</span>`}</div>
      </div>`;
    }

    function renderModelSummary() {
      if (state.meta.mode !== 'inkling') return renderStream();
      const scope = state.meta.examples;
      const rows = Object.values(state.meta.arms || {}).map(arm => {
        const scoreScope = arm.scoreEligibleExamples ?? scope;
        const realTime = arm.realTimeHolisticPasses == null
          ? 'not judged'
          : `${arm.realTimeHolisticPasses}/${scoreScope} (${((arm.realTimeHolisticPasses / scoreScope) * 100).toFixed(1)}%)`;
        const semantic = arm.holisticPasses == null
          ? 'not judged'
          : `${arm.holisticPasses}/${scoreScope} (${((arm.holisticPasses / scoreScope) * 100).toFixed(1)}%)`;
        const generation = arm.generationLatencyMeanSeconds == null
          ? '—'
          : `${arm.generationLatencyMeanSeconds.toFixed(2)} / ${arm.generationLatencyMedianSeconds.toFixed(2)}s`;
        const combined = arm.combinedLatencyMeanSeconds == null
          ? '—'
          : `${arm.combinedLatencyMeanSeconds.toFixed(2)} / ${arm.combinedLatencyMedianSeconds.toFixed(2)}s`;
        return `<tr>
          <td><strong>${esc(arm.label)}</strong>${arm.invalidOrTruncated ? `<br><span class="badge bad">${arm.invalidOrTruncated} invalid/truncated</span>` : ''}</td>
          <td>${(arm.macroNormalizedLevenshteinSimilarity * 100).toFixed(1)}%</td>
          <td>${arm.exactMatches}/${state.meta.examples}</td>
          <td><strong>${realTime}</strong></td>
          <td>${semantic}</td>
          <td>${generation}</td>
          <td>${combined}</td>
          <td>${arm.evaluationCostUSD == null ? '— subscription' : `$${arm.evaluationCostUSD.toFixed(2)}`}</td>
          <td>${arm.trainingCostUSD ? `$${arm.trainingCostUSD.toFixed(2)}` : '—'}</td>
          <td><strong>${arm.totalAttributedCostUSD == null ? '—' : `$${arm.totalAttributedCostUSD.toFixed(2)}`}</strong></td>
        </tr>`;
      }).join('');
      return `<div class="card">
        <div class="card-head"><strong>AGGREGATE MODEL COMPARISON</strong><span class="spacer"></span><span>${state.meta.examples} captured opportunities</span></div>
        <div class="summary-table-wrap"><table class="summary-table">
          <thead><tr><th>Model arm</th><th>Macro edit accuracy</th><th>Exact</th><th>Real-time holy-shit</th><th>Semantic holy-shit<br>(ignore speed)</th><th>Generation mean / median</th><th>Combined mean / median</th><th>Evaluation cost</th><th>Training cost</th><th>Attributed total</th></tr></thead>
          <tbody>${rows}</tbody>
        </table></div>
      </div>
      <div class="card"><div class="card-head"><strong>METRIC CONTRACT</strong></div><div class="card-body"><pre>Macro edit accuracy = mean normalized Levenshtein similarity across all ${state.meta.examples} captured examples.
Exact = byte-identical generated completion.
Real-time holy-shit = semantic manual pass and generation + estimated review/acceptance time beats the recorded human write by more than one second.
Semantic holy-shit = the same manual quality bar while ignoring model latency. Both rates use the model-independent ${state.meta.holisticReview?.scoringEligibility?.examples ?? scope}-opportunity utility gate; all ${scope} examples remain visible for inspection.
Generation latency is the user-facing generation request. Combined latency additionally includes target-likelihood scoring where that arm performed it.
Costs are API/provider-equivalent estimates at frozen rates when a frozen rate exists. GPT subscription runs have no separately attributable marginal invoice. GPT-5.4 uses $2.50/M uncached input and $15/M output; no prompt exceeded its long-context surcharge threshold. GPT-5.5 is shown as subscription-only because this run recorded no defensible API-equivalent rate.</pre></div></div>`;
    }

    function renderPredictions() {
      const c = state.detail.comparison;
      const s = state.detail.summary;
      const review = state.detail.holisticReview || {};
      const longInput = review.humanInputOver3Seconds || {};
      const scoringGate = review.scoringEligibility || {};
      if (state.meta.mode === 'inkling') {
        const cards = Object.entries(state.meta.arms).map(([arm,meta]) => {
          const armSummary = s.arms?.[arm] || {};
          return predictionCard(meta.label, c.arms?.[arm] || {}, true, armSummary.holistic, armSummary.realTimeHolistic, arm.endsWith('reasoning_on') ? 'inkling-on' : 'inkling-off');
        }).join('');
        return `<div class="card target-card"><div class="card-head"><strong>HUMAN TARGET</strong></div><div class="card-body"><pre>${esc(state.detail.targetText)}</pre></div></div>
          <div class="prediction-grid">${cards}</div>
          ${review.passBar ? `<div class="card"><div class="card-head"><strong>HOLY-SHIT BAR</strong><span class="spacer"></span><span>binary · semantic and real-time variants</span></div><div class="card-body"><pre>${esc(review.passBar)}${scoringGate.estimate?.formula ? `\n\nReal-time gate: generation latency + ${esc(scoringGate.estimate.formula)}` : ''}</pre></div></div>` : ''}`;
      }
      return `<div class="card target-card"><div class="card-head"><strong>HUMAN TARGET</strong></div><div class="card-body"><pre>${esc(state.detail.targetText)}</pre></div></div>
        <div class="prediction-grid">
          ${predictionCard('Properly trained Qwen3.5-9B', c.personalizedQwen || {}, true, s.personalizedHolistic, false, 'q-holistic')}
          ${predictionCard('GPT-5.6-sol xhigh', c.frontier || {}, false, s.frontierHolistic, false, 'gpt-holistic')}
        </div>
        ${review.passBar ? `<div class="card"><div class="card-head"><strong>WANTED-SUGGESTION BAR</strong><span class="spacer"></span><span>binary · score-eligible opportunities only</span></div><div class="card-body"><pre>${esc(review.passBar)}${scoringGate.estimate?.formula ? `\n\nEligibility: ${esc(scoringGate.estimate.formula)}` : ''}${longInput.measurement ? `\n\n&gt;3s subset: ${esc(longInput.measurement)}` : ''}</pre></div></div>` : ''}`;
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
      const comparison = {...state.detail.comparison};
      delete comparison.frozenQwen;
      delete comparison.personalizedBitsSavedVersusFrozen;
      return `${jsonCard('Compiled example', state.detail.rawExample)}${jsonCard('Comparison result', comparison)}${jsonCard('Target event projection', state.detail.targetEvent)}${jsonCard('Context plan', state.detail.contextPlan)}`;
    }

    function renderTab() {
      document.querySelectorAll('.tab').forEach(tab => tab.classList.toggle('active', tab.dataset.tab === state.tab));
      const renderers = {summary:renderModelSummary,stream:renderStream,predictions:renderPredictions,conditioning:renderConditioning,packing:renderPacking,raw:renderRaw};
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
      if (state.meta.mode !== 'inkling') state.tab = 'stream';
      renderMeta();
      applyFilters();
      const hash = decodeURIComponent(location.hash.slice(1));
      const initial = state.examples.some(value => value.exampleID === hash) ? hash : state.examples[0]?.exampleID;
      if (initial) selectExample(initial);
    }

    ['search','block','app','type','duration','outcome','sort'].forEach(id => $(id).addEventListener(id === 'search' ? 'input' : 'change', applyFilters));
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
    parser.add_argument(
        "--comparison-results",
        type=Path,
        help="optional Qwen/GPT-5.6 comparison artifact to add to Inkling mode",
    )
    parser.add_argument(
        "--qwen-scores",
        type=Path,
        help="optional Tinker score directory supplying Qwen generation latency",
    )
    parser.add_argument(
        "--gpt54-results",
        type=Path,
        help="optional completed GPT-5.4 score directory to add to Inkling mode",
    )
    parser.add_argument(
        "--gpt55-results",
        type=Path,
        help="optional completed GPT-5.5 score directory to add to Inkling mode",
    )
    parser.add_argument(
        "--gpt56-128k-results",
        type=Path,
        help="optional completed GPT-5.6-sol 128K score directory",
    )
    parser.add_argument(
        "--gpt56-128k-packed",
        type=Path,
        help="packed 128K semantic inputs corresponding to --gpt56-128k-results",
    )
    parser.add_argument(
        "--inkling-v5-results",
        type=Path,
        help="optional completed native-loss Inkling v5 score directory",
    )
    parser.add_argument(
        "--comparison-holistic-review",
        type=Path,
        help="optional Qwen/GPT-5.6 subjective holistic-review labels",
    )
    parser.add_argument(
        "--suggestion-eligibility",
        type=Path,
        help="optional shared timing-based eligibility artifact for suggestion scoring",
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
        arguments.comparison_results,
        arguments.qwen_scores,
        arguments.gpt54_results,
        arguments.gpt55_results,
        arguments.gpt56_128k_results,
        arguments.gpt56_128k_packed,
        arguments.inkling_v5_results,
        arguments.comparison_holistic_review,
        arguments.suggestion_eligibility,
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
        if paths.suggestion_eligibility is not None:
            print(f"Gate:    {paths.suggestion_eligibility}")
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

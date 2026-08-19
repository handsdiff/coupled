#!/usr/bin/env python3
"""Deterministic generated-completion metrics for Phase 1.

"Character" means a Unicode code point in this first metric contract. This is
dependency-free and deterministic across the collector's supported machines.
The contract deliberately does not normalize Unicode or internal whitespace.
"""

from __future__ import annotations

from typing import Any


METRIC_CONTRACT_VERSION = "phase1-generated-completion-metrics-v1"
PASTE_MARKER = "<|paste|>"


def levenshtein_distance(left: str, right: str) -> int:
    """Return Unicode-code-point Levenshtein distance using bounded memory."""

    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)

    # Remove equal ends before the dynamic program. Generated completions often
    # share a prefix with the target, so this materially reduces audit work.
    prefix = 0
    limit = min(len(left), len(right))
    while prefix < limit and left[prefix] == right[prefix]:
        prefix += 1
    left = left[prefix:]
    right = right[prefix:]
    suffix = 0
    limit = min(len(left), len(right))
    while suffix < limit and left[-(suffix + 1)] == right[-(suffix + 1)]:
        suffix += 1
    if suffix:
        left = left[:-suffix]
        right = right[:-suffix]
    if not left:
        return len(right)
    if not right:
        return len(left)

    if len(left) > len(right):
        left, right = right, left
    previous = list(range(len(left) + 1))
    for row_index, right_character in enumerate(right, start=1):
        current = [row_index]
        for column_index, left_character in enumerate(left, start=1):
            current.append(min(
                current[-1] + 1,
                previous[column_index] + 1,
                previous[column_index - 1] + (left_character != right_character),
            ))
        previous = current
    return previous[-1]


def longest_common_prefix_length(left: str, right: str) -> int:
    length = 0
    for left_character, right_character in zip(left, right):
        if left_character != right_character:
            break
        length += 1
    return length


def score_prediction(
    target: str,
    prediction: str,
    *,
    target_paste_actions: int,
) -> dict[str, Any]:
    target_length = len(target)
    prediction_length = len(prediction)
    denominator = max(target_length, prediction_length)
    distance = levenshtein_distance(target, prediction)
    correct_prefix = longest_common_prefix_length(target, prediction)
    predicted_paste_actions = prediction.count(PASTE_MARKER)
    matched_paste_actions = min(target_paste_actions, predicted_paste_actions)
    return {
        "exactMatch": prediction == target,
        "surroundingWhitespaceNormalizedExactMatch": prediction.strip() == target.strip(),
        "targetCharacters": target_length,
        "predictionCharacters": prediction_length,
        "predictionEmpty": prediction_length == 0,
        "levenshteinDistance": distance,
        "normalizedLevenshteinSimilarity": (
            1.0 if denominator == 0 else 1.0 - (distance / denominator)
        ),
        "correctPrefixCharacters": correct_prefix,
        "correctPrefixTargetCoverage": (
            correct_prefix / target_length if target_length else None
        ),
        "correctPrefixPredictionCoverage": (
            correct_prefix / prediction_length if prediction_length else None
        ),
        "targetPasteActions": target_paste_actions,
        "predictedPasteActions": predicted_paste_actions,
        "matchedPasteActions": matched_paste_actions,
    }


def summarize_prediction_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "examples": 0,
            "exactMatches": 0,
            "surroundingWhitespaceNormalizedExactMatches": 0,
            "emptyPredictions": 0,
            "macroNormalizedLevenshteinSimilarity": None,
            "microNormalizedLevenshteinSimilarity": None,
            "correctPrefix": None,
            "pasteActions": None,
        }

    examples = len(rows)
    target_characters = sum(int(row["targetCharacters"]) for row in rows)
    prediction_characters = sum(int(row["predictionCharacters"]) for row in rows)
    edit_distance = sum(int(row["levenshteinDistance"]) for row in rows)
    edit_denominator = sum(
        max(int(row["targetCharacters"]), int(row["predictionCharacters"]))
        for row in rows
    )
    correct_prefix = sum(int(row["correctPrefixCharacters"]) for row in rows)
    target_paste_actions = sum(int(row["targetPasteActions"]) for row in rows)
    predicted_paste_actions = sum(int(row["predictedPasteActions"]) for row in rows)
    matched_paste_actions = sum(int(row["matchedPasteActions"]) for row in rows)
    target_prefix_coverages = [
        float(row["correctPrefixTargetCoverage"])
        for row in rows
        if row["correctPrefixTargetCoverage"] is not None
    ]
    prediction_prefix_coverages = [
        float(row["correctPrefixPredictionCoverage"])
        for row in rows
        if row["correctPrefixPredictionCoverage"] is not None
    ]
    return {
        "examples": examples,
        "exactMatches": sum(bool(row["exactMatch"]) for row in rows),
        "exactMatchRate": sum(bool(row["exactMatch"]) for row in rows) / examples,
        "surroundingWhitespaceNormalizedExactMatches": sum(
            bool(row["surroundingWhitespaceNormalizedExactMatch"]) for row in rows
        ),
        "surroundingWhitespaceNormalizedExactMatchRate": sum(
            bool(row["surroundingWhitespaceNormalizedExactMatch"]) for row in rows
        ) / examples,
        "emptyPredictions": sum(bool(row["predictionEmpty"]) for row in rows),
        "macroNormalizedLevenshteinSimilarity": sum(
            float(row["normalizedLevenshteinSimilarity"]) for row in rows
        ) / examples,
        "microNormalizedLevenshteinSimilarity": (
            1.0 if edit_denominator == 0 else 1.0 - (edit_distance / edit_denominator)
        ),
        "totalTargetCharacters": target_characters,
        "totalPredictionCharacters": prediction_characters,
        "totalLevenshteinDistance": edit_distance,
        "correctPrefix": {
            "totalCharacters": correct_prefix,
            "meanCharactersPerExample": correct_prefix / examples,
            "macroMeanTargetCoverage": (
                sum(target_prefix_coverages) / len(target_prefix_coverages)
                if target_prefix_coverages
                else None
            ),
            "macroMeanPredictionCoverage": (
                sum(prediction_prefix_coverages) / len(prediction_prefix_coverages)
                if prediction_prefix_coverages
                else None
            ),
            "microTargetCoverage": (
                correct_prefix / target_characters if target_characters else None
            ),
            "microPredictionCoverage": (
                correct_prefix / prediction_characters if prediction_characters else None
            ),
        },
        "pasteActions": {
            "target": target_paste_actions,
            "predicted": predicted_paste_actions,
            "matched": matched_paste_actions,
            "precision": (
                matched_paste_actions / predicted_paste_actions
                if predicted_paste_actions
                else None
            ),
            "recall": (
                matched_paste_actions / target_paste_actions
                if target_paste_actions
                else None
            ),
        },
    }

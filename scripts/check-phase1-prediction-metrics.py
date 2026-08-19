#!/usr/bin/env python3
"""No-network checks for the Phase 1 generated-completion scorecard."""

from itertools import product

from phase1_prediction_metrics import (
    levenshtein_distance,
    score_prediction,
    summarize_prediction_metrics,
)


def baseline_levenshtein(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for row_index, left_character in enumerate(left, start=1):
        current = [row_index]
        for column_index, right_character in enumerate(right, start=1):
            current.append(min(
                current[-1] + 1,
                previous[column_index] + 1,
                previous[column_index - 1] + (left_character != right_character),
            ))
        previous = current
    return previous[-1]


def main() -> int:
    if not (
        levenshtein_distance("", "abc") == 3
        and levenshtein_distance("kitten", "sitting") == 3
        and levenshtein_distance("same", "same") == 0
    ):
        raise AssertionError("Levenshtein distance contract failed")
    short_strings = [""] + [
        "".join(value)
        for length in range(1, 5)
        for value in product("ab", repeat=length)
    ]
    for left in short_strings:
        for right in short_strings:
            if levenshtein_distance(left, right) != baseline_levenshtein(left, right):
                raise AssertionError("optimized Levenshtein distance changed its result")

    exact = score_prediction("abc", "abc", target_paste_actions=0)
    overlong = score_prediction("abc", "abcx", target_paste_actions=0)
    whitespace = score_prediction(" abc\n", "abc", target_paste_actions=0)
    paste = score_prediction(
        "before <|paste|> after",
        "before <|paste|> afterwards <|paste|>",
        target_paste_actions=1,
    )
    if not (
        exact["exactMatch"]
        and exact["normalizedLevenshteinSimilarity"] == 1.0
        and overlong["correctPrefixCharacters"] == 3
        and overlong["correctPrefixTargetCoverage"] == 1.0
        and overlong["correctPrefixPredictionCoverage"] == 0.75
        and whitespace["surroundingWhitespaceNormalizedExactMatch"]
        and not whitespace["exactMatch"]
        and paste["targetPasteActions"] == 1
        and paste["predictedPasteActions"] == 2
        and paste["matchedPasteActions"] == 1
    ):
        raise AssertionError("per-prediction metric contract failed")

    summary = summarize_prediction_metrics([exact, overlong, paste])
    if not (
        summary["examples"] == 3
        and summary["exactMatches"] == 1
        and summary["correctPrefix"]["totalCharacters"] == 28
        and summary["correctPrefix"]["macroMeanTargetCoverage"] == 1.0
        and summary["pasteActions"]["precision"] == 0.5
        and summary["pasteActions"]["recall"] == 1.0
    ):
        raise AssertionError("aggregate metric contract failed")
    print("Phase 1 prediction metric checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

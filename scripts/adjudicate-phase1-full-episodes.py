#!/usr/bin/env python3
"""Conservatively classify exhaustive shadow candidates into closed episodes."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


MIN_AUTHORED_CHARACTERS = 40
MIN_WORDS = 6


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def canonical_line(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"


def words(value: str) -> list[str]:
    return re.findall(r"[\w’']+", value, flags=re.UNICODE)


def candidate_app(candidate: dict[str, Any]) -> str:
    return (
        candidate.get("initialConditioningState", {})
        .get("destination", {})
        .get("bundleIdentifier", "")
    )


def candidate_passes(candidate: dict[str, Any]) -> bool:
    return bool(candidate.get("mechanicalGates", {}).get("passed"))


def prompt_onset_is_proven(candidate: dict[str, Any]) -> bool:
    onset = candidate.get("onsetEvidence") or {}
    return not onset.get("requiresProvenPromptOnset") or bool(
        onset.get("promptOnsetProven")
    )


def closure_reason(candidate: dict[str, Any]) -> str | None:
    closure = candidate.get("closureEvidence", {})
    status = closure.get("status")
    bundle = candidate_app(candidate)
    if status == "objective_submission_observed":
        return "objective_submission_observed"
    if closure.get("returnObserved") and prompt_onset_is_proven(candidate) and (
        bundle == "com.openai.codex"
        or (
            bundle == "com.google.Chrome"
            and any(
                marker in str(candidate.get("members", [{}])[0].get("windowTitle") or "").lower()
                for marker in ("chatgpt", "claude", "gemini")
            )
        )
    ):
        return "return_observed_in_submission_surface"
    following = candidate.get("closureContext", {}).get("followingEvents", [])
    # Session termination says only that observation stopped. It does not prove
    # that the user finished the thought. Treating EOF as closure admitted
    # visibly incomplete tails into loss (for example, a sentence ending in
    # "the core difference"). A reviewed episode may still override this, but
    # automatic construction requires an observed boundary.
    if not following:
        return None
    first = following[0]
    if first.get("kind") == "read":
        return "post_settlement_read_observed"
    if first.get("kind") == "write":
        member = candidate.get("members", [])[-1]
        current = (member.get("application"), member.get("windowTitle"))
        destination = first.get("destination") or {}
        later = (destination.get("application"), destination.get("window"))
        if later != current:
            return "different_visible_destination_write"
    return None


def fragment_reason(content: str, candidate: dict[str, Any]) -> str | None:
    if content.strip() in {"Do anything", "Ask Gemini", "Start writing..."}:
        return "application_prompt_scaffold"
    if not content.strip():
        return "empty_completion"
    if content[0].isspace() and candidate_app(candidate) != "md.obsidian":
        return "leading_continuation_whitespace"
    if content.lstrip().startswith((". ", ", ", ": ")):
        return "leading_continuation_punctuation"
    if content[-1].isspace():
        return "trailing_continuation_whitespace"
    lowered = content.rstrip().lower()
    if lowered.endswith((",", "(", "-", ":")):
        return "unfinished_terminal_punctuation"
    last = words(lowered)[-1] if words(lowered) else ""
    if last in {
        "a", "an", "and", "as", "at", "by", "for", "from", "in", "is",
        "of", "or", "that", "the", "to", "with", "than", "then",
    }:
        return "unfinished_terminal_word"
    for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
        if content.count(opening) != content.count(closing):
            return "unbalanced_delimiter"
    members = candidate.get("members", [])
    if len(members) == 1 and members[0].get("operation") != "insert":
        cursor = candidate.get("initialConditioningState", {}).get("cursorContext", {})
        if cursor.get("selectedText") or cursor.get("rightContext"):
            return "internal_non_insert_micro_edit"
    return None


def is_substantive(target: dict[str, Any]) -> bool:
    authored = "".join(
        segment.get("content", "")
        for segment in target.get("segments", [])
        if segment.get("type") == "authored_text"
    ).strip()
    return len(authored) >= MIN_AUTHORED_CHARACTERS and len(words(authored)) >= MIN_WORDS


def has_micro_fragment_evidence(candidate: dict[str, Any]) -> bool:
    members = candidate.get("members", [])
    if len(members) < 2:
        return False
    for member in members:
        content = member.get("currentLossTarget")
        if not isinstance(content, str):
            return True
        stripped = content.strip()
        if (
            len(stripped) < MIN_AUTHORED_CHARACTERS
            or content[:1].isspace()
            or content[-1:].isspace()
            or member.get("operation") != "insert"
            or member.get("boundaryReason") in {
                "pointer_selection_boundary", "selection_navigation"
            }
        ):
            return True
        lowered = stripped.lower()
        terminal = words(lowered)[-1] if words(lowered) else ""
        if lowered.endswith((",", "(", "-", ":")) or terminal in {
            "a", "an", "and", "as", "at", "by", "for", "from", "in", "is",
            "of", "or", "that", "the", "to", "with", "than", "then",
        }:
            return True
    return False


def authored_target(content: str) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "resolvedContent": content,
        "segments": [{"type": "authored_text", "content": content}],
    }


def normalize_episode_target(target: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(target))
    segments = result.get("segments", [])
    if len(segments) == 1 and segments[0].get("type") == "authored_text":
        content = segments[0].get("content", "")
        content = re.sub(
            "\\n\u200b(?:\\t)?\\n\u200b\\n-\\n\u200b ", "\\n", content
        ).strip()
        segments[0]["content"] = content
        result["resolvedContent"] = content
    else:
        scaffold = "\\n\u200b(?:\\t)?\\n\u200b\\n-\\n\u200b "
        authored_indices = [
            index for index, segment in enumerate(segments)
            if segment.get("type") == "authored_text"
        ]
        for index in authored_indices:
            segments[index]["content"] = re.sub(
                scaffold, "\n", segments[index].get("content", "")
            )
        if authored_indices:
            first = authored_indices[0]
            last = authored_indices[-1]
            removed_prefix = len(segments[first]["content"]) - len(
                segments[first]["content"].lstrip("\n\r\u200b")
            )
            removed_suffix = len(segments[last]["content"]) - len(
                segments[last]["content"].rstrip("\n\r\u200b")
            )
            segments[first]["content"] = segments[first]["content"].lstrip("\n\r\u200b")
            segments[last]["content"] = segments[last]["content"].rstrip("\n\r\u200b")
            resolved = re.sub(scaffold, "\n", result.get("resolvedContent", ""))
            if removed_prefix:
                resolved = resolved[removed_prefix:]
            if removed_suffix:
                resolved = resolved[:-removed_suffix]
            result["resolvedContent"] = resolved
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--candidates", required=True, type=Path)
    parser.add_argument("--prior-adjudications", action="append", type=Path, default=[])
    parser.add_argument("--approved-policy", type=Path)
    parser.add_argument("--output-adjudications", required=True, type=Path)
    parser.add_argument("--output-candidates", required=True, type=Path)
    args = parser.parse_args()

    corpus = args.corpus.resolve()
    events = load_jsonl(corpus / "events.jsonl")
    examples = load_jsonl(corpus / "examples.jsonl")
    event_by_id = {row["sourceEventID"]: row for row in events}
    example_by_target = {row["targetEventID"]: row for row in examples}
    write_ids = {row["sourceEventID"] for row in events if row["kind"] == "write"}
    candidates = load_jsonl(args.candidates.resolve())
    candidate_by_label = {row["label"]: row for row in candidates}

    old_targets: dict[str, dict[str, Any]] = {}
    old_decisions: dict[str, str] = {}
    for path in args.prior_adjudications:
        for row in load_jsonl(path.resolve()):
            label = row.get("label")
            if isinstance(label, str):
                old_decisions[label] = row.get("decision")
                if isinstance(row.get("finalizedTarget"), dict):
                    old_targets[label] = row["finalizedTarget"]
    approved_today: set[str] = set()
    if args.approved_policy:
        policy = load_json(args.approved_policy.resolve())
        approved_today = {row["label"] for row in policy.get("approvedMerges", [])}

    selected: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    claimed: set[str] = set()

    # Human-reviewed positive controls and merges have first priority, but the
    # current replay must still pass the production evidence gates.
    approved_prior = {
        label for label, decision in old_decisions.items()
        if decision in {"merge_closed_episode", "keep_single_closed_episode"}
    } | approved_today
    for label in sorted(approved_prior):
        candidate = candidate_by_label.get("reviewed_" + label)
        target = old_targets.get(label)
        if (
            candidate is None
            or target is None
            or not candidate_passes(candidate)
            or not prompt_onset_is_proven(candidate)
        ):
            continue
        members = candidate["memberWriteEventIDs"]
        if claimed.intersection(members):
            continue
        selected.append((candidate, normalize_episode_target(target), "reviewed_closed_episode"))
        claimed.update(members)

    # Add only maximal, mechanically proved automatic runs. Pasted runs require
    # explicit reviewed structured authorship and are not auto-merged.
    automatic = [
        row for row in candidates
        if (
            row["label"].startswith("automatic_run_")
            or row["label"].startswith("submission_onset_")
        )
        and candidate_passes(row)
        and prompt_onset_is_proven(row)
        and isinstance(
            row.get("singleCompletionDiagnostic", {}).get("proposedFinalizedTarget"), str
        )
    ]
    automatic.sort(key=lambda row: (-len(row["memberWriteEventIDs"]), row["beganAt"]))
    for candidate in automatic:
        members = candidate["memberWriteEventIDs"]
        if claimed.intersection(members):
            continue
        if closure_reason(candidate) is None:
            continue
        if any(
            index < len(candidate.get("members", [])) - 1
            and (
                member.get("boundaryReason") == "return_pressed"
                or "return" in member.get("inputHints", [])
            )
            for index, member in enumerate(candidate.get("members", []))
        ):
            continue
        source_targets = [
            example_by_target[value]["target"]
            for value in members if value in example_by_target
        ]
        if any(
            segment.get("type") == "paste"
            for target in source_targets for segment in target.get("segments", [])
        ):
            continue
        content = candidate["singleCompletionDiagnostic"]["proposedFinalizedTarget"]
        if not has_micro_fragment_evidence(candidate):
            continue
        incomplete = fragment_reason(content, {**candidate, "members": []})
        if incomplete not in {None, "leading_continuation_whitespace"}:
            continue
        cursor = candidate.get("initialConditioningState", {}).get("cursorContext", {})
        if cursor.get("selectedText") or (
            cursor.get("rightContext")
            and cursor.get("fieldState") != "unpopulated_prompt"
        ):
            continue
        selected.append((candidate, authored_target(content.strip()), "automatic_proven_composition"))
        claimed.update(members)

    # Remaining eligible WRITEs are evaluated as singletons. Closed but short
    # actions stay in historical WRITE context without receiving loss.
    for example in examples:
        event_id = example["targetEventID"]
        if event_id in claimed:
            continue
        candidate = candidate_by_label[f"singleton_{example['chronologicalOrdinal'] + 1:04d}"]
        target = normalize_episode_target(example["target"])
        close = closure_reason(candidate)
        fragment = fragment_reason(target["resolvedContent"], candidate)
        if (
            candidate_passes(candidate)
            and prompt_onset_is_proven(candidate)
            and close
            and fragment is None
        ):
            selected.append((candidate, target, "automatic_closed_singleton"))
            claimed.add(event_id)

    adjudications: list[dict[str, Any]] = []
    production_candidates: list[dict[str, Any]] = []
    used_candidate_ids: set[str] = set()
    for candidate, target, provenance in sorted(selected, key=lambda item: item[0]["beganAt"]):
        decision = "closed_loss_episode" if is_substantive(target) else "closed_history_episode"
        adjudications.append({
            "schemaVersion": 1,
            "label": candidate["label"],
            "candidateID": candidate["candidateID"],
            "memberWriteEventIDs": candidate["memberWriteEventIDs"],
            "decision": decision,
            "finalizedTarget": target,
            "closureReason": closure_reason(candidate),
            "classificationProvenance": provenance,
            "onsetEvidence": candidate.get("onsetEvidence"),
            "minimumAuthoredCharacters": MIN_AUTHORED_CHARACTERS,
            "minimumWords": MIN_WORDS,
        })
        production_candidates.append(candidate)
        used_candidate_ids.add(candidate["candidateID"])

    # Every remaining semantic WRITE is explicitly excluded. A minimal bound
    # candidate is sufficient because excluded evidence is never stitched into
    # model-facing history or a target.
    for ordinal, event_id in enumerate(sorted(write_ids - claimed)):
        candidate_id = "excluded_candidate_" + event_id.removeprefix("evt_")
        candidate = {
            "schemaVersion": 2,
            "candidateID": candidate_id,
            "label": f"excluded_{ordinal:04d}",
            "memberWriteEventIDs": [event_id],
            "authority": "conservative_full_coverage_exclusion",
        }
        production_candidates.append(candidate)
        adjudications.append({
            "schemaVersion": 1,
            "label": candidate["label"],
            "candidateID": candidate_id,
            "memberWriteEventIDs": [event_id],
            "decision": "exclude_unresolved_episode",
            "reason": (
                "not_proven_closed_composition" if event_id in example_by_target
                else "history_write_not_bound_to_closed_composition"
            ),
        })

    covered = [value for row in adjudications for value in row["memberWriteEventIDs"]]
    if len(covered) != len(set(covered)) or set(covered) != write_ids:
        raise ValueError("episode adjudications do not form an exact WRITE partition")
    for path in (args.output_adjudications, args.output_candidates):
        if path.exists():
            raise ValueError(f"output exists: {path}")
    with args.output_adjudications.open("w", encoding="utf-8") as handle:
        for row in adjudications:
            handle.write(canonical_line(row))
    with args.output_candidates.open("w", encoding="utf-8") as handle:
        for row in production_candidates:
            handle.write(canonical_line(row))
    counts = {
        "sourceWrites": len(write_ids),
        "closedEpisodes": len(selected),
        "lossEpisodes": sum(row["decision"] == "closed_loss_episode" for row in adjudications),
        "historyOnlyEpisodes": sum(row["decision"] == "closed_history_episode" for row in adjudications),
        "excludedWrites": sum(row["decision"] == "exclude_unresolved_episode" for row in adjudications),
        "microWritesAbsorbed": sum(len(candidate["memberWriteEventIDs"]) for candidate, _, _ in selected),
    }
    print(json.dumps(counts, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

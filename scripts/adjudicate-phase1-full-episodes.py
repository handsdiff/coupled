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
MIN_SUBMITTED_AUTHORED_CHARACTERS = 4
CHROMIUM_BROWSER_BUNDLES = {"com.google.Chrome", "company.thebrowser.Browser"}


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
            bundle in CHROMIUM_BROWSER_BUNDLES
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
    members = candidate.get("members", [])
    submitted = bool(
        candidate.get("closureEvidence", {}).get("returnObserved")
        or candidate.get("closureEvidence", {}).get("objectiveSubmissionBoundary")
    )
    if (
        len(members) == 1
        and members[0].get("operation") != "insert"
        and not submitted
    ):
        return "internal_non_insert_micro_edit"
    return None


def is_substantive(
    target: dict[str, Any], candidate: dict[str, Any], provenance: str
) -> bool:
    authored = "".join(
        segment.get("content", "")
        for segment in target.get("segments", [])
        if segment.get("type") == "authored_text"
    ).strip()
    if provenance == "reviewed_regression_fixture":
        return True
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


def target_from_policy(candidate: dict[str, Any], policy: str) -> dict[str, Any]:
    if policy == "candidate_net_edit":
        content = candidate.get("singleCompletionDiagnostic", {}).get(
            "proposedFinalizedTarget"
        )
        if not isinstance(content, str) or not content:
            raise ValueError(f"candidate has no proved net edit: {candidate.get('label')}")
        return authored_target(content)
    if policy == "observed_net_edit":
        content = (
            candidate.get("singleCompletionDiagnostic", {})
            .get("netFieldEdit", {})
            .get("content")
        )
        if not isinstance(content, str) or not content:
            raise ValueError(f"candidate has no observed net edit: {candidate.get('label')}")
        return authored_target(content)
    if policy == "terminal_field_value":
        content = candidate.get("finalObservation", {}).get("value")
        if not isinstance(content, str) or not content:
            raise ValueError(f"candidate has no terminal field value: {candidate.get('label')}")
        return authored_target(content)
    if policy == "first_source_target":
        target = candidate.get("members", [{}])[0].get("currentTarget")
        if not isinstance(target, dict):
            raise ValueError(f"candidate has no source target: {candidate.get('label')}")
        return json.loads(json.dumps(target))
    if policy == "proven_paste_authorship":
        evidence = candidate.get("pasteAuthorshipEvidence", [])
        if len(evidence) != 1 or evidence[0].get("status") != "proven":
            raise ValueError(f"candidate lacks unique paste proof: {candidate.get('label')}")
        segments = json.loads(json.dumps(evidence[0].get("segments", [])))
        for segment in segments:
            if segment.get("type") == "paste":
                segment.pop("content", None)
        return {
            "schemaVersion": 1,
            "resolvedContent": evidence[0]["resolvedContent"],
            "segments": segments,
        }
    raise ValueError(f"unknown target policy {policy}: {candidate.get('label')}")


def semantic_history_target(event: dict[str, Any]) -> dict[str, Any] | None:
    try:
        audit = json.loads(event.get("auditSerialized", "{}"))
    except json.JSONDecodeError:
        return None
    content = audit.get("resolvedCompletion", audit.get("content"))
    segments = audit.get("authorshipSegments")
    if not isinstance(content, str) or not content or not isinstance(segments, list) or not segments:
        return None
    return {
        "schemaVersion": 1,
        "resolvedContent": content,
        "segments": json.loads(json.dumps(segments)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--candidates", action="append", required=True, type=Path)
    parser.add_argument("--regression-policy", type=Path)
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
    candidates = [
        row
        for path in args.candidates
        for row in load_jsonl(path.resolve())
    ]
    candidate_ids = [row.get("candidateID") for row in candidates]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("candidate evidence contains duplicate IDs")
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

    selected: list[tuple[dict[str, Any], dict[str, Any], str, str | None]] = []
    claimed: set[str] = set()

    # User/reviewer-adjudicated cases are stable regression requirements, not
    # manual edits to derived JSONL. They still bind to replayed raw candidate
    # evidence and cannot overlap any other episode.
    if args.regression_policy:
        policy = load_json(args.regression_policy.resolve())
        rows = policy.get("adjudications")
        if not isinstance(rows, list) or not rows:
            raise ValueError("regression policy has no adjudications")
        for row in rows:
            label = row.get("label")
            candidate = candidate_by_label.get(label)
            if candidate is None or not candidate_passes(candidate):
                raise ValueError(f"regression candidate missing or fails gates: {label}")
            members = candidate["memberWriteEventIDs"]
            if claimed.intersection(members):
                raise ValueError(f"overlapping regression episode: {label}")
            decision = row.get("decision")
            if decision not in {"closed_loss_episode", "closed_history_episode"}:
                raise ValueError(f"invalid regression decision: {label}")
            if decision == "closed_loss_episode" and not prompt_onset_is_proven(candidate):
                raise ValueError(f"loss regression lacks prediction-time onset: {label}")
            target = target_from_policy(candidate, row.get("targetPolicy"))
            selected.append((candidate, normalize_episode_target(target), "reviewed_regression_fixture", decision))
            claimed.update(members)

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
        selected.append((candidate, normalize_episode_target(target), "reviewed_closed_episode", None))
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
        selected.append((candidate, authored_target(content.strip()), "automatic_proven_composition", None))
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
            selected.append((candidate, target, "automatic_closed_singleton", None))
            claimed.add(event_id)

    adjudications: list[dict[str, Any]] = []
    production_candidates: list[dict[str, Any]] = []
    used_candidate_ids: set[str] = set()
    for candidate, target, provenance, forced_decision in sorted(
        selected, key=lambda item: item[0]["beganAt"]
    ):
        decision = forced_decision or (
            "closed_loss_episode"
            if is_substantive(target, candidate, provenance)
            else "closed_history_episode"
        )
        adjudications.append({
            "schemaVersion": 1,
            "label": candidate["label"],
            "candidateID": candidate["candidateID"],
            "memberWriteEventIDs": candidate["memberWriteEventIDs"],
            "decision": decision,
            "finalizedTarget": target,
            "closureReason": (
                closure_reason(candidate)
                or (
                    "human_reviewed_closed_composition"
                    if decision == "closed_loss_episode"
                    else "resolved_history_transition"
                )
            ),
            "classificationProvenance": provenance,
            "onsetEvidence": candidate.get("onsetEvidence"),
            "minimumAuthoredCharacters": MIN_AUTHORED_CHARACTERS,
            "minimumWords": MIN_WORDS,
            "minimumSubmittedAuthoredCharacters": MIN_SUBMITTED_AUTHORED_CHARACTERS,
        })
        production_candidates.append(candidate)
        used_candidate_ids.add(candidate["candidateID"])

    # Preserve every remaining semantically resolved WRITE as history. An
    # inability to prove closure or target onset withholds loss; it does not
    # erase a user-visible state transition from later causal context.
    for ordinal, event_id in enumerate(sorted(write_ids - claimed)):
        event = event_by_id[event_id]
        target = semantic_history_target(event)
        if target is not None:
            candidate_id = "history_candidate_" + event_id.removeprefix("evt_")
            candidate = {
                "schemaVersion": 3,
                "candidateID": candidate_id,
                "label": f"resolved_history_{ordinal:04d}",
                "memberWriteEventIDs": [event_id],
                "authority": "semantic_resolved_history_projection",
                "beganAt": event.get("beganAt"),
                "candidateAvailableAt": event.get("availableAt"),
                "semanticHistoryProjection": target,
            }
            production_candidates.append(candidate)
            adjudications.append({
                "schemaVersion": 1,
                "label": candidate["label"],
                "candidateID": candidate_id,
                "memberWriteEventIDs": [event_id],
                "decision": "closed_history_episode",
                "finalizedTarget": target,
                "closureReason": "semantic_transition_preserved_without_target_eligibility",
                "classificationProvenance": "semantic_resolved_history_fallback",
                "minimumAuthoredCharacters": MIN_AUTHORED_CHARACTERS,
                "minimumWords": MIN_WORDS,
                "minimumSubmittedAuthoredCharacters": MIN_SUBMITTED_AUTHORED_CHARACTERS,
            })
            continue
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
        "microWritesAbsorbed": sum(
            len(candidate["memberWriteEventIDs"])
            for candidate, _, _, _ in selected
        ),
    }
    print(json.dumps(counts, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

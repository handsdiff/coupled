#!/usr/bin/env python3
"""Reconstruct human and model timing from preserved Phase 1 evidence.

Human time begins when the latest materially relevant READ or prior closed
WRITE completed and ends at the target's final contributing mutation. A model
sample is projected once, three seconds after that material boundary. Focus,
pointer movement, application routing, and the target's own expression do not
move the human-time anchor.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


PASTE_MARKER = "<|paste|>"
TIMING_SCHEMA_VERSION = 2
TIMING_CONVERSION_VERSION = "phase1-human-timing-v2"
CONFIRMED_SUBMISSION_DISPOSITIONS = {
    "confirmed_field_cleared",
    "confirmed_field_disappeared",
    "confirmed_placeholder_restored",
    "surface_changed",
}


class TimingError(RuntimeError):
    """Raised when source evidence is missing or internally inconsistent."""


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def seconds(later: datetime, earlier: datetime) -> float:
    return round((later - earlier).total_seconds(), 3)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise TimingError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise TimingError(f"expected an object in {path}")
    return value


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with path.open() as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise TimingError(f"expected an object at {path}:{line_number}")
                yield value
    except (OSError, json.JSONDecodeError) as error:
        raise TimingError(f"cannot read {path}: {error}") from error


def target_text(target: dict[str, Any]) -> str:
    pieces: list[str] = []
    for segment in target.get("segments", []):
        if not isinstance(segment, dict):
            continue
        pieces.append(
            PASTE_MARKER
            if segment.get("type") == "paste"
            else str(segment.get("content", ""))
        )
    return "".join(pieces)


@dataclass(frozen=True)
class SemanticEvent:
    event_id: str
    unit_id: str | None
    kind: str
    session_id: str
    began_at: datetime | None
    available_at: datetime
    source_record_ids: tuple[str, ...]
    closure_status: str | None


@dataclass(frozen=True)
class MaterialAction:
    occurred_at: datetime
    model_available_at: datetime
    event_id: str
    kind: str
    source_record_ids: tuple[str, ...]
    evidence: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class Example:
    ordinal: int
    example_id: str
    session_id: str
    target_episode_id: str
    began_at: datetime
    available_at: datetime
    source_record_ids: tuple[str, ...]
    context_event_ids: tuple[str, ...]
    target_text: str


@dataclass(frozen=True)
class Gate:
    reading_words_per_minute: float = 238.0
    mental_evaluation_seconds: float = 1.35
    shortcut_keystrokes: int = 4
    seconds_per_shortcut_keystroke: float = 0.2
    minimum_net_savings_seconds: float = 1.0

    def interaction_seconds(self, text: str) -> float:
        words = len(re.findall(r"\S+", text))
        return round(
            60 * words / self.reading_words_per_minute
            + self.mental_evaluation_seconds
            + self.shortcut_keystrokes * self.seconds_per_shortcut_keystroke,
            3,
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "readingWordsPerMinute": self.reading_words_per_minute,
            "mentalEvaluationSeconds": self.mental_evaluation_seconds,
            "shortcutKeystrokes": self.shortcut_keystrokes,
            "secondsPerShortcutKeystroke": self.seconds_per_shortcut_keystroke,
            "minimumNetSavingsSeconds": self.minimum_net_savings_seconds,
        }


def compact_examples(path: Path, first_ordinal: int) -> list[Example]:
    result: list[Example] = []
    for line_number, row in enumerate(iter_jsonl(path), start=1):
        ordinal = int(row["chronologicalOrdinal"]) + 1
        if ordinal != line_number:
            raise TimingError(f"non-contiguous example ordinal at {path}:{line_number}")
        if ordinal < first_ordinal:
            continue
        began = row.get("targetBeganAt")
        available = row.get("targetAvailableAt")
        if not isinstance(began, str) or not isinstance(available, str):
            raise TimingError(f"example {ordinal} lacks target timing")
        result.append(
            Example(
                ordinal=ordinal,
                example_id=str(row["exampleID"]),
                session_id=str(row.get("sessionID", "")),
                target_episode_id=str(
                    row.get("targetUnitID")
                    or row.get("targetEventID")
                    or row["exampleID"]
                ),
                began_at=parse_time(began),
                available_at=parse_time(available),
                source_record_ids=tuple(row.get("targetSourceRecordIDs", [])),
                context_event_ids=tuple(row.get("contextEventIDs", [])),
                target_text=target_text(row.get("target", {})),
            )
        )
    return result


def semantic_events(path: Path) -> dict[str, SemanticEvent]:
    by_id: dict[str, SemanticEvent] = {}
    for row in iter_jsonl(path):
        available = row.get("availableAt")
        event_id = row.get("sourceEventID")
        if not isinstance(available, str) or not isinstance(event_id, str):
            continue
        began = row.get("beganAt")
        unit_id = row.get("episodeID")
        event = SemanticEvent(
            event_id=event_id,
            unit_id=str(unit_id) if isinstance(unit_id, str) else None,
            kind=str(row.get("kind", "unknown")),
            session_id=str(row.get("sessionID", "")),
            began_at=parse_time(began) if isinstance(began, str) else None,
            available_at=parse_time(available),
            source_record_ids=tuple(row.get("sourceRecordIDs", [])),
            closure_status=(
                str(row["closureStatus"])
                if isinstance(row.get("closureStatus"), str)
                else None
            ),
        )
        if event_id in by_id:
            raise TimingError(f"duplicate semantic event ID: {event_id}")
        by_id[event_id] = event
        if event.unit_id is not None:
            existing = by_id.get(event.unit_id)
            if existing is not None and existing != event:
                raise TimingError(f"duplicate semantic unit ID: {event.unit_id}")
            by_id[event.unit_id] = event
    return by_id


def raw_paths(corpus: Path, project: Path) -> list[tuple[str, Path]]:
    relative_paths: set[str] = set()
    for candidate in iter_jsonl(corpus / "raw-episode-candidates.jsonl"):
        for member in candidate.get("members", []):
            if not isinstance(member, dict):
                continue
            value = member.get("rawPath")
            if isinstance(value, str):
                relative_paths.add(value)
    if not relative_paths:
        raise TimingError("candidate evidence names no raw source files")
    resolved = [(relative, (project / relative).resolve()) for relative in relative_paths]
    missing = [str(path) for _, path in resolved if not path.is_file()]
    if missing:
        raise TimingError(f"missing raw source files: {', '.join(missing)}")
    return sorted(resolved)


def collect_raw_evidence(
    paths: Iterable[tuple[str, Path]],
    needed_record_ids: set[str],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, list[dict[str, Any]]],
    list[dict[str, str]],
]:
    records: dict[str, dict[str, Any]] = {}
    submissions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    source_files: list[dict[str, str]] = []
    for relative, path in paths:
        source_files.append({"path": relative, "sha256": sha256_file(path)})
        for row in iter_jsonl(path):
            record_id = str(row.get("recordID", ""))
            if record_id in needed_record_ids:
                previous = records.get(record_id)
                if previous is not None and previous != row:
                    raise TimingError(f"conflicting raw record ID: {record_id}")
                records[record_id] = row
            if row.get("recordType") == "prompt_submission_observation":
                source_id = row.get("sourceWriteRecordID")
                if isinstance(source_id, str) and source_id in needed_record_ids:
                    submissions[source_id].append(row)
    for rows in submissions.values():
        rows.sort(key=lambda row: str(row.get("observedAt", "")))
    return records, dict(submissions), source_files


def _input_times(
    rows: Iterable[dict[str, Any]], *, mutating: bool
) -> list[tuple[datetime, str, str]]:
    result: list[tuple[datetime, str, str]] = []
    for row in rows:
        record_id = str(row.get("recordID", ""))
        for input_event in row.get("inputEvents", []):
            if not isinstance(input_event, dict):
                continue
            observed = input_event.get("observedAt")
            if not isinstance(observed, str):
                continue
            if bool(input_event.get("mutationCapable")) is not mutating:
                continue
            result.append(
                (
                    parse_time(observed),
                    record_id,
                    str(input_event.get("hint", "input")),
                )
            )
    return result


def _confirmed_submission_times(
    event: SemanticEvent,
    rows: Iterable[dict[str, Any]],
    submissions_by_write: dict[str, list[dict[str, Any]]],
) -> list[tuple[datetime, str, str]]:
    if event.closure_status != "closed_submission":
        return []
    result = [
        value
        for value in _input_times(rows, mutating=False)
        if value[2] in {"return", "submit"}
    ]
    for source_id in event.source_record_ids:
        for observation in submissions_by_write.get(source_id, []):
            if observation.get("disposition") not in CONFIRMED_SUBMISSION_DISPOSITIONS:
                continue
            action = observation.get("action")
            observed = action.get("observedAt") if isinstance(action, dict) else None
            if isinstance(observed, str):
                result.append(
                    (
                        parse_time(observed),
                        str(observation.get("recordID", "")),
                        str(action.get("kind", "submission")),
                    )
                )
    return result


def material_action_for_event(
    event: SemanticEvent,
    raw_records: dict[str, dict[str, Any]],
    submissions_by_write: dict[str, list[dict[str, Any]]],
    *,
    before: datetime,
) -> MaterialAction | None:
    rows = [
        raw_records[record_id]
        for record_id in event.source_record_ids
        if record_id in raw_records
    ]
    if event.kind == "read":
        human_times: list[tuple[datetime, str]] = []
        model_times = [event.available_at]
        for row in rows:
            last_activity = row.get("lastActivityAt")
            if isinstance(last_activity, str):
                when = parse_time(last_activity)
                if when < before:
                    human_times.append((when, str(row.get("recordID", ""))))
            observed = row.get("observedAt") or row.get("capturedAt")
            if isinstance(observed, str):
                model_times.append(parse_time(observed))
        if not human_times:
            return None
        occurred_at = max(value[0] for value in human_times)
        return MaterialAction(
            occurred_at=occurred_at,
            model_available_at=max(model_times),
            event_id=event.event_id,
            kind=event.kind,
            source_record_ids=event.source_record_ids,
            evidence=tuple(
                {
                    "recordID": record_id,
                    "recordType": "screen_ocr_observation",
                    "humanActionAt": format_time(when),
                }
                for when, record_id in sorted(human_times)
            ),
        )
    if event.kind != "write":
        return None

    mutations = [
        value for value in _input_times(rows, mutating=True) if value[0] < before
    ]
    if not mutations:
        return None
    final_mutation = max(mutations)
    completions = [final_mutation]
    completions.extend(
        value
        for value in _confirmed_submission_times(event, rows, submissions_by_write)
        if final_mutation[0] <= value[0] < before
    )
    completed = max(completions)
    evidence: list[dict[str, Any]] = [
        {
            "recordID": final_mutation[1],
            "recordType": "active_tap_write_attempt",
            "humanActionAt": format_time(final_mutation[0]),
            "hint": final_mutation[2],
            "role": "final_contributing_mutation",
        }
    ]
    if completed != final_mutation:
        evidence.append(
            {
                "recordID": completed[1],
                "recordType": "prompt_submission_observation",
                "humanActionAt": format_time(completed[0]),
                "hint": completed[2],
                "role": "confirmed_submission",
            }
        )
    return MaterialAction(
        occurred_at=completed[0],
        model_available_at=event.available_at,
        event_id=event.event_id,
        kind=event.kind,
        source_record_ids=event.source_record_ids,
        evidence=tuple(evidence),
    )


def final_mutation(
    example: Example, raw_records: dict[str, dict[str, Any]]
) -> tuple[datetime, datetime, datetime | None, list[dict[str, Any]]]:
    mutating: list[datetime] = []
    submission: list[datetime] = []
    evidence: list[dict[str, Any]] = []
    for record_id in example.source_record_ids:
        row = raw_records.get(record_id)
        if row is None:
            raise TimingError(f"example {example.ordinal} lacks raw target {record_id}")
        for input_event in row.get("inputEvents", []):
            if not isinstance(input_event, dict):
                continue
            observed = input_event.get("observedAt")
            if not isinstance(observed, str):
                continue
            when = parse_time(observed)
            is_mutating = bool(input_event.get("mutationCapable"))
            hint = str(input_event.get("hint", "input"))
            evidence.append(
                {
                    "recordID": record_id,
                    "observedAt": format_time(when),
                    "hint": hint,
                    "mutationCapable": is_mutating,
                }
            )
            if is_mutating:
                mutating.append(when)
            elif hint in {"return", "submit"}:
                submission.append(when)
        observed_submission = row.get("submissionObservedAt")
        if isinstance(observed_submission, str):
            submission.append(parse_time(observed_submission))
    if not mutating:
        raise TimingError(f"example {example.ordinal} has no mutation-capable input")
    first = min(mutating)
    final = max(mutating)
    workflow = max((value for value in submission if value >= final), default=None)
    return first, final, workflow, evidence


def reconstruct_record(
    example: Example,
    events_by_id: dict[str, SemanticEvent],
    raw_records: dict[str, dict[str, Any]],
    submissions_by_write: dict[str, list[dict[str, Any]]],
    sample_delay_seconds: float,
    gate: Gate,
) -> dict[str, Any]:
    first_mutation, final_content, workflow_completion, mutation_evidence = (
        final_mutation(example, raw_records)
    )
    interaction = gate.interaction_seconds(example.target_text)
    base: dict[str, Any] = {
        "oneBasedExampleOrdinal": example.ordinal,
        "exampleID": example.example_id,
        "targetEpisodeID": example.target_episode_id,
        "sessionID": example.session_id,
        "targetWordCount": len(re.findall(r"\S+", example.target_text)),
        "sampleDelaySeconds": sample_delay_seconds,
        "expressionBeganAt": format_time(example.began_at),
        "firstMutationAt": format_time(first_mutation),
        "humanFinishedAt": format_time(final_content),
        "humanWorkflowCompletedAt": format_time(workflow_completion),
        "writeAvailableAt": format_time(example.available_at),
        "humanTypingSeconds": seconds(final_content, first_mutation),
        "writeSettlementLagSeconds": seconds(example.available_at, final_content),
        "estimatedSuggestionInteractionSeconds": interaction,
        "minimumNetSavingsSeconds": gate.minimum_net_savings_seconds,
        "mutationEvidence": mutation_evidence,
    }
    actions: list[MaterialAction] = []
    missing_context_ids: list[str] = []
    for event_id in example.context_event_ids:
        event = events_by_id.get(event_id)
        if event is None:
            missing_context_ids.append(event_id)
            continue
        if event.session_id != example.session_id:
            continue
        action = material_action_for_event(
            event,
            raw_records,
            submissions_by_write,
            before=example.began_at,
        )
        if action is not None:
            actions.append(action)
    if missing_context_ids:
        raise TimingError(
            f"example {example.ordinal} references unknown context events: "
            + ", ".join(missing_context_ids)
        )
    if not actions:
        return {
            **base,
            "humanStartAt": None,
            "modelSampleAt": None,
            "requiredContextAvailableAt": None,
            "modelStartAt": None,
            "humanTimeSeconds": None,
            "modelStartToHumanFinishSeconds": None,
            "sampleRelationToWriting": None,
            "timingReconstructable": False,
            "realTimeUtilityEligible": False,
            "disposition": "timing_unknown_no_material_anchor",
        }

    anchor = max(actions, key=lambda value: (value.occurred_at, value.event_id))
    sample_at = anchor.occurred_at + timedelta(seconds=sample_delay_seconds)
    model_start_at = max(sample_at, anchor.model_available_at)
    human_time = seconds(final_content, anchor.occurred_at)
    model_to_finish = seconds(final_content, model_start_at)
    ideal_net = round(model_to_finish - interaction, 3)
    if sample_at < first_mutation:
        sample_relation = "before_writing"
    elif sample_at <= final_content:
        sample_relation = "during_writing"
    else:
        sample_relation = "after_writing"
    return {
        **base,
        "disposition": "timing_reconstructed",
        "humanStartAt": format_time(anchor.occurred_at),
        "humanStartKind": anchor.kind,
        "humanStartEventID": anchor.event_id,
        "humanStartSourceRecordIDs": list(anchor.source_record_ids),
        "humanStartEvidence": list(anchor.evidence),
        "modelSampleAt": format_time(sample_at),
        "requiredContextAvailableAt": format_time(anchor.model_available_at),
        "modelStartAt": format_time(model_start_at),
        "contextReadinessDelaySeconds": seconds(model_start_at, sample_at),
        "humanTimeSeconds": human_time,
        "sampleToHumanFinishSeconds": seconds(final_content, sample_at),
        "modelStartToHumanFinishSeconds": model_to_finish,
        "sampleRelationToWriting": sample_relation,
        "idealSuggestionNetSavingsSeconds": ideal_net,
        "timingReconstructable": True,
        "realTimeUtilityEligible": ideal_net > gate.minimum_net_savings_seconds,
    }


def real_time_suggestion_pass(
    timing: dict[str, Any],
    generation_latency_seconds: float,
    *,
    semantic_pass: bool,
    substantive: bool,
) -> bool:
    """Return whether a useful suggestion would beat completed human writing."""

    if (
        not semantic_pass
        or not substantive
        or not timing.get("timingReconstructable")
        or not timing.get("realTimeUtilityEligible")
    ):
        return False
    human_seconds = timing.get("modelStartToHumanFinishSeconds")
    interaction_seconds = timing.get("estimatedSuggestionInteractionSeconds")
    minimum_savings = timing.get("minimumNetSavingsSeconds", 1.0)
    if not isinstance(human_seconds, (int, float)) or not isinstance(
        interaction_seconds, (int, float)
    ):
        return False
    return (
        float(human_seconds)
        - float(generation_latency_seconds)
        - float(interaction_seconds)
        > float(minimum_savings)
    )


def build_artifact(
    project: Path,
    corpus: Path,
    *,
    first_ordinal: int = 51,
    sample_delay_seconds: float = 3.0,
    gate: Gate | None = None,
) -> dict[str, Any]:
    gate = gate or Gate()
    manifest = load_json(corpus / "corpus.json")
    examples = compact_examples(corpus / "examples.jsonl", first_ordinal)
    events_by_id = semantic_events(corpus / "events.jsonl")
    sources = raw_paths(corpus, project)
    context_ids = {
        event_id for example in examples for event_id in example.context_event_ids
    }
    unknown = context_ids - set(events_by_id)
    if unknown:
        raise TimingError(f"corpus references {len(unknown)} unknown semantic events")
    needed_records = {
        record_id
        for event_id in context_ids
        for record_id in events_by_id[event_id].source_record_ids
    }
    needed_target_records = {
        record_id for example in examples for record_id in example.source_record_ids
    }
    needed_records.update(needed_target_records)
    raw_records, submissions, source_files = collect_raw_evidence(
        sources, needed_records
    )
    missing_targets = needed_target_records - set(raw_records)
    if missing_targets:
        raise TimingError(f"missing {len(missing_targets)} target raw records")
    records = [
        reconstruct_record(
            example,
            events_by_id,
            raw_records,
            submissions,
            sample_delay_seconds,
            gate,
        )
        for example in examples
    ]
    counts: dict[str, int] = defaultdict(int)
    for record in records:
        counts[str(record["disposition"])] += 1
        relation = record.get("sampleRelationToWriting")
        if isinstance(relation, str):
            counts[f"sample_{relation}"] += 1
    counts["records"] = len(records)
    counts["timingReconstructed"] = sum(
        bool(row.get("timingReconstructable")) for row in records
    )
    counts["realTimeUtilityEligible"] = sum(
        bool(row.get("realTimeUtilityEligible")) for row in records
    )
    return {
        "artifactType": "phase1_human_timing",
        "schemaVersion": TIMING_SCHEMA_VERSION,
        "conversionVersion": TIMING_CONVERSION_VERSION,
        "corpusID": manifest.get("corpusID"),
        "source": {
            "corpusPath": str(corpus),
            "corpusManifestSHA256": sha256_file(corpus / "corpus.json"),
            "eventsSHA256": sha256_file(corpus / "events.jsonl"),
            "rawEpisodeCandidatesSHA256": sha256_file(
                corpus / "raw-episode-candidates.jsonl"
            ),
            "rawFiles": source_files,
        },
        "policy": {
            "firstOneBasedExampleOrdinal": first_ordinal,
            "sampleDelaySeconds": sample_delay_seconds,
            "humanStart": "latest material canonical READ or prior closed WRITE in the same continuous session",
            "readHumanBoundary": "raw lastActivityAt for the canonical READ",
            "writeHumanBoundary": "final contributing mutation, extended through confirmed submission when the episode closed by submission",
            "nonMaterialActivity": "focus, pointer movement, application routing, duplicate or suppressed OCR, and target expression do not move humanStartAt",
            "modelSampleAt": "humanStartAt plus sampleDelaySeconds",
            "modelStartAt": "max(modelSampleAt, required material context availability)",
            "humanFinish": "final mutation-capable input in the closed target episode",
            "humanTimeInterpretation": "wall-clock estimate from the last observable material action; long gaps can include unobserved inactivity and remain visible rather than being silently capped",
            "workflowCompletion": "non-mutating Return/submission after final content mutation, retained only as a diagnostic",
            "writeAvailableAt": "retained diagnostic only; never the human-completion endpoint",
            "semanticScoring": "independent manual judgment of prediction content; never gated by timing",
            "realTimeScoring": "semantic pass whose generation plus review and acceptance beats humanFinishedAt by the minimum savings",
            "generationConditioningCaveat": "existing generations use target-onset destination and cursor conditioning; timing is a retrospective latency projection and destination routing remains deferred",
            "interactionGate": gate.as_json(),
        },
        "counts": dict(sorted(counts.items())),
        "records": records,
    }

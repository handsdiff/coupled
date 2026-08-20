#!/usr/bin/env python3
"""Serve a read-only local inspector for a Phase 1 closed-episode corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import threading
import urllib.parse
import webbrowser
from collections import Counter
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


PASTE_MARKER = "<|paste|>"


class InspectorError(RuntimeError):
    """Raised when an immutable episode artifact cannot be inspected safely."""


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
                        f"expected a JSON object at {path}:{line_number}"
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


def target_text(target: Any, resolved_paste: bool = False) -> str:
    if not isinstance(target, dict):
        return ""
    pieces: list[str] = []
    for segment in target.get("segments", []):
        if not isinstance(segment, dict):
            continue
        if segment.get("type") == "paste" and not resolved_paste:
            pieces.append(PASTE_MARKER)
        else:
            pieces.append(str(segment.get("content", "")))
    if pieces:
        return "".join(pieces)
    return str(target.get("resolvedContent", ""))


def authored_text(target: Any) -> str:
    if not isinstance(target, dict):
        return ""
    return "".join(
        str(segment.get("content", ""))
        for segment in target.get("segments", [])
        if isinstance(segment, dict) and segment.get("type") == "authored_text"
    )


def display_decision(value: str) -> str:
    return {
        "closed_loss_episode": "Loss-bearing",
        "closed_history_episode": "History only",
        "exclude_unresolved_episode": "Unusable evidence",
    }.get(value, value.replace("_", " ").title())


@dataclass(frozen=True)
class Paths:
    corpus: Path
    candidates: Path
    baseline_corpus: Path | None


def discover_paths(
    project: Path,
    corpus_arg: Path | None,
    candidates_arg: Path | None,
    baseline_corpus_arg: Path | None,
) -> Paths:
    if corpus_arg is not None:
        corpus = corpus_arg.expanduser().resolve()
    else:
        choices = sorted(
            [
                *project.glob("coupled-data/phase1-raw-episode-corpus-*/corpus.json"),
                *project.glob("coupled-data/phase1-closed-episode-corpus-*/corpus.json"),
            ],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not choices:
            raise InspectorError("no episode corpus exists")
        corpus = choices[0].parent

    manifest_path = corpus / "corpus.json"
    manifest = load_json(manifest_path)
    required = [
        manifest_path,
        corpus / "episode-adjudications.jsonl",
        corpus / "episode-exclusions.jsonl",
        corpus / "events.jsonl",
        corpus / "examples.jsonl",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise InspectorError("missing corpus artifacts:\n" + "\n".join(missing))

    if candidates_arg is not None:
        candidates = candidates_arg.expanduser().resolve()
    else:
        evidence = manifest.get("source", {}).get("candidateEvidenceSHA256", {})
        candidates = Path(next(iter(evidence), "")) if evidence else Path()
        if not candidates.is_absolute():
            candidates = corpus / candidates
        if not candidates.is_file():
            candidate_choices = sorted(
                project.glob("coupled-data/phase1-full-episode-production-candidates-*.jsonl"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            if not candidate_choices:
                raise InspectorError("no production episode-candidate evidence exists")
            candidates = candidate_choices[0]

    expected = manifest.get("source", {}).get("candidateEvidenceSHA256", {}).get(
        str(candidates)
    )
    if expected and sha256(candidates) != expected:
        raise InspectorError("candidate evidence digest does not match corpus manifest")
    baseline_corpus = None
    if baseline_corpus_arg is not None:
        baseline_corpus = baseline_corpus_arg.expanduser().resolve()
        for name in ("corpus.json", "examples.jsonl"):
            if not (baseline_corpus / name).is_file():
                raise InspectorError(
                    f"baseline corpus is missing {name}: {baseline_corpus}"
                )
    return Paths(
        corpus=corpus,
        candidates=candidates,
        baseline_corpus=baseline_corpus,
    )


class EpisodeStore:
    def __init__(self, paths: Paths):
        self.paths = paths
        self.manifest = load_json(paths.corpus / "corpus.json")
        self.adjudications = load_jsonl(paths.corpus / "episode-adjudications.jsonl")
        self.exclusions = load_jsonl(paths.corpus / "episode-exclusions.jsonl")
        self.events = load_jsonl(paths.corpus / "events.jsonl")
        self.examples = load_jsonl(paths.corpus / "examples.jsonl")
        self.baseline_examples = (
            load_jsonl(paths.baseline_corpus / "examples.jsonl")
            if paths.baseline_corpus is not None
            else []
        )
        self.baseline_example_by_id = {
            row["exampleID"]: row
            for row in self.baseline_examples
            if isinstance(row.get("exampleID"), str)
        }
        self.candidates = load_jsonl(paths.candidates)
        source_path = Path(str(self.manifest.get("source", {}).get("path", "")))
        source_events_path = source_path / "events.jsonl"
        if not source_events_path.is_file():
            raise InspectorError(
                f"source semantic events do not exist: {source_events_path}"
            )
        self.source_events = load_jsonl(source_events_path)
        self.project = Path(__file__).resolve().parent.parent

        self.candidate_by_id = self._index(self.candidates, "candidateID", "candidates")
        self.adjudication_by_id = self._index(
            self.adjudications, "candidateID", "adjudications"
        )
        self.event_by_candidate = {
            row["candidateID"]: row
            for row in self.events
            if row.get("kind") == "write" and isinstance(row.get("candidateID"), str)
        }
        self.example_by_candidate = {
            row.get("episode", {}).get("candidateID"): row
            for row in self.examples
            if isinstance(row.get("episode"), dict)
        }
        self.exclusion_by_candidate = self._index(
            self.exclusions, "candidateID", "exclusions"
        )
        self.source_event_by_id = self._index(
            self.source_events, "sourceEventID", "source semantic events"
        )
        self.raw_record_by_id = self._load_raw_records()
        self._validate()
        self.summaries = [self._summary(row) for row in self.adjudications]
        self.summaries.sort(key=lambda row: (row["beganAt"], row["candidateID"]))
        for ordinal, row in enumerate(self.summaries, start=1):
            row["ordinal"] = ordinal

        current_example_ids = {
            row["exampleID"]
            for row in self.examples
            if isinstance(row.get("exampleID"), str)
        }
        self.removed_baseline_examples = [
            row
            for row in self.baseline_examples
            if row.get("exampleID") not in current_example_ids
        ]

    def _load_raw_records(self) -> dict[str, dict[str, Any]]:
        needed = {
            record_id
            for event in self.source_events
            for record_id in event.get("sourceRecordIDs", [])
            if isinstance(record_id, str)
        }
        sessions = {
            event.get("sessionID") for event in self.source_events
            if isinstance(event.get("sessionID"), str)
        }
        records: dict[str, dict[str, Any]] = {}
        for manifest_path in (self.project / "coupled-data").glob("*/session.json"):
            try:
                manifest = load_json(manifest_path)
            except InspectorError:
                continue
            if manifest.get("sessionID") not in sessions:
                continue
            raw_path = manifest_path.parent / "raw.jsonl"
            if not raw_path.is_file():
                continue
            with raw_path.open() as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    value = json.loads(line)
                    record_id = value.get("recordID")
                    if record_id in needed and record_id not in records:
                        records[record_id] = {
                            "record": value,
                            "path": str(raw_path),
                            "line": line_number,
                        }
        return records

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
        missing = sorted(set(self.adjudication_by_id) - set(self.candidate_by_id))
        if missing:
            raise InspectorError(f"candidate evidence missing for {len(missing)} decisions")
        counts = self.manifest.get("counts", {})
        if counts.get("closedEpisodeEvents") != len(self.event_by_candidate):
            raise InspectorError("closed episode event count disagrees with manifest")
        if counts.get("examples") != len(self.examples):
            raise InspectorError("loss-bearing example count disagrees with manifest")
        loss_ids = {
            row["candidateID"]
            for row in self.adjudications
            if row.get("decision") == "closed_loss_episode"
        }
        if loss_ids != set(self.example_by_candidate):
            raise InspectorError("loss-bearing adjudications and examples disagree")

    def _summary(self, adjudication: dict[str, Any]) -> dict[str, Any]:
        candidate_id = adjudication["candidateID"]
        candidate = self.candidate_by_id[candidate_id]
        members = self._members(adjudication, candidate)
        first = members[0] if members else {}
        target = adjudication.get("finalizedTarget") or {}
        authored = authored_text(target).strip()
        app = first.get("application") or first.get("targetIdentity", {}).get(
            "bundleIdentifier"
        )
        window = first.get("windowTitle") or first.get("targetIdentity", {}).get(
            "windowTitle"
        )
        decision = str(adjudication.get("decision", "unknown"))
        classification = adjudication.get("classificationProvenance")
        exclusion = self.exclusion_by_candidate.get(candidate_id)
        unresolved_text = " → ".join(
            value
            for value in (target_text(member.get("currentTarget")) for member in members)
            if value
        )
        example = self.example_by_candidate.get(candidate_id)
        example_id = example.get("exampleID") if isinstance(example, dict) else None
        baseline_example = self.baseline_example_by_id.get(example_id)
        delta_status = None
        if isinstance(example_id, str) and self.paths.baseline_corpus is not None:
            if baseline_example is None:
                delta_status = "added"
            elif baseline_example.get("target") != example.get("target"):
                delta_status = "changed"
            else:
                delta_status = "unchanged"
        return {
            "candidateID": candidate_id,
            "label": adjudication.get("label") or candidate.get("label"),
            "decision": decision,
            "decisionLabel": display_decision(decision),
            "classificationProvenance": classification,
            "application": app,
            "window": window,
            "beganAt": candidate.get("beganAt") or first.get("beganAt") or "",
            "availableAt": candidate.get("candidateAvailableAt")
            or first.get("availableAt")
            or "",
            "durationSeconds": candidate.get("durationSeconds"),
            "memberCount": len(members),
            "target": target_text(target) or unresolved_text,
            "resolvedContent": str(target.get("resolvedContent", "")),
            "authoredCharacters": len(authored),
            "authoredWords": len(authored.split()),
            "pasteCount": sum(
                1
                for segment in target.get("segments", [])
                if isinstance(segment, dict) and segment.get("type") == "paste"
            ),
            "closureReason": adjudication.get("closureReason"),
            "reconstructionStatus": adjudication.get("reconstructionStatus"),
            "closureStatus": adjudication.get("closureStatus"),
            "lossEligibility": adjudication.get("lossEligibility"),
            "decisionReason": adjudication.get("reason"),
            "selectionRationale": candidate.get("selectionRationale"),
            "exclusionReason": exclusion.get("reason") if exclusion else None,
            "mechanicalGatesPassed": candidate.get("mechanicalGates", {}).get(
                "passed"
            ),
            "exampleID": example_id,
            "deltaStatus": delta_status,
        }

    @staticmethod
    def _audit_value(source_event: dict[str, Any]) -> dict[str, Any]:
        value = source_event.get("auditSerialized")
        if not isinstance(value, str):
            return {}
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _source_member(self, event_id: str) -> dict[str, Any] | None:
        source = self.source_event_by_id.get(event_id)
        if source is None:
            return None
        audit = self._audit_value(source)
        segments = audit.get("authorshipSegments", [])
        target = {
            "schemaVersion": 1,
            "resolvedContent": audit.get("resolvedCompletion", audit.get("content", "")),
            "segments": segments,
        }
        raw_entries = [
            self.raw_record_by_id[value]
            for value in source.get("sourceRecordIDs", [])
            if value in self.raw_record_by_id
        ]
        raw_records = [value["record"] for value in raw_entries]
        first_raw = raw_records[0] if raw_records else {}
        last_raw = raw_records[-1] if raw_records else {}
        before = first_raw.get("before") if isinstance(first_raw, dict) else None
        after = last_raw.get("after") if isinstance(last_raw, dict) else None
        terminal_source = audit.get("derivationObservationSource")
        terminal = after
        if terminal_source == "pre_return_checkpoint":
            checkpoints = last_raw.get("returnCheckpoints", [])
            terminal = checkpoints[-1].get("observation") if checkpoints else after
        elif terminal_source == "post_paste_checkpoint":
            checkpoints = last_raw.get("pasteCheckpoints", [])
            terminal = checkpoints[-1].get("observation") if checkpoints else after
        input_hints = sorted({
            hint for record in raw_records for hint in record.get("inputHints", [])
        })
        return {
            "writeEventID": event_id,
            "sourceRecordID": next(iter(source.get("sourceRecordIDs", [])), None),
            "sourceRecordIDs": source.get("sourceRecordIDs", []),
            "beganAt": source.get("beganAt"),
            "availableAt": source.get("availableAt"),
            "application": audit.get("appName"),
            "windowTitle": audit.get("windowTitle"),
            "boundaryReason": audit.get("boundaryReason"),
            "inputHints": input_hints,
            "inputEventCount": sum(
                record.get("inputEventCount", 0) for record in raw_records
            ) or None,
            "operation": audit.get("operation"),
            "characterOffset": audit.get("characterOffset"),
            "removedContent": audit.get("removedContent"),
            "currentTarget": target,
            "beforeLogicalValue": before.get("value") if isinstance(before, dict) else None,
            "selectedTerminalLogicalValue": (
                terminal.get("value") if isinstance(terminal, dict) else None
            ),
            "selectedTerminalObservationSource": terminal_source,
            "targetIdentity": {
                "bundleIdentifier": audit.get("bundleIdentifier"),
                "windowTitle": audit.get("windowTitle"),
            },
            "conditioningState": first_raw.get("conditioningState"),
            "rawEvidence": {
                "records": [
                    {"recordID": entry["record"].get("recordID"), "path": entry["path"], "line": entry["line"]}
                    for entry in raw_entries
                ],
                "pasteCheckpointCount": sum(
                    len(record.get("pasteCheckpoints", [])) for record in raw_records
                ),
                "returnCheckpointCount": sum(
                    len(record.get("returnCheckpoints", [])) for record in raw_records
                ),
                "mutationCheckpointCount": sum(
                    len(record.get("mutationCheckpoints", [])) for record in raw_records
                ),
                "beforeAXErrors": first_raw.get("beforeAXErrors", []),
                "afterAXErrors": last_raw.get("afterAXErrors", []),
                "pasteCheckpoints": [
                    {
                        "checkpointID": checkpoint.get("checkpointID"),
                        "clipboardSnapshotID": checkpoint.get("clipboardSnapshotID"),
                        "clipboardChangeCount": checkpoint.get("clipboardChangeCount"),
                        "clipboardText": checkpoint.get("clipboardText"),
                        "clipboardTextWasTruncated": checkpoint.get("clipboardTextWasTruncated"),
                    }
                    for record in raw_records
                    for checkpoint in record.get("pasteCheckpoints", [])
                ],
            },
            "semanticReduction": audit,
            "sourceSemanticEvent": source,
        }

    def _members(
        self, adjudication: dict[str, Any], candidate: dict[str, Any]
    ) -> list[dict[str, Any]]:
        members = candidate.get("members")
        if isinstance(members, list) and members:
            return [value for value in members if isinstance(value, dict)]
        result: list[dict[str, Any]] = []
        for event_id in adjudication.get("memberWriteEventIDs", []):
            if not isinstance(event_id, str):
                continue
            member = self._source_member(event_id)
            if member is not None:
                result.append(member)
        return result

    def meta(self) -> dict[str, Any]:
        decisions = Counter(row["decision"] for row in self.summaries)
        apps = Counter(row["application"] or "Unknown" for row in self.summaries)
        member_counts = Counter(row["memberCount"] for row in self.summaries)
        deltas = Counter(
            row["deltaStatus"]
            for row in self.summaries
            if isinstance(row.get("deltaStatus"), str)
        )
        deltas["removed"] = len(self.removed_baseline_examples)
        return {
            "corpusID": self.manifest.get("corpusID"),
            "episodeVersion": self.manifest.get("episodeVersion"),
            "conversionVersion": self.manifest.get("conversionVersion"),
            "counts": self.manifest.get("counts"),
            "eligibility": self.manifest.get("eligibility"),
            "decisions": decisions,
            "applications": apps,
            "memberCounts": member_counts,
            "deltas": deltas,
            "paths": {
                "corpus": str(self.paths.corpus),
                "candidateEvidence": str(self.paths.candidates),
                "baselineCorpus": (
                    str(self.paths.baseline_corpus)
                    if self.paths.baseline_corpus is not None
                    else None
                ),
            },
        }

    def detail(self, candidate_id: str) -> dict[str, Any]:
        adjudication = self.adjudication_by_id.get(candidate_id)
        candidate = self.candidate_by_id.get(candidate_id)
        if adjudication is None or candidate is None:
            raise KeyError(candidate_id)
        event = self.event_by_candidate.get(candidate_id)
        example = self.example_by_candidate.get(candidate_id)
        exclusion = self.exclusion_by_candidate.get(candidate_id)
        members = []
        for index, member in enumerate(self._members(adjudication, candidate), start=1):
            source_member = self._source_member(str(member.get("writeEventID"))) or {}
            target = member.get("currentTarget")
            if not isinstance(target, dict):
                target = {}
            members.append(
                {
                    "ordinal": index,
                    "writeEventID": member.get("writeEventID"),
                    "sourceRecordID": member.get("sourceRecordID"),
                    "beganAt": member.get("beganAt"),
                    "availableAt": member.get("availableAt"),
                    "application": member.get("application"),
                    "windowTitle": member.get("windowTitle"),
                    "boundaryReason": member.get("boundaryReason"),
                    "inputHints": member.get("inputHints"),
                    "inputEventCount": member.get("inputEventCount"),
                    "operation": member.get("operation"),
                    "characterOffset": member.get("characterOffset"),
                    "removedContent": member.get("removedContent"),
                    "microTarget": target_text(target),
                    "microResolvedContent": target.get("resolvedContent"),
                    "beforeLogicalValue": member.get("beforeLogicalValue"),
                    "selectedTerminalLogicalValue": member.get(
                        "selectedTerminalLogicalValue"
                    ),
                    "selectedTerminalObservationSource": member.get(
                        "selectedTerminalObservationSource"
                    ),
                    "targetIdentity": member.get("targetIdentity"),
                    "conditioningState": member.get("conditioningState"),
                    "semanticReduction": member.get("semanticReduction"),
                    "pasteAuthorshipEvidence": member.get("pasteAuthorshipEvidence"),
                    "rawEvidence": member.get("rawEvidence") or source_member.get("rawEvidence"),
                }
            )
        return {
            "summary": next(
                row for row in self.summaries if row["candidateID"] == candidate_id
            ),
            "adjudication": adjudication,
            "candidateEvidence": {
                key: value
                for key, value in candidate.items()
                if key not in {"members"}
            },
            "members": members,
            "episodeEvent": event,
            "trainingExample": example,
            "baselineTrainingExample": self.baseline_example_by_id.get(
                example.get("exampleID") if isinstance(example, dict) else None
            ),
            "exclusion": exclusion,
        }


HTML = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Coupled · Episode Corpus</title>
  <style>
    :root {
      color-scheme: dark;
      --bg:#090b0e; --panel:#11151a; --panel2:#171c22; --line:#29313a;
      --text:#ecf1f6; --muted:#8e9aa8; --loss:#67dca9; --history:#78b8ff;
      --excluded:#ff7c86; --accent:#f1fa8c; --micro:#c7a8ff; --delta:#ffbf69;
      --mono:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;
      --sans:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    }
    *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--text);font-family:var(--sans);overflow:hidden}
    button,input,select{font:inherit;color:inherit;background:var(--panel2);border:1px solid var(--line);border-radius:7px}
    button{cursor:pointer;padding:7px 10px} button:hover{border-color:#52606e}
    header{height:74px;border-bottom:1px solid var(--line);padding:12px 18px;display:flex;align-items:center;gap:22px;background:#0d1014}
    .brand{font-size:16px;font-weight:750;white-space:nowrap}.brand span{color:var(--accent)}
    .metrics{display:flex;gap:18px;min-width:0}.metric{font-size:11px;color:var(--muted);white-space:nowrap}.metric strong{font-size:18px;color:var(--text);display:block}
    .version{margin-left:auto;color:var(--muted);font:11px var(--mono);text-align:right;white-space:nowrap}
    .layout{height:calc(100vh - 74px);display:grid;grid-template-columns:390px minmax(0,1fr)}
    aside{border-right:1px solid var(--line);min-height:0;display:flex;flex-direction:column}
    .filters{padding:12px;border-bottom:1px solid var(--line);display:grid;gap:8px}.filters input{padding:9px 10px;width:100%}
    .filter-row{display:grid;grid-template-columns:1fr 1fr;gap:8px}.filters select{padding:8px;min-width:0;width:100%}
    .check-row{display:flex;gap:12px;color:var(--muted);font-size:12px;align-items:center}.check-row label{display:flex;gap:5px;align-items:center}.check-row input{accent-color:var(--accent)}
    .result-count{font-size:11px;color:var(--muted)} #list{overflow:auto;flex:1}
    .row{padding:11px 12px;border-bottom:1px solid #1d242b;cursor:pointer}.row:hover{background:#14191f}.row.selected{background:#1b222a;box-shadow:inset 3px 0 var(--accent)}
    .row-top,.row-bottom{display:flex;gap:7px;align-items:center;color:var(--muted);font-size:10px}.row-title{font:12px/1.4 var(--mono);margin:7px 0;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere}
    .badge{display:inline-flex;border:1px solid var(--line);padding:2px 6px;border-radius:999px;white-space:nowrap}.loss{color:var(--loss);border-color:#2e634e}.history{color:var(--history);border-color:#31597d}.excluded{color:var(--excluded);border-color:#71353c}.micro{color:var(--micro);border-color:#554475}.delta{color:var(--delta);border-color:#775a32}
    main{min-width:0;min-height:0;display:flex;flex-direction:column}.detail-head{padding:14px 20px;border-bottom:1px solid var(--line);background:#0e1115}.detail-head h1{font:15px/1.45 var(--mono);margin:0;overflow-wrap:anywhere}.subline{margin-top:7px;display:flex;gap:10px;flex-wrap:wrap;color:var(--muted);font-size:11px}
    #detail{overflow:auto;min-height:0;padding:18px 20px 80px}.empty{color:var(--muted);padding:30px;text-align:center}
    .grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;margin-bottom:12px;overflow:hidden}.card-head{padding:9px 12px;border-bottom:1px solid var(--line);font-size:11px;color:var(--muted);display:flex;gap:8px;align-items:center}.card-head strong{color:var(--text)}.card-body{padding:12px}
    pre{margin:0;white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.55 var(--mono)}.target{border-left:3px solid var(--loss)}.decision{border-left:3px solid var(--history)}
    .threshold{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}.threshold div{background:var(--panel2);padding:9px;border-radius:7px;color:var(--muted);font-size:10px}.threshold strong{display:block;font-size:16px;color:var(--text);margin-bottom:2px}
    .micro-card{border-left:3px solid var(--micro)}.micro-card summary,.json-card summary{cursor:pointer;list-style:none;padding:10px 12px;display:flex;gap:8px;align-items:center}.micro-card summary::-webkit-details-marker,.json-card summary::-webkit-details-marker{display:none}.micro-card summary::before,.json-card summary::before{content:'›';color:var(--muted)}.micro-card[open] summary::before,.json-card[open] summary::before{transform:rotate(90deg)}
    .micro-body,.json-body{padding:12px;border-top:1px solid var(--line)}.micro-text{padding:10px;background:#0b0e11;border-radius:7px;margin:8px 0}.micro-meta{display:flex;gap:8px;flex-wrap:wrap;color:var(--muted);font:10px var(--mono)}
    .state-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.state-label{font-size:10px;color:var(--muted);margin-bottom:5px}.state{max-height:330px;overflow:auto;background:#0a0d10;padding:10px;border-radius:7px}
    .kv{display:grid;grid-template-columns:180px 1fr;gap:7px 12px;font:11px/1.4 var(--mono)}.kv dt{color:var(--muted)}.kv dd{margin:0;overflow-wrap:anywhere}
    @media(max-width:900px){.layout{grid-template-columns:330px minmax(0,1fr)}.grid,.state-grid{grid-template-columns:1fr}.metrics .optional{display:none}.threshold{grid-template-columns:repeat(2,1fr)}}
  </style>
</head>
<body>
  <header>
    <div class="brand">Coupled <span>episode review</span></div>
    <div class="metrics" id="metrics"></div>
    <div class="version" id="version"></div>
  </header>
  <div class="layout">
    <aside>
      <div class="filters">
        <input id="search" type="search" placeholder="Search target, app, window…" autocomplete="off">
        <div class="filter-row">
          <select id="decision"><option value="all">All decisions</option><option value="closed_loss_episode">Loss-bearing</option><option value="closed_history_episode">History only</option><option value="exclude_unresolved_episode">Unusable evidence</option></select>
          <select id="application"><option value="all">All applications</option></select>
        </div>
        <select id="delta"><option value="all">All baseline statuses</option><option value="added_or_changed">Added or changed targets</option><option value="added">Added targets</option><option value="changed">Changed targets</option><option value="unchanged">Unchanged targets</option></select>
        <div class="check-row">
          <label><input id="multi" type="checkbox"> Multi-WRITE only</label>
          <label><input id="paste" type="checkbox"> Paste only</label>
        </div>
        <div class="result-count" id="result-count"></div>
      </div>
      <div id="list"></div>
    </aside>
    <main>
      <div class="detail-head" id="detail-head"><h1>Select an episode</h1><div class="subline">Inspect the exact target, decision, micro-WRITE trajectory, and evidence.</div></div>
      <div id="detail"><div class="empty">Loading the episode-normalized corpus…</div></div>
    </main>
  </div>
<script>
const $=id=>document.getElementById(id); let summaries=[],meta=null,selected=null;
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const json=v=>esc(JSON.stringify(v,null,2));
const decisionClass=d=>d==='closed_loss_episode'?'loss':d==='closed_history_episode'?'history':'excluded';
const shortTime=v=>v?new Date(v).toLocaleString(): 'unknown time';
const fmtDuration=v=>Number.isFinite(v)?`${v.toFixed(1)}s`:'—';
function filtered(){const q=$('search').value.toLowerCase(),d=$('decision').value,a=$('application').value,z=$('delta').value,m=$('multi').checked,p=$('paste').checked;return summaries.filter(x=>(d==='all'||x.decision===d)&&(a==='all'||x.application===a)&&(z==='all'||x.deltaStatus===z||(z==='added_or_changed'&&(x.deltaStatus==='added'||x.deltaStatus==='changed')))&&(!m||x.memberCount>1)&&(!p||x.pasteCount>0)&&(!q||[x.target,x.resolvedContent,x.application,x.window,x.label,x.closureReason,x.exclusionReason,x.selectionRationale,x.beganAt,x.availableAt].join(' ').toLowerCase().includes(q)));}
function renderList(){const rows=filtered();$('result-count').textContent=`${rows.length} of ${summaries.length} decisions`;$('list').innerHTML=rows.map(x=>`<div class="row ${x.candidateID===selected?'selected':''}" data-id="${esc(x.candidateID)}"><div class="row-top"><span>#${x.ordinal}</span><span class="badge ${decisionClass(x.decision)}">${esc(x.decisionLabel)}</span>${x.deltaStatus==='added'||x.deltaStatus==='changed'?`<span class="badge delta">${esc(x.deltaStatus)} target</span>`:''}${x.memberCount>1?`<span class="badge micro">${x.memberCount} micro-WRITEs</span>`:''}</div><div class="row-title">${esc(x.target||`[${x.exclusionReason||'no finalized target'}]`)}</div><div class="row-bottom"><span>${esc(x.application||'Unknown')}</span><span>·</span><span>${esc(shortTime(x.beganAt))}</span><span>·</span><span>${x.authoredCharacters} chars</span></div></div>`).join('')||'<div class="empty">No matching episodes.</div>';document.querySelectorAll('.row').forEach(el=>el.onclick=()=>select(el.dataset.id));}
function targetSegments(target){return (target?.segments||[]).map(s=>`<span class="badge ${s.type==='paste'?'micro':'loss'}">${esc(s.type)}</span>`).join(' ')}
function kv(obj,keys){return `<dl class="kv">${keys.map(([label,key])=>`<dt>${esc(label)}</dt><dd>${esc(obj?.[key]??'—')}</dd>`).join('')}</dl>`}
function evidenceCard(title,obj,open=false){return `<details class="card json-card" ${open?'open':''}><summary><strong>${esc(title)}</strong></summary><div class="json-body"><pre>${json(obj)}</pre></div></details>`}
function renderMember(m){return `<details class="card micro-card" ${m.ordinal===1?'open':''}><summary><strong>Micro-WRITE ${m.ordinal}</strong><span class="badge micro">${esc(m.operation||'unknown')}</span><span>${esc(m.boundaryReason||'')}</span></summary><div class="micro-body"><div class="micro-meta"><span>${esc(shortTime(m.beganAt))}</span><span>→ ${esc(shortTime(m.availableAt))}</span><span>offset ${esc(m.characterOffset)}</span><span>${esc((m.inputHints||[]).join(', '))}</span><span>${esc(m.inputEventCount)} inputs</span></div><div class="micro-text"><div class="state-label">MICRO TARGET / HISTORY TRANSITION</div><pre>${esc(m.microTarget)}</pre></div>${m.removedContent?`<div class="micro-text"><div class="state-label">REMOVED</div><pre>${esc(m.removedContent)}</pre></div>`:''}<div class="state-grid"><div><div class="state-label">BEFORE FIELD STATE</div><pre class="state">${esc(m.beforeLogicalValue)}</pre></div><div><div class="state-label">SELECTED TERMINAL STATE · ${esc(m.selectedTerminalObservationSource||'unknown')}</div><pre class="state">${esc(m.selectedTerminalLogicalValue)}</pre></div></div>${m.pasteAuthorshipEvidence?evidenceCard('Paste authorship proof',m.pasteAuthorshipEvidence,true):''}${m.rawEvidence?evidenceCard('Raw input/checkpoint evidence',m.rawEvidence):''}${evidenceCard('Identity and conditioning',{targetIdentity:m.targetIdentity,conditioningState:m.conditioningState})}${evidenceCard('Semantic reduction',m.semanticReduction)}</div></details>`}
async function select(id){selected=id;renderList();$('detail').innerHTML='<div class="empty">Loading evidence…</div>';const r=await fetch(`/api/episode?id=${encodeURIComponent(id)}`);if(!r.ok){$('detail').innerHTML='<div class="empty">Could not load episode.</div>';return}const d=await r.json(),s=d.summary,a=d.adjudication,c=d.candidateEvidence,target=a.finalizedTarget||{};$('detail-head').innerHTML=`<h1>${esc(s.target||`[${s.exclusionReason||'no finalized target'}]`)}</h1><div class="subline"><span class="badge ${decisionClass(s.decision)}">${esc(s.decisionLabel)}</span>${s.deltaStatus==='added'||s.deltaStatus==='changed'?`<span class="badge delta">${esc(s.deltaStatus)} target</span>`:''}<span>${esc(s.application||'Unknown')}</span><span>${esc(s.window||'')}</span><span>${esc(shortTime(s.beganAt))}</span><span>${s.memberCount} micro-WRITE${s.memberCount===1?'':'s'}</span></div>`;
const threshold=`<div class="card decision"><div class="card-head"><strong>Decision</strong><span>${esc(s.classificationProvenance||'unusable evidence')}</span></div><div class="card-body"><div class="threshold"><div><strong>${s.authoredCharacters}</strong>authored chars</div><div><strong>${s.authoredWords}</strong>authored words</div><div><strong>${s.memberCount}</strong>micro-WRITEs consolidated</div><div><strong>${s.pasteCount}</strong>grounded paste actions</div></div><div style="margin-top:12px">${kv({reconstruction:s.reconstructionStatus,closureStatus:s.closureStatus,loss:s.lossEligibility,reason:s.decisionReason,closure:a.closureReason,exclusion:s.exclusionReason,gates:s.mechanicalGatesPassed,policy:s.classificationProvenance},[['Reconstruction','reconstruction'],['Closure status','closureStatus'],['Loss eligibility','loss'],['Decision reason','reason'],['Closure evidence','closure'],['Exclusion','exclusion'],['Mechanical gates passed','gates'],['Classification policy','policy']])}</div></div></div>`;
const targetCard=`<div class="card target"><div class="card-head"><strong>${s.decision==='closed_loss_episode'?'LOSS TARGET':'FINALIZED EPISODE CONTENT'}</strong><span>${targetSegments(target)}</span></div><div class="card-body"><pre>${esc(s.target)}</pre>${s.resolvedContent&&s.resolvedContent!==s.target?`<details style="margin-top:12px"><summary>Resolved content used in later history</summary><pre style="margin-top:8px">${esc(s.resolvedContent)}</pre></details>`:''}</div></div>`;
$('detail').innerHTML=targetCard+(d.baselineTrainingExample?evidenceCard('Baseline target',d.baselineTrainingExample.target,true):'')+threshold+`<div class="card"><div class="card-head"><strong>Micro-WRITE trajectory</strong><span>raw field snapshots remain audit evidence</span></div><div class="card-body">${d.members.map(renderMember).join('')||'<div class="empty">No member trajectory available.</div>'}</div></div><div class="grid">${evidenceCard('Onset evidence',c.onsetEvidence||a.onsetEvidence,true)}${evidenceCard('Closure evidence',c.closureEvidence||c.closureContext,true)}${evidenceCard('Causal evidence',c.causalEvidence)}${evidenceCard('State-machine transitions',c.episodeStateMachine)}${evidenceCard('Continuity evidence',c.continuityEvidence)}${evidenceCard('Surface evidence',c.surfaceEvidence)}${evidenceCard('Single-completion diagnostic',c.singleCompletionDiagnostic)}</div>${evidenceCard('Full decision',a)}${d.trainingExample?evidenceCard('Training example metadata',{exampleID:d.trainingExample.exampleID,chronologicalOrdinal:d.trainingExample.chronologicalOrdinal,targetBeganAt:d.trainingExample.targetBeganAt,targetAvailableAt:d.trainingExample.targetAvailableAt,targetMask:d.trainingExample.targetMask,cursorFidelity:d.trainingExample.cursorFidelity}):''}${d.exclusion?evidenceCard('Exclusion record',d.exclusion):''}`;}
async function boot(){const [m,s]=await Promise.all([fetch('/api/meta').then(r=>r.json()),fetch('/api/episodes').then(r=>r.json())]);meta=m;summaries=s.episodes;$('metrics').innerHTML=`<div class="metric"><strong>${m.counts.sourceWrites}</strong>micro-WRITEs</div><div class="metric"><strong>${m.counts.closedEpisodeEvents}</strong>closed episodes</div><div class="metric"><strong style="color:var(--loss)">${m.decisions.closed_loss_episode||0}</strong>loss-bearing</div>${m.paths.baselineCorpus?`<div class="metric"><strong style="color:var(--delta)">${(m.deltas.added||0)+(m.deltas.changed||0)}</strong>target deltas</div>`:''}<div class="metric optional"><strong style="color:var(--history)">${m.decisions.closed_history_episode||0}</strong>history only</div><div class="metric optional"><strong style="color:var(--excluded)">${m.decisions.exclude_unresolved_episode||0}</strong>unusable evidence</div>`;$('version').innerHTML=`${esc(m.episodeVersion)}<br>${esc(m.conversionVersion)}`;Object.keys(m.applications).sort().forEach(a=>{const o=document.createElement('option');o.value=a;o.textContent=`${a} (${m.applications[a]})`;$('application').appendChild(o)});if(m.paths.baselineCorpus)$('delta').value='added_or_changed';renderList();}
['search','decision','application','delta','multi','paste'].forEach(id=>$(id).addEventListener(id==='search'?'input':'change',renderList));boot().catch(e=>{$('detail').innerHTML=`<div class="empty">${esc(e)}</div>`});
</script>
</body>
</html>'''


class Handler(BaseHTTPRequestHandler):
    store: EpisodeStore

    def log_message(self, format: str, *args: Any) -> None:
        return

    def send_json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            payload = HTML.encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path == "/api/meta":
            self.send_json(self.store.meta())
            return
        if parsed.path == "/api/episodes":
            self.send_json({"episodes": self.store.summaries})
            return
        if parsed.path == "/api/episode":
            candidate_id = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
            try:
                self.send_json(self.store.detail(candidate_id))
            except KeyError:
                self.send_json({"error": "unknown candidate"}, HTTPStatus.NOT_FOUND)
            return
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, help="closed-episode corpus directory")
    parser.add_argument("--candidates", type=Path, help="bound candidate evidence JSONL")
    parser.add_argument(
        "--baseline-corpus",
        type=Path,
        help="optional prior episode corpus used to highlight target deltas",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--open", action="store_true", help="open the inspector in a browser")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project = Path(__file__).resolve().parent.parent
    paths = discover_paths(
        project,
        args.corpus,
        args.candidates,
        args.baseline_corpus,
    )
    store = EpisodeStore(paths)
    handler = type("BoundHandler", (Handler,), {"store": store})
    server = ThreadingHTTPServer((args.host, args.port), handler)
    url = f"http://{args.host}:{server.server_port}"
    print(f"Coupled episode inspector: {url}", flush=True)
    print(f"Corpus: {paths.corpus}", flush=True)
    print(f"Candidate evidence: {paths.candidates}", flush=True)
    if paths.baseline_corpus is not None:
        print(f"Baseline corpus: {paths.baseline_corpus}", flush=True)
    if args.open:
        threading.Timer(0.2, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

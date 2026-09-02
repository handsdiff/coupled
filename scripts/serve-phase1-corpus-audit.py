#!/usr/bin/env python3
"""Serve a deterministic, read-only audit sample of a Phase 1 episode corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import urllib.parse
import webbrowser
from collections import Counter, defaultdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class AuditError(RuntimeError):
    pass


READ_PROVENANCE = {
    "ax_tree": {
        "label": "AX tree pane",
        "description": "OCR was re-run inside a non-fallback AX-pane selection.",
    },
    "ax_fallback": {
        "label": "AX v2 fallback",
        "description": "AX-pane v2 ran, but its conservative selector retained the fallback region.",
    },
    "legacy": {
        "label": "Legacy pointer crop",
        "description": "This historical READ predates the accepted AX-pane projection.",
    },
    "unresolved": {
        "label": "Unresolved provenance",
        "description": "The immutable source READ could not be joined to its semantic evidence.",
    },
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AuditError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise AuditError(f"expected JSON object: {path}")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise AuditError(f"expected object at {path}:{line_number}")
                rows.append(value)
    except (OSError, json.JSONDecodeError) as error:
        raise AuditError(f"cannot read {path}: {error}") from error
    return rows


def load_example_projections(path: Path) -> list[dict[str, Any]]:
    """Load audit fields while dropping multi-megabyte unbounded context copies."""
    retained_keys = (
        "exampleID",
        "chronologicalOrdinal",
        "sessionID",
        "modelFacingDestination",
        "conditioningState",
        "query",
        "target",
        "targetMetadata",
        "episode",
        "targetMask",
    )
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise AuditError(f"expected object at {path}:{line_number}")
                projected = {key: value.get(key) for key in retained_keys}
                context_block_ids = value.get(
                    "contextBlockIDs", value.get("contextEventIDs", [])
                )
                projected["sourceContextBlockCount"] = (
                    len(context_block_ids)
                    if isinstance(context_block_ids, list)
                    else None
                )
                rows.append(projected)
    except (OSError, json.JSONDecodeError) as error:
        raise AuditError(f"cannot read {path}: {error}") from error
    return rows


def target_text(target: dict[str, Any]) -> str:
    parts = []
    for segment in target.get("segments", []):
        if segment.get("type") == "paste":
            parts.append("<|paste|>")
        else:
            parts.append(str(segment.get("content", "")))
    return "".join(parts)


def projection(serialized: str) -> dict[str, Any]:
    try:
        value = json.loads(serialized)
    except json.JSONDecodeError as error:
        raise AuditError(f"invalid model-facing event JSON: {error}") from error
    if not isinstance(value, dict):
        raise AuditError("model-facing event is not an object")
    return value


def classify_read_surface(event: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    surface = event.get("readSurface")
    if not isinstance(surface, dict):
        return "legacy", {}
    selection = surface.get("surfaceSelection")
    if not isinstance(selection, dict):
        selection = {}
    if surface.get("ruleVersion") == "ax-pane-read-v2":
        if selection.get("isV1Fallback") is False:
            return "ax_tree", selection
        return "ax_fallback", selection
    return "legacy", selection


def artifact_path_by_digest(paths: list[Path]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for path in paths:
        result.setdefault(sha256(path), path)
    return result


class AuditStore:
    def __init__(
        self,
        *,
        project: Path,
        corpus: Path,
        packed: Path,
        sample_size: int,
        seed: int,
    ):
        self.project = project
        self.corpus = corpus.resolve()
        self.packed = packed.resolve()
        self.seed = seed
        self.corpus_manifest = load_json(self.corpus / "corpus.json")
        self.packing_manifest = load_json(self.packed / "packing.json")
        self._validate_artifact_digests()
        self.examples = load_example_projections(self.corpus / "examples.jsonl")
        self.events = load_jsonl(self.corpus / "events.jsonl")
        self.blocks = load_jsonl(self.corpus / "context-blocks.jsonl")
        self.plans = load_jsonl(self.packed / "context-plans.jsonl")
        self.example_by_id = self._index(self.examples, "exampleID", "examples")
        self.corpus_event_by_source_id = self._index(
            self.events, "sourceEventID", "corpus events"
        )
        self.block_by_id = self._index(self.blocks, "contextBlockID", "context blocks")
        self.plan_by_id = self._index(self.plans, "exampleID", "context plans")
        self.read_provenance, self.session_read_counts = self._load_read_provenance()
        self.event_read_counts = self._event_read_counts()
        self.context_read_counts, self.example_context_shapes = self._context_read_counts()
        self.target_session_counts = self._target_session_counts()
        self.sample = self._sample_examples(sample_size)
        self.sample_ids = {row["exampleID"] for row in self.sample}

    @staticmethod
    def _index(
        rows: list[dict[str, Any]], key: str, label: str
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            value = row.get(key)
            if not isinstance(value, str) or not value or value in result:
                raise AuditError(f"invalid or duplicate {key} in {label}: {value!r}")
            result[value] = row
        return result

    def _validate_artifact_digests(self) -> None:
        corpus_digests = self.corpus_manifest.get("artifactDigestsSHA256", {})
        for name in ("events.jsonl", "examples.jsonl", "context-blocks.jsonl"):
            path = self.corpus / name
            if not path.is_file() or sha256(path) != corpus_digests.get(name):
                raise AuditError(f"corpus artifact changed: {path}")
        packing_digests = self.packing_manifest.get("artifactDigestsSHA256", {})
        plan_path = self.packed / "context-plans.jsonl"
        if not plan_path.is_file() or sha256(plan_path) != packing_digests.get(
            "context-plans.jsonl"
        ):
            raise AuditError(f"packing artifact changed: {plan_path}")
        source = self.packing_manifest.get("source", {})
        if source.get("sessionID") != self.corpus_manifest.get("sessionID"):
            raise AuditError("packing and corpus identities differ")

    def _load_read_provenance(
        self,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, Counter[str]]]:
        source = self.corpus_manifest.get("source", {})
        micro_path = Path(str(source.get("path", ""))).resolve()
        micro_manifest = load_json(micro_path / "corpus.json")
        causal_candidates = list(
            (self.project / "coupled-data").glob("*-causal-v1[56]*/dataset.json")
        )
        causal_by_digest = artifact_path_by_digest(causal_candidates)
        semantic_candidates = list(
            (self.project / "coupled-data").glob("*-semantic-v1[34]*/events.jsonl")
        )
        semantic_by_digest = artifact_path_by_digest(semantic_candidates)
        result: dict[str, dict[str, Any]] = {}
        sessions: dict[str, Counter[str]] = defaultdict(Counter)
        for session in micro_manifest.get("sources", []):
            causal_digest = session.get("digestsSHA256", {}).get("dataset.json")
            causal_path = causal_by_digest.get(causal_digest)
            if causal_path is None:
                raise AuditError(
                    f"cannot resolve causal source for session {session.get('sessionID')}"
                )
            causal_manifest = load_json(causal_path)
            semantic_digest = (
                causal_manifest.get("source", {})
                .get("digestsSHA256", {})
                .get("events.jsonl")
            )
            semantic_path = semantic_by_digest.get(semantic_digest)
            if semantic_path is None:
                raise AuditError(
                    f"cannot resolve semantic source for session {session.get('sessionID')}"
                )
            for event in load_jsonl(semantic_path):
                if event.get("kind") != "read":
                    continue
                event_id = event.get("eventID")
                if not isinstance(event_id, str) or not event_id:
                    raise AuditError(f"READ has no eventID: {semantic_path}")
                category, selection = classify_read_surface(event)
                surface = event.get("readSurface")
                if not isinstance(surface, dict):
                    surface = {}
                corpus_event = self.corpus_event_by_source_id.get(event_id, {})
                identity = corpus_event.get("readSourceDerivation")
                if not isinstance(identity, dict):
                    identity = {}
                sessions[str(event.get("sessionID"))][category] += 1
                result[event_id] = {
                    "category": category,
                    "label": READ_PROVENANCE[category]["label"],
                    "description": READ_PROVENANCE[category]["description"],
                    "ruleVersion": surface.get("ruleVersion"),
                    "captureScope": event.get("captureScope"),
                    "method": selection.get("method"),
                    "reason": selection.get("reason"),
                    "confidence": selection.get("confidence"),
                    "selectedDepth": selection.get("selectedDepth"),
                    "selectedRole": selection.get("selectedRole"),
                    "selectedSubrole": selection.get("selectedSubrole"),
                    "identityCategory": identity.get("category"),
                    "identityRule": identity.get("rule"),
                    "identityNormalizerVersion": identity.get("normalizerVersion"),
                    "originalApplication": identity.get("originalApplication"),
                    "originalWindowTitle": identity.get("originalWindowTitle"),
                    "modelFacingSource": corpus_event.get("modelFacingReadSource"),
                    "sourceSemanticArtifact": str(semantic_path),
                }
        return result, sessions

    def _event_read_counts(self) -> Counter[str]:
        counts: Counter[str] = Counter()
        for event in self.events:
            if event.get("kind") != "read":
                continue
            category = self.read_provenance.get(
                str(event.get("sourceEventID")), {"category": "unresolved"}
            )["category"]
            counts[category] += 1
        if counts["unresolved"]:
            raise AuditError(
                f"{counts['unresolved']} corpus READs lack immutable provenance"
            )
        return counts

    def _identity_read_counts(self) -> Counter[str]:
        counts: Counter[str] = Counter()
        for event in self.events:
            if event.get("kind") != "read":
                continue
            derivation = event.get("readSourceDerivation")
            category = (
                derivation.get("category")
                if isinstance(derivation, dict)
                else "missing"
            )
            counts[str(category)] += 1
        if counts["missing"]:
            raise AuditError(
                f"{counts['missing']} corpus READs lack v16 source derivation"
            )
        return counts

    def _context_read_counts(
        self,
    ) -> tuple[Counter[str], dict[str, Counter[str]]]:
        counts: Counter[str] = Counter()
        shapes: dict[str, Counter[str]] = {}
        for example in self.examples:
            example_id = example["exampleID"]
            plan = self.plan_by_id.get(example_id)
            if plan is None:
                raise AuditError(f"missing context plan: {example_id}")
            local: Counter[str] = Counter()
            for retained in plan.get("retainedContextBlocks", []):
                block_id = retained.get("contextBlockID")
                block = self.block_by_id.get(block_id)
                if block is None:
                    raise AuditError(f"missing context block: {block_id}")
                serialized = retained.get("serializedOverride") or block["serialized"]
                if projection(serialized).get("kind") != "read":
                    continue
                category = self.read_provenance.get(
                    str(block_id), {"category": "unresolved"}
                )["category"]
                counts[category] += 1
                local[category] += 1
            shapes[example_id] = local
        if counts["unresolved"]:
            raise AuditError(
                f"{counts['unresolved']} retained READ occurrences lack provenance"
            )
        return counts, shapes

    def _session_class(self, session_id: str) -> str:
        counts = self.session_read_counts.get(session_id, Counter())
        if counts["ax_tree"]:
            return "ax_tree_enabled"
        if counts["ax_fallback"]:
            return "ax_fallback_only"
        return "legacy"

    def _target_session_counts(self) -> Counter[str]:
        result: Counter[str] = Counter()
        for example in self.examples:
            result[self._session_class(str(example.get("sessionID")))] += 1
        return result

    def _sample_examples(self, sample_size: int) -> list[dict[str, Any]]:
        if sample_size < 0:
            raise AuditError("sample size cannot be negative")
        if sample_size == 0 or sample_size >= len(self.examples):
            self.sample_method = "complete loss-bearing corpus"
            return sorted(
                self.examples, key=lambda row: int(row["chronologicalOrdinal"])
            )
        sample_size = min(sample_size, len(self.examples))
        self.sample_method = "deterministic stratified random sample"
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for example in self.examples:
            groups[self._session_class(str(example.get("sessionID")))].append(example)
        rng = random.Random(self.seed)
        selected: list[dict[str, Any]] = []
        ax_rows = groups.get("ax_tree_enabled", [])
        ax_quota = min(len(ax_rows), sample_size // 2)
        selected.extend(rng.sample(ax_rows, ax_quota))
        remaining = [row for row in self.examples if row not in selected]
        selected.extend(rng.sample(remaining, sample_size - len(selected)))
        return sorted(selected, key=lambda row: int(row["chronologicalOrdinal"]))

    def _summary(self, example: dict[str, Any]) -> dict[str, Any]:
        destination = example.get("modelFacingDestination", {})
        context = self.example_context_shapes[example["exampleID"]]
        metadata = example.get("targetMetadata", {})
        return {
            "exampleID": example["exampleID"],
            "chronologicalOrdinal": example["chronologicalOrdinal"],
            "application": destination.get("application")
            or example.get("conditioningState", {}).get("destination", {}).get("appName"),
            "surfaceKind": destination.get("surfaceKind"),
            "interactionMode": destination.get("interactionMode"),
            "target": target_text(example.get("target", {})),
            "microWriteCount": metadata.get("microWriteCount"),
            "targetSessionReadCapture": self._session_class(
                str(example.get("sessionID"))
            ),
            "retainedReadCounts": dict(context),
        }

    def meta(self) -> dict[str, Any]:
        examples_with = Counter()
        for counts in self.example_context_shapes.values():
            if not counts:
                examples_with["no_read"] += 1
                continue
            for category in counts:
                examples_with[category] += 1
            if counts["ax_tree"] and (counts["legacy"] or counts["ax_fallback"]):
                examples_with["mixed_ax_and_non_ax"] += 1
            elif counts["ax_tree"]:
                examples_with["only_ax_tree"] += 1
            else:
                examples_with["only_non_ax"] += 1
        return {
            "corpusID": self.corpus_manifest.get("corpusID"),
            "episodeVersion": self.corpus_manifest.get("episodeVersion"),
            "conversionVersion": self.corpus_manifest.get("conversionVersion"),
            "packerVersion": self.packing_manifest.get("packerVersion"),
            "sample": {
                "method": self.sample_method,
                "seed": self.seed,
                "examples": len(self.sample),
                "axTreeSessionExamples": sum(
                    row["targetSessionReadCapture"] == "ax_tree_enabled"
                    for row in self.summaries()
                ),
            },
            "counts": {
                "lossBearingClosedWrites": len(self.examples),
                "corpusReadEvents": dict(self.event_read_counts),
                "corpusReadIdentity": dict(self._identity_read_counts()),
                "retainedReadOccurrencesAcrossExamples": dict(
                    self.context_read_counts
                ),
                "examplesWithReadCapture": dict(examples_with),
                "targetsBySourceSessionReadCapture": dict(
                    self.target_session_counts
                ),
            },
            "readProvenanceLegend": READ_PROVENANCE,
            "paths": {"corpus": str(self.corpus), "packed": str(self.packed)},
        }

    def summaries(self) -> list[dict[str, Any]]:
        return [self._summary(row) for row in self.sample]

    def detail(self, example_id: str) -> dict[str, Any]:
        if example_id not in self.sample_ids:
            raise KeyError(example_id)
        example = self.example_by_id[example_id]
        plan = self.plan_by_id[example_id]
        retained_plan = plan.get("retainedContextBlocks", [])
        if not isinstance(retained_plan, list):
            raise AuditError(f"invalid retained context plan: {example_id}")
        retained = []
        for ordinal, item in enumerate(retained_plan):
            block_id = item["contextBlockID"]
            block = self.block_by_id[block_id]
            serialized = item.get("serializedOverride") or block["serialized"]
            value = projection(serialized)
            provenance = None
            if value.get("kind") == "read":
                provenance = self.read_provenance.get(block_id)
                if provenance is None:
                    raise AuditError(f"READ provenance disappeared: {block_id}")
            retained.append(
                {
                    "ordinal": ordinal,
                    "availableAt": block.get("availableAt"),
                    "contentTruncated": item.get("contentTruncated", False),
                    "projection": value,
                    "readProvenance": provenance,
                }
            )
        input_budget = self.packing_manifest.get("packing", {}).get(
            "inputTokenBudget"
        )
        input_tokens = plan.get("qwenModelInputTokenCount")
        source_count = example.get("sourceContextBlockCount")
        truncated_count = sum(
            item.get("contentTruncated") is True for item in retained_plan
        )
        return {
            "summary": self._summary(example),
            "retainedEvents": retained,
            "conditioningQuery": projection(example["query"]),
            "modelFacingDestination": example.get("modelFacingDestination"),
            "rawConditioningDestination": example.get("conditioningState", {}).get(
                "destination"
            ),
            "target": example.get("target"),
            "targetText": target_text(example.get("target", {})),
            "targetMetadata": example.get("targetMetadata"),
            "episode": example.get("episode"),
            "targetMask": example.get("targetMask"),
            "contextPlan": {
                "taskInstruction": plan.get("taskInstruction"),
                "inputTokenBudget": input_budget,
                "modelInputTokenCount": input_tokens,
                "unusedModelInputTokenBudget": (
                    input_budget - input_tokens
                    if isinstance(input_budget, int)
                    and isinstance(input_tokens, int)
                    else None
                ),
                "sourceContextEventCount": source_count,
                "retainedContextEventCount": len(retained_plan),
                "droppedContextEventCount": (
                    source_count - len(retained_plan)
                    if isinstance(source_count, int)
                    else None
                ),
                "partiallyRetainedContextEventCount": truncated_count,
            },
        }


HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Coupled · training corpus audit</title>
<style>
:root{color-scheme:dark;--bg:#090b0e;--panel:#11151a;--panel2:#171c22;--line:#2a333d;--text:#edf2f7;--muted:#96a3b2;--ax:#4ade80;--fallback:#fbbf24;--legacy:#94a3b8;--write:#60a5fa;--accent:#c084fc}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:13px/1.45 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.shell{display:grid;grid-template-columns:380px minmax(0,1fr);min-height:100vh}.side{height:100vh;position:sticky;top:0;overflow:hidden;border-right:1px solid var(--line);padding:16px;display:grid;grid-template-rows:auto auto auto minmax(0,1fr) auto;gap:10px}.main{padding:20px;min-width:0}.muted{color:var(--muted)}h1{font-size:18px;margin:0 0 4px}.stats{display:grid;grid-template-columns:1fr 1fr;gap:6px}.stat{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:7px}.stat b{font-size:17px;display:block}.filters{display:grid;grid-template-columns:1fr 1fr;gap:7px}.filters input{grid-column:1/-1}select,input,button{font:inherit;color:var(--text);background:var(--panel2);border:1px solid var(--line);border-radius:7px;padding:7px;min-width:0}#items{min-height:0;overflow:auto;padding-right:4px}.item{display:block;width:100%;text-align:left;background:transparent;margin:3px 0;padding:9px}.item.active{background:var(--panel2);border-color:#66788b}.item small{display:block;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.top{display:flex;gap:7px;align-items:center;flex-wrap:wrap}.top h2{margin:0 auto 0 0;font-size:18px}.nav{display:flex;gap:6px}.badge{display:inline-block;border:1px solid var(--line);border-radius:99px;padding:2px 7px;font:11px ui-monospace,SFMono-Regular,Menlo,monospace}.ax_tree,.schema7_ax_selected{color:var(--ax);border-color:color-mix(in srgb,var(--ax) 55%,var(--line))}.ax_fallback,.schema7_pointer_fallback{color:var(--fallback);border-color:color-mix(in srgb,var(--fallback) 55%,var(--line))}.legacy,.legacy_pre_schema7,.unresolved,.schema7_unresolved{color:var(--legacy)}.write{color:var(--write)}.identity{color:var(--accent)}.panel{border:1px solid var(--line);border-radius:9px;background:var(--panel);margin-top:12px;overflow:hidden}.panelhead{display:flex;align-items:center;gap:7px;padding:10px 12px;border-bottom:1px solid var(--line)}.panelbody{padding:12px}.panel h3{margin:0;font-size:13px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.grid .panel{min-width:0}.event summary{display:flex;align-items:center;gap:7px;padding:9px 11px;cursor:pointer}.event .panelbody{border-top:1px solid var(--line)}.auditgrid{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(280px,.7fr);gap:12px}.subhead{font-size:11px;color:var(--muted);letter-spacing:.06em;margin:0 0 6px}pre{margin:0;white-space:pre-wrap;overflow-wrap:anywhere;max-height:520px;overflow:auto;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}.target{font:16px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;overflow-wrap:anywhere}.spacer{flex:1}.legend{display:grid;gap:5px;max-height:95px;overflow:auto}.empty{padding:80px;text-align:center;color:var(--muted)}
@media(max-width:900px){.shell{display:block}.side{position:static;height:auto;overflow:visible;border-right:0;border-bottom:1px solid var(--line);display:block}.stats,.filters{margin-top:10px}#items{max-height:45vh;margin-top:10px}.legend{margin-top:10px}.grid,.auditgrid{grid-template-columns:1fr}}
</style></head>
<body><div class="shell"><aside class="side"><div><h1>Training corpus audit</h1><div id="subtitle" class="muted"></div></div><div id="stats" class="stats"></div><div class="filters"><select id="app"><option value="">All target apps</option></select><select id="capture"><option value="">Any READ evidence</option><option value="ax_tree">Contains proper AX-pane READ</option><option value="ax_fallback">Contains fallback READ</option><option value="legacy">Contains legacy READ</option></select><input id="search" placeholder="Search target or destination"></div><div id="items"></div><div id="legend" class="legend"></div></aside><main class="main" id="main"><div class="empty">Loading exact promoted corpus…</div></main></div>
<script>
const state={meta:null,rows:[],filtered:[],selected:null,requestSerial:0};
const $=id=>document.getElementById(id);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pretty=v=>JSON.stringify(v,null,2);
const compact=o=>Object.fromEntries(Object.entries(o).filter(([,v])=>v!==null&&v!==undefined&&v!==''));
async function get(path){const r=await fetch(path,{cache:'no-store'});if(!r.ok)throw new Error(await r.text());return r.json()}
function label(category){return state.meta.readProvenanceLegend[category]?.label||category}
function targetSessionBadge(row){const c=row.targetSessionReadCapture==='ax_tree_enabled'?'ax_tree':row.targetSessionReadCapture==='ax_fallback_only'?'ax_fallback':'legacy';const text=c==='ax_tree'?'schema-7 target session':c==='ax_fallback'?'fallback-only target session':'legacy target session';return `<span class="badge ${c}">${esc(text)}</span>`}
function hashID(){const value=decodeURIComponent(location.hash.replace(/^#/,''));return value&&state.rows.some(r=>r.exampleID===value)?value:null}
function setHash(id){history.replaceState(null,'','#'+encodeURIComponent(id))}
function apply(){const app=$('app').value,capture=$('capture').value,q=$('search').value.trim().toLowerCase();state.filtered=state.rows.filter(r=>(!app||r.application===app)&&(!capture||(r.retainedReadCounts?.[capture]||0)>0)&&(!q||(r.target+' '+r.application+' '+r.surfaceKind+' '+r.interactionMode).toLowerCase().includes(q)));if($('visibleCount'))$('visibleCount').textContent=state.filtered.length;const requested=hashID();if(requested&&state.filtered.some(r=>r.exampleID===requested))state.selected=requested;if(!state.filtered.some(r=>r.exampleID===state.selected))state.selected=state.filtered[0]?.exampleID||null;drawList();if(state.selected)selectExample(state.selected,false);else $('main').innerHTML='<div class="empty">No matching examples.</div>'}
function drawList(){$('items').innerHTML=state.filtered.map(r=>`<button class="item ${r.exampleID===state.selected?'active':''}" data-id="${esc(r.exampleID)}"><b>#${r.chronologicalOrdinal+1} · ${esc(r.application||'Unresolved target app')}</b><small>${esc(r.target.slice(0,110)||'∅')}</small><small>${r.microWriteCount} micro-WRITE${r.microWriteCount===1?'':'s'} · ${esc(r.surfaceKind||'unknown surface')} · ${esc(r.interactionMode||'unknown mode')}</small></button>`).join('');document.querySelectorAll('.item').forEach(b=>b.onclick=()=>selectExample(b.dataset.id,true))}
function move(delta){const index=state.filtered.findIndex(r=>r.exampleID===state.selected);const next=state.filtered[index+delta];if(next)selectExample(next.exampleID,true)}
function eventText(p){if(p.kind==='read')return p.content||'';return pretty(p)}
function identityAudit(p,prov){if(!prov)return null;return {modelFacingSource:p.source||prov.modelFacingSource||{},originalCapturedSource:compact({application:prov.originalApplication,windowTitle:prov.originalWindowTitle}),sourceDerivation:compact({normalizerVersion:prov.identityNormalizerVersion,category:prov.identityCategory,rule:prov.identityRule}),paneEvidence:compact({ruleVersion:prov.ruleVersion,captureScope:prov.captureScope,method:prov.method,reason:prov.reason,confidence:prov.confidence,selectedDepth:prov.selectedDepth,selectedRole:prov.selectedRole,selectedSubrole:prov.selectedSubrole})}}
function renderEvents(d){$('events').innerHTML=d.retainedEvents.map((e,i)=>{const p=e.projection,prov=e.readProvenance,category=prov?.category||'write';const application=p.source?.application||p.destination?.application||p.application||'Unresolved app';const surface=p.source?.surfaceKind||p.source?.window||p.destination?.surfaceKind||'';const provenanceBadge=p.kind==='read'?`<span class="badge ${category}" title="${esc(prov.description)}">${esc(prov.label)}</span>`:'<span class="badge write">closed WRITE history</span>';const audit=identityAudit(p,prov);return `<details class="panel event" ${i>=d.retainedEvents.length-4?'open':''}><summary><span class="muted">${i+1}/${d.retainedEvents.length}</span><span class="badge ${p.kind==='read'?category:'write'}">${esc((p.kind||'event').toUpperCase())}</span><b>${esc(application)}</b>${surface?`<span class="muted">${esc(surface)}</span>`:''}${provenanceBadge}${e.contentTruncated?'<span class="badge ax_fallback">oldest event truncated</span>':''}<span class="spacer"></span><span class="muted">${esc(e.availableAt||'')}</span></summary><div class="panelbody auditgrid"><div><div class="subhead">${p.kind==='read'?'MODEL-FACING READ CONTENT':'COMPLETE MODEL-FACING WRITE EVENT'}</div><pre>${esc(eventText(p))}</pre></div>${audit?`<div><div class="subhead">READ SOURCE IDENTITY + EVIDENCE</div><pre>${esc(pretty(audit))}</pre></div>`:''}</div></details>`}).join('')}
async function selectExample(id,updateURL){if(!state.filtered.some(r=>r.exampleID===id))return;state.selected=id;if(updateURL||hashID()!==id)setHash(id);drawList();document.querySelector('.item.active')?.scrollIntoView({block:'nearest'});const serial=++state.requestSerial;$('main').innerHTML='<div class="empty">Loading exact packed context…</div>';try{const d=await get('/api/example?id='+encodeURIComponent(id));if(serial!==state.requestSerial)return;const s=d.summary;const readCounts=Object.entries(s.retainedReadCounts||{}).map(([k,v])=>`<span class="badge ${k}">${v} ${esc(label(k))}</span>`).join('');$('main').innerHTML=`<div class="top"><h2>#${s.chronologicalOrdinal+1} · ${esc(s.application)} · closed substantive WRITE</h2><div class="nav"><button id="previous">← Previous</button><button id="next">Next →</button></div>${targetSessionBadge(s)}<span class="badge write">${s.microWriteCount} micro-WRITE${s.microWriteCount===1?'':'s'}</span>${readCounts}</div><section class="panel"><div class="panelhead"><h3>LOSS-BEARING CLOSED TARGET</h3><span class="spacer"></span><span class="badge write">authored content + paste marker + EOS</span></div><div class="panelbody target">${esc(d.targetText)}</div></section><div class="grid"><section class="panel"><div class="panelhead"><h3>MODEL-FACING WRITE DESTINATION</h3></div><div class="panelbody"><pre>${esc(pretty(d.modelFacingDestination))}</pre></div></section><section class="panel"><div class="panelhead"><h3>ORIGINAL PRE-MUTATION DESTINATION</h3></div><div class="panelbody"><pre>${esc(pretty(d.rawConditioningDestination))}</pre></div></section></div><div class="grid"><section class="panel"><div class="panelhead"><h3>EXACT PACKING PLAN</h3></div><div class="panelbody"><pre>${esc(pretty(d.contextPlan))}</pre></div></section><section class="panel"><div class="panelhead"><h3>EPISODE CONSTRUCTION</h3></div><div class="panelbody"><pre>${esc(pretty({targetMetadata:d.targetMetadata,episode:d.episode,targetMask:d.targetMask}))}</pre></div></section></div><section class="panel"><div class="panelhead"><h3>EXACT RETAINED MODEL HISTORY</h3><span class="spacer"></span><span class="muted">The Qwen v7 context plan over causal-v16 READ/WRITE serialization.</span></div><div class="panelbody" id="events"></div></section><section class="panel"><div class="panelhead"><h3>CONDITIONING QUERY</h3></div><div class="panelbody"><pre>${esc(pretty(d.conditioningQuery))}</pre></div></section>`;renderEvents(d);$('previous').onclick=()=>move(-1);$('next').onclick=()=>move(1)}catch(error){if(serial===state.requestSerial)$('main').innerHTML=`<pre>${esc(error.stack||error)}</pre>`}}
async function init(){[state.meta,state.rows]=await Promise.all([get('/api/meta'),get('/api/examples')]);$('subtitle').textContent=`${state.meta.sample.method} · ${state.meta.episodeVersion} · ${state.meta.conversionVersion}`;const c=state.meta.counts,reads=c.corpusReadEvents;$('stats').innerHTML=`<div class="stat"><b id="visibleCount">${state.meta.sample.examples}</b>visible of ${c.lossBearingClosedWrites}</div><div class="stat"><b>${reads.ax_tree||0}</b>proper AX-pane READs</div><div class="stat"><b>${reads.ax_fallback||0}</b>fallback READs</div><div class="stat"><b>${reads.legacy||0}</b>legacy READs</div>`;$('legend').innerHTML=Object.entries(state.meta.readProvenanceLegend).filter(([k])=>k!=='unresolved').map(([k,v])=>`<div><span class="badge ${k}">${esc(v.label)}</span> <span class="muted">${esc(v.description)}</span></div>`).join('');[...new Set(state.rows.map(r=>r.application).filter(Boolean))].sort().forEach(v=>$('app').insertAdjacentHTML('beforeend',`<option>${esc(v)}</option>`));$('app').onchange=$('capture').onchange=$('search').oninput=apply;state.selected=hashID();apply();addEventListener('hashchange',()=>{const id=hashID();if(id&&id!==state.selected){state.selected=id;apply()}});addEventListener('keydown',event=>{if(event.target.matches('input,select,button'))return;if(event.key==='ArrowLeft')move(-1);if(event.key==='ArrowRight')move(1)})}
init().catch(error=>$('main').innerHTML=`<pre>${esc(error.stack||error)}</pre>`)
</script></body></html>'''


def handler(store: AuditStore) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def send(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, value: Any) -> None:
            self.send(
                HTTPStatus.OK,
                "application/json; charset=utf-8",
                json.dumps(value, ensure_ascii=False).encode(),
            )

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            try:
                if parsed.path in {"/", "/index.html"}:
                    self.send(HTTPStatus.OK, "text/html; charset=utf-8", HTML.encode())
                elif parsed.path == "/api/meta":
                    self.send_json(store.meta())
                elif parsed.path == "/api/examples":
                    self.send_json(store.summaries())
                elif parsed.path == "/api/example":
                    example_id = query.get("id", [""])[0]
                    self.send_json(store.detail(example_id))
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            except KeyError:
                self.send_error(HTTPStatus.NOT_FOUND, "unknown sampled example")
            except (AuditError, ValueError) as error:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

        def log_message(self, format: str, *values: object) -> None:
            return

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--packed", required=True, type=Path)
    parser.add_argument(
        "--sample-size", type=int, default=40,
        help="deterministic sample size; use 0 to review every example",
    )
    parser.add_argument("--seed", type=int, default=1701)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    project = Path(__file__).resolve().parent.parent
    store = AuditStore(
        project=project,
        corpus=arguments.corpus,
        packed=arguments.packed,
        sample_size=arguments.sample_size,
        seed=arguments.seed,
    )
    meta = store.meta()
    if arguments.check:
        print(json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    server = ThreadingHTTPServer(
        (arguments.host, arguments.port), handler(store)
    )
    url = f"http://{arguments.host}:{server.server_address[1]}/"
    print(f"Phase 1 corpus audit: {url}", flush=True)
    print(
        f"Loaded {len(store.examples)} loss-bearing closed WRITEs; "
        f"showing {len(store.sample)} {store.sample_method} cases.",
        flush=True,
    )
    if not arguments.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

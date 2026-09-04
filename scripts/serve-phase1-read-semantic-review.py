#!/usr/bin/env python3
"""Serve a shadow semantic READ projection for manual review."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import urllib.parse
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class ReviewError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ReviewError(f"expected object: {path}")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ReviewError(f"expected object at {path}:{line_number}")
            rows.append(value)
    return rows


def source_ids(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(value) for value in row.get("sourceRecordIDs", []) if value)


def numeric(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def normalized_point(
    x: Any, y: Any, bounds: dict[str, Any] | None,
) -> dict[str, float] | None:
    if not isinstance(bounds, dict):
        return None
    values = [numeric(value) for value in (
        x, y, bounds.get("x"), bounds.get("y"),
        bounds.get("width"), bounds.get("height"),
    )]
    if any(value is None for value in values):
        return None
    point_x, point_y, left, top, width, height = values
    if width <= 0 or height <= 0:
        return None
    return {
        "x": (point_x - left) / width,
        "y": (point_y - top) / height,
    }


def top_origin_region(value: Any) -> dict[str, float] | None:
    """Convert a Vision bottom-origin normalized ROI to CSS top-origin geometry."""
    if not isinstance(value, dict):
        return None
    values = [numeric(value.get(key)) for key in ("x", "y", "width", "height")]
    if any(item is None for item in values):
        return None
    x, y, width, height = values
    return {"x": x, "y": 1 - y - height, "width": width, "height": height}


class ReviewStore:
    def __init__(
        self, baseline: Path, candidate: Path, surfaces: Path, source: Path,
        compiled: Path | None = None, packed: Path | None = None,
    ):
        self.baseline = baseline.resolve()
        self.candidate = candidate.resolve()
        self.surfaces = surfaces.resolve()
        self.source = source.resolve()
        self.compiled = compiled.resolve() if compiled else None
        self.packed = packed.resolve() if packed else None
        if (self.compiled is None) != (self.packed is None):
            raise ReviewError("--compiled and --packed must be supplied together")
        base_manifest = load_json(self.baseline / "reduction.json")
        candidate_manifest = load_json(self.candidate / "reduction.json")
        if base_manifest.get("reducerVersion") != "phase1-semantic-v14":
            raise ReviewError("baseline must be phase1-semantic-v14")
        candidate_version = candidate_manifest.get("reducerVersion")
        if candidate_version not in {
            "phase1-semantic-v16", "phase1-semantic-v17",
            "phase1-semantic-v18",
        }:
            raise ReviewError("candidate must be phase1-semantic-v16, v17, or v18")
        self.candidate_version = str(candidate_version)
        for directory, manifest in (
            (self.baseline, base_manifest), (self.candidate, candidate_manifest)
        ):
            expected = manifest.get("artifacts", {}).get("digestsSHA256", {})
            for name in ("events.jsonl", "unresolved.jsonl"):
                if sha256(directory / name) != expected.get(name):
                    raise ReviewError(f"artifact digest differs: {directory}/{name}")
        base_raw = base_manifest.get("source", {}).get("digestsSHA256", {}).get("raw.jsonl")
        candidate_raw = candidate_manifest.get("source", {}).get("digestsSHA256", {}).get("raw.jsonl")
        if not base_raw or base_raw != candidate_raw or sha256(self.source / "raw.jsonl") != base_raw:
            raise ReviewError("baseline, candidate, and source raw journal differ")

        surface_manifest = load_json(self.surfaces / "read-surface-evidence.json")
        evidence_path = self.surfaces / "read-surfaces.jsonl"
        surface_hash = surface_manifest.get("artifacts", {}).get("digestsSHA256", {}).get(
            "read-surfaces.jsonl"
        )
        if sha256(evidence_path) != surface_hash:
            raise ReviewError("READ-surface evidence digest differs")
        if candidate_manifest.get("source", {}).get("readSurfaceEvidence", {}).get(
            "readSurfacesSHA256"
        ) != surface_hash:
            raise ReviewError("candidate is not bound to supplied surface evidence")
        evidence = {
            str(row["sourceRecordID"]): row for row in load_jsonl(evidence_path)
        }
        unresolved_surface_rows = load_jsonl(self.surfaces / "unresolved.jsonl")
        raw_rows = load_jsonl(self.source / "raw.jsonl")
        raw_by_id = {
            str(row["recordID"]): row for row in raw_rows if row.get("recordID")
        }
        frame_by_ocr = {
            str(row["recordID"]): str(row["sourceFrameRecordID"])
            for row in raw_rows
            if row.get("recordType") == "visual_ocr_observation"
            and row.get("recordID") and row.get("sourceFrameRecordID")
        }

        base_read_rows = [
            row for row in load_jsonl(self.baseline / "events.jsonl")
            if row.get("kind") == "read"
        ]
        base_reads = {source_ids(row): row for row in base_read_rows}
        base_reads_by_source: dict[str, list[dict[str, Any]]] = {}
        for base_row in base_read_rows:
            for source_id in source_ids(base_row):
                base_reads_by_source.setdefault(source_id, []).append(base_row)
        candidate_reads = [
            row for row in load_jsonl(self.candidate / "events.jsonl")
            if row.get("kind") == "read"
        ]
        candidate_by_id = {str(row["eventID"]): row for row in candidate_reads}
        model_occurrences: dict[str, list[dict[str, Any]]] = {}
        packing_summary: dict[str, Any] | None = None
        if self.compiled is not None and self.packed is not None:
            packing_manifest = load_json(self.packed / "packing.json")
            if packing_manifest.get("packerVersion") != "phase1-token-pack-v8":
                raise ReviewError("packed review requires phase1-token-pack-v8")
            if packing_manifest.get("packing", {}).get(
                "readNoveltyRendering", {}
            ).get("status") != "shadow_opt_in":
                raise ReviewError("packed review requires the shadow READ renderer")
            compiled_events_path = self.compiled / "events.jsonl"
            expected_compiled_hash = packing_manifest.get("source", {}).get(
                "digestsSHA256", {}
            ).get("events.jsonl")
            if sha256(compiled_events_path) != expected_compiled_hash:
                raise ReviewError("packed artifact is not bound to supplied compiled events")
            compiled_events = {
                str(item["sourceEventID"]): item
                for item in load_jsonl(compiled_events_path)
            }
            plan_path = self.packed / "context-plans.jsonl"
            expected_plan_hash = packing_manifest.get(
                "artifactDigestsSHA256", {}
            ).get("context-plans.jsonl")
            if sha256(plan_path) != expected_plan_hash:
                raise ReviewError("packed context-plan digest differs")
            for plan in load_jsonl(plan_path):
                for block in plan.get("retainedContextBlocks", []):
                    rendering = block.get("readRendering")
                    if not isinstance(rendering, dict):
                        continue
                    event_id = str(block.get("contextBlockID", ""))
                    compiled_event = compiled_events.get(event_id)
                    if compiled_event is None:
                        raise ReviewError(f"packed block lacks compiled event: {event_id}")
                    serialized = block.get("serializedOverride")
                    if serialized is None:
                        serialized = compiled_event.get("serialized")
                    try:
                        model_payload = json.loads(serialized)
                    except (TypeError, json.JSONDecodeError) as error:
                        raise ReviewError(f"invalid packed READ projection: {event_id}") from error
                    model_occurrences.setdefault(event_id, []).append({
                        "exampleID": plan.get("exampleID"),
                        "targetEventID": plan.get("targetEventID"),
                        "decision": rendering.get("decision"),
                        "dependencyAvailable": rendering.get("dependencyAvailable"),
                        "dependsOnEventID": rendering.get("dependsOnEventID"),
                        "contentTruncated": block.get("contentTruncated"),
                        "modelFacingContent": model_payload.get("content"),
                        "serializedSHA256": block.get("serializedSHA256"),
                    })
            packing_summary = {
                "packerVersion": packing_manifest.get("packerVersion"),
                "counts": packing_manifest.get("counts", {}).get(
                    "readRendering", {}
                ),
                "tokensRemoved": packing_manifest.get("counts", {}).get(
                    "modelInputTokensRemovedByReadNoveltyRendering", 0
                ),
            }
        rows: list[dict[str, Any]] = []
        self.images: dict[str, tuple[Path, str]] = {}
        for row in candidate_reads:
            ids = source_ids(row)
            baseline_candidates = {
                str(item.get("eventID")): item
                for source_id in ids
                for item in base_reads_by_source.get(source_id, [])
            }
            baseline_row = base_reads.get(ids) or (
                max(
                    baseline_candidates.values(),
                    key=lambda item: int(item.get("sequence", 0)),
                )
                if baseline_candidates else None
            )
            baseline_rows = sorted(
                baseline_candidates.values(),
                key=lambda item: int(item.get("sequence", 0)),
            )
            surface = next((evidence[item] for item in reversed(ids) if item in evidence), {})
            observed = str(surface.get("content", row.get("content", "")))
            semantic = str(row.get("content", ""))
            details = row.get("reduction", {}).get("semanticReadContent", {})
            removed = details.get("removedLines", [])
            novelty = row.get("readNovelty", {})
            predecessor = candidate_by_id.get(str(novelty.get("dependsOnEventID", "")))
            image_keys: list[dict[str, Any]] = []
            seen_image_hashes: set[str] = set()
            for source_id in ids:
                raw_id = frame_by_ocr.get(source_id, source_id)
                raw = raw_by_id.get(raw_id, {})
                relative = raw.get("screenshotRelativePath")
                expected_hash = raw.get("screenshotSHA256")
                if isinstance(relative, str) and isinstance(expected_hash, str):
                    path = (self.source / relative).resolve()
                    if (
                        self.source in path.parents and path.is_file()
                        and expected_hash not in seen_image_hashes
                    ):
                        seen_image_hashes.add(expected_hash)
                        image_key = f"{row['eventID']}:{len(image_keys)}"
                        self.images[image_key] = (path, expected_hash)
                        image_keys.append({
                            "key": image_key,
                            "sourceRecordID": source_id,
                            "capturedAt": raw.get("capturedAt"),
                            "recordType": raw.get("recordType"),
                            "pixelWidth": raw.get("screenshotPixelWidth"),
                            "pixelHeight": raw.get("screenshotPixelHeight"),
                        })
            image_by_source = {
                str(item.get("sourceRecordID")): item for item in image_keys
            }
            pane_evidence: list[dict[str, Any]] = []
            for source_id in ids:
                surface_evidence = evidence.get(source_id)
                if not isinstance(surface_evidence, dict):
                    continue
                selection = surface_evidence.get("surfaceSelection")
                if not isinstance(selection, dict):
                    selection = {}
                ocr = raw_by_id.get(source_id, {})
                frame = raw_by_id.get(frame_by_ocr.get(source_id, ""), {})
                point_record = frame if frame else ocr
                bounds = ocr.get("windowBounds")
                if not isinstance(bounds, dict):
                    frame_surface = frame.get("surface") if frame else None
                    bounds = (
                        frame_surface.get("windowBounds")
                        if isinstance(frame_surface, dict) else None
                    )
                image = image_by_source.get(source_id, {})
                pane_evidence.append({
                    "sourceRecordID": source_id,
                    "imageKey": image.get("key"),
                    "recordType": ocr.get("recordType"),
                    "pane": top_origin_region(selection.get("regionOfInterest")),
                    "comparison": top_origin_region(
                        selection.get("comparisonRegionOfInterest")
                    ),
                    "semanticPoint": (
                        selection.get("semanticPointNormalizedTop")
                        if isinstance(
                            selection.get("semanticPointNormalizedTop"), dict
                        ) else normalized_point(
                            ocr.get("x", point_record.get("x")),
                            ocr.get("y", point_record.get("y")),
                            bounds,
                        )
                    ),
                    "rawPointer": normalized_point(
                        point_record.get("rawInteractionX", ocr.get("x")),
                        point_record.get("rawInteractionY", ocr.get("y")),
                        bounds,
                    ),
                    "semanticPointReason": (
                        ocr.get("semanticContentPointReason")
                        or frame.get("semanticContentPointReason")
                    ),
                    "method": selection.get("method"),
                    "confidence": selection.get("confidence"),
                    "reason": selection.get("reason"),
                    "ruleVersion": (
                        selection.get("ruleVersion")
                        or surface_evidence.get("ruleVersion")
                    ),
                    "selectedDepth": selection.get("selectedDepth"),
                    "selectedRole": selection.get("selectedRole"),
                    "selectedSubrole": selection.get("selectedSubrole"),
                    "isV1Fallback": selection.get("isV1Fallback"),
                    "regionOfInterest": selection.get("regionOfInterest"),
                    "comparisonRegionOfInterest": selection.get(
                        "comparisonRegionOfInterest"
                    ),
                    "recovery": selection.get("recovery"),
                    "canonicalization": selection.get("canonicalization"),
                    "resolution": selection.get("resolution"),
                })
            scaffolding_changed = bool(removed)
            novelty_decision = str(novelty.get("decision", "missing"))
            changed = scaffolding_changed or novelty_decision in {
                "emit_new_content", "suppress_no_new_content",
                "suppress_nonsemantic_microglyph",
            }
            occurrences = model_occurrences.get(str(row["eventID"]), [])
            rendering_counts = Counter(
                str(item.get("decision", "missing")) for item in occurrences
            )
            model_changed = any(
                item.get("decision") in {
                    "render_novel_content", "render_empty_adjacent_repeat"
                }
                for item in occurrences
            )
            model_fallback = any(
                item.get("decision") in {
                    "render_complete_dependency_unavailable",
                    "render_complete_uncertain_microglyph",
                    "retain_existing_truncated_state",
                    "retain_nonread_model_projection",
                }
                for item in occurrences
            )
            rows.append({
                "id": str(row["eventID"]),
                "changed": changed,
                "capturedAt": row.get("capturedAt") or row.get("availableAt"),
                "application": row.get("appName") or row.get("bundleIdentifier"),
                "windowTitle": row.get("windowTitle"),
                # Keep review labels stable when a candidate reducer removes an
                # earlier non-event. Users refer to these source-aligned IDs
                # while comparing successive shadow projections.
                "sequence": (
                    baseline_row.get("sequence") if baseline_row
                    else row.get("sequence")
                ),
                "candidateSequence": row.get("sequence"),
                "baselineSequence": baseline_row.get("sequence") if baseline_row else None,
                "baselineSequences": [
                    item.get("sequence") for item in baseline_rows
                ],
                "baselineReads": [
                    {
                        "eventID": item.get("eventID"),
                        "sequence": item.get("sequence"),
                        "capturedAt": item.get("capturedAt"),
                        "content": item.get("content", ""),
                    }
                    for item in baseline_rows
                ],
                "sourceRecordIDs": list(ids),
                "priorCompleteSemantic": predecessor.get("content", "") if predecessor else "",
                "observedOCR": observed,
                "removedScaffolding": removed,
                "completeSemantic": semantic,
                "novelContent": novelty.get("content", ""),
                "novelty": novelty,
                "semanticDetails": details,
                "baselineContent": baseline_row.get("content", "") if baseline_row else "",
                "imageKey": image_keys[-1]["key"] if image_keys else "",
                "images": image_keys,
                "paneEvidence": pane_evidence,
                "observationReconciliation": row.get("reduction", {}).get(
                    "observationReconciliation"
                ),
                "dynamicVisualConsolidation": row.get("reduction", {}).get(
                    "dynamicVisualConsolidation"
                ),
                "modelOccurrences": occurrences,
                "modelRenderingCounts": dict(sorted(rendering_counts.items())),
                "modelChanged": model_changed,
                "modelFallback": model_fallback,
                "unresolved": False,
                "unresolvedReason": None,
            })

        represented_source_ids = {
            source_id for row in rows for source_id in row["sourceRecordIDs"]
        }
        for unresolved in unresolved_surface_rows:
            source_id = str(unresolved.get("sourceRecordID") or "")
            if not source_id or source_id in represented_source_ids:
                continue
            raw_id = frame_by_ocr.get(source_id, source_id)
            raw = raw_by_id.get(source_id, {})
            frame = raw_by_id.get(raw_id, {}) if raw_id != source_id else {}
            screenshot_record = frame or raw
            relative = screenshot_record.get("screenshotRelativePath")
            expected_hash = screenshot_record.get("screenshotSHA256")
            image_keys: list[dict[str, Any]] = []
            if isinstance(relative, str) and isinstance(expected_hash, str):
                path = (self.source / relative).resolve()
                if self.source in path.parents and path.is_file():
                    image_key = f"unresolved:{source_id}:0"
                    self.images[image_key] = (path, expected_hash)
                    image_keys.append({
                        "key": image_key,
                        "sourceRecordID": source_id,
                        "capturedAt": raw.get("capturedAt")
                            or screenshot_record.get("capturedAt"),
                        "recordType": raw.get("recordType"),
                        "pixelWidth": screenshot_record.get(
                            "screenshotPixelWidth"
                        ),
                        "pixelHeight": screenshot_record.get(
                            "screenshotPixelHeight"
                        ),
                    })
            selection = unresolved.get("surfaceSelection")
            if not isinstance(selection, dict):
                selection = {}
            bounds = raw.get("windowBounds")
            if not isinstance(bounds, dict):
                surface = screenshot_record.get("surface")
                bounds = (
                    surface.get("windowBounds")
                    if isinstance(surface, dict) else None
                )
            pane_evidence = [{
                "sourceRecordID": source_id,
                "imageKey": image_keys[0]["key"] if image_keys else None,
                "recordType": raw.get("recordType"),
                "pane": top_origin_region(selection.get("regionOfInterest")),
                "comparison": top_origin_region(
                    selection.get("comparisonRegionOfInterest")
                ),
                "semanticPoint": (
                    selection.get("semanticPointNormalizedTop")
                    if isinstance(
                        selection.get("semanticPointNormalizedTop"), dict
                    ) else normalized_point(
                        raw.get("x", screenshot_record.get("x")),
                        raw.get("y", screenshot_record.get("y")),
                        bounds,
                    )
                ),
                "rawPointer": normalized_point(
                    screenshot_record.get("rawInteractionX", raw.get("x")),
                    screenshot_record.get("rawInteractionY", raw.get("y")),
                    bounds,
                ),
                "semanticPointReason": raw.get("semanticContentPointReason")
                    or screenshot_record.get("semanticContentPointReason"),
                "method": selection.get("method") or "unresolved",
                "confidence": selection.get("confidence") or "unresolved",
                "reason": unresolved.get("reason"),
                "ruleVersion": unresolved.get("ruleVersion"),
                "selectedDepth": selection.get("selectedDepth"),
                "selectedRole": selection.get("selectedRole"),
                "selectedSubrole": selection.get("selectedSubrole"),
                "isV1Fallback": False,
                "regionOfInterest": selection.get("regionOfInterest"),
                "comparisonRegionOfInterest": selection.get(
                    "comparisonRegionOfInterest"
                ),
                "recovery": selection.get("recovery"),
                "resolution": "unresolved",
            }]
            baseline_candidates = base_reads_by_source.get(source_id, [])
            baseline_row = max(
                baseline_candidates,
                key=lambda item: int(item.get("sequence", 0)),
            ) if baseline_candidates else None
            reason = str(unresolved.get("reason") or "unresolved")
            rows.append({
                "id": f"unresolved:{source_id}",
                "changed": True,
                "capturedAt": raw.get("capturedAt")
                    or screenshot_record.get("capturedAt"),
                "application": raw.get("appName")
                    or raw.get("bundleIdentifier"),
                "windowTitle": raw.get("windowTitle"),
                "sequence": baseline_row.get("sequence") if baseline_row
                    else f"raw {unresolved.get('sourceRawLine')}",
                "candidateSequence": None,
                "baselineSequence": baseline_row.get("sequence")
                    if baseline_row else None,
                "baselineSequences": [baseline_row.get("sequence")]
                    if baseline_row else [],
                "baselineReads": [{
                    "eventID": baseline_row.get("eventID"),
                    "sequence": baseline_row.get("sequence"),
                    "capturedAt": baseline_row.get("capturedAt"),
                    "content": baseline_row.get("content", ""),
                }] if baseline_row else [],
                "sourceRecordIDs": [source_id],
                "priorCompleteSemantic": "",
                "observedOCR": str(raw.get("content") or ""),
                "removedScaffolding": [],
                "completeSemantic": "",
                "novelContent": "",
                "novelty": {"decision": "unresolved_pane"},
                "semanticDetails": {},
                "baselineContent": baseline_row.get("content", "")
                    if baseline_row else str(raw.get("content") or ""),
                "imageKey": image_keys[0]["key"] if image_keys else "",
                "images": image_keys,
                "paneEvidence": pane_evidence,
                "observationReconciliation": None,
                "dynamicVisualConsolidation": None,
                "modelOccurrences": [],
                "modelRenderingCounts": {},
                "modelChanged": False,
                "modelFallback": False,
                "unresolved": True,
                "unresolvedReason": reason,
            })
        rows.sort(key=lambda row: (str(row.get("capturedAt") or ""), row["id"]))
        self.rows = rows
        self.by_id = {row["id"]: row for row in rows}
        decisions = Counter(str(row["novelty"].get("decision", "missing")) for row in rows)
        pane_methods = Counter(
            str(item.get("method") or "missing")
            for row in rows for item in row.get("paneEvidence", [])
        )
        self.summary = {
            "status": "semantic_read_shadow_review_only_not_training_authority",
            "baselineVersion": "phase1-semantic-v14",
            "candidateVersion": self.candidate_version,
            "sourceRawSHA256": base_raw,
            "readCount": len(candidate_reads),
            "reviewObservationCount": len(rows),
            "changedReadCount": sum(
                bool(row["changed"]) and not bool(row.get("unresolved"))
                for row in rows
            ),
            "scaffoldingChangedReadCount": sum(bool(row["removedScaffolding"]) for row in rows),
            "noveltyDecisions": dict(sorted(decisions.items())),
            "paneSelectionMethods": dict(sorted(pane_methods.items())),
            "packing": packing_summary,
            "unresolvedPaneObservationCount": sum(
                bool(row.get("unresolved")) for row in rows
            ),
        }

    def index(self) -> list[dict[str, Any]]:
        keys = (
            "id", "changed", "capturedAt", "application", "windowTitle",
            "sequence", "modelChanged", "modelFallback", "modelRenderingCounts",
            "unresolved", "unresolvedReason", "baselineSequences",
        )
        result = []
        for row in self.rows:
            value = {key: row.get(key) for key in keys}
            removed = row.get("removedScaffolding", [])
            value["scaffoldingCount"] = len(removed)
            value["genericScaffoldingCount"] = sum(
                item.get("reason") in {
                    "stable_peripheral_interface_text",
                    "stable_same_window_bottom_interface_text",
                    "stable_application_bottom_interface_text",
                    "stable_session_interface_text",
                }
                for item in removed
            )
            value["noveltyDecision"] = row.get("novelty", {}).get("decision", "missing")
            panes = row.get("paneEvidence", [])
            selected_pane = panes[-1] if panes else {}
            value["paneMethod"] = selected_pane.get("method") or "missing"
            value["paneConfidence"] = selected_pane.get("confidence") or "missing"
            value["paneReason"] = selected_pane.get("reason") or "missing"
            value["paneCanonicalized"] = any(
                item.get("method") == "canonicalized_prior_outer_ax_pane"
                for item in panes
            )
            value["sourceRecordIDs"] = row.get("sourceRecordIDs", [])
            result.append(value)
        return result


HTML = r'''<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Coupled · READ before/after review</title>
<style>
:root{color-scheme:light dark;font:13px/1.45 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:Canvas;color:CanvasText}
*{box-sizing:border-box}body{margin:0}.shell{display:grid;grid-template-columns:330px minmax(0,1fr);min-height:100vh}
.side{border-right:1px solid color-mix(in srgb,CanvasText 18%,transparent);padding:16px;position:sticky;top:0;height:100vh;overflow:auto}
.main{padding:22px;min-width:0}.muted{opacity:.62}.stats{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0}
.tag{padding:3px 7px;border-radius:999px;background:color-mix(in srgb,CanvasText 8%,Canvas)}
select,input,button{font:inherit;color:CanvasText;background:Canvas;border:1px solid color-mix(in srgb,CanvasText 22%,transparent);border-radius:7px;padding:7px}
.filters{display:grid;gap:7px;margin:12px 0}.items{display:grid;gap:4px}.item{text-align:left;background:transparent}
.item.active{border-color:Highlight;background:color-mix(in srgb,Highlight 18%,Canvas)}
.item small{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;opacity:.65}
.top{display:flex;align-items:center;gap:7px;flex-wrap:wrap}.top h1{font-size:21px;margin:0 auto 0 0}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:14px}
.panel{border:1px solid color-mix(in srgb,CanvasText 16%,transparent);border-radius:9px;min-width:0;overflow:hidden}
.panel h2{font-size:14px;margin:0;padding:10px 12px;border-bottom:1px solid color-mix(in srgb,CanvasText 14%,transparent)}
pre{margin:0;padding:12px;white-space:pre-wrap;overflow-wrap:anywhere;max-height:540px;overflow:auto;font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;background:color-mix(in srgb,CanvasText 4%,Canvas)}
img{display:block;max-width:100%;max-height:560px;margin:auto}.wide{grid-column:1/-1}.changed{color:#c06400}.empty{padding:70px;text-align:center;opacity:.6}
.shot+.shot{border-top:1px solid color-mix(in srgb,CanvasText 12%,transparent)}.shotmeta{padding:7px 10px}
.shotstage{position:relative;width:100%;margin:auto;line-height:0;background:#050607;overflow:hidden}
.shotstage img{display:block;width:100%;height:auto;max-height:none;margin:0}
.overlay{position:absolute;pointer-events:none;z-index:4}
.overlay.pane{border:3px solid #22c55e;background:rgba(34,197,94,.035)}
.overlay.comparison{border:2px dashed #38bdf8;background:rgba(56,189,248,.025);z-index:5}
.point{position:absolute;width:13px;height:13px;border-radius:50%;transform:translate(-50%,-50%);pointer-events:none;z-index:8}
.point.semantic{background:#fde047;border:2px solid #111;box-shadow:0 0 0 2px #fde047}
.point.raw{background:transparent;border:2px solid #fb923c;box-shadow:0 0 0 1px #111;z-index:7}
.paneinfo{padding:9px 10px;border-top:1px solid color-mix(in srgb,CanvasText 12%,transparent);font:11px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}
.paneinfo .primary{font-weight:650}.paneinfo .fallback{color:#fb923c}.paneinfo .high{color:#22c55e}.paneinfo .unresolved{color:#ef4444}
.legend{display:flex;gap:13px;flex-wrap:wrap;padding:7px 10px;border-top:1px solid color-mix(in srgb,CanvasText 12%,transparent);font-size:11px}
.legend i{display:inline-block;width:14px;height:9px;margin-right:5px;vertical-align:middle}
.legend .lpane{border:2px solid #22c55e}.legend .lcomparison{border:2px dashed #38bdf8}
.legend .lsemantic{width:9px;height:9px;border-radius:50%;background:#fde047}.legend .lraw{width:9px;height:9px;border-radius:50%;border:2px solid #fb923c}
@media(max-width:900px){.shell{display:block}.side{position:static;height:auto;border-right:0;border-bottom:1px solid color-mix(in srgb,CanvasText 18%,transparent)}.grid{grid-template-columns:1fr}.wide{grid-column:auto}}
</style>
<div class="shell">
  <aside class="side">
    <b>READ before/after review</b>
    <div id="meta" class="muted"></div>
    <div id="stats" class="stats"></div>
    <div class="filters">
      <select id="scope">
        <option value="canonicalized">Pane-v7 canonicalizations</option>
        <option value="changed">Changed READs</option>
        <option value="model-changed">Adjacent overlap removed</option>
        <option value="scaffolding">Interface text removed</option>
        <option value="unresolved">Unresolved / excluded panes</option>
        <option value="">All READs</option>
      </select>
      <select id="app"><option value="">All applications</option></select>
      <select id="pane"><option value="">All pane-selection methods</option></select>
      <input id="search" placeholder="Search #, window, or app">
    </div>
    <div id="items" class="items"></div>
  </aside>
  <main id="main" class="main"></main>
</div>
<script>
const $=x=>document.getElementById(x);
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
let rows=[],filtered=[],index=0;
function inScope(r,s){
  if(!s)return true;
  if(s==='canonicalized')return r.paneCanonicalized;
  if(s==='model-changed')return r.modelChanged;
  if(s==='changed')return r.changed||r.modelChanged;
  if(s==='scaffolding')return r.scaffoldingCount>0;
  if(s==='unresolved')return r.unresolved;
  return false;
}
function currentRead(r){
  if(r.unresolved)return `[Excluded from semantic READ: ${r.unresolvedReason}]`;
  if(['suppress_no_new_content','suppress_nonsemantic_microglyph'].includes(r.novelty.decision))return '[No new READ text]';
  return r.novelContent||r.completeSemantic||'[No new READ text]';
}
function outcomeLabel(r){
  if(r.unresolved)return `Excluded · ${r.unresolvedReason}`;
  if(['suppress_no_new_content','suppress_nonsemantic_microglyph'].includes(r.novelty.decision))return 'No newly available text';
  if(r.novelty.decision==='emit_new_content'){
    const n=r.novelty.lineAlignment?.matchedLineCount;
    return n?`New text retained · ${n} repeated line${n===1?'':'s'} removed`:'New text retained · repeated text removed';
  }
  if(r.novelty.decision==='retain_full_uncertain')return 'Full READ retained · comparison uncertain';
  return 'Full READ retained · new surface or boundary';
}
function pct(v){return (Math.max(-.02,Math.min(1.02,Number(v)||0))*100).toFixed(4)+'%'}
function rect(value,kind){
  if(!value)return '';
  return `<div class="overlay ${kind}" style="left:${pct(value.x)};top:${pct(value.y)};width:${pct(value.width)};height:${pct(value.height)}"></div>`;
}
function point(value,kind,title){
  if(!value)return '';
  return `<div class="point ${kind}" title="${esc(title)}" style="left:${pct(value.x)};top:${pct(value.y)}"></div>`;
}
function samePoint(a,b){return a&&b&&Math.abs(a.x-b.x)<.001&&Math.abs(a.y-b.y)<.001}
function paneSummary(p){
  if(!p)return '<div class="paneinfo fallback">No pane-selection evidence for this sensor record.</div>';
  const role=[p.selectedRole,p.selectedSubrole].filter(Boolean).join(' / ')||'none';
  const cssClass=p.isV1Fallback?'fallback':(p.confidence==='high'?'high':'');
  const pointReason=p.semanticPointReason?` · anchor ${esc(p.semanticPointReason)}`:'';
  const resolutionClass=p.resolution==='unresolved'?'unresolved':cssClass;
  const recovery=p.recovery?.sourceRecordID?` · recovered from ${esc(p.recovery.sourceRecordID)}`:'';
  const canonical=p.canonicalization?.sourceRecordID?` · canonical outer pane from ${esc(p.canonicalization.sourceRecordID)} after ${esc(p.canonicalization.intervalSeconds)}s`:'';
  return `<div class="paneinfo"><div class="primary ${resolutionClass}">${esc(p.ruleVersion)} · ${esc(p.method||'unknown')} · ${esc(p.confidence||'unknown')}</div><div>${esc(p.reason||'no reason')} · AX ${esc(role)}${p.selectedDepth==null?'':` · depth ${esc(p.selectedDepth)}`}${pointReason}${recovery}${canonical}</div><div class="muted">source ${esc(p.sourceRecordID)} · ROI ${esc(JSON.stringify(p.regionOfInterest||null))}</div></div>`;
}
function imagePanels(r){
  const values=r.images||[],panes=r.paneEvidence||[];
  if(!values.length)return '';
  return `<section class="panel wide"><h2>Raw screenshot${values.length===1?'':'s'} · exact pane selection and pointer evidence</h2>${values.map((item,i)=>{
    const p=panes.find(value=>value.imageKey===item.key)||panes.find(value=>value.sourceRecordID===item.sourceRecordID);
    const raw=p?.rawPointer,semantic=p?.semanticPoint;
    const rawMarker=raw&&!samePoint(raw,semantic)?point(raw,'raw','Raw physical pointer'):'';
    return `<div class="shot"><div class="muted shotmeta">${i+1}/${values.length} · ${esc(item.capturedAt)} · ${esc(item.recordType)} · ${esc(item.pixelWidth)}×${esc(item.pixelHeight)}</div><div class="shotstage"><img src="/api/image?id=${encodeURIComponent(item.key)}">${rect(p?.pane,'pane')}${rect(p?.comparison,'comparison')}${rawMarker}${point(semantic,'semantic','Semantic point used for AX pane selection')}</div>${paneSummary(p)}<div class="legend"><span><i class="lpane"></i>selected pane / authoritative OCR</span><span><i class="lcomparison"></i>comparison crop</span><span><i class="lsemantic"></i>semantic point</span><span><i class="lraw"></i>raw pointer when different</span></div></div>`;
  }).join('')}</section>`;
}
function apply(){
  const scope=$('scope').value,a=$('app').value,p=$('pane').value,q=$('search').value.toLowerCase();
  filtered=rows.filter(r=>inScope(r,scope)&&(!a||r.application===a)&&(!p||r.paneMethod===p)&&(!q||(r.sequence+' '+(r.baselineSequences||[]).join(' ')+' '+(r.sourceRecordIDs||[]).join(' ')+' '+r.windowTitle+' '+r.application+' '+r.paneMethod+' '+r.paneReason).toLowerCase().includes(q)));
  index=Math.min(index,Math.max(0,filtered.length-1));list();show();
}
function list(){
  $('items').innerHTML=filtered.map((r,i)=>{const absorbed=(r.baselineSequences||[]).length>1?` · absorbs #${r.baselineSequences.join(', #')}`:'';return `<button class="item ${i===index?'active':''}" data-i="${i}"><b class="${r.changed||r.modelChanged?'changed':''}">#${r.sequence}${esc(absorbed)}</b><small>${esc(r.application)} · ${esc(r.windowTitle)}</small><small>${esc(r.paneMethod)} · ${esc(r.paneConfidence)}</small><small>${esc(r.capturedAt)}</small></button>`}).join('');
  document.querySelectorAll('.item').forEach(b=>b.onclick=()=>{index=+b.dataset.i;location.hash=filtered[index].id;list();show()});
}
async function show(){
  const s=filtered[index];
  if(!s){$('main').innerHTML='<div class="empty">No matching READs</div>';return}
  const r=await(await fetch('/api/read?id='+encodeURIComponent(s.id))).json();
  const recon=r.observationReconciliation?` · reconciled ${r.observationReconciliation.memberObservationIDs?.length||0} sensor observations`:'';
  const dynamic=r.dynamicVisualConsolidation?` · settled ${r.dynamicVisualConsolidation.memberCount} dynamic states to the final state`:'';
  const currentSequence=r.candidateSequence!==r.sequence?` · current semantic event #${r.candidateSequence}`:'';
  const originals=(r.baselineReads||[]).length?r.baselineReads.map(v=>`--- Original READ #${v.sequence} · ${v.capturedAt} ---\n${v.content||'[empty]'}`).join('\n\n'):r.baselineContent||'[No READ in the original version]';
  const baselineLabels=(r.baselineSequences||[]).map(v=>'#'+v).join(', ')||'#'+r.sequence;
  $('main').innerHTML=`<div class="top"><h1>Review #${r.sequence} ${esc(r.application)} · ${esc(r.windowTitle)}</h1><span class="tag">${esc(outcomeLabel(r))}</span><span class="tag">${index+1} of ${filtered.length}</span></div><div class="muted">original baseline event${(r.baselineSequences||[]).length===1?'':'s'} ${esc(baselineLabels)}${esc(currentSequence)} · ${esc(r.capturedAt)} · cases are chronological within the selected filter${esc(recon)}${esc(dynamic)}</div><div class="grid">${imagePanels(r)}<section class="panel"><h2>Before · Original READ event${(r.baselineSequences||[]).length===1?'':'s'}</h2><pre>${esc(originals)}</pre></section><section class="panel"><h2>After · Newly available READ text</h2><pre>${esc(currentRead(r))}</pre></section></div>`;
}
Promise.all([fetch('/api/summary').then(r=>r.json()),fetch('/api/index').then(r=>r.json())]).then(([s,x])=>{
  rows=x;
  $('meta').textContent=`${s.baselineVersion} → ${s.candidateVersion} · shadow review`;
  $('stats').innerHTML=`<span class="tag">READs ${s.readCount}</span><span class="tag changed">changed ${s.changedReadCount}</span><span class="tag">unresolved ${s.unresolvedPaneObservationCount}</span>`;
  [...new Set(rows.map(r=>r.application).filter(Boolean))].sort().forEach(v=>$('app').insertAdjacentHTML('beforeend',`<option>${esc(v)}</option>`));
  [...new Set(rows.map(r=>r.paneMethod).filter(Boolean))].sort().forEach(v=>$('pane').insertAdjacentHTML('beforeend',`<option>${esc(v)}</option>`));
  $('scope').onchange=$('app').onchange=$('pane').onchange=$('search').oninput=()=>{index=0;apply()};apply();
  const requested=decodeURIComponent(location.hash.slice(1));
  const requestedIndex=filtered.findIndex(r=>r.id===requested||String(r.sequence)===requested);
  if(requestedIndex>=0){index=requestedIndex;list();show()}
}).catch(e=>$('main').textContent=e.stack||e);
</script>'''


def handler(store: ReviewStore) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def send_body(self, body: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            if parsed.path in {"/", "/index.html"}:
                self.send_body(HTML.encode(), "text/html; charset=utf-8")
            elif parsed.path == "/api/summary":
                self.send_body(json.dumps(store.summary).encode(), "application/json")
            elif parsed.path == "/api/index":
                self.send_body(json.dumps(store.index()).encode(), "application/json")
            elif parsed.path == "/api/read":
                row = store.by_id.get(query.get("id", [""])[0])
                if row is None:
                    self.send_error(404)
                else:
                    self.send_body(json.dumps(row).encode(), "application/json")
            elif parsed.path == "/api/image":
                item = store.images.get(query.get("id", [""])[0])
                if item is None:
                    self.send_error(404)
                elif sha256(item[0]) != item[1]:
                    self.send_error(409, "screenshot digest differs")
                else:
                    self.send_body(
                        item[0].read_bytes(),
                        mimetypes.guess_type(item[0].name)[0] or "image/png",
                    )
            else:
                self.send_error(404)

        def log_message(self, format: str, *values: object) -> None:
            return

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--read-surface-evidence", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--compiled", type=Path)
    parser.add_argument("--packed", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8772, type=int)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    store = ReviewStore(
        arguments.baseline, arguments.candidate,
        arguments.read_surface_evidence, arguments.source,
        arguments.compiled, arguments.packed,
    )
    if arguments.check:
        print(json.dumps(store.summary, sort_keys=True))
        return 0
    server = ThreadingHTTPServer((arguments.host, arguments.port), handler(store))
    print(
        f"Semantic READ {store.candidate_version} review: "
        f"http://{arguments.host}:{arguments.port}"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

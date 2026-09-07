#!/usr/bin/env python3
"""Serve a shadow semantic READ projection for manual review."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import urllib.parse
from collections import Counter
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock
from typing import Any

from phase1_read_novelty import (
    EMPTY_NOVELTY_DECISIONS as EMPTY_NOVELTY_RENDERINGS,
    POSITIVE_NOVELTY_DECISIONS as POSITIVE_NOVELTY_RENDERINGS,
)


class ReviewError(RuntimeError):
    pass


POSITIVE_NOVELTY_DECISIONS = frozenset(POSITIVE_NOVELTY_RENDERINGS)
EMPTY_NOVELTY_DECISIONS = frozenset(EMPTY_NOVELTY_RENDERINGS)
MODEL_REWRITTEN_RENDER_DECISIONS = frozenset(
    POSITIVE_NOVELTY_RENDERINGS.values()
) | frozenset(EMPTY_NOVELTY_RENDERINGS.values())
MODEL_FALLBACK_RENDER_DECISIONS = frozenset({
    "render_complete_dependency_unavailable",
    "render_complete_uncertain_microglyph",
    "retain_existing_truncated_state",
    "retain_nonread_model_projection",
})


def novelty_projection_changed(decision: str) -> bool:
    return decision in POSITIVE_NOVELTY_DECISIONS | EMPTY_NOVELTY_DECISIONS


def model_rendering_changed(decision: str) -> bool:
    return decision in MODEL_REWRITTEN_RENDER_DECISIONS


def review_projection(
    novelty: dict[str, Any], complete_semantic: str,
) -> tuple[str, str, bool]:
    """Return displayed content, outcome label, and empty-projection flag."""
    decision = str(novelty.get("decision", "missing"))
    if decision in EMPTY_NOVELTY_DECISIONS:
        labels = {
            "suppress_ambiguous_adjacent_difference": (
                "No newly available text · ambiguous high-overlap change"
            ),
            "suppress_nonsemantic_microglyph": (
                "No newly available text · OCR micro-change"
            ),
            "suppress_unstable_scroll_edge": (
                "No newly available text · scroll edge not stable yet"
            ),
        }
        return "[No new READ text]", labels.get(
            decision, "No newly available text"
        ), True
    if decision in POSITIVE_NOVELTY_DECISIONS:
        content = novelty.get("content")
        if not isinstance(content, str):
            raise ReviewError(f"{decision} lacks string novelty content")
        alignment = novelty.get("lineAlignment")
        matched = (
            alignment.get("matchedLineCount")
            if isinstance(alignment, dict) else None
        )
        suffix = (
            f" · {matched} repeated line{'s' if matched != 1 else ''} removed"
            if isinstance(matched, int) and matched > 0
            else " · repeated text removed"
        )
        if decision == "emit_stable_interior_after_clipped_boundary":
            label = "Stable interior text retained · clipped edge removed"
        elif decision == "emit_scroll_new_edge":
            label = "Newly exposed scroll edge retained"
        elif decision == "emit_contiguous_new_content":
            label = "New contiguous text retained" + suffix
        else:
            label = "New text retained" + suffix
        return content or "[No new READ text]", label, content == ""
    if decision == "retain_full_uncertain":
        label = "Full READ retained · comparison uncertain"
    elif novelty.get("reason") == (
        "candidate_alignment_unproven_preserve_complete_state"
    ):
        label = "Full READ retained · safe delta unproven"
    elif novelty.get("reason") == "no_causal_predecessor":
        label = "Full READ retained · no adjacent predecessor"
    elif novelty.get("reason") == "surface_changed":
        label = "Full READ retained · surface changed"
    else:
        label = "Full READ retained · substantial/new state"
    return complete_semantic or "[No new READ text]", label, False


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


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ReviewError(f"expected object at {path}:{line_number}")
            yield value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return list(iter_jsonl(path))


def raw_review_index(path: Path) -> dict[str, dict[str, Any]]:
    """Keep screenshot/geometry metadata, never complete AX trees or writes."""
    keys = {
        "recordID", "recordType", "sourceFrameRecordID", "capturedAt",
        "screenshotRelativePath", "screenshotSHA256", "screenshotPixelWidth",
        "screenshotPixelHeight", "windowBounds", "x", "y",
        "rawInteractionX", "rawInteractionY", "semanticContentPointReason",
        "appName", "bundleIdentifier", "windowTitle", "content",
    }
    result = {}
    for row in iter_jsonl(path):
        record_id = row.get("recordID")
        if not record_id or not (
            row.get("screenshotRelativePath")
            or row.get("recordType") in {
                "visual_ocr_observation", "screen_ocr_observation",
            }
            or row.get("recordType") == "read_observation"
        ):
            continue
        item = {key: value for key, value in row.items() if key in keys}
        surface = row.get("surface")
        if isinstance(surface, dict):
            item["surface"] = {"windowBounds": surface.get("windowBounds")}
        result[str(record_id)] = item
    return result


def source_ids(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(value) for value in row.get("sourceRecordIDs", []) if value)


def baseline_event_key(row: dict[str, Any]) -> tuple[Any, ...]:
    event_id = row.get("eventID")
    if event_id:
        return ("event", str(event_id))
    # Collector preview rows have no eventID. Different observations in a
    # consolidated candidate must not all collapse under the key None.
    return ("preview", row.get("sequence"), source_ids(row))


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
        self, baseline: Path | None, baseline_preview: Path | None,
        candidate: Path, surfaces: Path, source: Path,
        compiled: Path | None = None, packed: Path | None = None,
    ):
        if (baseline is None) == (baseline_preview is None):
            raise ReviewError(
                "supply exactly one of --baseline or --baseline-preview"
            )
        self.baseline = baseline.resolve() if baseline else None
        self.baseline_preview = (
            baseline_preview.resolve() if baseline_preview else None
        )
        self.candidate = candidate.resolve()
        self.surfaces = surfaces.resolve()
        self.source = source.resolve()
        self.compiled = compiled.resolve() if compiled else None
        self.packed = packed.resolve() if packed else None
        if (self.compiled is None) != (self.packed is None):
            raise ReviewError("--compiled and --packed must be supplied together")
        candidate_manifest = load_json(self.candidate / "reduction.json")
        if self.baseline is not None:
            base_manifest = load_json(self.baseline / "reduction.json")
            if base_manifest.get("reducerVersion") != "phase1-semantic-v14":
                raise ReviewError("baseline must be phase1-semantic-v14")
            baseline_version = "phase1-semantic-v14"
            base_event_path = self.baseline / "events.jsonl"
        else:
            base_manifest = None
            baseline_version = "collector-preview"
            base_event_path = self.baseline_preview
            assert base_event_path is not None
            if not base_event_path.is_file():
                raise ReviewError(f"baseline preview does not exist: {base_event_path}")
        candidate_version = candidate_manifest.get("reducerVersion")
        if candidate_version not in {
            "phase1-semantic-v16", "phase1-semantic-v17",
            "phase1-semantic-v18", "phase1-semantic-v19",
            "phase1-semantic-v20", "phase1-semantic-v21",
            "phase1-semantic-v22", "phase1-semantic-v23", "phase1-semantic-v24",
        }:
            raise ReviewError(
                "candidate must be phase1-semantic-v16 through v24"
            )
        self.candidate_version = str(candidate_version)
        self.session_id = str(candidate_manifest.get("sessionID", ""))
        manifest_pairs = [(self.candidate, candidate_manifest)]
        if self.baseline is not None and base_manifest is not None:
            manifest_pairs.insert(0, (self.baseline, base_manifest))
        for directory, manifest in manifest_pairs:
            expected = manifest.get("artifacts", {}).get("digestsSHA256", {})
            for name in ("events.jsonl", "unresolved.jsonl"):
                if sha256(directory / name) != expected.get(name):
                    raise ReviewError(f"artifact digest differs: {directory}/{name}")
        candidate_raw = candidate_manifest.get("source", {}).get("digestsSHA256", {}).get("raw.jsonl")
        if not candidate_raw or sha256(self.source / "raw.jsonl") != candidate_raw:
            raise ReviewError("candidate and source raw journal differ")
        if base_manifest is not None:
            base_raw = base_manifest.get("source", {}).get(
                "digestsSHA256", {}
            ).get("raw.jsonl")
            if base_raw != candidate_raw:
                raise ReviewError("baseline, candidate, and source raw journal differ")
        else:
            base_raw = candidate_raw

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
        raw_by_id = raw_review_index(self.source / "raw.jsonl")
        frame_by_ocr = {
            str(row["recordID"]): str(row["sourceFrameRecordID"])
            for row in raw_by_id.values()
            if row.get("recordType") == "visual_ocr_observation"
            and row.get("recordID") and row.get("sourceFrameRecordID")
        }

        base_read_rows = [
            row for row in load_jsonl(base_event_path)
            if row.get("kind") == "read"
        ]
        if self.baseline_preview is not None:
            preview_session_ids = {
                str(row.get("sessionID")) for row in base_read_rows
                if row.get("sessionID")
            }
            candidate_session_id = str(candidate_manifest.get("sessionID", ""))
            if preview_session_ids != {candidate_session_id}:
                raise ReviewError(
                    "baseline preview and candidate session IDs differ"
                )
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
        model_content_pool: dict[str, str] = {}
        packing_summary: dict[str, Any] | None = None
        if self.compiled is not None and self.packed is not None:
            packing_manifest = load_json(self.packed / "packing.json")
            if packing_manifest.get("packerVersion") not in {
                "phase1-token-pack-v8", "phase1-token-pack-v9",
                "phase1-token-pack-v10", "phase1-token-pack-v11",
                "phase1-token-pack-v12",
            }:
                raise ReviewError("packed review requires phase1-token-pack-v8 through v12")
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
            # A shared episode corpus can span many sessions. Retain only this
            # session's READ payloads while still validating all plan references.
            compiled_ids = set()
            compiled_events = {}
            for item in iter_jsonl(compiled_events_path):
                event_id = str(item["sourceEventID"])
                compiled_ids.add(event_id)
                if event_id in candidate_by_id:
                    if item.get("sessionID") != self.session_id:
                        raise ReviewError(f"compiled READ session differs: {event_id}")
                    compiled_events[event_id] = item
            plan_path = self.packed / "context-plans.jsonl"
            expected_plan_hash = packing_manifest.get(
                "artifactDigestsSHA256", {}
            ).get("context-plans.jsonl")
            if sha256(plan_path) != expected_plan_hash:
                raise ReviewError("packed context-plan digest differs")
            for plan in iter_jsonl(plan_path):
                for block in plan.get("retainedContextBlocks", []):
                    rendering = block.get("readRendering")
                    if not isinstance(rendering, dict):
                        continue
                    event_id = str(block.get("contextBlockID", ""))
                    if event_id not in compiled_ids:
                        raise ReviewError(f"packed block lacks compiled event: {event_id}")
                    compiled_event = compiled_events.get(event_id)
                    if compiled_event is None:
                        continue
                    serialized = block.get("serializedOverride")
                    if serialized is None:
                        serialized = compiled_event.get("serialized")
                    try:
                        model_payload = json.loads(serialized)
                    except (TypeError, json.JSONDecodeError) as error:
                        raise ReviewError(f"invalid packed READ projection: {event_id}") from error
                    content = model_payload.get("content")
                    if isinstance(content, str):
                        content = model_content_pool.setdefault(content, content)
                    model_occurrences.setdefault(event_id, []).append({
                        "exampleID": plan.get("exampleID"),
                        "targetEventID": plan.get("targetEventID"),
                        "decision": rendering.get("decision"),
                        "dependencyAvailable": rendering.get("dependencyAvailable"),
                        "dependsOnEventID": rendering.get("dependsOnEventID"),
                        "contentTruncated": block.get("contentTruncated"),
                        "modelFacingContent": content,
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
                baseline_event_key(item): item
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
            image_by_frame: dict[str, dict[str, Any]] = {}
            for source_id in ids:
                raw_id = frame_by_ocr.get(source_id, source_id)
                raw = raw_by_id.get(raw_id, {})
                relative = raw.get("screenshotRelativePath")
                expected_hash = raw.get("screenshotSHA256")
                if isinstance(relative, str) and isinstance(expected_hash, str):
                    path = (self.source / relative).resolve()
                    if self.source in path.parents and path.is_file():
                        image = image_by_frame.get(raw_id)
                        if image is None:
                            image_key = f"{row['eventID']}:{len(image_keys)}"
                            self.images[image_key] = (path, expected_hash)
                            image = {
                                "key": image_key,
                                "sourceRecordID": source_id,
                                "sourceFrameRecordID": raw_id,
                                "sourceRecordIDs": [],
                                "capturedAt": raw.get("capturedAt"),
                                "recordType": raw.get("recordType"),
                                "pixelWidth": raw.get("screenshotPixelWidth"),
                                "pixelHeight": raw.get("screenshotPixelHeight"),
                            }
                            image_by_frame[raw_id] = image
                            image_keys.append(image)
                        image["sourceRecordIDs"].append(source_id)
                        # Pane evidence belongs to the OCR observation linked
                        # to this exact frame, not to its pixel hash. A later
                        # identical frame can have different attention evidence.
                        if source_id in evidence:
                            observation = raw_by_id.get(source_id, raw)
                            image["sourceRecordID"] = source_id
                            image["recordType"] = observation.get("recordType")
            image_by_source = {
                source_id: item for item in image_keys
                for source_id in item["sourceRecordIDs"]
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
            changed = scaffolding_changed or novelty_projection_changed(
                novelty_decision
            )
            projected_content, outcome_label, novelty_suppressed = (
                review_projection(novelty, semantic)
            )
            occurrences = model_occurrences.get(str(row["eventID"]), [])
            rendering_counts = Counter(
                str(item.get("decision", "missing")) for item in occurrences
            )
            model_changed = any(model_rendering_changed(
                str(item.get("decision", "missing"))
            ) for item in occurrences)
            model_fallback = any(
                str(item.get("decision", "missing"))
                in MODEL_FALLBACK_RENDER_DECISIONS
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
                "reviewProjectedContent": projected_content,
                "reviewOutcomeLabel": outcome_label,
                "noveltySuppressed": novelty_suppressed,
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
                "samePaneSequence": row.get("reduction", {}).get(
                    "samePaneSequence"
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
                "reviewProjectedContent": (
                    f"[Excluded from semantic READ: {reason}]"
                ),
                "reviewOutcomeLabel": f"Excluded · {reason}",
                "noveltySuppressed": False,
                "semanticDetails": {},
                "baselineContent": baseline_row.get("content", "")
                    if baseline_row else str(raw.get("content") or ""),
                "imageKey": image_keys[0]["key"] if image_keys else "",
                "images": image_keys,
                "paneEvidence": pane_evidence,
                "observationReconciliation": None,
                "dynamicVisualConsolidation": None,
                "samePaneSequence": None,
                "modelOccurrences": [],
                "modelRenderingCounts": {},
                "modelChanged": False,
                "modelFallback": False,
                "unresolved": True,
                "unresolvedReason": reason,
            })
        rows.sort(key=lambda row: (str(row.get("capturedAt") or ""), row["id"]))
        baseline_kind = "collector_preview" if self.baseline_preview else "reviewed_semantic"
        baseline_description = (
            "Full-window OCR shown live during collection; not the original pane-v2 review."
            if self.baseline_preview else
            "Previously reviewed semantic READ projection (phase1-semantic-v14)."
        )
        for row in rows:
            row["baselineKind"] = baseline_kind
            row["baselineDescription"] = baseline_description
        self.rows = rows
        self.by_id = {row["id"]: row for row in rows}
        decisions = Counter(str(row["novelty"].get("decision", "missing")) for row in rows)
        pane_methods = Counter(
            str(item.get("method") or "missing")
            for row in rows for item in row.get("paneEvidence", [])
        )
        self.summary = {
            "status": "semantic_read_shadow_review_only_not_training_authority",
            "baselineVersion": baseline_version,
            "baselineKind": baseline_kind,
            "baselineDescription": baseline_description,
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


class ReviewSessions:
    """Lazy session routing; at most one expanded ReviewStore lives in memory."""

    def __init__(self, configurations: list[dict[str, Any]], factory=ReviewStore):
        if not configurations:
            raise ReviewError("sessions manifest must contain at least one session")
        self.configurations: dict[str, dict[str, Any]] = {}
        for configuration in configurations:
            session_id = configuration.get("id")
            if not isinstance(session_id, str) or not session_id.strip():
                raise ReviewError("every review session needs a nonempty id")
            if session_id in self.configurations:
                raise ReviewError(f"duplicate review session id: {session_id}")
            self.configurations[session_id] = configuration
        self.factory = factory
        self.lock = RLock()
        self.cached_id: str | None = None
        self.cached_store: ReviewStore | None = None

    @classmethod
    def from_manifest(cls, path: Path) -> ReviewSessions:
        manifest = load_json(path)
        sessions = manifest.get("sessions")
        if not isinstance(sessions, list) or not all(
            isinstance(item, dict) for item in sessions
        ):
            raise ReviewError("sessions manifest requires a sessions array")

        def artifact(value: Any) -> Path | None:
            if value is None:
                return None
            if not isinstance(value, str) or not value:
                raise ReviewError("review artifact paths must be nonempty strings")
            return (path.resolve().parent / value).resolve()

        configurations = []
        for item in sessions:
            baseline = artifact(item.get("baseline"))
            preview = artifact(item.get("baselinePreview"))
            if (baseline is None) == (preview is None):
                raise ReviewError("every session needs baseline or baselinePreview")
            candidate = artifact(item.get("candidate"))
            surfaces = artifact(item.get("readSurfaceEvidence"))
            source = artifact(item.get("source"))
            if any(value is None for value in (candidate, surfaces, source)):
                raise ReviewError("every session needs candidate, readSurfaceEvidence, source")
            compiled = artifact(manifest.get("compiled"))
            packed = artifact(manifest.get("packed"))
            if (compiled is None) != (packed is None):
                raise ReviewError("sessions manifest needs both compiled and packed")
            configurations.append({
                "id": item.get("id"), "label": item.get("label") or item.get("id"),
                "arguments": (baseline, preview, candidate, surfaces, source, compiled, packed),
            })
        return cls(configurations)

    def index(self) -> list[dict[str, str]]:
        return [{"id": item["id"], "label": str(item["label"])}
                for item in self.configurations.values()]

    def get(self, session_id: str = "") -> ReviewStore:
        with self.lock:
            session_id = session_id or next(iter(self.configurations))
            if session_id not in self.configurations:
                raise ReviewError(f"unknown review session: {session_id}")
            if self.cached_id != session_id:
                # Release the previous day before constructing the next; this is
                # intentionally not an unbounded cache of raw/packed evidence.
                self.cached_id = None
                self.cached_store = None
                self.cached_store = self.factory(
                    *self.configurations[session_id]["arguments"]
                )
                self.cached_id = session_id
            assert self.cached_store is not None
            return self.cached_store


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
      <select id="session" aria-label="Collection day" hidden></select>
      <select id="scope">
        <option value="canonicalized">Pane-v7 canonicalizations</option>
        <option value="changed">Changed READs</option>
        <option value="model-changed">Adjacent overlap removed</option>
        <option value="scaffolding">Interface text removed</option>
        <option value="unresolved">Unresolved / excluded panes</option>
        <option value="" selected>All READs</option>
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
const sessionID=new URLSearchParams(location.search).get('session')||'';
function api(path){return path+(path.includes('?')?'&':'?')+'session='+encodeURIComponent(sessionID)}
async function fetchJSON(path){const response=await fetch(path);let value;try{value=await response.json()}catch(error){throw Error(`HTTP ${response.status}: ${response.statusText}`)}if(!response.ok||value?.error)throw Error(value?.error||`HTTP ${response.status}: ${response.statusText}`);return value}
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
  return r.reviewProjectedContent||'[No new READ text]';
}
function outcomeLabel(r){
  return r.reviewOutcomeLabel||'READ projection unavailable';
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
function imagePanels(r,selectedIndex=null){
  const values=r.images||[],panes=r.paneEvidence||[];
  if(!values.length)return '';
  const i=selectedIndex===null?values.length-1:Math.max(0,Math.min(values.length-1,selectedIndex));
  const selector=values.length>1?`<div class="shotmeta"><label>Observation <select id="observation">${values.map((item,n)=>`<option value="${n}" ${n===i?'selected':''}>${n+1}/${values.length} · ${esc(item.capturedAt)}${n===values.length-1?' · final observation':''}</option>`).join('')}</select></label><div class="muted">One screenshot is loaded at a time. Earlier observations remain available here.</div></div>`:'';
  return `<section id="rawScreenshotPanel" class="panel wide"><h2>Raw screenshot${values.length===1?'':'s'} · exact pane selection and pointer evidence</h2>${selector}${[values[i]].map(item=>{
    const p=panes.find(value=>value.imageKey===item.key)||panes.find(value=>value.sourceRecordID===item.sourceRecordID);
    const raw=p?.rawPointer,semantic=p?.semanticPoint;
    const rawMarker=raw&&!samePoint(raw,semantic)?point(raw,'raw','Raw physical pointer'):'';
    return `<div class="shot"><div class="muted shotmeta">${i+1}/${values.length} · ${esc(item.capturedAt)} · ${esc(item.recordType)} · ${esc(item.pixelWidth)}×${esc(item.pixelHeight)}</div><div class="shotstage"><img decoding="async" src="${api('/api/image?id='+encodeURIComponent(item.key))}">${rect(p?.pane,'pane')}${rect(p?.comparison,'comparison')}${rawMarker}${point(semantic,'semantic','Semantic point used for AX pane selection')}</div>${paneSummary(p)}<div class="legend"><span><i class="lpane"></i>selected pane / authoritative OCR</span><span><i class="lcomparison"></i>comparison crop</span><span><i class="lsemantic"></i>semantic point</span><span><i class="lraw"></i>raw pointer when different</span></div></div>`;
  }).join('')}</section>`;
}
function bindImageSelection(r){const selector=$('observation');if(selector)selector.onchange=()=>{$('rawScreenshotPanel').outerHTML=imagePanels(r,Number(selector.value));bindImageSelection(r)}}
function apply(){
  const scope=$('scope').value,a=$('app').value,p=$('pane').value,q=$('search').value.toLowerCase();
  const numberQuery=q.match(/^#?(\d+)$/);
  filtered=rows.filter(r=>inScope(r,scope)&&(!a||r.application===a)&&(!p||r.paneMethod===p)&&(!q||(numberQuery?[r.sequence,...(r.baselineSequences||[])].includes(Number(numberQuery[1])):(r.sequence+' '+(r.baselineSequences||[]).join(' ')+' '+(r.sourceRecordIDs||[]).join(' ')+' '+r.windowTitle+' '+r.application+' '+r.paneMethod+' '+r.paneReason).toLowerCase().includes(q))));
  index=Math.min(index,Math.max(0,filtered.length-1));list();show();
}
function list(){
  $('items').innerHTML=filtered.map((r,i)=>{const absorbed=(r.baselineSequences||[]).length>1?` · absorbs #${r.baselineSequences.join(', #')}`:'';return `<button class="item ${i===index?'active':''}" data-i="${i}"><b class="${r.changed||r.modelChanged?'changed':''}">#${r.sequence}${esc(absorbed)}</b><small>${esc(r.application)} · ${esc(r.windowTitle)}</small><small>${esc(r.paneMethod)} · ${esc(r.paneConfidence)}</small><small>${esc(r.capturedAt)}</small></button>`}).join('');
  document.querySelectorAll('.item').forEach(b=>b.onclick=()=>{index=+b.dataset.i;location.hash=filtered[index].id;list();show()});
}
async function show(){
  const s=filtered[index];
  if(!s){$('main').innerHTML='<div class="empty">No matching READs</div>';return}
  try{
  const r=await fetchJSON(api('/api/read?id='+encodeURIComponent(s.id)));
  if(filtered[index]?.id!==s.id)return;
  const recon=r.observationReconciliation?` · reconciled ${r.observationReconciliation.memberObservationIDs?.length||0} sensor observations`:'';
  const sequence=r.samePaneSequence?` · combined ${r.samePaneSequence.memberCount} passive observations at ${r.samePaneSequence.closureReason}`:'';
  const dynamic=r.dynamicVisualConsolidation?` · settled ${r.dynamicVisualConsolidation.memberCount} dynamic states to the final state`:sequence;
  const fallback=r.modelFallback?`<span class="tag warning">Some 32K contexts restore the full READ because its required predecessor was truncated</span>`:'';
  const currentSequence=r.candidateSequence!==r.sequence?` · current semantic event #${r.candidateSequence}`:'';
  const preview=r.baselineKind==='collector_preview';
  const baselineTitle=preview?'Recorded live collector preview':'Original reviewed READ';
  const baselineRowLabel=preview?'Live preview READ':'Original READ';
  const originals=(r.baselineReads||[]).length?r.baselineReads.map(v=>`--- ${baselineRowLabel} #${v.sequence} · ${v.capturedAt} ---\n${v.content||'[empty]'}`).join('\n\n'):r.baselineContent||'[No corresponding baseline READ]';
  const baselineLabels=(r.baselineSequences||[]).map(v=>'#'+v).join(', ')||'#'+r.sequence;
  $('main').innerHTML=`<div class="top"><h1>Review #${r.sequence} ${esc(r.application)} · ${esc(r.windowTitle)}</h1><span class="tag">${esc(outcomeLabel(r))}</span>${fallback}<span class="tag">${index+1} of ${filtered.length}</span></div><div class="muted">${esc(baselineRowLabel)} ${esc(baselineLabels)}${esc(currentSequence)} · ${esc(r.capturedAt)} · cases are chronological within the selected filter${esc(recon)}${esc(dynamic)}</div><div class="grid">${imagePanels(r)}<section class="panel"><h2>Before · ${esc(baselineTitle)}</h2><div class="muted shotmeta">${esc(r.baselineDescription)}</div><pre>${esc(originals)}</pre></section><section class="panel"><h2>After · Newly available READ text</h2><pre>${esc(currentRead(r))}</pre></section></div>`;
  bindImageSelection(r);
  }catch(error){if(filtered[index]?.id===s.id)$('main').textContent='Unable to load READ: '+(error.message||error)}
}
Promise.all([fetchJSON(api('/api/summary')),fetchJSON(api('/api/index')),fetchJSON('/api/sessions')]).then(([s,x,days])=>{
  if(s.error||x.error)throw Error(s.error||x.error);
  days.forEach(day=>{const option=document.createElement('option');option.value=day.id;option.textContent=day.label;$('session').appendChild(option)});
  $('session').value=sessionID||days[0]?.id||'';
  $('session').hidden=days.length<2;
  $('session').onchange=()=>{location.href='/?session='+encodeURIComponent($('session').value)};
  rows=x;
  const dayLabel=days.find(day=>day.id===$('session').value)?.label;
  $('meta').textContent=`${dayLabel?dayLabel+' · ':''}${s.baselineVersion} → ${s.candidateVersion} · shadow review`;
  $('stats').innerHTML=`<span class="tag">READs ${s.readCount}</span><span class="tag changed">changed ${s.changedReadCount}</span><span class="tag">unresolved ${s.unresolvedPaneObservationCount}</span>`;
  [...new Set(rows.map(r=>r.application).filter(Boolean))].sort().forEach(v=>$('app').insertAdjacentHTML('beforeend',`<option>${esc(v)}</option>`));
  [...new Set(rows.map(r=>r.paneMethod).filter(Boolean))].sort().forEach(v=>$('pane').insertAdjacentHTML('beforeend',`<option>${esc(v)}</option>`));
  $('scope').onchange=$('app').onchange=$('pane').onchange=$('search').oninput=()=>{index=0;apply()};apply();
  const requested=decodeURIComponent(location.hash.slice(1));
  const requestedIndex=filtered.findIndex(r=>r.id===requested||String(r.sequence)===requested);
  if(requestedIndex>=0){index=requestedIndex;list();show()}
  else if(requested){filtered=[];$('items').innerHTML='';$('main').textContent='Requested READ was not found in this session: '+requested}
}).catch(e=>$('main').textContent=e.stack||e);
</script>'''


def handler(repository: ReviewStore | ReviewSessions) -> type[BaseHTTPRequestHandler]:
    request_lock = repository.lock if isinstance(repository, ReviewSessions) else RLock()

    class Handler(BaseHTTPRequestHandler):
        def send_body(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            # Serialize session construction and responses, so concurrent image
            # requests cannot retain the evicted day's expanded store.
            with request_lock:
                try:
                    self.serve_get()
                except (ReviewError, OSError, json.JSONDecodeError) as error:
                    self.send_body(
                        json.dumps({"error": str(error)}).encode(),
                        "application/json", 409,
                    )

        def serve_get(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            if parsed.path in {"/", "/index.html"}:
                self.send_body(HTML.encode(), "text/html; charset=utf-8")
                return
            if parsed.path == "/api/sessions":
                entries = repository.index() if isinstance(repository, ReviewSessions) else [
                    {"id": "", "label": "Current session"}
                ]
                self.send_body(json.dumps(entries).encode(), "application/json")
                return
            store = repository.get(query.get("session", [""])[0]) if isinstance(
                repository, ReviewSessions
            ) else repository
            if parsed.path == "/api/summary":
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
    parser.add_argument(
        "--sessions", type=Path,
        help="JSON manifest of per-session artifacts plus shared compiled/packed corpus; paths relative to manifest",
    )
    baseline_group = parser.add_mutually_exclusive_group()
    baseline_group.add_argument("--baseline", type=Path)
    baseline_group.add_argument(
        "--baseline-preview", type=Path,
        help="collector events.preview.jsonl from the same source session",
    )
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--read-surface-evidence", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--compiled", type=Path)
    parser.add_argument("--packed", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8772, type=int)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    single_arguments = (
        arguments.baseline, arguments.baseline_preview, arguments.candidate,
        arguments.read_surface_evidence, arguments.source, arguments.compiled, arguments.packed,
    )
    if arguments.sessions:
        if any(value is not None for value in single_arguments):
            parser.error("--sessions cannot be combined with single-session artifact flags")
        store = ReviewSessions.from_manifest(arguments.sessions)
    else:
        if not (arguments.baseline or arguments.baseline_preview) or any(
            value is None for value in (
                arguments.candidate, arguments.read_surface_evidence, arguments.source,
            )
        ):
            parser.error("supply --sessions or baseline, candidate, read-surface-evidence, source")
        store = ReviewStore(*single_arguments)
    if arguments.check:
        result = (
            {"sessions": [{**item, "summary": store.get(item["id"]).summary}
                          for item in store.index()], "maximumCachedSessions": 1}
            if isinstance(store, ReviewSessions) else store.summary
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    server = ThreadingHTTPServer((arguments.host, arguments.port), handler(store))
    print(
        f"Semantic READ {'combined corpus' if isinstance(store, ReviewSessions) else store.candidate_version} review: "
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

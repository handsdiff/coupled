#!/usr/bin/env python3
"""Application-neutral AX pane selection for Phase 1 READ construction."""

from __future__ import annotations

import math
from typing import Any

from phase1_read_surface import surface_region as v1_surface_region


SURFACE_RULE_VERSION = "ax-pane-read-v2"
PROPOSAL_RULE_VERSION = SURFACE_RULE_VERSION

SEMANTIC_PRIORITIES = {
    "AXLandmarkMain": (130, "main_landmark"),
    "AXContentList": (125, "content_list"),
    "AXCodeStyleGroup": (120, "code_editor"),
    "AXTextArea": (115, "text_area"),
    "AXLandmarkNavigation": (110, "navigation_landmark"),
    "AXLandmarkComplementary": (105, "complementary_landmark"),
    "AXList": (100, "list"),
}


def numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def require_number(mapping: dict[str, Any], key: str) -> float:
    value = mapping.get(key)
    if not numeric(value) or not math.isfinite(float(value)):
        raise ValueError(f"invalid numeric {key}: {value!r}")
    return float(value)


def rectangle(mapping: dict[str, Any]) -> dict[str, float]:
    return {key: require_number(mapping, key) for key in ("x", "y", "width", "height")}


def intersect(
    first: dict[str, float], second: dict[str, float]
) -> dict[str, float] | None:
    left = max(first["x"], second["x"])
    top = max(first["y"], second["y"])
    right = min(first["x"] + first["width"], second["x"] + second["width"])
    bottom = min(first["y"] + first["height"], second["y"] + second["height"])
    if right <= left or bottom <= top:
        return None
    return {"x": left, "y": top, "width": right - left, "height": bottom - top}


def contains_point(frame: dict[str, float], x: float, y: float, tolerance: float = 2) -> bool:
    return (
        frame["x"] - tolerance <= x <= frame["x"] + frame["width"] + tolerance
        and frame["y"] - tolerance <= y <= frame["y"] + frame["height"] + tolerance
    )


def frame_near(first: dict[str, float], second: dict[str, float], tolerance: float = 16) -> bool:
    return all(abs(first[key] - second[key]) <= tolerance for key in first)


def contains_rectangle(
    outer: dict[str, float], inner: dict[str, float], tolerance: float = 2
) -> bool:
    return (
        outer["x"] <= inner["x"] + tolerance
        and outer["y"] <= inner["y"] + tolerance
        and outer["x"] + outer["width"] >= inner["x"] + inner["width"] - tolerance
        and outer["y"] + outer["height"] >= inner["y"] + inner["height"] - tolerance
    )


def screen_rectangle(
    global_frame: dict[str, float],
    window: dict[str, float],
    pixel_width: float,
    pixel_height: float,
) -> dict[str, float] | None:
    clipped = intersect(global_frame, window)
    if clipped is None:
        return None
    scale_x = pixel_width / window["width"]
    scale_y = pixel_height / window["height"]
    return {
        "x": round((clipped["x"] - window["x"]) * scale_x, 4),
        "y": round((clipped["y"] - window["y"]) * scale_y, 4),
        "width": round(clipped["width"] * scale_x, 4),
        "height": round(clipped["height"] * scale_y, 4),
    }


def normalized_top_rectangle(
    screen: dict[str, float], pixel_width: float, pixel_height: float
) -> dict[str, float]:
    return {
        "x": round(screen["x"] / pixel_width, 8),
        "y": round(screen["y"] / pixel_height, 8),
        "width": round(screen["width"] / pixel_width, 8),
        "height": round(screen["height"] / pixel_height, 8),
    }


def normalized_top_to_vision_region(
    normalized_top: dict[str, float],
) -> dict[str, float]:
    """Convert top-origin normalized UI geometry to Vision's bottom origin."""
    value = rectangle(normalized_top)
    if (
        value["x"] < 0
        or value["y"] < 0
        or value["width"] <= 0
        or value["height"] <= 0
        or value["x"] + value["width"] > 1.00000001
        or value["y"] + value["height"] > 1.00000001
    ):
        raise ValueError(f"normalized rectangle is outside the image: {value!r}")
    return {
        "x": round(value["x"], 8),
        "y": round(1 - value["y"] - value["height"], 8),
        "width": round(value["width"], 8),
        "height": round(value["height"], 8),
    }


def vision_region_to_screen(
    region: dict[str, float], pixel_width: float, pixel_height: float
) -> dict[str, float]:
    return {
        "x": round(region["x"] * pixel_width, 4),
        "y": round((1 - region["y"] - region["height"]) * pixel_height, 4),
        "width": round(region["width"] * pixel_width, 4),
        "height": round(region["height"] * pixel_height, 4),
    }


def semantic_priority(node: dict[str, Any]) -> tuple[int, str] | None:
    for value in (node.get("subrole"), node.get("role")):
        if value in SEMANTIC_PRIORITIES:
            return SEMANTIC_PRIORITIES[str(value)]
    return None


def proposal_for_record(record: dict[str, Any]) -> dict[str, Any]:
    """Return review-only v1/AX rectangles and one conservative v2 proposal."""
    window = rectangle(record.get("windowBounds") or {})
    pixel_width = require_number(record, "screenshotPixelWidth")
    pixel_height = require_number(record, "screenshotPixelHeight")
    pointer_x = require_number(record, "x")
    pointer_y = require_number(record, "y")
    probe = record.get("accessibilitySurface")
    if not isinstance(probe, dict):
        raise ValueError("screen observation has no accessibilitySurface")
    raw_ancestors = probe.get("ancestors")
    if not isinstance(raw_ancestors, list):
        raise ValueError("accessibilitySurface ancestors are invalid")

    v1_region, v1_provenance = v1_surface_region(record)
    v1_screen = vision_region_to_screen(v1_region, pixel_width, pixel_height)

    ancestors: list[dict[str, Any]] = []
    frames: list[dict[str, float] | None] = []
    for node in raw_ancestors:
        frame_value = node.get("frame") if isinstance(node, dict) else None
        try:
            frame = rectangle(frame_value) if isinstance(frame_value, dict) else None
        except ValueError:
            frame = None
        frames.append(frame)

    window_area = window["width"] * window["height"]
    for frame_index, (node, frame) in enumerate(zip(raw_ancestors, frames)):
        output = {
            "depth": node.get("depth"),
            "role": node.get("role"),
            "subrole": node.get("subrole"),
            "title": node.get("title"),
            "elementDescription": node.get("elementDescription"),
            "identifier": node.get("identifier"),
            "processIdentifier": node.get("processIdentifier"),
            "globalFrame": frame,
            "errors": node.get("errors") or [],
        }
        if frame is None:
            output.update({
                "screenRectangle": None,
                "normalizedTopRectangle": None,
                "containsPointer": False,
                "areaFraction": None,
                "widthFraction": None,
                "heightFraction": None,
                "nearFrameCount": 0,
                "nearAdjacentFrameCount": 0,
                "semanticKind": None,
            })
        else:
            clipped = intersect(frame, window)
            screen = screen_rectangle(frame, window, pixel_width, pixel_height)
            area = 0 if clipped is None else clipped["width"] * clipped["height"]
            semantic = semantic_priority(node)
            output.update({
                "screenRectangle": screen,
                "normalizedTopRectangle": (
                    normalized_top_rectangle(screen, pixel_width, pixel_height)
                    if screen is not None else None
                ),
                "containsPointer": contains_point(frame, pointer_x, pointer_y),
                "areaFraction": round(area / window_area, 8),
                "widthFraction": round((clipped or {"width": 0})["width"] / window["width"], 8),
                "heightFraction": round((clipped or {"height": 0})["height"] / window["height"], 8),
                "nearFrameCount": sum(
                    other is not None and frame_near(frame, other) for other in frames
                ),
                "nearAdjacentFrameCount": sum(
                    frames[adjacent_index] is not None
                    and frame_near(frame, frames[adjacent_index])
                    for adjacent_index in (frame_index - 1, frame_index + 1)
                    if 0 <= adjacent_index < len(frames)
                ),
                "semanticKind": semantic[1] if semantic else None,
            })
        ancestors.append(output)

    def eligible(node: dict[str, Any]) -> bool:
        return bool(
            node["screenRectangle"]
            and node["containsPointer"]
            and node["areaFraction"] is not None
            and 0.08 <= node["areaFraction"] <= 0.82
            and node["screenRectangle"]["width"] >= 80
            and node["screenRectangle"]["height"] >= 80
        )

    semantic_candidates = []
    for node in ancestors:
        if not eligible(node) or node["semanticKind"] is None:
            continue
        original = raw_ancestors[int(node["depth"])]
        priority, kind = semantic_priority(original) or (0, "")
        semantic_candidates.append((priority, -node["areaFraction"], -int(node["depth"]), kind, node))

    selected: dict[str, Any] | None = None
    method = "v1_fallback"
    confidence = "fallback"
    reason = "no_high_confidence_ax_pane"
    if semantic_candidates:
        _, _, _, kind, selected = max(semantic_candidates)
        method = "ax_semantic_container"
        confidence = "high"
        reason = f"semantic_{kind}"
    else:
        geometric_candidates = [
            node for node in ancestors
            if eligible(node)
            and node["nearFrameCount"] >= 2
            and node["heightFraction"] >= 0.60
            and 0.12 <= node["widthFraction"] <= 0.72
            and node["areaFraction"] <= 0.72
        ]
        if geometric_candidates:
            selected = min(
                geometric_candidates,
                key=lambda node: (node["areaFraction"], int(node["depth"])),
            )
            method = "ax_repeated_vertical_pane"
            confidence = "medium"
            reason = "repeated_pane_geometry_without_application_rule"
        else:
            bounded_candidates = [
                node for node in ancestors
                if eligible(node)
                and node["nearAdjacentFrameCount"] >= 1
                and 0.15 <= node["areaFraction"] <= 0.75
                and node["widthFraction"] >= 0.25
                and node["heightFraction"] >= 0.25
            ]
            if bounded_candidates:
                selected = min(
                    bounded_candidates,
                    key=lambda node: (node["areaFraction"], int(node["depth"])),
                )
                method = "ax_repeated_bounded_surface"
                confidence = "medium"
                reason = "repeated_bounded_surface_geometry_without_application_rule"
            else:
                outer_candidates = [
                    node for node in ancestors
                    if node["screenRectangle"]
                    and node["containsPointer"]
                    and node["areaFraction"] is not None
                    and node["areaFraction"] <= 0.95
                    and contains_rectangle(node["screenRectangle"], v1_screen)
                ]
                if outer_candidates:
                    selected = min(
                        outer_candidates,
                        key=lambda node: int(node["depth"]),
                    )
                    method = "ax_nearest_outer_container"
                    confidence = "low"
                    reason = "nearest_ax_ancestor_enclosing_v1_crop"

    selected_screen = selected["screenRectangle"] if selected else v1_screen
    selected_normalized = normalized_top_rectangle(
        selected_screen, pixel_width, pixel_height
    )
    pointer_screen = {
        "x": round((pointer_x - window["x"]) * pixel_width / window["width"], 4),
        "y": round((pointer_y - window["y"]) * pixel_height / window["height"], 4),
    }
    return {
        "ruleVersion": PROPOSAL_RULE_VERSION,
        "image": {"width": pixel_width, "height": pixel_height},
        "windowBounds": window,
        "pointer": {
            "global": {"x": pointer_x, "y": pointer_y},
            "screen": pointer_screen,
        },
        "v1": {
            "method": v1_provenance["method"],
            "confidence": v1_provenance["confidence"],
            "regionOfInterest": v1_region,
            "screenRectangle": v1_screen,
            "normalizedTopRectangle": normalized_top_rectangle(
                v1_screen, pixel_width, pixel_height
            ),
        },
        "ancestors": ancestors,
        "proposal": {
            "method": method,
            "confidence": confidence,
            "reason": reason,
            "selectedDepth": selected.get("depth") if selected else None,
            "selectedRole": selected.get("role") if selected else None,
            "selectedSubrole": selected.get("subrole") if selected else None,
            "screenRectangle": selected_screen,
            "normalizedTopRectangle": selected_normalized,
            "regionOfInterest": normalized_top_to_vision_region(selected_normalized),
            "isV1Fallback": selected is None,
        },
    }


def surface_region(record: dict[str, Any]) -> tuple[dict[str, float], dict[str, Any]]:
    """Return the reviewed v2 Vision OCR region and compact provenance."""
    review = proposal_for_record(record)
    proposal = review["proposal"]
    region = proposal["regionOfInterest"]
    return region, {
        "ruleVersion": SURFACE_RULE_VERSION,
        "method": proposal["method"],
        "confidence": proposal["confidence"],
        "reason": proposal["reason"],
        "selectedDepth": proposal["selectedDepth"],
        "selectedRole": proposal["selectedRole"],
        "selectedSubrole": proposal["selectedSubrole"],
        "isV1Fallback": proposal["isV1Fallback"],
        "regionOfInterest": region,
    }

#!/usr/bin/env python3
"""Pure Phase 1 READ-surface selection and adjacent-overlap rules."""

from __future__ import annotations

import math
from typing import Any


SURFACE_RULE_VERSION = "pointer-local-read-v1"


def clamp(value: float, lower: float, upper: float) -> float:
    return min(max(value, lower), upper)


def profile_region(bundle: str) -> tuple[float, float, float, float]:
    """Return left, top, width, and height in normalized window coordinates."""
    if bundle == "com.openai.codex":
        return (0.18, 0.05, 0.80, 0.90)
    if bundle == "com.google.Chrome":
        return (0.08, 0.10, 0.84, 0.82)
    if bundle == "md.obsidian":
        return (0.18, 0.05, 0.80, 0.90)
    return (0.08, 0.08, 0.84, 0.84)


def pointer_dimensions(bundle: str) -> tuple[float, float]:
    if bundle == "com.microsoft.VSCode":
        return (0.68, 0.50)
    if bundle == "com.openai.codex":
        return (0.72, 0.72)
    return (0.72, 0.68)


def surface_region(record: dict[str, Any]) -> tuple[dict[str, float], dict[str, Any]]:
    """Select the frozen target-blind OCR region for one raw READ observation."""
    bundle = str(record.get("bundleIdentifier") or "")
    bounds = record.get("windowBounds") or {}
    trigger_types = set(record.get("triggerTypes") or [])
    pointer_trigger = bool(
        trigger_types
        & {"click", "scroll", "pointer_moved", "pointer_dragged", "pointer_settled"}
    )
    try:
        width = float(bounds["width"])
        height = float(bounds["height"])
        normalized_x = (float(record["x"]) - float(bounds["x"])) / width
        normalized_top = (float(record["y"]) - float(bounds["y"])) / height
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        normalized_x = math.nan
        normalized_top = math.nan
    pointer_inside = (
        pointer_trigger
        and math.isfinite(normalized_x)
        and math.isfinite(normalized_top)
        and -0.02 <= normalized_x <= 1.02
        and -0.02 <= normalized_top <= 1.02
    )
    if pointer_inside:
        region_width, region_height = pointer_dimensions(bundle)
        left = clamp(normalized_x - region_width / 2, 0, 1 - region_width)
        top = clamp(normalized_top - region_height / 2, 0, 1 - region_height)
        method = "pointer_local"
        confidence = "interaction_locus"
    else:
        left, top, region_width, region_height = profile_region(bundle)
        method = "application_profile"
        confidence = "activation_or_missing_pointer_fallback"
    region = {
        "x": round(left, 8),
        # Vision uses a bottom-left normalized origin; raw pointer coordinates
        # and the profile rules use a top-left origin.
        "y": round(1 - top - region_height, 8),
        "width": round(region_width, 8),
        "height": round(region_height, 8),
    }
    provenance = {
        "ruleVersion": SURFACE_RULE_VERSION,
        "method": method,
        "confidence": confidence,
        "triggerTypes": sorted(trigger_types),
        "pointerNormalized": (
            {"x": round(normalized_x, 8), "top": round(normalized_top, 8)}
            if math.isfinite(normalized_x) and math.isfinite(normalized_top)
            else None
        ),
        "regionOfInterest": region,
    }
    return region, provenance


def normalized_line(value: str) -> str:
    return " ".join(value.split()).casefold()


def remove_adjacent_line_overlap(
    previous_lines: list[str], current_lines: list[str]
) -> tuple[list[str], int]:
    """Remove an exact normalized suffix/prefix overlap between adjacent READs."""
    maximum = min(len(previous_lines), len(current_lines))
    normalized_previous = [normalized_line(value) for value in previous_lines]
    normalized_current = [normalized_line(value) for value in current_lines]
    for count in range(maximum, 0, -1):
        if normalized_previous[-count:] == normalized_current[:count]:
            return current_lines[count:], count
    return current_lines, 0

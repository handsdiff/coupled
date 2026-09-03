#!/usr/bin/env python3
"""AX pane v5: full-pane content plus a cropped comparison projection."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from phase1_read_surface_v2 import normalized_top_to_vision_region
from phase1_read_surface_v3 import proposal_for_record as v3_proposal_for_record
from phase1_read_surface_v4 import (
    BOTTOM_CROP_FRACTION,
    TOP_CROP_FRACTION,
    inset_vertical,
)


SURFACE_RULE_VERSION = "ax-pane-read-v5"
PROPOSAL_RULE_VERSION = SURFACE_RULE_VERSION


def proposal_for_record(record: dict[str, Any]) -> dict[str, Any]:
    """Keep the complete selected pane authoritative and derive an interior view."""
    review = deepcopy(v3_proposal_for_record(record))
    proposal = review["proposal"]
    full_screen = proposal["screenRectangle"]
    full_normalized = proposal["normalizedTopRectangle"]
    comparison_screen = inset_vertical(full_screen)
    comparison_normalized = inset_vertical(full_normalized)

    review["ruleVersion"] = SURFACE_RULE_VERSION
    proposal["ruleVersion"] = SURFACE_RULE_VERSION
    proposal["screenRectangle"] = full_screen
    proposal["normalizedTopRectangle"] = full_normalized
    proposal["regionOfInterest"] = normalized_top_to_vision_region(full_normalized)
    proposal["comparisonScreenRectangle"] = comparison_screen
    proposal["comparisonNormalizedTopRectangle"] = comparison_normalized
    proposal["comparisonRegionOfInterest"] = normalized_top_to_vision_region(
        comparison_normalized
    )
    proposal["paneCrop"] = {
        "stage": "comparison_only_before_ocr",
        "topFraction": TOP_CROP_FRACTION,
        "bottomFraction": BOTTOM_CROP_FRACTION,
    }
    return review


def surface_regions(
    record: dict[str, Any],
) -> tuple[dict[str, float], dict[str, float], dict[str, Any]]:
    review = proposal_for_record(record)
    proposal = review["proposal"]
    full = proposal["regionOfInterest"]
    comparison = proposal["comparisonRegionOfInterest"]
    return full, comparison, {
        "ruleVersion": SURFACE_RULE_VERSION,
        "method": proposal["method"],
        "confidence": proposal["confidence"],
        "reason": proposal["reason"],
        "selectedDepth": proposal["selectedDepth"],
        "selectedRole": proposal["selectedRole"],
        "selectedSubrole": proposal["selectedSubrole"],
        "isV1Fallback": proposal["isV1Fallback"],
        "regionOfInterest": full,
        "comparisonRegionOfInterest": comparison,
        "paneCrop": proposal["paneCrop"],
    }


def surface_region(record: dict[str, Any]) -> tuple[dict[str, float], dict[str, Any]]:
    """Compatibility adapter: the authoritative v5 region is the complete pane."""
    full, _, selection = surface_regions(record)
    return full, selection

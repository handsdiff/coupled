#!/usr/bin/env python3
"""AX pane v3 selection with a 20% top/bottom pre-OCR inset."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from phase1_read_surface_v2 import normalized_top_to_vision_region
from phase1_read_surface_v3 import proposal_for_record as v3_proposal_for_record


SURFACE_RULE_VERSION = "ax-pane-read-v4"
PROPOSAL_RULE_VERSION = SURFACE_RULE_VERSION
TOP_CROP_FRACTION = 0.20
BOTTOM_CROP_FRACTION = 0.20


def inset_vertical(rectangle: dict[str, float]) -> dict[str, float]:
    """Inset a top-origin rectangle by 20% of its own height at both edges."""
    height = float(rectangle["height"])
    return {
        "x": round(float(rectangle["x"]), 8),
        "y": round(float(rectangle["y"]) + height * TOP_CROP_FRACTION, 8),
        "width": round(float(rectangle["width"]), 8),
        "height": round(
            height * (1 - TOP_CROP_FRACTION - BOTTOM_CROP_FRACTION), 8
        ),
    }


def proposal_for_record(record: dict[str, Any]) -> dict[str, Any]:
    review = deepcopy(v3_proposal_for_record(record))
    proposal = review["proposal"]
    original_screen = proposal["screenRectangle"]
    original_normalized = proposal["normalizedTopRectangle"]
    cropped_screen = inset_vertical(original_screen)
    cropped_normalized = inset_vertical(original_normalized)

    review["ruleVersion"] = SURFACE_RULE_VERSION
    proposal["ruleVersion"] = SURFACE_RULE_VERSION
    proposal["uncroppedScreenRectangle"] = original_screen
    proposal["uncroppedNormalizedTopRectangle"] = original_normalized
    proposal["screenRectangle"] = cropped_screen
    proposal["normalizedTopRectangle"] = cropped_normalized
    proposal["regionOfInterest"] = normalized_top_to_vision_region(
        cropped_normalized
    )
    proposal["paneCrop"] = {
        "stage": "before_ocr",
        "topFraction": TOP_CROP_FRACTION,
        "bottomFraction": BOTTOM_CROP_FRACTION,
    }
    return review


def surface_region(record: dict[str, Any]) -> tuple[dict[str, float], dict[str, Any]]:
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
        "uncroppedRegionOfInterest": normalized_top_to_vision_region(
            proposal["uncroppedNormalizedTopRectangle"]
        ),
        "paneCrop": proposal["paneCrop"],
        "regionOfInterest": region,
    }

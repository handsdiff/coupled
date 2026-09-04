#!/usr/bin/env python3
"""AX pane v7: retain one canonical outer pane across nested sensor probes."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from math import hypot
from typing import Any

from phase1_read_surface_v2 import normalized_top_to_vision_region
from phase1_read_surface_v4 import inset_vertical
from phase1_read_surface_v6 import (
    PaneAnchor,
    PaneResolver as V6PaneResolver,
    normalized_node_rectangle,
    normalized_pointer,
    screen_rectangle,
    surface_regions_from_review,
    window_key,
)


SURFACE_RULE_VERSION = "ax-pane-read-v7"
PROPOSAL_RULE_VERSION = SURFACE_RULE_VERSION

MAXIMUM_CROSS_SENSOR_INTERVAL_SECONDS = 1.5
MAXIMUM_NORMALIZED_POINTER_DISTANCE = 0.02
MINIMUM_CONTAINMENT_OVER_INNER_AREA = 0.98
MINIMUM_OUTER_ANCESTOR_AREA_RATIO = 0.85
MINIMUM_OUTER_TO_INNER_AREA_RATIO = 1.10

# These nodes can identify a genuinely different receiving/reading surface in
# one application window. Never replace them with a remembered outer pane.
DISTINCT_SEMANTIC_ROOT_ROLES = {
    "AXCodeStyleGroup",
    "AXTextArea",
    "AXTextField",
    "AXWebArea",
}
DISTINCT_SEMANTIC_ROOT_SUBROLES = {
    "AXDocumentArticle",
    "AXLandmarkMain",
}

# Production evidence currently proves one unstable descendant transition:
# Chromium chat surfaces may expose the same visible state first through the
# established outer pane and then through a nested AXContentList.  Generic
# nested AXGroups are deliberately not canonicalized: their geometry can move
# with real layout changes, and the existing reducer already reconciles the
# demonstrated same-state cases without widening the OCR pane.
CANONICALIZABLE_DESCENDANT_SUBROLES = {
    "AXContentList",
}


def timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def rectangle_area(rectangle: dict[str, float]) -> float:
    return max(0.0, rectangle["width"]) * max(0.0, rectangle["height"])


def rectangle_intersection_area(
    left: dict[str, float], right: dict[str, float],
) -> float:
    width = max(
        0.0,
        min(left["x"] + left["width"], right["x"] + right["width"])
        - max(left["x"], right["x"]),
    )
    height = max(
        0.0,
        min(left["y"] + left["height"], right["y"] + right["height"])
        - max(left["y"], right["y"]),
    )
    return width * height


def containment_over_inner(
    inner: dict[str, float], outer: dict[str, float],
) -> float:
    area = rectangle_area(inner)
    return rectangle_intersection_area(inner, outer) / area if area > 0 else 0.0


def current_ancestry_supports_outer_anchor(
    record: dict[str, Any],
    anchor: PaneAnchor,
    selected_depth: int,
) -> tuple[bool, dict[str, Any] | None, float]:
    ancestors = (record.get("accessibilitySurface") or {}).get("ancestors")
    if not isinstance(ancestors, list):
        return False, None, 0.0
    anchor_area = rectangle_area(anchor.normalized_top_rectangle)
    for node in ancestors:
        if not isinstance(node, dict):
            continue
        depth = node.get("depth")
        if not isinstance(depth, int) or depth <= selected_depth:
            continue
        rectangle = normalized_node_rectangle(record, node)
        if rectangle is None:
            continue
        node_area = rectangle_area(rectangle)
        if anchor_area <= 0 or node_area / anchor_area \
                < MINIMUM_OUTER_ANCESTOR_AREA_RATIO:
            continue
        containment = containment_over_inner(rectangle, anchor.normalized_top_rectangle)
        reverse = containment_over_inner(anchor.normalized_top_rectangle, rectangle)
        if max(containment, reverse) >= MINIMUM_CONTAINMENT_OVER_INNER_AREA:
            return True, node, max(containment, reverse)
    return False, None, 0.0


class PaneResolver(V6PaneResolver):
    """Preserve a proven outer pane across one duplicate nested AX probe.

    This is deliberately narrower than general pane inheritance. It applies
    only to near-simultaneous screen/visual sensor observations at the same
    point, when the new selected pane is strictly nested and its AX ancestry
    independently preserves the established outer geometry.
    """

    def __init__(self) -> None:
        super().__init__(rule_version=SURFACE_RULE_VERSION)
        self._anchor_records: dict[tuple[str, str, str], dict[str, Any]] = {}

    def _canonicalize_direct(
        self,
        record: dict[str, Any],
        review: dict[str, Any],
        anchor: PaneAnchor,
    ) -> bool:
        prior_record = self._anchor_records.get(anchor.window_key)
        proposal = review.get("proposal")
        if prior_record is None or not isinstance(proposal, dict):
            return False
        record_types = {
            prior_record.get("recordType"), record.get("recordType"),
        }
        if record_types != {
            "screen_ocr_observation", "visual_ocr_observation",
        }:
            return False
        prior_at = timestamp(prior_record.get("capturedAt"))
        current_at = timestamp(record.get("capturedAt"))
        if prior_at is None or current_at is None:
            return False
        interval = (current_at - prior_at).total_seconds()
        if not 0 <= interval <= MAXIMUM_CROSS_SENSOR_INTERVAL_SECONDS:
            return False
        prior_point = normalized_pointer(prior_record)
        current_point = normalized_pointer(record)
        if prior_point is None or current_point is None:
            return False
        pointer_distance = hypot(
            prior_point["x"] - current_point["x"],
            prior_point["y"] - current_point["y"],
        )
        if pointer_distance > MAXIMUM_NORMALIZED_POINTER_DISTANCE:
            return False
        selected_role = proposal.get("selectedRole")
        selected_subrole = proposal.get("selectedSubrole")
        if (
            selected_role in DISTINCT_SEMANTIC_ROOT_ROLES
            or selected_subrole in DISTINCT_SEMANTIC_ROOT_SUBROLES
        ):
            return False
        if selected_subrole not in CANONICALIZABLE_DESCENDANT_SUBROLES:
            return False
        current_rectangle = proposal.get("normalizedTopRectangle")
        selected_depth = proposal.get("selectedDepth")
        if not isinstance(current_rectangle, dict) or not isinstance(selected_depth, int):
            return False
        anchor_area = rectangle_area(anchor.normalized_top_rectangle)
        current_area = rectangle_area(current_rectangle)
        if (
            current_area <= 0
            or anchor_area / current_area < MINIMUM_OUTER_TO_INNER_AREA_RATIO
            or containment_over_inner(
                current_rectangle, anchor.normalized_top_rectangle,
            ) < MINIMUM_CONTAINMENT_OVER_INNER_AREA
        ):
            return False
        supported, supporting_node, geometry_match = \
            current_ancestry_supports_outer_anchor(
                record, anchor, selected_depth,
            )
        if not supported or supporting_node is None:
            return False

        attempted_current = {
            key: deepcopy(proposal.get(key)) for key in (
                "method", "confidence", "reason", "selectedDepth",
                "selectedRole", "selectedSubrole", "selectedLabel",
                "normalizedTopRectangle", "regionOfInterest",
            )
        }
        pixel_width = float(review["image"]["width"])
        pixel_height = float(review["image"]["height"])
        normalized = deepcopy(anchor.normalized_top_rectangle)
        screen = screen_rectangle(normalized, pixel_width, pixel_height)
        comparison_normalized = inset_vertical(normalized)
        comparison_screen = screen_rectangle(
            comparison_normalized, pixel_width, pixel_height,
        )
        proposal.update({
            "method": "canonicalized_prior_outer_ax_pane",
            "confidence": "canonicalized",
            "reason": "nested_cross_sensor_descendant_reused_outer_pane",
            "resolution": "canonicalized_prior_outer_pane",
            "selectedDepth": anchor.selected_depth,
            "selectedRole": anchor.selected_role,
            "selectedSubrole": anchor.selected_subrole,
            "selectedLabel": anchor.selected_label,
            "selectedTitle": anchor.selected_title,
            "selectedDescription": anchor.selected_description,
            "selectedIdentifier": anchor.selected_identifier,
            "screenRectangle": screen,
            "normalizedTopRectangle": normalized,
            "regionOfInterest": normalized_top_to_vision_region(normalized),
            "comparisonScreenRectangle": comparison_screen,
            "comparisonNormalizedTopRectangle": comparison_normalized,
            "comparisonRegionOfInterest": normalized_top_to_vision_region(
                comparison_normalized
            ),
            "isV1Fallback": False,
            "paneIdentity": anchor.identity,
            "physicalPointerClassification": "nested_same_surface_probe",
            "canonicalization": {
                "sourceRecordID": anchor.source_record_id,
                "sourceCapturedAt": anchor.captured_at,
                "policy": "near_simultaneous_cross_sensor_nested_pane_v1",
                "intervalSeconds": round(interval, 6),
                "normalizedPointerDistance": round(pointer_distance, 8),
                "nestedContainment": containment_over_inner(
                    current_rectangle, anchor.normalized_top_rectangle,
                ),
                "outerAncestorGeometryMatch": geometry_match,
                "supportingAncestorDepth": supporting_node.get("depth"),
                "supportingAncestorRole": supporting_node.get("role"),
                "observedSelection": attempted_current,
            },
        })
        return True

    def resolve(self, record: dict[str, Any]) -> dict[str, Any]:
        review = super().resolve(record)
        key = window_key(record)
        proposal = review.get("proposal", {})
        if (
            key is not None
            and proposal.get("resolved") is True
            and proposal.get("resolution") in {
                "current_ax_pane", "canonicalized_prior_outer_pane",
            }
        ):
            self._anchor_records[key] = deepcopy(record)
        return review


__all__ = [
    "PaneResolver", "PROPOSAL_RULE_VERSION", "SURFACE_RULE_VERSION",
    "surface_regions_from_review",
]

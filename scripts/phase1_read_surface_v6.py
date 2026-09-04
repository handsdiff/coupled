#!/usr/bin/env python3
"""Stateful AX pane recovery without an authoritative pointer-crop fallback."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable

from phase1_read_surface_v2 import normalized_top_to_vision_region
from phase1_read_surface_v4 import inset_vertical
from phase1_read_surface_v5 import proposal_for_record as v5_proposal_for_record


SURFACE_RULE_VERSION = "ax-pane-read-v6"
PROPOSAL_RULE_VERSION = SURFACE_RULE_VERSION

# These roles describe application chrome or manipulation affordances, not a
# semantic document/chat/terminal pane. They may be the physical interaction
# target, but must not replace the last proven content anchor.
APPLICATION_CHROME_ROLES = {
    "AXMenuBar",
    "AXMenuBarItem",
    "AXScrollBar",
    "AXTab",
    "AXTabGroup",
    "AXToolbar",
}
APPLICATION_CHROME_SUBROLES = {
    "AXApplicationStatus",
}

# Some Electron windows expose the bottom status strip only as generic
# AXGroups.  A pointer this close to the bottom window edge is still strong
# evidence that the failed probe describes application chrome rather than a
# new content pane.  Keep this deliberately one-sided: the top of a document
# can contain real content and must not be inferred to be chrome from geometry
# alone.
EXTREME_BOTTOM_CHROME_Y = 0.96
EXTREME_TOP_CHROME_Y = 0.10

SEMANTIC_CONTENT_ROLES = {
    "AXCodeStyleGroup",
    "AXHeading",
    "AXImage",
    "AXLink",
    "AXList",
    "AXStaticText",
    "AXTextArea",
    "AXTextField",
    "AXWebArea",
}

RETAINED_SEMANTIC_POINT_REASONS = {
    "retained_prior_content_anchor",
    "retained_prior_semantic_content_point",
}

# v2-v5 limited every pane to 82% of the window.  A browser AXWebArea commonly
# occupies ~86% because it correctly excludes only the toolbar.  Treat these
# semantically named document surfaces as direct evidence instead of allowing
# that historical area cap to turn them into pointer-crop fallbacks.
DOCUMENT_SURFACE_PRIORITIES = {
    "AXDocumentArticle": 300,
    "AXLandmarkMain": 200,
    "AXWebArea": 100,
}


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def numeric(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def window_key(record: dict[str, Any]) -> tuple[str, str, str] | None:
    bundle = record.get("bundleIdentifier")
    process = record.get("processIdentifier")
    window = record.get("windowID")
    if not isinstance(bundle, str) or not bundle or process is None or window is None:
        return None
    return bundle, str(process), str(window)


def normalized_pointer(record: dict[str, Any]) -> dict[str, float] | None:
    bounds = record.get("windowBounds")
    if not isinstance(bounds, dict):
        return None
    values = [numeric(value) for value in (
        record.get("x"), record.get("y"), bounds.get("x"), bounds.get("y"),
        bounds.get("width"), bounds.get("height"),
    )]
    if any(value is None for value in values):
        return None
    x, y, left, top, width, height = values
    if width <= 0 or height <= 0:
        return None
    return {
        "x": round((x - left) / width, 8),
        "y": round((y - top) / height, 8),
    }


def pointer_is_application_chrome(record: dict[str, Any]) -> tuple[bool, str | None]:
    probe = record.get("accessibilitySurface")
    ancestors = probe.get("ancestors") if isinstance(probe, dict) else None
    if isinstance(ancestors, list):
        for node in ancestors:
            if not isinstance(node, dict):
                continue
            role = node.get("role")
            subrole = node.get("subrole")
            if subrole in APPLICATION_CHROME_SUBROLES:
                return True, str(subrole)
            if role in APPLICATION_CHROME_ROLES:
                return True, str(role)

    point = normalized_pointer(record)
    if point is not None and point["y"] <= EXTREME_TOP_CHROME_Y:
        roles = {
            node.get("role") for node in ancestors if isinstance(node, dict)
        } if isinstance(ancestors, list) else set()
        if roles.isdisjoint(SEMANTIC_CONTENT_ROLES):
            return True, "extreme_top_window_chrome"
    if point is not None and point["y"] >= EXTREME_BOTTOM_CHROME_Y:
        roles = {
            node.get("role") for node in ancestors if isinstance(node, dict)
        } if isinstance(ancestors, list) else set()
        if roles.isdisjoint(SEMANTIC_CONTENT_ROLES):
            return True, "extreme_bottom_window_chrome"
    return False, None


def retained_semantic_point(record: dict[str, Any]) -> tuple[bool, str | None]:
    reason = record.get("semanticContentPointReason")
    if not isinstance(reason, str):
        return False, None
    if reason in RETAINED_SEMANTIC_POINT_REASONS:
        return True, reason
    return False, None


def pointer_inside_anchor(record: dict[str, Any], anchor: PaneAnchor) -> bool:
    point = normalized_pointer(record)
    if point is None:
        return False
    region = anchor.normalized_top_rectangle
    return (
        region["x"] <= point["x"] <= region["x"] + region["width"]
        and region["y"] <= point["y"] <= region["y"] + region["height"]
    )


def current_ax_is_compatible_with_anchor(
    record: dict[str, Any], anchor: PaneAnchor,
) -> bool:
    """Require spatial continuity plus matching AX pane semantics.

    Exact window identity alone is intentionally insufficient: Electron can
    host an editor, terminal, and agent chat inside one window.  A failed
    content probe may reuse an anchor only when the current point remains
    inside that pane and the AX chain still exposes the anchor's role (and,
    when available, its semantic subrole).
    """
    if not pointer_inside_anchor(record, anchor):
        return False
    ancestors = (record.get("accessibilitySurface") or {}).get("ancestors")
    if not isinstance(ancestors, list) or not ancestors:
        return False
    matching = [
        node for node in ancestors
        if isinstance(node, dict) and node.get("role") == anchor.selected_role
    ]
    if not matching:
        return False
    if anchor.selected_subrole:
        matching = [
            node for node in matching
            if node.get("subrole") == anchor.selected_subrole
        ]
        if not matching:
            return False
    if anchor.selected_role != "AXGroup":
        return True

    # AXGroup is ubiquitous in Electron and does not identify an editor,
    # terminal, or chat pane by itself.  Require a stable semantic label or
    # matching pane geometry before recovering a generic-group anchor.
    for node in matching:
        current_identifier = node.get("identifier")
        if (
            anchor.selected_identifier
            and current_identifier == anchor.selected_identifier
        ):
            return True
        current_label = (
            node.get("identifier") or node.get("title")
            or node.get("elementDescription")
        )
        if anchor.selected_label and current_label == anchor.selected_label:
            return True
        current_region = normalized_node_rectangle(record, node)
        if current_region is not None and rectangles_match(
            current_region, anchor.normalized_top_rectangle,
        ):
            return True
    return False


def normalized_node_rectangle(
    record: dict[str, Any], node: dict[str, Any],
) -> dict[str, float] | None:
    bounds = record.get("windowBounds")
    frame = node.get("frame")
    if not isinstance(bounds, dict) or not isinstance(frame, dict):
        return None
    values = [numeric(value) for value in (
        frame.get("x"), frame.get("y"), frame.get("width"), frame.get("height"),
        bounds.get("x"), bounds.get("y"), bounds.get("width"), bounds.get("height"),
    )]
    if any(value is None for value in values):
        return None
    x, y, width, height, left, top, window_width, window_height = values
    if width <= 0 or height <= 0 or window_width <= 0 or window_height <= 0:
        return None
    return {
        "x": (x - left) / window_width,
        "y": (y - top) / window_height,
        "width": width / window_width,
        "height": height / window_height,
    }


def rectangles_match(
    left: dict[str, float], right: dict[str, float], *, tolerance: float = 0.01,
) -> bool:
    return all(
        abs(left[key] - right[key]) <= tolerance
        for key in ("x", "y", "width", "height")
    )


def selected_node(review: dict[str, Any]) -> dict[str, Any] | None:
    proposal = review.get("proposal")
    ancestors = review.get("ancestors")
    if not isinstance(proposal, dict) or not isinstance(ancestors, list):
        return None
    depth = proposal.get("selectedDepth")
    return next(
        (
            node for node in ancestors
            if isinstance(node, dict) and node.get("depth") == depth
        ),
        None,
    )


def screen_rectangle(
    normalized: dict[str, float], pixel_width: float, pixel_height: float,
) -> dict[str, float]:
    return {
        "x": round(normalized["x"] * pixel_width, 4),
        "y": round(normalized["y"] * pixel_height, 4),
        "width": round(normalized["width"] * pixel_width, 4),
        "height": round(normalized["height"] * pixel_height, 4),
    }


def pane_identity(selection: dict[str, Any]) -> str:
    return "pane_" + digest({
        "role": selection.get("selectedRole"),
        "subrole": selection.get("selectedSubrole"),
        "label": selection.get("selectedLabel"),
        "region": selection.get("normalizedTopRectangle"),
    })


def promote_current_document_surface(review: dict[str, Any]) -> bool:
    proposal = review.get("proposal")
    ancestors = review.get("ancestors")
    if (
        not isinstance(proposal, dict)
        or not proposal.get("isV1Fallback")
        or not isinstance(ancestors, list)
    ):
        return False

    candidates: list[tuple[int, float, int, dict[str, Any], str]] = []
    for node in ancestors:
        if not isinstance(node, dict) or not node.get("containsPointer"):
            continue
        surface_kind = next(
            (
                value for value in (node.get("subrole"), node.get("role"))
                if value in DOCUMENT_SURFACE_PRIORITIES
            ),
            None,
        )
        area = numeric(node.get("areaFraction"))
        screen = node.get("screenRectangle")
        normalized = node.get("normalizedTopRectangle")
        depth = node.get("depth")
        if (
            surface_kind is None
            or area is None
            or not 0.15 <= area <= 0.95
            or not isinstance(screen, dict)
            or not isinstance(normalized, dict)
            or not isinstance(depth, int)
        ):
            continue
        candidates.append((
            DOCUMENT_SURFACE_PRIORITIES[surface_kind], -area, -depth,
            node, surface_kind,
        ))

    method = "ax_document_content_surface"
    confidence = "high"
    if candidates:
        _, _, _, selected, surface_kind = max(candidates)
        reason = f"semantic_{surface_kind}"
    else:
        # Chat and document applications sometimes expose no named landmark,
        # but do expose the same large, bounded content container repeatedly in
        # the ancestor chain.  v2-v5 missed the common ~75-86% case because of
        # narrow area/width caps.  A repeated current AX rectangle that excludes
        # at least one window edge is direct pane evidence, not remembered state.
        repeated = [
            node for node in ancestors
            if isinstance(node, dict)
            and node.get("containsPointer")
            and numeric(node.get("areaFraction")) is not None
            and 0.20 <= float(node["areaFraction"]) <= 0.90
            and numeric(node.get("widthFraction")) is not None
            and numeric(node.get("heightFraction")) is not None
            and float(node["widthFraction"]) >= 0.50
            and float(node["heightFraction"]) >= 0.40
            and (
                float(node["widthFraction"]) < 0.98
                or float(node["heightFraction"]) < 0.98
            )
            and int(node.get("nearAdjacentFrameCount") or 0) >= 1
            and isinstance(node.get("screenRectangle"), dict)
            and isinstance(node.get("normalizedTopRectangle"), dict)
            and isinstance(node.get("depth"), int)
        ]
        if not repeated:
            return False
        selected = min(
            repeated,
            key=lambda node: (float(node["areaFraction"]), int(node["depth"])),
        )
        surface_kind = "repeated_outer_content_pane"
        method = "ax_repeated_outer_content_pane"
        confidence = "medium"
        reason = "repeated_large_bounded_content_pane"
    full_screen = deepcopy(selected["screenRectangle"])
    full_normalized = deepcopy(selected["normalizedTopRectangle"])
    comparison_screen = inset_vertical(full_screen)
    comparison_normalized = inset_vertical(full_normalized)
    proposal.update({
        "method": method,
        "confidence": confidence,
        "reason": reason,
        "selectedDepth": selected.get("depth"),
        "selectedRole": selected.get("role"),
        "selectedSubrole": selected.get("subrole"),
        "screenRectangle": full_screen,
        "normalizedTopRectangle": full_normalized,
        "regionOfInterest": normalized_top_to_vision_region(full_normalized),
        "comparisonScreenRectangle": comparison_screen,
        "comparisonNormalizedTopRectangle": comparison_normalized,
        "comparisonRegionOfInterest": normalized_top_to_vision_region(
            comparison_normalized
        ),
        "isV1Fallback": False,
    })
    return True


@dataclass(frozen=True)
class PaneAnchor:
    source_record_id: str
    captured_at: str
    window_key: tuple[str, str, str]
    normalized_top_rectangle: dict[str, float]
    semantic_point_normalized_top: dict[str, float] | None
    method: str
    confidence: str
    reason: str
    selected_depth: Any
    selected_role: Any
    selected_subrole: Any
    selected_label: Any
    selected_title: Any
    selected_description: Any
    selected_identifier: Any
    identity: str


class PaneResolver:
    """Resolve records in capture-time order while retaining per-window panes."""

    def __init__(self, *, rule_version: str = SURFACE_RULE_VERSION) -> None:
        self._rule_version = rule_version
        self._anchors: dict[tuple[str, str, str], PaneAnchor] = {}

    def _canonicalize_direct(
        self,
        record: dict[str, Any],
        review: dict[str, Any],
        anchor: PaneAnchor,
    ) -> bool:
        """Version hook for stricter descendants; v6 never canonicalizes."""
        return False

    def resolve(self, record: dict[str, Any]) -> dict[str, Any]:
        review = deepcopy(v5_proposal_for_record(record))
        review["ruleVersion"] = self._rule_version
        proposal = review["proposal"]
        attempted = {
            "method": proposal.get("method"),
            "confidence": proposal.get("confidence"),
            "reason": proposal.get("reason"),
            "isV1Fallback": proposal.get("isV1Fallback"),
        }
        promote_current_document_surface(review)
        key = window_key(record)
        chrome, chrome_role = pointer_is_application_chrome(record)
        retained_point, retained_point_reason = retained_semantic_point(record)
        direct = not bool(proposal.get("isV1Fallback")) and not chrome
        source_record_id = str(record.get("recordID") or "")
        captured_at = str(record.get("capturedAt") or "")
        anchor = self._anchors.get(key) if key is not None else None
        canonicalized = bool(
            direct
            and anchor is not None
            and self._canonicalize_direct(record, review, anchor)
        )

        if direct:
            node = {} if canonicalized else selected_node(review) or {}
            proposal.update({
                "ruleVersion": self._rule_version,
                "resolved": True,
                "resolution": proposal.get("resolution") or "current_ax_pane",
                "selectedLabel": (
                    proposal.get("selectedLabel") if canonicalized else
                    node.get("identifier") or node.get("title")
                    or node.get("elementDescription")
                ),
                "selectedTitle": proposal.get("selectedTitle")
                    if canonicalized else node.get("title"),
                "selectedDescription": proposal.get("selectedDescription")
                    if canonicalized else node.get("elementDescription"),
                "selectedIdentifier": proposal.get("selectedIdentifier")
                    if canonicalized else node.get("identifier"),
                "semanticPointNormalizedTop": normalized_pointer(record),
                "physicalPointerClassification": proposal.get(
                    "physicalPointerClassification", "content_or_unclassified"
                ),
                "attemptedSelection": attempted,
            })
            if not canonicalized:
                proposal["paneIdentity"] = pane_identity(proposal)
            if key is not None:
                self._anchors[key] = PaneAnchor(
                    source_record_id=source_record_id,
                    captured_at=captured_at,
                    window_key=key,
                    normalized_top_rectangle=deepcopy(
                        proposal["normalizedTopRectangle"]
                    ),
                    semantic_point_normalized_top=deepcopy(
                        proposal.get("semanticPointNormalizedTop")
                    ),
                    method=str(proposal.get("method") or ""),
                    confidence=str(proposal.get("confidence") or ""),
                    reason=str(proposal.get("reason") or ""),
                    selected_depth=proposal.get("selectedDepth"),
                    selected_role=proposal.get("selectedRole"),
                    selected_subrole=proposal.get("selectedSubrole"),
                    selected_label=proposal.get("selectedLabel"),
                    selected_title=proposal.get("selectedTitle"),
                    selected_description=proposal.get("selectedDescription"),
                    selected_identifier=proposal.get("selectedIdentifier"),
                    identity=str(proposal["paneIdentity"]),
                )
            return review

        current_ax_compatible = (
            anchor is not None
            and current_ax_is_compatible_with_anchor(record, anchor)
        )
        recovery_is_compatible = chrome or retained_point or current_ax_compatible
        if anchor is None or not recovery_is_compatible:
            proposal.update({
                "ruleVersion": self._rule_version,
                "resolved": False,
                "resolution": "unresolved",
                "method": "unresolved",
                "confidence": "unresolved",
                "reason": (
                    "window_identity_unavailable_for_pane_recovery"
                    if key is None else
                    "no_trustworthy_current_or_prior_ax_pane"
                    if anchor is None else
                    "failed_content_probe_not_safe_for_pane_recovery"
                ),
                "screenRectangle": None,
                "normalizedTopRectangle": None,
                "regionOfInterest": None,
                "comparisonScreenRectangle": None,
                "comparisonNormalizedTopRectangle": None,
                "comparisonRegionOfInterest": None,
                "isV1Fallback": False,
                "physicalPointerClassification": (
                    f"application_chrome:{chrome_role}" if chrome else
                    f"retained_semantic_point:{retained_point_reason}"
                    if retained_point else
                    "inside_compatible_ax_pane" if current_ax_compatible else
                    "unresolved"
                ),
                "attemptedSelection": attempted,
            })
            return review

        pixel_width = float(review["image"]["width"])
        pixel_height = float(review["image"]["height"])
        normalized = deepcopy(anchor.normalized_top_rectangle)
        screen = screen_rectangle(normalized, pixel_width, pixel_height)
        comparison_normalized = inset_vertical(normalized)
        comparison_screen = screen_rectangle(
            comparison_normalized, pixel_width, pixel_height
        )
        proposal.update({
            "ruleVersion": self._rule_version,
            "resolved": True,
            "resolution": "recovered_prior_window_pane",
            "method": "recovered_prior_ax_pane",
            "confidence": "recovered",
            "reason": "current_ax_unresolved_reused_last_proven_same_window_pane",
            "selectedDepth": None,
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
            "semanticPointNormalizedTop": deepcopy(
                anchor.semantic_point_normalized_top
            ),
            "physicalPointerClassification": (
                f"application_chrome:{chrome_role}" if chrome else
                f"retained_semantic_point:{retained_point_reason}"
                if retained_point else "inside_compatible_ax_pane"
            ),
            "attemptedSelection": attempted,
            "recovery": {
                "sourceRecordID": anchor.source_record_id,
                "capturedAt": anchor.captured_at,
                "sourceMethod": anchor.method,
                "sourceConfidence": anchor.confidence,
                "sourceReason": anchor.reason,
                "sourceSelectedDepth": anchor.selected_depth,
                "sameCanonicalWindow": True,
            },
        })
        return review

    def resolve_records(
        self, records: Iterable[tuple[int, dict[str, Any]]],
    ) -> dict[str, dict[str, Any]]:
        ordered = sorted(
            records,
            key=lambda item: (
                str(item[1].get("capturedAt") or ""), item[0],
                str(item[1].get("recordID") or ""),
            ),
        )
        output: dict[str, dict[str, Any]] = {}
        for _, record in ordered:
            record_id = record.get("recordID")
            if isinstance(record_id, str) and record_id:
                output[record_id] = self.resolve(record)
        return output


def surface_regions_from_review(
    review: dict[str, Any],
) -> tuple[dict[str, float], dict[str, float], dict[str, Any]] | None:
    proposal = review["proposal"]
    if not proposal.get("resolved"):
        return None
    full = proposal["regionOfInterest"]
    comparison = proposal["comparisonRegionOfInterest"]
    selection_keys = (
        "ruleVersion", "method", "confidence", "reason", "resolved",
        "resolution", "selectedDepth", "selectedRole", "selectedSubrole",
        "selectedLabel", "selectedTitle", "selectedDescription",
        "selectedIdentifier", "isV1Fallback", "paneIdentity",
        "semanticPointNormalizedTop", "physicalPointerClassification",
        "attemptedSelection", "recovery", "canonicalization", "paneCrop",
    )
    selection = {
        key: deepcopy(proposal[key]) for key in selection_keys if key in proposal
    }
    selection["regionOfInterest"] = full
    selection["comparisonRegionOfInterest"] = comparison
    return full, comparison, selection

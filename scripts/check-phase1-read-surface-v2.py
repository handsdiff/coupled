#!/usr/bin/env python3
"""Focused checks for conservative Phase 1 AX pane proposals."""

from __future__ import annotations

from phase1_read_surface_v2 import (
    SURFACE_RULE_VERSION,
    normalized_top_to_vision_region,
    proposal_for_record,
    surface_region,
)


def record(ancestors: list[dict], pointer: tuple[float, float] = (500, 500)) -> dict:
    return {
        "bundleIdentifier": "test.bundle",
        "windowBounds": {"x": 0, "y": 0, "width": 1000, "height": 1000},
        "screenshotPixelWidth": 1000,
        "screenshotPixelHeight": 1000,
        "x": pointer[0],
        "y": pointer[1],
        "triggerTypes": ["scroll"],
        "accessibilitySurface": {"ancestors": ancestors},
    }


def node(depth: int, role: str, frame: tuple[float, float, float, float], subrole: str | None = None) -> dict:
    value = {
        "depth": depth,
        "role": role,
        "frame": dict(zip(("x", "y", "width", "height"), frame)),
    }
    if subrole:
        value["subrole"] = subrole
    return value


def main() -> int:
    assert normalized_top_to_vision_region({
        "x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4,
    }) == {"x": 0.1, "y": 0.4, "width": 0.3, "height": 0.4}

    semantic = proposal_for_record(record([
        node(0, "AXStaticText", (300, 450, 400, 30)),
        node(1, "AXGroup", (150, 100, 700, 800), "AXLandmarkMain"),
        node(2, "AXGroup", (0, 0, 1000, 1000)),
    ]))
    assert semantic["proposal"]["method"] == "ax_semantic_container"
    assert semantic["proposal"]["selectedDepth"] == 1
    assert semantic["proposal"]["regionOfInterest"] == {
        "x": 0.15, "y": 0.1, "width": 0.7, "height": 0.8,
    }
    semantic_region, semantic_selection = surface_region(record([
        node(0, "AXStaticText", (300, 450, 400, 30)),
        node(1, "AXGroup", (150, 100, 700, 800), "AXLandmarkMain"),
        node(2, "AXGroup", (0, 0, 1000, 1000)),
    ]))
    assert semantic_region == semantic["proposal"]["regionOfInterest"]
    assert semantic_selection["ruleVersion"] == SURFACE_RULE_VERSION
    assert semantic_selection["method"] == "ax_semantic_container"
    assert "ancestors" not in semantic_selection

    sidebar = proposal_for_record(record([
        node(0, "AXGroup", (20, 80, 200, 850)),
        node(1, "AXGroup", (20, 75, 200, 860)),
        node(2, "AXGroup", (0, 0, 1000, 1000)),
    ], pointer=(100, 500)))
    assert sidebar["proposal"]["method"] == "ax_repeated_vertical_pane"

    repeated_bounded_surface = proposal_for_record(record([
        node(0, "AXImage", (100, 500, 400, 450)),
        node(1, "AXGroup", (100, 500, 400, 450)),
        node(2, "AXGroup", (0, 0, 1000, 1000)),
    ], pointer=(300, 700)))
    assert repeated_bounded_surface["proposal"]["method"] == "ax_repeated_bounded_surface"

    small_ambiguous_surface = proposal_for_record(record([
        node(0, "AXImage", (100, 600, 400, 300)),
        node(1, "AXGroup", (100, 600, 400, 300)),
        node(2, "AXGroup", (0, 0, 1000, 1000)),
    ], pointer=(300, 700)))
    assert small_ambiguous_surface["proposal"]["method"] == "v1_fallback"

    outer_container = proposal_for_record(record([
        node(0, "AXGroup", (250, 400, 500, 120)),
        node(1, "AXGroup", (50, 50, 900, 900)),
        node(2, "AXGroup", (0, 0, 1000, 1000)),
    ]))
    assert outer_container["proposal"]["method"] == "ax_nearest_outer_container"
    assert outer_container["proposal"]["selectedDepth"] == 1

    tiny_control = proposal_for_record(record([
        node(0, "AXTextArea", (450, 450, 80, 40)),
        node(1, "AXGroup", (0, 0, 1000, 1000)),
    ], pointer=(500, 470)))
    assert tiny_control["proposal"]["method"] == "v1_fallback"

    print("Phase 1 read-surface-v2 proposal checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Checks for conservative cross-sensor nested-pane canonicalization."""

from __future__ import annotations

from phase1_read_surface_v7 import PaneResolver, surface_regions_from_review


def record(
    record_id: str,
    *,
    record_type: str,
    captured: str,
    x: float = 700,
    y: float = 650,
    window_id: int = 20,
    ancestors: list[dict],
) -> dict:
    return {
        "recordID": record_id,
        "recordType": record_type,
        "capturedAt": captured,
        "bundleIdentifier": "com.openai.codex",
        "processIdentifier": 10,
        "windowID": window_id,
        "windowTitle": "ChatGPT",
        "windowBounds": {"x": 0, "y": 0, "width": 1000, "height": 1000},
        "screenshotPixelWidth": 2000,
        "screenshotPixelHeight": 2000,
        "x": x,
        "y": y,
        "accessibilitySurface": {"ancestors": ancestors},
    }


def outer_main() -> list[dict]:
    return [
        {
            "depth": 0, "role": "AXGroup",
            "frame": {"x": 300, "y": 80, "width": 700, "height": 900},
        },
        {
            "depth": 1, "role": "AXGroup", "subrole": "AXLandmarkMain",
            "frame": {"x": 200, "y": 0, "width": 800, "height": 1000},
        },
    ]


def nested_content_list() -> list[dict]:
    return [
        {
            "depth": 0, "role": "AXStaticText",
            "frame": {"x": 500, "y": 620, "width": 300, "height": 30},
        },
        {
            "depth": 1, "role": "AXList", "subrole": "AXContentList",
            "frame": {"x": 400, "y": 400, "width": 500, "height": 400},
        },
        {
            "depth": 2, "role": "AXGroup",
            "frame": {"x": 300, "y": 60, "width": 700, "height": 920},
        },
        {
            "depth": 3, "role": "AXGroup",
            "frame": {"x": 200, "y": 0, "width": 800, "height": 1000},
        },
    ]


def distinct_editor() -> list[dict]:
    return [
        {
            "depth": 0, "role": "AXTextArea",
            "frame": {"x": 400, "y": 400, "width": 500, "height": 400},
            "identifier": "terminal-input",
        },
        {
            "depth": 1, "role": "AXGroup",
            "frame": {"x": 200, "y": 0, "width": 800, "height": 1000},
        },
    ]


def nested_generic_group() -> list[dict]:
    return [
        {
            "depth": 0, "role": "AXStaticText",
            "frame": {"x": 500, "y": 620, "width": 300, "height": 30},
        },
        {
            "depth": 1, "role": "AXGroup",
            "frame": {"x": 400, "y": 400, "width": 500, "height": 400},
        },
        {
            "depth": 2, "role": "AXGroup",
            "frame": {"x": 200, "y": 0, "width": 800, "height": 1000},
        },
    ]


def main() -> int:
    resolver = PaneResolver()
    outer = resolver.resolve(record(
        "outer", record_type="screen_ocr_observation",
        captured="2026-01-01T00:00:01.000Z", ancestors=outer_main(),
    ))
    outer_regions = surface_regions_from_review(outer)
    assert outer_regions is not None
    outer_full, _, outer_selection = outer_regions
    assert outer_selection["selectedSubrole"] == "AXLandmarkMain"

    nested = resolver.resolve(record(
        "nested", record_type="visual_ocr_observation",
        captured="2026-01-01T00:00:01.600Z", ancestors=nested_content_list(),
    ))
    nested_regions = surface_regions_from_review(nested)
    assert nested_regions is not None
    nested_full, _, nested_selection = nested_regions
    assert nested_full == outer_full
    assert nested_selection["paneIdentity"] == outer_selection["paneIdentity"]
    assert nested_selection["method"] == "canonicalized_prior_outer_ax_pane"
    assert nested_selection["resolution"] == "canonicalized_prior_outer_pane"
    assert nested_selection["canonicalization"]["sourceRecordID"] == "outer"
    assert nested_selection["canonicalization"]["observedSelection"][
        "selectedSubrole"
    ] == "AXContentList"

    # A later nested interaction is not assumed to be the same sensor state.
    delayed = resolver.resolve(record(
        "delayed", record_type="visual_ocr_observation",
        captured="2026-01-01T00:00:04.000Z", ancestors=nested_content_list(),
    ))
    delayed_regions = surface_regions_from_review(delayed)
    assert delayed_regions is not None
    assert delayed_regions[2]["method"] == "ax_semantic_container"
    assert delayed_regions[2]["selectedSubrole"] == "AXContentList"

    # A receiving/editor surface is distinct even inside the same outer pane.
    distinct_resolver = PaneResolver()
    distinct_resolver.resolve(record(
        "outer-before-editor", record_type="screen_ocr_observation",
        captured="2026-01-01T00:00:01.000Z", ancestors=outer_main(),
    ))
    editor = distinct_resolver.resolve(record(
        "editor", record_type="visual_ocr_observation",
        captured="2026-01-01T00:00:01.400Z", ancestors=distinct_editor(),
    ))
    editor_regions = surface_regions_from_review(editor)
    assert editor_regions is not None
    assert editor_regions[2]["selectedRole"] == "AXTextArea"
    assert editor_regions[2]["method"] != "canonicalized_prior_outer_ax_pane"

    # A generic nested group is not enough evidence to widen the pane.  The
    # production trace contains such layout changes, and widening one exposed
    # noisy composer text without improving event reconciliation.
    generic_resolver = PaneResolver()
    generic_resolver.resolve(record(
        "outer-before-generic", record_type="screen_ocr_observation",
        captured="2026-01-01T00:00:01.000Z", ancestors=outer_main(),
    ))
    generic = generic_resolver.resolve(record(
        "generic", record_type="visual_ocr_observation",
        captured="2026-01-01T00:00:01.400Z", ancestors=nested_generic_group(),
    ))
    generic_regions = surface_regions_from_review(generic)
    if generic_regions is not None:
        assert generic_regions[2]["selectedRole"] == "AXGroup"
        assert generic_regions[2]["method"] \
            != "canonicalized_prior_outer_ax_pane"
    else:
        assert generic["proposal"]["method"] \
            != "canonicalized_prior_outer_ax_pane"

    # Pane memory cannot bridge a materially different pointer location.
    moved_resolver = PaneResolver()
    moved_resolver.resolve(record(
        "outer-before-move", record_type="screen_ocr_observation",
        captured="2026-01-01T00:00:01.000Z", ancestors=outer_main(),
    ))
    moved = moved_resolver.resolve(record(
        "nested-after-move", record_type="visual_ocr_observation",
        captured="2026-01-01T00:00:01.400Z", x=850,
        ancestors=nested_content_list(),
    ))
    assert moved["proposal"]["method"] \
        != "canonicalized_prior_outer_ax_pane"

    # Pane memory is strictly local to one canonical application window.
    window_resolver = PaneResolver()
    window_resolver.resolve(record(
        "outer-window-20", record_type="screen_ocr_observation",
        captured="2026-01-01T00:00:01.000Z", ancestors=outer_main(),
    ))
    other_window = window_resolver.resolve(record(
        "nested-window-21", record_type="visual_ocr_observation",
        captured="2026-01-01T00:00:01.400Z", window_id=21,
        ancestors=nested_content_list(),
    ))
    assert other_window["proposal"]["method"] \
        != "canonicalized_prior_outer_ax_pane"

    print("Phase 1 read-surface-v7 canonical pane checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

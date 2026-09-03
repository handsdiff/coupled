#!/usr/bin/env python3
"""Checks for stateful pane recovery and conservative unresolved evidence."""

from __future__ import annotations

from phase1_read_surface_v6 import PaneResolver, surface_regions_from_review


def record(
    record_id: str,
    *,
    bundle: str = "com.microsoft.VSCode",
    process: int = 10,
    window: int = 20,
    captured: str,
    x: float,
    y: float,
    ancestors: list[dict],
) -> dict:
    return {
        "recordID": record_id,
        "recordType": "screen_ocr_observation",
        "capturedAt": captured,
        "bundleIdentifier": bundle,
        "processIdentifier": process,
        "windowID": window,
        "windowTitle": "fixture",
        "windowBounds": {"x": 0, "y": 0, "width": 1000, "height": 1000},
        "screenshotPixelWidth": 2000,
        "screenshotPixelHeight": 2000,
        "x": x,
        "y": y,
        "accessibilitySurface": {"ancestors": ancestors},
    }


def content_pane(x: int, width: int, role: str) -> list[dict]:
    return [
        {
            "depth": 0, "role": "AXStaticText",
            "frame": {"x": x + 20, "y": 200, "width": 200, "height": 20},
        },
        {
            "depth": 1, "role": role,
            "frame": {"x": x, "y": 100, "width": width, "height": 800},
            "identifier": role.lower(),
        },
        {
            "depth": 2, "role": "AXGroup",
            "frame": {"x": 0, "y": 0, "width": 1000, "height": 1000},
        },
    ]


def status_bar() -> list[dict]:
    return [
        {
            "depth": 0, "role": "AXButton",
            "frame": {"x": 300, "y": 970, "width": 200, "height": 30},
        },
        {
            "depth": 1, "role": "AXGroup", "subrole": "AXApplicationStatus",
            "frame": {"x": 0, "y": 960, "width": 1000, "height": 40},
        },
        {
            "depth": 2, "role": "AXGroup",
            "frame": {"x": 0, "y": 0, "width": 1000, "height": 1000},
        },
    ]


def failed_interior_probe() -> list[dict]:
    return [
        {
            "depth": 0, "role": "AXStaticText",
            "frame": {"x": 450, "y": 400, "width": 20, "height": 20},
        },
        {
            "depth": 1, "role": "AXGroup",
            "frame": {"x": 0, "y": 0, "width": 1000, "height": 1000},
        },
    ]


def browser_document() -> list[dict]:
    return [
        {
            "depth": 0, "role": "AXStaticText",
            "frame": {"x": 300, "y": 300, "width": 400, "height": 40},
        },
        {
            "depth": 1, "role": "AXWebArea",
            "frame": {"x": 0, "y": 120, "width": 1000, "height": 870},
        },
        {
            "depth": 2, "role": "AXGroup",
            "frame": {"x": 0, "y": 0, "width": 1000, "height": 1000},
        },
    ]


def generic_group_pane(x: int, y: int, width: int, height: int) -> list[dict]:
    return [
        {
            "depth": 0, "role": "AXGroup",
            "frame": {"x": x, "y": y, "width": width, "height": height},
        },
        {
            "depth": 1, "role": "AXGroup",
            "frame": {"x": x, "y": y, "width": width, "height": height},
        },
        {
            "depth": 2, "role": "AXGroup",
            "frame": {"x": 0, "y": 0, "width": 1000, "height": 1000},
        },
    ]


def large_repeated_content_pane() -> list[dict]:
    return [
        {
            "depth": 0, "role": "AXTextArea",
            "frame": {"x": 300, "y": 850, "width": 600, "height": 60},
        },
        {
            "depth": 1, "role": "AXGroup",
            "frame": {"x": 200, "y": 60, "width": 800, "height": 940},
        },
        {
            "depth": 2, "role": "AXGroup",
            "frame": {"x": 200, "y": 60, "width": 800, "height": 940},
        },
        {
            "depth": 3, "role": "AXWindow",
            "frame": {"x": 0, "y": 0, "width": 1000, "height": 1000},
        },
    ]


def main() -> int:
    resolver = PaneResolver()
    editor = resolver.resolve(record(
        "editor", captured="2026-01-01T00:00:01Z", x=250, y=400,
        ancestors=content_pane(100, 600, "AXCodeStyleGroup"),
    ))
    editor_regions = surface_regions_from_review(editor)
    assert editor_regions is not None
    editor_full, _, editor_selection = editor_regions
    assert editor_selection["resolution"] == "current_ax_pane"
    assert editor_selection["selectedRole"] == "AXCodeStyleGroup"

    recovered_editor = resolver.resolve(record(
        "status-after-editor", captured="2026-01-01T00:00:02Z", x=400, y=985,
        ancestors=status_bar(),
    ))
    recovered_regions = surface_regions_from_review(recovered_editor)
    assert recovered_regions is not None
    recovered_full, _, recovered_selection = recovered_regions
    assert recovered_full == editor_full
    assert recovered_selection["method"] == "recovered_prior_ax_pane"
    assert recovered_selection["recovery"]["sourceRecordID"] == "editor"
    assert recovered_selection["physicalPointerClassification"] \
        == "application_chrome:AXApplicationStatus"
    assert recovered_selection["semanticPointNormalizedTop"] == {"x": 0.25, "y": 0.4}

    # A genuine content interaction in a distinct pane replaces the remembered
    # pane. Later chrome interaction must recover the terminal, not the editor.
    terminal = resolver.resolve(record(
        "terminal", captured="2026-01-01T00:00:03Z", x=650, y=700,
        ancestors=content_pane(500, 450, "AXTextArea"),
    ))
    terminal_regions = surface_regions_from_review(terminal)
    assert terminal_regions is not None
    terminal_full, _, terminal_selection = terminal_regions
    assert terminal_selection["paneIdentity"] != editor_selection["paneIdentity"]
    after_terminal = resolver.resolve(record(
        "status-after-terminal", captured="2026-01-01T00:00:04Z", x=400, y=985,
        ancestors=status_bar(),
    ))
    after_terminal_regions = surface_regions_from_review(after_terminal)
    assert after_terminal_regions is not None
    assert after_terminal_regions[0] == terminal_full
    assert after_terminal_regions[2]["recovery"]["sourceRecordID"] == "terminal"

    # Bottom-edge geometry cannot override current semantic content.  A real
    # terminal input at the bottom of the window replaces the editor anchor.
    bottom_terminal_resolver = PaneResolver()
    bottom_terminal_resolver.resolve(record(
        "bottom-editor", captured="2026-01-01T00:00:01Z", x=250, y=400,
        ancestors=content_pane(100, 600, "AXCodeStyleGroup"),
    ))
    bottom_terminal = bottom_terminal_resolver.resolve(record(
        "bottom-terminal", captured="2026-01-01T00:00:02Z", x=650, y=985,
        ancestors=[
            {
                "depth": 0, "role": "AXTextArea",
                "frame": {"x": 500, "y": 200, "width": 450, "height": 800},
                "identifier": "terminal-input",
            },
            {
                "depth": 1, "role": "AXGroup",
                "frame": {"x": 0, "y": 0, "width": 1000, "height": 1000},
            },
        ],
    ))
    bottom_terminal_regions = surface_regions_from_review(bottom_terminal)
    assert bottom_terminal_regions is not None
    assert bottom_terminal_regions[2]["resolution"] == "current_ax_pane"
    assert bottom_terminal_regions[2]["selectedRole"] == "AXTextArea"

    # Same-window identity alone is insufficient.  A failed interior probe may
    # represent a new editor/terminal pane that AX did not expose, so reusing
    # the last terminal pane here would silently misattribute the READ.
    interior = resolver.resolve(record(
        "failed-interior", captured="2026-01-01T00:00:04.5Z", x=450, y=400,
        ancestors=failed_interior_probe(),
    ))
    assert surface_regions_from_review(interior) is None
    assert interior["proposal"]["reason"] \
        == "failed_content_probe_not_safe_for_pane_recovery"

    # Electron sometimes exposes its bottom status strip only as generic
    # groups.  Extreme-bottom geometry is sufficient to reuse the proven pane.
    bottom = resolver.resolve(record(
        "generic-bottom", captured="2026-01-01T00:00:04.6Z", x=450, y=995,
        ancestors=[
            {
                "depth": 0, "role": "AXButton",
                "frame": {"x": 400, "y": 970, "width": 100, "height": 30},
            },
            {
                "depth": 1, "role": "AXGroup",
                "frame": {"x": 0, "y": 0, "width": 1000, "height": 1000},
            },
        ],
    ))
    bottom_regions = surface_regions_from_review(bottom)
    assert bottom_regions is not None
    assert bottom_regions[0] == terminal_full
    assert bottom_regions[2]["physicalPointerClassification"] \
        == "application_chrome:extreme_bottom_window_chrome"

    # A generic AXGroup role is not pane identity.  A different same-window
    # group must not inherit the old pane merely because the pointer remains
    # inside its old rectangle.
    group_resolver = PaneResolver()
    group_anchor = group_resolver.resolve(record(
        "group-anchor", captured="2026-01-01T00:00:01Z", x=400, y=400,
        ancestors=generic_group_pane(100, 100, 700, 700),
    ))
    assert surface_regions_from_review(group_anchor) is not None
    compatible = group_resolver.resolve(record(
        "compatible-point", captured="2026-01-01T00:00:02Z", x=450, y=450,
        ancestors=failed_interior_probe(),
    ))
    assert surface_regions_from_review(compatible) is None
    assert compatible["proposal"]["reason"] \
        == "failed_content_probe_not_safe_for_pane_recovery"

    # The same geometry with a different current AX role does not prove that
    # the user remains in the remembered pane.
    incompatible = group_resolver.resolve(record(
        "incompatible-point", captured="2026-01-01T00:00:03Z", x=450, y=450,
        ancestors=[
            {"depth": 0, "role": "AXTextArea", "frame": {
                "x": 400, "y": 400, "width": 100, "height": 100,
            }},
        ],
    ))
    assert surface_regions_from_review(incompatible) is None

    # A top-edge probe with only window/chrome ancestry can reuse an anchor;
    # top-edge semantic content cannot.
    top_chrome = group_resolver.resolve(record(
        "top-chrome", captured="2026-01-01T00:00:04Z", x=450, y=40,
        ancestors=[
            {"depth": 0, "role": "AXButton", "frame": {
                "x": 400, "y": 20, "width": 100, "height": 40,
            }},
            {"depth": 1, "role": "AXWindow", "frame": {
                "x": 0, "y": 0, "width": 1000, "height": 1000,
            }},
        ],
    ))
    assert surface_regions_from_review(top_chrome) is not None
    assert top_chrome["proposal"]["physicalPointerClassification"] \
        == "application_chrome:extreme_top_window_chrome"

    # The same numeric window in another process/application cannot inherit.
    switched = resolver.resolve(record(
        "other-app", bundle="com.google.Chrome", process=11, window=20,
        captured="2026-01-01T00:00:05Z", x=400, y=985,
        ancestors=status_bar(),
    ))
    assert surface_regions_from_review(switched) is None
    assert switched["proposal"]["reason"] \
        == "no_trustworthy_current_or_prior_ax_pane"

    # Capture time, rather than raw append order, determines which anchor is
    # causally available to a fallback observation.
    ordered = PaneResolver().resolve_records([
        (1, record(
            "late-status", captured="2026-01-01T00:00:08Z", x=400, y=985,
            ancestors=status_bar(),
        )),
        (2, record(
            "early-content", captured="2026-01-01T00:00:07Z", x=250, y=400,
            ancestors=content_pane(100, 600, "AXCodeStyleGroup"),
        )),
    ])
    late = surface_regions_from_review(ordered["late-status"])
    assert late is not None
    assert late[2]["recovery"]["sourceRecordID"] == "early-content"

    # A first chrome observation has no invented crop or model-facing region.
    first = PaneResolver().resolve(record(
        "first-status", captured="2026-01-01T00:00:01Z", x=400, y=985,
        ancestors=status_bar(),
    ))
    assert surface_regions_from_review(first) is None
    assert first["proposal"]["regionOfInterest"] is None
    assert first["proposal"]["attemptedSelection"]["method"] == "v1_fallback"

    # A browser document pane is trustworthy current AX evidence even when it
    # exceeds the historical 82%-of-window cap.  It must not require a prior
    # anchor and it correctly excludes the top browser chrome.
    browser = PaneResolver().resolve(record(
        "browser-document", bundle="com.google.Chrome", process=11, window=21,
        captured="2026-01-01T00:00:01Z", x=500, y=500,
        ancestors=browser_document(),
    ))
    browser_regions = surface_regions_from_review(browser)
    assert browser_regions is not None
    assert browser_regions[2]["method"] == "ax_document_content_surface"
    assert browser_regions[2]["selectedRole"] == "AXWebArea"
    assert browser_regions[2]["attemptedSelection"]["method"] == "v1_fallback"

    # A large repeated chat/document container is also trustworthy current
    # evidence even without a named AX landmark.
    chat = PaneResolver().resolve(record(
        "chat-pane", bundle="com.openai.chat", process=12, window=22,
        captured="2026-01-01T00:00:01Z", x=500, y=900,
        ancestors=large_repeated_content_pane(),
    ))
    chat_regions = surface_regions_from_review(chat)
    assert chat_regions is not None
    assert chat_regions[2]["method"] == "ax_repeated_outer_content_pane"
    assert chat_regions[2]["selectedRole"] == "AXGroup"

    print("Phase 1 read-surface-v6 pane recovery checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

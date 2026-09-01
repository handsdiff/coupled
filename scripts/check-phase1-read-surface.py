#!/usr/bin/env python3
"""Focused checks for the frozen Phase 1 READ-surface rule."""

from __future__ import annotations

from phase1_read_surface import (
    SURFACE_RULE_VERSION,
    remove_adjacent_line_overlap,
    surface_region,
)


def record(
    *,
    bundle: str = "com.google.Chrome",
    trigger: str = "scroll",
    x: float = 500,
    y: float = 400,
) -> dict:
    return {
        "bundleIdentifier": bundle,
        "triggerTypes": [trigger],
        "windowBounds": {"x": 0, "y": 0, "width": 1000, "height": 800},
        "x": x,
        "y": y,
    }


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    region, provenance = surface_region(record())
    require(provenance["ruleVersion"] == SURFACE_RULE_VERSION, "rule version differs")
    require(provenance["method"] == "pointer_local", "scroll must use pointer")
    require(region == {"x": 0.14, "y": 0.16, "width": 0.72, "height": 0.68}, "center crop differs")

    left, _ = surface_region(record(x=0))
    require(left["x"] == 0, "left crop was not clamped")
    right, _ = surface_region(record(x=1000))
    require(right["x"] == 0.28, "right crop was not clamped")
    top, _ = surface_region(record(y=0))
    require(top["y"] == 0.32, "top crop Vision origin differs")
    bottom, _ = surface_region(record(y=800))
    require(bottom["y"] == 0, "bottom crop was not clamped")

    vscode, _ = surface_region(record(bundle="com.microsoft.VSCode"))
    require(vscode["width"] == 0.68 and vscode["height"] == 0.5, "VS Code crop differs")
    codex, _ = surface_region(record(bundle="com.openai.codex"))
    require(codex["width"] == 0.72 and codex["height"] == 0.72, "Codex crop differs")

    fallback, fallback_provenance = surface_region(record(trigger="application_activated"))
    require(fallback_provenance["method"] == "application_profile", "activation must use fallback")
    require(fallback == {"x": 0.08, "y": 0.08, "width": 0.84, "height": 0.82}, "Chrome fallback differs")

    missing, missing_provenance = surface_region({
        "bundleIdentifier": "md.obsidian",
        "triggerTypes": ["scroll"],
        "windowBounds": {"x": 0, "y": 0, "width": 0, "height": 800},
    })
    require(missing_provenance["method"] == "application_profile", "bad pointer must fall back")
    require(missing == {"x": 0.18, "y": 0.05, "width": 0.8, "height": 0.9}, "Obsidian fallback differs")

    emitted, count = remove_adjacent_line_overlap(
        ["alpha", "  BETA  "], ["beta", "gamma"]
    )
    require(emitted == ["gamma"] and count == 1, "normalized overlap differs")
    unchanged, count = remove_adjacent_line_overlap(["alpha"], ["beta"])
    require(unchanged == ["beta"] and count == 0, "unrelated lines changed")

    print("Phase 1 READ-surface checks passed.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Checks for pane-relative top/bottom cropping before OCR."""

from __future__ import annotations

from phase1_read_surface_v3 import proposal_for_record as v3_proposal
from phase1_read_surface_v4 import proposal_for_record as v4_proposal


def main() -> int:
    record = {
        "bundleIdentifier": "com.openai.codex",
        "windowBounds": {"x": 0, "y": 0, "width": 1000, "height": 1000},
        "screenshotPixelWidth": 1000,
        "screenshotPixelHeight": 1000,
        "x": 500,
        "y": 500,
        "triggerTypes": ["visual_change_settled"],
        "accessibilitySurface": {"ancestors": [
            {"depth": 0, "role": "AXStaticText",
             "frame": {"x": 300, "y": 480, "width": 400, "height": 20}},
            {"depth": 1, "role": "AXGroup", "subrole": "AXLandmarkMain",
             "frame": {"x": 200, "y": 100, "width": 600, "height": 800}},
            {"depth": 2, "role": "AXGroup",
             "frame": {"x": 0, "y": 0, "width": 1000, "height": 1000}},
        ]},
    }
    before = v3_proposal(record)["proposal"]
    after = v4_proposal(record)["proposal"]

    assert after["selectedDepth"] == before["selectedDepth"] == 1
    assert after["method"] == before["method"] == "ax_semantic_container"
    assert after["uncroppedScreenRectangle"] == before["screenRectangle"]
    assert after["screenRectangle"] == {
        "x": 200.0, "y": 260.0, "width": 600.0, "height": 480.0,
    }
    assert after["normalizedTopRectangle"] == {
        "x": 0.2, "y": 0.26, "width": 0.6, "height": 0.48,
    }
    assert after["regionOfInterest"] == {
        "x": 0.2, "y": 0.26, "width": 0.6, "height": 0.48,
    }
    assert after["paneCrop"] == {
        "stage": "before_ocr", "topFraction": 0.2, "bottomFraction": 0.2,
    }
    assert after["ruleVersion"] == "ax-pane-read-v4"
    print("Phase 1 read-surface-v4 crop checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

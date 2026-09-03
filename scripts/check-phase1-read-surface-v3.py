#!/usr/bin/env python3
"""Checks for expansion of partial semantic containers in AX pane v3."""

from __future__ import annotations

from phase1_read_surface_v2 import proposal_for_record as v2_proposal
from phase1_read_surface_v3 import proposal_for_record as v3_proposal


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
            {"depth": 1, "role": "AXList", "subrole": "AXContentList",
             "frame": {"x": 200, "y": 400, "width": 600, "height": 180}},
            {"depth": 2, "role": "AXGroup",
             "frame": {"x": 200, "y": 100, "width": 600, "height": 700}},
            {"depth": 3, "role": "AXGroup",
             "frame": {"x": 0, "y": 0, "width": 1000, "height": 1000}},
        ]},
    }
    before_review = v2_proposal(record)
    after_review = v3_proposal(record)
    before = before_review["proposal"]
    after = after_review["proposal"]
    assert before["selectedDepth"] == 1
    assert before["method"] == "ax_semantic_container"
    assert after["selectedDepth"] == 2
    assert after["method"] == "ax_semantic_container_expanded"
    assert after["reason"] == "semantic_content_list_expanded_to_containing_pane"
    assert after_review["ruleVersion"] == "ax-pane-read-v3"
    print("Phase 1 read-surface-v3 proposal checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

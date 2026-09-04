#!/usr/bin/env python3
"""Regression checks for the semantic READ review projection."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


scripts = Path(__file__).resolve().parent
sys.path.insert(0, str(scripts))
spec = importlib.util.spec_from_file_location(
    "phase1_read_semantic_review",
    scripts / "serve-phase1-read-semantic-review.py",
)
assert spec is not None and spec.loader is not None
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)

for decision in review.EMPTY_NOVELTY_DECISIONS:
    content, label, suppressed = review.review_projection(
        {"decision": decision, "content": ""}, "OCR THAT MUST NOT APPEAR",
    )
    assert content == "[No new READ text]", decision
    assert label.startswith("No newly available text"), decision
    assert suppressed is True, decision
    assert review.novelty_projection_changed(decision), decision

for decision in review.POSITIVE_NOVELTY_DECISIONS:
    content, label, suppressed = review.review_projection(
        {
            "decision": decision,
            "content": "new material",
            "lineAlignment": {"matchedLineCount": 2},
        },
        "complete OCR",
    )
    assert content == "new material", decision
    assert "retained" in label, decision
    if decision != "emit_stable_interior_after_clipped_boundary":
        assert "2 repeated lines removed" in label, decision
    assert suppressed is False, decision
    assert review.novelty_projection_changed(decision), decision

content, label, suppressed = review.review_projection(
    {"decision": "full_state", "content": "complete OCR"}, "complete OCR",
)
assert content == "complete OCR"
assert label == "Full READ retained · substantial/new state"
assert suppressed is False
assert not review.novelty_projection_changed("full_state")

content, label, suppressed = review.review_projection(
    {
        "decision": "full_state",
        "reason": "candidate_alignment_unproven_preserve_complete_state",
        "content": "complete OCR",
    },
    "complete OCR",
)
assert content == "complete OCR"
assert label == "Full READ retained · safe delta unproven"
assert suppressed is False

for decision in review.MODEL_REWRITTEN_RENDER_DECISIONS:
    assert review.model_rendering_changed(decision), decision
for decision in review.MODEL_FALLBACK_RENDER_DECISIONS:
    assert not review.model_rendering_changed(decision), decision

# The browser consumes the server-authoritative projection and label instead of
# maintaining a second, drift-prone decision table.
assert "return r.reviewProjectedContent||'[No new READ text]'" in review.HTML
assert "return r.reviewOutcomeLabel||'READ projection unavailable'" in review.HTML
assert "r.novelty.decision" not in review.HTML

print("Phase 1 semantic READ review checks passed")

#!/usr/bin/env python3
"""AX pane selection that expands partial semantic lists to their content pane."""

from __future__ import annotations

from typing import Any

from phase1_read_surface_v2 import proposal_for_record as v2_proposal_for_record


SURFACE_RULE_VERSION = "ax-pane-read-v3"
PROPOSAL_RULE_VERSION = SURFACE_RULE_VERSION


def proposal_for_record(record: dict[str, Any]) -> dict[str, Any]:
    return v2_proposal_for_record(
        record,
        rule_version=SURFACE_RULE_VERSION,
        expand_partial_semantic_containers=True,
    )


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
        "regionOfInterest": region,
    }

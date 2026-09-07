#!/usr/bin/env python3
"""Dependency-aware model rendering for shadow Phase 1 READ novelty.

Immutable surface evidence retains every complete selected pane. Semantic v23
projects explicit scroll observations through its stable interior before this
module runs. Predecessor-dependent novelty is rendered only when that exact
predecessor remains reconstructable in the packed context.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Callable


RENDERER_VERSION = "dependency-aware-read-novelty-v3"

POSITIVE_NOVELTY_DECISIONS = {
    "emit_new_content": "render_novel_content",
    "emit_stable_interior_after_clipped_boundary": (
        "render_grounded_stable_interior"
    ),
    "emit_contiguous_new_content": "render_contiguous_novel_content",
    "emit_scroll_new_edge": "render_scroll_new_edge",
}
EMPTY_NOVELTY_DECISIONS = {
    "suppress_no_new_content": "render_empty_adjacent_repeat",
    "suppress_clipped_boundary_no_stable_novelty": (
        "render_empty_proven_no_novelty"
    ),
    "suppress_cross_projection_ocr_disagreement": (
        "render_empty_proven_no_novelty"
    ),
    "suppress_ambiguous_adjacent_difference": (
        "render_empty_high_precision_ambiguity"
    ),
    "suppress_nonsemantic_microglyph": "render_empty_low_information_change",
    "suppress_unstable_scroll_edge": "render_empty_pending_stable_scroll_edge",
}


def canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _rendering(
    decision: str,
    novelty: dict[str, Any],
    *,
    dependency_available: bool,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schemaVersion": 1,
        "rendererVersion": RENDERER_VERSION,
        "decision": decision,
        "semanticNoveltyDecision": novelty.get("decision"),
        "dependencyAvailable": dependency_available,
    }
    dependency = novelty.get("dependsOnEventID")
    if isinstance(dependency, str):
        result["dependsOnEventID"] = dependency
    return result


def apply_dependency_aware_read_rendering(
    blocks: list[dict[str, Any]],
    events_by_id: dict[str, dict[str, Any]],
    encode: Callable[[str], list[int]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Return rendered copies and deterministic per-example decision counts.

    ``blocks`` must already be the retained chronological suffix. A
    content-truncated READ is not a complete predecessor. A READ rendered as a
    proven delta remains reconstructable and can support the next link in the
    chain. Uncertain microglyph suppression is deliberately not model-facing.
    """

    rendered: list[dict[str, Any]] = []
    complete_read_states: set[str] = set()
    counts = {
        "novelContentRendered": 0,
        "adjacentRepeatContentSuppressed": 0,
        "completeFallbackDependencyUnavailable": 0,
        "completeFallbackUncertainMicroglyph": 0,
        "truncatedReadStateRetained": 0,
        "nonReadProjectionRetained": 0,
        "completeReadStatesRetained": 0,
        "tokensRemoved": 0,
    }

    for source_block in blocks:
        block = copy.deepcopy(source_block)
        event_id = block.get("eventID")
        event = events_by_id.get(event_id)
        if not isinstance(event_id, str) or not isinstance(event, dict):
            raise ValueError("retained context block lacks its compiled event")
        if event.get("kind") != "read":
            rendered.append(block)
            continue

        novelty = event.get("readNovelty")
        is_truncated = block.get("contentTruncated") is True
        if not isinstance(novelty, dict):
            if not is_truncated:
                complete_read_states.add(event_id)
                counts["completeReadStatesRetained"] += 1
            rendered.append(block)
            continue

        if novelty.get("currentEventID") != event_id:
            raise ValueError(f"READ {event_id} has mismatched novelty identity")
        serialized_payload = json.loads(block["serialized"])
        if (
            serialized_payload.get("kind") != "read"
            or not isinstance(serialized_payload.get("content"), str)
        ):
            block["readRendering"] = _rendering(
                "retain_nonread_model_projection",
                novelty,
                dependency_available=False,
            )
            counts["nonReadProjectionRetained"] += 1
            rendered.append(block)
            continue
        semantic_decision = novelty.get("decision")
        dependency = novelty.get("dependsOnEventID")
        dependency_available = (
            isinstance(dependency, str) and dependency in complete_read_states
        )

        if is_truncated:
            block["readRendering"] = _rendering(
                "retain_existing_truncated_state",
                novelty,
                dependency_available=dependency_available,
            )
            counts["truncatedReadStateRetained"] += 1
            rendered.append(block)
            continue

        if semantic_decision in (
            POSITIVE_NOVELTY_DECISIONS.keys() | EMPTY_NOVELTY_DECISIONS.keys()
        ):
            novel_content = novelty.get("content")
            if not isinstance(novel_content, str):
                raise ValueError(f"READ {event_id} has invalid novelty content")
            if dependency_available:
                payload = dict(serialized_payload)
                payload["content"] = novel_content
                packed_serialized = canonical_json(payload)
                packed_ids = encode(packed_serialized + "\n")
                if len(packed_ids) <= len(block["tokenIDs"]):
                    block["serialized"] = packed_serialized
                    block["tokenIDs"] = packed_ids
                    block["packedSerialized"] = packed_serialized
                    render_decision = (
                        POSITIVE_NOVELTY_DECISIONS.get(semantic_decision)
                        or EMPTY_NOVELTY_DECISIONS[semantic_decision]
                    )
                    block["readRendering"] = _rendering(
                        render_decision,
                        novelty,
                        dependency_available=True,
                    )
                    if semantic_decision in POSITIVE_NOVELTY_DECISIONS:
                        counts["novelContentRendered"] += 1
                    else:
                        counts["adjacentRepeatContentSuppressed"] += 1
                else:
                    block["readRendering"] = _rendering(
                        "render_complete_no_token_savings",
                        novelty,
                        dependency_available=True,
                    )
            else:
                block["readRendering"] = _rendering(
                    "render_complete_dependency_unavailable",
                    novelty,
                    dependency_available=False,
                )
                counts["completeFallbackDependencyUnavailable"] += 1
        else:
            block["readRendering"] = _rendering(
                "render_complete_semantic_full_state",
                novelty,
                dependency_available=dependency_available,
            )

        complete_read_states.add(event_id)
        counts["completeReadStatesRetained"] += 1
        original_count = len(source_block["tokenIDs"])
        rendered_count = len(block["tokenIDs"])
        counts["tokensRemoved"] += original_count - rendered_count
        if rendered_count <= 0:
            raise AssertionError("READ rendering produced an empty event block")
        block["renderedSerializedSHA256"] = hashlib.sha256(
            block["serialized"].encode()
        ).hexdigest()
        rendered.append(block)

    return rendered, counts

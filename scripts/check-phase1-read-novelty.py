#!/usr/bin/env python3
"""Regression checks for dependency-aware READ rendering."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from phase1_read_novelty import apply_dependency_aware_read_rendering


def encode(text: str) -> list[int]:
    return list(text.encode("utf-8"))


def event(event_id: str, content: str, novelty: dict | None = None) -> dict:
    result = {
        "sourceEventID": event_id,
        "kind": "read",
        "serialized": json.dumps(
            {"content": content, "kind": "read"},
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    if novelty is not None:
        result["readNovelty"] = {
            "schemaVersion": 1,
            "currentEventID": event_id,
            **novelty,
        }
    return result


def block(row: dict, *, truncated: bool = False) -> dict:
    serialized = row["serialized"]
    return {
        "eventID": row["sourceEventID"],
        "serialized": serialized,
        "tokenIDs": encode(serialized + "\n"),
        "contentTruncated": truncated,
    }


events = {
    "a": event("a", "ABCD", {"decision": "full_state", "content": "ABCD"}),
    "b": event("b", "CDEF", {
        "decision": "emit_new_content", "content": "EF", "dependsOnEventID": "a",
    }),
    "c": event("c", "EFGH", {
        "decision": "emit_new_content", "content": "GH", "dependsOnEventID": "b",
    }),
    "d": event("d", "EFGH", {
        "decision": "suppress_no_new_content", "content": "", "dependsOnEventID": "c",
    }),
    "g": event("g", "Showing most recent -\nC", {
        "decision": "suppress_nonsemantic_microglyph", "content": "",
        "dependsOnEventID": "d",
    }),
}

rendered, counts = apply_dependency_aware_read_rendering(
    [block(events[key]) for key in ("a", "b", "c", "d", "g")], events, encode
)
payloads = [json.loads(value["serialized"]) for value in rendered]
assert [value["content"] for value in payloads] == [
    "ABCD", "EF", "GH", "", "Showing most recent -\nC",
]
assert rendered[3]["tokenIDs"]
assert counts["novelContentRendered"] == 2
assert counts["adjacentRepeatContentSuppressed"] == 1
assert counts["completeFallbackUncertainMicroglyph"] == 1

missing, missing_counts = apply_dependency_aware_read_rendering(
    [block(events["b"])], events, encode
)
assert json.loads(missing[0]["serialized"])["content"] == "CDEF"
assert missing[0]["readRendering"]["decision"] == (
    "render_complete_dependency_unavailable"
)
assert missing_counts["completeFallbackDependencyUnavailable"] == 1

truncated, _ = apply_dependency_aware_read_rendering(
    [block(events["a"], truncated=True), block(events["b"])], events, encode
)
assert json.loads(truncated[1]["serialized"])["content"] == "CDEF"
assert truncated[1]["readRendering"]["dependencyAvailable"] is False

privacy_event = event("private", "secret", {
    "decision": "full_state", "content": "secret",
})
privacy_block = block(privacy_event)
privacy_block["serialized"] = '{"kind":"write","privacy":"sensitive_content_redacted"}'
privacy_block["tokenIDs"] = encode(privacy_block["serialized"] + "\n")
privacy, privacy_counts = apply_dependency_aware_read_rendering(
    [privacy_block], {"private": privacy_event}, encode
)
assert privacy[0]["serialized"] == privacy_block["serialized"]
assert privacy_counts["nonReadProjectionRetained"] == 1

print("Phase 1 dependency-aware READ novelty checks passed")

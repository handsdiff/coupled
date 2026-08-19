#!/usr/bin/env python3
"""Resolve explicitly approved shadow candidates into episode adjudications."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected object")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def authored(content: str) -> dict[str, Any]:
    if not content:
        raise ValueError("approved episode target is empty")
    return {"schemaVersion": 1, "resolvedContent": content,
            "segments": [{"type": "authored_text", "content": content}]}


def concatenate_members(candidate: dict[str, Any]) -> dict[str, Any]:
    segments: list[dict[str, Any]] = []
    resolved = ""
    for member in candidate["members"]:
        target = member.get("currentTarget")
        if not isinstance(target, dict):
            continue
        value = target.get("resolvedContent")
        if not isinstance(value, str):
            raise ValueError(f"member target lacks resolved content: {candidate['label']}")
        resolved += value
        for segment in target.get("segments", []):
            segment = json.loads(json.dumps(segment))
            if segment.get("type") == "paste":
                segment.pop("content", None)
            segments.append(segment)
    if not segments or not resolved:
        raise ValueError(f"cannot concatenate candidate: {candidate['label']}")
    return {"schemaVersion": 1, "resolvedContent": resolved, "segments": segments}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    policy = load_json(args.policy)
    rows = load_jsonl(args.candidates)
    by_label = {row["label"]: row for row in rows}
    approved = policy.get("approvedMerges")
    if not isinstance(approved, list) or not approved:
        raise ValueError("policy requires approvedMerges")
    result = []
    for item in approved:
        label = item["label"]
        candidate = by_label.get(label)
        if candidate is None:
            raise ValueError(f"unknown candidate label: {label}")
        target_policy = item.get("targetPolicy", "net_field_edit")
        if target_policy == "net_field_edit":
            edit = candidate["singleCompletionDiagnostic"].get("netFieldEdit")
            if not isinstance(edit, dict) or edit.get("operation") != "insert" or edit.get("removedContent") != "":
                raise ValueError(f"{label}: approved net field edit is not a pure insertion")
            target = authored(edit["content"])
        elif target_policy == "concatenate_structured_member_targets":
            target = concatenate_members(candidate)
        else:
            raise ValueError(f"{label}: unknown target policy {target_policy}")
        result.append({
            "schemaVersion": 2,
            "candidateID": candidate["candidateID"],
            "label": label,
            "memberWriteEventIDs": candidate["memberWriteEventIDs"],
            "status": "human_approved_episode_policy",
            "decision": "merge_closed_episode",
            "targetPolicy": target_policy,
            "finalizedTarget": target,
            "closureAssessment": item.get("closureAssessment", "closed_substantive_composition"),
            "representableAsSingleCompletion": True,
            "notes": item.get("notes", "Approved from the 2026-08-19 episode taste audit."),
            "partitions": [],
        })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for row in result:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"Wrote {len(result)} approved episode adjudications to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Regression checks for deterministic Phase 1 corpus assembly."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from phase1_corpus import assemble, audit, sha256


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def fixture(root: Path, ordinal: int, began_at: str, available_at: str) -> Path:
    directory = root / f"session-{ordinal}"
    directory.mkdir()
    session_id = f"session-{ordinal}"
    event_id = f"event-{ordinal}"
    serialized = json.dumps(
        {"kind": "write", "authorshipSegments": [{"type": "authored_text", "content": f"w{ordinal}"}]},
        sort_keys=True,
        separators=(",", ":"),
    )
    event = {
        "schemaVersion": 1,
        "conversionVersion": "phase1-causal-v14",
        "sessionID": session_id,
        "sourceEventID": event_id,
        "kind": "write",
        "beganAt": began_at,
        "availableAt": available_at,
        "sourceLine": 1,
        "serialized": serialized,
        "auditSerialized": json.dumps({"content": f"w{ordinal}"}, sort_keys=True),
    }
    query = json.dumps({"kind": "write_conditioning_state", "session": ordinal}, sort_keys=True)
    example = {
        "exampleID": f"example-{ordinal}",
        "sessionID": session_id,
        "targetEventID": event_id,
        "targetBeganAt": began_at,
        "contextEventIDs": [],
        "context": "",
        "query": query,
        "modelInput": query,
        "target": {
            "schemaVersion": 1,
            "resolvedContent": f"w{ordinal}",
            "segments": [{"type": "authored_text", "content": f"w{ordinal}"}],
        },
    }
    contract = {
        "conversionVersion": "phase1-causal-v14",
        "sessionID": session_id,
        "serialization": {"contextVersion": 3, "targetFormat": "structured_authorship_segments"},
        "objective": {"target": "authored_text_plus_grounded_paste_actions"},
        "eligibility": {"minimumTrimmedAuthoredCharactersForTextOnlyTarget": 4},
        "timing": {"causalFilter": "event.availableAt < target.beganAt"},
        "source": {"reducerVersion": "phase1-semantic-v7"},
        "counts": {
            "convertedEvents": 1,
            "examples": 1,
            "targetExclusions": 0,
            "contextExclusions": 0,
            "rejections": 0,
        },
    }
    write_json(directory / "dataset.json", contract)
    write_jsonl(directory / "events.jsonl", [event])
    write_jsonl(directory / "examples.jsonl", [example])
    for name in ("target-exclusions.jsonl", "context-exclusions.jsonl", "rejections.jsonl"):
        write_jsonl(directory / name, [])
    return directory


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="phase1-corpus-check-") as temporary:
        root = Path(temporary)
        first = fixture(root, 1, "2026-01-01T00:00:01Z", "2026-01-01T00:00:02Z")
        second = fixture(root, 2, "2026-01-02T00:00:01Z", "2026-01-02T00:00:02Z")
        output_a = root / "corpus-a"
        output_b = root / "corpus-b"
        assemble([first, second], output_a, block_size=1)
        assemble([first, second], output_b, block_size=1)
        audit(output_a)
        for name in (
            "corpus.json", "dataset.json", "events.jsonl", "context-blocks.jsonl",
            "examples.jsonl", "gaps.jsonl", "privacy-policy.json",
            "target-exclusions.jsonl",
            "context-exclusions.jsonl", "rejections.jsonl",
        ):
            if sha256(output_a / name) != sha256(output_b / name):
                raise AssertionError(f"corpus assembly is not deterministic: {name}")
        examples = [json.loads(line) for line in (output_a / "examples.jsonl").read_text().splitlines()]
        if examples[0]["contextBlockIDs"]:
            raise AssertionError("first session unexpectedly has prior context")
        second_blocks = examples[1]["contextBlockIDs"]
        if len(second_blocks) != 2 or not second_blocks[1].startswith("gap_"):
            raise AssertionError("later session did not receive semantic history plus coverage gap")
        if examples[0]["experimentBlockID"] == examples[1]["experimentBlockID"]:
            raise AssertionError("fixed one-example blocks were not assigned")
        with (output_a / "gaps.jsonl").open("a", encoding="utf-8") as handle:
            handle.write("{}\n")
        try:
            audit(output_a)
        except ValueError as error:
            if "digest mismatch" not in str(error):
                raise
        else:
            raise AssertionError("corpus audit accepted a tampered artifact")

        privacy_policy = root / "privacy.json"
        write_json(privacy_policy, {
            "schemaVersion": 1,
            "policyVersion": "phase1-context-privacy-v1",
            "events": [{
                "sourceEventID": "event-1",
                "reason": "private_form_response",
                "groupID": "fixture-form",
            }],
        })
        private_output = root / "corpus-private"
        assemble(
            [first, second], private_output, block_size=1,
            privacy_policy_path=privacy_policy,
        )
        private_manifest = audit(private_output)
        private_events = [
            json.loads(line)
            for line in (private_output / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        private_context = (private_output / "context-blocks.jsonl").read_text(encoding="utf-8")
        private_examples = (private_output / "examples.jsonl").read_text(encoding="utf-8")
        private_source = next(
            event for event in private_events if event["sourceEventID"] == "event-1"
        )
        if json.loads(private_source["serialized"])["authorshipSegments"][0]["content"] != "w1":
            raise AssertionError("privacy policy modified semantic evidence")
        if '"content":"w1"' in private_context or '"content":"w1"' in private_examples:
            raise AssertionError("private content survived in model-facing artifacts")
        if "sensitive_content_redacted" not in private_context:
            raise AssertionError("private context lacks an explicit redaction marker")
        if private_manifest["counts"]["examples"] != 1:
            raise AssertionError("private target remained eligible")
    print("Phase 1 corpus checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

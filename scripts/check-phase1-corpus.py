#!/usr/bin/env python3
"""Regression checks for deterministic Phase 1 corpus assembly."""

from __future__ import annotations

import json
import shutil
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

        # A raw-authoritative episode corpus stays immutable while the
        # experiment adapter exposes its separately hashed block ledger.
        raw_episode = root / "raw-episode"
        shutil.copytree(output_b, raw_episode)
        raw_examples = [
            json.loads(line)
            for line in (raw_episode / "examples.jsonl").read_text().splitlines()
        ]
        for example in raw_examples:
            example["experimentBlockID"] = "block-0001"
        write_jsonl(raw_episode / "examples.jsonl", raw_examples)
        episode_blocks = [{
            "blockID": "block-0001",
            "exampleCount": len(raw_examples),
            "exampleIDs": [example["exampleID"] for example in raw_examples],
        }]
        write_jsonl(raw_episode / "episode-blocks.jsonl", episode_blocks)
        raw_manifest = json.loads((raw_episode / "corpus.json").read_text())
        raw_manifest.pop("assemblerVersion", None)
        raw_manifest.pop("blocking", None)
        raw_manifest.update({
            "schemaVersion": 2,
            "artifactType": "phase1_raw_authoritative_episode_corpus",
            "episodeVersion": "phase1-raw-episode-v6",
            "conversionVersion": "phase1-raw-episode-causal-v6",
            "rawEpisodeArchitecture": {
                "productionConsumesRegressionFixture": False,
                "sourceAuthority": "immutable_raw_journals",
            },
        })
        raw_manifest["artifactDigestsSHA256"]["examples.jsonl"] = sha256(
            raw_episode / "examples.jsonl"
        )
        raw_manifest["artifactDigestsSHA256"]["episode-blocks.jsonl"] = sha256(
            raw_episode / "episode-blocks.jsonl"
        )
        write_json(raw_episode / "corpus.json", raw_manifest)
        write_json(raw_episode / "dataset.json", raw_manifest)
        adapted = audit(raw_episode)
        if not (
            adapted["blocking"]["blockCount"] == 1
            and adapted["blocking"]["blocks"] == episode_blocks
            and adapted["experimentAdapter"]["semanticInterpretationChanged"] is False
        ):
            raise AssertionError("raw episode experiment adapter changed its source")
        write_jsonl(raw_episode / "episode-blocks.jsonl", [{
            **episode_blocks[0], "exampleIDs": list(reversed(episode_blocks[0]["exampleIDs"]))
        }])
        try:
            audit(raw_episode)
        except ValueError as error:
            if "digest mismatch" not in str(error):
                raise
        else:
            raise AssertionError("raw episode adapter accepted a tampered block ledger")
    print("Phase 1 corpus checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Frozen-byte and exhaustive-validation checks for bounded corpus assembly."""

from __future__ import annotations

import argparse
import importlib.util
import json
import tempfile
from pathlib import Path

import phase1_corpus as current
from phase1_jsonl import JSONLSequence


ARTIFACTS = (
    "corpus.json", "dataset.json", "events.jsonl", "context-blocks.jsonl",
    "examples.jsonl", "gaps.jsonl", "privacy-policy.json",
    "target-exclusions.jsonl", "context-exclusions.jsonl", "rejections.jsonl",
)

# Generated from the pre-streaming implementation, not the current code:
# SHA256 96226dcf6284013c7888397ff0bcfc3caaceb82d5f9682d0ffeecb269567e7eb.
GOLDEN_SHA256 = {
    "plain": {
        "context-blocks.jsonl": "90424d71ecc7df0530b977b1cfef5612181de7539233a97513737223f0e913f6",
        "context-exclusions.jsonl": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "corpus.json": "369aa8da1b2e71e8dcce0a0148d0265a04b82c2ab0ff4a1c69e811679e15ea72",
        "dataset.json": "369aa8da1b2e71e8dcce0a0148d0265a04b82c2ab0ff4a1c69e811679e15ea72",
        "events.jsonl": "a970604cce8dd5468d44b00bc9057c4f65b9ebd30acc303e732eacd9b0b52355",
        "examples.jsonl": "4f29880d2d0162143840e53069e3d4def257ecfc638f9a79ae408761d6504fb4",
        "gaps.jsonl": "1c3356e09ead8ca072446c2597002b514b71f3134cd3e94537e31a52000d151d",
        "privacy-policy.json": "7ad49de5a661f9b97bd406ccdebbf2760095d2612ac1ed28bb8b35a01657ae85",
        "rejections.jsonl": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "target-exclusions.jsonl": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    },
    "privacy": {
        "context-blocks.jsonl": "f2ef9dc2e2813b9ee81a621adc2b833c5f261965c8917b7679366df7f2d47181",
        "context-exclusions.jsonl": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "corpus.json": "cb7b6e8648f1301161011023d8366338c775d0ad5d7f8665bf83f29fbdf9d04b",
        "dataset.json": "cb7b6e8648f1301161011023d8366338c775d0ad5d7f8665bf83f29fbdf9d04b",
        "events.jsonl": "a05c4140838dafe3e30f165e657d480e6cc6898b64c57f3a214a428ac50d3106",
        "examples.jsonl": "44ab435fb27d690e39073279d0a30ebf5aab5847972baf529cb2f5e0bea7c17b",
        "gaps.jsonl": "82ffcc65873fa31e2319c36462429d5472cab5f06115cf2bac5ed3e0544c2f79",
        "privacy-policy.json": "6c9a24071bbcaf8b0ca023f7b5d61ffaba647d341d521130166f456b7b947162",
        "rejections.jsonl": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "target-exclusions.jsonl": "22be86418452666b3a13b8b1ec1760852e7201e0b709508c1d927845e413e2e3",
    },
}


def write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def fixture(root: Path, day: int) -> Path:
    path = root / f"session-{day}"
    path.mkdir()
    session = f"session-{day}"
    date = f"2026-01-0{day}"
    events = [{
        "kind": "read", "sessionID": session, "sourceEventID": f"read-{day}",
        "availableAt": f"{date}T00:00:00Z",
        "serialized": json.dumps({"kind": "read", "content": ("Readable context 😀 café\n" * 48) + str(day)}, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    }]
    examples = []
    for index in range(1, 7):
        content = f"authored continuation {day}-{index}"
        segments = [{"type": "authored_text", "content": content}]
        if index == 3:
            segments.append({"type": "paste", "clipboardSnapshotID": "clipboard", "content": "copied passage"})
        context = "\n".join(event["serialized"] for event in events)
        query = json.dumps({"kind": "write_conditioning_state", "destination": session, "cursorContext": {"leftContext": "before 😀", "rightContext": "after"}}, ensure_ascii=False, sort_keys=True)
        event_id = f"write-{day}-{index}"
        began = f"{date}T00:00:{index * 2:02d}Z"
        examples.append({
            "exampleID": f"example-{day}-{index}", "sessionID": session,
            "targetEventID": event_id, "targetBeganAt": began,
            "contextEventIDs": [event["sourceEventID"] for event in events],
            "context": context, "modelInput": context + "\n" + query,
            "query": query, "target": {"segments": segments},
        })
        events.append({
            "kind": "write", "sessionID": session, "sourceEventID": event_id,
            "beganAt": began, "availableAt": f"{date}T00:00:{index * 2 + 1:02d}Z",
            "serialized": json.dumps({"kind": "write", "authorshipSegments": segments}, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        })
    current.write_jsonl(path / "events.jsonl", events)
    current.write_jsonl(path / "examples.jsonl", examples)
    write(path / "dataset.json", {
        "conversionVersion": "phase1-causal-v16", "sessionID": session,
        "serialization": {"contextVersion": 5, "targetFormat": "structured_authorship_segments"},
        "objective": {"target": "authored_text_plus_grounded_paste_actions"},
        "eligibility": {"minimumTrimmedAuthoredCharactersForTextOnlyTarget": 4},
        "timing": {"causalFilter": "event.availableAt < target.beganAt"},
        "source": {"reducerVersion": "phase1-semantic-v24"},
        "counts": {"convertedEvents": len(events), "examples": len(examples), "targetExclusions": 0, "contextExclusions": 0, "rejections": 0},
    })
    for name in ("target-exclusions.jsonl", "context-exclusions.jsonl", "rejections.jsonl"):
        (path / name).write_text("")
    return path


def expect_failure(call, fragment: str) -> None:
    try:
        call()
    except ValueError as error:
        assert fragment in str(error), str(error)
    else:
        raise AssertionError(f"expected rejection containing {fragment}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-module", type=Path)
    args = parser.parse_args()
    reference = None
    if args.reference_module:
        spec = importlib.util.spec_from_file_location("prior_phase1_corpus", args.reference_module)
        assert spec and spec.loader
        reference = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reference)
    recorded = {}
    with tempfile.TemporaryDirectory(prefix="phase1-corpus-memory-check-") as temporary:
        root = Path(temporary)
        sources = [fixture(root, day) for day in (1, 2, 3)]
        input_hashes = [current.sha256(source / "examples.jsonl") for source in sources]
        assert isinstance(current.load_session(sources[0])["examples"], JSONLSequence)
        privacy = root / "privacy.json"
        write(privacy, {"schemaVersion": 1, "policyVersion": "phase1-context-privacy-v1", "events": [{"sourceEventID": "write-1-1", "reason": "fixture_sensitive", "groupID": "form"}]})
        for name, policy in (("plain", None), ("privacy", privacy)):
            output = root / name
            actual_write = current.write_jsonl

            def bounded_write(path, rows):
                if path.name == "examples.jsonl":
                    assert not isinstance(rows, (list, tuple)), "assembled expanded prefixes must stream"
                return actual_write(path, rows)

            current.write_jsonl = bounded_write
            try:
                current.assemble(sources, output, block_size=4, privacy_policy_path=policy)
            finally:
                current.write_jsonl = actual_write
            current.audit(output)
            hashes = {filename: current.sha256(output / filename) for filename in ARTIFACTS}
            recorded[name] = hashes
            if reference:
                previous = root / f"reference-{name}"
                reference.assemble(sources, previous, block_size=4, privacy_policy_path=policy)
                reference.audit(output)
                assert hashes == {filename: current.sha256(previous / filename) for filename in ARTIFACTS}
            elif GOLDEN_SHA256:
                assert hashes == GOLDEN_SHA256[name], name
            else:
                raise AssertionError("frozen reference hashes have not been populated")

        assert input_hashes == [current.sha256(source / "examples.jsonl") for source in sources]
        source_examples = sources[0] / "examples.jsonl"
        original = source_examples.read_text()
        rows = current.load_jsonl(source_examples)
        rows[-1]["context"] += "unexpected future context"
        current.write_jsonl(source_examples, rows)
        expect_failure(lambda: current.load_session(sources[0]), "context is inconsistent")
        source_examples.write_text(original)

        # Refresh manifests after a deliberate mutation to verify the semantic
        # context audit itself still rejects it, beyond the digest gate.
        assembled_path = root / "plain/examples.jsonl"
        rows = current.load_jsonl(assembled_path)
        rows[-1]["modelInput"] += "wrong"
        current.write_jsonl(assembled_path, rows)
        manifest = current.load_json(root / "plain/corpus.json")
        manifest["artifactDigestsSHA256"]["examples.jsonl"] = current.sha256(assembled_path)
        write(root / "plain/corpus.json", manifest)
        write(root / "plain/dataset.json", manifest)
        expect_failure(lambda: current.audit(root / "plain"), "model input disagrees with lineage")
    if reference:
        print(json.dumps(recorded, indent=2, sort_keys=True))
    print("Phase 1 bounded-memory corpus checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

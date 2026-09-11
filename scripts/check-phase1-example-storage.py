#!/usr/bin/env python3
"""Shared-context equivalence, causal metadata, and tamper/space gates."""

import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from phase1_example_storage import write_examples
from phase1_jsonl import JSONLSequence


def rejected(call):
    try:
        call()
    except ValueError:
        return
    raise AssertionError("invalid storage accepted")


with tempfile.TemporaryDirectory(prefix="phase1-example-storage-check-") as directory:
    root = Path(directory).resolve()
    blocks = [{"contextBlockID": "a", "serialized": "earlier read 🧠\n" + "prior context " * 1000},
              {"contextBlockID": "b", "serialized": 'resolved paste: actual payload'}]
    (root / "context-blocks.jsonl").write_text("".join(json.dumps(b) + "\n" for b in blocks))
    expected = []
    for index, ids in enumerate([[], ["a"], ["a", "b"], ["b", "a"]]):
        context = "\n".join(next(b["serialized"] for b in blocks if b["contextBlockID"] == key) for key in ids)
        query = 'destination + cursor + clipboard 🧠'
        expected.append({"exampleID": str(index), "contextBlockIDs": ids,
                         "context": context, "query": query, "modelInput": context + "\n" + query if context else query,
                         "target": {"segments": [{"type": "authored_text", "content": "before "}, {"type": "paste", "clipboardSnapshotID": "clip1"}]},
                         "targetMask": {"query": -100, "target": True}, "targetBeganAt": "2026-09-02T12:00:00Z"})
    path = root / "examples.jsonl"
    with patch("phase1_storage.shutil.disk_usage") as usage:
        usage.return_value.free = 100 * 1024 ** 3
        write_examples(path, expected, compact=True)
    assert list(JSONLSequence(path)) == expected
    assert JSONLSequence(path)[-1] == expected[-1]
    assert path.stat().st_size < sum(len(json.dumps(r)) for r in expected) / 5
    # Raw storage cannot masquerade as an ordinary expanded row.
    stored = [json.loads(s) for s in path.read_text().splitlines()]
    assert all('context' not in r and 'modelInput' not in r for r in stored)
    stored[1]['_sharedContext']['contextSHA256'] = '0' * 64
    path.write_text(''.join(json.dumps(r) + '\n' for r in stored))
    rejected(lambda: list(JSONLSequence(path)))
    stored[1]['_sharedContext']['version'] = 'unknown'
    path.write_text(''.join(json.dumps(r) + '\n' for r in stored))
    rejected(lambda: list(JSONLSequence(path)))
    # Mutations of the shared table fail even on an already opened sequence.
    path.unlink()
    with patch("phase1_storage.shutil.disk_usage") as usage:
        usage.return_value.free = 100 * 1024 ** 3
        write_examples(path, expected, compact=True)
    seq = JSONLSequence(path)
    assert seq[0] == expected[0]
    with (root / 'context-blocks.jsonl').open('a') as handle:
        handle.write('\n')
    rejected(lambda: seq[1])
    rejected(lambda: list(JSONLSequence(path)))
    with patch("phase1_storage.shutil.disk_usage") as usage:
        usage.return_value.free = 0
        rejected(lambda: write_examples(root / 'no-space.jsonl', expected, compact=False))
    assert not (root / 'no-space.jsonl').exists()

print("Shared-history storage checks passed: identical decoded records; targets, masks, queries, ordering intact; tamper and low-space rejection")

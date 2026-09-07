#!/usr/bin/env python3
"""Check disk-backed artifact access without changing row contents."""

import json
import tempfile
from pathlib import Path

from phase1_jsonl import JSONLSequence


with tempfile.TemporaryDirectory(prefix="phase1-jsonl-check-") as directory:
    path = Path(directory) / "rows.jsonl"
    expected = [{"id": 1, "context": "one 🧠"}, {"id": 2, "context": "two"}]
    path.write_text("\n" + "\n\n".join(json.dumps(x) for x in expected) + "\n")
    rows = JSONLSequence(path)
    assert len(rows) == 2
    assert list(rows) == expected == list(rows)
    assert rows[0] == expected[0] and rows[-1] == expected[-1]
    assert rows[:] == expected and rows[::-1] == expected[::-1]
    rows[0]["context"] = "does not mutate disk"
    assert rows[0] == expected[0]
    try:
        rows[2]
        raise AssertionError("out-of-range index accepted")
    except IndexError:
        pass
    path.write_text('{"changed": true}\n')
    for read in (lambda: rows[0], lambda: list(rows)):
        try:
            read()
            raise AssertionError("changed artifact accepted")
        except ValueError:
            pass
    for invalid in ("[]\n", "bad JSON\n"):
        path.write_text(invalid)
        try:
            list(JSONLSequence(path))
            raise AssertionError("invalid object accepted")
        except ValueError:
            pass

print("Phase 1 bounded-memory JSONL checks passed")

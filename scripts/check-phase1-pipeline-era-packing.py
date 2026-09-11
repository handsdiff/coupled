#!/usr/bin/env python3
"""Offline independent-cohort scheduling and historical IO-adapter checks."""
import datetime as dt
import importlib.util
import json
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod


pack = module("pack_era_test", ROOT / "scripts/pack-phase1-pipeline-era.py")
replay = module("replay_era_test", ROOT / "scripts/replay-phase1-historical-pipeline.py")
start = dt.datetime(2026, 9, 2, tzinfo=dt.timezone.utc)
for kind in ("read", "write", "coverage_gap"):
    assert pack.causally_available({"kind": kind, "availableAt": "a"}, "b")
    assert not pack.causally_available({"kind": kind, "availableAt": "c"}, "b")
    assert pack.causally_available({"kind": kind, "availableAt": "b"}, "b") == (kind == "coverage_gap")


def examples(n):
    return [{"exampleID": str(i), "targetBeganAt": (start + dt.timedelta(seconds=10*i)).isoformat(),
             "targetAvailableAt": (start + dt.timedelta(seconds=10*i+3)).isoformat()} for i in range(n)]


for count in (101, 150, 660, 713):
    rows = examples(count); schedule = pack.schedule(rows)
    assert sum(len(b["exampleIDs"]) for b in schedule) == count
    assert not schedule[-1]["trainThisBlockAfterScoring"]
    assert not schedule[0]["freeGenerationComparison"]
    assert sum(len(b["exampleIDs"]) for b in schedule if b["freeGenerationComparison"]) == count - 50
    seen = 0
    for b in schedule:
        assert b["trainedExamplesBeforeScoring"] == seen
        seen += b["optimizerStepsAfterScoring"]
bad = examples(51); bad[49]["targetAvailableAt"] = bad[50]["targetAvailableAt"]
try:
    pack.schedule(bad)
except AssertionError:
    pass
else:
    raise AssertionError("Cross-boundary training leakage accepted")

with tempfile.TemporaryDirectory(prefix="phase1-era-io-check-") as folder:
    path = Path(folder) / "examples.jsonl"
    expected = [{"exampleID": "a", "context": "long history 🧠", "target": "x"},
                {"exampleID": "b", "context": "later history", "target": "y"}]
    path.write_text("".join(json.dumps(r) + "\n" for r in expected))
    class Dummy:
        @staticmethod
        def load_jsonl(p): return [json.loads(s) for s in Path(p).read_text().splitlines()]
    replay.lazy_examples(Dummy)
    actual = Dummy.load_jsonl(path)
    assert list(actual) == expected and actual[1] == expected[1] and actual[:1] == expected[:1]
    assert [{**r} for r in actual] == expected
    assert [dict(r.items()) for r in actual] == expected
    # Repeated metadata scans must not re-read expanded history from disk.
    source = actual.source
    original = source.__class__.__getitem__
    def no_read(*args): raise AssertionError("Metadata lookup re-read large history")
    source.__class__.__getitem__ = no_read
    try:
        for _ in range(10):
            assert [r["exampleID"] for r in actual] == ["a", "b"]
    finally:
        source.__class__.__getitem__ = original
print("Independent-cohort schedules and exact lazy JSONL adapter passed; no final update, no cross-block leakage")

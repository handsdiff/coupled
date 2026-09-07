#!/usr/bin/env python3
"""Local real-session regression gate for ordinary same-pane repetition.

The personal evidence stays outside Git. Run against the frozen September 2–4
v24 baseline and a fresh candidate; never use this audit as construction input.
Sanitized, no-data counterparts run in Checks/main.swift.
"""
import argparse
import json
from pathlib import Path


def rows(path):
    with path.open() as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--day", type=int, choices=(2, 3, 4), required=True)
    args = parser.parse_args()
    old = {row["eventID"]: row for row in rows(args.baseline / f"sep{args.day}-reduced/events.jsonl")}
    new = {row["eventID"]: row for row in rows(args.candidate / f"sep{args.day}-reduced/events.jsonl")}
    assert old.keys() == new.keys(), "event identity or grouping changed"
    for eid, before in old.items():
        after = new[eid]
        assert {k: v for k, v in before.items() if k != "readNovelty"} == {
            k: v for k, v in after.items() if k != "readNovelty"
        }, f"capture, pane, timing, WRITE, or full content changed: {eid}"
    by_sequence = {row["sequence"]: row for row in old.values()}
    for sequence in {2: (), 3: (264, 1139), 4: (328,)}[args.day]:
        before = by_sequence[sequence]
        after = new[before["eventID"]]
        assert before["readNovelty"]["content"], "baseline no longer demonstrates failure"
        assert after["readNovelty"]["content"] == "", f"repeat remains: {sequence}"
        assert after["readNovelty"]["decision"] == "suppress_ambiguous_adjacent_difference"
        assert after["readNovelty"]["repeatedFraction"] >= 0.72
    if args.day == 2:
        # Catch the first candidate's accidental downstream replay: recognizing
        # the OCR repeat at 1312 must not forget its coverage on later scrolls.
        for sequence in (1313, 1314, 1315):
            before = by_sequence[sequence]
            assert new[before["eventID"]] == before, f"scroll frontier regression: {sequence}"
        approved = json.loads((args.baseline / "user-approved-read-baseline.json").read_text())
        for case in approved["cases"]:
            eid = case["eventID"]
            assert new[eid] == old[eid], f"approved case changed: {case['approvedUILabels']}"
        reference = args.baseline.parent / "semantic-v23-verification/audit.json"
        if reference.exists():
            for case in json.loads(reference.read_text())["cases"]:
                assert new[case["id"]] == old[case["id"]], f"reference changed: {case['baselineSequences']}"
    if args.day == 3:
        # Fresh ordinary observations can also expose a genuinely new paragraph
        # beneath a mostly repeated body. It must survive as a coherent region.
        before = by_sequence[1155]
        content = new[before["eventID"]]["readNovelty"]["content"]
        assert content and len(content) < len(before["content"]) / 2, "new paragraph lost or repeated body replayed"
    print(json.dumps({"day": args.day, "eventsChecked": len(new), "status": "passed"}))


if __name__ == "__main__":
    main()

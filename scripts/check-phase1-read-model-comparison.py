#!/usr/bin/env python3
"""Synthetic fail-closed checks; no provider credentials, network or personal data."""
import copy
import hashlib
import itertools
import sys
from pathlib import Path

from phase1_read_model_comparison import (
    MODELS, VARIANTS, Privacy, audit_records, canonical, fingerprint, import_packer,
    make_prompt, native_episode_order, request_plan, select_cohort, target_text, timeline,
)


class CharacterTokenizer:
    def encode(self, text, **_):
        return list(text.encode())
    def decode(self, ids, **_):
        return bytes(ids).decode(errors="replace")


def rejected(fn):
    try:
        fn()
    except (ValueError, AssertionError, KeyError):
        return
    raise AssertionError("Corrupted contract was accepted")


def main():
    frozen = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".build/phase1-factorial-frozen-95609c3")
    packer = import_packer(frozen)
    tok = CharacterTokenizer()
    secret = {"sourceEventID": "secret", "sourceRecordIDs": ["raw-secret"], "kind": "write",
              "availableAt": "2026-01-01T00:00:00Z", "serialized": canonical({"kind": "write", "authorshipSegments": [{"type": "paste", "content": "654321"}]})}
    privacy = Privacy({"secret": secret}, {"sensitiveEventIDs": ["secret"]})
    assert privacy.unsafe("clipboard contained 654321")
    assert privacy.unsafe("Password: hidden")
    assert privacy.unsafe(canonical({"api_key": "synthetic_secret_example_not_real"}))
    assert not privacy.unsafe("api_key=os.getenv('TEST_API_KEY')")
    assert not privacy.unsafe("please review this paragraph")
    fallback = {**secret, "sourceEventID": "different-read", "kind": "read", "serialized": canonical({"kind": "read", "content": "innocuous"}), "readNovelty": {"content": "654321"}}
    redacted = privacy.filter_event(fallback)
    assert "readNovelty" not in redacted
    assert redacted["privacyRedacted"]
    assert "654321" not in canonical(redacted)
    dependent = {**fallback, "sourceEventID": "later", "sourceRecordIDs": [],
                 "readNovelty": {"dependsOnEventID": "different-read", "content": "safe delta"}}
    cleaned = privacy.filter_stream([fallback, dependent])
    assert cleaned[1]["privacyDependencyFallback"]
    assert "readNovelty" not in cleaned[1]
    t = {"segments": [{"type": "authored_text", "content": "review "}, {"type": "paste", "clipboardSnapshotID": "clip"}]}
    assert target_text(t) == "review <|paste|>"
    e = {"exampleID": "example", "query": canonical({"kind": "write_query", "cursorContext": ""}),
         "target": t, "targetEventID": "target", "targetBeganAt": "2026-01-01T00:00:02Z",
         "episode": {"memberWriteEventIDs": ["micro"]}, "modelFacingDestination": {"application": "test"}}
    e.update(targetText=target_text(t), targetSHA256=fingerprint(t), querySHA256=fingerprint(e["query"]))
    read = {"sourceEventID": "read", "kind": "read", "availableAt": "2026-01-01T00:00:01Z", "serialized": canonical({"kind": "read", "content": "context"})}
    future = {**read, "sourceEventID": "future", "availableAt": e["targetBeganAt"]}
    m, order = timeline([future, read], [])
    p = [make_prompt(e, v, m, order, packer, tok, {}, 1000) for v in VARIANTS]
    assert all([b["eventID"] for b in x["retainedBlocks"]] == ["read"] for x in p)
    r = request_plan(p, [e], 17)
    audit_records([e], p, r, privacy, 1000)
    assert len({(x["variant"], x["model"]) for x in r}) == 4
    bad = copy.deepcopy(p); bad[0]["query"] = "changed query"
    rejected(lambda: audit_records([e], bad, r, privacy, 1000))
    bad = copy.deepcopy(p); bad[0]["retainedBlocks"][0]["availableAt"] = e["targetBeganAt"]
    rejected(lambda: audit_records([e], bad, r, privacy, 1000))
    bad = copy.deepcopy(p); bad[0]["modelInput"] += "654321"
    rejected(lambda: audit_records([e], bad, r, privacy, 1000))
    bad_e = copy.deepcopy(e); bad_e["target"]["segments"][0]["content"] = "wrong"
    rejected(lambda: audit_records([bad_e], p, r, privacy, 1000))
    bad = copy.deepcopy(r); bad[0]["model"] = "openai/gpt-6-astra"
    rejected(lambda: audit_records([e], p, bad, privacy, 1000))
    member = {**read, "sourceEventID": "micro", "kind": "write"}
    mm, oo = timeline([member], [])
    rejected(lambda: make_prompt(e, "new", mm, oo, packer, tok, {}, 1000))
    # Closure moves availability later, but native projection keeps the WRITE
    # at its first micro-member position. Do not "repair" that order here.
    closed = {**read, "sourceEventID": "closed", "kind": "write", "sessionID": "s",
              "availableAt": "2026-01-01T00:00:03Z", "memberWriteEventIDs": ["m1", "m2"],
              "serialized": canonical({"kind": "write", "content": "thought"})}
    earlier_read = {**read, "sessionID": "s"}
    micros = [{**closed, "sourceEventID": "m1"}, earlier_read, {**closed, "sourceEventID": "m2"}]
    native = native_episode_order(micros, [earlier_read, closed], [])
    assert native == ["closed", "read"]
    mm, _ = timeline([earlier_read, closed], [])
    ee = {**e, "targetBeganAt": "2026-01-01T00:00:04Z", "contextBlockIDs": native}
    for v in VARIANTS:
        pp = make_prompt(ee, v, mm, native, packer, tok, {}, 1000)
        assert [b["eventID"] for b in pp["retainedBlocks"]] == native
        assert [b["availableAt"] for b in pp["retainedBlocks"]] != sorted(b["availableAt"] for b in pp["retainedBlocks"])
    # Different READ availability across pipelines must affect eligibility,
    # without changing the common target or query.
    later_read = {**earlier_read, "availableAt": "2026-01-01T00:00:05Z"}
    old_map, _ = timeline([earlier_read], []); new_map, _ = timeline([later_read], [])
    ee = {**ee, "contextBlockIDs": []}
    op = make_prompt(ee, "old", old_map, ["read"], packer, tok, {}, 1000)
    np = make_prompt(ee, "new", new_map, ["read"], packer, tok, {}, 1000)
    assert len(op["retainedBlocks"]) == 1 and not np["retainedBlocks"]
    assert op["query"] == np["query"]
    rejected(lambda: make_prompt({**ee, "contextBlockIDs": ["read"]}, "new", new_map, ["read"], packer, tok, {}, 1000))
    pool = [{**e, "exampleID": str(i), "modelFacingDestination": {"application": "AB"[i % 2]}} for i in range(40)]
    assert select_cohort(pool, 20, 17) == select_cohort(list(reversed(pool)), 20, 17)
    assert select_cohort(pool, 20, 17)[1] == {"A": 10, "B": 10}
    print("PASS: factorial pairing, native order preservation, independent READ availability, causal cutoff, member exclusion, stable cohort, target/query integrity, paste serialization, privacy propagation, route guard")


if __name__ == "__main__":
    main()

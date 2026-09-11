#!/usr/bin/env python3
"""Rebuild every frozen paired prompt locally; validate provenance and privacy."""
import argparse
import json
from pathlib import Path

from phase1_read_model_comparison import (
    TOKENIZER_REPO, TOKENIZER_REVISION, VARIANTS, Privacy, audit_records, canonical,
    file_hash, import_packer, make_prompt, native_episode_order, rows,
)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", type=Path)
    ap.add_argument("--report", type=Path)
    args = ap.parse_args()
    root = args.input
    hashes = json.loads((root / "artifact-hashes.json").read_text())
    for name, digest in hashes.items():
        assert file_hash(root / name) == digest, f"Artifact changed: {name}"
    plan = json.loads((root / "plan.json").read_text())
    for name, digest in (plan["sourceHashes"] | plan["rawDigests"]).items():
        assert file_hash(name) == digest, f"Bound input/producer changed: {name}"
    config_path = next(Path(k) for k in plan["sourceHashes"] if Path(k).name == "config.json")
    config = json.loads(config_path.read_text())
    original = Path(config["newCorpus"])
    original_events = {r["sourceEventID"]: r for r in rows(original / "events.jsonl")}
    privacy = Privacy(original_events, config["privacyPolicy"])
    cohort = list(rows(root / "cohort.jsonl"))
    selected = {r["exampleID"]: r for r in cohort}
    matched = set()
    for source in rows(original / "examples.jsonl"):
        e = selected.get(source["exampleID"])
        if e is None:
            continue
        for field in ("query", "target", "targetMask", "targetBeganAt", "targetAvailableAt", "episode", "targetSourceRecordIDs", "contextBlockIDs"):
            assert e[field] == source[field], f"Changed frozen target/query/provenance: {field}"
        matched.add(source["exampleID"])
    assert matched == set(selected)
    prompts = list(rows(root / "prompts.jsonl"))
    requests = list(rows(root / "requests.planned.jsonl"))
    report = audit_records(cohort, prompts, requests, privacy, config["contextTokenBudget"])
    maps, orders = {}, {}
    for variant in VARIANTS:
        events = list(rows(root / f"{variant}-context-events.jsonl"))
        maps[variant] = {e["sourceEventID"]: e for e in events}
        orders[variant] = [e["sourceEventID"] for e in events]
    write_projection = lambda v: {k: e["serialized"] for k, e in maps[v].items() if e["kind"] == "write"}
    assert write_projection("old") == write_projection("new")
    original_old = [e for folder in config["oldCausalSources"] for e in rows(Path(folder) / "events.jsonl")]
    saved_order = list(rows(root / "old-native-event-order.jsonl"))
    assert saved_order == [{k: e[k] for k in ("sourceEventID", "kind", "sessionID", "availableAt")} for e in original_old]
    common_writes = [e for e in original_events.values() if e["kind"] == "write"]
    expected_old_order = native_episode_order(original_old, [e for e in original_old if e["kind"] == "read"] + common_writes,
                                              list(rows(original / "gaps.jsonl")))
    assert expected_old_order == orders["old"], "Old history was re-sorted after native episode projection"
    for event in original_old:
        if event["kind"] == "read":
            assert maps["old"][event["sourceEventID"]]["availableAt"] == event["availableAt"]
    for key, event in original_events.items():
        assert maps["new"][key]["availableAt"] == event["availableAt"]
    packer = import_packer(config["frozenProducer"])
    from transformers import AutoTokenizer
    snapshot, _ = packer.resolve_tokenizer_snapshot(TOKENIZER_REPO, TOKENIZER_REVISION, True)
    tokenizer = AutoTokenizer.from_pretrained(snapshot, local_files_only=True, use_fast=True, split_special_tokens=True)
    cache = {v: {} for v in VARIANTS}
    for i, prompt in enumerate(prompts, 1):
        v = prompt["variant"]
        rebuilt = make_prompt(selected[prompt["exampleID"]], v, maps[v], orders[v], packer,
                              tokenizer, cache[v], config["contextTokenBudget"])
        assert rebuilt == prompt, "Saved prompt differs from independently repacked event lineage"
        if i % 20 == 0:
            print(f"Re-audited {i}/{len(prompts)} actual prompts", flush=True)
    report.update(sourceTargetsQueriesMasksUnchanged=True, allPromptsRepackedIdentically=True,
                  boundFileHashesVerified=True, actualProviderCalls=0,
                  nativeHistoryOrdersVerified=True, sourceAvailabilityTimestampsUnchanged=True,
                  sharedTimestampResortApplied=False)
    if args.report:
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

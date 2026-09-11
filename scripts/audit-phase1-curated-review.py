#!/usr/bin/env python3
"""Audit both review arms and pack changed-case canaries without transmission."""
import argparse
from array import array
from collections import Counter
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import sys

os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false",
                  USE_TORCH="0", USE_TF="0", USE_FLAX="0")
from phase1_jsonl import JSONLSequence

ROOT = Path(__file__).resolve().parents[1]
PREP = ROOT / "coupled-data/sep02-10-training-prep-20260910"


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / (name + ".py"))
    value = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = value
    spec.loader.exec_module(value)
    return value


h = load("curate-phase1-episode-joins")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def baseline_regression(before, after, restorations, batch_reads=(), suppressed_reads=()):
    fields = ("target", "query", "targetMask", "targetBeganAt", "targetAvailableAt", "targetEventID", "conditioningState")
    targets = lambda path: {r["exampleID"]: fingerprint({k: r[k] for k in fields}) for r in h.rows(path / "cohort.jsonl")}
    previous, current = targets(before), targets(after)
    retired = {eid for r in restorations for eid in r["retiredExampleIDs"]}
    added = {r["example"]["exampleID"] for r in restorations if r.get("targetEligible", True)}
    assert previous.keys()-current.keys() == previous.keys() & retired
    assert current.keys()-previous.keys() <= added
    assert all(previous[eid] == current[eid] for eid in previous.keys() & current.keys()), "Retained target/query/mask/timing changed"
    history_fields = ("serialized", "beganAt", "availableAt", "memberWriteEventIDs", "sourceRecordIDs", "lossEligibility")
    def writes(path):
        return {r["sourceEventID"]: (fingerprint(r), fingerprint({k: r.get(k) for k in history_fields})) for r in h.rows(path / "common-write-events.jsonl")}
    old, new = writes(before), writes(after)
    retired_writes = {eid for r in restorations for eid in r["retiredWriteEventIDs"]}
    restored_writes = {r["event"]["sourceEventID"] for r in restorations}
    assert old.keys()-new.keys() == old.keys() & retired_writes
    assert new.keys()-old.keys() <= restored_writes
    retained = old.keys() & new.keys()
    assert all(old[eid][1] == new[eid][1] for eid in retained), "Retained history content/timing changed"
    assert all(old[eid][0] == new[eid][0] for eid in retained-restored_writes), "Unrelated history metadata changed"
    reads = {}
    for arm in ("old", "new"):
        lookup = lambda path: {e["sourceEventID"]: fingerprint(e) for e in h.rows(path / arm / "events.jsonl") if e["kind"] == "read"}
        a, b = lookup(before), lookup(after)
        if arm == 'new':
            replacements = {r['eventID']:r for r in batch_reads}
            assert set(a)-set(b) == set(suppressed_reads)
            assert not set(b)-set(a)
            for eid in set(a)&set(b):
                if eid in replacements:
                    assert a[eid] == replacements[eid]['beforeSHA256']
                    assert b[eid] == fingerprint(replacements[eid]['event'])
                else:
                    assert a[eid] == b[eid], 'Unreviewed READ change'
        else:
            assert a == b, 'Old READ arm changed'
        reads[arm] = sum(a[eid] == b.get(eid) for eid in a)
    return {"status": "passed", "before": str(before), "after": str(after),
            "unchangedTargetContracts": len(previous.keys() & current.keys()), "retiredTargets": sorted(previous.keys()-current.keys()),
            "unchangedHistoricalContent": len(retained), "byteIdenticalHistoricalWrites": sum(old[eid][0] == new[eid][0] for eid in retained),
            "retiredHistoricalWrites": len(old.keys()-new.keys()), "restoredHistoricalWrites": len(new.keys()-old.keys()),
            "unchangedReadRecords": reads}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--baseline", type=Path, help="Additional strict retained-contract/READ regression check")
    p.add_argument("--save-packed-contexts", action="store_true", help="Save decoded actual reference-tokenizer canary inputs for manual inspection")
    a = p.parse_args()
    manifest = json.loads((a.input / "review.json").read_text())
    for path, digest in manifest["sourceHashes"].items():
        assert h.sha(Path(path)) == digest, ("Changed source", path)
    for name, digest in manifest["artifactsSHA256"].items():
        assert h.sha(a.input / name) == digest, ("Changed artifact", name)
    cohort = {r["exampleID"]: r for r in h.rows(a.input / "cohort.jsonl")}
    # Drop expanded prompts and token arrays one row at a time.
    frozen = {r["exampleID"]: {k: r[k] for k in ("target", "targetMask", "query", "targetBeganAt", "targetAvailableAt", "targetEventID")}
              for r in h.rows(PREP / "paired-qwen38-reference32k/cohort.jsonl")}
    fixed_fields = ("target", "targetMask", "query", "targetBeganAt", "targetAvailableAt", "targetEventID")
    unchanged = 0
    repairs_path = a.input / "target-corrections.json"
    repairs = json.loads(repairs_path.read_text()) if repairs_path.exists() else []
    if repairs:
        assert h.sha(repairs_path) == manifest["targetCorrectionsSHA256"]
    repaired = {r["targetEventID"]: r for r in repairs}
    reviewed_examples = set()
    for eid in cohort.keys() & frozen.keys():
        current, previous = cohort[eid], frozen[eid]
        repair = repaired.get(current["targetEventID"])
        if repair:
            assert all(current[k] == previous[k] for k in fixed_fields if k not in {"target", "targetAvailableAt"}), eid
            assert previous["target"]["segments"] == [{"type": "authored_text", "content": repair["observedAXContent"]}]
            assert current["target"]["segments"] == [{"type": "authored_text", "content": repair["resolvedContent"]}]
            if "resolvedContent" in current["target"]:
                assert current["target"]["resolvedContent"] == repair["resolvedContent"]
            assert current["targetAvailableAt"] == repair["availableAt"] >= previous["targetAvailableAt"]
            assert current["visualTargetCuration"] == repair
            reviewed_examples.add(eid)
        else:
            assert all(current[k] == previous[k] for k in fixed_fields), eid
            unchanged += 1
    assert len(reviewed_examples) == len(repairs), "Unused/duplicate target correction"
    common_writes = {e["sourceEventID"]: e for e in h.rows(a.input / "common-write-events.jsonl")}
    batch_path = a.input / 'batch-changes.jsonl'
    batch, batch_reads = [], []
    if batch_path.exists():
        batch = list(h.rows(batch_path))
        batch_reads = json.loads((a.input/'batch-read-changes.json').read_text())
        spec = json.loads((a.input/'batch-reviews.json').read_text())
        assert h.sha(a.input/'batch-read-changes.json') == manifest['batchReadChangesSHA256']
        assert h.sha(a.input/'batch-reviews.json') == manifest['batchReviewsSHA256']
        curator = h.load_module('apply-phase1-reviewed-batch')
        rebuilt, rebuilt_reads, _ = curator.make_batch(Path(manifest['parentCorpus']), spec)
        assert rebuilt == batch and rebuilt_reads == batch_reads, 'Batch does not reproduce from bound evidence'
        for c in batch:
            assert common_writes[c['event']['sourceEventID']] == c['event']
            assert not set(c['retiredWriteEventIDs']) & common_writes.keys()
            assert not set(c['retiredExampleIDs']) & cohort.keys()
            if c['targetEligible']:
                current = cohort[c['example']['exampleID']]
                assert all(current[k] == c['example'][k] for k in fixed_fields)
                assert current['conditioningState'] == c['example']['conditioningState']
                assert current['cursorFidelity'] is None
            else:
                assert c['example'] is None and c['event']['lossEligibility'] == 'ineligible'
        del rebuilt, rebuilt_reads
    restored_path = a.input / "prompt-restorations.jsonl"
    restorations = list(h.rows(restored_path)) if restored_path.exists() else []
    if restorations:
        assert h.sha(restored_path) == manifest["promptRestorationsSHA256"]
        restorer = h.load_module("restore-phase1-prompt-onsets")
        raw_by_path = {}
        for r in restorations:
            review = r["proof"]["review"]
            raw_by_path.setdefault(ROOT / review["rawPath"], set()).update(review["attemptRecordIDs"])
        raw = {}
        for path, needed in raw_by_path.items():
            for r in h.rows(path):
                if r.get("recordID") in needed:
                    raw[r["recordID"]] = r
        for restored in restorations:
            proof = restored["proof"]
            attempts = [raw[rid] for rid in proof["review"]["attemptRecordIDs"]]
            assert {r["recordID"]: restorer.projection.stable_id("", r) for r in attempts} == proof["rawRecordHashes"]
            content, epochs = restorer.reconstruct(attempts)
            assert epochs == proof["epochs"]
            if not restored.get("targetEligible", True):
                assert restored["example"]["exampleID"] not in cohort
                current = restored["example"]  # audit projection, never a training example
                assert restored["event"]["lossEligibility"] == "ineligible"
                assert proof["targetEligibility"] == "history_only_novel_read"
                assert not restored["suppressedReadEventIDs"], "Novel READ was erased with history-only repair"
            else:
                current = cohort[restored["example"]["exampleID"]]
            event = common_writes[current["targetEventID"]]
            assert current["target"]["segments"] == [{"type": "authored_text", "content": content}]
            assert current["conditioningState"] == attempts[0]["conditioningState"]
            assert current["cursorFidelity"] is None, "Later fragment's cursor validation was carried into restored onset"
            assert current["conversionVersion"] == restorer.VERSION
            assert current["targetMetadata"]["memberWriteEventIDs"] == event["memberWriteEventIDs"]
            assert current["targetMetadata"]["availableAt"] == event["availableAt"]
            assert current["query"] == restorer.projection.serialize_query(attempts[0]["conditioningState"], event["modelFacingDestination"])
            assert current["targetBeganAt"] == event["beganAt"] == attempts[0]["beganAt"]
            assert current["targetAvailableAt"] == event["availableAt"] == proof["evidenceAvailableAt"]
            assert event["availableAt"] >= attempts[-1]["terminalDecisionAt"]
            assert json.loads(event["serialized"])["authorshipSegments"] == current["target"]["segments"]
            assert not set(restored["retiredExampleIDs"]) & cohort.keys()
            assert not set(restored["retiredWriteEventIDs"]) & common_writes.keys()
    members = [mid for e in common_writes.values() for mid in e["memberWriteEventIDs"]]
    assert len(set(members)) == len(members)
    canaries = (set(cohort) - set(frozen)) | reviewed_examples
    for c in batch:
        following = sorted((r for r in cohort.values() if r['targetBeganAt'] > c['event']['availableAt']),key=lambda r:r['targetBeganAt'])
        canaries.update(r['exampleID'] for r in following[:2])
    for restored in restorations:
        if not restored.get("targetEligible", True):
            following = sorted((r for r in cohort.values() if r["targetBeganAt"] > restored["event"]["availableAt"]),
                               key=lambda r: r["targetBeganAt"])
            canaries.update(r["exampleID"] for r in following[:2])
    prior_order = list(frozen)
    for case in (1, 100, 240, 269, 275, 370, 377, 499, 575, 585, 687):
        eid = prior_order[case - 1]
        if eid in cohort:
            canaries.add(eid)
    from transformers import AutoTokenizer
    settings = json.loads((PREP / "paired-qwen38-reference32k/packing-audit.json").read_text())
    tokenizer = AutoTokenizer.from_pretrained(settings["contextBudget"]["referenceTokenizerDirectory"],
        local_files_only=True, trust_remote_code=False, split_special_tokens=True)
    packer = load("pack-phase1-dataset")
    marker = packer.encode_plain_text(tokenizer, "<|paste|>")
    a.output.mkdir(parents=True, exist_ok=False)
    if a.baseline:
        h.save(a.output / "baseline-regression.json", baseline_regression(a.baseline, a.input, restorations + batch,
            batch_reads, manifest.get('suppressedNonNovelReadsNewArm', [])))
    packed_reports, signatures = [], {}
    for arm in ("old", "new"):
        path = a.input / arm
        events = {e["sourceEventID"]: e for e in h.rows(path / "events.jsonl")}
        assert not {eid for r in restorations for eid in r["suppressedReadEventIDs"]} & events.keys()
        for eid, repair in repaired.items():
            event = events[eid]
            assert json.loads(event["serialized"])["authorshipSegments"] == [{"type": "authored_text", "content": repair["resolvedContent"]}]
            assert event["availableAt"] == repair["availableAt"]
            assert {o["recordID"] for o in repair["observations"]} <= set(event["sourceRecordIDs"])
            # Privacy.filter_stream deliberately strips audit-only payloads
            # from model-facing arm events. Verify provenance in the preserved
            # common WRITE artifact and the actual payload in each arm above.
            audit = json.loads(common_writes[eid]["auditSerialized"])
            assert audit["observedAXCompletion"] == repair["observedAXContent"]
            assert audit["visualTargetCuration"] == repair
        blocks = {b["contextBlockID"]: b for b in h.rows(path / "context-blocks.jsonl")}
        event_map = dict(events)
        for eid, b in blocks.items():
            if eid not in event_map:
                event_map[eid] = {"sourceEventID": eid, "kind": "coverage_gap", "availableAt": b["beforeAt"], "serialized": b["serialized"]}
        signatures[arm] = fingerprint({eid: e["serialized"] for eid, e in events.items() if e["kind"] == "write"})
        cache, visited = {}, []
        for r in JSONLSequence(path / "examples.jsonl"):
            eid = r["exampleID"]
            visited.append(eid)
            assert all(r[k] == cohort[eid][k] for k in fixed_fields)
            ids = r["contextBlockIDs"]
            assert len(ids) == len(set(ids))
            assert all(event_map[k]["availableAt"] < r["targetBeganAt"] for k in ids)
            assert not ({r["targetEventID"]} | set(r["episode"]["memberWriteEventIDs"])).intersection(ids)
            assert not set(members).intersection(ids), "micro-WRITE in model history"
            assert r["targetMask"] == cohort[eid]["targetMask"]
            assert r["targetMask"]["eosTokenCount"] == 1
            if eid not in canaries:
                continue
            # Exact suffix plus first overflowing event; the production packer
            # does not backfill earlier events after dependency rendering.
            budget = 32768 - len(packer.encode_plain_text(tokenizer, packer.DEFAULT_TASK_INSTRUCTION + "\n")) - len(packer.encode_plain_text(tokenizer, r["query"]))
            suffix = []
            for bid in reversed(ids):
                text = event_map[bid]["serialized"]
                if bid not in cache:
                    cache[bid] = (text, array("I", packer.encode_plain_text(tokenizer, text + "\n")))
                suffix.append(bid); budget -= len(cache[bid][1])
                if budget < 0:
                    break
            suffix.reverse()
            context = "\n".join(event_map[bid]["serialized"] for bid in suffix)
            projection = {"exampleID": eid, "query": r["query"], "contextBlockIDs": suffix,
                          "context": context, "modelInput": context + "\n" + r["query"] if context else r["query"]}
            packed = packer.pack_model_input(projection, event_map,
                tokenizer, 32768, packer.DEFAULT_TASK_INSTRUCTION, cache, dependency_aware_read_novelty=arm == "new")
            if a.save_packed_contexts:
                directory = a.output / 'packed-contexts' / arm
                directory.mkdir(parents=True, exist_ok=True)
                with (directory / (eid + '.txt')).open('x') as handle:
                    handle.write(tokenizer.decode(packed['inputIDs'], skip_special_tokens=False))
            cache = {key: cache[key] for key in suffix}
            target_ids, spans, pastes = packer.pack_target(r["target"]["segments"], tokenizer, marker, tokenizer.eos_token_id)
            inputs = list(packed["inputIDs"]) + target_ids
            labels = [-100] * len(packed["inputIDs"]) + target_ids
            assert labels[-1] == tokenizer.eos_token_id and target_ids.count(tokenizer.eos_token_id) == 1
            assert all(y == -100 or y == token for y, token in zip(labels, inputs))
            assert len(packed["inputIDs"]) <= 32768
            packed_reports.append({"arm": arm, "exampleID": eid, "inputTokens": len(packed["inputIDs"]),
                "targetTokens": len(target_ids), "pasteActions": pastes,
                "inputIDsSHA256": packer.token_ids_sha256(packed["inputIDs"]),
                "targetIDsSHA256": packer.token_ids_sha256(target_ids), "readRenderingCounts": packed["readRenderingCounts"]})
        assert visited == list(cohort), "Paired chronology changed"
    assert signatures["old"] == signatures["new"], "Historical WRITEs differ between arms"
    per_example = {}
    for r in packed_reports:
        prior = per_example.setdefault(r["exampleID"], r["targetIDsSHA256"])
        assert prior == r["targetIDsSHA256"]
    report = {"status": "passed", "version": "phase1-curated-review-audit-v2",
        "examplesPerArm": len(cohort), "unchangedOriginalTargetsQueriesMasks": unchanged,
        "reviewedCorrectedOriginalTargets": len(reviewed_examples),
        "restoredPromptOnsets": len(restorations),
        "batchReviewedCompositions": len(batch), "batchReadRepairs":len(batch_reads),
        "newOrMergedExamples": len(set(cohort) - set(frozen)), "retiredOriginalExamples": len(set(frozen) - set(cohort)),
        "packedCanariesPerArm": len(canaries), "pairedHistoricalWritesSHA256": signatures["old"],
        "strictCausalCutoffsAndNoMicroWriteHistory": True, "sameTargetTokenIDsAcrossArms": True,
        "exactlyOneEOSAndInputLossMasked": True, "tokenizerScope": "frozen Qwen3.5 reference packer; Qwen3.8 native pack still follows full curation",
        "trainingFreezeApproved": False, "sourceReviewSHA256": h.sha(a.input / "review.json"),
        "scriptSHA256": h.sha(Path(__file__)), "peakRSSMiB": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 ** 2 if sys.platform == "darwin" else 1024)}
    h.save(a.output / "audit.json", report)
    h.save(a.output / "packed-canaries.json", packed_reports)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

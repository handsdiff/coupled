#!/usr/bin/env python3
"""Rebuild paired review corpora with common repaired WRITEs and separate READs.

Local curation checkpoint only. No training, provider calls, or frozen-pack
replacement. Preserves raw evidence and the old READ arm's native ordering.
"""
import argparse
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

from phase1_example_storage import write_examples
from phase1_read_model_comparison import Privacy, native_episode_order, target_text

ROOT = Path(__file__).resolve().parents[1]
PREP = ROOT / "coupled-data/sep02-10-training-prep-20260910"
VERSION = "phase1-curated-paired-review-v3"


def module(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / (name + ".py"))
    value = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = value
    spec.loader.exec_module(value)
    return value


h = module("curate-phase1-episode-joins")


def text_patches():
    day4 = "coupled-data/phase1-ordinary-work-2026-09-04-1/screenshots/"
    day7 = "coupled-data/phase1-ordinary-work-2026-09-07-1/screenshots/"
    scroll = [
        ("mplete response. Long ChatGPT output\nearlier portions offscreen.", "complete response. Long ChatGPT output can auto-scroll earlier portions offscreen.", 1),
        ("rough variants of one pairwise similarity algorithm:", "through variants of one pairwise similarity algorithm:", 1),
        ("\ngh\nsimilarity → suppress everything", "\nhigh similarity → suppress everything", 1),
        ("exception is al\nscroll jump", "exception is a\nscroll jump", 1),
    ]
    return [
        ("evt_4a702feff9074612c14111f9504bb8d598464a2d1d4a3aca7cac3bb0039a02e5", day4 + "visual-24E88496-F124-4B92-A93A-8F20820FCBE5.png", scroll),
        ("evt_ab11dc4fd23cf26fd17e0a65a1a6f1f0c5847a4789d4c00dc060996b3966a581", day4 + "visual-24E88496-F124-4B92-A93A-8F20820FCBE5.png", scroll),
        ("evt_8f94cdc3c953391f9f72d8b742795426bac7ade9cd87ea518bc6e6921883fc31", day7 + "B873BD7C-8177-465E-8AE0-5447572ED672.png", [("›ther interruptions", "Other interruptions", 1)]),
        ("evt_3dbb8149c92e39111856e2e0486c48f843f907c75211bf5f1a7cfce1148b8e12", day7 + "visual-187A9F40-31E4-49E9-A32B-A050BDFB894E.png", [("*a>> sol", "astra >> sol", 1)]),
        ("evt_e5dedcfb3f6278679a78e477d7a7b35a8637e71b0d0eddc3056ee705450e2c52", day4 + "visual-6EF0E7D2-4BF6-4945-9BA1-399B439D54CB.png", [("RHF", "RLHF", 3)]),
        ("evt_f0da0092218a3f51b6e4920eb1a2b68eac70b09df699bcd1614a8513ee7c0b0c", day4 + "visual-6EF0E7D2-4BF6-4945-9BA1-399B439D54CB.png", [("RHF", "RLHF", 2)]),
        ("evt_6daebcbaf972ccb3da79ae0915320693c2d2f65a7f7c17351937646ba5141aec", day4 + "visual-ED40D1C6-D8AC-4530-845A-5A142784E1C6.png", [("\nidentity.e panes.\n", "\n", 1)]),
        ("evt_73900df9ac3f11dd760f6d5cc45d5f1ff57b8ab282146eadbe46009f15ac4452", day7 + "visual-67D79D17-9323-4252-A66E-9977028C60D0.png", [(
            "• Ran rg\nsed\n-files -g AGENTS.md -g \"*retry*' -g '*repeat*' -g '*comparison*' scripts .agents -codex 2>/dev/null | head -100\n'1,240p\nscripts/run-phasel-read-model-comparison.py\nsed\n'1,150p' coupled-data/phase1-read-pipeline-factorial-full-20260907/holistic-v3/policy.json\nscripts/audit-phase1-read-model-comparison.py\nscripts/prepare-phasel-read-model-comparison-py",
            "• Ran rg --files -g AGENTS.md -g '*retry*' -g '*repeat*' -g '*comparison*' scripts .agents .codex 2>/dev/null | head -100\nsed -n '1,240p' scripts/run-phase1-read-model-comparison.py\nsed -n '1,150p' coupled-data/phase1-read-pipeline-factorial-full-20260907/holistic-v3/policy.json\nscripts/audit-phase1-read-model-comparison.py\nscripts/prepare-phase1-read-model-comparison.py", 1)]),
    ]


def patch_read(event, edits):
    event = deepcopy(event)
    original = json.loads(event["serialized"])["content"]
    final = original
    for before, after, count in edits:
        h.require(final.count(before) == count, "Unexpected correction match count")
        final = final.replace(before, after)
    for key in ("serialized", "auditSerialized"):
        if key in event:
            payload = json.loads(event[key])
            if "content" in payload:
                h.require(payload["content"] == original, "Unreviewed alternate READ representation")
                payload["content"] = final
                event[key] = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    novelty = event.get("readNovelty", {})
    if novelty.get("content") == original:
        novelty["content"] = final
    else:
        # The second scroll frame is an exact-repeat dependency with no delta.
        # Keep suppression intact while correcting its full-state fallback.
        h.require(novelty.get("content") == "" and novelty.get("dependsOnEventID"), "Partial delta requires separate correction")
    if "currentCharacterCount" in novelty:
        novelty["currentCharacterCount"] = len(final)
    if "overlapCharacterCount" in novelty and novelty.get("content") == "":
        novelty["overlapCharacterCount"] = len(final)
    return event, {"eventID": event["sourceEventID"], "before": original, "after": final, "edits": edits}


def repair_visual_target(event, example, review, raw):
    """Curated rendered-text correction; never pretend the AX value was right."""
    h.require(review["decision"] == "visually_verified_submitted_authored_text", "Unsupported target review")
    h.require(review["reviewedBy"] == "assistant_visual_evidence_review", "Missing visual review")
    h.require(event["sourceEventID"] == review["targetEventID"] == example["targetEventID"], "Wrong target identity")
    h.require(event["memberWriteEventIDs"] == review["memberWriteEventIDs"], "Changed target members")
    h.require(example["targetBeganAt"] == review["beganAt"], "Changed target onset")
    h.require(example["targetAvailableAt"] == review["originalAvailableAt"], "Changed target availability")
    h.require(hashlib.sha256(example["query"].encode()).hexdigest() == review["querySHA256"], "Changed target query")
    old = review["observedAXContent"]
    h.require(example["target"]["segments"] == [{"type": "authored_text", "content": old}], "Unreviewed target or paste lineage")
    payload = json.loads(event["serialized"])
    h.require(payload["authorshipSegments"] == [{"type": "authored_text", "content": old}], "Unreviewed historical completion")
    attempts = [raw[rid] for rid in review["attemptRecordIDs"]]
    h.require(attempts[0]["before"]["value"] == "" and not attempts[0]["before"].get("valueWasTruncated"), "No empty pre-mutation baseline")
    h.require(set(review["attemptRecordIDs"]) <= set(event["sourceRecordIDs"]), "Missing attempt lineage")
    h.require(all(not r.get("pasteCheckpoints") and not ({"paste", "shortcut"} & set(r.get("inputHints", []))) for r in attempts), "Ambiguous target authorship")
    terminal = attempts[-1]
    returns = terminal.get("returnCheckpoints", [])
    h.require(terminal.get("boundaryReason") == "return_pressed" and len(returns) == 1, "Unproven submission")
    h.require(returns[0]["observation"]["value"] == old, "AX discrepancy changed")
    return_at = returns[0]["inputObservedAt"]
    pre, post = [raw[o["recordID"]] for o in review["observations"]]
    for reference, record in zip(review["observations"], (pre, post)):
        h.require(record["sessionID"] == event["sessionID"], "Cross-session screenshot")
        h.require(record["capturedAt"] == reference["capturedAt"] and record["screenshotSHA256"] == reference["sha256"], "Changed visual evidence")
        h.require(h.sha(ROOT / reference["path"]) == reference["sha256"], "Changed screenshot bytes")
    last_mutation = max(i["observedAt"] for r in attempts for i in r["inputEvents"] if i.get("mutationCapable"))
    h.require(last_mutation <= pre["capturedAt"] < return_at < post["capturedAt"], "Screenshots do not bracket final submission")
    h.require(review["availableAt"] == max(review["originalAvailableAt"], post["capturedAt"]), "Backdated visual completion")
    content = review["resolvedContent"]
    h.require(isinstance(content, str) and len(content.strip()) >= 4 and content != old, "Invalid visual correction")
    event, example = deepcopy(event), deepcopy(example)
    segments = [{"type": "authored_text", "content": content}]
    payload["authorshipSegments"] = segments
    event["serialized"] = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    audit = json.loads(event["auditSerialized"])
    audit.update(authorshipSegments=segments, resolvedCompletion=content, observedAXCompletion=old,
                 visualTargetCuration=review)
    event["auditSerialized"] = json.dumps(audit, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    event["availableAt"] = review["availableAt"]
    event["sourceRecordIDs"] = sorted(set(event["sourceRecordIDs"]) | {r["recordID"] for r in review["observations"]})
    example["target"]["segments"] = segments
    if "resolvedContent" in example["target"]:
        example["target"]["resolvedContent"] = content
    example.update(targetAvailableAt=event["availableAt"], targetSourceRecordIDs=event["sourceRecordIDs"], visualTargetCuration=review)
    return event, example


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--joins", required=True, type=Path)
    p.add_argument("--recoveries", required=True, type=Path)
    p.add_argument("--replay", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--target-adjudications", type=Path)
    p.add_argument("--prompt-restorations", type=Path)
    a = p.parse_args()
    cohort = {r["exampleID"]: {k: r[k] for k in ("target", "query", "targetMask", "targetBeganAt", "targetAvailableAt", "targetEventID")}
              for r in h.rows(PREP / "paired-qwen38-reference32k/cohort.jsonl")}
    excluded = {r["exampleID"] for r in h.rows(PREP / "episodes/examples.jsonl") if r["exampleID"] not in cohort}
    original = {e["sourceEventID"]: e for e in h.rows(PREP / "episodes/events.jsonl")}
    joined = list(h.rows(a.joins / "episodes/events.jsonl"))
    writes = [e for e in joined if e["kind"] == "write"]
    suppress = set()
    for c in h.rows(a.joins / "candidates.jsonl"):
        for assessment in c.get("causalEvidence", {}).get("interveningReadAssessments", []):
            if assessment.get("status") in h.NON_NOVEL:
                suppress.add(assessment["eventID"])
    suppressed_raw = set()
    for e in h.rows(PREP / "micro-corpus/events.jsonl"):
        if e["kind"] == "read" and (e["sourceEventID"] not in original or e["sourceEventID"] in suppress):
            suppressed_raw.update(e["sourceRecordIDs"])
    recoveries = list(h.rows(a.recoveries / "recovered-compositions.jsonl"))
    writes.extend(r["event"] for r in recoveries)
    read_repairs = {eid: (image, edits) for eid, image, edits in text_patches()}
    new_reads, patch_reviews, source_hashes = [], [], {}
    for day in (2, 3, 4, 7):
        path = a.replay / f"sep{day}-causal/events.jsonl"
        source_hashes[str(path.resolve())] = h.sha(path)
        for e in h.rows(path):
            if e["kind"] != "read":
                continue
            overlap = suppressed_raw.intersection(e["sourceRecordIDs"])
            if e["sourceEventID"] in read_repairs:
                image, edits = read_repairs[e["sourceEventID"]]
                e, review = patch_read(e, edits)
                review.update(screenshot=image, screenshotSHA256=h.sha(ROOT / image),
                              disposition="suppressed_reviewed_non_novel_read" if overlap else "retained_corrected_read")
                patch_reviews.append(review)
            if overlap:
                h.require(overlap == set(e["sourceRecordIDs"]), "Mixed suppressed READ lineage")
                continue
            new_reads.append(e)
    h.require(len(patch_reviews) == 8, "Missing READ correction")
    pinned = json.loads((PREP / "inputs.json").read_text())
    old_native, old_reads = [], []
    for source in pinned["oldExistingCausal"] + [str(PREP / "old-sep7-causal")]:
        path = Path(source) / "events.jsonl"
        source_hashes[str(path.resolve())] = h.sha(path)
        for e in h.rows(path):
            old_native.append(e)
            if e["kind"] == "read":
                old_reads.append(e)
    # A recovered raw attempt had no old micro-event. Insert its placeholder in
    # native order without sorting/reordering any existing old event.
    for recovered in sorted(recoveries, key=lambda r: r["event"]["availableAt"]):
        e = recovered["event"]
        proxy = {**e, "sourceEventID": e["memberWriteEventIDs"][0]}
        index = next((i for i, old in enumerate(old_native) if old["availableAt"] > e["availableAt"]), len(old_native))
        old_native.insert(index, proxy)
    gaps = list(h.rows(PREP / "episodes/gaps.jsonl"))
    policy = json.loads((ROOT / "coupled-data/clean-read-closed-write-review-20260911-r2/review.json").read_text())["privacyPolicy"]
    privacy = Privacy({e["sourceEventID"]: e for e in new_reads + writes}, policy)
    examples = []
    for r in h.rows(a.joins / "episodes/examples.jsonl"):
        if r["exampleID"] in excluded:
            continue
        r = {k: v for k, v in r.items() if k not in {"_sharedContext", "context", "modelInput", "contextBlockIDs", "contextEventIDs", "contextSourceRecordIDs", "sourceRecordIDs"}}
        if r["exampleID"] in cohort:
            h.require(all(r[k] == v for k, v in cohort[r["exampleID"]].items()), "Unrelated target/query/mask changed")
        h.require(not privacy.unsafe(r["query"] + json.dumps(r["target"])), "Unsafe curated query/target")
        examples.append(r)
    template_mask = next(iter(cohort.values()))["targetMask"]
    for recovered in recoveries:
        e = recovered["event"]
        h.require(not privacy.unsafe(recovered["query"] + json.dumps(recovered["target"])), "Unsafe recovered query/target")
        examples.append({"schemaVersion": 12, "conversionVersion": VERSION,
            "exampleID": "recovered_example_" + e["sourceEventID"], "sessionID": e["sessionID"],
            "targetEventID": e["sourceEventID"], "targetBeganAt": e["beganAt"], "targetAvailableAt": e["availableAt"],
            "target": recovered["target"], "targetMask": template_mask, "query": recovered["query"],
            "conditioningState": recovered["conditioningState"], "targetSourceRecordIDs": e["sourceRecordIDs"],
            "targetUnitType": "closed_composition_episode", "targetUnitID": e["episodeID"],
            "modelFacingDestination": e["modelFacingDestination"], "logicalDestinationKey": e["logicalDestinationKey"],
            "destinationDerivation": e["destinationDerivation"], "cursorFidelity": None,
            "episode": {"memberWriteEventIDs": e["memberWriteEventIDs"], "memberCount": 1,
                        "decision": "closed_loss_episode", "closureReason": "reviewed_pre_return_submission",
                        "episodeVersion": VERSION, "onsetEvidence": {"requiresProvenPromptOnset": True, "promptOnsetProven": True,
                        "proofReason": "raw_empty_prompt_before_first_mutation"}}, "recovery": recovered["proof"]})
    target_reviews = json.loads(a.target_adjudications.read_text()) if a.target_adjudications else []
    if target_reviews:
        source_hashes[str(a.target_adjudications.resolve())] = h.sha(a.target_adjudications)
        needed, raw = {}, {}
        for review in target_reviews:
            needed.setdefault(ROOT / review["rawPath"], set()).update(review["attemptRecordIDs"] + [o["recordID"] for o in review["observations"]])
            for o in review["observations"]:
                source_hashes[str((ROOT / o["path"]).resolve())] = o["sha256"]
        for path, ids in needed.items():
            source_hashes[str(path.resolve())] = h.sha(path)
            for record in h.rows(path):
                if record.get("recordID") in ids:
                    raw[record["recordID"]] = record
        wi = {e["sourceEventID"]: i for i, e in enumerate(writes)}
        ei = {e["targetEventID"]: i for i, e in enumerate(examples)}
        h.require(len({r["targetEventID"] for r in target_reviews}) == len(target_reviews), "Duplicate target review")
        for review in target_reviews:
            w, e = wi[review["targetEventID"]], ei[review["targetEventID"]]
            writes[w], examples[e] = repair_visual_target(writes[w], examples[e], review, raw)
            h.require(not privacy.unsafe(examples[e]["query"] + json.dumps(examples[e]["target"])), "Unsafe corrected target")
    restorations = []
    if a.prompt_restorations:
        manifest_path = a.prompt_restorations / "restoration.json"
        manifest = json.loads(manifest_path.read_text())
        for path, digest in manifest["sourceHashes"].items():
            h.require(h.sha(Path(path)) == digest, "Prompt restoration source changed: " + path)
        artifact = a.prompt_restorations / "restored-prompts.jsonl"
        h.require(h.sha(artifact) == manifest["artifactSHA256"], "Prompt restoration artifact changed")
        restorations = list(h.rows(artifact))
        retired = [eid for r in restorations for eid in r["retiredWriteEventIDs"]]
        h.require(len(retired) == len(set(retired)), "Overlapping prompt restorations")
        h.require(set(retired) <= {e["sourceEventID"] for e in writes}, "Missing retired prompt WRITE")
        retired_examples = {eid for r in restorations for eid in r["retiredExampleIDs"]}
        h.require(retired_examples <= {e["exampleID"] for e in examples}, "Missing retired prompt target")
        writes = [e for e in writes if e["sourceEventID"] not in retired]
        examples = [e for e in examples if e["exampleID"] not in retired_examples]
        suppressed = {eid for r in restorations for eid in r["suppressedReadEventIDs"]}
        old_reads = [e for e in old_reads if e["sourceEventID"] not in suppressed]
        new_reads = [e for e in new_reads if e["sourceEventID"] not in suppressed]
        native_ids = {e["sourceEventID"] for e in old_native}
        for r in restorations:
            e, example = r["event"], r["example"]
            h.require(not privacy.unsafe(example["query"] + json.dumps(example["target"])), "Unsafe restored prompt")
            writes.append(e)
            if r.get("targetEligible", True):
                examples.append(example)
            else:
                h.require(e["lossEligibility"] == "ineligible" and e["targetExclusionReason"] == "novel_read_during_reconstructed_prompt", "Unexplained history-only restoration")
            # Raw-recovered prefix may have no old semantic member. Its later
            # members still place the complete episode in native stream order.
            if not native_ids.intersection(e["memberWriteEventIDs"]):
                proxy = {**e, "sourceEventID": e["memberWriteEventIDs"][0]}
                index = next((i for i, old in enumerate(old_native) if old["availableAt"] > e["availableAt"]), len(old_native))
                old_native.insert(index, proxy)
        source_hashes[str(manifest_path.resolve())] = h.sha(manifest_path)
        source_hashes[str(artifact.resolve())] = h.sha(artifact)
    examples.sort(key=lambda r: (r["targetBeganAt"], r["exampleID"]))
    a.output.mkdir(parents=True, exist_ok=False)
    h.save(a.output / "read-corrections.json", patch_reviews)
    h.save(a.output / "target-corrections.json", target_reviews)
    h.save_rows(a.output / "prompt-restorations.jsonl", restorations)
    h.save_rows(a.output / "common-write-events.jsonl", writes)
    h.save_rows(a.output / "cohort.jsonl", ({**r, "chronologicalOrdinal": i, "targetText": target_text(r["target"])} for i, r in enumerate(examples)))
    counts = {}
    for arm, reads in (("old", old_reads), ("new", new_reads)):
        path = a.output / arm
        path.mkdir()
        events = reads + writes
        event_map = {e["sourceEventID"]: e for e in events}
        if arm == "old":
            order = native_episode_order(old_native, events, gaps)
        else:
            ordered = sorted(events, key=lambda e: (e["availableAt"], e.get("beganAt") or e["availableAt"], e["sourceEventID"]))
            order = [e["sourceEventID"] for e in ordered] + [g["contextBlockID"] for g in gaps]
            gapmap = {g["contextBlockID"]: g for g in gaps}
            order.sort(key=lambda eid: ((event_map[eid]["availableAt"] if eid in event_map else gapmap[eid]["beforeAt"]), eid))
        safe = privacy.filter_stream(events)
        by_event = {e["sourceEventID"]: e for e in safe}
        by_block = {g["contextBlockID"]: g for g in gaps}
        by_block.update({e["sourceEventID"]: {"contextBlockID": e["sourceEventID"], "contextBlockType": "semantic_event",
            "availableAt": e["availableAt"], "sessionID": e["sessionID"], "serialized": e["serialized"], "sourceEventID": e["sourceEventID"]} for e in safe})
        h.require(set(order) == set(by_block) and len(order) == len(by_block), "Incomplete/duplicate paired history")
        h.save_rows(path / "events.jsonl", safe)
        h.save_rows(path / "context-blocks.jsonl", (by_block[eid] for eid in order))
        def rendered():
            for i, original_example in enumerate(examples):
                r = deepcopy(original_example)
                cutoff = r["targetBeganAt"]
                ids = [eid for eid in order if (by_block[eid].get("availableAt") or by_block[eid]["beforeAt"]) < cutoff]
                h.require(not ({r["targetEventID"]} | set(r["episode"]["memberWriteEventIDs"])).intersection(ids), "Target leakage")
                r.update(chronologicalOrdinal=i, experimentBlockID=f"block-{i // 50 + 1:04d}", contextBlockIDs=ids)
                r["contextEventIDs"] = [eid for eid in ids if eid in by_event]
                r["contextSourceRecordIDs"] = sorted({rid for eid in r["contextEventIDs"] for rid in by_event[eid]["sourceRecordIDs"]})
                r["sourceRecordIDs"] = sorted(set(r["targetSourceRecordIDs"] + r["contextSourceRecordIDs"]))
                r["context"] = "\n".join(by_block[eid]["serialized"] for eid in ids)
                r["modelInput"] = r["context"] + "\n" + r["query"] if r["context"] else r["query"]
                yield r
        storage = write_examples(path / "examples.jsonl", rendered(), compact=True)
        counts[arm] = {"reads": len(reads), "writes": len(writes), "examples": len(examples), "exampleStorage": storage}
    h.save(a.output / "review.json", {"version": "phase1-curated-paired-review-v5" if restorations else VERSION, "trainingFreezeApproved": False,
        "counts": counts, "sameTargetsQueriesMasksAndWriteHistoryAcrossArms": True,
        "readCorrectionsNewArmOnly": sum(r["disposition"] == "retained_corrected_read" for r in patch_reviews),
        "reviewedReadCorrections": len(patch_reviews), "recoveredSubmittedCompositions": len(recoveries),
        "reviewedTargetCorrections": len(target_reviews), "targetCorrectionsSHA256": h.sha(a.output / "target-corrections.json"),
        "restoredPromptOnsets": len(restorations), "promptRestorationsSHA256": h.sha(a.output / "prompt-restorations.jsonl"),
        "historyOnlyPromptRestorations": sum(not r.get("targetEligible", True) for r in restorations),
        "remainingShortcutAttempts": json.loads((a.recoveries / "recovery.json").read_text())["remainingAttemptsPending"], "remainingCompositionAndContextIssues": True,
        "sourceHashes": {**source_hashes, str(Path(__file__).resolve()): h.sha(Path(__file__)),
                         str(a.joins.resolve() / "curation.json"): h.sha(a.joins / "curation.json"),
                         str(a.recoveries.resolve() / "recovery.json"): h.sha(a.recoveries / "recovery.json")},
        "artifactsSHA256": {str(p.relative_to(a.output)): h.sha(p) for p in a.output.rglob("*.jsonl")}})
    print(json.dumps(counts, indent=2))


if __name__ == "__main__":
    main()

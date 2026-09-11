#!/usr/bin/env python3
"""Freeze locally audited 2 x 2 prompts. Never contacts a provider."""
import argparse
import html
import json
from collections import Counter
from pathlib import Path

from phase1_read_model_comparison import (
    MODELS, TOKENIZER_REPO, TOKENIZER_REVISION, VARIANTS, VERSION, Privacy,
    audit_records, canonical, dump, dump_rows, file_hash, fingerprint,
    import_packer, make_prompt, native_episode_order, request_plan, rows, select_cohort, target_text, timeline,
)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    args = ap.parse_args()
    config = json.loads(args.config.read_text())
    assert config["historyOrderingPolicy"] == "pipeline_native_no_shared_resort"
    out = args.output.resolve()
    if out.exists():
        raise ValueError("Use a fresh immutable output directory")
    new = Path(config["newCorpus"])
    frozen = Path(config["frozenProducer"])
    replay = json.loads(Path(config["oldReplayManifest"]).read_text())
    assert replay["status"] == "old_read_replay_complete_no_provider_calls"
    new_implementation = json.loads((new.parent / "implementation.json").read_text())
    for path, digest in replay["rawDigests"].items():
        assert new_implementation["sourceDigests"][path] == digest, "Old/new raw source mismatch"
    assert config["seed"] == 17
    paths = [args.config, new / "examples.jsonl", new / "events.jsonl", new / "gaps.jsonl",
             new / "dataset.json", new / "corpus.json", new.parent / "implementation.json", Path(config["oldReplayManifest"])]
    for source in config["oldCausalSources"]:
        paths += [Path(source) / "events.jsonl", Path(source) / "dataset.json"]
    paths += sorted((frozen / "scripts").glob("*.py"))
    paths += [Path(__file__), Path(__file__).with_name("phase1_read_model_comparison.py")]
    paths += [Path(p) for p in config.get("runtimeEvidenceFiles", [])]
    bindings = {str(p.resolve()): file_hash(p) for p in paths}
    for path, digest in replay["rawDigests"].items():
        assert file_hash(path) == digest, "Raw session changed after old replay"
    new_events = list(rows(new / "events.jsonl"))
    privacy = Privacy({r["sourceEventID"]: r for r in new_events}, config["privacyPolicy"])
    examples = []
    exclusions = []
    excluded_numbers = set(config["excludeOneBasedTargetOrdinals"])
    for ordinal, row in enumerate(rows(new / "examples.jsonl"), 1):
        reason = None
        if ordinal in excluded_numbers:
            reason = "known_incomplete_navigation_split_target_under_review"
        elif privacy.unsafe(row["query"] + canonical(row["target"])):
            reason = "sensitive_query_or_target"
        if reason:
            exclusions.append({"exampleID": row["exampleID"], "originalTargetNumber": ordinal, "reason": reason})
            continue
        # Drop multi-megabyte copies of existing contexts. Construct each anew.
        examples.append({k: row[k] for k in (
            "exampleID", "query", "target", "targetMask", "targetEventID", "targetBeganAt",
            "targetAvailableAt", "episode", "modelFacingDestination", "sessionID", "sourceSessionOrdinal",
            "targetSourceRecordIDs", "contextBlockIDs",
        )})
        examples[-1]["originalTargetNumber"] = ordinal
    cohort, quotas = select_cohort(examples, config["exampleCount"], config["seed"])
    for row in cohort:
        row["targetText"] = target_text(row["target"])
        row["targetSHA256"] = fingerprint(row["target"])
        row["querySHA256"] = fingerprint(row["query"])
    old_reads, old_native_events = [], []
    for index, source in enumerate(config["oldCausalSources"]):
        old_manifest = json.loads((Path(source) / "dataset.json").read_text())
        assert old_manifest["source"]["reducerVersion"] == config["pipelineArms"]["old"]["semantic"]
        assert old_manifest["source"]["digestsSHA256"]["raw.jsonl"] in replay["rawDigests"].values()
        for row in rows(Path(source) / "events.jsonl"):
            old_native_events.append(row)
            if row["kind"] == "read":
                row["sourceSessionOrdinal"] = index
                old_reads.append(row)
    common_writes = [r for r in new_events if r["kind"] == "write"]
    new_reads = [r for r in new_events if r["kind"] == "read"]
    assert {e["sessionID"] for e in old_reads} == {e["sessionID"] for e in new_reads}, "Unmatched session coverage"
    gaps = list(rows(new / "gaps.jsonl"))
    maps = {}
    orders = {}
    stream_stats = {}
    for variant, reads in (("old", old_reads), ("new", new_reads)):
        filtered = privacy.filter_stream(reads + common_writes)
        maps[variant], orders[variant] = timeline(filtered, gaps)
        if variant == "old":
            orders[variant] = native_episode_order(old_native_events, reads + common_writes, gaps)
            assert set(orders[variant]) == set(maps[variant]), "Old native projection lost a READ or closed WRITE"
        else:
            # Per-example contextBlockIDs are authoritative for the new arm.
            # Save the event index in its original artifact order, not sorted.
            orders[variant] = [e["sourceEventID"] for e in new_events] + [g["contextBlockID"] for g in gaps]
        stream_stats[variant] = {"reads": len(reads), "commonClosedWrites": len(common_writes),
                                 "redactedEvents": sum(e.get("privacyRedacted", False) for e in filtered),
                                 "privacyDependencyFallbacks": sum(e.get("privacyDependencyFallback", False) for e in filtered)}
    writes = lambda mapping: {k: e["serialized"] for k, e in mapping.items() if e["kind"] == "write"}
    assert writes(maps["old"]) == writes(maps["new"]), "WRITE history changed between data arms"

    packer = import_packer(frozen)
    from transformers import AutoTokenizer
    snapshot, resolved_revision = packer.resolve_tokenizer_snapshot(TOKENIZER_REPO, TOKENIZER_REVISION, True)
    assert resolved_revision == TOKENIZER_REVISION
    tokenizer = AutoTokenizer.from_pretrained(snapshot, local_files_only=True, use_fast=True, split_special_tokens=True)
    caches = {v: {} for v in VARIANTS}
    prompts = []
    for index, example in enumerate(cohort, 1):
        for variant in VARIANTS:
            p = make_prompt(example, variant, maps[variant], orders[variant], packer,
                            tokenizer, caches[variant], config["contextTokenBudget"])
            assert not privacy.unsafe(p["modelInput"]), "Sensitive content reached actual prompt"
            prompts.append(p)
        if index % 10 == 0:
            print(f"Packed {index}/{len(cohort)} matched examples", flush=True)
    requests = request_plan(prompts, cohort, config["seed"])
    audit = audit_records(cohort, prompts, requests, privacy, config["contextTokenBudget"])
    assert bindings == {p: file_hash(p) for p in bindings}, "Input or producer changed while preparing"
    out.mkdir(parents=True)
    dump_rows(out / "cohort.jsonl", cohort)
    dump_rows(out / "prompts.jsonl", prompts)
    dump_rows(out / "requests.planned.jsonl", requests)
    dump_rows(out / "excluded-examples.jsonl", exclusions)
    for variant in VARIANTS:
        dump_rows(out / f"{variant}-context-events.jsonl", (maps[variant][key] for key in orders[variant]))
    dump_rows(out / "old-native-event-order.jsonl", ({k: e[k] for k in ("sourceEventID", "kind", "sessionID", "availableAt")} for e in old_native_events))
    prompt_by_key = {(p["exampleID"], p["variant"]): p for p in prompts}
    changed = sum(prompt_by_key[e["exampleID"], "old"]["modelInput"] != prompt_by_key[e["exampleID"], "new"]["modelInput"] for e in cohort)
    audit.update({"identicalUnpackedWriteHistory": True, "changedPromptPairs": changed,
                  "excludedCandidates": dict(Counter(e["reason"] for e in exclusions)),
                  "tokenizerRevision": resolved_revision, "modelCallsExecuted": 0})
    audit["newNativeContextOrderPreservedExactly"] = all(
        p["originalContextOrderPreserved"] for p in prompts if p["variant"] == "new")
    audit["sharedTimestampResortApplied"] = False
    dump(out / "audit.json", audit)
    model_tokens = {v: sum(p["referenceInputTokens"] for p in prompts if p["variant"] == v) for v in VARIANTS}
    plan = {
        "version": VERSION, "purpose": "paired READ-pipeline x frozen-frontier-model diagnostic",
        "status": "prepared_locally_awaiting_review_not_executed", "training": False,
        "exampleCount": len(cohort), "plannedPredictions": len(requests), "models": list(MODELS),
        "reasoningEffort": "xhigh", "exampleCountsByApplication": quotas,
        "exampleCountsBySession": dict(Counter(e["sessionID"] for e in cohort)),
        "groundedPasteActions": sum(s["type"] == "paste" for e in cohort for s in e["target"]["segments"]),
        "pipelineArms": config["pipelineArms"], "selectionSeed": config["seed"],
        "filteredStreamCounts": stream_stats,
        "sourceHashes": bindings, "rawDigests": replay["rawDigests"],
        "contextBudget": {"tokens": config["contextTokenBudget"], "referenceTokenizer": TOKENIZER_REPO,
                          "revision": resolved_revision, "policy": "same event-aware suffix budget; no per-provider repacking",
                          "nativeProviderPreambleAdditional": True, "totalReferenceInputTokensPerModel": model_tokens,
                          "newDependencyRenderer": "dependency-aware-read-novelty-v3",
                          "instruction": packer.DEFAULT_TASK_INSTRUCTION},
        "historyOrdering": "No shared re-sort or availability rewrite. New arm uses exact frozen per-example contextBlockIDs. Old arm preserves its causal event order through the frozen closed-episode first-occurrence projection. Both apply the same prediction-onset availability cutoff without moving events.",
        "privacyPolicy": config["privacyPolicy"],
        "privacyScope": "Known credentials/OTP lineage, propagated payloads, high-specificity secret patterns. Not anonymized; personal work remains.",
        "providerContract": {"endpoint": "http://127.0.0.1:4000/v1/responses", "authentication": "existing ChatGPT/Codex OAuth via LiteLLM",
                             "apiKeyFallback": False, "modelFallback": False, "tools": [], "stream": True,
                             "nativePreamble": "existing Codex provider preamble held identical; fingerprinted runtime files",
                             "automaticRetries": 0, "concurrency": 1,
                             "quotaExhaustion": "pause; never switch provider", "temperature": "omitted",
                             "generationTokenCap": "not supplied: existing subscription Responses adapter forbids max-output parameters",
                             "reasoningAndOutputTokensMustBeRetained": True,
                             "requestRecovery": "durable in-flight journal required before execution; uncertain request must not silently replay"},
        "measurementContract": {"primary": "paired blinded holistic prediction quality, judged after outputs exist",
                                "secondary": ["exact match", "correct prefix", "macro/micro character similarity", "paste precision/recall"],
                                "latency": "monotonic dispatch-to-completion; retain TTFT if exposed; retries separate",
                                "usage": "raw provider input/cached/reasoning/output counts per request",
                                "cost": "API-equivalent estimate using actual usage; not subscription billing",
                                "pricesUSDPerMillion": config["apiEquivalentPricesUSDPerMillion"],
                                "priceSource": "https://developers.openai.com/api/docs/models/compare",
                                "priceChecked": "2026-09-07", "excludeWarmupBlock": False,
                                "invalidOrEmptyOutputs": "retain and score; do not silently filter"},
        "limitations": [
            "Old arm replays pane-v2/semantic-v14 on the same full sessions; it is not an exact recreation of the older training experiment.",
            "Targets/query/closed-WRITE history are held fixed at episode-v9; five known incomplete targets excluded, not repaired here.",
            "Different retained history reach under the same budget is part of the READ-pipeline effect, not a common-event-suffix ablation.",
            "New semantic-v25 has known pending reviewer fixes; this is a frozen diagnostic snapshot, not final pipeline approval.",
            "Astra synthetic subscription access is verified. Remaining quota and full-context reliability are not verified.",
            "Model aliases may change remotely; preserve returned model/revision when available, otherwise mark exact revision unverified.",
        ],
        "executionGates": ["review this frozen privacy-filtered cohort", "separate two-model subscription executor with journaling and exact-model checks",
                           "identical frozen native preamble for both models", "no personal request in this preparation"],
    }
    dump(out / "plan.json", plan)
    # Local-only two-panel inspection. Escape all collected content as text.
    parts = ["<!doctype html><meta charset=utf-8><title>Frozen paired READ comparison</title>",
             "<style>body{font:16px system-ui;margin:24px;max-width:1800px}section{border-top:2px solid #aaa;padding:20px 0}.pair{display:grid;grid-template-columns:1fr 1fr;gap:20px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f5f5;padding:12px;max-height:650px;overflow:auto}summary{cursor:pointer}</style>",
             "<h1>Same next WRITE; old versus new READ history</h1><p>100 paired targets, four planned model arms. Private local review; no predictions have been requested.</p>"]
    for index, e in enumerate(cohort, 1):
        parts.append(f"<section><h2>{index} · source target {e['originalTargetNumber']} · {html.escape(e['modelFacingDestination']['application'])}</h2><p>{html.escape(e['targetBeganAt'])}</p><h3>Expected completed WRITE (identical in all arms)</h3><pre>{html.escape(e['targetText'])}</pre><div class=pair>")
        for variant in VARIANTS:
            p = prompt_by_key[e['exampleID'], variant]
            readable = [json.dumps(json.loads(b['serialized']), indent=2, ensure_ascii=False) for b in p['retainedBlocks']]
            parts.append(f"<div><h3>{variant.title()} READ pipeline · {p['referenceInputTokens']:,} reference tokens</h3><details><summary>Show actual retained history and query</summary><pre>{html.escape(chr(10).join(readable) + chr(10) + json.dumps(json.loads(p['query']), indent=2, ensure_ascii=False))}</pre></details><details><summary>Exact model input</summary><pre>{html.escape(p['modelInput'])}</pre></details></div>")
        parts.append("</div></section>")
    (out / "review.html").write_text("\n".join(parts))
    dump(out / "artifact-hashes.json", {p.name: file_hash(p) for p in sorted(out.iterdir()) if p.is_file()})
    print(json.dumps({"output": str(out), "audit": audit, "applications": quotas}, indent=2))


if __name__ == "__main__":
    main()

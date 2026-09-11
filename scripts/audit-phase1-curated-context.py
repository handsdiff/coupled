#!/usr/bin/env python3
"""Recount known READ defects in actual 32K inputs of a curated cohort.

Streams examples; retains only the exact packable suffix and bounded token cache.
This measures exposure to reviewed defects, not a population cleanliness score.
"""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import resource
import sys

from importlib.machinery import SourceFileLoader
h = SourceFileLoader("curated_context_helpers", str(Path(__file__).with_name("curate-phase1-episode-joins.py"))).load_module()
audit = h.load_module("audit-phase1-post-pane-context")
scan = h.load_module("pack-phase1-fidelity-audit")
ROOT = h.ROOT


def hits_in_packed(packed, source, witnesses):
    hits = []
    for span in packed["contextEventSpans"]:
        eid = span["eventID"]
        if eid not in witnesses:
            continue
        content = json.loads(span.get("packedSerialized", source[eid]["serialized"])).get("content", "")
        for w in witnesses[eid]:
            phrases = [p for p in w["phrases"] if scan.contains_phrase(p, content)]
            if phrases:
                hits.append({**w, "phrases": phrases})
    return hits


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--write-issues", type=Path, help="Reviewed outstanding original case numbers; measures direct and historical exposure")
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    paths = [ROOT / "coupled-data/context-fidelity-audit-20260911-r2/packed-audit-classified/verified-read-witnesses.json",
             ROOT / "coupled-data/post-pane-context-audit-20260911-final/followup-witnesses.json"]
    witnesses = defaultdict(list)
    for path in paths:
        for w in json.loads(path.read_text()):
            witnesses[w["eventID"]].append(w)
    source = {r["sourceEventID"]: r for r in h.rows(a.input / "new/events.jsonl")}
    for b in h.rows(a.input / "new/context-blocks.jsonl"):
        source.setdefault(b["contextBlockID"], b)
    settings = json.loads((audit.PREP / "paired-qwen38-reference32k/packing-audit.json").read_text())
    tokenizer = audit.AutoTokenizer.from_pretrained(settings["contextBudget"]["referenceTokenizerDirectory"],
        local_files_only=True, trust_remote_code=False, split_special_tokens=True)
    packer = h.load_module("pack-phase1-dataset")
    original, original_targets = {}, {}
    for i, r in enumerate(h.rows(audit.PREP / "paired-qwen38-reference32k/cohort.jsonl"), 1):
        original[r["exampleID"]] = i
        original_targets[i] = r["targetEventID"]
    write_issues = json.loads(a.write_issues.read_text()) if a.write_issues else []
    flagged = {original_targets[r["originalCase"]]: r for r in write_issues}
    h.require(all(eid in source for eid in flagged), "Issue registry refers to a retired WRITE; re-review its disposition")
    cache, cases, stages, affected_events = {}, [], defaultdict(set), defaultdict(set)
    for i, r in enumerate(h.rows(a.input / "new/examples.jsonl"), 1):
        ids = audit.budget_suffix(r["contextBlockIDs"], source, r["query"], settings["taskInstruction"], tokenizer, packer, cache)
        assert all((source[eid].get("availableAt") or source[eid]["beforeAt"]) < r["targetBeganAt"] for eid in ids)
        context = "\n".join(source[eid]["serialized"] for eid in ids)
        bounded = {"exampleID": r["exampleID"], "query": r["query"], "contextBlockIDs": ids,
                   "context": context, "modelInput": (context + "\n" if context else "") + r["query"]}
        packed = packer.pack_model_input(bounded, source, tokenizer, 32768, settings["taskInstruction"], cache, True)
        hits = hits_in_packed(packed, source, witnesses)
        historical_hits = []
        for span in packed["contextEventSpans"]:
            eid = span["eventID"]
            if eid not in flagged:
                continue
            payload = json.loads(span.get("packedSerialized", source[eid]["serialized"]))
            if payload.get("content") or any(s.get("content") for s in payload.get("authorshipSegments", [])):
                historical_hits.append({"eventID": eid, **flagged[eid]})
        for w in hits:
            stages[w["stage"]].add(r["exampleID"])
            affected_events[w["eventID"]].add(r["exampleID"])
        cases.append({"ordinal": i, "originalCase": original.get(r["exampleID"]), "exampleID": r["exampleID"],
            "targetBeganAt": r["targetBeganAt"], "inputTokens": len(packed["inputIDs"]), "defects": hits,
            **({"directWriteIssue": flagged.get(r["targetEventID"]), "historicalWriteIssues": historical_hits} if a.write_issues else {})})
        cache = {key: cache[key] for key in ids}
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024**2 if sys.platform == "darwin" else 1024)
        if rss > 3000:
            raise MemoryError("Curated context audit exceeded 3000 MiB")
        if i % 100 == 0:
            print(f"Audited {i} contexts; peak {rss:.0f} MiB", flush=True)
    affected = [r for r in cases if r["defects"]]
    summary = {"version": "phase1-curated-context-audit-v1", "examples": len(cases),
        "affectedContexts": len(affected), "percent": 100 * len(affected) / len(cases),
        "byStage": {k: {"count": len(v), "exampleIDs": sorted(v)} for k, v in stages.items()},
        "byReadEvent": {k: {"count": len(v), "exampleIDs": sorted(v)} for k, v in affected_events.items()},
        "scope": "Known screenshot-verified READ defects surviving actual dependency-aware reference-tokenizer 32K packing; lower bound, not exhaustive error rate",
        "cohortChanged": True, "providerCalls": 0, "peakRSSMiB": rss,
        "sourceHashes": {str(path.resolve()): h.sha(path) for path in paths + [a.input / "review.json", a.input / "new/events.jsonl", a.input / "new/examples.jsonl", Path(__file__), ROOT / "scripts/pack-phase1-dataset.py"]}}
    if a.write_issues:
        direct = {r["exampleID"] for r in cases if r["directWriteIssue"] or r["defects"]}
        historical = {r["exampleID"] for r in cases if r["historicalWriteIssues"]}
        summary.update(directTargetIssues=sum(bool(r["directWriteIssue"]) for r in cases),
                       directTargetOrReadIssues=len(direct), directTargetOrReadPercent=100*len(direct)/len(cases),
                       historicalWriteExposure=len(historical), additionalHistoricalOnly=len(historical-direct),
                       anyKnownIssueExposure=len(direct|historical), anyKnownIssuePercent=100*len(direct|historical)/len(cases),
                       combinedScope="Known reviewed target/query problems plus known READ defects and actual packed historical-WRITE exposure; not an exhaustive error or model-failure rate")
        summary["sourceHashes"][str(a.write_issues.resolve())] = h.sha(a.write_issues)
    h.save(a.output / "summary.json", summary)
    h.save(a.output / "cases.json", cases)
    print(json.dumps({k: v for k, v in summary.items() if k in ("examples", "affectedContexts", "percent", "scope", "peakRSSMiB")}, indent=2))


if __name__ == "__main__":
    main()

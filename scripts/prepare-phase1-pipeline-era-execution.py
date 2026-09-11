#!/usr/bin/env python3
"""Bind independent packs and local tests into a NON-authorized execution plan."""
import json
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
ERA = ROOT / "coupled-data/sep02-10-pipeline-era-comparison-20260911"
PREP = ROOT / "coupled-data/sep02-10-training-prep-20260910"
from phase1_qwen38_execution import BPB_DEFINITION, file_hash, fingerprint, verify_execution_binding


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--packs', type=Path, default=ERA/'packs-v2')
    ap.add_argument('--checks', type=Path, default=ERA/'execution-checks.json')
    ap.add_argument('--output', type=Path, default=ERA/'execution-plan.json')
    args = ap.parse_args()
    contract = json.loads((PREP / "training-contract.json").read_text())
    checks = json.loads(args.checks.read_text())
    assert checks["status"] == "passed" and checks["networkBlocked"] and checks["providerCalls"] == 0
    assert checks.get("bpbDefinition") == BPB_DEFINITION, "Refresh execution checks for current BPB definition"
    paths = {Path(p): h for p, h in checks["testedFilesSHA256"].items()}
    paths[args.checks.resolve()] = file_hash(args.checks)
    paths[Path(__file__)] = file_hash(__file__)
    # These are the documented Sep-10 planning rates, not a live price quote.
    prices = {"train": 4.103, "prefill": 1.86, "sample": 5.595}
    arms = {}; totals = {"train": 0., "generationPrefill": 0., "generationMaximumOutput": 0., "nll": 0.}
    for arm in ("old", "new"):
        pack = args.packs.resolve() / arm
        report = json.loads((pack / "packing-audit.json").read_text())
        audit = json.loads((pack / "independent-audit.json").read_text())
        assert audit["status"] == "passed" and audit["executionAdapterCheckedEveryRow"]
        assert len(audit["deterministicRepeatedArtifacts"]) == len(report["artifactsSHA256"])
        for p, h in report["sourceFilesSHA256"].items():
            assert file_hash(p) == h, p
            paths[Path(p)] = h
        for name, h in report["tokenizerFilesSHA256"].items():
            path = PREP / "tokenizer" / name
            assert file_hash(path) == h, path
            paths[path] = h
        for p, h in report["referenceTokenizerFilesSHA256"].items():
            assert file_hash(p) == h, p
            paths[Path(p)] = h
        paths[pack / "independent-audit.json"] = file_hash(pack / "independent-audit.json")
        t, c = report["tokens"], report["counts"]
        costs = {"train": t["trainingPositions"] * prices["train"] / 1e6,
                 "generationPrefill": 2 * t["generationPrefillOneModel"] * prices["prefill"] / 1e6,
                 "generationMaximumOutput": 2 * c["scoredExamples"] * contract["generation"]["maximumTokens"] * prices["sample"] / 1e6,
                 "nll": ((2*t["NLLSequenceTokensOneModel"] + t["warmupFrozenNLLSequenceTokens"]) * prices["prefill"]
                         + (2*c["scoredExamples"] + min(50,c["examples"])) * prices["sample"]) / 1e6}
        for k,v in costs.items(): totals[k] += v
        arms[arm] = {"packDirectory": str(pack), "counts": c, "tokens": t, "maximumTokenCostUSD": costs,
                     "nativeRowsSHA256": file_hash(pack / "native-rows.jsonl"),
                     "blocksSHA256": file_hash(pack / "blocks.json"), "taskInstruction": report["taskInstruction"]}
    assert arms["old"]["taskInstruction"] == arms["new"]["taskInstruction"], "Review unequal task instructions"
    total = sum(totals.values())
    plan = {"version": "phase1-pipeline-era-execution-v1", "status": "offline_prepared_NOT_AUTHORIZED",
            "purpose": "Whole historical pipeline versus maximum-effort cleaned construction, independent supervision",
            "contract": contract, "arms": arms, "independentBlockSchedules": True,
            "sameTargetsRequired": False, "trainOnFinalBlock": False,
            "pricesPerMillionTokens": prices, "priceReferenceDate": "2026-09-10",
            "costAssumptions": ["uncached prefill", "every generation reaches 512 tokens", "NLL includes SDK one-token sample",
                                "no retries or live preflight included", "reconfirm model availability and pricing before paid approval"],
            "maximumTokenCostBreakdownUSD": totals, "conservativeTokenCostUSD": total,
            "suggestedReserveUSD": total * .15, "suggestedCeilingRoundedUSD": int((total * 1.15 + 9.999) // 10) * 10,
            "evaluation": {"primary": "holistic intended-thought usefulness", "firstBlockGenerationExcluded": True,
                           "NLL": "frozen versus personalized within each arm; not absolute paired old/new target NLL",
                           "BPB": BPB_DEFINITION,
                           "constructionErrorsScoredSeparately": True, "matchedSubsetSecondaryOnly": True},
            "sourceFilesSHA256": {str(p):h for p,h in sorted(paths.items())},
            "providerCalls": 0, "trainingLaunched": False,
            "remainingGates": ["review actual old/new targets and differing supervision",
                               "clean immutable implementation revision",
                               "separately authorized bounded live preflight on these exact packs",
                               "private project and data-transmission approval for this plan",
                               "explicit full-run budget and GO"]}
    verify_execution_binding(plan)
    with args.output.open("x") as f:
        json.dump(plan, f, indent=2, sort_keys=True); f.write("\n")
    print(json.dumps({"planSHA256":fingerprint(plan), "counts":{a:r["counts"] for a,r in arms.items()},
                      "projectedMaximumTokenCostUSD":total, "providerCalls":0}, indent=2))


if __name__ == "__main__": main()

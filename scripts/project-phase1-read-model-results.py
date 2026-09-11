#!/usr/bin/env python3
"""Recover exact predictions from saved wire evidence without any provider calls.

The frozen executor's v1 result files remain immutable transport/audit records.
This separately versioned projection is the model-facing evaluation output.
"""
import argparse
import hashlib
import json
from pathlib import Path
import os
from phase1_subscription_output import VERSION, interpret


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execution", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    assert not args.output.exists(), "Use a fresh projection directory"
    plan = args.execution / "executor-plan.json"
    plan_hash = digest(plan)
    rows, hashes = [], {}
    for file in sorted((args.execution / "results").glob("[0-9]*/result.json")):
        base = json.loads(file.read_text())
        attempt = file.parent
        marker = json.loads((attempt / "inflight.json").read_text())
        assert marker["binding"]["executorPlanSHA256"] == plan_hash
        for key, val in marker["binding"]["request"].items():
            if key != "status":
                assert base[key] == val
        timing = json.loads((attempt / "transport.json").read_text())
        assert timing == base["timing"]
        assert digest(attempt / "response.body") == timing["bodySHA256"]
        result = {**base, **interpret((attempt / "response.body").read_bytes(), base["model"])}
        result["originalResultSHA256"] = digest(file)
        rows.append(result)
        for name in ("result.json", "response.body", "transport.json", "inflight.json"):
            path = attempt / name
            hashes[str(path.resolve())] = digest(path)
    assert [r["requestOrdinal"] for r in rows] == list(range(len(rows)))
    args.output.mkdir(parents=True, mode=0o700)
    output = args.output / "predictions.jsonl"
    output.write_text("".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in rows))
    os.chmod(output, 0o600)
    report = {"version": VERSION, "executorPlanSHA256": plan_hash, "recorded": len(rows),
        "sourceSHA256": hashes, "predictionsSHA256": digest(output),
        "implementationSHA256": {p.name: digest(p) for p in [Path(__file__), Path(__file__).with_name("phase1_subscription_output.py")]},
        "wireRecoveryCount": sum(r["responseInterpretation"]["source"] == "sse_output_item_done" for r in rows),
        "emptyPredictions": sum(not r["prediction"] for r in rows),
        "invalidPredictions": sum(not r["validCompletion"] for r in rows), "providerCallsDuringProjection": 0}
    (args.output / "projection.json").write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    print(json.dumps({k: report[k] for k in ("version", "recorded", "wireRecoveryCount", "emptyPredictions", "invalidPredictions", "providerCallsDuringProjection")}))


if __name__ == "__main__":
    main()

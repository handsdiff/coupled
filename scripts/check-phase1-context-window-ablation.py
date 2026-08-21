#!/usr/bin/env python3
"""No-network checks for the GPT-5.6 Sol context-window contract."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from phase1_context_window_ablation import (
    MODEL,
    RUNNER_VERSION,
    WINDOWS,
    api_equivalent_cost,
    load_jsonl,
    sha256,
)
from phase1_experiment import canonical_bytes
from phase1_prediction_metrics import score_prediction


def main() -> int:
    assert list(WINDOWS.items()) == [
        ("8k", 8192), ("16k", 16384), ("32k", 32768), ("64k", 65536)
    ]
    assert MODEL == {
        "route": "chatgpt/gpt-5.6-sol",
        "requestedModel": "gpt-5.6-sol",
        "reasoningEffort": "xhigh",
    }
    cost = api_equivalent_cost([
        {"usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000}},
    ])
    assert cost["totalUSD"] == "35.000000"
    assert cost["billingMode"] == "api_equivalent_not_subscription_charge"
    project = Path(__file__).resolve().parent.parent
    root = project / "coupled-data/phase1-gpt56-context-packs-v1-r2-20260820"
    reference = (
        project / "coupled-data/phase1-raw-episode-pack-v6-v10-canonical-20260820"
    )
    if root.is_dir() and reference.is_dir():
        rows = {
            key: load_jsonl(root / key / "semantic-examples.jsonl")
            for key in WINDOWS
        }
        reference_plans = {
            value["exampleID"]: value
            for value in load_jsonl(reference / "context-plans.jsonl")
        }
        ids = [value["exampleID"] for value in rows["32k"]]
        assert len(ids) == len(set(ids)) == 224
        assert all([value["exampleID"] for value in rows[key]] == ids for key in WINDOWS)
        for ordinal, example_id in enumerate(ids):
            previous = []
            target = rows["8k"][ordinal]["target"]
            query_hash = rows["8k"][ordinal]["rightEdgeQuerySHA256"]
            instruction = rows["8k"][ordinal]["taskInstruction"]
            for key, budget in WINDOWS.items():
                row = rows[key][ordinal]
                retained = [
                    value["contextBlockID"] for value in row["retainedContextBlocks"]
                ]
                assert row["canonicalPackingTokenCount"] <= budget
                assert row["target"] == target
                assert row["rightEdgeQuerySHA256"] == query_hash
                assert row["taskInstruction"] == instruction
                assert not previous or retained[-len(previous) :] == previous
                previous = retained
            reference_plan = reference_plans[example_id]
            assert (
                rows["32k"][ordinal]["semanticModelInputSHA256"]
                == reference_plan["semanticModelInputSHA256"]
            )
            assert (
                rows["32k"][ordinal]["retainedContextBlocks"]
                == reference_plan["retainedContextBlocks"]
            )
        corpus = (
            project / "coupled-data/phase1-raw-episode-corpus-v6-v10-review-20260820"
        )
        comparator = (
            project / "coupled-data/phase1-raw-episode-frontier-v7-v6-v10-20260820"
        )
        with tempfile.TemporaryDirectory(prefix="phase1-context-window-check-") as raw:
            temporary = Path(raw)
            plan_path = temporary / "plan.json"
            prepared = subprocess.run(
                [
                    sys.executable,
                    str(project / "scripts/prepare-phase1-context-window-ablation.py"),
                    "--corpus", str(corpus),
                    "--context-packs", str(root),
                    "--reference-32k-pack", str(reference),
                    "--existing-32k-output", str(comparator),
                    "--output", str(plan_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if prepared.returncode != 0:
                raise AssertionError(f"context plan fixture failed: {prepared.stderr}")
            plan = json.loads(plan_path.read_text())
            expected_ids = plan["source"]["prospectiveExampleIDs"]
            runs = temporary / "runs"
            for key in ("8k", "16k", "64k"):
                directory = runs / key
                directory.mkdir(parents=True)
                by_id = {value["exampleID"]: value for value in rows[key]}
                scores = []
                for ordinal, example_id in enumerate(expected_ids, 1):
                    row = by_id[example_id]
                    target = row["target"]
                    scores.append({
                        "exampleID": example_id,
                        "responseModel": MODEL["requestedModel"],
                        "requestedReasoningEffort": MODEL["reasoningEffort"],
                        "target": target,
                        "prediction": target,
                        "pasteActionCount": row["pasteActionCount"],
                        "predictionMetrics": score_prediction(
                            target,
                            target,
                            target_paste_actions=row["pasteActionCount"],
                        ),
                        "latencySeconds": 1.0,
                        "usage": {
                            "input_tokens": row["canonicalPackingTokenCount"],
                            "output_tokens": 1,
                            "output_tokens_details": {"reasoning_tokens": 0},
                        },
                    })
                scores_path = directory / "scores.jsonl"
                scores_path.write_bytes(
                    b"".join(canonical_bytes(value) for value in scores)
                )
                manifest = {
                    "status": "complete",
                    "runnerVersion": RUNNER_VERSION,
                    "windowKey": key,
                    "inputTokenBudget": WINDOWS[key],
                    "model": MODEL,
                    "source": {
                        "planSHA256": sha256(plan_path),
                        "packingSHA256": plan["source"]["packs"][key]["packingSHA256"],
                    },
                    "artifactDigestsSHA256": {
                        "scores.jsonl": sha256(scores_path)
                    },
                }
                (directory / "window.json").write_bytes(canonical_bytes(manifest))
            audit_path = temporary / "audit"
            audited = subprocess.run(
                [
                    sys.executable,
                    str(project / "scripts/audit-phase1-context-window-ablation.py"),
                    "--corpus", str(corpus),
                    "--plan", str(plan_path),
                    "--runs", str(runs),
                    "--output", str(audit_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if audited.returncode != 0:
                raise AssertionError(f"context synthetic audit failed: {audited.stderr}")
            report = json.loads((audit_path / "context-windows.json").read_text())
            assert report["status"] == "passed"
            assert set(report["summaries"]) == set(WINDOWS)
            assert all(value["examples"] == 174 for value in report["summaries"].values())
    print("phase1 context-window checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

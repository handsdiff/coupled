#!/usr/bin/env python3
"""Freeze the no-network GPT-5.6 Sol context-window ablation plan."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from phase1_context_window_ablation import (
    ABLATION_VERSION,
    MODEL,
    PLAN_VERSION,
    WINDOWS,
    ContextWindowError,
    sha256,
)
from phase1_inkling import load_experiment_blocks, load_jsonl


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--context-packs", required=True, type=Path)
    parser.add_argument("--reference-32k-pack", required=True, type=Path)
    parser.add_argument("--existing-32k-output", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    output = arguments.output.expanduser().resolve()
    if output.exists():
        raise ContextWindowError(f"output already exists: {output}")
    corpus_path = arguments.corpus.expanduser().resolve()
    context_packs = arguments.context_packs.expanduser().resolve()
    reference_pack = arguments.reference_32k_pack.expanduser().resolve()
    packs = {key: context_packs / key for key in WINDOWS}
    corpus = json.loads((corpus_path / "corpus.json").read_text(encoding="utf-8"))
    blocks = load_experiment_blocks(corpus_path)
    expected_ids = [value for block in blocks[1:] for value in block["exampleIDs"]]
    if len(expected_ids) != 174:
        raise ContextWindowError("context ablation expects 174 prospective examples")
    pack_records: dict[str, dict] = {}
    corpus_order = [value for block in blocks for value in block["exampleIDs"]]
    for key, budget in WINDOWS.items():
        pack = packs[key]
        packing = json.loads((pack / "packing.json").read_text(encoding="utf-8"))
        rows = load_jsonl(pack / "semantic-examples.jsonl")
        row_by_id = {value["exampleID"]: value for value in rows}
        if not (
            packing.get("inputTokenBudget") == budget
            and [value["exampleID"] for value in rows] == corpus_order
            and packing.get("source", {}).get("corpusSHA256")
            == sha256(corpus_path / "corpus.json")
        ):
            raise ContextWindowError(f"{key} packing contract differs")
        pack_records[key] = {
            "directory": str(pack),
            "inputTokenBudget": budget,
            "packingSHA256": sha256(pack / "packing.json"),
            "semanticExamplesSHA256": sha256(pack / "semantic-examples.jsonl"),
            "prospectiveSemanticModelInputSHA256s": [
                row_by_id[value]["semanticModelInputSHA256"] for value in expected_ids
            ],
        }
    comparator = arguments.existing_32k_output.expanduser().resolve()
    manifest_path = comparator / "frontier.json"
    scores_path = comparator / "scores.jsonl"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not (
        manifest.get("status") == "complete"
        and manifest.get("source", {}).get("corpusSHA256")
        == sha256(corpus_path / "corpus.json")
        and manifest.get("source", {}).get("packingSHA256")
        == sha256(reference_pack / "packing.json")
        and manifest.get("artifactDigestsSHA256", {}).get("scores.jsonl")
        == sha256(scores_path)
    ):
        raise ContextWindowError("existing 32K comparator lineage differs")
    project = Path(__file__).resolve().parent.parent
    implementation = [
        project / "scripts/phase1_context_window_ablation.py",
        project / "scripts/prepare-phase1-context-window-packs.py",
        project / "scripts/phase1_subscription_responses.py",
        project / "scripts/prepare-phase1-context-window-ablation.py",
        project / "scripts/run-phase1-context-window-ablation.py",
        project / "scripts/run-phase1-context-window-sequence.py",
        project / "scripts/audit-phase1-context-window-ablation.py",
        project / "scripts/check-phase1-context-window-ablation.py",
    ]
    plan = {
        "schemaVersion": 1,
        "planVersion": PLAN_VERSION,
        "ablationVersion": ABLATION_VERSION,
        "status": "local_plan_only_no_authentication_or_data_transfer",
        "source": {
            "corpusID": corpus["corpusID"],
            "corpusSHA256": sha256(corpus_path / "corpus.json"),
            "examplesSHA256": sha256(corpus_path / "examples.jsonl"),
            "prospectiveExampleIDs": expected_ids,
            "packs": pack_records,
            "reference32KPackingSHA256": sha256(reference_pack / "packing.json"),
            "reference32KContextPlansSHA256": sha256(reference_pack / "context-plans.jsonl"),
        },
        "model": MODEL,
        "protocol": {
            "windows": WINDOWS,
            "packingTokenizer": "the same frozen Qwen tokenizer and event-aware suffix algorithm used by the 32K comparator",
            "instructionQueryAndTargetIdenticalAcrossWindows": True,
            "onlyRetainedHistoryBudgetChanges": True,
            "examplesPerWindow": len(expected_ids),
            "newSubscriptionCalls": len(expected_ids) * 3,
            "warmupBlockScored": False,
            "trainingOperations": 0,
        },
        "transport": {
            "type": "loopback_litellm_chatgpt_subscription_responses",
            "endpoint": "http://127.0.0.1:4000/v1/responses",
            "openAIAPIKeyFallbackAllowed": False,
            "tokenLimitFieldsSent": False,
        },
        "existing32KComparator": {
            "directory": str(comparator),
            "manifestSHA256": sha256(manifest_path),
            "scoresSHA256": sha256(scores_path),
        },
        "implementation": {
            "fileDigestsSHA256": {
                str(path.relative_to(project)): sha256(path) for path in implementation
            }
        },
        "authorizationBoundary": {
            "thisCommandContactsProviders": False,
            "thisCommandTransmitsPersonalData": False,
            "executionRequiresPersonalDataAndSubscriptionConfirmations": True,
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print(f"Wrote context-window plan to {output}")
    print(f"Frozen new subscription calls: {plan['protocol']['newSubscriptionCalls']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

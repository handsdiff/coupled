#!/usr/bin/env python3
"""Freeze a no-network plan for the Phase 1 frontier-model arc."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from phase1_experiment import prospective_example_ids, validate_inputs
from phase1_frontier_model_arc import ARC_VERSION, MODEL_SPECS, PLAN_VERSION, sha256
from phase1_training_contract import TrainingContractError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--packed", required=True, type=Path)
    parser.add_argument(
        "--existing-gpt-5.6-output",
        dest="existing_gpt_5_6_output",
        required=True,
        type=Path,
    )
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    corpus_path = arguments.corpus.expanduser().resolve()
    packed_path = arguments.packed.expanduser().resolve()
    existing_path = arguments.existing_gpt_5_6_output.expanduser().resolve()
    output = arguments.output.expanduser().resolve()
    if output.exists():
        raise TrainingContractError(f"output already exists: {output}")
    corpus, examples, _, plans = validate_inputs(corpus_path, packed_path)
    evaluation_ids = prospective_example_ids(corpus["blocking"]["blocks"])
    if len(evaluation_ids) != 174:
        raise TrainingContractError("frontier arc expects the frozen 174-example scope")
    if [value["exampleID"] for value in examples if value["exampleID"] in set(evaluation_ids)] != evaluation_ids:
        raise TrainingContractError("prospective example order differs")
    existing_manifest = existing_path / "frontier.json"
    existing_scores = existing_path / "scores.jsonl"
    if not (existing_manifest.is_file() and existing_scores.is_file()):
        raise TrainingContractError("existing GPT-5.6 comparator is incomplete")
    existing = json.loads(existing_manifest.read_text(encoding="utf-8"))
    if not (
        existing.get("status") == "complete"
        and existing.get("source", {}).get("corpusSHA256") == sha256(corpus_path / "corpus.json")
        and existing.get("source", {}).get("packingSHA256") == sha256(packed_path / "packing.json")
        and existing.get("artifactDigestsSHA256", {}).get("scores.jsonl") == sha256(existing_scores)
    ):
        raise TrainingContractError("existing GPT-5.6 comparator lineage differs")
    project = Path(__file__).resolve().parent.parent
    implementation = [
        project / "scripts/phase1_frontier_model_arc.py",
        project / "scripts/phase1_subscription_responses.py",
        project / "scripts/prepare-phase1-frontier-model-arc.py",
        project / "scripts/preflight-phase1-frontier-model-arc.py",
        project / "scripts/run-phase1-frontier-model-arc.py",
        project / "scripts/audit-phase1-frontier-model-arc.py",
        project / "scripts/litellm-phase1-frontier-model-arc.yaml",
    ]
    plan = {
        "schemaVersion": 1,
        "planVersion": PLAN_VERSION,
        "arcVersion": ARC_VERSION,
        "status": "local_plan_only_no_authentication_or_data_transfer",
        "source": {
            "corpusID": corpus["corpusID"],
            "corpusSHA256": sha256(corpus_path / "corpus.json"),
            "examplesSHA256": sha256(corpus_path / "examples.jsonl"),
            "packingSHA256": sha256(packed_path / "packing.json"),
            "packedExamplesSHA256": sha256(packed_path / "packed-examples.jsonl"),
            "contextPlansSHA256": sha256(packed_path / "context-plans.jsonl"),
            "prospectiveExampleIDs": evaluation_ids,
            "semanticModelInputSHA256s": [
                plans[value]["semanticModelInputSHA256"] for value in evaluation_ids
            ],
        },
        "models": list(MODEL_SPECS),
        "operations": {
            "examplesPerModel": len(evaluation_ids),
            "modelArms": len(MODEL_SPECS),
            "maximumLogicalSubscriptionCalls": len(evaluation_ids) * len(MODEL_SPECS),
            "scoreBeforeAnyTraining": True,
            "trainingOperations": 0,
        },
        "transport": {
            "type": "loopback_litellm_chatgpt_subscription_responses",
            "endpoint": "http://127.0.0.1:4000/v1/responses",
            "openAIAPIKeyFallbackAllowed": False,
            "tokenLimitFieldsSent": False,
            "sameSemanticInputsAcrossModels": True,
        },
        "existingComparator": {
            "key": "gpt-5.6-sol-xhigh",
            "directory": str(existing_path),
            "manifestSHA256": sha256(existing_manifest),
            "scoresSHA256": sha256(existing_scores),
        },
        "implementation": {
            "fileDigestsSHA256": {
                str(path.relative_to(project)): sha256(path) for path in implementation
            }
        },
        "authorizationBoundary": {
            "thisCommandContactsProviders": False,
            "thisCommandTransmitsPersonalData": False,
            "fullExecutionRequiresPersonalDataConfirmation": True,
            "fullExecutionRequiresSubscriptionUsageConfirmation": True,
            "noAPIKeyFallback": True,
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print(f"Wrote local model-arc plan to {output}")
    print(f"Frozen subscription calls: {plan['operations']['maximumLogicalSubscriptionCalls']}")
    print("No authentication, provider call, or personal-data transfer occurred.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

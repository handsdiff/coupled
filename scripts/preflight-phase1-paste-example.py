#!/usr/bin/env python3
"""Run one privacy-filtered grounded-paste Phase 1 frontier preflight."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from phase1_experiment import semantic_model_input, target_text, validate_inputs
from phase1_subscription_responses import (
    MODEL,
    REASONING_EFFORT,
    SubscriptionResponseError,
    request_completion,
)
from phase1_training_contract import TrainingContractError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--packed", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--endpoint", default="http://127.0.0.1:4000/v1/responses")
    parser.add_argument("--example-id")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        raise TrainingContractError(f"output already exists: {args.output}")

    corpus_path = args.corpus.expanduser().resolve()
    packed_path = args.packed.expanduser().resolve()
    corpus, examples, packed, plans = validate_inputs(corpus_path, packed_path)
    packed_by_id = {row["exampleID"]: row for row in packed.rows}
    candidates = [
        example for example in examples
        if packed_by_id[example["exampleID"]]["pasteActionCount"] > 0
    ]
    if args.example_id:
        candidates = [
            example for example in candidates
            if example["exampleID"] == args.example_id
        ]
    if not candidates:
        raise TrainingContractError("no matching grounded-paste example")
    example = candidates[0]
    plan = plans[example["exampleID"]]
    model_input = semantic_model_input(corpus_path, example, plan)
    expected = target_text(example["target"])
    report: dict[str, Any] = {
        "schemaVersion": 1,
        "preflightVersion": "phase1-paste-example-preflight-v1",
        "status": "local_plan_only",
        "corpusID": corpus["corpusID"],
        "exampleID": example["exampleID"],
        "targetEventID": example["targetEventID"],
        "experimentBlockID": example["experimentBlockID"],
        "model": MODEL,
        "reasoningEffort": REASONING_EFFORT,
        "semanticModelInputSHA256": plan["semanticModelInputSHA256"],
        "semanticModelInputUTF8Bytes": len(model_input.encode()),
        "expectedTarget": expected,
        "expectedPasteActions": packed_by_id[example["exampleID"]]["pasteActionCount"],
        "privacyPolicySHA256": hashlib.sha256(
            (corpus_path / "privacy-policy.json").read_bytes()
        ).hexdigest(),
        "usesOpenAIAPIKey": False,
    }
    if args.execute:
        started = time.monotonic()
        prediction, response = request_completion(
            args.endpoint,
            model_input,
            local_proxy_key=os.environ.get("LITELLM_PROXY_KEY"),
        )
        report.update({
            "status": "authenticated_single_example_complete",
            "prediction": prediction,
            "exactMatch": prediction == expected,
            "normalizedExactMatch": prediction.strip() == expected.strip(),
            "latencySeconds": time.monotonic() - started,
            "responseID": response.get("id"),
            "responseModel": response.get("model"),
            "usage": response.get("usage"),
        })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {report['status']} to {args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, KeyError, SubscriptionResponseError, TrainingContractError) as error:
        raise SystemExit(f"preflight-phase1-paste-example: {error}")

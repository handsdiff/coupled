#!/usr/bin/env python3
"""Audit the local, no-provider Phase 1 Qwen 35B experiment artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from phase1_qwen35_native import (
    MODEL_SPECS,
    RENDERER_NAME,
    audit_native_pack,
    canonical_sha256,
    load_jsonl,
)
from phase1_training_contract import (
    IGNORE_LABEL,
    TrainingContractError,
    adapt_row_to_tinker,
    sha256,
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise TrainingContractError(message)


def audit_plan(
    directory: Path,
    *,
    corpus: Path | None,
    shared_pack: Path | None,
) -> dict[str, Any]:
    directory = directory.expanduser().resolve()
    plan_path = directory / "provider-plan.json"
    if not plan_path.is_file():
        raise TrainingContractError(f"missing provider plan: {plan_path}")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    require(
        plan.get("status")
        == "local_preflight_complete_no_provider_calls_authorization_required",
        "provider plan is not at the no-provider authorization boundary",
    )
    require(
        plan.get("authorizationBoundary")
        == {
            "paidOperationsAuthorized": False,
            "personalDataTransmitted": False,
            "providerCallsMadeByThisPreparation": False,
        },
        "provider plan authorization boundary changed",
    )
    require(
        plan.get("training", {}).get("renderer") == RENDERER_NAME,
        "provider plan uses the wrong renderer",
    )
    require(
        plan.get("training", {}).get("rendererReduction") == "none",
        "provider plan would normalize per-example completion loss",
    )
    require(
        plan.get("experiment", {}).get("terminalBlockReceivesUpdate") is False,
        "provider plan would waste a terminal update",
    )

    manifests: dict[str, dict[str, Any]] = {}
    rows_by_model: dict[str, list[dict[str, Any]]] = {}
    for model_key in MODEL_SPECS:
        manifest, rows = audit_native_pack(
            directory / model_key,
            corpus_directory=corpus,
            shared_pack_directory=shared_pack,
        )
        manifests[model_key] = manifest
        rows_by_model[model_key] = rows
        artifact = plan["nativeArtifacts"][model_key]
        require(
            artifact["nativePackSHA256"]
            == sha256(directory / model_key / "native-pack.json"),
            f"{model_key} native manifest digest changed",
        )
        require(
            artifact["renderedExamplesSHA256"]
            == sha256(directory / model_key / "rendered-examples.jsonl"),
            f"{model_key} native rows digest changed",
        )

    base = manifests["qwen35_base"]
    hybrid = manifests["qwen36_hybrid"]
    require(
        base["tokenizerVocabularySHA256"] == hybrid["tokenizerVocabularySHA256"],
        "the two local tokenizer vocabularies differ",
    )
    require(
        base["pasteMarkerTokenIDs"] == hybrid["pasteMarkerTokenIDs"],
        "the two tokenizers encode <|paste|> differently",
    )
    require(
        base["responseTerminatorTokenID"]
        == hybrid["responseTerminatorTokenID"]
        == 248046,
        "native response terminator is not <|im_end|>",
    )
    require(
        base["tokenizerEOSID"] != base["responseTerminatorTokenID"],
        "Base tokenizer EOS trap is no longer exercised by the audit",
    )
    require(
        hybrid["tokenizerEOSID"] == hybrid["responseTerminatorTokenID"],
        "hybrid tokenizer EOS metadata changed unexpectedly",
    )

    base_rows = rows_by_model["qwen35_base"]
    hybrid_rows = rows_by_model["qwen36_hybrid"]
    require(
        [row["exampleID"] for row in base_rows]
        == [row["exampleID"] for row in hybrid_rows],
        "native model packs have different example order",
    )
    for left, right in zip(base_rows, hybrid_rows, strict=True):
        for key in (
            "semanticModelInputSHA256",
            "targetTextSHA256",
            "inputIDs",
            "labels",
            "modelInputTokenCount",
            "targetTokenCount",
            "pasteActionCount",
        ):
            require(
                left[key] == right[key],
                f"tokenizer packs differ for {left['exampleID']}: {key}",
            )
        contract = adapt_row_to_tinker(left)
        weighted = [
            target
            for target, weight in zip(
                contract.target_tokens, contract.weights, strict=True
            )
            if weight == 1.0
        ]
        require(
            weighted[-1] == left["responseTerminatorTokenID"],
            f"{left['exampleID']} does not weight the native terminator",
        )
        require(
            weighted[:-1]
            == left["inputIDs"][left["modelInputTokenCount"] : -1],
            f"{left['exampleID']} weighted content is not exact target content",
        )
        require(
            all(
                label == IGNORE_LABEL
                for label in left["labels"][: left["modelInputTokenCount"]]
            ),
            f"{left['exampleID']} applies loss to prompt/native envelope",
        )

    result = {
        "status": "passed",
        "providerPlanSHA256": sha256(plan_path),
        "models": {
            key: {
                "model": manifests[key]["model"],
                "examples": manifests[key]["counts"]["examples"],
                "maximumFullSequenceTokens": manifests[key]["counts"][
                    "maximumFullSequenceTokens"
                ],
                "lossBearingTokens": manifests[key]["counts"][
                    "lossBearingTokens"
                ],
                "nativeRowsCanonicalSHA256": canonical_sha256(rows_by_model[key]),
            }
            for key in MODEL_SPECS
        },
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, type=Path)
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--shared-pack", type=Path)
    arguments = parser.parse_args()
    if (arguments.corpus is None) != (arguments.shared_pack is None):
        parser.error("--corpus and --shared-pack must be supplied together")
    result = audit_plan(
        arguments.experiment,
        corpus=arguments.corpus,
        shared_pack=arguments.shared_pack,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

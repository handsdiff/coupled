#!/usr/bin/env python3
"""Audit the local, no-provider Phase 1 Qwen 35B experiment artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from phase1_qwen35_native import (
    MODEL_SPECS,
    RENDERER_CONTRACT,
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
        plan.get("training", {}).get("rendererContract") == RENDERER_CONTRACT,
        "provider plan uses the wrong renderer contract",
    )
    require(
        plan.get("training", {}).get("renderersByModel")
        == {key: MODEL_SPECS[key]["renderer"] for key in MODEL_SPECS},
        "provider plan model-specific renderers changed",
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
        base["rendererStrategy"] == "raw_completion_eos"
        and base["responseTerminatorTokenID"] == base["tokenizerEOSID"] == 248044
        and base["expectedParseTermination"] == "eos",
        "Base arm is not exact raw continuation terminated by tokenizer EOS",
    )
    require(
        hybrid["rendererStrategy"] == "qwen3_5_disable_thinking_exact_content"
        and hybrid["responseTerminatorTokenID"]
        == hybrid["tokenizerEOSID"]
        == 248046
        and hybrid["expectedParseTermination"] == "stop_sequence",
        "hybrid arm is not exact-content native non-thinking chat",
    )
    require(base["renderer"] != hybrid["renderer"], "model renderers were conflated")

    base_rows = rows_by_model["qwen35_base"]
    hybrid_rows = rows_by_model["qwen36_hybrid"]
    require(
        [row["exampleID"] for row in base_rows]
        == [row["exampleID"] for row in hybrid_rows],
        "native model packs have different example order",
    )
    tokenized_sequences_differ = 0
    for left, right in zip(base_rows, hybrid_rows, strict=True):
        for key in (
            "semanticModelInputSHA256",
            "targetTextSHA256",
            "pasteActionCount",
        ):
            require(
                left[key] == right[key],
                f"tokenizer packs differ for {left['exampleID']}: {key}",
            )
        tokenized_sequences_differ += left["inputIDs"] != right["inputIDs"]
        for row in (left, right):
            contract = adapt_row_to_tinker(row)
            weighted = [
                target
                for target, weight in zip(
                    contract.target_tokens, contract.weights, strict=True
                )
                if weight == 1.0
            ]
            require(
                weighted[-1] == row["responseTerminatorTokenID"],
                f"{row['exampleID']} does not weight its response terminator",
            )
            require(
                weighted[:-1]
                == row["inputIDs"][row["modelInputTokenCount"] : -1],
                f"{row['exampleID']} weighted content is not exact target content",
            )
            require(
                all(
                    label == IGNORE_LABEL
                    for label in row["labels"][: row["modelInputTokenCount"]]
                ),
                f"{row['exampleID']} applies loss to its prompt envelope",
            )
    require(
        tokenized_sequences_differ == len(base_rows),
        "model-specific renderers unexpectedly produced identical token sequences",
    )

    result = {
        "status": "passed",
        "providerPlanSHA256": sha256(plan_path),
        "modelSpecificTokenSequences": True,
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

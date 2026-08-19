#!/usr/bin/env python3
"""Prepare a no-network, no-data-transfer Phase 1 provider operation plan."""

from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from phase1_experiment import TINKER_TRAINING_CONTRACT, validate_inputs
from phase1_training_contract import TrainingContractError, sha256


PLAN_VERSION = "phase1-provider-plan-v3"
QWEN_MODEL = "Qwen/Qwen3.5-9B-Base"
OPENAI_MODEL = "gpt-5.6-sol"
OPENAI_REASONING_EFFORT = "xhigh"
TINKER_PRICING_URL = "https://tinker-docs.thinkingmachines.ai/tinker/models/"
OPENAI_MODEL_URL = "https://developers.openai.com/api/docs/models/gpt-5.6-sol"
TINKER_PREFILL_PER_MILLION = Decimal("0.66")
TINKER_SAMPLE_PER_MILLION = Decimal("1.995")
TINKER_TRAIN_PER_MILLION = Decimal("1.463")
OPENAI_INPUT_PER_MILLION = Decimal("5.00")
OPENAI_OUTPUT_PER_MILLION = Decimal("30.00")
CHECKPOINT_RESERVE = Decimal("1.00")
HARD_EXECUTION_CEILING = Decimal("40.00")


def money(tokens: int, price: Decimal) -> Decimal:
    return Decimal(tokens) * price / Decimal(1_000_000)


def decimal_string(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.000001")))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--packed", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--tinker-project-id", required=True)
    parser.add_argument("--epochs-per-update", type=int, default=1)
    parser.add_argument("--qwen-generation-token-ceiling", type=int, default=512)
    parser.add_argument("--openai-max-output-tokens", type=int, default=8192)
    parser.add_argument(
        "--frontier-transport",
        choices=("responses_api", "litellm_chatgpt_subscription"),
        default="responses_api",
    )
    arguments = parser.parse_args()
    if arguments.output.exists():
        raise TrainingContractError(f"output already exists: {arguments.output}")
    if (
        arguments.epochs_per_update <= 0
        or arguments.qwen_generation_token_ceiling <= 0
        or arguments.openai_max_output_tokens <= 0
    ):
        raise TrainingContractError("epochs and generation ceiling must be positive")
    if arguments.epochs_per_update != TINKER_TRAINING_CONTRACT[
        "epochsPerCumulativeUpdate"
    ]:
        raise TrainingContractError(
            "epochs per update differ from the frozen Tinker training contract"
        )

    corpus_path = arguments.corpus.expanduser().resolve()
    packed_path = arguments.packed.expanduser().resolve()
    corpus, examples, packed, plans = validate_inputs(corpus_path, packed_path)
    packed_by_id = {row["exampleID"]: row for row in packed.rows}
    block_rows = [
        [packed_by_id[value] for value in block["exampleIDs"]]
        for block in corpus["blocking"]["blocks"]
    ]
    cumulative: list[dict] = []
    update_plans = []
    total_training_positions = 0
    total_loss_presentations = 0
    presentation_counts = {example["exampleID"]: 0 for example in examples}
    for ordinal, (block, rows) in enumerate(
        zip(corpus["blocking"]["blocks"], block_rows, strict=True), 1
    ):
        cumulative.extend(rows)
        positions = arguments.epochs_per_update * sum(
            len(row["inputIDs"]) - 1 for row in cumulative
        )
        loss_presentations = arguments.epochs_per_update * sum(
            row["targetTokenCount"] for row in cumulative
        )
        for row in cumulative:
            presentation_counts[row["exampleID"]] += arguments.epochs_per_update
        total_training_positions += positions
        total_loss_presentations += loss_presentations
        update_plans.append({
            "updateOrdinal": ordinal,
            "afterBlockID": block["blockID"],
            "cumulativeExamples": len(cumulative),
            "epochs": arguments.epochs_per_update,
            "submittedPositions": positions,
            "lossBearingTokenPresentations": loss_presentations,
        })

    qwen_model_input_tokens = sum(row["modelInputTokenCount"] for row in packed.rows)
    qwen_full_sequence_tokens = sum(len(row["inputIDs"]) for row in packed.rows)
    tinker_nll_prefill = 2 * qwen_full_sequence_tokens
    tinker_generation_prefill = 2 * qwen_model_input_tokens
    tinker_prefill = tinker_nll_prefill + tinker_generation_prefill
    tinker_sample_ceiling = (
        2 * len(examples) * arguments.qwen_generation_token_ceiling
    )
    tinker_cost = {
        "trainingUSD": money(total_training_positions, TINKER_TRAIN_PER_MILLION),
        "prefillUSD": money(tinker_prefill, TINKER_PREFILL_PER_MILLION),
        "sampleUSD": money(tinker_sample_ceiling, TINKER_SAMPLE_PER_MILLION),
        "checkpointReserveUSD": CHECKPOINT_RESERVE,
    }
    tinker_total = sum(tinker_cost.values(), Decimal(0))
    if tinker_total > HARD_EXECUTION_CEILING:
        raise TrainingContractError("Tinker projection exceeds the hard execution ceiling")

    context_blocks = {
        row["contextBlockID"]: row
        for row in (
            json.loads(line)
            for line in (corpus_path / "context-blocks.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }
    semantic_utf8_bytes = 0
    semantic_characters = 0
    for example in examples:
        plan = plans[example["exampleID"]]
        serialized = []
        for block in plan["retainedContextBlocks"]:
            text = block.get("serializedOverride")
            if text is None:
                text = context_blocks[block["contextBlockID"]]["serialized"]
            serialized.append(text)
        context = "\n".join(serialized)
        body = example["query"] if not context else context + "\n" + example["query"]
        semantic_input = plan["taskInstruction"] + "\n" + body
        if hashlib.sha256(semantic_input.encode()).hexdigest() != plan[
            "semanticModelInputSHA256"
        ]:
            raise TrainingContractError("semantic context plan reconstruction failed")
        semantic_utf8_bytes += len(semantic_input.encode())
        semantic_characters += len(semantic_input)

    openai_output_ceiling = len(examples) * arguments.openai_max_output_tokens
    openai_proxy_input_tokens = qwen_model_input_tokens
    openai_proxy_cost = money(openai_proxy_input_tokens, OPENAI_INPUT_PER_MILLION)
    openai_output_cost = money(openai_output_ceiling, OPENAI_OUTPUT_PER_MILLION)
    # A UTF-8 byte is a deliberately loose tokenizer-independent upper bound
    # for ordinary text tokenization. It is a safety ceiling, not an estimate.
    openai_byte_bound_cost = money(semantic_utf8_bytes, OPENAI_INPUT_PER_MILLION)

    frontier_arm = (
        "litellm_chatgpt_subscription_gpt_5.6_sol_xhigh"
        if arguments.frontier_transport == "litellm_chatgpt_subscription"
        else "responses_api_gpt_5.6_sol_xhigh"
    )
    openai_plan = {
        "model": OPENAI_MODEL,
        "reasoningEffort": OPENAI_REASONING_EFFORT,
        "transport": arguments.frontier_transport,
        "operations": {
            "responseCalls": len(examples),
            "semanticInputCharacters": semantic_characters,
            "semanticInputUTF8Bytes": semantic_utf8_bytes,
            "qwenTokenCountProxyNotBillingAuthority": openai_proxy_input_tokens,
            "comparableTargetNLLAvailable": False,
        },
            "unverifiedUntilAuthenticatedPreflight": [
            "account model access",
            "resolved model snapshot if exposed",
            "actual usage-limit consumption",
        ],
    }
    if arguments.frontier_transport == "responses_api":
        openai_plan.update({
            "billing": "separate_openai_api_billing",
            "pricingAsOf": "2026-08-18",
            "pricingSource": OPENAI_MODEL_URL,
            "pricesPerMillionUSD": {
                "input": str(OPENAI_INPUT_PER_MILLION),
                "output": str(OPENAI_OUTPUT_PER_MILLION),
            },
            "projectedCostUSD": {
                "inputUsingQwenTokenProxy": decimal_string(openai_proxy_cost),
                "maximumOutput": decimal_string(openai_output_cost),
                "proxyTotal": decimal_string(openai_proxy_cost + openai_output_cost),
                "tokenizerIndependentUTF8ByteInputBound": decimal_string(openai_byte_bound_cost),
                "byteBoundTotal": decimal_string(openai_byte_bound_cost + openai_output_cost),
            },
        })
        openai_plan["operations"]["maximumOutputTokensIncludingReasoning"] = (
            openai_output_ceiling
        )
    else:
        openai_plan.update({
            "billing": "chatgpt_subscription_shared_codex_usage_pool",
            "providerModelRoute": "chatgpt/gpt-5.6-sol",
            "localEndpoint": "http://127.0.0.1:4000/v1/responses",
            "marginalAPIChargeProjectedUSD": None,
            "transportDocumentation": "https://docs.litellm.ai/docs/providers/chatgpt",
            "usageLimitSource": "https://help.openai.com/en/articles/11369540/",
            "experimentalQualification": (
                "Direct subscription-backed Responses request through a local LiteLLM proxy; "
                "not OpenAI Platform API billing"
            ),
            "outputTokenCeilingEnforceableByTransport": False,
            "tokenLimitFieldsStrippedByProvider": True,
            "openAIAPIKeyFallbackAllowed": False,
            "requiresNoAdditionalCreditAuthorization": True,
        })

    project_directory = Path(__file__).resolve().parent.parent
    implementation_paths = [
        Path(__file__).resolve(),
        project_directory / "scripts/phase1_experiment.py",
        project_directory / "scripts/phase1_training_contract.py",
        project_directory / "scripts/phase1_tinker_overfit_contract.py",
        project_directory / "scripts/phase1_subscription_responses.py",
        project_directory / "scripts/run-phase1-frontier-arm.py",
        project_directory / "scripts/run-phase1-tinker-prequential.py",
        project_directory / "scripts/audit-phase1-real-experiment.py",
        project_directory / "scripts/tinker-requirements.txt",
        project_directory / "scripts/litellm-subscription-requirements.txt",
    ]
    for path in implementation_paths:
        if not path.is_file():
            raise TrainingContractError(f"missing experiment implementation file: {path}")

    plan = {
        "schemaVersion": 1,
        "planVersion": PLAN_VERSION,
        "status": "local_plan_only_no_authentication_or_data_transfer",
        "implementation": {
            "fileDigestsSHA256": {
                str(path.relative_to(project_directory)): sha256(path)
                for path in implementation_paths
            },
        },
        "source": {
            "corpusID": corpus["corpusID"],
            "corpusSHA256": sha256(corpus_path / "corpus.json"),
            "examplesSHA256": sha256(corpus_path / "examples.jsonl"),
            "packingSHA256": sha256(packed_path / "packing.json"),
            "packedExamplesSHA256": sha256(packed_path / "packed-examples.jsonl"),
            "contextPlansSHA256": sha256(packed_path / "context-plans.jsonl"),
        },
        "protocol": {
            "examples": len(examples),
            "blocks": len(block_rows),
            "arms": ["frozen_qwen", frontier_arm, "personalized_qwen"],
            "taskInstruction": packed.manifest["packing"]["taskInstruction"],
            "qwenGenerationTokenCeilingPerExample": (
                arguments.qwen_generation_token_ceiling
            ),
            "openAIMaxOutputTokensPerExampleIncludingReasoning": (
                arguments.openai_max_output_tokens
            ),
            "scoreCompleteBlockBeforeUpdate": True,
            "personalizedUpdatePolicy": "warm_start_then_train_full_cumulative_corpus",
            "updates": update_plans,
            "finalPerExamplePresentationCounts": presentation_counts,
        },
        "tinker": {
            "projectID": arguments.tinker_project_id,
            "model": QWEN_MODEL,
            "tokenizerRevision": packed.manifest["tokenizer"]["resolvedRevision"],
            "pricingAsOf": "2026-08-18",
            "pricingSource": TINKER_PRICING_URL,
            "pricesPerMillionUSD": {
                "prefill": str(TINKER_PREFILL_PER_MILLION),
                "sample": str(TINKER_SAMPLE_PER_MILLION),
                "training": str(TINKER_TRAIN_PER_MILLION),
            },
            "operations": {
                "frozenAndPersonalizedNLLPrefillTokens": tinker_nll_prefill,
                "frozenAndPersonalizedGenerationPrefillTokens": tinker_generation_prefill,
                "totalPrefillTokens": tinker_prefill,
                "maximumSampledTokens": tinker_sample_ceiling,
                "trainingSubmittedPositions": total_training_positions,
                "lossBearingTokenPresentations": total_loss_presentations,
                "samplerCheckpointSaves": len(block_rows),
                "optimizerStateCheckpointSaves": len(block_rows),
            },
            "projectedCostUSD": {
                **{key: decimal_string(value) for key, value in tinker_cost.items()},
                "totalIncludingReserve": decimal_string(tinker_total),
            },
            "trainingContract": TINKER_TRAINING_CONTRACT,
            "hardExecutionCeilingUSD": str(HARD_EXECUTION_CEILING),
            "interruptionPolicy": {
                "partialUpdateReplayAllowedUnderThisPlan": False,
                "inFlightOperationReplayAllowedUnderThisPlan": False,
                "reason": "a repeated paid operation is outside the frozen cost projection",
            },
        },
        "openai": openai_plan,
        "authorizationBoundary": {
            "thisCommandReadsCredentials": False,
            "thisCommandContactsProviders": False,
            "thisCommandTransmitsPersonalData": False,
            "separateApprovalRequiredForAuthenticatedMetadataCalls": True,
            "separateApprovalRequiredForAnyDataBearingCall": True,
            "hardCostCeilingFrozen": True,
            "chatGPTSubscriptionCannotFundResponsesAPI": True,
        },
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote local provider plan to {arguments.output}")
    print(f"Tinker projected total including reserve: ${decimal_string(tinker_total)}")
    if arguments.frontier_transport == "responses_api":
        print(
            "OpenAI proxy / byte-bound totals: "
            f"${decimal_string(openai_proxy_cost + openai_output_cost)} / "
            f"${decimal_string(openai_byte_bound_cost + openai_output_cost)}"
        )
    else:
        print(
            "OpenAI frontier transport: ChatGPT-subscription LiteLLM Responses; "
            "no Responses API charge is projected."
        )
    print("No authentication, provider call, or personal-data transfer occurred.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TrainingContractError) as error:
        raise SystemExit(f"prepare-phase1-experiment: {error}")

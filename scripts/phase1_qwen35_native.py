#!/usr/bin/env python3
"""Native Qwen renderer contract for the Phase 1 35B comparison.

The semantic corpus and its shared 32K context plans remain tokenizer-neutral
authority.  This module applies the official Tinker Cookbook Qwen renderer at
the final model boundary and proves the exact causal loss positions.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from phase1_experiment import (
    semantic_model_input,
    target_text,
    validate_inputs,
)
from phase1_training_contract import (
    IGNORE_LABEL,
    TrainingContractError,
    adapt_row_to_tinker,
    sha256,
)


NATIVE_PACK_VERSION = "phase1-qwen35-native-pack-v1"
NATIVE_SCHEMA_VERSION = 1
RENDERER_NAME = "coupled/qwen3_5_disable_thinking_exact_content_v1"
UPSTREAM_RENDERER_NAME = "qwen3_5_disable_thinking"
MAXIMUM_CONTEXT_LENGTH = 65_536
GENERATION_TOKEN_CEILING = 512

MODEL_SPECS: dict[str, dict[str, str]] = {
    "qwen35_base": {
        "model": "Qwen/Qwen3.5-35B-A3B-Base",
        "localTokenizerRevision": "0f0813072d2358973511097385626f21fcb6d422",
        "modelType": "base",
    },
    "qwen36_hybrid": {
        "model": "Qwen/Qwen3.6-35B-A3B",
        "localTokenizerRevision": "995ad96eacd98c81ed38be0c5b274b04031597b0",
        "modelType": "hybrid_nonthinking",
    },
}


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode()


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise TrainingContractError(
                    f"{path}:{line_number}: invalid JSON: {error}"
                ) from error
            if not isinstance(value, dict):
                raise TrainingContractError(
                    f"{path}:{line_number}: expected a JSON object"
                )
            rows.append(value)
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("wb") as handle:
        for row in rows:
            handle.write(canonical_bytes(row))


def package_versions() -> dict[str, str]:
    result = {}
    for package in ("tinker", "tinker-cookbook", "transformers", "tokenizers"):
        try:
            result[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError as error:
            raise TrainingContractError(
                f"native Qwen rendering requires {package}"
            ) from error
    return result


def vocabulary_sha256(tokenizer: Any) -> str:
    digest = hashlib.sha256()
    for token, token_id in sorted(tokenizer.get_vocab().items()):
        digest.update(canonical_bytes([token, token_id]))
    return digest.hexdigest()


def tokenizer_file_digests(directory: Path) -> dict[str, str]:
    return {
        str(path.relative_to(directory)): sha256(path)
        for path in sorted(directory.rglob("*"))
        if path.is_file() and ".cache" not in path.parts
    }


@dataclass(frozen=True)
class NativeRuntime:
    tokenizer: Any
    renderer: Any
    response_terminator_token_id: int
    response_terminator_text: str


def _exact_content_renderer(tokenizer: Any) -> Any:
    """Use Qwen's native non-thinking format without normalizing WRITE text.

    The upstream Qwen3.5 renderer calls ``strip()`` on every string message.
    That is unsuitable for an autocomplete corpus where a leading or trailing
    space can be part of the exact human completion.  Tinker Cookbook supports
    custom renderers; this subclass changes only that normalization step.  All
    framing, parsing, tool syntax, and stop-token behavior remain inherited
    from the official Qwen3.5 non-thinking renderer.
    """
    try:
        from tinker_cookbook.renderers.qwen3 import Qwen3VLRenderer
        from tinker_cookbook.renderers.qwen3_5 import (
            Qwen3_5DisableThinkingRenderer,
        )
    except ImportError as error:
        raise TrainingContractError(
            "native Qwen rendering requires the Qwen3.5 Cookbook renderer"
        ) from error

    class ExactContentQwen3_5DisableThinkingRenderer(
        Qwen3_5DisableThinkingRenderer
    ):
        def render_message(self, message: Any, ctx: Any) -> Any:
            # Deliberately bypass Qwen3_5Renderer.render_message, whose sole
            # preprocessing step is message["content"].strip().  Dispatching
            # through Qwen3VLRenderer retains every other virtual override on
            # this Qwen3.5 subclass, including the native non-thinking header.
            return Qwen3VLRenderer.render_message(self, message, ctx)

    return ExactContentQwen3_5DisableThinkingRenderer(tokenizer)


def load_native_runtime(model_key: str, tokenizer_directory: Path) -> NativeRuntime:
    if model_key not in MODEL_SPECS:
        raise TrainingContractError(f"unsupported native Qwen model key: {model_key}")
    tokenizer_directory = tokenizer_directory.expanduser().resolve()
    if not (tokenizer_directory / "tokenizer.json").is_file():
        raise TrainingContractError(
            f"tokenizer directory is incomplete: {tokenizer_directory}"
        )
    try:
        from tinker_cookbook import tokenizer_utils
    except ImportError as error:
        raise TrainingContractError(
            "native Qwen rendering requires tinker-cookbook"
        ) from error

    tokenizer = tokenizer_utils.get_tokenizer(str(tokenizer_directory))
    return native_runtime_from_tokenizer(model_key, tokenizer)


def native_runtime_from_tokenizer(model_key: str, tokenizer: Any) -> NativeRuntime:
    """Construct the exact renderer around a local or Tinker-returned tokenizer."""
    if model_key not in MODEL_SPECS:
        raise TrainingContractError(f"unsupported native Qwen model key: {model_key}")
    renderer = _exact_content_renderer(tokenizer)
    stop_sequences = renderer.get_stop_sequences()
    if len(stop_sequences) != 1 or type(stop_sequences[0]) is not int:
        raise TrainingContractError(
            "Qwen native renderer must expose one integer response terminator"
        )
    terminator = stop_sequences[0]
    terminator_text = tokenizer.decode(
        [terminator], skip_special_tokens=False, clean_up_tokenization_spaces=False
    )
    if terminator_text != "<|im_end|>":
        raise TrainingContractError(
            f"unexpected Qwen response terminator: {terminator_text!r}"
        )
    return NativeRuntime(tokenizer, renderer, terminator, terminator_text)


def _float_list(value: Any) -> list[float]:
    if hasattr(value, "tolist"):
        value = value.tolist()
    if not isinstance(value, list):
        raise TrainingContractError("renderer weights are not a list")
    return [float(item) for item in value]


def render_native_row(
    *,
    example: dict[str, Any],
    semantic_input: str,
    runtime: NativeRuntime,
) -> dict[str, Any]:
    try:
        from tinker_cookbook.renderers import TrainOnWhat, get_text_content
        from tinker_cookbook.supervised.data import conversation_to_datum
    except ImportError as error:
        raise TrainingContractError(
            "native Qwen rendering requires Tinker Cookbook supervised helpers"
        ) from error

    completion = target_text(example["target"])
    messages = [
        {"role": "user", "content": semantic_input},
        {"role": "assistant", "content": completion},
    ]
    prompt_ids = runtime.renderer.build_generation_prompt(messages[:-1]).to_ints()
    full_input, full_weights_tensor = runtime.renderer.build_supervised_example(
        messages, train_on_what=TrainOnWhat.LAST_ASSISTANT_MESSAGE
    )
    full_ids = full_input.to_ints()
    full_weights = _float_list(full_weights_tensor)
    datum = conversation_to_datum(
        messages,
        runtime.renderer,
        max_length=None,
        train_on_what=TrainOnWhat.LAST_ASSISTANT_MESSAGE,
        reduction="none",
    )
    shifted_input = datum.model_input.to_ints()
    shifted_targets = [
        int(value) for value in datum.loss_fn_inputs["target_tokens"].to_numpy().tolist()
    ]
    shifted_weights = _float_list(
        datum.loss_fn_inputs["weights"].to_numpy()
    )

    if not full_ids or len(full_ids) > MAXIMUM_CONTEXT_LENGTH:
        raise TrainingContractError(
            f"{example['exampleID']} rendered length is outside the Tinker limit"
        )
    if len(full_ids) != len(full_weights):
        raise TrainingContractError(
            f"{example['exampleID']} renderer token/weight lengths differ"
        )
    if full_ids[: len(prompt_ids)] != prompt_ids:
        raise TrainingContractError(
            f"{example['exampleID']} supervised sequence does not extend generation prompt"
        )
    expected_weights = [0.0] * len(prompt_ids) + [1.0] * (
        len(full_ids) - len(prompt_ids)
    )
    if full_weights != expected_weights:
        raise TrainingContractError(
            f"{example['exampleID']} native renderer loss is not completion-only"
        )
    if not (
        shifted_input == full_ids[:-1]
        and shifted_targets == full_ids[1:]
        and shifted_weights == full_weights[1:]
    ):
        raise TrainingContractError(
            f"{example['exampleID']} Tinker Cookbook causal shift disagrees"
        )
    if full_ids[-1] != runtime.response_terminator_token_id:
        raise TrainingContractError(
            f"{example['exampleID']} lacks the native response terminator"
        )
    if sum(full_weights) != len(full_ids) - len(prompt_ids):
        raise TrainingContractError(
            f"{example['exampleID']} completion loss count is inconsistent"
        )

    response_ids = full_ids[len(prompt_ids) :]
    parsed_message, _ = runtime.renderer.parse_response(response_ids)
    parsed_text = get_text_content(parsed_message)
    if parsed_text != completion:
        raise TrainingContractError(
            f"{example['exampleID']} native response does not round-trip target text"
        )

    labels = [IGNORE_LABEL] * len(prompt_ids) + full_ids[len(prompt_ids) :]
    row = {
        "schemaVersion": NATIVE_SCHEMA_VERSION,
        "nativePackVersion": NATIVE_PACK_VERSION,
        "exampleID": example["exampleID"],
        "experimentBlockID": example["experimentBlockID"],
        "targetEventID": example["targetEventID"],
        "targetUnitID": example.get("targetUnitID"),
        "sessionID": example.get("sessionID"),
        "semanticModelInputSHA256": hashlib.sha256(
            semantic_input.encode()
        ).hexdigest(),
        "targetTextSHA256": hashlib.sha256(completion.encode()).hexdigest(),
        "inputIDs": full_ids,
        "labels": labels,
        "modelInputTokenCount": len(prompt_ids),
        "fullSequenceTokenCount": len(full_ids),
        "targetTokenCount": int(sum(full_weights)),
        "targetContentTokenCount": int(sum(full_weights)) - 1,
        "pasteActionCount": sum(
            segment.get("type") == "paste"
            for segment in example.get("target", {}).get("segments", [])
        ),
        "responseTerminatorTokenID": runtime.response_terminator_token_id,
        "generationStopTokenIDs": [runtime.response_terminator_token_id],
        "lossContract": {
            "trainOnWhat": "last_assistant_message",
            "reduction": "none",
            "promptAndNativeNonthinkingEnvelopeMasked": True,
            "authoredContentAndLiteralPasteMarkerWeighted": True,
            "oneNativeResponseTerminatorWeighted": True,
        },
    }
    contract = adapt_row_to_tinker(row)
    if (
        contract.model_input_token_ids != shifted_input
        or contract.target_tokens != shifted_targets
        or contract.weights != shifted_weights
        or contract.weighted_positions != row["targetTokenCount"]
    ):
        raise TrainingContractError(
            f"{example['exampleID']} provider-neutral causal adapter changed native Datum"
        )
    return row


def build_native_rows(
    *,
    corpus_directory: Path,
    shared_pack_directory: Path,
    model_key: str,
    tokenizer_directory: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    corpus_directory = corpus_directory.expanduser().resolve()
    shared_pack_directory = shared_pack_directory.expanduser().resolve()
    runtime = load_native_runtime(model_key, tokenizer_directory)
    return build_native_rows_with_runtime(
        corpus_directory=corpus_directory,
        shared_pack_directory=shared_pack_directory,
        model_key=model_key,
        runtime=runtime,
    )


def build_native_rows_with_runtime(
    *,
    corpus_directory: Path,
    shared_pack_directory: Path,
    model_key: str,
    runtime: NativeRuntime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    corpus_directory = corpus_directory.expanduser().resolve()
    shared_pack_directory = shared_pack_directory.expanduser().resolve()
    corpus, examples, _, plans = validate_inputs(
        corpus_directory, shared_pack_directory
    )
    rows = []
    for example in examples:
        semantic_input = semantic_model_input(
            corpus_directory, example, plans[example["exampleID"]]
        )
        rows.append(
            render_native_row(
                example=example,
                semantic_input=semantic_input,
                runtime=runtime,
            )
        )

    model = MODEL_SPECS[model_key]
    metadata = {
        "modelKey": model_key,
        **model,
        "renderer": RENDERER_NAME,
        "upstreamRenderer": UPSTREAM_RENDERER_NAME,
        "rendererAdaptation": (
            "preserve exact string content; otherwise inherit the official "
            "Qwen3.5 non-thinking renderer"
        ),
        "responseTerminatorTokenID": runtime.response_terminator_token_id,
        "responseTerminatorText": runtime.response_terminator_text,
        "tokenizerEOSID": runtime.tokenizer.eos_token_id,
        "tokenizerPadID": runtime.tokenizer.pad_token_id,
        "tokenizerLength": len(runtime.tokenizer),
        "tokenizerVocabularySHA256": vocabulary_sha256(runtime.tokenizer),
        "pasteMarkerTokenIDs": runtime.tokenizer.encode(
            "<|paste|>", add_special_tokens=False
        ),
        "packageVersions": package_versions(),
        "maximumContextLength": MAXIMUM_CONTEXT_LENGTH,
        "counts": {
            "examples": len(rows),
            "fullSequenceTokens": sum(row["fullSequenceTokenCount"] for row in rows),
            "modelInputTokens": sum(row["modelInputTokenCount"] for row in rows),
            "lossBearingTokens": sum(row["targetTokenCount"] for row in rows),
            "pasteActions": sum(row["pasteActionCount"] for row in rows),
            "maximumFullSequenceTokens": max(
                row["fullSequenceTokenCount"] for row in rows
            ),
        },
        "source": {
            "corpusID": corpus["corpusID"],
            "datasetSHA256": sha256(corpus_directory / "dataset.json"),
            "examplesSHA256": sha256(corpus_directory / "examples.jsonl"),
            "sharedPackingSHA256": sha256(shared_pack_directory / "packing.json"),
            "sharedContextPlansSHA256": sha256(
                shared_pack_directory / "context-plans.jsonl"
            ),
        },
    }
    return rows, metadata


def audit_native_pack(
    directory: Path,
    *,
    corpus_directory: Path | None = None,
    shared_pack_directory: Path | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    directory = directory.expanduser().resolve()
    manifest_path = directory / "native-pack.json"
    rows_path = directory / "rendered-examples.jsonl"
    tokenizer_directory = directory / "tokenizer"
    if not (manifest_path.is_file() and rows_path.is_file()):
        raise TrainingContractError(f"not a native Qwen pack: {directory}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not (
        manifest.get("schemaVersion") == NATIVE_SCHEMA_VERSION
        and manifest.get("nativePackVersion") == NATIVE_PACK_VERSION
        and manifest.get("renderer") == RENDERER_NAME
        and manifest.get("modelKey") in MODEL_SPECS
        and manifest.get("artifactDigestsSHA256", {}).get(
            "rendered-examples.jsonl"
        )
        == sha256(rows_path)
    ):
        raise TrainingContractError("native pack manifest is incompatible or changed")
    expected_tokenizer_digests = manifest.get("tokenizerFileDigestsSHA256")
    if expected_tokenizer_digests != tokenizer_file_digests(tokenizer_directory):
        raise TrainingContractError("native pack tokenizer files changed")
    rows = load_jsonl(rows_path)
    if len(rows) != manifest.get("counts", {}).get("examples"):
        raise TrainingContractError("native pack example count changed")
    for row in rows:
        if not (
            row.get("schemaVersion") == NATIVE_SCHEMA_VERSION
            and row.get("nativePackVersion") == NATIVE_PACK_VERSION
            and row.get("fullSequenceTokenCount") == len(row.get("inputIDs", []))
            and len(row.get("inputIDs", [])) == len(row.get("labels", []))
            and row.get("responseTerminatorTokenID")
            == manifest.get("responseTerminatorTokenID")
            and row.get("generationStopTokenIDs")
            == [manifest.get("responseTerminatorTokenID")]
            and row.get("labels", [])[-1]
            == manifest.get("responseTerminatorTokenID")
        ):
            raise TrainingContractError(
                f"native pack row is malformed: {row.get('exampleID')}"
            )
        adapt_row_to_tinker(row)
    if corpus_directory is not None or shared_pack_directory is not None:
        if corpus_directory is None or shared_pack_directory is None:
            raise TrainingContractError("native replay audit requires both source paths")
        replay_rows, replay_metadata = build_native_rows(
            corpus_directory=corpus_directory,
            shared_pack_directory=shared_pack_directory,
            model_key=manifest["modelKey"],
            tokenizer_directory=tokenizer_directory,
        )
        if canonical_sha256(replay_rows) != canonical_sha256(rows):
            raise TrainingContractError("native pack deterministic replay changed")
        for key in (
            "model",
            "localTokenizerRevision",
            "renderer",
            "responseTerminatorTokenID",
            "tokenizerEOSID",
            "tokenizerVocabularySHA256",
            "counts",
            "source",
        ):
            if manifest.get(key) != replay_metadata.get(key):
                raise TrainingContractError(f"native replay metadata changed: {key}")
    return manifest, rows

#!/usr/bin/env python3
"""Shared Inkling-Small rendering and scoring contract for Phase 1."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from phase1_experiment import canonical_bytes


INKLING_MODEL = "thinkingmachines/Inkling-Small"
INKLING_CONTRACT_VERSION = "phase1-inkling-contract-v1"
INKLING_PACK_VERSION = "phase1-inkling-pack-v1"
INKLING_PLAN_VERSION = "phase1-inkling-plan-v1"
INKLING_RUNNER_VERSION = "phase1-inkling-prequential-v1"
INKLING_AUDIT_VERSION = "phase1-inkling-audit-v1"
REASONING_CONDITIONS = {
    "reasoning_off": 0.0,
    "reasoning_on": 0.9,
}
ARM_NAMES = {
    "reasoning_off": {
        "frozen": "frozen_inkling_small_reasoning_off",
        "personalized": "personalized_inkling_small_reasoning_off",
    },
    "reasoning_on": {
        "frozen": "frozen_inkling_small_reasoning_on",
        "personalized": "personalized_inkling_small_reasoning_on",
    },
}
GENERATION_CONTRACT = {
    "temperature": 0.6,
    "seed": 17,
    "maximumTokens": 512,
}
TRAINING_CONTRACT = {
    "algorithm": "lora",
    "rank": 32,
    "seed": 17,
    "trainAttention": True,
    "trainMLP": True,
    "trainUnembedding": True,
    "batchExamplesPerForwardBackward": 1,
    "epochsPerNewBlockUpdate": 1,
    "optimizer": {
        "type": "adam",
        "learningRate": 0.0002,
        "beta1": 0.9,
        "beta2": 0.95,
        "epsilon": 1e-12,
        "weightDecay": 0.0,
        "gradientClipNorm": 1.0,
    },
    "exampleOrder": {
        "version": "phase1-prequential-order-v2",
        "algorithm": "sha256_ascending_then_example_id",
        "material": "phase1-prequential-new-block:{seed}:{updateOrdinal}:{exampleID}",
    },
    "checkpointTTLSeconds": 604800,
}


class InklingContractError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    with path.open("ab") as handle:
        handle.write(canonical_bytes(value))
        handle.flush()
        os.fsync(handle.fileno())


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(canonical_bytes(value))
    os.replace(temporary, path)


def load_semantic_inputs(
    corpus_path: Path, semantic_pack_path: Path
) -> dict[str, str]:
    examples = load_jsonl(corpus_path / "examples.jsonl")
    plans = load_jsonl(semantic_pack_path / "context-plans.jsonl")
    blocks = load_jsonl(corpus_path / "context-blocks.jsonl")
    if [value["exampleID"] for value in examples] != [
        value["exampleID"] for value in plans
    ]:
        raise InklingContractError("semantic plans differ from corpus order")
    context_by_id = {value["contextBlockID"]: value for value in blocks}
    result: dict[str, str] = {}
    for example, plan in zip(examples, plans, strict=True):
        serialized: list[str] = []
        for retained in plan["retainedContextBlocks"]:
            text = retained.get("serializedOverride")
            if text is None:
                text = context_by_id[retained["contextBlockID"]]["serialized"]
            if hashlib.sha256(text.encode()).hexdigest() != retained["serializedSHA256"]:
                raise InklingContractError("retained context block hash differs")
            serialized.append(text)
        context = "\n".join(serialized)
        body = example["query"] if not context else context + "\n" + example["query"]
        semantic = plan["taskInstruction"] + "\n" + body
        if hashlib.sha256(semantic.encode()).hexdigest() != plan["semanticModelInputSHA256"]:
            raise InklingContractError("semantic input hash differs")
        result[example["exampleID"]] = semantic
    return result


def load_experiment_blocks(corpus_path: Path) -> list[dict[str, Any]]:
    path = corpus_path / "episode-blocks.jsonl"
    if path.is_file():
        blocks = load_jsonl(path)
    else:
        manifest = json.loads((corpus_path / "corpus.json").read_text(encoding="utf-8"))
        blocks = manifest.get("blocking", {}).get("blocks", [])
    if not blocks or [value.get("blockID") for value in blocks] != [
        f"block-{ordinal:04d}" for ordinal in range(1, len(blocks) + 1)
    ]:
        raise InklingContractError("experiment blocks are absent or noncontiguous")
    return blocks


def renderer_components() -> tuple[Any, Any, Any, Any]:
    try:
        from tml_renderers import chat, tokenizers, v0
    except ImportError as error:
        raise InklingContractError(
            "Inkling rendering requires tml-renderers==0.1.0 and torch"
        ) from error
    tokenizer = tokenizers.o200k_base_chat()
    renderer = v0.Renderer(tokenizer)
    return chat, tokenizer, renderer, v0


def flatten_token_spans(spans: list[Any]) -> list[int]:
    tokens: list[int] = []
    for span in spans:
        inner = span.span
        if not hasattr(inner, "tokens"):
            raise InklingContractError("Phase 1 text rendering produced a media span")
        tokens.extend(int(value) for value in inner.tokens)
    return tokens


def _messages(chat: Any, semantic_input: str, target: str | None, effort: float) -> list[Any]:
    if not 0.0 <= effort < 1.0:
        raise InklingContractError("Inkling effort must be in [0, 1)")
    author = chat.Author
    kind = chat.AuthorKind
    messages = [
        chat.Message(chat.ThinkingEffort(round(effort * 1000)), author(kind.System)),
        chat.Message(chat.Text(semantic_input), author(kind.User)),
    ]
    if target is not None:
        messages.extend([
            chat.Message(chat.Text(target), author(kind.Model)),
            chat.Message(chat.ModelEndSampling(), author(kind.Model)),
        ])
    return messages


def render_training_row(
    *, semantic_input: str, target: str, effort: float
) -> dict[str, Any]:
    if not target:
        raise InklingContractError("Inkling target may not be empty")
    chat, tokenizer, renderer, _ = renderer_components()
    prompt_spans, _ = renderer.render_for_completion(
        _messages(chat, semantic_input, None, effort)
    )
    prompt_ids = flatten_token_spans(prompt_spans)
    rendered = renderer.render_for_sft(
        _messages(chat, semantic_input, target, effort)
    )
    if len(rendered) != 1:
        raise InklingContractError("Inkling SFT rendering produced multiple examples")
    full_ids = flatten_token_spans(rendered[0].input_token_spans)
    if full_ids[: len(prompt_ids)] != prompt_ids:
        raise InklingContractError("Inkling completion prompt is not an SFT prefix")
    target_ids = [int(value) for value in tokenizer.encode_ordinary(target)]
    message_model = int(tokenizer.encode_special("message_model"))
    content_text = int(tokenizer.encode_special("content_text"))
    end_message = int(tokenizer.encode_special("end_message"))
    end_sampling = int(tokenizer.encode_special("content_model_end_sampling"))
    expected_completion = [
        message_model,
        content_text,
        *target_ids,
        end_message,
        end_sampling,
    ]
    if full_ids[len(prompt_ids) :] != expected_completion:
        raise InklingContractError("Inkling completion envelope changed")
    labels = [-100] * len(full_ids)
    content_start = len(prompt_ids) + 2
    for position, token_id in enumerate(target_ids, content_start):
        labels[position] = token_id
    labels[-1] = end_sampling
    if labels[-2] != -100:
        raise InklingContractError("Inkling end-message token unexpectedly receives loss")
    return {
        "inputIDs": full_ids,
        "labels": labels,
        "modelInputTokenCount": len(prompt_ids),
        "targetTokenCount": len(target_ids) + 1,
        "targetContentTokenCount": len(target_ids),
        "targetContentTokenIDs": target_ids,
        "eosTokenID": end_sampling,
        "stopTokenIDs": [int(value) for value in renderer.stop()],
    }


def parse_completion(
    *, semantic_input: str, effort: float, token_ids: list[int]
) -> dict[str, Any]:
    chat, tokenizer, renderer, _ = renderer_components()
    _, parser = renderer.render_for_completion(
        _messages(chat, semantic_input, None, effort)
    )
    raw_decoded = tokenizer.decode(token_ids)
    try:
        messages = parser.parse_tokens(token_ids)
    except Exception as error:  # Native parser supplies the useful error type only at runtime.
        return {
            "status": "parse_failed",
            "prediction": "",
            "reasoning": "",
            "rawDecoded": raw_decoded,
            "error": f"{type(error).__name__}: {error}",
        }
    answers: list[str] = []
    reasoning: list[str] = []
    message_audit: list[dict[str, Any]] = []
    for message in messages:
        content = message.content
        item = {
            "author": str(message.author.kind),
            "channel": str(message.channel_enum),
            "contentType": type(content).__name__,
        }
        if isinstance(content, chat.Text):
            item["text"] = content.text
            if (
                message.author.kind == chat.AuthorKind.Model
                and message.channel_enum
                in {chat.MessageChannel.Main, chat.MessageChannel.Final}
            ):
                answers.append(content.text)
        elif isinstance(content, chat.Thinking):
            item["text"] = content.text
            reasoning.append(content.text)
        message_audit.append(item)
    return {
        "status": "parsed",
        "prediction": "".join(answers),
        "reasoning": "".join(reasoning),
        "rawDecoded": raw_decoded,
        "messages": message_audit,
    }

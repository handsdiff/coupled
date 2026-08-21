#!/usr/bin/env python3
"""Local regression checks for Inkling rendering and causal loss alignment."""

from __future__ import annotations

from phase1_inkling import (
    REASONING_CONDITIONS,
    parse_completion,
    render_training_row,
)
from phase1_training_contract import IGNORE_LABEL, TinkerDatumContract
from phase1_tinker_overfit_contract import build_and_validate_sdk_datums
from tml_renderers import chat, tokenizers, v0
from phase1_inkling import flatten_token_spans


def contract(row: dict, example_id: str) -> TinkerDatumContract:
    model_input = row["inputIDs"][:-1]
    targets = row["inputIDs"][1:]
    weights = [
        0.0 if label == IGNORE_LABEL else 1.0 for label in row["labels"][1:]
    ]
    for label, target, weight in zip(row["labels"][1:], targets, weights, strict=True):
        if weight:
            assert label == target
    return TinkerDatumContract(
        example_id=example_id,
        model_input_token_ids=model_input,
        target_tokens=targets,
        weights=weights,
        weighted_positions=sum(value != 0.0 for value in weights),
    )


def main() -> int:
    semantic = (
        "Predict the exact next human WRITE completion.\n"
        '{"kind":"read","content":"source material"}\n'
        '{"kind":"write_query","cursorContext":{"leftContext":"draft "}}'
    )
    target = "please review <|paste|> tomorrow"
    rows = {}
    contracts = []
    for condition, effort in REASONING_CONDITIONS.items():
        row = render_training_row(
            semantic_input=semantic, target=target, effort=effort
        )
        rows[condition] = row
        assert row["targetTokenCount"] == row["targetContentTokenCount"] + 1
        assert row["labels"][-1] == row["eosTokenID"]
        assert row["labels"][-2] == IGNORE_LABEL
        assert all(
            value == IGNORE_LABEL
            for value in row["labels"][: row["modelInputTokenCount"] + 2]
        )
        completion = row["inputIDs"][row["modelInputTokenCount"] :]
        parsed = parse_completion(
            semantic_input=semantic, effort=effort, token_ids=completion
        )
        assert parsed["status"] == "parsed"
        assert parsed["prediction"] == target
        assert not parsed["reasoning"]
        contracts.append(contract(row, condition))
    assert rows["reasoning_off"]["inputIDs"] != rows["reasoning_on"]["inputIDs"]
    assert (
        rows["reasoning_off"]["targetContentTokenIDs"]
        == rows["reasoning_on"]["targetContentTokenIDs"]
    )
    tokenizer = tokenizers.o200k_base_chat()
    renderer = v0.Renderer(tokenizer)
    author = chat.Author
    kind = chat.AuthorKind
    prompt = [
        chat.Message(chat.ThinkingEffort(900), author(kind.System)),
        chat.Message(chat.Text(semantic), author(kind.User)),
    ]
    completed = prompt + [
        chat.Message(chat.Thinking("private reasoning"), author(kind.Model)),
        chat.Message(chat.Text(target), author(kind.Model)),
        chat.Message(chat.ModelEndSampling(), author(kind.Model)),
    ]
    prompt_spans, _ = renderer.render_for_completion(prompt)
    prompt_ids = flatten_token_spans(prompt_spans)
    full_ids = flatten_token_spans(renderer.render_for_sft(completed)[0].input_token_spans)
    parsed_reasoning = parse_completion(
        semantic_input=semantic, effort=0.9, token_ids=full_ids[len(prompt_ids) :]
    )
    assert parsed_reasoning["prediction"] == target
    assert parsed_reasoning["reasoning"] == "private reasoning"
    datums, validations, sdk_version = build_and_validate_sdk_datums(contracts)
    assert len(datums) == len(validations) == 2
    assert sdk_version == "0.25.0"
    print("phase1 Inkling checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

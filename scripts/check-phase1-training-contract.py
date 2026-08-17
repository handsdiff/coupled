#!/usr/bin/env python3
"""Small deterministic checks for the Phase 1 causal-shift adapter."""

from __future__ import annotations

from types import SimpleNamespace

from phase1_training_contract import (
    TrainingContractError,
    adapt_row_to_tinker,
    compare_pack_tokenizers,
)


def row(labels: list[int]) -> dict[str, object]:
    return {
        "exampleID": "synthetic-example",
        "inputIDs": [10, 11, 12, 13],
        "labels": labels,
        "modelInputTokenCount": 2,
        "targetTokenCount": 2,
    }


datum = adapt_row_to_tinker(row([-100, -100, 12, 13]))
assert datum.model_input_token_ids == [10, 11, 12]
assert datum.target_tokens == [11, 12, 13]
assert datum.weights == [0.0, 1.0, 1.0]
assert datum.weighted_positions == 2

try:
    adapt_row_to_tinker(row([-100, -100, 99, 13]))
except TrainingContractError as error:
    assert "causal shift mismatch" in str(error)
else:
    raise AssertionError("adapter accepted a wrong loss-bearing target")

bad_count = row([-100, -100, 12, 13])
bad_count["targetTokenCount"] = 1
try:
    adapt_row_to_tinker(bad_count)
except TrainingContractError as error:
    assert "targetTokenCount" in str(error)
else:
    raise AssertionError("adapter accepted an incorrect target-token count")


class FakeTokenizer:
    def __init__(self, vocabulary: dict[str, int]):
        self.vocabulary = vocabulary
        self.inverse = {token_id: token for token, token_id in vocabulary.items()}
        self.eos_token_id = 3
        self.pad_token_id = 3
        self.vocab_size = len(vocabulary)
        self.name_or_path = "fake"

    def __len__(self) -> int:
        return len(self.vocabulary)

    def get_vocab(self) -> dict[str, int]:
        return self.vocabulary.copy()

    def convert_ids_to_tokens(self, token_id: int) -> str:
        return self.inverse[token_id]

    def encode(self, text: str, add_special_tokens: bool = False) -> list[int]:
        del add_special_tokens
        if text == "<|paste|>":
            return [2]
        return [ord(character) for character in text]

    def decode(self, token_ids: list[int], **_: object) -> str:
        return "".join(self.inverse[token_id] for token_id in token_ids)


fake_dataset = SimpleNamespace(
    rows=[{"exampleID": "fake", "inputIDs": [0, 1, 2, 3]}],
    eos_token_id=3,
    pad_token_id=3,
    paste_marker="<|paste|>",
    paste_marker_token_ids=[2],
)
local = FakeTokenizer({"a": 0, "b": 1, "<|paste|>": 2, "<eos>": 3})
matching = FakeTokenizer({"a": 0, "b": 1, "<|paste|>": 2, "<eos>": 3})
comparison = compare_pack_tokenizers(fake_dataset, local, matching)
assert comparison["compatible"] is True
assert comparison["packTokenIDs"]["uniqueIDs"] == 4

mismatching = FakeTokenizer({"a": 1, "b": 0, "<|paste|>": 2, "<eos>": 3})
comparison = compare_pack_tokenizers(fake_dataset, local, mismatching)
assert comparison["compatible"] is False
assert comparison["completeVocabulary"]["exact"] is False
assert comparison["packTokenIDs"]["mismatchIDs"] == [0, 1]

print("Phase 1 training-contract checks passed")

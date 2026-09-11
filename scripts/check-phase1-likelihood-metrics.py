#!/usr/bin/env python3
"""Fast offline BPB checks; no tokenizer download, provider client, or data writes."""
import copy
import hashlib
import math
from types import SimpleNamespace
import unittest

from phase1_qwen38_execution import (
    BPB_DEFINITION, ContractError, TinkerBridge, aggregate_likelihood,
    evaluation_by_block, nll_result, text_bpb_result,
)


def evaluated(logprobs, text):
    return {**nll_result(logprobs), **text_bpb_result(logprobs, text)}


class LikelihoodChecks(unittest.TestCase):
    def test_ascii_and_unchanged_nll(self):
        logs = [-math.log(2), -2 * math.log(2), -9.]
        original = nll_result(logs)
        result = evaluated(logs, "ab")
        for key, value in original.items():
            self.assertEqual(result[key], value)
        self.assertEqual(result["lossBearingTokens"], 3)
        self.assertEqual(result["targetUTF8Bytes"], 2)
        self.assertAlmostEqual(result["bitsPerByte"], 1.5)

    def test_unicode_bytes_not_characters_or_token_count(self):
        text = "é🙂"
        r = evaluated([-1., -2., -7.], text)
        self.assertEqual(r["targetUTF8Bytes"], 6)
        self.assertAlmostEqual(r["bitsPerByte"], 3 / math.log(2) / 6)
        # Same text likelihood under different token segmentations -> same BPB.
        other = evaluated([-3., -100.], text)
        self.assertAlmostEqual(r["bitsPerByte"], other["bitsPerByte"])
        decomposed = evaluated([-3., -7.], "e\u0301🙂")
        self.assertEqual(decomposed["targetUTF8Bytes"], 7)

    def test_terminator_probability_only_affects_nll(self):
        a = evaluated([-1., -2.], "text")
        b = evaluated([-1., -1000.], "text")
        self.assertEqual(a["bitsPerByte"], b["bitsPerByte"])
        self.assertNotEqual(a["meanNLL"], b["meanNLL"])

    def test_paste_marker_counts_only_its_literal_bytes(self):
        r = evaluated([-1., -2., -3., -4.], "before <|paste|> after")
        self.assertEqual(r["targetUTF8Bytes"], len(b"before <|paste|> after"))
        self.assertEqual(r["textNLLSum"], 6.)
        self.assertEqual(r["bpbVersion"], BPB_DEFINITION["version"])

    def test_aggregate_is_ratio_of_sums(self):
        a = evaluated([-2 * math.log(2), -3.], "a")
        b = evaluated([-3 * math.log(2), -4.], "bbb")
        r = aggregate_likelihood([a, b])
        self.assertEqual(r["examples"], 2)
        self.assertAlmostEqual(r["bitsPerByte"], 5 / 4)
        self.assertNotAlmostEqual(r["bitsPerByte"], (a["bitsPerByte"] + b["bitsPerByte"]) / 2)
        self.assertAlmostEqual(r["meanNLL"], (a["weightedNLLSum"] + b["weightedNLLSum"]) / 4)
        self.assertEqual(aggregate_likelihood([r]), r)

    def test_invalid_or_absent_evidence_fails(self):
        for logs, text in [([], "a"), ([-1.], "a"), ([-1., -1.], ""),
                           ([None, -1.], "a"), ([float("nan"), -1.], "a"),
                           ([float("-inf"), -1.], "a"), ([1., -1.], "a")]:
            with self.assertRaises(ContractError):
                text_bpb_result(logs, text)
        for rows in [[], [nll_result([-1.])]]:
            with self.assertRaises(ContractError):
                aggregate_likelihood(rows)

    def test_block_summary_ignores_training_and_separates_arms(self):
        records = [{"kind": "binding"}, {"kind": "operation_result", "operation": "train"}]
        for pipeline, condition, block in [("old", "frozen", 1), ("old", "frozen", 2),
                                            ("old", "personalized", 2), ("new", "frozen", 1)]:
            records.append({"kind": "operation_result", "operation": "nll",
                            "identity": {"pipeline": pipeline, "condition": condition, "blockOrdinal": block},
                            "value": evaluated([-1., -2.], "abc")})
        summaries = evaluation_by_block(records)
        self.assertEqual(len(summaries), 4)
        self.assertTrue(all(r["examples"] == 1 for r in summaries))

    def bridge_fixture(self):
        row = {"promptTokenIDs": [101, 102], "completionTokenIDs": [10, 11, 248046],
               "targetSHA256": hashlib.sha256("é".encode()).hexdigest()}
        calls = []

        def decode(ids, clean_up_tokenization_spaces):
            self.assertEqual(ids, [10, 11])  # Not separate byte-fragment tokens or EOS.
            self.assertFalse(clean_up_tokenization_spaces)
            return "é"

        def compute(ids):
            calls.append(ids)
            self.assertEqual(ids, [101, 102, 10, 11, 248046])
            return SimpleNamespace(result=lambda: [None, -999., -1., -2., -8.])

        sdk = SimpleNamespace(ModelInput=SimpleNamespace(from_ints=lambda ids: ids))
        bridge = TinkerBridge(None, sdk, SimpleNamespace(decode=decode), None, {})
        return bridge, SimpleNamespace(compute_logprobs=compute), row, calls

    def test_bridge_reuses_one_nll_call_excludes_prompt_and_eos(self):
        bridge, sampler, row, calls = self.bridge_fixture()
        before = copy.deepcopy(row)
        r = bridge.nll(sampler, row)
        self.assertEqual(len(calls), 1)
        self.assertEqual(row, before)
        self.assertEqual(r["weightedNLLSum"], 11.)
        self.assertEqual(r["textNLLSum"], 3.)
        self.assertEqual(r["targetUTF8Bytes"], 2)
        self.assertAlmostEqual(r["bitsPerByte"], 3 / math.log(2) / 2)

    def test_wrong_text_or_terminator_rejected_before_call(self):
        for mutation in [lambda r: r.update(targetSHA256="wrong"),
                         lambda r: r["completionTokenIDs"].pop(),
                         lambda r: r["completionTokenIDs"].insert(0, 248046)]:
            bridge, sampler, row, calls = self.bridge_fixture()
            mutation(row)
            with self.assertRaises(ContractError):
                bridge.nll(sampler, row)
            self.assertFalse(calls)


if __name__ == "__main__":
    unittest.main()

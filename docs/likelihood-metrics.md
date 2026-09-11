# Likelihood evaluation

The active Qwen3.8 evaluator records both metrics before updating on each block:

- **NLL (unchanged):** mean negative natural-log probability per loss-bearing target token, including the final native terminator.
- **BPB:** negative log probability of the target **text**, converted to bits and divided by that text's UTF-8 byte length. Lower is better.

`BPB = textNLLSum / (ln(2) × targetUTF8Bytes)`

BPB excludes the final native terminator from both probability and byte counts. It also excludes history, destination/cursor/clipboard conditioning, renderer envelopes, and padding. The exact text is decoded as a whole and verified against the packed target hash; Unicode is not normalized. Literal `<|paste|>` markers count as target text; resolved clipboard payloads do not.

Each evaluation result retains `targetLogprobs`, the existing NLL fields, and `textNLLSum`, `textBits`, `targetUTF8Bytes`, `bitsPerByte`, and `bpbVersion`. The executor returns token-weighted NLL and byte-weighted BPB by pipeline, chronological block, and frozen/personalized condition. Block 1 has only the frozen warmup measurement. Aggregate any larger interval with `aggregate_likelihood`: sum bits and bytes, **do not average example or block BPBs**.

Frozen BPB minus personalized BPB on the same examples is bits saved per byte. The sum of bits saved across predictions made before their training updates is a prequential learning diagnostic. Compare matched examples within each pipeline; BPB does not remove differences in target difficulty or composition between old/new datasets. Semantic usefulness and real-time usefulness remain separate metrics.

This is local arithmetic on the existing NLL response: no additional provider requests, target changes, tokenizer edits, or optimizer changes. Existing historical results are not rewritten. The frozen execution plan records the metric definition, so regenerate offline execution checks and the plan before live preflight after changing evaluation code.

Offline checks:

```sh
python3 -B scripts/check-phase1-likelihood-metrics.py
```

The full `check-phase1-pipeline-era-execution.py` test also checks BPB on native packed targets and preserves the exact generation/NLL/training request counts. Its `--output` must be a new report path; it never overwrites the original report.

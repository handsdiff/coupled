# Coupled checkpoint — September 11, 2026

Current state only. Earlier work is preserved in the
[archived checkpoint](docs/checkpoints/2026-09-11-before-era-replay.md).

## Objective and experiment

Predict the next closed, substantive thought—not the mechanics of editing it:

`causal READ/WRITE history + destination/cursor/clipboard → authored completion`

Closed WRITEs appear in both history and supervision. Paste payload remains
visible with provenance in history; targets contain literal `<|paste|>` actions.
Only completion and native termination receive loss. Raw evidence is immutable;
reviewed manual reconstruction is allowed and hash-bound for this experiment.

Compare **whole pipelines**, not just READ cleanup:

- **Old:** September 1 commit `f6a2e713c378adc4e2f989b76882d9a14159b526`;
  pane v2, semantic v13, causal v15, raw episodes v8, historical packer v7.
  It retains its own targets and ignores later dynamic visual READ records.
- **New:** `coupled-data/sep02-10-curated-repair-20260911/paired-v11-checked/new`.
  Best reviewed construction, including source-backed manual repairs.
- Same September 2–10 source sessions. Do not force shared targets, counts,
  episode boundaries or histories. `paired-*/old` is a READ-only diagnostic,
  **not** the actual historical baseline.

| Prepared quantity | Old | New |
|---|---:|---:|
| Eligible targets | 664 | 659 |
| Historical WRITEs | 1,138 | 1,141 |
| READs | 5,158 | 6,775 |
| Training examples | 650 | 650 |
| Post-warmup opportunities per model | 614 | 609 |
| Chronological blocks | 14 | 14 |

The old cohort excludes five credential-related targets. Common privacy rules
also apply to context. This is not a guarantee of finding every secret.

## Final cleanup and quality limits

- Joined former new-pack 561–562 into the complete note using four continuous
  raw attempts; removed the self-derived READ that falsely divided it.
- Folded the immediate parenthetical refinement into former target 570 using
  ten continuous raw attempts. No intervening READ or clipboard change.
- Removed two exact, screenshot-confirmed terminal-footer projections,
  including the repeated state's full-context fallback.
- Preserved all 657 other target/query/mask/time contracts, all unrelated
  historical WRITEs and READs, and all 26 golden READ records from the preceding
  cleaned corpus. The historical experiment arm is unchanged.

See [readiness and explicit limitations](docs/DATASET-READINESS.md). Seven earlier
compositions remain history-only. Two inspected neighboring continuations still
lack a safe merge proof. Accepted edge/OCR tradeoffs and coverage gaps remain
documented. There is **no claim of zero dataset error** or universal rules for
all manually repaired cases.

## Frozen preparation locations

Under `coupled-data/sep02-10-pipeline-era-comparison-20260911/`:

- `revision-v2/comparison.json`: independent sources and curation provenance.
- `packs-v3/{old,new}`: native Qwen inputs, targets, blocks and audits.
- `revision-v2/execution-checks.json`: no-network schedule/recovery checks.
- `revision-v2/execution-plan.json`: exact recipe, hashes and cost assumptions.

Earlier `packs-v2`, `comparison.json` and `execution-plan.json` are superseded
preparations, not prior model results. No provider calls or training were made.

All **1,323 native rows** passed prompt reconstruction, masking, one-EOS and
SDK-shift checks. Both arms reproduce byte-identically; the old arm also matches
its preceding pack. The no-network executor passed 2,446 generations, 2,546 NLL
calls and 1,300 updates, including interrupted training/scoring/checkpoint saves.
All eleven recent composed WRITEs appear intact in the first later native input.
BPB is recorded from existing NLL results without extra provider calls; see
[its exact definition](docs/likelihood-metrics.md).

Plan fingerprint: `0f74496321312a2f37b2038ba9a0e6ee2e306962bf7858cc9a39846f932fea59`.
The September-10-rate uncached/max-output estimate is **$435.09**, excluding
preflight and retries. This is not approved spending or a current price quote.

Recipe: Qwen/Qwen3.8-27B, reasoning off; fresh rank-32 adapter per pipeline,
attention/MLP/unembedding; seed 17, batch one, Adam 2e-4, betas .9/.95,
epsilon 1e-12, weight decay 0, clipping 1. Score each block before training;
train each new block once, warm-start full optimizer state, no final update.
Generation: temperature .6, seed 17, maximum 512 tokens. Shared 32K reference
budget, native Qwen framing and termination; no target truncation.

Report holistic usefulness, cost and latency with each arm's denominator.
Frozen/personalized likelihood is comparable within an arm; old/new absolute
NLL is not a paired statistic when targets differ. First-block generations do
not count in the post-training comparison.

## Before execution

1. Final review of targets, retained limitations, audits and immutable commit.
2. Confirm current provider pricing; authorize a bounded live preflight.
3. Inspect preflight outputs, then approve the full-run budget and launch.

**STOP: preparation is not launch authorization.** The live collector was not
stopped, rebuilt or changed by this cleanup. Preserve raw capture and prior
model outputs; prune only explicitly identified reconstructible artifacts.

References: [experiment definition](PIPELINE-COMPARISON.md),
[storage policy](STORAGE.md), [collection guide](COLLECTION_GUIDE.md), and
`~/Vaults/Notes/Thesis.md`, `Data.md`, `Phase 1.md`.

# Qwen3.6 old/new pipeline comparison

September 12, 2026. The user selected **Qwen/Qwen3.6-35B-A3B at `2e-4`**.
This fixes model selection; it does not launch another pilot or authorize the
full-run bill. All previous packs, raw data and model results stay unchanged.

## Question and data

Test useful next-thought prediction and the benefit of personal training, then
compare that benefit under the whole historical pipeline and best-reviewed new
pipeline. Do not force identical targets to turn this into a READ-only ablation.

| Quantity | Old | New |
|---|---:|---:|
| Eligible closed WRITEs | 664 | 655 |
| Examples trained once | 650 | 650 |
| Post-warmup predictions per model | 614 | 605 |
| Blocks | 13 × 50 + 14 | 13 × 50 + 5 |

Each pipeline has a frozen baseline and its own fresh personalized adapter.
The first block supplies warmup training and baseline likelihood only. Score
every subsequent block before learning it. Continue from the current adapter
and Adam state, train only the new block once, and never train the last block.
No pilot checkpoint is reused. Total: 2,438 predictions, 2,538 target-likelihood
calls (including 100 warmup baseline calls), and 1,300 optimizer steps.

The first 100 new-pipeline examples informed model/LR selection. Report later
blocks separately; neither the full trace nor overlapping old examples should
be described as an untouched validation set.

## Complete recipe

| Setting | Value |
|---|---|
| Model | `Qwen/Qwen3.6-35B-A3B` |
| Reasoning | Off; no invented reasoning labels |
| Starting state | Fresh model plus a new adapter per pipeline |
| LoRA | Rank 32; attention, MLP, unembedding |
| Trainable parameters | Cookbook estimate 561,463,296; confirm against provider metadata |
| Initialization / shuffle seed | 17 |
| Within-block order | Existing SHA-256 deterministic order; unchanged |
| Block / batch size | 50 / 1 example |
| Updates | One epoch per new block; no cumulative replay |
| Optimizer | Adam, constant learning rate `2e-4` |
| Betas / epsilon | `0.9 / 0.95` / `1e-12` |
| Weight decay / gradient clipping | `0` / norm `1.0` |
| Loss | Cross-entropy, reduction `none`; prompt weight 0, completion weight 1 |
| Completion | Exact authored text and literal `<\|paste\|>` actions, then one native EOS |
| Native EOS | `248046` (`im_end`), loss-bearing |
| Framing | HF-matched native non-thinking prefix; entirely masked |
| Context | Exact frozen per-pipeline 32K reference-budget semantic inputs |
| Native context limit | 65,536; no target truncation or context refill |
| Sampling | One answer; temperature `0.6`; seed 17; maximum 512 tokens |
| Checkpoints | Separate sampler and full optimizer states after every update block |
| Retention | Seven-day TTL; storage included in proposed budget |

The Qwen3.6 renderer preserves whitespace and is the same native contract used
in the validated pilot. Every row must agree with the HF generation template,
Cookbook supervised sequence and SDK one-token causal shift. The first content
token receives loss; the prompt and empty non-thinking prefix do not. Native
rendering changes neither the underlying context bytes nor the target bytes.

The local tokenizer revision is `995ad96eacd98c81ed38be0c5b274b04031597b0`.
This is **not** verification of the provider's model-weight revision: Tinker
currently exposes the model name. Complete vocabulary/file hashes are retained;
remote compatibility must be reconfirmed before execution.

## Preparation and release gate

Canonical local preparation:
`coupled-data/sep02-10-qwen36-pipeline-comparison-20260912-v2/`.
The preparation fingerprint is
`23e6ae08c6ac8ac45b9cb86a2e2d389d5f026f90dea2dc1b75fdc4936a1df787`.
`v1` is a development preparation, not a launch artifact. All six native-row,
cohort and block artifacts repeat byte-identically; v2 strengthens the runtime
binding. All 100 Qwen3.6 pilot input/target token sequences match the full pack.

Validation completed: all 1,319 real rows passed the independent native audit;
the full 2,438-generation / 2,538-NLL / 1,300-update mock rehearsal passed,
including interrupted training, scoring and checkpoint recovery. Repository
checks passed. There were zero provider calls. These are local mechanical
checks, not evidence of successful full-corpus learning or remote stability.

`scripts/prepare-phase1-qwen36-comparison.py` creates immutable native packs,
checks semantic equality and computes cost from submitted positions. Its audit
rechecks all rows. `scripts/check-phase1-qwen36-comparison.py` uses the real SDK
and exact independent schedules with a fake service to test chronology,
full-optimizer continuation, interrupted training/scoring/checkpoint recovery,
charged uncertainty, no final update and no repeated completed requests.

These commands block network/provider construction. They do not start training.
Before paid launch, freeze a clean launcher revision against the preparation
and audits, reserve checkpoint storage outside the token allowance, retain all
in-flight spend on recovery, and obtain explicit approval of the full cost.
Existing pilot approvals are not silently reused as full-run authorization.

## Evaluation

Primary: intended-thought usefulness, using the established human-calibrated
bar, with substantive successes distinguished from easy approvals/status checks.
Keep every generation, including invalid/empty answers, in its denominator.

Also retain paired frozen/personalized content NLL, full completion NLL, EOS
and first-token breakdowns, BPB, per-block training losses, actual outputs and
token IDs, stop reasons, query timings, usage, cost and optimizer lineage.
Compare learning curves as preceding unique examples increase; do not project
the pilot's percentage improvement as a constant future rate. Absolute old/new
NLL is not a paired statistic because the two targets can differ.

Reuse frontier results only when the actual semantic query/target contract
matches. No new frontier calls are included in this training preparation.

## Pricing basis

On September 12, Tinker lists $0.54/M prefill tokens, $1.335/M sampled tokens,
$1.177/M training tokens and $0.10/GB-month checkpoint storage.
[Official pricing](https://tinker-docs.thinkingmachines.ai/tinker/models/).
The plan assumes uncached input and every answer reaching 512 tokens, uses the
actual shifted training lengths, and includes the SDK's one-token NLL sample.
It separately reserves interrupted work and checkpoint storage. This is a
conservative estimate, not an invoice or permission to spend.

| Projected charge, both pipelines | USD |
|---|---:|
| Training | 44.34 |
| Generation prefill | 38.73 |
| Generation output at 512-token ceiling | 1.67 |
| Target likelihood evaluation | 40.04 |
| Scheduled token total | **124.78** |
| Optional interrupted-work reserve | 3.60 |
| Seven-day checkpoint storage reserve | 12.00 |
| Including reserves | **140.38** |
| Proposed authorization ceiling | **150.00** |

Old-pipeline token estimate: $69.71. New-pipeline token estimate: $55.06.
The actual token totals are 37,675,255 training positions and 33,570
loss-bearing training-token presentations, including 1,300 native EOS tokens.
Context processing is charged even though context is masked from loss.

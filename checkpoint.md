# Coupled checkpoint — September 12, 2026

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
- **New:** `coupled-data/sep02-10-cleanliness-review-20260911/corpus-v1/new`.
  Best reviewed construction, including source-backed manual repairs.
- Same September 2–10 source sessions. Do not force shared targets, counts,
  episode boundaries or histories. `paired-*/old` is a READ-only diagnostic,
  **not** the actual historical baseline.

| Prepared quantity | Old | New |
|---|---:|---:|
| Eligible targets | 664 | 655 |
| Historical WRITEs | 1,138 | 1,134 |
| READs | 5,158 | 6,767 |
| Training examples | 650 | 650 |
| Post-warmup opportunities per model | 614 | 605 |
| Chronological blocks | 14 | 14 |

The old cohort excludes five credential-related targets. Common privacy rules
also apply to context. This is not a guarantee of finding every secret.

## Reviewer-driven cleanup and quality limits

- Screened all 6,775 retained READs; reran accurate local OCR for 3,766 unique
  image/region jobs. Native ordering is evidence, not unconditional authority:
  table/column relationships stay fixed, and new recognition strings are not
  automatically adopted. Unproven correspondences remain explicit.
- Repaired seven compositions, including the reviewer's 474–475 false split
  and six additional continuations/revisions. Eleven targets and three
  history-only fragments become seven complete targets; eight self-derived
  boundary READs are removed. Initial queries and causal onset remain intact.
- Fourteen screenshot-backed READ adjudications fix wrong-source overlays,
  sidebar/status fragments, specific OCR glyphs and the timeline's row order.
  Exact adjacent overlap is rechecked after correction; dependencies retain
  the full fallback when earlier context is unavailable.
- Against `paired-v11-checked`: 648 unaffected target/query/mask/time contracts,
  1,127 unrelated historical WRITEs and all 26 golden READs are unchanged.
  3,157 retained READ records change; 3,610 remain identical. The actual
  historical experiment arm remains byte-identical.

See [readiness and explicit limitations](docs/DATASET-READINESS.md). Seven earlier
compositions remain history-only. Some discontinuities and composition-boundary
judgments remain unresolved. Accepted edge/OCR tradeoffs and coverage gaps are
not reopened. There is **no claim of zero dataset error**, exhaustive visual
certification, or universal rules for all manually repaired cases.

## Frozen preparation locations

Under `coupled-data/sep02-10-pipeline-era-comparison-20260911/`:

- `revision-v3/comparison.json`: independent sources and curation provenance.
- `packs-v4/{old,new}`: native Qwen inputs, targets and blocks.
- `revision-v3/`: current native audits, execution checks and offline plan.

Earlier `packs-v2`, `packs-v3` and `revision-v2/execution-plan.json` are superseded
preparations, not prior model results. Do not launch the old plan. No provider
calls or training were made during this cleanup.

The corpus audit passes strict causality, reconstruction of the seven joins,
the unaffected-record comparison, and 76 reference-packed canaries per arm.
All 1,319 native SDK rows pass prompt reconstruction, masks, causal shift and
native termination checks. The new pack repeats byte-for-byte; the old pack
matches the prior frozen arm. The no-network executor rehearsal passes 2,438
simulated generations, 2,538 NLL calls and 1,300 updates, including interrupted
training/scoring/checkpoint recovery. These tests do not certify semantic
cleanliness or live GPU behavior. BPB uses existing NLL results without extra
provider calls; see [its exact definition](docs/likelihood-metrics.md).

The previous plan fingerprint and cost estimate are obsolete for this corpus.
The regenerated `revision-v3/execution-plan.json` fingerprint is
`061bffe2cfa1da25325c781a58164435fdbfe6c71704edc7cf7e8f9a59419099`.
Its planning rates are dated September 10, not a fresh quote; pricing and budget
still require separate approval. Preparation is not permission to launch.

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

The September 12 reasoning-off preflight completed under the authorized $20
cap. Its 230 journaled operations all have results; masks, native EOS, returned
loss sums and full optimizer restoration checks passed. Ten-example overfit
stopped at epoch five: mean NLL 3.5423 → 0.01363 and 8/10 exact reproductions.
The frozen 20-case × four-answer substantive probe scored 0/80 in implementer
intent review (not independent adjudication). This is a model-selection warning,
not proof that supervised personalization cannot work.

Results and the response-bound audit are in
`coupled-data/sep02-10-qwen38-comprehensive-preflight-20260912-v3/`.
Dispatched-token upper bound $12.39184 plus $0.25 storage reserve; final dollar
billing is unverified. No full experiment started. The main corpus/packs/recipe
and collector were not changed.

The additionally authorized shared $20 model comparison is now complete in
`coupled-data/sep02-10-qwen-model-preflights-live-20260912-v3/`, using preparation
`sep02-10-qwen-model-preflights-20260912-v4`. `REVIEW.md` contains all frozen
answers, trained memorization outputs, curves, full hyperparameters, costs and
restore caveats; `audit.json` binds responses, grades, sources and implementation.

| Configuration | Intended-thought passes, frozen | Ten-example memorization NLL | Exact at five passes |
|---|---:|---:|---:|
| Qwen3.8-27B, reasoning off | 0/80 | 3.5423 → 0.01363 | 8/10 |
| Qwen3.6-35B-A3B, reasoning off | 3/80 | 3.7604 → 0.05405 | 8/10 |
| Qwen3.5-35B-A3B-Base | 0/80 | 3.8183 → 0.04729 | 8/10 |
| Qwen3.8-27B, reasoning low | 0/80 | Not trained | Not trained |

The twenty substantive cases have five cases per application and four seeds
each. Grades allow paraphrase/useful elaboration but require the intended
thought; they are implementer judgments, not independently adjudicated or a
random-corpus accuracy estimate. Memorization is not future-write learning.

- Native formats, full remote/local vocabulary identity, selected token decode,
  shifted masks, native EOS and returned weighted loss sums pass. Exact server
  model revisions remain unverified. Base EOS is 248044; hybrid EOS is 248046.
- Earlier xhigh reasoning probes remain preserved: three of four exhausted
  8,192 tokens without a final answer. The separate low-effort series completed
  all 80 final answers without length failures. Reasoning-on **training was not
  tested**; no empty or invented human reasoning traces were supervised.
- Base's frozen outputs all hit 512 tokens, generally continuing serialized
  history. All three trained configurations subsequently passed the same
  memorization gate at epoch five, avoiding the additional five planned passes.
- Qwen3.8-off passed both next-update checkpoint-continuation checks exactly.
  Qwen3.6 failed old and new; Qwen3.5 Base passed old and failed new. Sampler
  likelihood agrees before/after restore, but subsequent training diverges.
  Three unchanged-trainer forwards agree exactly. An explicit seeded restore
  did not fix Qwen3.6; a long sampler probe still matched exactly. A same-client
  load control was rejected by Tinker with HTTP 400, not treated as a pass.
  The cause remains unresolved; do not relabel these as passed restore gates.
- All dispatched paid operations are accounted for. Ten prior baseline results
  were reused with hashes, not resampled. Additional-authorization token bound
  is **$15.03494**, including both stopped attempts and all three diagnostics,
  plus $0.50 storage reserve. This is separate from the original $12.39184
  bound and is not invoice-verified billing. No larger experiment was launched.
- The final read-only billing lookup has token buckets for the earlier sessions,
  but not the completed main additional suite/diagnostics. Saved separately in
  `billing-snapshot.json`; missing provider buckets are not zero cost.
- Runs and diagnostics preserve tokens, final answers, raw reasoning, losses,
  timings and checkpoint paths. One-hour disposable checkpoint TTL was planned;
  their saved paths do not imply ongoing server retention. Local process memory
  was approximately 157 MiB when checked; collector/raw evidence were untouched.
- Full repository checks and dedicated no-network recovery checks pass. Some
  older integration tests explicitly skip missing local historical artifacts.
  The completed-run audit and review regenerate byte-for-byte; the chart was
  visually inspected. The continuation failures above remain explicit.

The next bounded diagnostic is a three-branch control on Qwen3.6 old and
Qwen3.5 Base new, the demonstrated failing trajectories. A and B are independent
seed-17 clients trained on the same first two examples; C is an explicitly
seeded client loading A's full optimizer checkpoint. Compare repeated training
forwards and short/long sampler likelihood before and after one identical next
update. Native tokenization, masks, optimizer and data stay unchanged. This
distinguishes fresh-client variability from restore-specific differences; it
does not relax the existing tolerance or authorize the main run. All 98 planned
operations use pre-dispatch journaling and refuse replay. Additional token bound
is $1.72036; the combined bound including previous work and storage is $17.25530
under the same $20 authorization. Preparation and execution are separate; see
`scripts/diagnose-phase1-qwen-client-control.py` and its no-network check.

Next: review the saved answers and failed continuation checks. The tests prove
the formats can train and memorize; they do **not** yet select a model that
predicts future thoughts well or establish an old/new pipeline benefit. A small
unseen-write learning check is a more informative next spending gate than
jumping directly to the full experiment. Any larger run still needs approval.

**STOP: preparation is not launch authorization.** The live collector was not
stopped, rebuilt or changed by this cleanup. Preserve raw capture and prior
model outputs; prune only explicitly identified reconstructible artifacts.

References: [experiment definition](PIPELINE-COMPARISON.md),
[storage policy](STORAGE.md), [collection guide](COLLECTION_GUIDE.md), and
`~/Vaults/Notes/Thesis.md`, `Data.md`, `Phase 1.md`.

# Old versus new data construction

## Question

Did the overall reconstruction and curation work improve learning and useful
next-thought prediction? This is **not just a READ-cleanup ablation**.

## Old means before the heavy reconstruction work

Use Git commit `f6a2e713c378adc4e2f989b76882d9a14159b526` (September 1,
2026, 20:30 EDT), the final commit before September 2:

- AX pane v2; semantic reducer v13; causal compiler v15; raw episodes v8.
- Preserve that pipeline's READs, historical WRITEs, targets, queries,
  boundaries, eligibility, and timestamps. Do not import subsequent repairs.
- This predates the September 2 visual-change monitor and promotion of dynamic
  READs. The old reducer does not consume the newer visual-observation records.

The later `pane-v2 / semantic-v14` arm is **not** this baseline. Nor is
`paired-v9-checked/old`, which uses repaired WRITEs shared with the new arm.
Keep those artifacts as historical READ-only diagnostics, not the primary
whole-pipeline experiment.

## New means the best reviewed dataset we currently have

Use `coupled-data/sep02-10-curated-repair-20260911/paired-v11-checked/new`:
659 eligible targets, 1,141 historical WRITEs, and 6,775 READs.
See [the quality checklist and explicit remaining limits](docs/DATASET-READINESS.md).

Keep all reviewed READ and WRITE repairs, reconstructed closed compositions,
eligibility decisions, and explicit manual OCR corrections. A correction does
not need a universal production rule before training. Preserve the decision,
raw/screenshot lineage, before/after content, and producing code so the work can
be audited and codified later. The comparison preparation inventories existing
adjudications and curation scripts; older proposals are not silently applied.

## What is and is not matched

Both arms process the same preserved September 2–10 work sessions. Keep the
model and optimization recipe consistent. **Do not force equal target text,
example counts, queries, histories, or episode boundaries.** Different amounts
of usable supervision are part of the result. Report training presentations,
target tokens, chronological coverage, cost, latency, and score denominators.

Score free generations using the established holistic intended-thought bar.
Audit target-construction errors separately; do not automatically mark a model
wrong for disagreeing with an erroneous old label. Within each arm, frozen
versus personalized NLL remains useful. Absolute old/new NLL is not a paired
same-target comparison when the targets differ. A matched subset may be shown
secondarily, but must not replace the full comparison.

Replaying old code on newer raw logs is not an exact counterfactual recording
with the old collector. Explicitly report ignored newer record types and any
compatibility adaptations. Never silently import new semantic logic to make
historical replay succeed.

## Current preparation status

`scripts/prepare-phase1-pipeline-era-comparison.py` freezes the old source from
Git, binds the cleaned new artifact and raw-session hashes, inventories manual
corrections, and emits a non-executable comparison manifest. It does not build
the old corpus, pack tokens, start training, or contact providers.

The independent historical replay is complete and passed its original corpus
audit. It produced 669 targets, 1,138 historical WRITEs and 5,158 READs. The common
credential-safety filter excludes five old targets, yielding **664 old versus
659 new**. No new WRITE repairs or manual adjudications enter the old producer.

Native Qwen3.8 packs are at `packs-v3/{old,new}` in the comparison directory.
Both completed exhaustive HF/Cookbook/SDK token and loss-mask validation.
Independent audits and byte-identical repeated data artifacts passed. The
actual two schedules passed the no-network executor test: 2,446 generated-answer
operations, 2,546 NLL operations and 1,300 optimizer steps were simulated, with
interruption, full-optimizer resume, duplicate-request and budget guards.
This proves the local execution contract, not remote GPU behavior.

| Planned quantity | Old | New |
|---|---:|---:|
| Eligible examples | 664 | 659 |
| Training examples | 650 | 650 |
| Post-warmup scoring opportunities per model | 614 | 609 |
| Chronological blocks | 14 | 14 |

Exact token counts, native prompt lengths and projected spending are recorded
in each packing audit and `revision-v2/execution-plan.json`.

Each arm has its own blocks and starts from a fresh adapter. Only newly scored
blocks are trained once, with full optimizer-state continuation; no final-block
training. Equal training counts arise from block rounding, not target matching.
The common 32K reference-token **budget** is not a guarantee of equal actual
prompt lengths. Preserve each pipeline's packing/rendering behavior and report
that resulting cost/exposure difference rather than silently refilling context.

The older expanded-history code required an exact metadata index with deferred
history reads to avoid repeatedly scanning gigabytes per candidate. That adapter
preserves every decoded record and leaves semantic source code unchanged. The
replay binds both wrapper versions and retains the interrupted IO-only attempt.
Packing treats known-at-onset session-gap metadata separately from observations:
READ/WRITE availability is strictly before the target; a gap marker may end
exactly at onset. No timestamps are moved and no content is repaired by packing.

The earlier shared-target execution plan and price estimate are superseded.
Execution still requires a new reviewed plan, immutable implementation and paid
preflight/launch approval. No provider calls have been made in this preparation.
Existing raw data and model results stay unchanged.

The final local preparation is bound by `revision-v2/execution-plan.json`.
Its canonical fingerprint is `0f74496321312a2f37b2038ba9a0e6ee2e306962bf7858cc9a39846f932fea59`;
the uncached/max-output estimate is **$435.09** before preflight/retries.
It supersedes the earlier `ecaf3688e500…` plan, not any completed model results.
The estimate uses the reviewed September 10 rates, excluding live preflight and
retries. Pricing needs reconfirmation before approval. This is an estimate, not
spending or permission to spend; the plan remains **NOT AUTHORIZED**.

Five newly generated expanded intermediate example files were removed after
validation (17.156 GiB); `intermediate-cleanup-plan.json` lists the exact files.
They are not native-pack/execution inputs. Their hashes and deletion outcomes
are journaled; final historical episodes, reduction evidence, raw data and
native packs remain. Two superseded final-cleanup preview example files were
also deleted (1.778 GiB), with their exact file hashes and outcomes retained in
`revision-v2/cleanup-journal.jsonl`. Neither is a final execution input.
Regeneration uses the recorded historical producer and
stage commands; the pruned intermediate directories are not complete corpora.

The prepared definition is
`coupled-data/sep02-10-pipeline-era-comparison-20260911/revision-v2/comparison.json`.
Its exported historical producer has 140 files independently checked byte-for-
byte against Git. The versioned manual-correction inventory binds the reviewed
evidence/code and the three newly constructed new-arm artifacts. Contract
tests reject shared-target enforcement, importing new repairs into the old arm,
and substituting the later semantic-v14 baseline.

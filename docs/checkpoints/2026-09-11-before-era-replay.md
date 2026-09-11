# Coupled checkpoint — 2026-09-06

This is a snapshot of the current project state, not a changelog. Superseded
designs, failed runs, and intermediate results belong in Git history:

```sh
git log --follow -- checkpoint.md
git show <commit>:checkpoint.md
```

Future updates should replace stale facts here instead of appending history.

## Objective and phase boundary

Phase 1 tests whether ordinary personal computer activity contains enough
signal to predict the content of the user's next closed, substantive write:

```text
causally available READ/WRITE history
+ observed destination, semantic cursor context, and clipboard state
→ next authored composition
```

The target is content, not application, field, cursor offset, edit operation,
removed text, or event metadata. Those are conditioning or audit evidence. The
semantic ontology remains READ and WRITE; clipboard state is not a third event.

The current experiment is offline and uses destination/cursor/clipboard state
captured at target onset. Sample-time routing and live suggestions remain Phase
2. Phase 1 results must not imply that routing is already solved.

## Active baseline

**Experiment scope corrected September 11:** compare the complete pre-heavy-
reconstruction pipeline against maximum-effort cleaned construction, not READs
alone. See [PIPELINE-COMPARISON.md](PIPELINE-COMPARISON.md). The old producer is
the final pre-September-2 commit `f6a2e713c378adc4e2f989b76882d9a14159b526`:
pane v2, semantic v13, causal v15, episodes v8. It predates dynamic visual READ
promotion. Do not add new WRITE repairs, new targets, or new READs to this arm.
The old and new targets, counts, histories, queries, and episode boundaries may
differ. The old corpus must be replayed independently; it is not yet built.

The selected **cleaned new arm** is
`coupled-data/sep02-10-curated-repair-20260911/paired-v9-checked/new`: **660
targets and 1,143 historical WRITEs**. Its evidence-bound manual repairs are
valid experimental construction even before universal rules exist. Preserve all
adjudications and source provenance for later codification. See `BATCH-REVIEW.md`
and `REPORT.md` in that artifact's parent. The sibling `old/` is a repaired-WRITE,
old-READ hybrid: retain it as a READ-only diagnostic, not the primary baseline.
The previous paired training plan/costs are superseded for the primary comparison.
Historical-source replay, native packing, revised schedules/costs and execution
approval remain outstanding. No training is authorized by this checkpoint.
The frozen comparison definition and unchanged historical source export are at
`coupled-data/sep02-10-pipeline-era-comparison-20260911/`; 140 producer files
match the pre-September-2 commit, and 72 curation evidence/code files are
fingerprinted. This preparation does not claim the independent old replay ran.

Sep 11 READ cleanup is a separate offline candidate (`phase1-semantic-v26`),
not a training freeze. It disables the cross-application/session-wide text
blacklist: frequently repeated research notes or source code are not thereby
UI controls. Known controls, peripheral rules and adjacent same-pane repeat
handling remain. Reducers through v25 explicitly retain the old rule for
reproduction; the running collector and raw evidence are unchanged.

The offline Vision helper now preserves native observation order instead of
the fixed-0.02 vertical-tolerance sorter. Legacy sorted caches cannot silently
enter new native-order builds. The 192-frame/384-region diagnostic is at
`coupled-data/native-order-check-20260911-r4/`: 196 order-only changes, 144
identical results, 44 recognition differences. The materialized candidate
adopts only full original-line permutations, plus one explicitly screenshot-
verified URL correction in case 245. Other fresh recognition differences are
not adopted. This changes 156 of 11,579 pane records; unreviewed frames are
unchanged. This is **not** a claim that all historical OCR has been rerun.

The larger `ocr-box-row-order-v1` replacement below remains a superseded shadow
ordering experiment, not the chosen production fix. Its separate pane-edge
recovery work also remains unpromoted. The immediate priority is a clean,
evidence-backed dataset; a correction need not become a universal heuristic.
Do not treat the earlier 54/687 flagged-target count as an overall context-error
rate: the newly confirmed session blacklist affected additional historical
READs and their downstream contexts.

The completed READ-only replay is at `clean-read-v26-native-20260911`, with
Sep 4 replaced by `clean-read-v26-adjudicated-20260911` for the verified URL.
`clean-read-v26-audit-20260911-r2` verifies all 1,633 WRITE events are unchanged,
all 1,412 causal micro-examples satisfy their cutoffs and target-mask/EOS
contract, and no session-blacklist removals remain. The motivating Obsidian
READ now retains 1,932 characters instead of only a stray character and word
counts. Native ordering also lets the existing active-WRITE authorship guard
correctly exclude one formerly contaminated READ. Two sensor-group READ IDs
change, so contexts were rebuilt from the new stream, not patched by old IDs.

`coupled-data/clean-read-closed-write-review-20260911-r2/` contains 687 closed-
WRITE examples with repaired READ histories, unchanged targets/queries/onsets/
masks, the existing privacy policy, and shared-context storage. This is a
**review artifact, not a training-frozen or token-packed dataset**: historical
native OCR outside the reviewed frames, pending WRITE/prefix/episode repairs,
and final packing still remain. The earlier pane-edge candidate was not
silently promoted. Peak replay/compiler RSS was approximately 2.7 GiB; the
legacy v25 replay matched its original events and dispositions byte-for-byte.
Native evidence/decisions/manifests also reproduced byte-for-byte. Repository
checks pass; historical integrations whose derived artifacts were deleted
remain explicitly skipped. No provider calls, paid training, raw edits, or
collector replacement occurred.

The Sep 10 `phase1-raw-episode-v10` READ-boundary work is an offline review
candidate, **not a promoted training baseline**. It compares otherwise-joinable
compositions against earlier raw/full-pane observations and the pre-mutation
editable state; unexplained remnants remain blocking and target-ineligible.
Navigation alone no longer closes an unsubmitted prompt. Existing structural
partitions, collector behavior, and frozen experiment artifacts are unchanged.
The replay and boundary-by-boundary review live in
`coupled-data/sep02-10-episode-boundary-v10-review-20260910/`.
Do not interpret passing mechanical checks as resolving all 35 audited splits.
The `v10-2` replay joins nine of those boundaries; 26 remain blocked. Its
655 pre-experiment-filter examples pass corpus/causal/source-lineage audits,
and candidate/decision replay is byte-identical (peak replay RSS 2.2 GiB).
This count is not directly the earlier 687-example filtered frontier cohort.
The earlier retain judgment for cases 360–361 also needs correction: the
purported new reply is visible in a pre-onset screenshot. Details and before/
after targets are in that review directory's `README.md` and
`v10-2-comparison.md`. None of this promotes v10 for training.

The active code baseline is the commit containing this checkpoint. The
causal-v16 READ-source projection described below is the validated baseline.

| Layer | Current contract |
| --- | --- |
| Raw READ / WRITE | schema 7 / schema 15 |
| READ surface | `ax-pane-read-v2` |
| Semantic reduction | `phase1-semantic-v14` |
| Causal micro-events | `phase1-causal-v16` |
| Episode construction | `phase1-raw-episode-v9` |
| Episode projection | `phase1-raw-episode-causal-v9` |
| Packing | `phase1-token-pack-v7` |
| Human/model timing | `phase1-human-timing-v2` |

```text
raw session + screenshots
→ immutable READ-surface evidence
→ semantic READ/WRITE events
→ causal micro-events and normalized READ sources / WRITE destinations
→ gap-aware multi-session corpus
→ closed composition episodes
→ causal examples
→ model-specific packing and loss masks
```

`raw.jsonl`, `session.json`, and retained screenshots are authoritative.
`events.preview.jsonl` and the live viewer are debugging aids only.

Semantic v14 adds visual-change READ evidence to the v13 pointer/activity path.
The bounded SCStream monitor promotes only a settled
visual-change frame or a causally safe frame immediately preceding a WRITE. It
persists that exact frame before running OCR, and the reducer verifies the
screenshot digest plus frame→OCR raw lineage. Full-window OCR remains raw
evidence: v14 emits a model-facing READ only after `ax-pane-read-v2` re-OCRs
the same image around a separately retained semantic content anchor. Frames
whose change interval overlaps an active WRITE remain suppressed raw evidence.
Exact near-simultaneous pointer/visual pane observations are emitted once. An
uninterrupted run of autonomous visual changes on the same surface emits only
its final visible viewport; every intermediate frame remains immutable raw
evidence. A WRITE or ordinary user-triggered READ ends the run. The semantic
projection therefore does not synthesize a full response from transient text
that was never visible at once.

The current review candidate is `ax-pane-read-v7` plus
`phase1-semantic-v25`; neither is promoted merely by generating this artifact.
Pane v6 resolves each observation in capture-time order from either a
trustworthy current AX pane or the last compatible pane proven in the same
application process and window. Current browser document surfaces and repeated
bounded content panes can establish a new anchor. Application chrome can reuse
that anchor, but a failed content probe cannot reuse it merely because the
window ID matches. Pane memory never crosses applications, processes, or
windows. If neither current nor compatible prior AX evidence is available, the
observation remains explicit unresolved evidence and produces no semantic READ;
the old pointer crop and full-window OCR never become silent authority.

Pane v7 adds one deliberately narrow canonicalization before OCR: when
near-simultaneous screen and visual sensors probe the same point in the same
window, and Chromium exposes the latter probe as a strictly nested
`AXContentList` whose ancestry independently preserves the established outer
pane, both observations reuse that outer pane and pane identity. The exact
nested selection remains audit evidence. Distinct editors, text fields, web
areas, main landmarks, document articles, generic nested groups, later
interactions, different pointers, and different windows are not canonicalized.
This fixes one physical state being represented as two READ surfaces without
turning pane memory into a broad application heuristic.

Pane-v6/v7 OCR retains the complete selected pane and a separate 20% vertical-inset
comparison observation. The v25 review pipeline removes interface scaffolding,
reconciles pointer/visual observations, selects corroborated interior text for
scrolls, records adjacent order-preserving novelty, and consolidates passive
dynamic observations to the last visible viewport. Raw pane OCR remains intact.
Material interactions and pre-WRITE checkpoints are intended consolidation
boundaries. Packer v12 renders newly exposed text only when its required
predecessor survives context packing; otherwise it restores the complete semantic
state. The full-session audit below records where these rules still fail, so
passing mechanical tests does not promote them to the canonical pipeline.

The focused `phase1-visual-read-promotion-canary-1` is complete and immutable.
It retained 28 promoted frames: 14 causally eligible frames each produced one
full-window raw OCR observation, while 14 frames whose visual-change interval
overlapped an active WRITE remained raw-only. Native `ax-pane-read-v2` replay
produced pane evidence for all 14 visual observations; semantic v14 emitted 14
visual READs. The Obsidian frame captured during the sentinel's original active
WRITE interval remained raw-only; a later causally valid pre-WRITE frame, after
that WRITE had completed and immediately before its deletion, did contain the
then-visible sentinel. Moving the pointer over Chrome's toolbar retained the
semantic content anchor and emitted Gemini response text rather than toolbar
text. The canary also exposed stale
window-title metadata when Chromium reused a CGWindowID across a tab change;
the monitor now refreshes that window's title and bounds at each completed
frame. The correction and v14 behavior were subsequently observed in a
packaged ordinary-work build and replayed through closed episodes and 32K
packing.

## Active data contract

Collection covers Chrome, Arc, Codex/ChatGPT, Obsidian, and VS Code, including
its integrated terminal. The current settings are one-second READ settlement,
three-second WRITE settlement, zero fixed crop, retained full-window PNGs, 512
characters of cursor context per side, and schema-7 Accessibility ancestry.

READ raw evidence contains the settled window rectangle, Apple Vision OCR,
interaction timing and point, and a bounded metadata-only AX ancestor chain.
The screenshot is screen-coordinate based, so overlapping windows can
contaminate it. Raw READs measure visible interaction surfaces, not gaze or
comprehension.

WRITE raw evidence begins with a synchronous pre-mutation editable snapshot and
retains checkpoints, terminal state, selection, cursor context, destination,
input timing, clipboard/paste evidence, submissions, and sensor failures. The
canonical edit is the minimum contiguous BEFORE→AFTER transition. Cursor
coordinates never bias the diff, there is no synthetic deletion fallback, and
ambiguous evidence remains unresolved rather than inferred from keystrokes.

The active pane-v2/semantic-v14 baseline selects an AX-defined READ panel from
the retained full-window evidence. The pane-v7/semantic-v25 review candidate
applies the stricter stateful pane policy above before any semantic
deduplication. Causal v16 derives compact logical WRITE destinations and READ
sources while preserving the original evidence. Proven VS Code terminal focus
no longer inherits a background editor filename. A schema-7 READ names a main
surface or active pane only from its AX selection; a subpane does not inherit an
outer window title that may describe a background document. OCR content and
surrounding events are not used to guess identity. Pre-schema-7 READ
serialization remains unchanged. Shell-versus-agent WRITE identity remains
`unknown` unless direct evidence or an explicit manifest-pinned local mapping
proves it.

Episode v9 groups faithful micro-WRITEs into closed compositions. Duplicate or
unchanged READs do not split an episode; a novel causal READ, outside WRITE,
submission, changed composition region, destination change, or unresolved state
discontinuity can. Volatile raw AX identities are audit evidence, not sufficient
boundaries by themselves. Self-authored pane text is compared using normalized
model-facing application identity, so `Code` versus `Visual Studio Code` cannot
turn an outgoing partial composition into novel inbound evidence. Every
micro-WRITE receives an explicit disposition; production construction has no
manual adjudication input.

Loss eligibility is narrower than event validity:

- persistent-editor episodes require 40 trimmed authored characters and six
  authored words;
- proven submitted prompts require four trimmed authored characters;
- onset, reconstruction, and structural closure must be proven;
- pure paste, pure deletion, mechanical navigation/URLs, unresolved
  authorship, and open fragments remain history-only.

Only events available strictly before target onset enter history. Append order
is never causal authority. The loss-bearing sequence is authored spans, one
literal `<|paste|>` string per proven paste, and one native EOS. Pasted payload,
history, query, metadata, and padding receive no loss. `<|paste|>` uses the
unchanged tokenizer vocabulary. Targets are never truncated; older complete
history events are removed first so the query stays intact at the right edge.

## Timing and usefulness contract

Two evaluations remain separate:

- **Semantic holy shit:** would the completion itself have been a genuinely
  valuable prediction of the substantive write?
- **Real-time holy shit:** did a semantic pass arrive early enough to review and
  accept while still saving more than one second?

The human opportunity clock is not the model serving clock. Human time starts
immediately after the latest observable material action because reading,
deciding, and physically expressing the next thought are all time a suggestion
could save. The three-second delay is only a provisional inference debounce; it
is not a claim that thinking begins three seconds later.

- A canonical READ starts from raw `lastActivityAt`; its content becomes
  model-visible at OCR availability.
- A prior closed WRITE starts from its final contributing mutation, extended
  through confirmed submission when applicable.
- A new canonical READ or completed prior WRITE resets the human boundary.
- Focus, pointer movement, routing, duplicate/suppressed OCR, and the target's
  own mutations do not reset it.
- `humanStartAt` is the latest material boundary.
- `humanFinishedAt` is the target's final contributing mutation.
- `modelSampleAt = humanStartAt + 3 seconds`.
- `modelStartAt = max(modelSampleAt, requiredContextAvailableAt)`.

Starting human time at `modelSampleAt` would erase three seconds of potential
reading/thinking time and confuse a serving throttle with cognition. Long gaps
remain visible but uncertain because they may include away time. Because saved
generations use target-onset conditioning, current timing is a retrospective
latency projection, not proof that a live router knew the destination then.

## Verified state

- The promoted READ selector was manually preferred on all 19 reviewed
  schema-7 observations across VS Code, ChatGPT, Obsidian, and Chrome.
- `phase1-ordinary-work-2026-09-01-4` is finalized through READ evidence,
  semantic v13, and causal v16: 1,018 raw records; 274 READs; 61 WRITEs; 52
  unresolved/non-event dispositions; 335 causal events; 51 micro-event targets;
  10 target exclusions; zero context exclusions or rejections. Its READ-source
  split is 204 proper AX-pane selections, 69 pointer fallbacks, and one
  unresolved surface.
- The 11-session causal-v16 replay contains 4,711 legacy READs, 204 proper
  schema-7 AX selections, 69 schema-7 pointer fallbacks, and one unresolved
  schema-7 READ. It preserves all 1,255 micro targets. Episode v8 remains at
  1,103 closed episodes and 606 loss-bearing examples; target strings, masks,
  metadata, WRITE serialization, episode membership, and eligibility are
  unchanged. These cohorts remain explicit because schema-7 capability does
  not imply successful AX-pane selection.
- `phase1-ordinary-work-2026-09-01-5` is finalized as a separate compatible
  shard through `ax-pane-read-v2`, semantic v13, causal v16, and episode v8:
  3,100 raw records; 837 READs; 167 WRITEs; 1,004 causal events; 149 eligible
  micro targets; 140 closed episodes; and 69 loss-bearing closed-composition
  targets. Its READ-source split is 596 proper AX-pane selections, 238 pointer
  fallbacks, and three unresolved surfaces. The closed-episode audits pass.
- Together, the existing 11-session corpus and the September 1 session contain
  1,243 closed episodes and 675 loss-bearing closed-composition targets. They
  remain separate immutable shards for now: reconstructing one monolithic
  corpus would duplicate many gigabytes of cumulative model-input text and is
  unnecessary for semantic validation. A later packing/experiment step must
  consume both shards in chronological order with an explicit coverage gap.
- A point-in-time clone of the first 58 minutes of
  `phase1-ordinary-work-2026-09-02-1` contained 2,178 raw records and 605
  replayable pane observations. Relative to unconsolidated v14, final-viewport
  consolidation reduced semantic READs from 583 to 468 and visual READs from
  318 to 203. The 31 closed loss-bearing episodes, their memberships,
  conditioning states, targets, queries, and masks remained identical. At 32K,
  dropped context-event instances fell from 4,532 to 3,570 and discarded input
  tokens from 902,002 to 712,356. Seventy-five ChatGPT visual READs remained,
  including responses captured without pointer activity. Relative to v13, two
  prior closed targets split where genuinely new assistant output became
  causally available mid-composition; the normalized self-authorship fix kept
  `ensure you keep memory usage within reason` as one episode. All closed-
  episode and packing audits passed.
- The immutable clone through `2026-09-02T18:20:34.197Z` covers roughly 95
  minutes and contains 3,027 raw records. Pane v7 reviewed all 857 eligible
  screen/visual observations: 841 produced hash-bound full-pane and
  comparison-only OCR evidence, while 16 remained explicit unresolved
  dispositions. None used the old v1 crop as authoritative content. Of the 841
  resolved observations, 45 safely reused an earlier pane from the same
  application process and window. Three near-simultaneous nested
  `AXContentList` observations reused the proven outer pane; current AX evidence
  resolved the remainder. Two exact evidence builds were byte-identical.
- Semantic-v18 replay over that pane-v7 evidence produced 575 READs, 87 WRITEs,
  and 371 non-event dispositions. Two independent replays were byte-identical
  (`events.jsonl` SHA-256
  `0d749f6536ecfd438cfbcb252418c9555b375cc795ba0c580661783b5040fb94`).
  Relative to pane v6, exactly three nested same-state ChatGPT READs were
  absorbed into their immediately preceding outer-pane READs; every WRITE and
  all other READ content remained unchanged. Causal v16 retained all 662
  events, yielded 77 eligible micro-WRITE examples
  and 10 target exclusions, and had zero context exclusions or rejections.
  The 32K Qwen pack contains the same 77 examples and five grounded paste
  actions. Example identities, targets, masks, conditioning queries, and every
  loss-bearing token sequence are unchanged from pane v6. Pane, causal,
  packing, and repository-wide audits pass.
- Semantic-v19 is the subsequent clipped-OCR candidate over the same immutable
  September 2 evidence. It removes a line only when exact pane-edge geometry
  plus low Vision confidence proves clipping, or when reconciled same-state
  screen and visual observations disagree on an abnormally short peripheral
  line. Its independent 20-percent interior OCR is comparison evidence rather
  than authoritative content; it can suppress false novelty when the full-pane
  OCR alone claims an interior change. Two clean full replays were byte-for-byte
  identical (`events.jsonl` SHA-256
  `1504c22ba45486b1412fb3e93b1e35c8e95f8e73afa5c91475d19e5249fafe26`).
  Event identities and counts remain 575 READs and 87 WRITEs. Thirty-one READ
  complete states changed, while every WRITE, all 77 target strings, masks,
  destination/cursor queries, and target token sequences remained unchanged.
  The causal-v16 compile and dependency-aware 32K Qwen pack pass with five
  grounded paste actions and zero context exclusions or rejections. This fixes
  the reviewed clipped-gibberish cases without incorporating September 3. The
  separate event-531 cross-window screenshot attribution case is deliberately
  unchanged and remains an explicit deferred limitation.
- Semantic-v20 corrects the remaining v19 grounding and packing failures without
  changing collection evidence. Comparison-only OCR may contribute novelty only
  when the exact normalized text is present in the authoritative full-pane OCR;
  otherwise the reducer keeps the authoritative observation or records an
  explicit uncertain disposition. Cross-sensor clipping now also requires strong
  geometric or confidence evidence, which preserves the valid CoinGecko label
  `20. Jul` while continuing to remove the reviewed clipped OCR. Two full replays
  were byte-for-byte identical: `events.jsonl` SHA-256
  `3679ecc514d7316ccd145a40047d512138ab7de09ac057df27c2f13e743228c7`,
  `unresolved.jsonl` SHA-256
  `876780e3da1ce91df76cc9fb37f365323261d3ffc35acfb8221ab44159f76387`,
  and `reduction.json` SHA-256
  `9d15d7bb08566a5395c9a272614f25b07ac020af7e687c903a6209869bc2d003`.
  Counts remain 575 READs, 87 WRITEs, and 371 non-event dispositions. Every
  WRITE, all 77 target strings, masks, queries, and loss-bearing token sequences
  remain unchanged. Packer v9 explicitly consumes all v20 novelty decisions
  instead of falling back to complete states: its 77-example 32K pack contains
  1,690,731 model-input tokens, removes 543,841 proven repeated READ tokens, and
  is 6,115 tokens smaller than the semantic-v18/packer-v8 baseline. The reviewed
  event-622 garble is absent from model input, and event 386 retains `20. Jul`.
- Semantic-v21 is the high-precision model-facing READ candidate over the same
  immutable September 2 evidence. It preserves every v20 complete semantic
  READ and WRITE, but no longer serializes v20's disconnected token patchwork.
  Exact/equivalent adjacent states render empty; one proven contiguous change
  renders that authoritative span; a substantial state change without a safe
  overlap retains the complete cleaned pane; and a difference with at least
  two-thirds proven repetition but no coherent novel region renders empty.
  Comparison/inset OCR may corroborate or veto a proposed peripheral span but
  can never originate model-facing words. If an exact alignment is rejected,
  the reducer tries one ordered OCR-tolerant contiguous line block and then
  falls back to the full state unless substantial repetition is independently
  proven. This prevents a one-character OCR error at a scroll boundary from
  silently deleting a large new section.
- Two semantic-v21 full replays were byte-for-byte identical:
  `events.jsonl` SHA-256
  `24b3bfa3b95781f1f9d22a59a93eb54617525197985b957ae81284e6b0d1d02b`,
  `unresolved.jsonl` SHA-256
  `876780e3da1ce91df76cc9fb37f365323261d3ffc35acfb8221ab44159f76387`,
  and `reduction.json` SHA-256
  `a6d085c41ca73f1fe531bb6f8c26805e29573b4f9bcd6c383527881ef3ffd332`.
  Counts remain 575 READs, 87 WRITEs, and 371 non-event dispositions. The READ
  projection contains 38 contiguous novel spans, 402 complete states, 82
  high-overlap ambiguous suppressions, and 53 equivalent/no-content
  suppressions. Every WRITE object and all 77 causal target strings,
  conditioning queries, and masks remain unchanged.
- Packer v10 is bound to semantic-v21 and rejects older reducer artifacts. Its
  audited 77-example Qwen 32K pack contains five grounded paste actions,
  1,702,199 model-input tokens, and removes 532,373 repeated READ tokens.
  Known patchwork/repeated cases at semantic event lines 10, 81, 238, 351,
  364, 366, 395, 457, 470, 577, and 639 render empty; the coherent additions at
  lines 442 and 469 remain. The large Obsidian scroll transition at line 409 is
  conservatively retained after its exact overlap proposal fails grounding.
  The separate line-531 cross-window screenshot attribution case remains the
  explicitly deferred limitation. The 16 unresolved pane observations remain
  visible separately rather than being hidden.
- The localhost semantic READ review now consumes the same v21 novelty and v3
  model-rendering decision maps as packing. In particular, all 82 ambiguous
  high-overlap suppressions display as no new READ text, and all 38 contiguous
  novel spans display and filter as changed projections; this UI correction
  does not modify any reducer, causal, or packed artifact.
- Semantic-v22 is the conservative correction to v21's destructive ambiguity
  fallback. Proven equivalent adjacent states still render empty, one verified
  contiguous authoritative change still renders alone, and every other
  difference now retains the complete cleaned pane. The approximate reflow
  matcher remains in the event only as audit evidence; it can no longer erase a
  READ based on repeated fraction. Semantic-v21 remains reproducible.
- Two full semantic-v22 replays were byte-for-byte identical:
  `events.jsonl` SHA-256
  `7d9f5af284856233fa9242ff579cd3dfa450453ad3759112e106d199b2aeed13`,
  `unresolved.jsonl` SHA-256
  `876780e3da1ce91df76cc9fb37f365323261d3ffc35acfb8221ab44159f76387`,
  and `reduction.json` SHA-256
  `f91c2d48a53f1bb82deb48650699c19eb32ce3f617d81e9b40affb2ed78a779d`.
  Counts remain 575 READs, 87 WRITEs, and 371 non-event dispositions. The v22
  READ projection contains 38 contiguous novel spans, 484 complete states, and
  53 proven equivalent/no-content suppressions. It contains no ambiguous
  whole-state suppressions.
- The six reviewed lost-content lines 97, 116, 248, 305, 351, and 430 now retain
  their complete cleaned states. Lines 98 and 236 remain proven empty repeats.
  Lines 10, 142, 364, 366, 397, 457, 470, 577, 622, and 639 intentionally become
  redundant full states because the current pipeline cannot prove them empty
  without the removed heuristic. Lines 386, 409, 424, 442, and 469 remain
  intact. The deferred cross-window and tiny-status cases remain unchanged.
- Every semantic WRITE is byte-identical to v21. Causal-v16 over v22 remains 77
  examples; its targets, conditioning queries, and masks are byte-identical to
  v21 (`examples.jsonl` SHA-256
  `b0cbe9bb1690c317928660b71416e25c071fb66207f5da95a281dfcabd224f4d`).
  Packer v11 binds the changed v22 contract without changing rendering logic.
  Its deterministic packed examples SHA-256 is
  `996149be4aabfdc9824424bf6843f9dedd462031b79e5878828dfde6e35dc206`;
  the pack contains 1,962,024 model-input tokens and removes 272,548 proven
  repeated READ tokens. Relative to v21/v10, conservative preservation adds
  259,825 model-input tokens. This is intentional redundancy rather than silent
  causal information loss.
- Semantic-v23 is the sequence-aware READ shadow over the same immutable
  September 2 evidence. Explicit scrolls first project authoritative pane OCR
  through the stable 20-percent top/bottom interior and then emit one coherent
  newly exposed edge. Exact/reflow-equivalent states emit empty; strongly
  repeated cross-sensor states also emit empty rather than replaying a pane.
  Passive visual progress resolves once to its final observed viewport. The
  final verification removed intermediate-fragment accumulation: final content
  takes precedence over reconstructing the response's transient process.
  Click, scroll, activation, surface-transition,
  pre-WRITE, and WRITE onset close passive groups; closing a group is distinct
  from forgetting same-pane coverage. Pre-WRITE visual checkpoints remain raw
  evidence and never become model-facing READs.
- Two full v23 replays are byte-for-byte identical: `events.jsonl` SHA-256
  `7c37e14517f1c08dd7331055a737c55e274dd9404f400d4c183157a74d92150f`
  and `unresolved.jsonl` SHA-256
  `5d7e82c544c6b87d306f40adb30ad67c8a7bf56e32ff63425721301179eae2d0`.
  They contain 529 READs, 87 WRITEs, and 417 non-event dispositions. The
  established sequence cases now behave as intended: ChatGPT 337→338 and
  380→382→384→385 expose coherent scroll continuations once; Code 493→494 and
  687–690 suppress repeated cross-sensor/reflow states; ChatGPT 504→505→507
  retains readable continuation while suppressing the repeat; and Obsidian
  477→478 emits the second viewport only from `continual learning...` onward,
  searching inward past a clipped OCR edge rather than replaying preceding
  paragraphs. Genuine returns after intervening work remain full READs.
- Causal-v16 over v23 contains 616 converted events, 77 eligible micro-WRITE
  examples, 10 target exclusions, zero context exclusions, and zero
  rejections. End-to-end episode-v9 construction then produces 68 historical
  closed WRITEs and 43 loss-bearing episodes (11 multi-WRITE targets), with no
  micro-WRITEs in model-facing history. The episode manifest now propagates
  the compatible source reducer version, enabling the same READ rendering in
  actual episode packing rather than only in the micro-WRITE canary.
  The audited episode Qwen 32K pack contains 854,921 input tokens and 1,011
  target tokens including EOS; it removes 252,893 repeated READ tokens. Five
  retained contexts require the documented complete-state fallback because
  their overlap dependency is unavailable. This episode sample has no eligible
  paste-action targets; the full contract suite separately tests paste masks.
  Final artifacts use semantic-v23 `shadow-r20` (identical replay `r21`),
  causal-v16 `r6`, episode-v9 `r2`, and episode Qwen pack-v12 `r2`.
  Earlier r18/r19 and micro-WRITE packs remain reproducible development artifacts.
- Final verification passes 184 assertions covering 41 established review rows,
  all 35 passive groups, material-action boundaries, exact observation timing,
  unchanged WRITE payloads, and closed-episode contexts. The source-linked
  local report is `coupled-data/semantic-v23-verification/audit.json`; its
  companion `verify.py` is review-only and never consumed by production.
  Case 248's partially authored composer text is absent from its selected READ
  and actual packed episode contexts. The subsequent September 6 user review
  accepted the table OCR in 277/280, X-pane breadth, 477–478 repeat tradeoff,
  and minor noise in 693; these are not outstanding cleanup blockers.
  The review UI on port 8772 now uses final episode packing, opens with all
  READs chronologically, and searches numeric review labels exactly.
  The source-linked September 6 acceptance record covers 26 review rows.
  Wider-session transfer remains a separate gate from that accepted snapshot.
- The complete September 2, September 3, and September 4–6 source sessions
  are stopped and hash-verified: 32,409 raw records, 979 finalized WRITE
  attempts, and 9,423 PNGs. One last September 6 capture has pre-WRITE evidence
  but no finalized attempt; SIGTERM does not flush an active WRITE in this
  collector build. Raw integrity does not imply every human action was captured.
  The completed local combined review is stored under
  `coupled-data/sep02-04-semantic-v24-review-20260906` with bounded OCR batches
  and a 6 GiB guard for larger construction stages. It is not a training freeze.
  It has 4,042 reduced READs and 877 micro-WRITEs; episode normalization yields
  3,992 READs, 665 closed WRITEs, and 387 current loss-bearing targets
  (107 / 139 / 141 by source session). The remaining 278 closed WRITEs are
  history-only. Twelve untrusted episode groups remain explicitly excluded.
  Repeated reductions, 14,201 mechanical assertions, closed-corpus audits, and
  the Qwen 32K packing audit pass. Nine grounded paste actions and exactly one
  loss-bearing EOS per target survive packing. These are candidate counts,
  not a statement that all targets are human-approved completed thoughts.
- Semantic-v24 is an opt-in review candidate, not an automatic promotion of v23.
  It sends pre-WRITE observations through ordinary content comparison instead
  of blanket discard, and raises the cross-sensor ambiguous-repeat threshold
  from 40% to 72%. The old snapshot preserves 25 of 26 approved rows; UI597
  changes from empty to a 905-character viewport. In the approved v23 stream,
  that acknowledgment appears in a later READ before the next WRITE. The
  independent review subsequently confirmed an intervening switch to ChatGPT
  and return to Code: that later reread is legitimate. Two occurrences alone
  do not establish a regression. Compare repetition within the uninterrupted
  Code sequence; do not blindly restore the earlier suppression.
  The pre-WRITE correction is separately motivated by a missing terminal result.
- The expanded source audit finds a fresh pane-selection limitation: 1,133 of
  8,603 READ observations are selection-unresolved; 924 contain a named document
  pane above the existing 95% window-area limit. Inspected examples include
  readable fullscreen browser articles and Gemini responses. These counts are
  not unique lost passages, and some other exclusions are legitimate. Pane
  rules remain unchanged for the transfer test. Old UI116/601 app/pixel
  mismatches remain a separate capture limitation. See the combined review's
  `REVIEW_REPORT.md` and `pane-selection-quality-review.md` for raw-linked detail.
- The full-day replay additionally removes recurring interface/ad lines in
  approved X rows 396–399; panes, lineage, times, and post prose are unchanged.
  Thus 21/26 full-day approval records are byte-identical, four reflect the
  larger session's scaffolding calibration, and 597 is the threshold delta.
- Fresh September 3 stationary-chat repeats remain: 263–264 repeats the same
  response because stronger ordered overlap is attempted only for scrolling or
  near-simultaneous sensor changes. Raw screenshots confirm the duplicate.
  This is a comparison-coverage gap, not wrong pane selection; see
  `fresh-repeat-review.md`. No new semantic patch is folded into this replay.
  Some passive groups also start inside already-open material-input intervals;
  the no-interior-boundary-start audit does not establish that every group is
  entirely passive. Sampled surrounding events retained the content.
- Combined construction now streams expanded histories and token records.
  Old/new corpus, episode-review, and packed artifacts pass byte-equivalence
  checks, including all 43 prior packed examples. The IO implementation is
  separately fingerprinted in `construction-implementation.json`; the reducer,
  OCR, target policy, serialization, masks, and tokenizer remain unchanged.
  An audit-only stale v23 guard was corrected to require matching v23/v24
  source and rendering contracts; its separate fingerprint is recorded in
  `validation-implementation.json`. Generated artifacts did not change.
- Fresh full-session checks reveal material remaining semantic failures:
  lecture READ278 collapses different slides and omits two intermediate slides
  from actual packed inputs; Gemini READ503 drops a sentence opener solely
  because comparison OCR lacks a bullet glyph; stationary panes can still emit
  full repeats. Source IDs and decoded-input proofs are in
  `sep4-sequence-quality-review.md` and `fresh-repeat-review.md`.
- Sep4-session READ1478 contains 488 characters of an ongoing human Code prompt.
  The `->` versus OCR `→` difference defeats the exact-prefix authorship guard.
  It enters four later packed contexts, not its own target. Separately, packed
  target 56 is an unfinished Code prompt split by the episode navigation rule
  before its continuous same-field completion. The completed successor is
  history-only. These are real authorship/target-demarcation defects, not raw
  file corruption; no case-specific repair was applied to this review corpus.
- September 3 includes infrastructure credentials in a captured Wiki page.
  The combined corpus is local and not privacy-redacted. Target exclusions
  alone cannot prevent sensitive READ content appearing in later contexts;
  no new external transmission is authorized by this review task.
- The September 6 reviewed implementation is saved at `a739934`. The next
  separate candidate is semantic-v25, implementing reviewer slice A only:
  ordinary consecutive same-pane observations can use the existing stronger
  ordered comparison without a scroll or near-simultaneous sensor switch.
  Successful prior comparisons and the 72% ambiguous-repeat threshold are
  unchanged. Application/surface transitions and WRITE boundaries still reset
  comparison; X→Y→X returns remain legitimate rereads.
  A proven coherent newly exposed edge is retained from cleaned full-pane OCR,
  including when it falls outside the comparison inset. Similarity does not
  authorize concatenating scattered OCR differences. Suppression advances
  observed coverage so the following scroll does not replay an old block.
- The v25 replay is stored under
  `coupled-data/sep02-04-semantic-v25-review-20260907-r4` and reuses the immutable
  September 2–4 source journals and pane-v7 evidence. The earlier v25 r1–r3
  attempts are superseded diagnostics, not approved artifacts. Compared with
  reviewed v24, 39 READ projections change: 36 large repeats become empty when
  their predecessor is available, and three retain shorter edges. The three
  motivating repeats (Sep3 264 and 1139, Sep4 328) disappear from all 15 affected
  packed-context occurrences. All 26 approved cases and the overlapping 41-case
  reference remain byte-identical to v24, including legitimate returns.
  Every full READ, pane, identity, timestamp, lineage, and all 877 micro-WRITEs
  remain unchanged. The 665 closed WRITEs and all 387 targets, initial
  conditioning, masks, and episode membership are unchanged. The packing audit
  passes, including identical 9,977 loss-bearing token IDs across the 387
  examples. The full comparison reports 9,368 passing checks; all three
  sessions reproduce byte-identical events, dispositions, and reduction
  manifests. Raw journals and cached pane evidence retain their hashes.
  Memory was guarded at 6 GiB per construction stage, with the largest
  observed stage process tree approximately 2.24 GiB.
- Slice A is not a claim of perfect novelty detection: Sep4 READ842 still emits
  a 69-character repeated search snippet after OCR dropped its initial letter.
  It previously replayed 922 characters. The two Sep3 shortened edges retain
  real new text; this third residual is explicitly recorded for independent
  review. Slices B–D and premature WRITE closure are not repaired by v25.
  `slice-a-review.html` shows every changed READ chronologically against v24;
  `slice-a-audit.json` includes actual packed occurrences and target comparison.
  `REVIEWER_HANDOFF.md` records the independent-review checklist and hashes.
- `phase1-visual-read-promotion-canary-1` was stopped cleanly and reduced
  separately as a candidate-v14 validation trace; it does not alter the frozen
  semantic-v13 corpus.
- The historical 224- and 450-example experiments are developmental evidence,
  not untouched prospective confirmation of the promoted pipeline.

## Open boundaries

- READ capture is an interaction-surface proxy, not attention measurement.
- Dynamic interfaces can change without a tracked activity trigger.
- Final-viewport visual consolidation intentionally does not assert that text
  which passively scrolled away was read; those earlier frames remain available
  for later reducer or attention-model ablations.
- Screen-coordinate screenshots can include overlapping windows.
- Precise READ pane labels, WRITE resource/field identity, and integrated-
  terminal mode remain nullable when AX evidence does not prove them.
- Dictation, drag/drop, context-menu paste, and untriggered automation are not
  reliably attributed.
- Sample-time routing, live query capture, display, invalidation, and
  suggestion-conditioned feedback remain Phase 2.

Missing evidence stays unknown; these limits do not authorize guesses.

## Next step

Development checkpoint: combined v25 implements slice A and awaits independent
semantic review; it is not training-ready. Review ordinary same-pane repetition
(Sep3 263–264, 1138–1139; Sep4 327–328), the two retained new passages, the small
READ842 residual, approved scrolling cases, and app-switch-and-return rereads.
Text grounding/authorship (B), pane selection (C), distinct-slide boundaries
(D), then premature WRITE closure remain separately versioned changes. The
reviewer confirmed incomplete targets 56, 87, 116, 212, and 215.

1. Review the completed combined corpus: READs on port 8772, closed targets and
   exact packed context on 8773, original approved comparison on 8774.
   `review-guide.md` links the fresh failures by source identity. Full-day Before
   panels use the recorded live preview; they are not the old pane-v2 baseline.
2. Resolve the demonstrated lecture-state loss, interior OCR hole, authorship
   contamination, incomplete-prompt target, fullscreen-pane exclusions, and
   remaining stationary-repeat edge cases with general, separately versioned rules.
   Preserve approved cases and the explicit UI597 tradeoff; no point fixes.
3. Replay the same immutable evidence and compare both targets and actual packed
   histories. Keep mechanical audit success separate from semantic approval.
4. Resume chronological score-before-update training only after the corpus
   passes. Preserve exact inputs, outputs, routes, timing, and costs for later
   rescoring.

First determine whether the cleaner data improves content prediction. Do not
build the Phase 2 router merely to make the offline result look complete.
Once supported-app data is semantically legible and predictions remain poor,
we should move the bottleneck to data quantity, model capacity, or the learning objective.

## Parallel frontier representation comparison — September 9

This inference-only experiment does not change the collector, production
reducer, or training targets. Its corrected inputs are frozen in
`coupled-data/phase1-astra-median-context-20260909-prepared-v1` (plan SHA256
`b7283eeb10642ae0666cd9b1cdafe3285c099815116e36f52456e95c24ee3e98`).

- Keep the completed original 32K cleaned baseline.
- Compare full-window OCR and screenshots over that baseline's entire retained
  historical interval, not a five-minute tail with cleaned background.
- Compare expanded cleaned and full-window OCR at one shared **413,836 input
  token** budget: the median calibrated estimate of all 69 full-interval image
  inputs. This is not per-case matching and not a new token-budget image arm.
- Preserve underfilled histories without padding (21 cleaned, 5 OCR). Case 41's
  image condition exceeds capacity; retain its full input and an explicit
  unscored capacity disposition. Do not shorten it or treat it as model failure.

The five-condition, read-only UI is on port **8784**. It serves exact frozen
inputs, raw screenshots, and saved output/status when available. HTTP checks
verified 25 case/condition inputs and screenshot hashes. Browser visual QA was
unavailable; JavaScript syntax and read-only/cross-origin checks passed.

Execution snapshot: `coupled-data/phase1-astra-median-context-20260909-run-v2`,
plan SHA256 `037e560cea95a771c0aeeb41b06a55458b912bdc46c595cbc55bcd84f49a59c9`.
It binds the code/runtime, uses only the existing Astra xhigh subscription
proxy, performs no client/proxy shortening, saves wire evidence before dispatch, and loads one
request at a time with a 4 GiB process-tree limit. All 82 exact reuses were
verified against original transport evidence. There are **262 new requests**
and one blocked condition. Completed responses are never sampled again; only
an explicit output-free server error can retry once. Consecutive server errors
or uncertain dispatch pause with evidence.

The largest feasible image request (case 302: 366 images, 260 MiB wire) passed
offline save/restore testing; the v2 test peaked below 1.9 GiB. That request and a
large expanded-text request (case 345) form the authorized live capacity
preflight. Their predictions count toward the batch. Batch execution requires
the bound preflight to pass. The run's `preflight.json`, `progress.json`,
`resources.json`, and `execution-status.json` are the current status authority.
No training is part of this experiment. Holistic scoring remains separate from
construction fidelity and capacity exclusions.

The v1 preflight received HTTP 400, `Unsupported parameter: truncation`, with
no prediction/usage returned. Its full evidence remains in the v1 directory
and is hash-bound in the v2 plan. V2 omits that unsupported API parameter and
uses the earlier successful subscription request format. The full-input
usage check remains mandatory; documented API parameter support is not proof
of subscription-endpoint support.

Both v2 live preflights passed: case 302 images used exactly **890,937** input
tokens (estimate 890,937); case 345 expanded cleaned used **413,507** (estimate
413,250, +0.062%). Both completed with valid output and are retained without
resampling. The authorized remainder started under PID 68120; 260 new requests
remained at launch. The input budget stays frozen at 413,836. The preflight
process-tree peak was below 2 GiB. At batch launch 13.3 GiB disk was free versus
6.4 GiB remaining uncompressed request bodies; stored bodies are compressed.
The runner writes all outputs/usage/timings, audits on completion, and marks
completion pending holistic scoring. Port 8784 shows bound progress.

On September 9 at 19:42 Eastern, the user explicitly requested automatic
scoring and analysis after sampling. A separate attach-only watcher was started
under PID 94600 (`run-v2/analysis-followup/`). Its attempted design checked local completion/lock
state every 30 seconds without model calls, verifies the final coverage and
prediction hash, then resumes the same implementer thread for holistic scoring,
paired analysis, cost/latency reporting, and the UI update. It does not launch,
stop, retry, or change sampling. A real runner stop instead triggers an
investigation handoff, not automatic retransmission. No-network regressions
cover audit tampering, the capacity exception, writer-lock retry, and duplicate
handoff prevention. The handoff records the agreed grading rubric and requires
new results to remain blinded during initial judgment; it does not itself
claim that scoring has occurred.

Follow-up incident, September 10 at 01:03 Eastern: the above handoff did **not**
work end-to-end. Sampling paused at 20:06 Eastern September 9 after 197/262 new
predictions (279 total including reuse). Case 148's full-interval image stream
returned a transfer error with code 500, without an authoritative terminal
response. The watcher detected the stop, but every CLI attempt to resume the
app-owned thread was rejected as `already has an active writer`; its mocked
writer-lock tests did not establish live handoff reliability. The implementer
stopped watcher PID 94600 after the user's check-in, leaving all attempt and
prediction evidence intact. At that point scoring had not started. Do not describe this run
as complete or this same-thread CLI handoff as reliable. There are 65 new
predictions still missing; the failed transport must be resolved explicitly
without overwriting prior outputs or silently replaying uncertain work.

Recovery, September 10: the user authorized one replacement of the interrupted
case 148 image request. `scripts/resume-phase1-median-context.py` archived the
original failed transport under `authorized-replacement-20260910`, bound all
279 prior result hashes, and dispatched exactly one replacement. It completed
at 05:11:14 UTC with 545,176 input tokens. The unchanged frozen batch runner
resumed at 05:13:42 UTC for the 64 previously unsent requests; no completed
prediction was regenerated. The failed attempt's unreported usage remains
unknown, not zero.

The user then explicitly requested keeping this thread live and checking local
progress every few minutes rather than another timed handoff. The failed CLI
watcher remains stopped. Offline blinded grading is proceeding against saved
answers while sampling continues. Draft judgments are not final scoring:
publication requires complete 344-answer coverage, both reviewers, explicit
reconciliation, exact prior-grade preservation for the 82 reuses, and the final
hash/usage audit. The UI on port 8784 will display the finalized grades and
paired summary only after that publication gate passes.

Completed September 10 at 05:58:42 UTC: the frozen runner finished and audited
all **262 new answers plus 82 exact reuses = 344 predictions**. All completed
responses are structurally valid. The single capacity-blocked screenshot
condition remains unscored. Final prediction SHA256:
`b30e42acfb223889f86447916975b4e181e92918e30feacb6062f2f2363c8088`.
The recovery audit confirmed all 279 pre-existing results were preserved.

Final holistic scoring is in
`run-v2/review-v1/scored-v1/`, with the written analysis in
`run-v2/review-v1/analysis.md`. Two assistant graders independently reviewed all
262 new answers; eight disagreements were explicitly reconciled. The 82
previously reconciled reuse grades remain unchanged. This is assistant review,
not new human ground truth or a claim that the original reviewer thread graded
these new answers. Construction fidelity remains separate from prediction
quality.

| Condition | Passes |
|---|---:|
| Original 32K cleaned | 29/69 |
| Full-interval full-window OCR | 36/69 |
| Full-interval screenshots | 37/68 |
| Expanded cleaned, median screenshot budget | 34/69 |
| Expanded OCR, median screenshot budget | 36/69 |

Same-interval OCR gains eight cases and loses one versus the cleaned baseline.
Screenshots gain five and lose four versus OCR on 68 common cases—a net one
pass—at 2.67× mean generation latency and 6.19× mean API-equivalent cost on
those same cases. OCR history expansion yields four gains and four losses.
These are selected-case, single-answer diagnostics, not population accuracy or
evidence about the best training representation.

New selected answers used 87,321,372 reported input tokens, equivalent to
$1,621.55538 under the frozen API pricing contract; this is **not a subscription
charge**. Unreported failed-attempt usage remains unknown. Resumed process-tree
peak memory was about 2.02 GiB, under the 4 GiB guard. Sampling is stopped by
normal successful completion; the ordinary collector was not touched.

The read-only review UI on **port 8784** now displays the final grades, reasons,
five-condition score table, and paired changes. HTTP checks matched the
published summary exactly and verified 40 case/condition grades, including
the unscored capacity exception. Existing input/screenshot checks and JavaScript
syntax checks passed; browser visual QA was unavailable.

## Storage and retained artifacts — September 10

The user chose deletion, not archival, for superseded pre-September-2
`examples.jsonl` and `packed-examples.jsonl`. The cleanup removed 266 derived
files (104.3 GiB), including two temporary archive copies created before that
decision. All 25,174 protected raw/session/image files matched their before/after
SHA-256 inventories at the completion of this agent's deletion pass. Subsequent
manual deletion, confirmed by the user, also removed 22 newer derived files and
`normal-work-dry-run-5/raw.jsonl` (231,208,975 bytes). The latter is original
capture, was not in this agent's deletion plan, and needs recovery from Trash
or a backup. Trash access was denied; recovery has not been confirmed. The
verified compact r4 episode corpus and identical pack remain available even
though the original expanded r4 examples were manually removed. Two
local Time Machine snapshots were removed with separate user approval; current
files and external backups were not removed. The exact deletion journal and
capture inventories are in `coupled-data/storage-maintenance-20260910/`.

Old pre-Sep2 dashboards and integration tests may require reconstruction of
their deleted derived inputs. Model outputs, scores, costs, annotations, raw
evidence, source configurations, and manifests were retained. Optional private
integration tests explicitly skip when their derived fixtures are unavailable.

Shared-history storage `phase1-shared-context-v1` is an opt-in storage-only
format: `assemble-phase1-corpus.py --compact-examples`. The episode constructor
inherits it; updated JSONL loaders hydrate the same model input on demand. No
semantic reconstruction, targets, masks, or active experiments were changed.
The separate real-corpus canary has 387 decoded examples identical to its source;
its Qwen packed examples and context plans are byte-identical to the original.
Assembly/episode/packing paths enforce a 20 GiB free-space reserve rather than
silently consuming the disk or deleting data. See [`STORAGE.md`](STORAGE.md).

## OCR-correction subscription pilot — September 10

Completed a shadow-only Luna/Terra comparison through the existing subscription
LiteLLM route: **108 strings × two models = 216/216 successful responses**, plus
two separate synthetic canaries. Reasoning was off; no paid API fallback,
screenshots, or future WRITE targets were transmitted. Raw capture and the
canonical pipeline were not changed.

The fixed-comparison diagnostic covered 39 reviewed neighborhoods: 36 intended
merges and three genuine-new-information controls. Original text fully explained
9/36 intended merges; Luna and Terra each explained 5/36 after correction. Each
gained one and lost five previously explained cases; both retained all three
new-information boundaries. This is not a full pre-deduplication pipeline replay
or a character-accuracy benchmark. Inputs mix current semantic READ text and
selected earlier full-pane/raw-window comparisons, plus ordinary READ controls.

Screenshot inspection confirmed useful repairs but also invented sentence
completions, deleted observed wording, and faulty table reconstruction. Therefore
the full-text rewriting step is **not approved for pipeline promotion as tested**.
Median request latency was 9.88 s for Luna and 9.76 s for Terra. Reference
uncached API equivalents for the pilot were $0.07604 and $0.76477, respectively,
not subscription charges. All request/wire bindings and 50 original comparison
residuals passed the local audit. No requests failed or were replayed.

Artifacts and detailed limitations:
`coupled-data/ocr-correction-luna-terra-20260910/README.md`;
side-by-side local review: `analysis/review.html` within that directory.
Frozen plan SHA256:
`6383e37a8623939dd66eb3f09ac736b6f6279388cf16b338f3bbdfb7c9d1b11a`.

### Luna correction with preceding READ context — September 10

Completed the user-authorized follow-up on the same **36 intended-merge
neighborhoods plus three genuine-new-information controls**. Each current
correction string is unchanged; up to three original earlier same-window/pane
OCR views supply reference evidence. Earlier comparison observations receive
only their own earlier views. No future WRITE targets, screenshots, or raw AX
field values were transmitted. Two comparisons without prior context reuse
their isolated results; **88/88 new Luna requests succeeded** through the
subscription with reasoning off and no paid fallback.

The unchanged boundary diagnostic fully explains **7/36** intended merges,
versus **5/36** for isolated Luna and **9/36** for original OCR. All three genuine
novelty controls remain boundaries. Relative to isolated Luna, unmatched-word
counts decrease in 11 neighborhoods, stay equal in 11, and increase in 14.
Among the original 27 unresolved cases, three now reach zero, but inspection
shows that two of those outputs import reference text or omit observed wording.
Therefore these are matching outcomes, not seven verified faithful corrections.

Context genuinely repairs `events.jsonl` in 336–337 and `BCIs` in 27. It also
replaces a visible CLI draft with an older empty-field placeholder in 369–370,
deletes `and early training run data` in 584, and imports older offscreen text in
380. Full-transcription rewriting remains **shadow-only, not approved for
pipeline promotion**. Raw capture and canonical construction are unchanged.

All current inputs, 264 earlier reference occurrences, runtime/request hashes,
and complete response streams were audited locally. Median latency was 10.30 s;
213,921 input and 44,516 output tokens imply $0.09620 uncached API-equivalent
cost, **not a subscription charge**. The scoped proxy stopped after completion.

Results, case ledger, screenshots and targeted inspection notes:
`coupled-data/ocr-correction-context-luna-20260910/README.md` and
`analysis/review.html`. Frozen plan SHA256:
`86f17bc6a83e28db619f62368fa11435b5d981c7a78034a0e28c01b5a453b159`.
Final summary SHA256:
`81264dff3ee74f41a904e8cb27444433e3eef0796de55c3bd14797d2e863e1c1`.

### Luna exact-edit correction with low reasoning — September 10

Completed the next shadow test on the same 36 intended-merge neighborhoods and
three novelty controls. All 90 original strings and earlier reference lists
match the contextual full-text experiment. Luna returns exact substring edits
with occurrence numbers and reasons; nonedited text is copied verbatim. Both
output format and reasoning effort changed, so this is not a single-factor
reasoning ablation. The subscription proxy does not enforce a JSON schema;
exact edit application and rejection are validated locally.

All 90 observations completed, including one separately retained successful
retry of a confirmed server-error stream. Of 90 outputs, 79 edit lists were
valid and 11 rejected; original OCR remains on rejection. Ignoring harmless
no-op entries in a separate sensitivity check does not change the boundary
result: **9/36**, versus contextual full text **7/36**, isolated full text
**5/36**, and original OCR **9/36**. All three novelty controls remain separate.

The draft/placeholder failure in 369 is avoided and a missing table cell in 686
is restored faithfully. However, 587 replaces real current UI counts with old
values, falsely improving matching; 262 edits genuine wording rather than OCR.
Exact patches are more auditable but do not guarantee correct judgment.
**No production promotion or collection changes.**

Median successful-request latency 12.22 s, mean 14.42 s. The 90 successes use
216,050 input / 63,150 output tokens, including 46,749 reasoning tokens;
uncached API-equivalent $0.11899 is a reference, not a subscription charge.
Failed-stream usage is unavailable and not assumed zero. Results, raw streams,
exact proposals, cases and targeted screenshot judgments are retained in
`coupled-data/ocr-edits-luna-low-20260910/README.md` and `analysis/review.html`.
Frozen main plan SHA256:
`a872289c7eadd83c1295c85ce5726aac715bf9ac4762a15c18c8eb3511a3636b`.

### Sol xhigh OCR comparison — September 10, stopped at five

User authorized accuracy-first follow-up using Sol xhigh through the existing
LiteLLM subscription, not a paid API route. The same 90 observation strings,
earlier reference lists, shuffled order, exact-edit prompt and validator are
frozen against the Luna low experiment; model and reasoning effort change.
The 36 problem neighborhoods and three novelty controls are unchanged.
No collector, canonical pipeline, raw evidence or training changes.

Offline contract/resume checks and the synthetic Sol canary passed. The user's
later adaptive instruction stopped execution after five observations because
the accuracy gate failed. Four edit lists were valid, but Sol also changed
already-correct sentence order. All five outputs and the original 90-request
plan remain preserved. A reserved undispatched result slot stopped the old
runner before request six; that local exit is not a server error. No Luna max
or Sol low was started.

Artifacts: `coupled-data/ocr-edits-sol-xhigh-20260910/`.
Frozen plan SHA256:
`72c95dd45bbae4be1a4098dff9246d5b1ae5488ce9f0954bfc705c46fea9cdb7`.
A separate read-only watcher completed the audit and reports the original plan
as incomplete. That reflects the intentional five-request stop, not lost data.
Zero unmatched words alone must not promote an OCR correction to production.

### Astra low matched OCR probe — September 10, complete

The next user-authorized gate completed exactly five requests through the
subscription. Same five Sol inputs, earlier references, prompt and patch
validator; no images or future targets sent. All five exact-edit lists are
valid. Median latency is **26.10 seconds**, versus Sol xhigh **180.47 seconds**.
All requests, response streams, edits, usage, timings and frozen code survive.

Local screenshot review shows useful but incomplete repairs. Astra avoids
Sol's erroneous Discord sentence reorder and faithfully repairs the capture
audit table. Other outputs retain line-order errors; the fifth returns no
changes to a clipped Obsidian semantic READ. This is partly clipping/layout
loss, not just character recognition. Neither model passed the completeness
gate; no remaining batch, collection change or production promotion follows.

Artifacts and per-input review:
`coupled-data/ocr-edits-astra-low-20260910/README.md` and
`analysis/review.html`. Frozen plan SHA256:
`4f9042efdedc054714e18c5102bbd66b08280fe7f1e0c77cd1135ee2f995659c`.
The no-network `review-phase1-ocr-probe.py` audits hashes and response replay for
both models. Five observations are not five fully evaluated boundary cases.

### Root-cause audit of the 67 flagged examples — September 10–11

Diagnostic-only follow-up; no new model requests, collector changes, production
repairs, or target edits. The source-bound report is
`coupled-data/sep02-10-fidelity-causes-20260911/final-review/REPORT.md`, with an
individual ledger for all 67 original flagged examples and seven additional
cases/controls. These are the frozen **687-cohort numbers**, not old UI indices.

The remaining 27 boundary failures must **not** be called 27 OCR-recognition
failures. Verified causes include panes cutting visible line starts (369–370,
574–575, 584), a floating button obscuring words (381–382), actual OCR/layout
corruption (239–240, 380), status/toolbar changes, and prior/draft alignment.
Exactly reordering already-recognized lines removes both residual words in
245–246 and 580–581; no model or character corrections are needed for those
diagnostic counterfactuals. This is not a universal table-order fix.

The 39 neighborhoods now retain the already-reviewed 36-repair/3-keep policy:
nine pass the existing v10 diagnostic, 27 do not. Eleven neighborhoods still
have explicitly partly unresolved mechanisms. In particular, 360–361 has a
pre-onset raw observation containing the allegedly new response, correcting
the earlier 35-repair/4-keep review. No automatic merge is authorized merely
by this grouping judgment.

WRITE issues are separate: all 11 prefix cases accept an empty AX baseline,
which is demonstrably not an empty composer in inspected screenshots. Case
523 also loses a surviving post-final-input checkpoint before episode onset
construction. Case 9's pre-Return screenshot directly contradicts its AX
value, with no intervening recorded mutation. All eight old-text-island local
trajectories replay exactly; their unchanged spans are not permission to
delete text from the intended final thought. Full restorations remain subject
to proof, and 529 remains unresolved.

Scripts: `audit-phase1-fidelity-causes.py`,
`review-phase1-fidelity-causes.py`, `check-phase1-fidelity-causes.py`.
Checks pass: frozen input and referenced screenshot hashes; two reproduced
ordering counterfactuals; eight exact edit/provenance replays; all case coverage;
and synthetic rejection tests. Peak RSS stayed below 337 MiB. Raw files were
streamed, screenshots linked rather than copied. This diagnosis also qualifies
the prior Astra table result: its input had pane clipping, not pure OCR errors.
The original model outputs and results remain unchanged.

### Pane-edge recovery and OCR ordering — September 11, offline validation

Two opt-in stages are implemented by `refine-phase1-read-surfaces.py`:

- A narrowly wider AX ancestor supplies same-frame OCR evidence for clipped
  left prefixes. The selected vertical interval stays unchanged. Original
  lines and their suffixes remain authoritative; full parent OCR is retained
  separately, not substituted wholesale or imported from a neighboring pane.
- `ocr-box-row-order-v1` orders unambiguous text by actual box geometry rather
  than the old 2%-of-window band. It is a lossless line permutation. Ambiguous
  columns, tables, invalid geometry and merged multiline boxes keep their
  original order.

This remains an explicitly offline candidate, not the canonical selector or
training dataset. It does not rebuild the collector, require permission
resets, change raw data, call a provider, or alter the live run. Existing
versions remain reproducible. `check-phase1-pane-order.py` is included in
`check.sh`.

Regression auditing compares every pane with its original, verifies unchanged
source/time/image identity, and replays all 39 reviewed READ boundaries.
Line-order-only changes must not create new information: the diagnostic may
retain an existing non-novelty proof only for the exact same line multiset,
observation, metadata, cutoff and draft. Both the original proof and the new
comparison result remain recorded; this is not fuzzy OCR repair or permission
to suppress newly added words, negations or quantities.

The review page shows before/after **pane-stage OCR**, not a finalized packed
model context. Full reducer/episode recompilation and promotion are a separate
gate; unresolved layout/occlusion/recognition cases are not claimed fixed.

Validated candidate: `coupled-data/pane-order-validation-20260911-r5/`, with
`ax-pane-edge-validation-v4` and `ocr-box-row-order-v1`. Across all 11,579
baseline panes, 152 have verified prefix recovery, 3,488 have reordered boxes,
and 7,961 records are exactly unchanged. Every changed line is checked against
the retained wider OCR or a complete original-line permutation. Source hashes,
timestamps and line counts remain intact.

All nine previously resolved boundary neighborhoods remain resolved, and all
three genuine-new-information controls remain blocking. The two order-only
cases, 245–246 and 580–581, now resolve completely: 11/36 proposed-repair
neighborhoods are explained, versus 9/36 before. Pane recovery is partial at
the downstream boundary level: 369–370 falls from 19 unexplained words per
READ to four; 574–575 falls from 14/1 to 8/1; 584 falls from 26/0 to 1/0.
Those residuals remain blocking rather than being waived away.

Rejected prototypes exposed three regression risks now covered by checks:
whole-parent OCR replacing correct existing suffixes (e.g. GTM → GT), loose
anchors rewriting several existing leading characters, and repeated phrases
being mistaken for missing prefixes. The final rule preserves suffixes,
permits at most one damaged leading alphabetic character, protects numeric
prefixes/signs, requires unique same-frame matches, and refuses extension
when the existing start already matches. No example IDs select behavior.

Final replay peak RSS was below 195 MiB per worker; the complete boundary
audit peaked below 700 MiB. Both enforce a 1 GiB process guard. Screenshots
were referenced, not copied. See `audit/summary.json`, `audit/boundaries.json`
and `audit/review.html` under the candidate directory.

A second complete replay at `pane-order-validation-20260911-r5-repeat/`
produced byte-identical `read-surfaces.jsonl` and `decisions.jsonl` for all
four sessions, using zero new OCR calls. `./scripts/check.sh` passes; its
historical real-runner, context-window integration and Inkling integration
checks explicitly skip unavailable old derived artifacts. These skipped
checks are not evidence of a new end-to-end training compilation.

### Pane selection followed by Astra-low recognition — September 11

**Post-fix measurement:** the subsequent full 687-context re-pack is in
`coupled-data/post-pane-context-audit-20260911-final/`. Verified remaining READ
defect exposure is **77/687 (11.21%)**, versus 83/687 (12.08%) before pane repair.
The original-string-only scan falls to 55, but 22 ostensibly cleared contexts
still contain screenshot-verified damaged prefixes (`›ther interruptions` or
`*a>> sol`). They remain counted. Pane-related exposure is 36 → 30; the other
tracked categories are unchanged. This is a known-defect lower bound, not an
exhaustive quality estimate. Astra remains a fifteen-input pilot, not a deployed
corpus-wide correction. Do not claim this is a post-Astra rate. All 687 targets,
queries, masks and closed historical WRITEs remain unchanged. Bounded audit
workers stayed at about 1 GiB. See the report and per-case ledger for evidence.

The current offline pane candidate is `ax-pane-edge-selection-v6` in
`coupled-data/pane-selection-v6-20260911-r2/`. It builds on the preceding
same-frame prefix proof but also recovers whole lines exposed by the corrected
left boundary. Top, right and bottom stay fixed. Existing OCR suffixes and line
order survive; newly added lines must be completely visible, outside the old
pane, and have one unambiguous position. A fragment still touching the new cut
edge is not treated as a complete line.

All 11,579 pane observations were replayed: 152 repaired, 11,427 exactly
unchanged. The repair extends 1,872 prefixes and recovers six whole lines,
including the missing `best` in three Obsidian observations. Original text,
source identities, capture times and screenshot hashes are checked. Six real
regression assertions and a byte-identical repeat of materialization pass.

Do **not** promote the preceding v5 wholesale-re-OCR prototype. It restored
some omitted text but damaged readable paragraphs/numbers elsewhere and returned
two empty comparison projections. V6 preserves existing text and uses the
second same-frame OCR only as evidence for exposed prefixes/lines. The failed
prototypes remain inspectable rather than being silently overwritten.

Astra-low was tested separately through the local subscription proxy on fifteen
purpose-selected pane inputs with up to three strictly earlier same-surface
views. No screenshots or future WRITE targets were transmitted. Exact edit
contracts preserve untouched bytes. After the pane-preservation revision, only
three inputs changed; those three were rerun and the twelve identical earlier
inputs reused. All eighteen actual requests completed; latest results for the
fifteen inputs contain 52 edits. Median latency is 8.60 seconds, mean 8.77 seconds.
Total usage across all eighteen requests is $0.623 uncached API-equivalent,
not API billing.

Assistant screenshot review found useful changes in twelve inputs, but seven
of those remain incomplete. Tables, severely damaged text and some identifiers
are not solved merely by a valid edit response. These are shadow corrections,
not blanket production authority, not a population accuracy estimate, and not
human adjudications. The final candidate's tested model inputs are verified
byte-for-byte against its pane evidence.

The collector, raw journals, original screenshots, episode memberships and
training targets remain unchanged. This does not freeze v26 for training or
claim a new whole-corpus context-error rate. See the combined `REPORT.md` in
the v6 candidate directory and the two OCR reports under
`pane-astra-low-20260911/analysis/` and `pane-astra-low-v6-20260911/analysis/`.

All four sessions subsequently passed semantic reduction and causal compilation;
all 1,633 low-level WRITEs are unchanged. Rechecking the 39 known boundaries
preserves the same ten complete non-novelty proofs and all three genuinely novel
controls. No earlier complete proof regressed. The pane fixes reduce, but do not
eliminate, residual discrepancies in 369–370, 574–575 and 584. The model edits are
still shadow evidence, not permission to merge episodes automatically.
One reconciled READ selects a different real screenshot 84 ms earlier; its raw
timestamp is verified and no cohort target cutoff is crossed. This is recorded
in `boundary-audit-r2/selection-changes.json`. The audit peaks at 1.51 GiB;
the largest final replay worker peaks at approximately 2.43 GiB.

### Bounded, evidence-backed corpus curation — September 11

The reviewer-approved next slice is now separate from the general collector and
reducer: `coupled-data/sep02-10-curated-repair-20260911/`. This is local corpus
curation, not a training freeze or a new collection schema. Raw journals,
screenshots, prior results and the live collector remain untouched.

- Ten reviewed non-novel READ boundaries join sixteen original examples and
  their history-only corrections/continuations. Reconstruction uses retained
  editable states, not concatenated targets. The initial query remains the first
  member's query; later history uses the merged composition. Other candidate
  decisions remain unchanged, including the three genuinely novel READ controls.
- Six rejected Gemini submissions are recovered from an observed empty prompt,
  valid mutation trajectory, complete pre-Return prompt, and subsequent reset.
  No intervening semantic READ or outside WRITE occurs in those six intervals.
  The submitted text, including human typos, is preserved. The production Swift
  destination normalizer is reused through a small offline adapter; no Python
  app-identity heuristics were added. The general shortcut guard is unchanged.
- Eight selected historical READs receive exact screenshot-supported span
  corrections, including the damaged prefixes, `RHF` recognition, duplicate
  `identity.e panes.` fragment, and misordered terminal commands. Corrections are
  confined to the new READ arm; both arms share repaired targets, queries,
  masks, chronology and historical WRITEs.

Case 670 illustrates why endpoint review matters: the keyboard evidence alone
did not establish submission, but its immediately following saved screenshot
shows the exact corrected prompt in a sent-message bubble, an empty composer,
and the assistant thinking. `closure-adjudications.json` binds that visual
judgment to the raw record, screenshot hash and finalized-content hash. Its
availability is the actual closure-observation time, not a backdated timestamp.
The earlier `joins-v1`/`paired-v1` preview without that closure is superseded by
`joins-v2` and the subsequent paired rebuild.

This pass is intentionally incomplete. Remaining work includes the other 26
reviewed boundary neighborhoods, malformed onset/composition cases, and 58
shortcut-rejected raw attempts. Of those 58, 23 have substantial pre-Return
content in a short initial field; two concern existing longer fields, ten have
short pre-Return content, twenty lack a Return checkpoint, and three lack an
initial AX value. These are triage categories, not eligibility decisions or
guaranteed new targets. Do not interpret eight repaired READs as eight entirely
clean contexts, or reuse 77/687 as a measured error rate for the changed cohort.

Local negative tests cover novel READs, changed fields, discontinuity, outside
WRITEs, submission boundaries, lost endpoints, paste ambiguity, timeouts and
fallback/delta consistency. A dedicated curated-review audit checks paired
causality and reference-tokenizer packing; the older generic episode audit has
a different onset schema and is not authority for this mixed curated artifact.
Full Qwen3.8 native packing and training authorization follow completion of the
remaining curation, not this partial checkpoint. No provider calls occurred.

The final `paired-v2-audit/audit.json` passes for 687 examples per arm: 671
unchanged original targets/queries/masks, ten merged compositions and six
recovered submissions. All 1,172 unaffected historical WRITEs are unchanged;
the common closed-WRITE stream now has 1,188 events. All examples pass causal
cutoffs and paired-identity checks; 26 canaries per arm additionally pass the
actual reference packer, target-token equality, input masking and single-EOS
checks. Join/recovery repeats are byte-identical. Final audit peak RSS is
2,826 MiB (2.76 GiB). Dedicated curation, READ-boundary and native-order tests
pass; the full repository suite was not rerun. See the repair directory's
`REPORT.md` for remaining exclusions and the explicit no-training-freeze gate.

### Second bounded curation checkpoint — September 11

The current review artifact is
`coupled-data/sep02-10-curated-repair-20260911/paired-v3`, superseding paired-v2.
It has 697 targets per arm: 668 unchanged originals, 13 reconstructed merges
and 16 recovered submissions. Both arms share 1,195 closed historical WRITEs.
The new work adds three visually adjudicated Obsidian corrections to their
original compositions and recovers ten additional complete submissions.
The general shortcut guard and collector are unchanged. Explicitly reviewed
novelty overrides retain the failed automatic assessment and bind the visual
evidence to hashes; they do not pretend an automatic proof succeeded.

`context-v3-audit` rechecks every actual new-arm 32K reference input. Remaining
known READ defects: 36/697 (5.16%), from six source events: viewport clipping
(24 contexts), OCR/layout (12), and an overlay (4), with overlapping categories.
The earlier 36/687 (5.24%) is superseded only because the cohort grew; this pass
repairs WRITEs, not those six READs. The pre-curation figure was 77/687 (11.21%).
These are known-defect lower bounds, not total pipeline error rates.

Remaining priorities: 23 false-boundary neighborhoods touching 37 original
examples; malformed onsets/old-text targets; and 48 rejected attempts. Recovery
now gives those attempts concrete review reasons rather than only a generic
shortcut rejection. None of these counts is a promise that every candidate
will become a target. The three genuine novel-READ controls remain unchanged.
The current repair directory's REPORT.md contains the ordered fix classes and
artifact links. No provider calls or training occurred.

The paired-v3 audit passed every example's causal/target/query/mask contract
and 39 reference-tokenizer canaries per arm (identical targets, input loss
masked, one EOS). Context-audit peak RSS was 667 MiB; paired-audit peak RSS was
2,464 MiB. Focused curation, raw-episode, READ-boundary and OCR-order tests pass;
the full repository suite was not rerun. Training remains explicitly unfrozen.

### Third bounded curation checkpoint — September 11

Current review corpus:
`coupled-data/sep02-10-curated-repair-20260911/paired-v4`.
Nine further false READ boundaries are repaired using reviewed before/after
screenshots plus continuous editable-state replay. Their prior READ evidence
remains available before the original onsets. Case 9's AX sentence-order error
is corrected from the pre-Return and queued-message screenshots, in both its
target and later WRITE history; original AX evidence stays in audit provenance.
No raw data or collector code changed.

There are 689 targets per arm and 1,186 common closed historical WRITEs.
Relative to paired-v3, 17 fragments become nine reconstructed targets, case 9 is
corrected, and 679 existing target/query/mask/timing contracts stay unchanged.
All 13 earlier join reviews and the three genuine-novelty controls are unchanged;
join regeneration is byte-identical. Mixed paste targets preserve their markers
and resolved historical payloads.

The actual new-arm 32K reference-input audit now finds **28/689 (4.06%)** known
READ-defect contexts, versus 36/697 (5.16%). Original cases 129–136 improve; no
previously clean retained target acquires a tracked defect. Five READ sources
remain: viewport clipping in 16 contexts, OCR/layout in 12, and an overlay in
four, with overlaps. This is not a combined or exhaustive pipeline error rate.

`paired-v4-audit-final/audit.json` passes all examples and 46 packing canaries per
arm: same target tokens, masked input, one EOS, strict cutoffs, no micro-WRITE
history. The initial audit directory is incomplete: its checker incorrectly
looked for audit provenance in the privacy-filtered arm projection. The final
checker verifies that provenance in the preserved common-WRITE artifact.
Peak RSS was 875 MiB for context audit and 2,700 MiB for paired audit. Focused
tests pass; the whole repository suite and full Qwen3.8 native pack were not run.

Next: 14 remaining false-boundary neighborhoods (20 original targets), 11
false-empty/prefix cases and seven old-text-island cases (overlapping lists),
then remaining READ damage and 48 rejected attempts. Larger chains must be
reviewed as whole compositions, not declared solved after a partial pair join.
The repair REPORT.md contains exact case lists and artifact links. Training
remains unfrozen; no provider calls occurred.

### Fourth bounded curation checkpoint — September 11

Current review corpus:
`coupled-data/sep02-10-curated-repair-20260911/paired-v5`.
Five whole compositions replace 11 loss-bearing fragments: original cases
262–263, 268–269, 380–382, 475–476 and 501–502. There are **683 targets per arm**
and **1,175 common closed historical WRITEs**. All 678 other target/query/mask/
timing contracts and 1,170 retained historical WRITEs are unchanged. The
previous 22 join reviews, case 9 correction and 16 recovered submissions hold.

The join utility now reconstructs consecutive chains with proof at every edge
and evidence tied to the first onset. The chain folds middle edits and deleted
wording into the final completion; it does not concatenate target fragments.
For two existing-note cases, manually reviewed initial AX field evidence is
explicitly bound alongside current screenshots, since the earlier screenshot
showed a different region. This remains corpus curation, not a broad novelty
heuristic or a collector change.

Two boundaries remain deliberately separate: 499 exposes a new lecture bullet;
505 edits the previous note bullet, not the new paragraph. The latter was
rejected after the trial reconstruction revealed the wrong grouping. Use
`read-boundary-adjudications-v4.json` and `joins-v5/`, not the superseded
`read-boundary-adjudications-v3.json` / `joins-v5-shadow/` trial.

Known READ-defect contexts are now **10/683 (1.46%)**, down from 28/689. Fourteen
unchanged examples lose a bad repeat; four affected fragments are absorbed.
No previously clean retained example gains a tracked defect. The remaining
viewport-edge witness affects original cases 275–284. This is not a total
target-plus-context error rate. Repeated/self-draft READ removal explains the
improvement; the OCR recognizer did not change.

Repeated reconstruction is byte-identical. All-example causal/paired checks
and 50 reference-packing canaries per arm pass: input masking, equal target
tokens, one EOS, strict cutoffs, no micro-WRITE history. Maximum observed audit
RSS was 2,700 MiB. Focused curation, raw-episode, READ-boundary and native-order
tests pass; the full suite and Qwen3.8 native pack remain outstanding.

Next: six unresolved boundary neighborhoods (nine original cases), eleven
false-empty/prefix target cases, and six old-text-island cases, with overlap;
then the residual READ witness and 48 rejected attempts. Case 382's old-text
problem is fixed. The repair REPORT.md lists exact cases, retained controls,
and all artifact hashes/audits. Training remains unfrozen. No raw data,
collection process, or provider service was changed or invoked.

### Fifth bounded curation checkpoint — September 11

Current review corpus:
`coupled-data/sep02-10-curated-repair-20260911/paired-v7`.
Original cases **410 and 523** now use the complete prompt and the original
pre-mutation query rather than the continuation after a false empty AX reset.
The first restores `ok cool monitor the ...`; the second restores the important
negative opening `you should apply that. also more feedback, i dont think any
of the answers for 220 should pass`. The temporary deleted `more` is not trained.

`restore-phase1-prompt-onsets.py` is a shared evidence-bound offline curation
rule: final local mutation states, same retained editable, reviewed real empty
onset, no intermediate submission/outside WRITE, stable clipboard, and explicit
intervening READ review. It rejects stale checkpoints, ambiguous paste or
deletions across the hidden prefix. Case 410 additionally has an exact queued
prompt screenshot; 523 uses complete local trajectories and a final-input
checkpoint before terminal AX resets. Neither is a concatenated key trace.

Both targets and later historical WRITEs are corrected, with deterministic
lineage and original conditioning. Copied fragment metadata is replaced, and
the later fragment's numeric cursor-fidelity assertion is cleared. The one
reviewed READ containing 410's own draft plus the already-seen response is
removed in both arms. All other READ records are unchanged.

Counts: **683 targets per arm**, **1,174 common historical WRITEs**. All **681
other target/query/mask/timing/conditioning contracts** and **1,172 retained
historical WRITEs** match paired-v5. The paired all-example audit and **52 actual
reference packing canaries per arm** pass, including source replay, masking,
one EOS, paired target IDs and causal exclusion. The restoration and its
manifest repeat byte-identically. Focused curation, raw-episode, READ-boundary
and READ-repair tests pass. Maximum final paired-audit RSS: **2,821 MiB**.

Known direct target/query flags are **19**, and known damaged-READ contexts
remain **10**: **29/683 (4.25%)** directly flagged. Counting actual packed
historical-WRITE exposure gives **93/683 (13.62%)** with some known issue;
this is an exposure lower bound, not an exhaustive error or model-failure rate.
`context-v7-audit/` retains the per-example measurements.

The other ten onset/continuation cases are not declared fixed. See
`prompt-onset-extension-review.json` for exact dispositions (clipboard changes,
larger revisions, deletion of earlier draft text and remaining READ review).
The original 51-example figure was potential exposure, not guaranteed recovery.
Case 529 remains an unconfirmed control. Use `prompt-onsets-v3/` and
`paired-v7/`; earlier v1/v2 restoration and paired-v6 artifacts are superseded
intermediate checks. Full suite and Qwen3.8 native packing remain outstanding.
Raw data and the live collector were untouched; no provider calls or training.

### Sixth bounded curation checkpoint — September 11

Previous review corpus (superseded by the batch checkpoint below):
`coupled-data/sep02-10-curated-repair-20260911/paired-v8`.
Original **54–55 and 524** now form two complete historical prompts, but are
**history-only**: 55 reads a newly completed reviewer response between parts;
524 opens a new “Why this judgment?” explanation before continuing. Restoring
the final action does not justify training its whole content from the initial
query when new information arrived afterward. The three former fragment
targets are retired. Earlier repairs 410/523 remain loss-bearing and unchanged.

The reviewed restoration v2 folds exact continuous revisions into the current
local endpoint, rather than duplicating the suffix. Raw probes labeled
`prompt_submission_observation` are inspected for their actual disposition;
only bound non-submitting focus/typing probes may cross the reconstruction.
Unknown/actual submissions, unsupported clipboard changes and unproven edits
still reject. Reviewed raw screenshots corroborate the real onset and surviving
prompt text; they are never retroactively inserted into the initial query.

There are **680 targets per arm** and **1,172 common historical WRITEs**.
All 680 retained target/query/mask/timing contracts and every READ record match
v7. All 1,170 retained historical contents match; 1,168 records are byte-identical
(410/523 receive updated audit provenance only). Repeated restoration artifacts
match byte-for-byte. Causal/paired audits and **56 reference packing canaries per
arm** pass. Peak audit memory is **2,832 MiB**, context audit **900 MiB**.

The prior known-issue registry now has **17 direct target flags**; its 10 READ
defect contexts remain. Any tracked direct/historical exposure falls from
93/683 to **81/680**, through history repair plus conservative target exclusion.
This is not a comprehensive cleanliness rate. Thirteen retained contexts lose
damaged prompt fragments (original 56–64 and 525–528); no retained example gains
a flag within that same registry.

One separate, newly verified READ limitation is not included in that rate:
524's opened rationale exists in raw OCR/screenshots but a `passive_dynamic`
group selects the later collapsed viewport. Keep this distinct from OCR
recognition or missing capture. Its exposure and earlier projection should be
audited before changing READ policy. No READs were changed by this slice.

Use `prompt-onsets-v4/`, `paired-v8/`, `paired-v8-audit/`, `context-v8-audit/`
and `outstanding-write-issues-v7.json` under the bounded-repair directory.
The REPORT records exact evidence and exclusions. The full suite/native
Qwen3.8 pack and training freeze remain pending. Raw files and live collection
were untouched; no provider calls or training were performed.

### Batched curation checkpoint — September 11

The user approved the fast path: curate a clean experimental corpus first,
codifying broader rules during/after training preparation. “11 neighborhoods”
means **nine related WRITE sequences and two READ situations**, not 82 independent
repairs. The larger count was their downstream exposure in subsequent examples.

`scripts/apply-phase1-reviewed-batch.py` applies the explicit
`batch-reviews-v1.json` adjudications to paired-v8. Every correction is bound to
raw observations, source hashes, existing event lineage and, where needed,
manually inspected screenshots. It does not change collection or the general
semantic reducer. The corrected artifact is **paired-v9-checked**; the earlier
paired-v9/paired-v9-final materializations are superseded.

- 22 old targets are replaced with two complete loss-bearing compositions and
  seven complete history-only compositions: **660 targets per arm**.
- 38 historical fragments become nine complete WRITEs: **1,143 shared WRITEs**.
- The two new targets are the complete URL-review request and chart-caption
  revision. Continuous states, original conditioning, exact final content and
  reviewed non-novel READs establish their eligibility.
- Later clipboard changes, newly inspected information and ambiguous initial
  state keep the other seven compositions history-only. Discord includes the
  full submitted introduction through original case 632, with unresolved mixed
  authorship rather than copied text incorrectly receiving authored-token loss.
- The new READ arm fixes the clipped note-edge garbage and retains the expanded
  rationale that a later collapsed screenshot had removed. No availability is
  backdated. Three separately reviewed non-novel/draft READs are removed.
  Every old-arm READ remains unchanged: **9,264 old / 6,776 new**.

All 658 untouched target contracts and 1,134 untouched historical WRITE records
match v8; every unreviewed READ matches. All-example causal/paired audit and
**71 reference-tokenizer packing canaries per arm pass**, including masks and
one EOS. Each restored composition survives fully in the next eligible actual
packed context. Both materializations' ten JSONL artifact hashes match; raw
adjudication reconstruction repeats deterministically. Focused regressions pass.
Peak memory: build 1,158 MiB; audit 2,405 MiB; context scan 754 MiB.

The known-witness scan now finds **zero surviving tracked defects in 660 new-arm
contexts**. This is not an exhaustive error-rate claim or a claim that all seven
history-only completions were recovered as training targets. Residual UI/OCR and
other mixed-READ authorship are not universally corrected. The old READ arm
intentionally remains the comparison baseline.

Review `BATCH-REVIEW.md` for before/after text and actual packed-context links.
`paired-v9-audit/` and `context-v9-audit/` hold the checks; `REPORT.md` records
artifact hashes. Native Qwen3.8 packing and the training freeze remain pending.
No provider calls, training, raw edits, collector changes or broad Git commit
were performed. Other agents' unrelated worktree changes are preserved.

## References

- Usage and artifact commands: [`README.md`](README.md) and
  [`COLLECTION_GUIDE.md`](COLLECTION_GUIDE.md)
- Research intent and phase boundaries: `~/Vaults/Notes/Phase 1.md`
- Current implementation: `Sources/` and `scripts/`
- Historical decisions and results: Git plus immutable `coupled-data/` and
  `episode-review/` artifacts

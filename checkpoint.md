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

## References

- Usage and artifact commands: [`README.md`](README.md) and
  [`COLLECTION_GUIDE.md`](COLLECTION_GUIDE.md)
- Research intent and phase boundaries: `~/Vaults/Notes/Phase 1.md`
- Current implementation: `Sources/` and `scripts/`
- Historical decisions and results: Git plus immutable `coupled-data/` and
  `episode-review/` artifacts

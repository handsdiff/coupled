# Coupled checkpoint — 2026-09-03

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
`phase1-semantic-v18`; neither is promoted merely by generating this artifact.
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

Pane-v6/v7 OCR keeps the complete selected pane as authoritative content. Its 20%
vertical inset is comparison-only. Semantic v18 then removes only proven
interface scaffolding, reconciles duplicate pointer/visual observations,
records adjacent order-preserving novelty without destroying complete READ
state, and consolidates passive dynamic responses to the last visible viewport.
Clicks, scrolls, activation/focus changes, and pre-WRITE checkpoints remain hard
consolidation boundaries. Packer v8 renders a READ delta only when its required
predecessor survives context packing; otherwise it renders the complete state.

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
the retained full-window evidence. The pane-v7/semantic-v18 review candidate
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

1. Manually review the pane-v7/semantic-v21 before/after UI, with particular
   attention to current AX panes, recovered prior panes, the 16 unresolved
   observations, browser chrome, VS Code editor/terminal transitions, passive
   AI responses, material-action boundaries, complete-state fallbacks, bounded
   ambiguous suppressions, and the named production cases.
2. If that review finds no remaining material issue beyond the explicitly
   deferred screen-occlusion case, apply the same pane-v7/semantic-v21 rules to
   the untouched September 3 session and inspect its new edge cases before any
   canonical-corpus promotion.
3. Audit READ surfaces, WRITE destinations, closed-episode boundaries,
   unresolved records, model-visible histories, loss-bearing targets, causal
   masks, and packing before freezing the new corpus.
4. Resume chronological score-before-update training only after that corpus
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

# Coupled checkpoint — 2026-09-01

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
| Semantic reduction | `phase1-semantic-v13` |
| Causal micro-events | `phase1-causal-v16` |
| Episode construction | `phase1-raw-episode-v8` |
| Episode projection | `phase1-raw-episode-causal-v8` |
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

An unfrozen visual-READ candidate is implemented behind
`phase1-semantic-v14`. The bounded SCStream monitor now promotes only a settled
visual-change frame or a causally safe frame immediately preceding a WRITE. It
persists that exact frame before running OCR, and the reducer verifies the
screenshot digest plus frame→OCR raw lineage. Full-window OCR remains raw
evidence: v14 emits a model-facing READ only after `ax-pane-read-v2` re-OCRs
the same image around a separately retained semantic content anchor. Frames
whose change interval overlaps an active WRITE remain suppressed raw evidence.
Exact near-simultaneous pointer/visual pane observations are emitted once.
This does not replace semantic v13 until a focused end-to-end canary confirms
the stored images, pane OCR, causal exclusions, and duplicate behavior on real
apps.

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
frame. The full build and regression suite pass after that correction. V14
remains a candidate until the metadata correction is observed in a packaged
live build and the resulting corpus is manually audited.

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

Semantic v13 selects the active AX-defined READ panel from the retained
full-window evidence, re-runs OCR there, and applies overlap removal only within
the same normalized pane. Causal v16 derives compact logical WRITE destinations
and READ sources while preserving the original evidence. Proven VS Code terminal
focus no longer inherits a background editor filename. A schema-7 READ names a
main surface or active pane only from its contemporaneous AX selection; a
subpane does not inherit an outer window title that may describe a background
document. OCR content and surrounding events are not used to guess identity.
Pre-schema-7 READ serialization remains unchanged. Shell-versus-agent WRITE
identity remains `unknown` unless direct evidence or an explicit manifest-pinned
local mapping proves it.

Episode v8 groups faithful micro-WRITEs into closed compositions. Duplicate or
unchanged READs do not split an episode; a novel causal READ, outside WRITE,
submission, changed composition region, destination change, or unresolved state
discontinuity can. Volatile raw AX identities are audit evidence, not sufficient
boundaries by themselves. Every micro-WRITE receives an explicit disposition;
production construction has no manual adjudication input.

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
- No collector is currently running. `phase1-visual-read-promotion-canary-1`
  was stopped cleanly and reduced separately as a candidate-v14 validation
  trace; it does not alter the frozen semantic-v13 corpus.
- The historical 224- and 450-example experiments are developmental evidence,
  not untouched prospective confirmation of the promoted pipeline.

## Open boundaries

- READ capture is an interaction-surface proxy, not attention measurement.
- Dynamic interfaces can change without a tracked activity trigger.
- Screen-coordinate screenshots can include overlapping windows.
- Precise READ pane labels, WRITE resource/field identity, and integrated-
  terminal mode remain nullable when AX evidence does not prove them.
- Dictation, drag/drop, context-menu paste, and untriggered automation are not
  reliably attributed.
- Sample-time routing, live query capture, display, invalidation, and
  suggestion-conditioned feedback remain Phase 2.

Missing evidence stays unknown; these limits do not authorize guesses.

## Next step

1. Package the capture-time Chromium metadata correction and confirm it in one
   normal collection; no dedicated permission-reset micro-canary is required.
2. Finalize completed compatible sessions with explicit coverage gaps and build
   episode v8 / episode-causal v8.
3. Manually audit READ surfaces, WRITE destinations, episode boundaries,
   unresolved records, exclusions, model-visible histories, and targets.
4. If no recurrent material error appears, freeze the untouched corpus, timing,
   contexts, routes, decoding, packing, cost/latency, and score-before-update
   contracts.
5. Score each new chronological block before appending it once to the preceding
   personalized checkpoint. Preserve exact inputs and outputs for rescoring.

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

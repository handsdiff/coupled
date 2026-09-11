# September 2–10 dataset: readiness and limits

This is the reviewed **new** construction, not a claim that every future session
will be clean. Raw evidence and prior model results remain unchanged. Finite
manual repairs are allowed for this experiment and retain their evidence.

## The intended behavior

| Requirement | Current construction and check |
|---|---|
| Predict a completed thought, not editor mechanics | Closed compositions replace their member micro-WRITEs in both targets and later history. Corrections are replayed, not concatenated. |
| Merge across non-novel observations | Initial field, earlier READs and already authored draft can explain an intervening READ. A repeated screen or own draft does not by itself split the thought. |
| Preserve genuine new information | New inbound evidence can start another prediction opportunity. The MAI cost/speed → error-rate chart → error-rate question remains separate. |
| Causal context | READ/WRITE `availableAt < targetBeganAt`; no target members in their own input. Onset conditioning is preserved. Evidence acquired later can reconstruct the outcome but is not backdated into its input. |
| Correct receiving surface | The same normalized destination appears in current queries and historical WRITEs, with raw identity retained for audit. Unknown terminal program identity remains unknown. |
| Coherent READs | AX pane selection, native OCR ordering, edge cleanup and source-backed corrections precede adjacent same-pane overlap handling. |
| Remove repeats without losing prerequisites | Packing uses the novel region only with the required preceding state retained. Otherwise it restores the complete cleaned READ; this is not an unrelated session-wide text blacklist. |
| Preserve dynamic responses | Passive progress can collapse to its later observed state, at that state's actual availability time. Material interaction and pre-WRITE evidence bound consolidation. |
| Remove interface scaffolding | Known controls and peripheral evidence are used; arbitrary repeated research text is not blacklisted. The final batch removes two exact, screenshot-confirmed terminal-footer projections, including the repeat's full-state fallback. |
| Paste authorship | Targets use literal `<\|paste\|>` actions; later history retains resolved payload with provenance. Uncertain paste authorship can remain history-only. |
| Correct loss | Native Qwen packing masks the complete input and supervises completion plus one native terminator. The SDK causal shift is checked per row; generation receives no target tokens. |
| Continual updates | Independent old/new cohorts; score before updating, train each new block once, continue optimizer state, never train the final block. |

## Final bounded cleanup

The local `final-reviews-v2.json` and resulting `batch-changes.jsonl` bind:

- Former new-pack **561–562**, original review **580–582**: one complete note,
  reconstructed from four continuous raw attempts. The separating READ is
  fully explained by the onset note and pre-READ draft. Its erroneous semantic
  READ is suppressed in the new arm; raw evidence remains.
- Former new-pack **570** and its immediately following history-only edit:
  retain “which is more likely true” **inside** the parenthetical it revises.
  Ten raw attempts are continuous; no READ intervenes. The later READ boundary
  is retained, not silently absorbed into this repair.
- Two identical terminal READ states: remove only the visually verified final
  model/status footer. Preserve body text, repeat suppression and fallback.

The next chronological WRITE was inspected for **55** recently repaired
compositions (the surviving reviewed joins, restorations and submission/batch
repairs). This exposed the additional parenthetical omission above. It does
not prove a universal theory of where all thoughts end.

## Explicitly retained limitations

- The earlier nine-composition batch contains **seven history-only** outcomes.
  Clipboard changes, new inbound information or uncertain authorship prevented
  a single proven target. Their final content is retained in history; this is
  not equivalent to recovering all their possible supervision.
- Two inspected neighboring-note boundaries remain conservative: a field-state
  discontinuity before “given that sol seems to have improved”, and a later
  READ with four unexplained OCR words before another continuation of the
  parenthetical. We did not claim either proves genuinely novel information,
  and did not relax the evidence gates merely to merge them. Both continuations
  remain in history.
- Previous user-accepted small edge/OCR omissions, table tradeoffs and the
  deferred lecture-slide/chart-return behavior remain deferred. This batch does
  not reopen pane selection or invent missing observations.
- Some user-authored text can still appear in later READs. Own-target leakage
  is checked separately; later-history authorship is not perfectly modeled.
- Known credential filtering is not a guarantee that arbitrary personal data
  contains no secrets. Audio, meetings, unsupported applications, opaque fields
  and non-keyboard pastes are coverage limitations—not recoverable OCR errors.
- Residual interface text or OCR imperfections may remain outside the reviewed
  examples. “Zero tracked defects” was a registry count, not an error-rate
  estimate; do not present it as exhaustive data cleanliness.

## Experiment interpretation

The historical arm is the actual September 1 pipeline, replayed on the same
later raw sessions; it does not inherit these repairs. Old/new targets and
counts may differ. Compare holistic usefulness and costs with explicit
denominators, and frozen/personalized likelihood **within** each arm. Absolute
old/new likelihood is not a paired same-target statistic. Bits per byte can
normalize a future report but does not make different target cohorts identical.

Final dataset, packing and execution manifests are local. Training and even the
paid preflight still require review and explicit authorization; this cleanup
does not grant it.

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

## September 11 reviewer-driven cleanup

The previous bounded cleanup below was not a corpus-wide cleanliness proof.
The independent reviewer found remaining false boundaries, scrambled prose,
duplicate screens and wrong-source labels in the actual native inputs.

The current artifact is
`coupled-data/sep02-10-cleanliness-review-20260911/corpus-v1/new`:

- 655 targets, 1,134 historical WRITEs, 6,767 READs.
- Seven exact-state composition repairs replace eleven targets and three
  history-only fragments. Eight self-derived boundary READs are removed.
- All 6,775 prior READs were screened; 3,766 local native OCR jobs supply
  ordering evidence. Automatic changes retain the original strings. Where
  native column-first order would detach table labels, only ordering *within*
  fixed original column runs is allowed. Exact one-to-one observation evidence
  is required; unproven layouts/recognition/partial-delta correspondences stay
  unchanged rather than being guessed.
- Fourteen finite screenshot-backed READ reviews correct glyphs, foreground
  sources, sidebar/status fragments and a row-preserving timeline. Five
  actual wrong-app labels are corrected, with two additional source-title
  clarifications. Evidence, reasons and source hashes remain local sidecars.
- After these repairs, exact adjacent equality or a substantial exact
  suffix/prefix overlap is rechecked on the same source. No fuzzy substring
  blacklist, no cross-WRITE merging, no removal of genuinely new remainder.
  The later representation is used only with its predecessor retained;
  otherwise the full corrected fallback remains available to packing.
- 648 unaffected target/query/mask/time contracts, 1,127 unrelated historical
  WRITEs and all 26 user-approved golden READ records are unchanged. The
  actual historical experiment arm is untouched.

The reviewer’s concrete cases are checked against actual native context plans:
former examples 12–22 have the corrected foreground sources; 420–426 no longer
receive the duplicate waiting screen; 460–467 retain coherent timeline rows
once, followed by the newly exposed response; 474–475 become one target.
Numbers here refer to the previous `packs-v3/new` cohort, not renumbered UI IDs.

The neighboring-note scan covers 84 adjacent same-note neighborhoods touching
eligible targets. Continuous state alone is **not** a merge rule: independent
bullets and genuinely new responses stay separate. It exposed the additional
six repairs beyond the reviewer’s motivating split.

## Earlier bounded cleanup retained

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
- A field-state discontinuity before “given that sol seems to have improved”
  still prevents a proven merge. The later parenthetical continuation previously
  blocked by OCR and a character-count footer is now repaired. Another reviewed
  neighborhood (former 524–526) includes finishing a previous word and beginning
  a new bullet in one raw interval. Its best thought boundary has not been
  adjudicated; do not describe the target abstraction as universally solved.
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
- The broad ordering screen is not a visual error-rate survey. In its latest
  reconciliation, 539 layouts, 226 line correspondences, 188 partial-delta
  cases and 38 nonexact source projections lacked an automatic proof; some
  are separately repaired by the finite reviews. These are *dispositions*, not
  that many confirmed bad READs or bad training examples. The audit files retain
  the individual IDs instead of silently calling them clean.

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

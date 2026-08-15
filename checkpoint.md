# Coupled checkpoint

The current committed `main` branch is the candidate implementation baseline, built on the grounded-paste, reverted-write, cursor-relocation, compact-history, and event-aware packing gates. The focused paste/read-attribution, reverted-write, and same-editable cursor-relocation acceptance traces have passed. Further collector or conversion changes are not treated as complete here until committed and validated.

## Objective

Construct a high-fidelity causal stream approximating:

```text
information the user was exposed to
+ current destination, semantic cursor state, and clipboard state
→ next authored text and grounded write actions
```

Raw collection must preserve enough evidence to revise event construction later. Derived events and training examples are versioned interpretations, not authoritative raw truth.

## Core distinctions

### Raw evidence versus interpretation

- `raw.jsonl` preserves screenshots, OCR, Accessibility states, input timing, clipboard evidence, checkpoints, and suppression reasons.
- `events.jsonl` contains provisional READ and WRITE events.
- The causal compiler produces chronologically valid history and candidate training examples.
- Collector append order is never treated as causal order.

### Event content versus training target

A WRITE event retains the complete observed net edit:

- inserted and removed content;
- operation and edit offset;
- destination and cursor state;
- reconstruction provenance.

The intended Phase 1 training target is narrower:

```text
target sequence = authored text + grounded paste actions + loader-appended EOS
```

Operation, removal, offsets, destination, timestamps, cursor metadata, and pasted payload tokens do not receive target loss. A proven paste action is serialized as the reserved `<|paste|>` marker using the model's existing tokenizer; its resolved payload remains available in the WRITE event and in later history.

## Implemented collection

### WRITE capture

Supported applications:

- Obsidian;
- Chrome;
- Codex/ChatGPT;
- Visual Studio Code, including its integrated terminal.

Mechanism:

1. Intercept the first mutating input.
2. Capture the focused editable's `BEFORE` state, complete within the configured value bound, before returning the input to the application; explicitly reject truncated evidence where reconstruction requires the missing text.
3. Reset `WRITE_DELAY` after each subsequent input.
4. Capture the held editable's terminal state.
5. Derive the canonical minimum contiguous `BEFORE → AFTER` edit.
6. Preserve the complete attempt and checkpoints in raw evidence.

Implemented safeguards:

- Cursor position does not influence diff reconstruction.
- Mouse-down selection changes and keyboard caret/selection navigation settle the current WRITE before the relocation reaches the application; the next mutation starts with fresh destination and cursor conditioning.
- The synthetic `BEFORE → empty` deletion fallback has been removed.
- Invalid terminal elements become unresolved unless a valid Return or paste checkpoint exists.
- A temporary typed checkpoint which returns to the original settled field state is no change, not a resurrected WRITE.
- Immediate Return is handled with synchronous pre-Return checkpoints.
- Clipboard state is captured with pre-mutation conditioning.
- Cmd-V payload plus immediate pre/post-paste field states are retained raw.
- Large deletions must be supported by complete observed `BEFORE` and `AFTER` states.
- Numeric cursor fidelity remains diagnostic and does not alter the diff.
- If an eligible editable remains semantically empty across Accessibility observations, the attempt remains raw with no derived WRITE (`no_change` or an unresolved capture resolution). The collector does not guess training content from keystrokes.

### Pre-mutation conditioning

Each WRITE attempts to retain:

- application and bundle;
- window and Accessibility role;
- field label or description when available;
- left context, selected text, and right context;
- field state and prompt state;
- current clipboard text, types, hash, truncation state, and pasteboard version.

Range-native Accessibility text is preferred. Pixel coordinates and numeric AX offsets are diagnostic rather than the semantic definition of cursor position.

This state is captured immediately before the first mutation and is sufficient for offline training examples.

### READ capture

READs are triggered by pointer movement, click, scroll, or detected application activation.

A derived READ requires:

1. a complete `READ_DELAY` interval;
2. a stable eligible app/window/display/title/bounds surface;
3. capture-time identity, bounds, screenshot, and OCR from that same surface.

Trigger-time identity remains raw provenance only.

Implemented safeguards:

- Mutating input supersedes an unsettled READ on the same window.
- In-flight screenshots are suppressed if writing or a surface transition makes them ambiguous.
- Surface changes restart the delay rather than inheriting an old timer.
- Stale per-window candidates are collapsed.
- Application activation rejects tiny helper surfaces and selects a plausible content window.
- Chrome auxiliary windows are retained raw but excluded from derived READs.
- Full-window screenshots are retained for later reprocessing.
- Adjacent viewport OCR overlap is removed without deleting later rereads.

### Standalone live event view

`Coupled.app` is a headless collector controlled from the terminal. `Coupled
Logs.app` is a separate, independently launchable process that follows the same
compact derived-event stream as `./scripts/coupled logs`. During a live run it
also displays that run's immutable resolved `session.json` settings. It has no
capture controls and retains only a bounded recent text buffer. Both
`com.niyant.coupled` and `com.niyant.coupled.logs` are permanently excluded from
READ and WRITE collection. The authoritative `events.jsonl`, `raw.jsonl`, and
stdout mirror remain unchanged. The viewer must not overlap a captured work
surface because semantic exclusion cannot remove pixels already covering a
rectangular screenshot.

## Implemented causal compilation

Current compiler: `phase1-causal-v12`

The v12 opaque-paste path has passed deterministic collector/compiler checks and
backward-compatibility compilation of `ordinary-work-audit-2`. That prior run
remains conservatively excluded because it predates the new explicit evidence;
the first live AX-opaque paste validation is still pending.

Timing:

```text
read.available_at        = capturedAt
target_write.began_at    = beganAt
prior_write.available_at = terminalDecisionAt

context(target) =
    stable_sort(events where event.available_at < target.began_at)
```

The compiler:

- reconstructs writes from raw evidence;
- repairs known legacy synthetic deletions;
- excludes stale reads from older sessions;
- serializes causal READ/WRITE history;
- uses compact model-facing history while retaining the richer canonical projection as `auditSerialized`;
- serializes structured historical WRITE text exactly once in provenance-bearing authorship segments;
- appends pre-mutation destination/cursor/clipboard conditioning as the query;
- emits structured authored-text and grounded paste-action targets;
- excludes pure deletions;
- records target and context exclusions explicitly;
- audits lineage back to source records.

For range-native semantic cursor contexts, complete semantic conditioning remains eligible even when unreliable numeric AX offsets disagree. Numeric offsets are diagnostic and do not alter reconstruction or impose a separate target gate. Phase 1 now states the same rule.

## EOS status

Implemented:

- Stored compiler targets remain tokenizer-independent structured segments.
- The loader contract tokenizes authored spans and the reserved `<|paste|>` marker with the unchanged model tokenizer, then appends exactly one EOS.
- Authored tokens, every paste-marker token, and EOS receive loss; pasted payload tokens do not.
- Dataset checks enforce the contract.
- The tokenizer-specific packer is connected to `Qwen/Qwen3.5-9B-Base` revision `68c46c4b3498877f3ef123c856ecfde50c39f404`.
- The saved tokenizer retains Qwen's original 248,077-entry vocabulary; `<|paste|>` encodes as the five existing IDs `[27, 91, 54966, 91, 29]`, and Qwen EOS remains `248044`.
- Packed causal-LM labels mask all history/query and padding positions with `-100`; authored tokens, paste actions, and exactly one trailing EOS receive loss.
- Input packing retains the newest complete causal event blocks within 32K, explicitly tail-truncates only an oversized oldest event's semantic text while preserving authorship boundaries, never emits partial JSON, and verifies that the right-edge destination/cursor/clipboard query survives intact.
- The packing manifest states that 32K is the history-plus-query budget, targets are appended outside that budget and may not be truncated, and the trainer must support the recorded total sequence capacity.
- The model vocabulary is not resized or otherwise modified. Exact decoded `<|paste|>` is interpreted as the action marker at inference; partial markers are invalid.

A literal EOS string must not be inserted into source targets.

## Latest validation

`read-boundary-test-1` confirmed:

- semantic left/selected/right cursor context;
- canonical diffing without neighboring old paragraphs;
- active-write READ suppression;
- bounded deletion instead of false whole-document deletion;
- correct app/window attribution for emitted READs;
- successful causal compilation and audit.

It also exposed:

- Obsidian-generated list and zero-width formatting entering net write content;
- application activation selecting tiny helper windows.

The helper-window issue was fixed in `9f0457b` and subsequently passed the activation portion of `paste-read-attribution-test-1`.

`paste-read-attribution-test-1` confirmed:

- exact Obsidian authored/paste/authored segmentation;
- exact Chrome paste-only segmentation;
- exact Codex authored/paste recovery after rapid Return and terminal invalidation;
- clipboard state present in pre-mutation conditioning;
- pasted payload omitted from the current structured target but retained with provenance in later WRITE history;
- all six emitted READs matched their capture-time application, window, and visible content;
- no Accessibility errors or event-tap timeouts;
- unresolved paste transitions were target-ineligible rather than silently treated as authored targets;
- successful causal compilation and audit under the then-current conversion.

It exposed one typed checkpoint that was fully deleted before settlement but was resurrected as a WRITE. Commit `228e763` removed that fallback and added a compiler-side rejection for older traces.

`reverted-write-test-1` then confirmed:

- identical settled `BEFORE` and `AFTER` produce raw `resolution: no_change`;
- no derived WRITE or proposed event ID is emitted;
- the causal compiler produces no training example for the burst;
- the corrected schema versions and causal audit pass.

`cursor-relocation-test-1` confirmed:

- typing at multiple distant positions in one Obsidian AX editor produces independently conditioned WRITEs;
- mouse relocation closes the prior write with `pointer_selection_boundary` before the click reaches the application;
- keyboard caret navigation closes the prior write with `selection_navigation`;
- each insertion retains the semantic left/right context from its own starting location;
- no document-spanning replacement was produced;
- pure deletion events remain history but are excluded as Phase 1 content targets;
- the causal compiler produced 13 events and seven nonempty targets with zero reconstruction rejection and passed its audit.

`vscode-opaque-write-test-1` and `vscode-opaque-write-test-2` confirmed:

- VS Code integrated-terminal and Codex-terminal prompts exposed usable Accessibility values in both runs and used ordinary canonical `BEFORE → AFTER` reconstruction;
- typo correction, three-second settlement, rapid Return, repeated writes in the same prompt, and ordinary shell commands retained the expected content and boundaries;
- VS Code READs captured visible editor/terminal pixels under the correct application and window, while candidates superseded by active writes were retained only as raw suppressions;
- the second run compiled into eight eligible examples with zero target exclusions, context exclusions, or reconstruction rejections and passed the causal audit;
- the unexercised keyboard-shadow fallback was subsequently removed rather than retained as parallel unvalidated collector machinery.

Tokenizer packing validation confirmed:

- `paste-read-attribution-test-1-phase1-v11` packed five examples with four structured paste actions; each became the same five-ID reserved marker sequence and every target ended in one loss-bearing EOS.
- Resolved payloads for all four paste targets remained in their historical WRITE serialization while being absent from the current target spans.
- Structured historical WRITEs contain their resolved text exactly once; `auditSerialized` retains the redundant resolved text and full checkpoint provenance for inspection.
- Packer self-audits prove that both legacy top-level content and structured authorship segments can be truncated to valid semantic suffixes without losing surviving segment types.
- The current compiler rebuilt Run 5 as 424 causal events, 48 targets, 147 target exclusions, 110 context exclusions, and one explicit reconstruction rejection; its causal audit passed.
- Run 5's 48 examples packed with the actual Qwen tokenizer and compact context serializer. The longest complete model input was 78,231 tokens. Event-aware packing discarded 608,483 old input tokens and 3,337 old event instances across examples; 15 boundary events were retained as valid JSON with explicit content-tail truncation.
- No malformed partial JSON entered model input. Every right-edge conditioning query remained intact, every model-input label was masked, padding behavior passed, and every target ended in Qwen EOS.
- Run 5 predates structured paste authorship and therefore is only a long-context/token-shape test, not evidence for paste-target fidelity.

## Target authorship

### Paste

Implemented representation:

```text
typed text + <|paste|> + typed text + EOS
```

Rules:

- Clipboard state at write onset is causal conditioning; COPY is not a third event type.
- Cmd-V captures immediate pre/post editable states and the pasteboard version used.
- A raw post-paste window screenshot is retained for audit without becoming a READ.
- When AX omits a visibly rendered paste, one nonempty Cmd-V payload can be restored only from the unchanged conditioning clipboard, with exact caret, authored-text, checkpoint, and segment agreement. The weaker evidence is explicit as `keyboard_clipboard_without_ax_transition`.
- Current targets represent each proven paste as the reserved `<|paste|>` marker without its payload.
- Later WRITE history retains the resolved payload and marks it as paste-derived.
- Ambiguous paste-containing bursts remain events but are target-ineligible.
- Context-menu paste, drag-and-drop, and application-driven insertion are outside the initial proven-paste scope.

### Automatic formatting and generated text

The collector faithfully records app-generated document transitions such as Obsidian list markers. It remains undecided whether those characters should:

- receive content loss;
- become application-action markers;
- be normalized during compilation; or
- make the target ineligible.

Autocomplete, accepted model completions, dictation, and other externally generated insertions require the same authorship distinction.

### Editing commands

Current and intended Phase 1 policy:

- Backspace and Delete are folded into the final net edit.
- Pure deletions remain history events but are not content targets.
- Cursor movement and selection are conditioning state.
- A changed clipboard closes the current opportunity before the next mutation; COPY remains raw evidence, not a derived event.
- Proven Cmd-V paste is a grounded action in the target.
- A general motor-action policy is outside the current content-prediction objective.

## Phase 1 ablation readiness

The authoritative compiled examples remain tokenizer-independent. They retain the complete causally eligible event-ID sequence, compact serialized text, rich audit projection, conditioning query, structured authorship target, and resolved write content. Token packing is a separate reproducible artifact.

Ready from the current data and packer:

- **Time data:** compile the same frozen source with or without `--include-timestamps-in-context`; timestamps remain outside target loss. The comparison must reuse a common semantic event suffix so timestamp tokens do not indirectly remove more history from only one arm.
- **Qwen context scaling:** repack the same compiled examples at 8K, 16K, 32K, and 64K with `--input-token-budget`; event-aware truncation keeps the query and valid event records intact.
- **Behavioral-cloning target:** current labels implement authored text, grounded paste markers, and EOS only.

The substrate is ready, but an experiment harness is still required:

- **Learning objective:** `resolvedContent`, structured paste actions, and target lineage support either token NLL or a resolved-content semantic reward, but GRPO/RLOO and reward execution are not implemented.
- **Checkpoint recency:** event chronology supports identical daily scoring, but immutable daily model lineage, replay, and `d`, `d-1`, `d-3`, `d-7` scoring are not implemented.
- **Sliding window versus retrieval:** every causally available event remains addressable by ID, but BM25 query preprocessing, retrieval selection, and a frozen retrieval-plan artifact are not implemented.
- **Direct versus private reasoning:** the same input and final target can be reused, but scratchpad generation, final-answer isolation, latency accounting, and scoring are not implemented.
- **Continual Qwen versus closed ICL:** source examples are reusable, but training, provider adapters, and matched scoring are not implemented.
- **Open- and closed-model scaling:** model-specific tokenization is supported in principle, but the present packer selects the retained event suffix under each tokenizer. Before cross-model comparison, freeze a tokenizer-independent context plan containing the exact event IDs and serialized text so every model receives the same information.

No ablation requires changing the collector schema. The principal missing layer is a prospective experiment harness that freezes day boundaries, context/retrieval plans, model lineage, decoding, target resolution, scores, latency, and cost. Phase 2 will require a new conversion admitting displayed model proposals into the appropriate history; Phase 3 will additionally need stronger resource/world-state identity, which is not a Phase 1 collection blocker.

## Remaining requirements

### Before the next authoritative collection

The focused component gates are complete. Treat `phase1-causal-v12` and the current three-second delays/crop configuration as the candidate baseline for an ordinary-work audit run.

1. Run normal work without changing collector rules mid-session.
2. Compile the session with `phase1-causal-v12` and preserve the source digests and audit outputs.
3. Manually sample the temporal trace against the actual work and record Phase 1's fidelity categories: missing events, temporal-ordering errors, incorrect content inclusion, authorship errors, write-boundary disagreement, destination ambiguity, and future leakage.
4. Quantify target eligibility and exclusion reasons, including pure deletion, unresolved paste authorship, incomplete semantic cursor state, and rejected reconstruction.
5. Fix only recurrent material errors demonstrated by that trace. If no training-blocking class appears, freeze the collector/configuration/conversion as the first immutable dataset version.

### Before initial offline training

1. Pass the ordinary-work reconstruction audit above and freeze an immutable dataset version.
2. Verify the training harness consumes the packed `labels` unchanged and the inference parser executes only a complete decoded `<|paste|>` marker.
3. Run the initial Phase 1 smoke test on eligible writes from the combined Obsidian, Chrome/browser, Codex, and VS Code stream, reporting aggregate and per-application results before the longer prospective continual experiment.

### Before live prediction

1. Capture destination, cursor context, and clipboard state when focus arrives.
2. Refresh that query when cursor or selection changes.
3. Measure drift between focus-time state and pre-mutation training state.
4. Render action markers such as `<|paste|>` as UI actions rather than literal text.
5. Preserve displayed model predictions as raw, Phase 1-excluded events.

## Important non-blocking fidelity improvements

- Structured Chrome URLs and field identity.
- Obsidian note paths.
- Surface-specific viewport crops.
- Better sentence-boundary handling for OCR.
- YouTube transcript and subtitle exposure.
- Reduced raw suppression churn during window animation.
- More reliable differentiation of titles, body fields, prompts, and transient browser UI.

## Next step

Run one ordinary-work candidate session with the current baseline, then compile and audit it before changing capture behavior. This tests the Thesis claim at the right level: whether the interleaved stream faithfully preserves the information-to-authored-output conversion during natural work, rather than whether isolated sensors can pass synthetic cases. Focus-time conditioning remains required before live prediction, but it is not a blocker for this offline training-data audit because the current pre-mutation query is causally valid for completed examples.

# Coupled checkpoint

Last committed implementation: `228e763`, built on the grounded-paste implementation in `bcec3bc`. The focused paste/read-attribution and reverted-write acceptance traces have passed. This is now the candidate baseline for the next ordinary-work fidelity audit; further collector changes are not treated as complete here until committed and validated.

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

Operation, removal, offsets, destination, timestamps, cursor metadata, and pasted payload tokens do not receive target loss. A proven paste action receives loss as one atomic token; its resolved payload remains available in the WRITE event and in later history.

## Implemented collection

### WRITE capture

Supported applications:

- Obsidian;
- Chrome;
- Codex/ChatGPT.

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

## Implemented causal compilation

Current compiler: `phase1-causal-v9`

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
- appends pre-mutation destination/cursor/clipboard conditioning as the query;
- emits structured authored-text and grounded paste-action targets;
- excludes pure deletions;
- records target and context exclusions explicitly;
- audits lineage back to source records.

For range-native semantic cursor contexts, complete semantic conditioning remains eligible even when unreliable numeric AX offsets disagree. Numeric offsets are diagnostic and do not alter reconstruction or impose a separate target gate. Phase 1 now states the same rule.

## EOS status

Implemented:

- Stored compiler targets remain tokenizer-independent structured segments.
- The loader contract tokenizes authored spans, maps each paste action to one atomic token, and appends exactly one EOS.
- Authored tokens, paste-action tokens, and EOS receive loss; pasted payload tokens do not.
- Dataset checks enforce the contract.

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
- successful `phase1-causal-v9` compilation and causal audit.

It exposed one typed checkpoint that was fully deleted before settlement but was resurrected as a WRITE. Commit `228e763` removed that fallback and added a compiler-side rejection for older traces.

`reverted-write-test-1` then confirmed:

- identical settled `BEFORE` and `AFTER` produce raw `resolution: no_change`;
- no derived WRITE or proposed event ID is emitted;
- the causal compiler produces no training example for the burst;
- the corrected schema versions and causal audit pass.

## Target authorship

### Paste

Implemented representation:

```text
typed text + <|paste|> + typed text + EOS
```

Rules:

- Clipboard state at write onset is causal conditioning; COPY is not a third event type.
- Cmd-V captures immediate pre/post editable states and the pasteboard version used.
- Current targets represent each proven paste as one atomic action without its payload.
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

## Remaining requirements

### Before the next authoritative collection

The prior focused component gates are complete. A same-editable cursor-relocation boundary has now been implemented on top of `228e763` and must pass one focused acceptance trace before the baseline is used for an ordinary-work audit run.

1. Confirm that typing at one location, relocating the caret within `WRITE_DELAY`, and typing elsewhere in the same editable produces two independently conditioned WRITEs.
2. Run normal work without changing collector rules mid-session.
3. Compile the session with `phase1-causal-v9` and preserve the source digests and audit outputs.
4. Manually sample the temporal trace against the actual work and record Phase 1's fidelity categories: missing events, temporal-ordering errors, incorrect content inclusion, authorship errors, write-boundary disagreement, destination ambiguity, and future leakage.
5. Quantify target eligibility and exclusion reasons, including pure deletion, unresolved paste authorship, incomplete semantic cursor state, and rejected reconstruction.
6. Fix only recurrent material errors demonstrated by that trace. If no training-blocking class appears, freeze the collector/configuration/conversion as the first immutable dataset version.

### Before initial offline training

1. Pass the ordinary-work reconstruction audit above and freeze an immutable dataset version.
2. Connect the tested target-loader contract to the chosen model tokenizer, register one atomic paste token distinct from EOS, and ensure its embedding/output parameters are trainable and saved.
3. Verify packed examples preserve the right-edge destination/cursor/clipboard query, map each proven paste to one token, append exactly one EOS, and mask loss from all model-input and pasted-payload tokens.
4. Run the Obsidian-only Phase 1 smoke test before the full prospective interleaved-stream experiment.

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

Validate the same-editable cursor-relocation boundary with one short trace. If it passes, run one ordinary-work candidate session with that baseline, then compile and audit it before changing capture behavior. This tests the Thesis claim at the right level: whether the interleaved stream faithfully preserves the information-to-authored-output conversion during natural work, rather than whether isolated sensors can pass synthetic cases. Focus-time conditioning remains required before live prediction, but it is not a blocker for this offline training-data audit because the current pre-mutation query is causally valid for completed examples.

# Coupled checkpoint

Base commit for this checkpoint: `9f0457b`

## Objective

Construct a high-fidelity causal stream approximating:

```text
information the user was exposed to
+ current destination and semantic cursor state
→ next user-generated written content
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
target = user-generated content or grounded action markers + EOS
```

Operation, removal, offsets, destination, timestamps, and cursor metadata do not receive target loss.

## Implemented collection

### WRITE capture

Supported applications:

- Obsidian;
- Chrome;
- Codex/ChatGPT.

Mechanism:

1. Intercept the first mutating input.
2. Capture the focused editable's complete `BEFORE` state before returning the input to the application.
3. Reset `WRITE_DELAY` after each subsequent input.
4. Capture the held editable's terminal state.
5. Derive the canonical minimum contiguous `BEFORE → AFTER` edit.
6. Preserve the complete attempt and checkpoints in raw evidence.

Implemented safeguards:

- Cursor position does not influence diff reconstruction.
- The synthetic `BEFORE → empty` deletion fallback has been removed.
- Invalid terminal elements become unresolved unless a valid Return or paste checkpoint exists.
- A temporary typed checkpoint which returns to the original settled field state is no change, not a resurrected WRITE.
- Immediate Return is handled with synchronous pre-Return checkpoints.
- Clipboard state is captured with pre-mutation conditioning.
- Cmd-V payload plus immediate pre/post-paste field states are retained raw.
- Large deletions must be supported by complete observed `BEFORE` and `AFTER` states.
- Numeric cursor fidelity remains diagnostic and does not alter the diff.

### Semantic cursor conditioning

Each WRITE attempts to retain:

- application and bundle;
- window and Accessibility role;
- field label or description when available;
- left context, selected text, and right context;
- field state and prompt state.

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

For range-native semantic cursor contexts, current compiler behavior accepts complete semantic conditioning even when unreliable numeric AX offsets disagree. Phase 1 text that still requires numeric offset agreement should be updated to match this decision.

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

The helper-window issue was fixed in `9f0457b` but has not yet been validated in a new live trace.

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

1. Validate the auxiliary-window activation fix.
2. Inspect another short trace for READ attribution and active-write exclusion.
3. Validate typed, paste-only, and mixed Cmd-V writes against the new authorship fields.
4. Freeze the next compiler version only after that trace passes.

### Before initial offline training

1. Connect the tested target-loader contract to the chosen model tokenizer.
2. Reconcile Phase 1's cursor-eligibility language with compiler behavior.
3. Audit sampled examples for authorship, boundaries, future leakage, and destination correctness.
4. Freeze the conversion version and immutable dataset.

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

Run a short activation test against `9f0457b`:

1. Cmd-Tab into Obsidian with the pointer stationary.
2. Wait longer than `READ_DELAY`.
3. Cmd-Tab into Chrome with the pointer stationary.
4. Wait again.
5. Confirm both READs use full content-window bounds and matching OCR.

Do not stack additional collector changes until this trace is inspected.

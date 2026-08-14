# Coupled checkpoint

Implementation baseline: `9f0457b`

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
- Immediate Return is handled with synchronous pre-Return checkpoints.
- Paste payload and post-paste state are retained raw.
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

Current compiler: `phase1-causal-v8`

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
- appends pre-mutation destination/cursor conditioning as the query;
- emits plain-text content targets;
- excludes pure deletions;
- records target and context exclusions explicitly;
- audits lineage back to source records.

For range-native semantic cursor contexts, current compiler behavior accepts complete semantic conditioning even when unreliable numeric AX offsets disagree. Phase 1 text that still requires numeric offset agreement should be updated to match this decision.

## EOS status

Implemented:

- Stored targets contain only source content.
- Dataset metadata requires exactly one EOS token.
- EOS receives loss.
- Dataset checks enforce the contract.

Not implemented:

- A tokenizer-specific training loader that disables automatic special tokens, appends exactly one `eos_token_id`, and constructs labels with loss only on target content and EOS.

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

## Unresolved target-authorship questions

### Paste

Current behavior is not training-ready: literal pasted payloads still become content targets.

Proposed representation:

```text
typed text + <|paste|> + typed text + EOS
```

Requirements:

- capture clipboard-copy state as causal context;
- retain literal clipboard payload raw;
- capture editable state immediately before and after every paste;
- represent pasted spans as grounded actions;
- apply loss to `<|paste|>`, not copied payload tokens;
- preserve resolved final content for audit and later history;
- exclude ambiguous paste-containing bursts until segmentation is reliable.

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
- Copy should update clipboard/context state; standalone copy capture is not yet implemented.
- Paste should eventually become a grounded action marker.
- A general motor-action policy is outside the current content-prediction objective.

## Remaining requirements

### Before the next authoritative collection

1. Validate the auxiliary-window activation fix.
2. Inspect another short trace for READ attribution and active-write exclusion.
3. Decide on a conservative interim paste policy, likely excluding paste-containing targets.
4. Freeze the next compiler version only after that trace passes.

### Before initial offline training

1. Implement the tokenizer-specific EOS loader.
2. Implement or conservatively exclude paste targets.
3. Reconcile Phase 1's cursor-eligibility language with compiler behavior.
4. Audit sampled examples for authorship, boundaries, future leakage, and destination correctness.
5. Freeze the conversion version and immutable dataset.

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

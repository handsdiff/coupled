# Checkpoint after normal-work-dry-run-5

## Implemented after this checkpoint

- **Baseline frozen:** Commit `fbb4fa8` preserves the post-run-5 content-only compiler and documentation before reconstruction changed.
- **Cursor-independent reconstruction:** Commits after the baseline derive every write from the canonical minimum contiguous `BEFORE → observed AFTER` diff. Cursor metadata no longer changes edit boundaries.
- **Synthetic deletion fallback removed:** The live collector no longer turns a delete-only transition into `BEFORE → empty`. The v6 compiler repairs the two known run-5 fallback events from their raw observed states.
- **Cursor fidelity measured independently:** Raw attempts and derived writes now compare the initial AX cursor with the earliest retained post-input mutation and the terminal edit. Only `aligned` writes become initial Phase 1 targets; mismatches remain causally available history and audit evidence.

## Implemented decisions

- **Content-only objective:** Phase 1 predicts content, not destination, cursor position, operation, offset, removed text, provenance, or other write-event metadata. Those fields remain conditioning state or audit evidence and receive no loss.
- **Cursor and destination conditioning:** Offline examples include the pre-mutation application/window/field metadata, initial selection, and bounded semantic context surrounding the cursor.
- **EOS contract:** The source content remains unchanged. The tokenizer-specific training loader tokenizes the target with automatic special tokens disabled, appends exactly one `eos_token_id`, and applies loss to every target token including EOS.
- **Focus-time requirement:** Before live prediction, the interface must capture the equivalent destination and semantic cursor state when focus arrives. Current pre-mutation conditioning is sufficient for offline experiments but occurs too late for focus-time serving.

## Reconstruction fixes completed in the next slice

- **Removed cursor-biased diffing:** Initial cursor position does not alter the independently reconstructed `BEFORE → AFTER` edit. Cursor state conditions prediction; it is not evidence that can override the observed diff.
- **Added cursor-fidelity evidence:** Each attempt records whether the initial AX cursor agrees with the earliest retained mutation and terminal edit. This measures rather than assumes rich-editor cursor reliability.
- **Removed the synthetic deletion fallback:** The collector never replaces an observed terminal state with a synthetic `BEFORE → empty` edit.

## Remaining capture fixes

- **Fix delayed read attribution:** Immediately before a delayed screenshot, re-resolve and validate the app, window, display, and relevant bounds. Suppress or restart the candidate if they no longer match, rather than attaching stale Obsidian or Chrome metadata to pixels from another app.
- **Prevent authorship leakage into reads:** A read captured while a write is active can contain partially typed human output and incorrectly label it as inbound information. The causal compiler prevents it from conditioning that same write, but it can still contaminate later history. Such content must be suppressed, attributed as authored output, or otherwise excluded from ordinary READ events.

## Paste representation

- **Preserve exact evidence:** Keep the literal pasted payload, clipboard provenance, field observations, and exact resulting `write.content` in raw/derived evidence.
- **Use an authorship-aware training target:** The training target should retain typed spans verbatim while replacing each pasted span with a learned action marker such as `<|paste|>`. Literal pasted payload tokens should not receive content-generation loss.
- **Apply loss to actions and authored content:** Typed tokens, `<|paste|>`, and the final EOS receive loss. Destination, cursor state, edit metadata, and literal pasted payload do not.
- **Support mixed bursts:** Capture the editable state immediately before and after each paste so the conversion can distinguish typed and pasted spans reliably. A burst such as `please review ` followed by a paste should become `please review <|paste|><|eos|>`, while the copied text remains available as evidence and later attributed history.
- **Tokenizer implementation:** If `<|paste|>` is a newly added tokenizer token, ensure its embedding and output-head parameters are trainable and saved; otherwise use a suitable existing reserved token.

## Resulting target boundary

The event stream preserves the exact observed write and its provenance. The Phase 1 training conversion produces an authorship-aware target consisting only of user-authored text and explicit learned actions, followed by exactly one loader-appended EOS token.

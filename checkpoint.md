# Checkpoint after normal-work-dry-run-5

## Implemented decisions

- **Content-only objective:** Phase 1 predicts content, not destination, cursor position, operation, offset, removed text, provenance, or other write-event metadata. Those fields remain conditioning state or audit evidence and receive no loss.
- **Cursor and destination conditioning:** Offline examples include the pre-mutation application/window/field metadata, initial selection, and bounded semantic context surrounding the cursor.
- **EOS contract:** The source content remains unchanged. The tokenizer-specific training loader tokenizes the target with automatic special tokens disabled, appends exactly one `eos_token_id`, and applies loss to every target token including EOS.
- **Focus-time requirement:** Before live prediction, the interface must capture the equivalent destination and semantic cursor state when focus arrives. Current pre-mutation conditioning is sufficient for offline experiments but occurs too late for focus-time serving.

## Remaining reconstruction and capture fixes

- **Remove cursor-biased diffing:** Initial cursor position must not alter the independently reconstructed `BEFORE → AFTER` edit. Cursor state conditions prediction; it is not evidence that can override the observed diff. Cursor-biased reconstruction can include unchanged earlier text in the target, such as combining the preceding line with the new write.
- **Validate cursor fidelity:** Record whether the AX cursor coordinates agree with the first observed mutation. Rich-editor coordinates, particularly in Obsidian and some Chrome editors, cannot yet be assumed reliable.
- **Remove the synthetic deletion fallback:** Do not replace an observed terminal state with a synthetic `BEFORE → empty` edit. This fallback produced false 22K–25K-character whole-document deletions. Derive from observed evidence or mark the attempt unresolved.
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

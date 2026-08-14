# Checkpoint after normal-work-dry-run-5

## Implemented after this checkpoint

- **Baseline frozen:** Commit `fbb4fa8` preserves the post-run-5 content-only compiler and documentation before reconstruction changed.
- **Cursor-independent reconstruction:** Commits after the baseline derive every write from the canonical minimum contiguous `BEFORE → observed AFTER` diff. Cursor metadata no longer changes edit boundaries.
- **Synthetic deletion fallback removed:** The live collector no longer turns a delete-only transition into `BEFORE → empty`. The compiler repairs the two known run-5 fallback events from their raw observed states.
- **Cursor fidelity measured independently:** Raw attempts and derived writes compare the initial AX cursor with the earliest retained post-input mutation and the terminal edit. That comparison remains audit evidence and does not alter reconstruction. Legacy sessions without range-native context retain the earlier conservative alignment gate.
- **Semantic cursor positioning validated and promoted:** New sessions use Accessibility range-native left, selected, and right text as model conditioning across Obsidian, Codex, and Chrome. Numeric AX coordinates remain diagnostic only. Known empty Codex and Gemini prompt chrome is represented separately from editable context. Legacy sessions retain their conservative numeric eligibility rule.
- **Write input now supersedes stale read timers:** A mutating key invalidates pending pointer-based reads on the same Core Graphics window before they can capture partially authored output. Raw suppression evidence links the candidate to its write attempt, and compiler v8 repairs the equivalent timing pattern in older sessions.
- **Delayed reads now require a stable capture surface:** Trigger-time identity is retained only as provenance. App/window/display/title/bounds are resolved again at settlement and used together for screenshot attribution, cropping, OCR, and deduplication. A changed surface starts a fresh delay instead of inheriting an old timer; app activation and post-click observation provide transition boundaries; stale per-window candidates are collapsed; and a transition during screenshot completion suppresses the derived read.
- **Activation ignores auxiliary windows:** An app-activation interval selects a plausible content window beneath the pointer, falling back to the app's largest plausible window. Tiny scrollbar, tab-strip, and renderer-helper surfaces cannot become the activation READ target.

## Implemented decisions

- **Content-only objective:** Phase 1 predicts content, not destination, cursor position, operation, offset, removed text, provenance, or other write-event metadata. Those fields remain conditioning state or audit evidence and receive no loss.
- **Cursor and destination conditioning:** Offline examples include the pre-mutation application/window/field metadata, initial selection, and bounded semantic context surrounding the cursor.
- **EOS contract:** The source content remains unchanged. The tokenizer-specific training loader tokenizes the target with automatic special tokens disabled, appends exactly one `eos_token_id`, and applies loss to every target token including EOS.
- **Focus-time requirement:** Before live prediction, the interface must capture the equivalent destination and semantic cursor state when focus arrives. Current pre-mutation conditioning is sufficient for offline experiments but occurs too late for focus-time serving.

## Reconstruction fixes completed in the next slice

- **Removed cursor-biased diffing:** Initial cursor position does not alter the independently reconstructed `BEFORE → AFTER` edit. Cursor state conditions prediction; it is not evidence that can override the observed diff.
- **Added cursor-fidelity evidence:** Each attempt records whether the initial AX cursor agrees with the earliest retained mutation and terminal edit. This measures rather than assumes rich-editor cursor reliability.
- **Removed the synthetic deletion fallback:** The collector never replaces an observed terminal state with a synthetic `BEFORE → empty` edit.

## Paste representation

- **Preserve exact evidence:** Keep the literal pasted payload, clipboard provenance, field observations, and exact resulting `write.content` in raw/derived evidence.
- **Use an authorship-aware training target:** The training target should retain typed spans verbatim while replacing each pasted span with a learned action marker such as `<|paste|>`. Literal pasted payload tokens should not receive content-generation loss.
- **Apply loss to actions and authored content:** Typed tokens, `<|paste|>`, and the final EOS receive loss. Destination, cursor state, edit metadata, and literal pasted payload do not.
- **Support mixed bursts:** Capture the editable state immediately before and after each paste so the conversion can distinguish typed and pasted spans reliably. A burst such as `please review ` followed by a paste should become `please review <|paste|><|eos|>`, while the copied text remains available as evidence and later attributed history.
- **Tokenizer implementation:** If `<|paste|>` is a newly added tokenizer token, ensure its embedding and output-head parameters are trainable and saved; otherwise use a suitable existing reserved token.

## Resulting target boundary

The event stream preserves the exact observed write and its provenance. The Phase 1 training conversion produces an authorship-aware target consisting only of user-authored text and explicit learned actions, followed by exactly one loader-appended EOS token.

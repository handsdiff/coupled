# Coupled checkpoint

The candidate architecture is now raw-first:

```text
lossless sensor evidence
→ versioned Phase 1 semantic READ/WRITE reduction
→ causal examples
→ tokenizer-specific packing and training
```

The live stream is a provisional monitor, not training authority. Further
collector or reducer changes are not complete until their raw-lineage,
determinism, and regression gates pass.

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

- `raw.jsonl` preserves screenshots, OCR, Accessibility states, input timing, clipboard evidence, checkpoints, and capture dispositions. WRITE evidence is persisted before preview interpretation.
- `events.preview.jsonl` contains provisional live READ and WRITE interpretations and is never a training input.
- `coupled reduce` produces finalized `events.jsonl`, explicit `unresolved.jsonl`, and `reduction.json` from raw evidence.
- The causal compiler verifies reducer/raw integrity and produces chronologically valid history and candidate training examples; it does not repeat semantic reduction.
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

Event validity and target eligibility are separate. Text-only targets require at least four trimmed Swift `Character` values; shorter WRITEs remain in causal history but receive no target loss. Grounded paste-only and mixed paste targets bypass this minimum because the paste action itself is loss-bearing. The compiler records the threshold in the dataset manifest and records each such exclusion as `authored_content_below_minimum_length`.

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
5. Persist the complete attempt and checkpoints to `raw.jsonl`.
6. Derive a provisional live preview; the offline reducer later makes the authoritative versioned decision.

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
compact provisional-event stream as `./scripts/coupled logs`. During a live run it
also displays that run's immutable resolved `session.json` settings. It has no
capture controls and retains only a bounded recent text buffer. Both
`com.niyant.coupled` and `com.niyant.coupled.logs` are permanently excluded from
READ and WRITE collection. The authoritative collection artifact is `raw.jsonl`;
the stdout mirror and `events.preview.jsonl` are debugging aids. The viewer must not overlap a captured work
surface because semantic exclusion cannot remove pixels already covering a
rectangular screenshot.

## Implemented semantic reduction and causal compilation

Current compiler: `phase1-causal-v14`
Current reducer: `phase1-semantic-v6`

The reducer consumes only `session.json` and `raw.jsonl`; deleting or corrupting
`events.preview.jsonl` produces byte-identical finalized events. Event IDs are
stable across reducer versions because they derive from session ID, ordered raw
lineage, and output ordinal. `reduction.json` binds the session, raw stream,
finalized events, and unresolved records by SHA-256.

Against `ordinary-work-audit-3`, semantic v3 recovered both Gemini submissions
from synchronous pre-Return observations, rejected the impossible 3,263-character
Obsidian expansion from a delete-only burst, produced 303 READs and 186 WRITEs,
and recorded 92 non-event dispositions (both deliberate filters and unresolved
evidence). Seven delayed READs whose trigger preceded a WRITE but whose capture
landed inside it are now reducer dispositions; a genuine READ triggered during
a longer WRITE remains in history. Causal v13 then produced 101
training examples, 85 target exclusions, zero context exclusions, and zero
integrity rejections; the causal audit passed.

Against `semantic-v2-validation-2`, semantic v3 reconstructs the demonstrated
middle insertion as `" then typing in between here"` while retaining the
equivalent canonical pixel-state diff `"hen typing in between here t"` as
`observedNetEdit`. All other validation writes and all 186 writes in
`ordinary-work-audit-3` remain semantically unchanged.

Against `normal-work-dry-run-6`, semantic v4 repairs the demonstrated
catastrophic Obsidian AX epoch jump by selecting a complete checkpoint captured
after the final input only when the terminal transition replaces at least 256
characters on both sides and the recent checkpoint trajectory remains locally
coherent. It excludes one noncontiguous Obsidian formatting transition, one
READ containing a 99-character exact normalized prefix of an active WRITE, two
fast-start WRITEs whose BEFORE already contained the first mutation, and six
segmented verification-code fields. The result is 157 READs, 92 WRITEs, and 84
non-event dispositions. Compared with semantic v3, exactly ten bad events are
removed, the repaired Obsidian event retains its stable ID, and every other
event is byte-identical apart from sequence renumbering. Repeated reduction is
byte-identical. Causal v13 produces 81 training examples, 11 pure-deletion
target exclusions, zero context exclusions, and zero integrity rejections; the
causal audit passes.

Against `normal-work-dry-run-7`, semantic v5 keeps Cut transitions in later
history while assigning them no authored segments, uses the post-input Cut
checkpoint when the terminal AX state changes epochs, and rejects an impossible
Cut expansion when no contracting observation exists. It recovers eleven
additional paste-containing WRITEs from later ordered same-field observations;
each local transition contains the exact conditioned clipboard payload once and
only structural whitespace around it. The three terminal fragments `./sc`,
`cou`, and `stop` reduce to one proven same-editable autocomplete completion,
`./scripts/coupled stop`, while a selection-only cursor relocation fixture
remains two WRITEs. No unrelated semantic-v4 event changes apart from sequence
renumbering. The replay produces 37 READs, 67 WRITEs, and 18 non-event
dispositions. Causal v13 produces 25 training examples, 42 target exclusions,
zero context exclusions, and zero integrity rejections; the causal audit passes.

Against `normal-work-dry-run-8`, semantic v6 extends the navigation rule only
when the same retained editable proves either application completion or an
exactly unchanged value, caret, and selection. Five raw terminal attempts now
reduce from the original empty field to one `./scripts/coupled stop` WRITE;
observable cursor relocation remains a boundary. Six Obsidian paste transitions
with complete reconstructible document states but unresolved authorship remain
context-only WRITEs with `unresolved_paste_transition` segments and never become
targets. Their six former non-event dispositions disappear. Every common v5
event retains the same stable ID and identical semantic fields apart from
sequence renumbering. The replay produces 67 READs, 63 WRITEs, and 18 non-event
dispositions. Causal v13 produced 31 training examples, 32 target exclusions,
zero context exclusions, and zero integrity rejections; the causal audit passes.
Its unchanged Qwen tokenizer packed all 31 examples with six grounded paste
actions, one loss-bearing EOS per target, zero discarded input tokens, and a
passing packed-dataset audit. Unresolved paste-transition text remains history
only and is never converted into a target marker.

Phase1-causal-v14 adds one compiler-only eligibility policy: without a grounded
paste action, trimmed authored content must contain at least four Swift
`Character` values. This moves exactly `"I"`, `"for"`, and the whitespace-only
WRITE from loss-bearing targets to explicit exclusions while retaining all
three WRITEs in later causal history. Grounded paste-only and mixed paste
targets remain eligible. Run 8 therefore contains 28 training examples, 35
target exclusions, zero context exclusions, and zero rejections. Repeated
causal compilation is byte-identical, the causal audit passes, and the Qwen
pack contains the same six grounded paste actions with one loss-bearing EOS per
target and zero discarded input tokens.

### Canonical Run 8 semantic and training freeze record

The canonical semantic reduction was generated from reducer implementation
commit `c659a29eae5bb171e5b701b80e761805e981c7e2`. The canonical v14 causal dataset
and pack were generated from compiler implementation commit
`bbe5e75262031c06a0ca5c1824f01c02fc02e09b`. Running `check.sh` validates the
source but does not refresh `.build/debug/coupled`; release generation must run
`./scripts/build.sh` first and use that freshly built executable.

Canonical semantic reduction:
`coupled-data/normal-work-dry-run-8-phase1-events-v6-c659a29-canonical`

```text
events.jsonl       b3886d4dffda1609ba8194037141b47ef38df67a7d63a5349a700644ed9f935b
reduction.json     4e316f2d0a6f914f63e134089af97e0805c137192f5ef37289e1319be52ccf77
unresolved.jsonl   e343628b7c45010dcbc7a198397e58aa8a54d425197bfdd4d614015c9a682a87
```

Canonical causal dataset:
`coupled-data/normal-work-dry-run-8-phase1-v14-v6-bbe5e75-canonical`

```text
context-exclusions.jsonl e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
dataset.json              ad531e39f05e441e16186f0b00c0d7a0b5c3814d95cdebccdc0d83170f66e873
events.jsonl              e4dfd323820d215e35ed64b8835716bfc47bc9c176267786e41186b21dc073e2
examples.jsonl            9a3d5d31a700e81135610740b44659a20ae415120e61e817a15b69db5366f677
rejections.jsonl          e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
target-exclusions.jsonl   4a3a6fb9528060b04cb74c47503af3766a4de0e07f0bc517a7fce94e0ad55e9c
```

Canonical Qwen pack:
`coupled-data/normal-work-dry-run-8-phase1-v14-v6-bbe5e75-canonical-qwen-pack`

```text
packed-examples.jsonl a7d9457d87c6bc7c7cb461a358476b4caf06628e1f4132f32dbc68bde7ce5ef0
packing.json          9e18e82cf91ad2f25b56970cadbb5752209535f1edfb6366638428573f662dfb
```

The earlier `normal-work-dry-run-8-phase1-events-v6-final`,
`normal-work-dry-run-8-phase1-v13-v6-final`, and associated `-qwen-pack`
artifacts are superseded. The initially generated `-c659a29` artifacts without
the `-canonical` suffix are also superseded because they were produced by a
stale debug executable. The canonical v13 causal dataset and pack are valid
prior-policy artifacts but are superseded by v14 for initial training. None of
those superseded paths are current training authority.

Timing:

```text
read.available_at        = capturedAt
target_write.began_at    = beganAt
prior_write.available_at = terminalDecisionAt

context(target) =
    stable_sort(events where event.available_at < target.began_at)
```

The raw-input semantic reducer:

- reads raw evidence without consulting the provisional preview;
- embeds raw lineage, selected observation, rule, and decision reason in every event;
- writes only `events.jsonl`, `unresolved.jsonl`, and `reduction.json`;
- recovers transient submitted fields from pre-Return checkpoints;
- rejects delete-only transitions which appear to insert content;
- retains Cut-only document transitions with empty authored completion and no
  target loss, preferring a contracting post-input observation over a later AX
  epoch change;
- resolves a delayed paste only from an ordered same-field observation with one
  exact conditioned clipboard span and structural surrounding text;
- retains a complete reconstructible but authorship-ambiguous paste transition
  in later history with explicit unresolved provenance and no target loss;
- composes same-editable navigation attempts only when the next BEFORE proves
  either an end-of-field application completion or exactly unchanged value,
  caret, and selection;
- bridges AX observation epochs only across proven clipboard-matched Cmd-V evidence;
- independently recomputes READ surface-race and Chrome auxiliary-window eligibility;
- removes a stale delayed READ before overlap only when its trigger predates a
  WRITE and its capture lands within that WRITE interval; new activity during a
  long WRITE remains eligible;
- applies adjacent READ overlap in semantic time using READ `capturedAt` and
  finalized WRITE `beganAt`, independent of asynchronous raw append order;
- uses ordered mutation observations only to disambiguate equivalent inline
  BEFORE-to-AFTER edits, without admitting temporary corrected text or crossing
  newline/zero-width structural boundaries;
- leaves ambiguous evidence unresolved instead of guessing.

The compiler:

- verifies reducer artifacts, stable event IDs, raw lineage, selected observations, and all source hashes without duplicating semantic reduction;
- retains stale-read exclusion only in the schema-14 compatibility importer;
- serializes causal READ/WRITE history;
- uses compact model-facing history while retaining the richer canonical projection as `auditSerialized`;
- serializes structured historical WRITE text exactly once in provenance-bearing authorship segments;
- appends pre-mutation destination/cursor/clipboard conditioning as the query;
- emits structured authored-text and grounded paste-action targets;
- excludes pure deletions;
- excludes text-only targets below the versioned minimum trimmed authored-content length while retaining their WRITEs in causal history;
- records target and context exclusions explicitly;
- audits lineage back to source records.

Historical schema-14 sessions retain a compatibility importer for regression
only. New schema-15 sessions write `events.preview.jsonl`, so they cannot bypass
the reducer.

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

## Mechanical training-harness preflight

The provider-neutral local training contract is now implemented in
`scripts/phase1_training_contract.py`. It validates the frozen pack and maps
each aligned causal-LM row to the shifted token-level representation expected
by Tinker:

```text
model_input[i] = packed.inputIDs[i]
target[i]      = packed.inputIDs[i + 1]
weight[i]      = 1 iff packed.labels[i + 1] is loss-bearing
```

Every weighted position must satisfy
`target[i] == packed.labels[i + 1]`. The repository check includes a
regression that rejects a one-token label mismatch and incorrect target-token
accounting.

The canonical Run 8 pack passed local preflight without importing the Tinker
SDK, authenticating, accessing the network, or transmitting data:

- 28 shifted datums;
- 482,281 packed tokens before the causal shift;
- 482,253 submitted positions per epoch after removing one position per datum;
- 203 loss-bearing positions and 482,050 zero-weight positions;
- maximum submitted datum length 30,050 tokens;
- exact local frozen-tokenizer checks for vocabulary length, EOS, padding,
  `<|paste|>` encoding, and representative Unicode round trips.

At the manually verified August 17, 2026 training price of $1.463 per million
submitted tokens, the projected training-only cost is $0.705536 per epoch,
$7.055361 for ten epochs, or $14.110723 for twenty epochs. Evaluation,
sampling, storage, taxes, and any changed pricing are excluded.

This validates only the mechanical data/label contract. It does not show that
the Phase 1 hypothesis works. A later separately authorized gate must verify
the server-side tokenizer without submitting the dataset, use a dedicated
private Tinker project rather than its default project, and then stop again
before any personal data transfer. An authorized training smoke must save both
sampler weights and full optimizer state and record actual usage and cost.

The authenticated tokenizer-only gate is implemented with Tinker SDK 0.25.0.
It requires an explicit dedicated-project UUID and confirmation flag, never
constructs a training `Datum`, and compares the complete token-to-ID vocabulary
plus every unique token ID and complete decoded sequence appearing in the
frozen pack. Its report distinguishes the tokenizer revision fetched by the
SDK from the server model-weight revision, which Tinker does not expose.

The first authenticated attempt reached Tinker and confirmed the requested
model through server capabilities, but creation of the tokenizer-bearing base
model client was blocked with HTTP 402 because the organization had no usable
billing balance. No packed tokens, labels, human content, sampling request, or
training request were transmitted. The command must be retried after billing
is enabled; the hardened retry records sanitized authentication, billing, and
permission failures without persisting the API key or raw server error.

After billing was enabled, the authenticated retry passed from clean commit
`3dfc597` in the dedicated private project:

- Tinker reported `Qwen/Qwen3.5-9B-Base` with 65,536-token context;
- all 248,077 complete token-to-ID vocabulary entries matched, with mapping
  SHA-256 `5f7bdf3d3fbddbdb7571c8fa268146cb84d82d4c1652eadf1a0e5420295d1dc3`;
- all 4,110 unique token IDs appearing in the frozen pack matched;
- all 28 complete packed sequences decoded identically;
- EOS, padding, representative encodings, and the five-token `<|paste|>`
  encoding matched;
- report SHA-256 is
  `87689efc2d9d70d995564d908d596a55eaedad2f61379617e2173d8ef577e6c4`.

Tinker exposed the correct model repository but no underlying server
model-weight revision, so that revision remains explicitly unverified. The
SDK's tokenizer loader likewise exposed no resolved Hugging Face revision; the
complete mapping and actual-pack comparisons are the compatibility evidence.
The preflight created a project-scoped base-model sampling client only to fetch
model/tokenizer metadata. It did not construct a `Datum`, transmit packed
tokens, labels, or human content, sample, train, or create a checkpoint.

The data-bearing smoke procedure is now frozen one gate further, still without
executing it. `scripts/prepare-phase1-tinker-overfit.py` imports the pinned SDK
locally, constructs every real Tinker `Datum`, and exhaustively round-trips its
`ModelInput`, `int64` target tensor, and `float32` weight tensor against the
provider-neutral causal-shift contract. The command contains no API-key,
`ServiceClient`, network, training, sampling, or checkpoint path.

The prepared execution plan is deliberately fixed rather than self-expanding:

- Qwen 3.5 9B Base with rank-32 LoRA, seed 17, and Adam at learning rate
  `2e-4`;
- 20 epochs, batch size one, 560 optimizer steps, and deterministic
  per-epoch SHA-256 ordering;
- 9,645,060 maximum submitted training positions;
- weighted base/final NLL, exact greedy generation for all 28 targets, exact
  paste-marker and EOS checks, and three-example state-reload parity;
- separate trained sampler, full optimizer-state, and reloaded-state sampler
  checkpoints with seven-day TTL;
- no automatic retries or extra epochs if acceptance fails.

At the August 17 price snapshot, training projects to `$14.110723`, bounded
prefill evaluation to `$0.997970`, and bounded sampled output to `$0.000565`.
The plan adds a `$1.00` checkpoint/storage reserve, for `$16.109258` projected
total under a `$20.00` reviewed ceiling. Prices must be reverified before an
execution path is authorized, and actual billing must be recorded afterward.
The local SDK integration and repository regressions pass with 28 exact Datums,
482,253 submitted positions per epoch, and 203 loss-bearing positions.

The canonical clean-tree preparation was generated from commit `cd5d30c` at:

```text
coupled-data/normal-work-dry-run-8-phase1-tinker-overfit-plan-cd5d30c.json
```

Its SHA-256 is
`2c4d00657d5432f71edbd2b8c8d0deae0e24f738459c9032fab7bea02a912777`.
The manifest is bound to the frozen pack and passing remote-tokenizer report,
records all 20 deterministic epoch-order hashes, and reports a clean working
tree. It remains a review artifact, not an executable authorization.

## Mechanical Tinker smoke result

The user explicitly authorized the frozen data-bearing smoke under its `$20`
ceiling. The manifest-gated runner was committed as `0061a2a`, official Tinker
prices were reverified unchanged, and the run executed in a dedicated private
Tinker project without exceeding or extending the approved plan. The private
project identifier is intentionally not recorded in version control.

Canonical run report:

```text
coupled-data/normal-work-dry-run-8-phase1-tinker-overfit-run-0061a2a.json
```

Report SHA-256:
`26edbdeeec5e688af0e18b191462e8f284f5538de682d99fba60afcc0ad5ac58`.

The mechanical gate passed completely:

- 20 fixed epochs, 560 forward/backward calls, 560 optimizer steps, and exactly
  9,645,060 submitted training positions;
- base weighted NLL `5.9992910034` and trained weighted NLL `0.0036094220`, a
  final/base ratio of `0.0006016414` against the required maximum `0.35`;
- all 28 greedy generations exactly matched their frozen targets and all 28
  terminated on EOS;
- all six paste-bearing examples generated the exact five-token `<|paste|>`
  marker sequence;
- the saved full optimizer state reloaded successfully, and its separately
  saved sampler reproduced selected weighted log-probabilities and generated
  token/stop-reason outputs exactly;
- the sampler, full optimizer state, and reloaded sampler checkpoints were all
  created with seven-day TTLs;
- 1,512,076 prefill positions, 221 observed sampled tokens, and three
  checkpoint saves remained inside the approved operation ceilings.

At the reverified frozen rates, logical operations cost an estimated
`$15.109134` before checkpoint storage: `$14.110723` training, `$0.997970`
uncached-prefill upper bound, and `$0.000441` sampling. Tinker's hourly billing
feed had not yet posted the session when the run completed, so provider-billed
usage remains a follow-up audit rather than being falsely reported as final.

This result proves the packed artifact, causal shift, masks, EOS behavior,
literal five-token paste marker, Tinker adapter, optimizer, checkpoints,
reload, and generation path work together. It is deliberately not evidence
for the Phase 1 behavioral hypothesis: all 28 examples were training examples,
and the test was constructed to overfit them.

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
- A proven Cmd-V may divide one debounced WRITE into locally observable AX epochs. Only an empty post-paste epoch on the same retained editable, with unchanged conditioned clipboard and complete endpoints, can produce `stateContinuity: segmented_at_grounded_paste`.
- `observedNetEdit` preserves the literal initial/final AX transition. `resolvedCompletion` composes the locally settled authored prefix, grounded paste payload, and locally settled authored suffix; the evidence is explicit as `grounded_paste_ax_epoch_transition`.
- Intermediate checkpoints remain raw evidence. Training uses only each epoch's final local diff, so temporary typo states receive no loss.
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

The substrate and local token/label validator are ready, but the remote
training runner and broader experiment harness are still required:

- **Learning objective:** `resolvedContent`, structured paste actions, and target lineage support either token NLL or a resolved-content semantic reward, but GRPO/RLOO and reward execution are not implemented.
- **Checkpoint recency:** event chronology supports identical daily scoring, but immutable daily model lineage, replay, and `d`, `d-1`, `d-3`, `d-7` scoring are not implemented.
- **Sliding window versus retrieval:** every causally available event remains addressable by ID, but BM25 query preprocessing, retrieval selection, and a frozen retrieval-plan artifact are not implemented.
- **Direct versus private reasoning:** the same input and final target can be reused, but scratchpad generation, final-answer isolation, latency accounting, and scoring are not implemented.
- **Continual Qwen versus closed ICL:** source examples are reusable, but training, provider adapters, and matched scoring are not implemented.
- **Open- and closed-model scaling:** model-specific tokenization is supported in principle, but the present packer selects the retained event suffix under each tokenizer. Before cross-model comparison, freeze a tokenizer-independent context plan containing the exact event IDs and serialized text so every model receives the same information.

No ablation requires changing the collector schema. The principal missing layer is a prospective experiment harness that freezes day boundaries, context/retrieval plans, model lineage, decoding, target resolution, scores, latency, and cost. Phase 2 will require a new conversion admitting displayed model proposals into the appropriate history; Phase 3 will additionally need stronger resource/world-state identity, which is not a Phase 1 collection blocker.

## Remaining requirements

### Before the next authoritative collection

Treat `phase1-semantic-v6`, `phase1-causal-v14`, and the current three-second
delays/crop configuration as the candidate baseline.

1. Run normal work without changing collector rules mid-session.
2. Reduce the raw session with `phase1-semantic-v6`; inspect finalized events and every non-event disposition.
3. Compile the finalized reduction with `phase1-causal-v14`, supplying the raw session directory for hash and lineage verification.
4. Manually sample the temporal trace against the actual work and record Phase 1's fidelity categories: missing events, temporal-ordering errors, incorrect content inclusion, authorship errors, write-boundary disagreement, destination ambiguity, and future leakage.
5. Quantify reducer unresolved reasons plus target/context exclusions. Fix only recurrent material errors demonstrated by that trace; otherwise freeze the collector/reducer/compiler versions.

### Before initial offline training

1. Pass the ordinary-work reconstruction audit above and freeze an immutable dataset version.
2. Retain the now-passing exhaustive causal-shift check, then separately verify the authenticated server tokenizer against the frozen local tokenizer without submitting the dataset.
3. With explicit authorization, run a mechanical overfit of the immutable pack in a dedicated private project, saving sampler and optimizer-state checkpoints and checking loss, reload, exact `<|paste|>` generation, and EOS token termination.
4. Run the initial Phase 1 experiment on eligible writes from the combined Obsidian, Chrome/browser, Codex, and VS Code stream, reporting aggregate and per-application results before the longer prospective continual experiment.

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

After the delayed provider-billing audit, retain the now-passing mechanical
harness and collect substantially more ordinary work through the frozen
collector/reducer/compiler pipeline. Reserve later chronological sessions as
validation data, score them before training on them, and then run the initial
Phase 1 comparison between the base model, the trained full-history model, and
the matched reduced/no-history control. Do not tune the data conversion on the
held-out sessions or interpret Run 8's memorization as a behavioral result.

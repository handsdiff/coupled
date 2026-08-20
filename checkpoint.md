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

The default `READ_DELAY` is now one second. The transition began at the clean
session boundary between `phase1-ordinary-work-2026-08-19-1` (three seconds)
and `phase1-ordinary-work-2026-08-19-2-read-delay-1` (one second). Historical
three-second observations remain immutable and retain their actual configured
delay and capture timestamp; they must not be backdated or relabeled as
one-second observations. They can remain explicitly identified lower-recall
history, while delay-homogeneous comparisons should use separate cohorts.

The bottom viewport crop is now 10%, matching the existing 10% top and side
crops. The transition began at the next clean session boundary,
`phase1-ordinary-work-2026-08-19-3-read-delay-1-bottom-crop-10`. Existing OCR
content retains its actual crop metadata. Historical sessions that retained
full-window screenshots can later be re-OCRed into a new versioned raw-derived
artifact with the 10% crop; existing OCR records must not be mutated in place.

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
`com.handsdiff.coupled` and `com.handsdiff.coupled.logs` are permanently
excluded from READ and WRITE collection. The authoritative collection artifact is `raw.jsonl`;
the stdout mirror and `events.preview.jsonl` are debugging aids. The viewer must not overlap a captured work
surface because semantic exclusion cannot remove pixels already covering a
rectangular screenshot.

## Implemented semantic reduction and causal compilation

Current compiler: `phase1-causal-v14`
Current reducer: `phase1-semantic-v10`

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

Semantic v7 is the current reducer candidate. It adds checkpoint-grounded
replacement reconstruction for complete initial selections and explicit
unpopulated-prompt states; preserves renderer fast-start chains as exact later
WRITE history while marking them target-ineligible because pre-first-mutation
conditioning is unavailable; and rejects unobserved mid-burst shortcut changes
rather than stitching uncertain semantic positions. Replayed from commit
`fbe601a`, Run 8 remains at 28 eligible targets and the August 18 ordinary-work
session produces 172 eligible targets. Both causal-v14 audits pass, yielding
exactly 200 eligible examples without manual record edits.

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
The user subsequently verified the final provider charge as `$14.37`.

### Mechanical smoke scaling record

The size of this experiment must be reported with separate context-compute and
supervision denominators:

- base model: `Qwen/Qwen3.5-9B-Base`, nominally 9 billion parameters;
- adaptation: rank-32 LoRA over attention, MLP, and unembedding;
- trainable adapter parameters: exactly `94,584,832` under Tinker's published
  model-specific counting utility, or approximately `1.05%` of the nominal 9B
  base model; the base weights remained frozen;
- examples: 28;
- one packed pass: 482,281 sequence tokens before the causal shift and 482,253
  submitted positions after shifting one position from each example;
- unique loss-bearing positions per pass: 203, comprising authored text,
  paste-marker tokens, and one EOS per target; the remaining 482,050 submitted
  positions had zero direct loss but supplied causal context for those targets;
- training exposure: 20 passes, totaling 9,645,060 submitted positions and
  4,060 loss-bearing target-position presentations;
- optimizer work: 560 batch-size-one steps;
- provider-verified total charge: `$14.37`.

For scaling-law comparisons, `482,253` is the distinct packed compute exposure
per pass, `9,645,060` is the repeated compute exposure over the whole run, and
`203` is the distinct supervised target-token count. These quantities must not
be collapsed into one ambiguous "training tokens" figure. The corresponding
whole-run ratios are approximately `0.102` submitted positions per trainable
parameter and `0.0000429` loss-bearing presentations per trainable parameter.
Because the same 28 examples were replayed 20 times, these figures describe a
memorization smoke test rather than an independent-data scaling point.

The adapter count comes from Tinker's `get_lora_param_count` table: for this
model, the published per-rank counts are 1,572,864 MLP, 1,130,496 attention,
and 252,416 unembedding parameters; their sum multiplied by rank 32 is
94,584,832. The count was documented after the immutable run report was
created, so the canonical JSON report and its digest remain unchanged.

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

The collector faithfully records app-generated document transitions such as Obsidian list markers. The current `phase1-causal-v14` baseline assigns no separate formatting provenance: formatting characters inside an otherwise verified eligible WRITE remain in resolved content and receive content loss. This is a deliberately simple initial policy, not a claim that every such character was consciously authored by the user.

Later ablations may instead:

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
- **Qwen context scaling:** repack the same compiled examples at 8K, 16K, 32K, and—where total runtime capacity leaves room for the complete target—64K with `--input-token-budget`; event-aware truncation keeps the query and valid event records intact. The authenticated Tinker runtime currently exposes 65,536 total tokens, so a nominal 64K history-plus-query plan must be reduced or run elsewhere when its untruncated target would exceed that limit.
- **Behavioral-cloning target:** current labels implement authored text, grounded paste markers, and EOS only.

The substrate, local token/label validator, authenticated Tinker tokenizer
comparison, LoRA training path, exact-generation audit, checkpoint/reload path,
and provider-backed prequential chronological-block/cross-model scoring harness
are now executed and audited:

- **Learning objective:** `resolvedContent`, structured paste actions, and target lineage support either token NLL or a resolved-content semantic reward, but GRPO/RLOO and reward execution are not implemented.
- **Checkpoint recency:** event chronology supports identical daily scoring, but immutable daily model lineage, replay, and `d`, `d-1`, `d-3`, `d-7` scoring are not implemented.
- **Sliding window versus retrieval:** every causally available event remains addressable by ID, but BM25 query preprocessing, retrieval selection, and a frozen retrieval-plan artifact are not implemented.
- **Direct versus private reasoning:** the same input and final target can be reused, but scratchpad generation, final-answer isolation, latency accounting, and scoring are not implemented.
- **Continual Qwen versus closed ICL:** the paid personalized-Qwen block adapter and subscription-backed frontier-model adapter have completed the initial 200-example developmental comparison. Larger prospective blocks remain necessary for a thesis-level conclusion.
- **Open- and closed-model scaling:** packer v7 freezes one shared semantic context plan containing the retained block IDs, any exact oldest-block rewrite, exact query digest, full semantic-input digest, and paste-action instruction. Every arm must reconstruct this same plan; no model tokenizer may select extra history. The current authorized experiment uses the complete unredacted 200-example corpus.

Every scored example must retain its individual pre-update NLL when exposed by the model interface. Block reports distinguish macro example-average NLL, in which every WRITE contributes equally, from micro target-token NLL, in which longer targets contribute more loss-bearing tokens. The completed Tinker overfit reports the latter as its headline aggregate; the two statistics must not be conflated.

No ablation requires changing the collector schema. Deterministic multi-session assembly, shared context plans, fixed block lineage, provider-backed scoring/sampling, cumulative Tinker updates, immutable checkpoint lineage, and actual usage/latency results are implemented. Daily boundaries, retrieval plans, and replay belong to later prospective or ablation work. Phase 2 will require a new conversion admitting displayed model proposals into the appropriate history; Phase 3 will additionally need stronger resource/world-state identity, which is not a Phase 1 collection blocker.

## Multi-session corpus assembly

The causal compiler still verifies one finalized session at a time. The
post-compile `phase1-corpus-v2` assembler combines compatible outputs without
rewriting stable event or example IDs; concatenating `examples.jsonl` files is
not treated as valid assembly.

“Same type” means compatible under the same effective data and learning
contract, not the same application mix, project, or subject matter. The corpus
compatibility record separates:

- collection/semantic compatibility: collector semantics and configuration,
  raw schema, completeness bounds, delays, crops, relevant allowlists and
  exclusions, reducer version, and causal compiler/eligibility version;
- experiment compatibility: history serializer, conditioning-query schema,
  target/authorship contract, context-gap policy, tokenizer/packing contract,
  and loss-mask contract.

The immutable corpus manifest records ordered session IDs, source and artifact
hashes, start/end times, compatibility fingerprints, chronological block
boundaries, reducer/compiler/context-plan versions, and every boundary's
coverage status: `continuous`, `interrupted`, or `unknown`. Original event IDs
and per-session lineage remain unchanged.

All preceding compatible sessions may enter cumulative training after their
blocks have been scored. An unobserved gap makes context incomplete but does not
make earlier events causally invalid. The initial context policy therefore
retains earlier eligible events behind an explicit structural gap marker rather
than either pretending continuous coverage or automatically resetting history.
The marker is serialization metadata, not a third semantic event. Hard reset
versus gap-aware carryover is a later versioned ablation.

The first assembled corpus contains Run 8 followed by the August 18 session:
28 + 172 = 200 eligible examples. An earlier candidate excluded five Inkling
form-response WRITEs and redacted them from later history. The user subsequently
authorized those responses as both learned targets and provider-transmitted
context, so that 195-example projection is superseded. The canonical unredacted
corpus has 200 examples in four chronological blocks of 50, retains all 837
semantic READ/WRITE events plus one structural `unknown` coverage gap, and has
zero redacted or privacy-excluded targets. Independent assembly is byte-identical.
Packer v7 creates the common 32K semantic context plan and packs all 200 targets
with 24 grounded paste actions; both independent packed example/context-plan
artifacts are byte-identical and their audits pass. The shared plan preserves one
identical left-edge
task instruction—predict the exact next human WRITE completion, represent every
paste action as literal `<|paste|>` rather than its payload, and output only that
completion—inside the 32K budget for every arm. This prevents the personalized
model from being the only condition taught an otherwise unstated serialization
convention.

The `phase1-prequential-v1` mock backend rehearses 600 scores and four updates
without a model, network, authentication, or cost. It proves each complete block
is scored before update and records the chosen initial exposure policy explicitly:
warm-start the prior checkpoint and train one epoch over the full cumulative
corpus. Across the four blocks, 2,782 loss-bearing target-token occurrences
become 7,298 presentations across updates. This is an exposure
choice recorded for later comparison, not additional unique human data.

## Initial Phase 1 developmental experiment

The first real provider-backed comparison completed on August 18–19, 2026 from
Git revision `04917014d19e896a538f4115b3b1e09ed12d1fc0`. It used the canonical
unredacted 200-example corpus, four chronological blocks of 50, one shared 32K
semantic context plan, and the frozen score-complete-block-before-update
protocol. Frozen Qwen and subscription-backed `gpt-5.6-sol` remained unchanged.
Personalized Qwen warm-started the previous rank-32 checkpoint and trained one
deterministic cumulative epoch after each block with seed 17, batch size one,
and Adam at `2e-4`.

The subscription scorer stopped after 177 examples because a completed
Responses result contained no visible output. An explicitly authorized
structure-only retransmission showed a successful completed response with
5,703 output tokens, 5,696 of them reasoning tokens, but an empty visible
completion. Runner v2 therefore treats only an explicitly `completed`,
error-free response with usage evidence as a valid empty prediction; incomplete
or malformed empty responses still fail closed. The exact 177-score prefix was
adopted rather than replayed, with its old implementation, old provider-plan
hash, prefix count, and prefix digest retained in the final frontier manifest.
The migration and negative cases are covered by the no-network runner checks.

The superseding provider plan is
`coupled-data/phase1-experiment-1-provider-plan-local-v8-empty-completion-resume.json`
with SHA-256
`cb55175d8bd5d0e54833a38f46867a5b2abde973b3b1842b2b03689501995105`.
It is byte-identical to reviewed plan v7 outside implementation hashes. The
frontier artifact contains all 200 predictions, including three legitimate
empty completions. Its manifest and score hashes are respectively
`6e9a2c3e8e425f4c30cea12cacb397563b453d6040a021f05c8c1b1090b62796`
and
`0a2ea25b350607c112589132c860c6cda690ca8c5550467f1b6be04e1aa28785`.

Tinker completed 400 NLL calls, 400 samples, 500 optimizer steps, 14,587,208
training positions, 24,415,028 prefill tokens, 120,232 observed sampled tokens,
and eight checkpoint saves (sampler plus optimizer state after each block).
Training NLL at the four update boundaries fell from `3.3493` to `2.5777`,
`1.9438`, and `1.4444`. The frozen-price estimate before checkpoint storage was
`$37.694867`; the user-verified final Tinker charge was `$30.10`, below both the
estimate and the authorized `$40.00` ceiling. The Tinker
manifest, score, and update hashes are respectively
`bbba0c7ae35e2552ebfe30c474df8ad1c500c1c754151405a3e69da5899341a1`,
`2c3322ae1de0b245d6b436a9bd165f870956c1a86c94b5610bc9371dccfd2815`,
and
`d164420cfa43a347b02a5aa029017c6c1adbe29b52e6d69c5f9f8c0517cb0a6b`.

The offline three-arm audit passed all coverage, target, context-plan, causal
ordering, checkpoint-lineage, and artifact-digest checks. A second offline audit
added a versioned generated-completion scorecard and cost/latency accounting
without making provider calls or changing any prediction. The current immutable
results are in
`coupled-data/phase1-experiment-1-results-v5-prospective-150-20260819`;
`experiment.json` hashes to
`6511f5849be030f172585e9cbe945a492c757d83af0b25c4b05fa412cd213ce8`
and `comparisons.jsonl` hashes to
`8da9d86fac1cd8ed521ddc124b142dd2526171e08b1da6519741a66546ad9c10`.
The latter retains all 200 executed calls as operational evidence. Headline
performance uses only the 150 examples in blocks 2–4; their dedicated
`evaluation-comparisons.jsonl` hashes to
`947dfb8bd0aca7900ae937d05ad06be33f17ca401a1f10c951af452e5cd90259`.
Block 1's 50 examples are warm-up/training material, not prospective evaluation:
the personalized arm had no prior personal update and was identical to frozen
Qwen at that point.
Audit v2 computes exact match, surrounding-whitespace-normalized exact match,
exact longest correct prefix, unit-cost Unicode-code-point Levenshtein
similarity in macro and micro forms, and per-example paste-action precision and
recall directly from frozen targets and predictions. It reports authored-only,
mixed authored/paste, and paste-only strata. The original provider-recorded
`SequenceMatcher` value remains only as a legacy provenance metric.

Headline developmental results:

- Over the 150 prospective examples and 2,058 target tokens, frozen Qwen micro target-token NLL was `4.5214`; personalized prequential Qwen was `3.2986`.
- Personalized Qwen saved `3,630.6` prequential bits versus frozen Qwen on prospective human WRITE tokens.
- Blocks 2, 3, and 4 saved `1,099.5`, `1,350.6`, and `1,180.5` bits after 50, 100, and 150 preceding examples. Block 1 is reported separately as warm-up, not as a scored model comparison.
- Future-block micro NLL changed from frozen/personalized `4.1270 → 3.1849`, `4.4580 → 3.1685`, and `5.2195 → 3.6549` on blocks 2–4.
- On the 150 prospective targets, `gpt-5.6-sol` exact-matched 12 (13 normalized), with macro/micro Levenshtein similarity `0.2574`/`0.1943` and `3.56%` micro target-prefix coverage. Six exact matches were the six paste-only targets; on 133 authored-only targets it exact-matched 6, with macro/micro similarity `0.2198`/`0.1900`.
- Personalized Qwen exact-matched 4/150 (5 normalized), with macro/micro similarity `0.1681`/`0.1302` and `1.40%` target-prefix coverage. On authored-only targets it exact-matched 2, with macro/micro similarity `0.1453`/`0.1201`. Frozen Qwen exact-matched none and had macro/micro similarity `0.0246`/`0.0262` over the prospective set.
- GPT paste-action precision/recall was `30.77%`/`94.12%`, personalized Qwen was `15.09%`/`47.06%`, and frozen Qwen was `0%`/`0%` over prospective examples.
- Personalized Qwen lowered prospective micro NLL in every observed application stratum: ChatGPT `4.2376 → 3.4006` (43 examples), Code `4.8029 → 3.2726` (21), Chrome `4.6353 → 2.7879` (60), and Obsidian `4.7249 → 3.8619` (26). These strata are too small for separate application-level claims.

Audit v5 adds `cost-latency.json`, a shareable `cost-latency.csv`, and per-query
timing/cost fields to `comparisons.jsonl`. Their hashes are respectively
`999cc5ad8a2f8540bed9bb060ebeae3e0d2db7cfca2e60f6bbd003877ad4e24c`
and
`5c23e48aa13c5ed02c1beccc4c5b23e93d70f2b96f629f9484d92a3e884bf8ee`.
GPT generation requests including reasoning had median/mean/p95 latency of
`14.60`/`20.01`/`56.64` seconds over the prospective set. Frozen Qwen's combined
likelihood-scoring plus generation operation measured `8.34`/`9.05`/`15.54`
seconds; personalized Qwen measured `4.84`/`6.54`/`15.74` seconds. These are not a direct generation-latency
comparison because the existing Tinker timing does not separate likelihood
scoring from sampling. `phase1-tinker-prequential-v3` now records those request
timings separately for future experiments; historical generation-only latency
cannot be reconstructed.

At the frozen Tinker rates, generation-only inference over the 150 prospective
examples was estimated at `$3.384495` (`$0.022563` per example) for frozen Qwen
and `$3.245942` (`$0.021640` per example) for personalized Qwen. Including
target-likelihood scoring raises those totals to `$6.629347` and `$6.490794`.
Cumulative training
was estimated at `$21.341085`; the full frozen-rate subtotal was `$37.694867`,
while the user-verified provider charge was `$30.10`. GPT used the existing
ChatGPT subscription, but its recorded usage can be priced as though it used the
API. At the official GPT-5.6 Sol rates frozen on August 19, 2026—`$5/M` uncached
input, `$0.50/M` cached input, and `$30/M` output—the 200 calls would have cost
`$35.193554`. The 150 prospective calls would have cost `$28.884495`, or
`$0.192563` per query. No call crossed the 272K-input premium threshold. This
API-equivalent estimate is distinct from the actual subscription charge.

The prospective-only rule applies to the entire comparison, not merely its
cost and latency reporting. Exact match, correct prefix, character similarity,
paste behavior, target NLL, and cumulative bits saved all exclude block 1.
Protocol `phase1-prequential-v2` makes this operational for future runs: block 1
is trained as the 50-example warm-up without issuing any model-scoring calls;
blocks 2 onward are scored before their updates. Provider plan v4 therefore
plans 150 GPT calls and 300 Tinker score operations for this four-block corpus,
while retaining all four cumulative training updates over all 200 examples.
The no-network mock produces 450 cross-arm scores, confirms that the first
personalized score uses the checkpoint trained through block 1, and the real
audit accepts both this prospective-only shape and the completed legacy run's
200-call operational evidence. Re-auditing the legacy run leaves every
150-example headline summary and the `3,630.6` prospective bits-saved result
unchanged.

This is a positive capacity and forward-generalization signal: weight updates on
earlier personal actions reduced surprise on later actions. It is not yet a
thesis conclusion. The corpus is small, cumulative replay gives early examples
more presentations, examples are temporally correlated, and GPT lacks directly
comparable target-token NLL through the subscription interface.

## Remaining requirements

### Before the next authoritative collection

Treat `phase1-semantic-v10`, `phase1-causal-v14`, the one-second READ delay,
three-second WRITE delay, and ten-percent top/side/bottom crop configuration as
the candidate baseline.

1. Run normal work without changing collector rules mid-session.
2. Reduce the raw session with `phase1-semantic-v10`; inspect finalized events and every non-event disposition.
3. Compile the finalized reduction with `phase1-causal-v14`, supplying the raw session directory for hash and lineage verification.
4. Manually sample the temporal trace against the actual work and record Phase 1's fidelity categories: missing events, temporal-ordering errors, incorrect content inclusion, authorship errors, write-boundary disagreement, destination ambiguity, and future leakage.
5. Quantify reducer unresolved reasons plus target/context exclusions. Fix only recurrent material errors demonstrated by that trace; otherwise freeze the collector/reducer/compiler versions.

### Before the foundational behavioral experiment

The exhaustive causal-shift check, authenticated server-tokenizer comparison,
and bounded Tinker LoRA overfit—including exact `<|paste|>`/EOS generation and
sampler/optimizer-state reload—have passed. They remain mechanical gates rather
than behavioral evidence.

1. Freeze the canonical 224-example raw-authoritative closed-episode corpus and shared 32K semantic context plans from the seven audited source sessions. The first 50 examples are training-only warm-up; the remaining 174 are prospectively scored within this developmental rerun.
2. Preflight the exact Tinker and subscription-backed LiteLLM Responses operations, model identities, decoding, projected token use, privacy boundaries, and hard cost ceilings without transmitting personal data.
3. After separate authorization, score each block with frozen base Qwen, frozen `gpt-5.6-sol` at `xhigh`, and the current personalized checkpoint using the same context plan.
4. Train the personalized Qwen only after complete block scoring, save sampler and optimizer state, and use each result only for the following block.
5. Record individual predictions and available NLLs plus macro, micro, chronological, aggregate, cost, latency, exposure, and per-application results before considering the later prospective continual experiment.

### Before live prediction

1. Capture destination, cursor context, and clipboard state when focus arrives.
2. Refresh that query when cursor or selection changes.
3. Measure drift between focus-time state and pre-mutation training state.
4. Preserve the exact focus-time context plan and model input used for each displayed prediction; evaluate it against the later eligible write without substituting the pre-mutation query.
5. Render action markers such as `<|paste|>` as UI actions rather than literal text.
6. Preserve displayed model predictions as raw, Phase 1-excluded events.

## Important non-blocking fidelity improvements

- Structured Chrome URLs and field identity.
- Obsidian note paths.
- Surface-specific viewport crops.
- Better sentence-boundary handling for OCR.
- YouTube transcript and subtitle exposure.
- Reduced raw suppression churn during window animation.
- More reliable differentiation of titles, body fields, prompts, and transient browser UI.

## Next step

Retain the prior `$14.37` memorization run as a harness gate and the completed
200-example prequential experiment as the first developmental capacity result.
Continue frozen-pipeline ordinary-work collection and append compatible sessions
as prospective chronological blocks. Score each new block with the last frozen
personalized checkpoint before updating it, so the learning curve extends
without turning future data into a retrospective holdout.

The next measurement gates are approximately 300 and 600 eligible WRITEs. At
300, rerun the complete pipeline as a replication/systems check. At 600, perform
the first more meaningful comparison and add the inexpensive central context
ablation on the same frozen substrate: current/reduced state versus the complete
causal READ/WRITE stream. This begins separating the value of temporal context
from the value of personal weight updates. If the forward NLL advantage remains,
continue the scaling curve through roughly 1,000 and 3,000 examples before
making stronger judgment-distillation claims. Keep subsequent chronological
blocks prospective and report unique human targets separately from repeated
training presentations.

Before the next paid run, carry the verified `$30.10` charge into cost planning,
freeze the next appended corpus and context plans, generate a new reviewed
provider plan, and decide whether to retain cumulative replay or add a
new-block-only training arm. Reduced-history, timestamp, context-length,
retrieval, rank, and model-scale ablations remain downstream of this first
replication. Any change to conversion, context construction, or training starts
a new versioned lineage rather than modifying the completed result.

The superseding unredacted provider plan calculates 14,587,208 cumulative Tinker
training positions and a `$38.863580` Tinker projection including a `$1` checkpoint
reserve. The frontier arm is configured as a direct Responses request through a
loopback LiteLLM proxy using the ChatGPT-subscription route
`chatgpt/gpt-5.6-sol` at `xhigh`; it must never fall back to an OpenAI API key.
This route draws from the ChatGPT/Codex subscription usage pool rather than API
token billing. LiteLLM documents that subscription requests strip token-limit
fields, so the former 8,192-token API ceiling is not enforceable on this
transport. LiteLLM 1.97.0 and the compatible FastAPI 0.137.2 are installed in an
ignored repository-local virtual environment. The loopback-only request contract
and mocked JSON/SSE response parser pass; the client sends no token-limit field
or metadata and cannot target a nonlocal URL. The ChatGPT OAuth flow and a
non-personal authenticated preflight passed: `chatgpt/gpt-5.6-sol` at requested
`xhigh` returned model `gpt-5.6-sol` and exact output `OK`, using 1,642 input and
5 output tokens. It used no OpenAI API key and transmitted no collected data.
The separately authorized single-example data gate also passed on the earlier
privacy-filtered candidate. The earliest eligible grounded-paste example is
unchanged in the unredacted corpus; it was reconstructed from the frozen shared
semantic context plan, and only its 11,592-byte model input was transmitted; its
held-out target was not sent. `gpt-5.6-sol` at requested `xhigh` returned the exact expected
`<|paste|>` completion in 3.53 seconds, using 4,150 input tokens and 85 output
tokens, of which 74 were reasoning tokens. The authenticated report is
`coupled-data/phase1-experiment-1-paste-preflight-authenticated-v1.json` with
SHA-256 `448dcc399d4cc3dc58873deebbd1864607ce69616ecfd8ac083bd87a274e27df`.
This proves the shared paste instruction is understood on one real example; it
is a transport/serialization gate, not behavioral evidence. Complete-corpus
scoring was subsequently authorized and completed, including the restored
Inkling material, as documented in the initial developmental experiment section.

Provider-plan v3 closes the final execution-review gaps. It freezes the actual
Qwen training contract in the reviewed artifact: rank-32 LoRA over attention,
MLP, and unembedding; seed 17; one example per forward/backward operation; one
epoch over the cumulative corpus after each block; deterministic SHA-256 example
ordering; Adam at learning rate `0.0002`, betas `0.9/0.95`, epsilon `1e-12`, no
weight decay, and gradient clipping at `1.0`; and seven-day sampler/optimizer
checkpoints. It also binds every runner/dependency digest and requires the same
clean Git revision on resume. Tinker writes an in-flight marker before each paid
score, training, or checkpoint operation and refuses to replay either an
in-flight operation or a partially completed update under the same `$40`
authorization. Recovery would require explicit reconciliation and a new
cost-authorized plan rather than silently omitting prior spend. A no-network
regression constructs all 600 synthetic arm scores, exercises safe resume plus
in-flight/partial-update/revision/dependency rejection, passes the final
three-arm audit, and proves the audit rejects a personalized checkpoint that has
already seen its scored block. The original frozen provider plan was
`coupled-data/phase1-experiment-1-provider-plan-local-v7-unredacted-frozen-runner.json`
with SHA-256
`b70be648abfd744b1e6af51a2aa4e2d80bba0a0693cff24e5f1f68b41e23710a`.
The completed execution supersedes it with plan v8 solely to add the
provider-completed empty-prediction rule and exact interrupted-prefix migration;
all non-implementation plan content remained identical.

Keep the initial task content-only given causal history plus known destination,
semantic cursor context, and clipboard state. Idle-triggered sampling,
destination or cursor-location prediction, and learned proactivity are deferred
until the content predictor has been evaluated and focus-triggered use supplies
evidence that those additional capabilities are necessary. Before live
prediction, complete the focus-time capture and drift measurements listed
above; they do not block the offline capacity comparison.

## Composition-episode shadow checkpoint (August 19)

The first developmental experiment remains frozen as the lower-granularity
formulation in which each eligible semantic WRITE becomes a separate loss
target. It revealed a more important action-abstraction question: cursor-bounded
and delay-bounded WRITEs faithfully describe editing mechanics, but the desired
target is the closed authored completion that captures the user's next thought.

No collector, semantic reducer, causal compiler, packed dataset, loss mask, or
model result has been changed. Commit `a1ae878` froze the initial
`phase1-episode-v0-shadow-r2` reconstruction checkpoint. The current
non-authoritative `phase1-episode-design-v1-shadow-r4` layer implements:

```text
raw evidence
→ unchanged semantic READ/WRITE events
→ shadow composition-episode review
→ episode-design proposals and human review annotations
```

Run it with:

```bash
python3 scripts/build-phase1-episode-review.py \
  --corpus coupled-data/phase1-experiment-1-corpus-v2-unredacted-canonical-20260818 \
  --packed coupled-data/phase1-experiment-1-corpus-v2-unredacted-canonical-20260818-qwen-pack-v7 \
  --output coupled-data/phase1-experiment-1-episode-design-review-v1-shadow-r4-final-20260819 \
  --selection-file episode-review/phase1-episode-development-gold-v0.json \
  --proposals-file episode-review/phase1-episode-development-gold-v0-proposals.json

python3 scripts/check-phase1-episode-review.py

python3 scripts/serve-phase1-episode-design-review.py \
  --review coupled-data/phase1-experiment-1-episode-design-review-v1-shadow-r4-final-20260819
```

The artifact contains the complete member trajectory, including semantic
history-only WRITEs omitted from the old loss-bearing examples; full initial,
intermediate, and final AX values; exact raw and reduced-event lineage; cursor
and selection evidence; causal events; a mechanical initial-to-final edit; and
an unreviewed annotation sidecar. Every member's terminal state is the exact raw
observation selected by the bound semantic reducer event. This matters for
submitted fields whose raw `after` is empty or invalid while a pre-Return
checkpoint retains the actual authored content.

Mechanical status has four hard gates in addition to cursor/semantic alignment:
every reducer-selected terminal value must exactly equal the next member's
logical `BEFORE`; logical application/document/role identity must remain stable;
no novel causal READ may occur inside the interval; and no outside WRITE may
overlap it. Accessibility element hashes remain diagnostic because Electron can
replace an AX object while exposing a continuously replayable field. An
intervening READ is non-novel only when its exact serialized observation already
appears in the episode-onset packed model history. A candidate failing any gate
can retain a diagnostic proposed edit
but cannot receive a `mechanically_representable_*` status. Mechanical
representability is deliberately not treated as episode closure or training
authority.

Two seeded neighborhoods expose the intended distinction:

- ChatGPT examples 32–41 contain 12 semantic WRITEs, including two
  history-only edits. The complete final prompt is representable as one
  insertion from the initial empty prompt. Its final semantic event selects a
  pre-Return checkpoint because raw terminal capture was invalid, and the next
  event is a ChatGPT READ showing the submitted prompt/response transition.
  This is strong closure evidence, recorded as
  `return_observed_requires_surface_interpretation`; Return is not universally
  treated as submission because it can remain ordinary multiline editing.
- Obsidian examples 152–158 contain eight semantic WRITEs, including the
  history-only `to` → `as` correction. Their final authored passage is
  representable as one completion. Obsidian's numeric AX selection is wrong by
  43 characters, but the independently captured semantic left/right ranges
  uniquely meet at the observed edit boundary. The diagnostic preserves and
  displays the numeric disagreement; it does not rewrite it.

The Markdown review now shows the reducer-selected provenance and a marked raw
`BEFORE`/selected-terminal transition for every member, plus the next five
semantic events after each proposed candidate so submission, clearing,
application switching, new composition, and later READ evidence are directly
inspectable.

The semantic-anchor proof is conservative: after removing only whitespace,
zero-width space, and BOM layout scaffolding, both anchors must contain at least
32 characters, occur exactly once, and meet on opposite sides of the observed
net edit. Regression checks cover final-state rather than keystroke-trace
targets, unique-versus-ambiguous anchors, edits outside the conditioned region,
history-only member inclusion, READ boundaries, and deterministic
serialization. An independent replay produced byte-identical artifacts.

The initial two-case artifact remains a useful narrow checkpoint. The
development selection now expands it to 20 deliberately varied neighborhoods:
submitted single writes; cursor revisions; long-editor compositions; mixed
typed/paste submissions; fragmented prompts; causal READ boundaries; disjoint
note regions; and independent terminal commands. The tracked selection fixes
the corpus, ordinals, category, and rationale. It is distinct from the tracked
assistant proposal sidecar, which remains explicitly pending human
adjudication.

The 20 neighborhoods contain 75 semantic WRITEs. Seven pass the complete
mechanical representation test, three are mechanically gated out, and ten
retain useful evidence without receiving a mechanical-positive label. The
assistant taste pass proposes 13 single-completion episodes: seven unchanged
singletons and six multi-WRITE merges. It proposes four definite splits, one
split at a causal READ, one causal defer, and one bounds expansion before
readjudication. These counts are diagnostic judgments, not training examples.
In particular:

- ChatGPT 32–41 and Obsidian 152–158 are proposed merges into their final
  authored completions.
- Claude 133–134 becomes one structured typed/paste/typed completion. Its
  opening quote was a history-only micro-WRITE; the proposed target preserves
  the original paste checkpoint and clipboard lineage rather than relabeling
  the clipboard payload as authored text.
- ChatGPT 181–185 is not accepted as currently bounded because its initial
  state already contains an earlier authored prompt prefix.
- Intervening model READs, distant Obsidian regions, and distinct terminal
  submissions remain split or deferred even when nearby text is semantically
  related.

The review projection now retains each member's complete structured target as
well as its display form. This prevents `<|paste|>` from erasing the underlying
authorship and clipboard evidence during episode adjudication. The generated
`proposed-annotations.jsonl` resolves deterministic target policies into
concrete structured targets, while the separate `annotations.jsonl` remains
blank for human decisions. Nothing reads either file as training authority.

Canonical 20-neighborhood shadow hashes:

- `episode-review.json`: `d65b03527b2c0e453aed55a8a8c531111fa49967e41286fe96416d828a15c02f`
- `episode-candidates.jsonl`: `37edbc2bc22b5607976428415853dffdd7ffa54a148278cdb22caa1bc3743879`
- `annotations.jsonl`: `3d50ad720d024d3a8dcf058f1c7ac3b36823dc683acfc8c9351a6a69420232d0`
- `proposed-annotations.jsonl`: `bd39b3397fe095d3bae58d9ebb9639d318f3f4f640e0ead32da16582bc99083b`
- `review.md`: `dd91dd408ab368514babc895abc1fa0f683e180b7b1494a895922c5d139aa8bf`

The 20-case artifact above remains the reconstruction checkpoint. It is
superseded for episode-design review by a 21-neighborhood projection that adds
the examples 120–123 fast-lookup sequence and binds every selected example to
the exact shared `phase1-token-pack-v7` model input used in the developmental
experiment. Sixty-seven distinct example inputs are stored with the exact task
instruction, retained serialized event history, conditioning query, token
counts, truncation counts, and SHA-256. The builder verifies those values
against the frozen packing plan rather than synthesizing a new context.

Every candidate now states that its onset is
`first_mutating_input_pre_application_proxy`. The historical sessions did not
capture an explicit focus-time prediction opportunity. This permits honest
offline episode reconstruction but is not evidence that the training query
matches a later live focus-time query.

Examples 119–125 establish the important distinction:

- Examples 115–119 remain one coherent closed Obsidian paragraph.
- Example 120 is an unfinished composition and remains history-only.
- Examples 121 and 122 are instrumental URL-navigation and in-page-Find
  actions. They remain causal history but receive no substantive-content loss.
- Example 123 is a substantive insight, but the short Cursor passage the human
  read after finding `sublinear` never entered the packed model context. It is
  therefore history-only with
  `confirmed_human_visible_model_missing_information`, not a target.
- Examples 124–125 are explicitly partitioned and merged into one target ending
  `perhaps missing the explicit compression aspect? perhaps not`.

This changes the abstraction from all-or-nothing candidate decisions to
partition-capable episode-design proposals. A causal sequence can preserve each
micro-WRITE while separately deciding which partitions are unfinished,
instrumental, under-conditioned, or one closed substantive outcome.
Every proposed representable partition is independently gated on exact
editable-state continuity, stable logical editable identity, and the absence of
an intervening novel causal READ. In particular, the `124–125` merge passes all three
checks; it is not being accepted merely because the enclosing `123–125`
neighborhood looks semantically related.

The remaining review pass adds two positive merges without weakening the
ambiguous negative controls:

- Obsidian 127–128 is one 392-character GTM composition. Its intervening READ is
  byte-identical to content already retained twice at episode onset, its field
  state replays exactly, and its logical app/document/role identity is stable.
  The changed AX element hash and numeric cursor disagreement remain visible
  diagnostics rather than invented evidence.
- ChatGPT 181–185 now includes the exact target-ineligible WRITE that ended 137
  milliseconds before example 181. The six micro-WRITEs replay into one complete
  870-character submitted prompt. Because the real onset was history-only, the
  old pack contains no historical query at that exact boundary; the review says
  so explicitly and shows the nearest later packed input only as a diagnostic.
- ChatGPT 193–196 remains deferred: its READ is novel relative to onset and its
  raw editable trajectory is discontinuous.

The dedicated read-only localhost UI renders the actual historical prediction
input beside the complete raw/reduced edit trajectory and proposed outcome. If
the proposed episode begins at a formerly history-only WRITE, it renders the
recorded onset conditioning and explicitly reports that no frozen packed input
existed rather than substituting a later query. It
also exposes visibility gaps and every partition's own sampling input. It uses
no remote assets and does not mutate annotations or training artifacts. The
tracked selection/proposal filenames retain their earlier `development-gold-v0`
names only for checkpoint continuity; their manifests explicitly identify them
as an episode-design review set, not gold evaluation data or training authority.

Canonical episode-design v1 hashes:

- `episode-review.json`: `007ada88f8c39460f0e622ad3b510a57146e769f9f508bb066868d99cdafdd83`
- `episode-candidates.jsonl`: `72996b5fbfe3d974bfe1ebee9a53c346ac710e9e3126ca62650d2a4b24f16641`
- `model-facing-inputs.jsonl`: `9a957109f5a6c1597115264e5be0fb2ed8c1c46409aa1d57afee1386069e8c83`
- `annotations.jsonl`: `6f8614b0f2d74df2f87a2f193ea65b6444597b71dad23387c02451588174bb63`
- `proposed-annotations.jsonl`: `4909d8ac480e89a7beff88061098a124c0910b9c2f1aaf08e13e54b665ad2cf6`
- `review.md`: `70940e7de18e89818883aee7e844f623bd82af73a37a6a00a8f2c1956c290aad`

An independent replay of all six artifacts is byte-identical, the UI's
read-only artifact check passes, and the HTTP index/detail endpoints were
exercised locally. The next checkpoint remains human review, not automation:
accept or revise these episode-design partitions before any versioned episode
layer becomes compiler or training authority.

## Closed composition episode corpus

The 21-case episode-design review and the user's 2026-08-19 taste audit now
feed a versioned layer between semantic events and loss-bearing examples:

```text
raw evidence
→ phase1-semantic-v7 READ/WRITE micro-events
→ phase1-episode-v1 closed composition episodes
→ phase1-episode-causal-v1 examples
→ phase1-token-pack-v6
```

Micro-WRITEs remain unchanged in chronological history and raw lineage. They
no longer independently receive loss when an adjudicated group represents one
closed composition. An episode uses the conditioning state captured before its
first member, removes every member WRITE from its own context, becomes
available only after its final member, and receives one structured target plus
one EOS. Novel causal READs still partition thoughts; exact/repeated READs do
not automatically do so. Ambiguous trajectories stay as history rather than
being forced into a completion.

Canonical all-data-through-2026-08-19 artifacts:

```text
coupled-data/phase1-episode-all-through-2026-08-19-micro-corpus-v1
coupled-data/phase1-episode-all-through-2026-08-19-corpus-v1
coupled-data/phase1-episode-all-through-2026-08-19-pack-v1
```

The source comprises the two sessions behind the original 200-example corpus
plus four ordinary-work sessions from August 19. The micro conversion contains
1,457 semantic events and 361 eligible micro targets. Episode v1 produces 295
loss-bearing units: 28 multi-WRITE episodes absorb 97 micro-WRITEs, and 11
reviewed micro-WRITEs remain history-only. The remaining unaffected eligible
WRITEs are explicitly marked `unreviewed_singleton_closed_baseline`; this is an
inspectable conservative baseline, not a claim that the system has solved a
universal semantic definition of thought closure.

Two user-supplied blind checks passed:

- the Obsidian sequence around `2026-08-19T12:43:16.708Z` reconstructs five
  semantic members into one finalized note composition, including the `(131)`
  middle edit and subsequent continuation;
- the Code sequence around `2026-08-19T19:05:05.004Z` reconstructs two members
  into `this is better. take a look at the reviewer thread. it flagged a
  post-submission trigger to clean up some other gaps.`

Episode construction is deterministic: an independent replay produced
byte-identical `corpus.json`, `examples.jsonl`, and `events.jsonl`. The episode
audit verifies unique member ownership, causal context cutoffs, absence of all
member WRITEs from their own input, grounded paste targets, and both blind
cases. The tokenizer pack contains 295 examples and 34 grounded paste actions;
the ordinary packed-data audit passes.

### Qwen-only episode smoke

A deliberately small Tinker smoke used three targets: the second blind
multi-WRITE episode, the shortest grounded-paste target, and the longest
ordinary target. It exercised 315 unique loss-bearing tokens, including paste
and EOS, over 20 bounded epochs and 60 optimizer steps.

Canonical artifacts:

```text
coupled-data/phase1-episode-smoke-corpus-v1
coupled-data/phase1-episode-smoke-pack-v1
coupled-data/phase1-episode-smoke-tinker-plan-v1.json
coupled-data/phase1-episode-smoke-tinker-run-v1.json
```

The mechanical results were successful:

- baseline weighted NLL: `3.544003669`;
- trained weighted NLL: `0.000278241` (ratio `0.00007851`);
- exact greedy generation: 3/3 targets;
- EOS termination: 3/3;
- grounded paste marker: exact;
- sampler weights and optimizer state saved and reloaded;
- reloaded generation was token-for-token identical.

The legacy overfit report is labeled `failed_acceptance` solely because it
requires bitwise-identical floating-point log probabilities after optimizer
reload. Reloaded mean NLL differed by at most `0.000019011`; exact generated
tokens, stop reasons, checkpoints, masks, and operation ceilings all matched.
This is a provider numerical-reproducibility diagnostic, not a dataset or
training-operation failure. Estimated logical cost was `$3.174507` before
checkpoint storage; provider billing was still pending when the report closed.

The next paid step is not another smoke. Inspect the changed episode targets,
freeze the episode policy/version, then regenerate the full Phase 1
score-before-update experiment over these closed composition units.

## Episode-normalized training candidate (v2)

The v1 prototype proved that several micro-WRITEs could reconstruct one useful
target, but it left the original micro-WRITEs in model-facing history and
treated 133 unreviewed eligible WRITEs as presumptively closed. That corpus is
superseded. The current training-candidate architecture is:

```text
raw evidence
→ phase1-semantic-v8 faithful READ/WRITE transitions
→ phase1-episode-v2 closed composition episodes
→ phase1-episode-causal-v2 examples
→ phase1-token-pack-v7
```

Semantic micro-WRITEs now exist only as immutable lineage and audit evidence.
Every model-facing historical WRITE is a closed episode, and every loss target
is a closed, substantive episode. No source micro-WRITE is serialized into a
model prompt. An unresolved or unclosed transition is conservatively omitted
from the cognitive history rather than presented as if it were an independent
thought.

Every accepted episode must bind to candidate evidence proving:

- continuously replayable editable state;
- stable logical editable identity;
- no novel causal READ between members;
- no overlapping outside WRITE;
- an observed closure boundary.

An exact READ already present at episode onset may be crossed and is suppressed
from the normalized stream; a novel READ partitions episodes. Submission,
focus/destination change, or a verified post-settlement observation can close
an episode. Session termination alone does not prove closure. Existing
application prompts, intermediate cursor corrections, typos, and Obsidian's
zero-width list scaffolding receive no content loss.

Closed episodes with fewer than 40 trimmed authored characters or six authored
words remain history-only. Paste actions do not bypass this substantiveness
gate. Paste payloads remain resolved and provenance-marked in later history,
while the current target contains the grounded paste action without payload
loss.

The six compatible sessions through `2026-08-19T20:14:13.255Z` contain 1,462
semantic events and 478 source micro-WRITEs. Episode v2 partitions the WRITEs
into:

- 115 loss-bearing closed substantive episodes;
- 101 closed history-only episodes;
- 175 conservatively excluded/unclosed micro-WRITEs;
- 35 multi-WRITE episodes, of which 33 receive loss;
- 303 absorbed source micro-WRITEs.

The normalized stream contains 216 model-facing WRITEs and suppresses one exact
repeated READ inside a merged episode. The strict audit proves exact source
WRITE coverage, unique episode ownership, raw lineage, causal context cutoffs,
zero target-member leakage, zero micro-WRITEs in history, grounded paste
masking, 40-character/six-word eligibility, and the two user-supplied blind
reconstructions around `2026-08-19T12:43:16.708Z` and
`2026-08-19T19:05:05.004Z`. A second construction was byte-identical.

The canonical candidate artifacts are:

```text
coupled-data/phase1-closed-episode-corpus-v2-candidate-20260819
coupled-data/phase1-closed-episode-pack-v2-candidate-20260819
coupled-data/phase1-closed-episode-smoke-corpus-v2-candidate-20260819
coupled-data/phase1-closed-episode-smoke-pack-v2-candidate-20260819
```

The complete 115-example pack passes the ordinary token audit. It contains
4,780 loss-bearing target tokens and seven grounded paste actions; EOS is added
once by the tokenizer-specific loader. A three-example smoke subset contains a
multi-WRITE episode, grounded paste, and longest ordinary episode. Its local
packing, causal-shift, mask, paste-marker, and EOS checks pass.

The paid private-project Qwen smoke also passes. Over 20 bounded epochs and 60
optimizer steps, weighted NLL fell from `3.573993` to `0.000011` (ratio
`0.000003105`). Exact greedy generation, EOS termination, the grounded paste
marker, sampler checkpoint, optimizer-state reload, reload NLL parity, reload
generation parity, and operation ceilings all passed for 3/3 examples. The
logical provider-operation estimate is `$2.885108` before checkpoint storage;
the billing API was still in its documented usage-lag state at report close.

Smoke artifacts:

```text
coupled-data/phase1-closed-episode-smoke-tinker-tokenizer-preflight-v2.json
coupled-data/phase1-closed-episode-smoke-tinker-plan-v2.json
coupled-data/phase1-closed-episode-smoke-tinker-run-v2.json
```

This closes the mechanical gate for episode v2. The next paid model operation
should be the regenerated three-arm score-before-update experiment, not another
memorization smoke.

## Submitted-composition onset correction (episode v3)

Episode v2 was mechanically sound but admitted several loss targets that began
partway through an already active prompt. In those cases the conditioning query
already contained the opening of the thought and the target contained only its
suffix. That trains continuation after the human has begun composing rather
than prediction of the next closed thought.

Episode v3 makes candidate discovery operate over every semantic WRITE,
including history-only transitions. For the validated ChatGPT, Claude, and
Gemini prompt surfaces, an accepted episode onset must now be proven by either:

- an empty, placeholder, or otherwise unpopulated prompt field; or
- a causal partition after the immediately preceding same-surface composition:
  a novel READ, an outside WRITE, or a prior submission.

No pause threshold is used. The exhaustive shadow pass generated 3,753 onset
probes and checked each against raw state continuity, logical editable identity,
READ novelty, and overlapping WRITE evidence. Unobservable probes are skipped;
an unexplained nonempty prompt onset is excluded rather than guessed. Return is
treated as a submission closure only on the currently validated prompt surfaces,
not every non-Obsidian editable.

The five suffix cases identified during review were replayed. Four had no
provable onset or causal partition and no longer receive loss. The remaining
parenthetical continuation follows a genuinely novel ChatGPT READ and remains a
separate loss target under the established rule that new inbound information
partitions thoughts. Another continuation beginning `until this is proven...`
is retained for the same reason. The complete earlier composition remains its
own closed historical WRITE.

Canonical artifacts:

```text
episode-review/phase1-full-episode-review-v3.json
coupled-data/phase1-full-episode-review-v3-r6-final
coupled-data/phase1-full-episode-adjudications-v3-r6.jsonl
coupled-data/phase1-full-episode-production-candidates-v3-r6.jsonl
coupled-data/phase1-closed-episode-corpus-v3-r6-final-20260819
coupled-data/phase1-closed-episode-pack-v3-r6-final-20260819
```

The final `phase1-episode-v3` / `phase1-episode-causal-v3` projection contains:

- 478 source semantic micro-WRITEs;
- 206 closed model-facing WRITE episodes;
- 110 loss-bearing substantive episodes;
- 96 closed history-only episodes;
- 196 conservatively excluded source WRITEs;
- 31 multi-WRITE episodes, 29 of them loss-bearing;
- 7 grounded paste actions and 4,514 target tokens after Qwen packing.

The strict corpus audit independently verifies empty or causal-partition onset
proofs against source event timing and identity. It also rejects the four known
unproven suffix prefixes. Corpus construction is byte-identical on replay; the
packed audit, causal-shift/mask tests, episode-review regressions, and full local
check suite pass. The prior v2 corpus and pack remain the immutable mechanical
smoke record but are superseded for the next three-arm experiment. No additional
paid smoke is required.

## Episode-construction review candidate (v4)

The user's second corpus review found that episode v3 conflated three separate
questions: which micro-WRITEs form one composition, what WRITE belongs in later
history, and whether that WRITE should receive loss. In particular, 41 of 42
flagged UI rows were excluded even though only six involved paste and most were
ordinary typed prompts or notes.

Episode v4 keeps those decisions separate:

- every semantically reconstructible transition remains a model-facing,
  history-only WRITE when closure or prediction-time onset is not proved;
- only a closed composition with an available onset query can receive loss;
- missing-onset submitted prompts may retain their complete terminal field
  value in history but cannot become targets;
- known application prompt scaffolds such as Claude's `Write a message…` are
  empty logical field states rather than removed human content;
- grammatical surface heuristics such as unmatched parentheses and trailing
  whitespace no longer reject otherwise proved compositions;
- concise submitted questions explicitly reviewed by the user may receive loss
  despite the general 40-character/six-word automatic threshold;
- pure pastes become grounded history WRITEs, while a proved mixed
  authored/paste/authored composition may receive `<|paste|>` loss without
  copied-payload loss.

The 30 user/reviewer-adjudicated cases are frozen by stable source event IDs in:

```text
episode-review/phase1-episode-regressions-v4.json
```

They comprise 18 loss episodes and 12 history-only episodes. The regression
checker requires exact decisions, exact member lineage, four concise submitted
questions, unavailable-onset exclusions from loss, uniquely grounded paste
segments, and the exact finalized content for both user-supplied blind cases at
`2026-08-19T12:43:16.708Z` and `2026-08-19T19:05:05.004Z`. The former contains
seven—not five—source micro-WRITEs once the two intervening transitions omitted
by the older review selection are included; only the complete seven-member
selection satisfies continuity and no-outside-WRITE gates.

Current immutable review artifacts:

```text
coupled-data/phase1-episode-regression-review-v4-r4
coupled-data/phase1-full-episode-adjudications-v4-r4.jsonl
coupled-data/phase1-full-episode-production-candidates-v4-r4.jsonl
coupled-data/phase1-closed-episode-corpus-v4-review-r3-20260819
coupled-data/phase1-closed-episode-pack-v4-review-r3-20260819
```

The v4 corpus partitions 478 semantic micro-WRITEs into 346 model-facing WRITE
episodes: 125 loss-bearing, 221 history-only, and 57 source transitions with no
usable semantic completion. Thirty episodes contain multiple source WRITEs and
25 of those receive loss. No source micro-WRITE ID appears directly
in model-facing history.

The tokenizer pack contains 125 examples, 4,327 target tokens, seven grounded
paste actions, and one EOS per target. Corpus audit, regression audit, packed
audit, full repository checks, and an independent deterministic replay pass.
This remains a review candidate—not authority for another paid Phase 1 run—until
the user finishes inspecting the revised loss/history assignments in the local
episode UI.

## Raw-authoritative episode review candidate (v1)

The v4 review showed that a fixed list of adjudicated event IDs was useful as a
regression oracle but was not a valid production conversion. The production
path now follows the architecture implied by the vault's sensor-first arc:

```text
immutable raw evidence
→ normalized semantic READ/WRITE primitives
→ raw-aware closed-composition episodes
→ normalized causal history and loss-bearing examples
→ tokenizer-specific packing
```

The production episode assembler accepts only the source micro-corpus, the
complete raw-evidence primitive projection, and an output directory. It does
not accept adjudications, forced decisions, example ordinals, or oracle event
IDs. The 30 user/reviewer-adjudicated cases remain test-only regressions, and a
static check rejects embedding any of their identities in the production
assembler.

Episode boundaries are decided from raw state continuity, logical destination,
affected text region, causal READ novelty, submission evidence, and field
departure. Repeated or self-derived READs do not split a composition; novel
causally available READs do. Reconstruction, structural closure, and loss
eligibility are separate statuses. Semantic micro-WRITEs remain audit lineage
only: model-facing history contains the normalized closed episodes.

Raw paste checkpoints are authoritative for authorship. This caught a real AX
epoch failure that had previously turned a pure Obsidian paste into a target
containing thousands of surrounding document characters. The corrected rule
derives the local paste transition from the synchronous pre/post checkpoint,
retains its resolved payload and provenance in later history, and gives pure
pastes no authored loss. Mixed authored/paste episodes retain authored text and
the grounded paste marker without copied-payload loss.

The complete six-session review projection contains:

- 1,462 source semantic events and 478 source micro-WRITEs;
- 317 normalized closed WRITE episodes;
- 152 loss-bearing closed substantive episodes;
- 38 excluded episode groups with no surviving authored or pasted output;
- 58 multi-WRITE episodes, 37 of them loss-bearing;
- 437 absorbed source micro-WRITEs and zero source micro-WRITEs in model-facing
  history;
- two repeated/self-derived READs suppressed inside active compositions.

Canonical review artifacts:

```text
coupled-data/phase1-raw-write-primitives-v1-20260819
coupled-data/phase1-raw-episode-corpus-v1-review-20260819
coupled-data/phase1-raw-episode-pack-v1-review-20260819
```

The corpus replay is byte-identical. Its `corpus.json`, `events.jsonl`, and
`examples.jsonl` SHA-256 digests are respectively:

```text
e0e5cf52b85601b4671dc44bae94dabe87ef18187fd67f7f50cb050a4573682f
220444badf8760c384bf86e1241e664b7566467252b5f5ddffa2c37e43c997df
a59c7f5289024f2d41587a153a56d57f27b0c788ce62f1c92a8afb9264570534
```

All 30 reviewer-oracle regressions, the raw-episode unit checks, corpus audit,
packed audit, and full repository check suite pass. The Qwen pack contains 152
examples, 4,977 target tokens, eight grounded paste actions, exactly one EOS
per target, and a maximum packed sequence length of 33,004 tokens. No provider
call or paid training was performed for this checkpoint.

This is the production-independent review candidate that supersedes v4. It is
not yet frozen for another paid Phase 1 experiment: the user should first
inspect the 152 loss-bearing episodes and their raw trajectories in the local
review UI.

## Raw episode closure and AX-epoch correction (v2)

Independent review of raw episode v1 found three remaining boundary problems.
Version 2 makes the corresponding narrow, evidence-based corrections:

- a novel causal READ closes the preceding prompt composition only when the
  following WRITE continues on the same logical field with replayable state;
  the preceding completion must meet the ordinary 40-character/six-word
  threshold rather than the short submitted-prompt threshold;
- session termination alone is not closure evidence; a final episode requires
  an independently observed submission or another structural boundary to
  receive loss;
- volatile braille progress glyphs in VS Code terminal descriptions do not
  change logical destination identity, and a same-prompt AX value reset can be
  stitched only when no submission occurred and raw local epochs remain
  independently reconstructible;
- a synchronous Cmd-V whose clipboard version matches conditioning can survive
  an opaque post-paste AX reset as an explicitly marked paste action. The
  record states that direct semantic insertion was not observed. Its payload
  remains resolved and provenance-marked in later history, while the target
  contains only the paste marker.

The motivating old #389–390 trace now reconstructs as one history-only draft:
an authored prefix, one grounded 2,436-character paste, and a final quote. It
does not receive loss because neither submission nor a closed-thought boundary
was observed. Three other previously unresolved submitted VS Code prompts now
produce mixed authored/paste targets rather than copied-payload supervision.

Relative to v1, every one of the original 152 targets is byte-identical. Seven
additional targets are admitted: four substantive thoughts partitioned by a
novel READ before same-field continuation, and three submitted mixed-write
prompts with grounded opaque paste actions. The previously reviewed incomplete
bullet-list draft remains history-only because its subsequent novel READ led to
a different destination rather than same-field continuation.

Canonical v2 review artifacts:

```text
coupled-data/phase1-raw-episode-corpus-v2-review-r4-20260820
coupled-data/phase1-raw-episode-pack-v2-review-r4-20260820
```

The projection contains 315 normalized closed WRITE episodes and 159
loss-bearing examples; 59 episodes contain multiple source micro-WRITEs and 39
of those receive loss. The pack contains 5,219 target tokens, 11 grounded paste
actions, exactly one EOS per target, and a maximum sequence length of 33,004.

The regression fixture now contains 32 cases, including same-field novel-READ
closure and the opaque formatted-paste epoch. The corpus audit also asserts
that unsubmitted session-end episodes cannot receive loss. Construction is
byte-identical on replay, and all original v1 targets remain unchanged.

The corpus, event, example, packing-manifest, and packed-example SHA-256 digests
are respectively:

```text
ff25415258977168054927076b8c0635a387d5ab738f97c07eb48cda4444a247
52fdac2e6665d7272d80574afa87b4e5598157105ee24a594259e6a5f3086cd7
576d72501a03d4284b1580ca6a3ca5c83dce2c1137b3d0ed0ec00b5b132cdd9c
3b74b68d3dcd11cfc8033ab82a07fcb426c85ab6e619a7f86563bd8d56810578
f7d42c06cecdde4257effb3b4b18ecd5c98ea29475cf5ca6ce5fc8caed361e9a
```

No provider call or paid training was performed. Version 2 supersedes raw
episode v1 for review, but it remains a review candidate until the seven newly
loss-bearing targets and the repaired #389–390 history trajectory are visually
accepted.

## Offset-grounded repeated-paste correction (raw episode v3)

Raw episode v2 still required a proven clipboard insertion to be globally
unique inside the completed episode text. That is unnecessarily strict: the
user may first author a phrase and later paste the same phrase as a quotation.
The synchronous pre/post-paste observation already records the actual field
offset of the paste.

Version 3 first maps that raw checkpoint offset into the episode's complete
initial-to-terminal edit. It accepts the placement only when:

- the exact raw insertion occurs at the observed field offset;
- replacing that raw span with a sentinel, normalizing the complete episode,
  and restoring the insertion reproduces the finalized content exactly; and
- paste placements remain ordered and non-overlapping.

The prior unique-text search remains only as a conservative compatibility
fallback for observations whose exact checkpoint offset cannot be projected.
An ambiguous location still becomes unresolved rather than guessed.

This repairs old UI #299–300. Its three micro-WRITEs now form one submitted,
loss-bearing episode whose target contains authored text, one grounded paste
action at the second occurrence of the repeated phrase, and the final authored
quote. The regression verifies the checkpoint ID, repeated authored occurrence,
paste payload, segment order, and full-content round trip.

Old #389–390 remains history-only. Its text and 2,436-character paste are
reconstructible, but closure is absent from the raw evidence. The regression
now explicitly describes this as `closure unobserved`: the conservative
history-only result does not assert that the user never submitted it.

Every v2 episode and target is unchanged except #299–300, which moves from
`paste_authorship_unresolved` history to a verified loss episode. The canonical
v3 artifacts are:

```text
coupled-data/phase1-raw-episode-corpus-v3-review-r5-20260820
coupled-data/phase1-raw-episode-pack-v3-review-r5-20260820
```

The projection contains 315 model-facing WRITE episodes and 160 loss-bearing
examples. Forty of the 59 multi-WRITE episodes receive loss. The pack contains
5,328 target tokens, 12 grounded paste actions, exactly one EOS per target,
and a maximum sequence length of 33,004 tokens.

The ten raw reducer checks, 33 episode regressions, corpus audit, packed audit,
and byte-identical replay pass. The corpus, events, examples, packing manifest,
and packed examples SHA-256 digests are respectively:

```text
a69367a001ae6a38f0dbe8024341156a9a6ba1ecb7f3e957006534eb60e37767
e44b28f2e1ea8ee1136bc2f7b9f2ce628d070747f8d9c5240247289fa2d43427
8971f310be6816ed8ef0e948aca89c9711ac95f7965ea4262556ba5767a227de
a43bf7533a30e53a5bc6800f7211bb35b6a4fa6c00fe07d28bf41b36eea643e7
77effdad1b1f3065bb86bd8c1cc4d94dbcc5d6b590bc030d813d8ad4f94c7071
```

No provider call or paid training was performed.

## Canceled-paste repair and expanded August 20 review corpus (raw episode v4)

Raw episode v4 handles the demonstrated paste-then-immediate-undo trajectory as
an observed cancellation rather than a paste target. The rule is deliberately
narrow: the synchronous paste transition must insert the conditioned clipboard
exactly, the immediately following input must be Undo, the next retained field
state must exactly restore the pre-paste value and selection, the payload must
be absent from the terminal edit, and the later checkpoint trajectory must
reach the selected terminal state. Raw paste and Undo evidence remain intact;
the finalized target contains only the surviving authored completion.

The active collection in
`coupled-data/phase1-ordinary-work-2026-08-19-10` was stopped cleanly and
reduced with semantic reducer v8. Its 2,342 raw records produced 520 READs and
198 verified micro-WRITEs; causal compilation admitted 164 micro-WRITE targets.
It was assembled chronologically with the prior six compatible sessions and
then passed through the same raw-authoritative episode constructor.

The expanded seven-session review projection contains:

- 2,180 source semantic events and 676 source micro-WRITEs;
- 450 normalized closed WRITE episodes;
- 221 loss-bearing closed substantive episodes;
- 41 excluded unresolved episode groups;
- 94 multi-WRITE episodes, 61 of them loss-bearing;
- 631 absorbed source micro-WRITEs and zero micro-WRITEs in model-facing
  history;
- four repeated/self-derived READs suppressed inside active compositions.

Canonical review artifacts:

```text
coupled-data/phase1-all-through-2026-08-20-micro-corpus-v3
coupled-data/phase1-raw-write-primitives-v2-20260820
coupled-data/phase1-raw-episode-corpus-v4-expanded-review-20260820
coupled-data/phase1-raw-episode-pack-v4-expanded-review-20260820
```

Construction is byte-identical on replay. All 34 adjudicated regressions,
including the canceled paste, pass. The corpus and packed audits pass. The pack
contains 221 examples, 7,034 target tokens, 15 grounded paste actions, exactly
one EOS per target, and a maximum sequence length of 33,004 tokens. The corpus,
event, example, packing-manifest, and packed-example SHA-256 digests are:

```text
004fd3bad6f5090f512230e913889fc11bfac5a3d2b0d08715c170ed270151ab
c22c67c68da5d419501c06720541710bde6734d4930d2ef257cd014dbd9ca763
56141b6bbb767a58cedd85d5e2aa1af0652fb3397771f185b3a9d1ebb770e36d
54bef7fdd83efa8be77b9b1f294d566e61b096a512a6c00181e36570962d0119
a0ad5a0b3740b014b9131f92ab98a8fb89cb04c0b78947d2d801cf2d9ffba66f
```

This remains a review candidate, not training authority. The read-only local
inspector serves it at `http://127.0.0.1:8767/`.

## Raw post-settlement prompt-closure evidence

The collector now retains one minimal reference after a prompt-like editable
settles: the source raw WRITE ID, held AX element, terminal observation ID,
terminal hash, and character count. It does not cache screen contents and does
not create a third semantic event.

An unmodified Return or any left click while that reference is live triggers
one bounded post-action observation. The raw `prompt_submission_observation`
record links back to the settled WRITE and retains the hit-tested action,
pre-action state, post-action state, surface validation, AX errors, and field
transition. The sensor does not require the clicked Accessibility node to label
itself as Send: that proved brittle in the first live mouse test. A click that
leaves the draft populated records that fact; a different window cannot close
the old prompt; and a later mutation supersedes the reference. The offline
semantic reducer will decide whether action plus transition proves submission.

This closes the missing-evidence gap for mouse submission after `WRITE_DELAY`
without adding online semantic interpretation.

Live validation then established both sides of the sensor contract:

- `prompt-closure-test-3` linked a settled prompt to a later mouse click on an
  `AXButton` labelled Send. The pre-action value exactly matched the settled
  terminal value, the field disappeared afterward, and the raw disposition was
  `confirmed_field_disappeared`.
- `prompt-closure-test-4` retained a populated draft when the user clicked into
  another window. Its raw disposition was `surface_changed`; it did not become
  submission evidence.

Semantic reducer v9 is now authoritative for these records. It accepts only a
linked positive transition with exact terminal observation identity/hash/count,
an exact pre-action field value, ordered timings, same-surface validation, and
either unmodified Return or a generic semantic submission button. Accepted
evidence is added to deterministic raw lineage and changes the semantic boundary
to `submission_boundary`; the original capture boundary remains separately
recorded. Negative raw observations never close a WRITE. The causal compiler
verifies the mixed WRITE/closure lineage without repeating the semantic rule.

Raw episode v5 carries this evidence into closed-composition construction. A
submitted episode becomes causally available at the actual post-action closure
observation, not at the earlier three-second write settlement. This preserves
the key invariant that the resulting prompt cannot enter history until the
submission that closed it has actually been observed.

The mechanism is surface-generic, not ChatGPT-specific. It applies to allowlisted
applications whose Accessibility surface exposes a recognizable prompt/editor
field and a provable Return or submission control transition. Stable document
editors continue through the ordinary raw WRITE path unchanged. Terminal-style
surfaces remain an empirical validation case rather than an assumed capability.

The live positive and negative traces reduce and compile with zero rejected
WRITEs. The positive trace also completes raw episode v5 construction, corpus
audit, and tokenizer packing; its normalized WRITE `availableAt` equals the
post-submission observation time. The full repository check suite passes. No
provider call or paid training was performed.

### Cross-surface validation

`prompt-closure-multisurface-test-5` exercised ChatGPT, Chrome prompt fields,
Obsidian, and VS Code/terminal surfaces. The collector was stopped explicitly
at 127 raw records before the final replay.

The ChatGPT Return case passed end to end: the settled completion was retained,
the later Return produced `confirmed_field_disappeared`, semantic v9 emitted a
`submission_boundary`, and raw episode v5 delayed causal availability until the
post-action observation. Chrome clicks that merely preserved the draft or
changed surface remained non-submission evidence. This trace did not provide a
second confirmed Chrome mouse-Send transition, so generic Chrome mouse closure
remains supported by the raw architecture but should not be claimed as newly
validated by this particular session.

The Obsidian multiline negative test correctly remained an ordinary
`write_delay_elapsed` WRITE rather than submission. It also exposed a second
AX list-scaffold rendering: current Obsidian sometimes emits
`newline + zero-width + newline + dash`, without the extra zero-width-only line
seen in the older fixture. The reducer's exact structural grammar now accepts
both observed variants while preserving the original transition in
`observedNetEdit`. The reconstructed authored completion contains the four
typed lines and no zero-width or bullet scaffold. A permanent regression covers
the compact variant.

The final session replay produced 12 READs and nine semantic WRITEs with zero
causal-compiler rejections. Raw episode v5 produced eight model-facing closed
episodes, three test-only loss-bearing episodes, and zero source micro-WRITEs in
model history. Its corpus audit passes. These validation strings are not part
of the ordinary-work training corpus.

### Chrome mouse-Send action semantics

The isolated `prompt-closure-chrome-send-test-6` proved that the state sensor is
correct in Chrome/Gemini: immediately before the click the held prompt contained
exactly `chrome mouse submit validation 2026-08-20`; 150 ms later the same field
had restored `Ask Gemini`; and the retained screenshot showed the exact prompt
as a submitted user message. A later READ captured Gemini's response.

Semantic v9 nevertheless rejected the closure because Chrome exposed the
clicked control as an unlabeled `AXGroup`. The initial action sensor inspected
only four AX ancestors, and the reducer unnecessarily required both an
`AXButton` role and a semantic submission term. This is an action-semantics
coverage failure, not a missing field transition.

The generic correction is semantic reducer v10 plus a deeper bounded action
probe. The collector now inspects up to ten ancestors of the hit-tested node,
preferring a node carrying a submission term and retaining button evidence as a
fallback. The reducer accepts an exact submission term from that bounded clicked
ancestry even when a browser reports the semantic node as a group rather than a
button. It still requires the complete pre/post field-transition proof; an
unlabelled click cannot close a WRITE. A permanent fixture verifies a semantic
`send` ancestor with role `AXGroup`. The full repository check passes. This
specific collector-side expansion still requires one live Chrome replay before
v10 is treated as validated.

## Semantic v10 replay and raw episode v6

All seven compatible ordinary-work sessions through August 20 were replayed
from immutable raw journals with semantic reducer v10 and causal compiler v14.
The replay produced 2,184 semantic events and 541 eligible micro examples with
zero causal-compiler rejections. Those micro examples remain an audit layer;
they are not the Phase 1 prediction unit.

Raw episode v5 initially converted the replay into 225 closed-composition
targets. A target-level comparison against the manually reviewed 221-example
v4 baseline found 221 byte-identical targets and four additions. One addition
was invalid as an independent thought: an Obsidian pointer relocation allowed
one character to materialize between the preceding terminal snapshot and the
next synchronous BEFORE, splitting the sentence at `who a` and supervising the
suffix `ren't using...` independently.

Raw episode v6 repairs that class at the composition layer without a language
heuristic. It permits a discontinuous pair to remain in one episode only when:

- the exact same retained AX element and logical destination are observed;
- the prior boundary is a pointer-selection boundary;
- the gap is no more than three seconds;
- neither side contains paste or cut provenance;
- the existing composition is already substantive; and
- the bridge edit and final net edit stay inside the active authored region.

The target still comes from the episode's original BEFORE and final AFTER.
Intermediate text and the unobserved bridge never receive their own loss. A
permanent regression proves the positive case and rejects a different element
or a gap above the bound.

The canonical review artifact is:

```text
coupled-data/phase1-raw-episode-corpus-v6-v10-review-20260820
```

It contains 455 model-facing closed episodes and 224 loss-bearing examples.
Ninety-five episodes contain multiple semantic WRITEs, 62 of which receive
loss; 638 source micro-WRITEs are absorbed and none appear independently in
model-facing history. Against the v4 baseline, 220 targets are byte-identical,
the incomplete GTM target is replaced by its finalized three-member thought,
and three new complete targets are admitted. The standalone `ren't using...`
target is absent. All 34 adjudicated episode regressions and the corpus audit
pass. A second construction is byte-identical.

Canonical SHA-256 digests are:

```text
corpus.json    04f53a8544aac13e2515d8e582794d4f88dba6e6578cc65a734e299191d96a11
events.jsonl   4791358e5a3cc63982014a9af61c170be023e69991ff2856a20e3d14730f97e2
examples.jsonl c72949ca16f2a040420e692dcb4d22d366d6c241a3653cfb17416753b4c86c8c
```

The read-only target-delta inspector serves this corpus against the reviewed
v4 baseline at `http://127.0.0.1:8768/`. The default view shows the four new
example identities, including the corrected replacement target; it also
reports the one superseded v4 example.

The user reviewed all four target deltas on August 20 and accepted them. Raw
episode v6 is therefore the frozen dataset-construction baseline for the next
ordinary-work collection and Phase 1 comparison. Later interpretation changes
must use a new version and preserve v6 artifacts rather than rewriting them.

### Qwen mechanical smoke for raw episode v6

A three-example private-project Tinker smoke exercised the corrected
three-member GTM episode, the shortest grounded-paste target, and the longest
ordinary target. The pack contained 387 loss-bearing positions and exactly one
paste action; all causal-shift, mask, paste-marker, and EOS audits passed before
transmission. Tinker's tokenizer matched the frozen local vocabulary and every
one of the 8,322 token IDs used by the pack.

The bounded rank-32 Qwen3.5-9B-Base LoRA run used 20 epochs, 60 optimizer
steps, and 1,853,240 submitted training positions. Weighted NLL fell from
`3.495846312` to `0.000194417` (ratio `0.000055614`). Exact greedy generation,
EOS termination, the grounded paste marker, sampler checkpoint, optimizer
state reload, reload NLL parity, reload generation parity, and all operation
ceilings passed for all three examples. This is a mechanical compatibility
test, not Phase 1 hypothesis evidence.

Artifacts:

```text
coupled-data/phase1-raw-episode-smoke-corpus-v6-v10-20260820
coupled-data/phase1-raw-episode-smoke-pack-v6-v10-20260820
coupled-data/phase1-raw-episode-smoke-tinker-tokenizer-preflight-v6-v10-20260820.json
coupled-data/phase1-raw-episode-smoke-tinker-plan-v6-v10-80bf5b7-20260820.json
coupled-data/phase1-raw-episode-smoke-tinker-run-v6-v10-80bf5b7-20260820.json
```

The approved plan SHA-256 is
`bf679a7d5bfcfc61cc22df2e0e4d11367153da7eaeb0789c13e1edfa029547bd`;
the completed run SHA-256 is
`c19c12e618cc19c6f244824e302e304e9f48ece90160aee643b02cabc4481d38`.
Logical provider operations are estimated at `$3.018118` before checkpoint
storage. Tinker's billing API still reported its normal pending-usage lag when
the run closed, so this estimate is not presented as final billed cost.

## Raw episode v6 foundational-experiment preflight

The frozen raw episode corpus now crosses the experiment boundary through
`phase1-raw-episode-experiment-adapter-v1`. The adapter does not rewrite the
corpus or reinterpret an episode. It verifies the separately hashed
`episode-blocks.jsonl`, requires every example exactly once in chronological
order, and exposes the five frozen blocks in memory to the provider-neutral
runner. The blocks contain `50 / 50 / 50 / 50 / 24` examples. Block 1 is a
50-example training-only warm-up; blocks 2–5 contain 174 provider-scored
examples. A tampered or reordered block ledger is rejected.

The canonical full pack is:

```text
coupled-data/phase1-raw-episode-pack-v6-v10-canonical-20260820
```

It contains 224 examples, 7,192 loss-bearing target tokens, 15 grounded paste
actions, and a maximum complete sequence of 33,003 tokens. The Qwen tokenizer
revision, causal shift, masks, literal paste marker, and one native EOS per
target pass the standalone packed audit. Canonical digests are:

```text
packing.json          4783edfff0bc3cbd95c1a9c7d1543edf81109ce942744d516c99163c29afff3d
packed-examples.jsonl 7ea374de1d69762c843fb9fb1a13b0b7613a73282fd2f3bd01f96ea495e1ec84
context-plans.jsonl   5bdc16d211dd08ae5cb30ca081cd679bbd19dcb242541d801964b44f906b0095
```

The no-network mock at
`coupled-data/phase1-raw-episode-mock-v6-v10-canonical-20260820` produced 522
scores, five cumulative updates, and 23,889 loss-bearing-token presentations.
Its manifest SHA-256 is
`47fdd5e5449032b2cf50f0ee29ca31ad944da1e251ac50a09dbaae65106df73b`.

Generated predictions will be reviewed under the frozen blind rubric
`experiment/phase1-blind-semantic-review-v1.json`, SHA-256
`8ad7e4115bef5f5480818a1ef4ffee3cf9d21b8dbc2a6c1508c69d1c0be27687`.
Model identity, likelihood, automatic similarity, cost, and latency remain
hidden until every judgment is frozen. The headline cross-model semantic
measures are usable rate and usable-or-directionally-correct rate; automatic
string metrics remain secondary. Paired pre-update NLL and bits saved remain
the primary Qwen personalization measure.

Provider plan v5 currently remains deliberately blocked. Five cumulative
updates and 174 two-arm Qwen scores project `$50.002718` in Tinker operations:
`$33.590142` training, `$15.057115` prefill, `$0.355461` maximum sampling, and
`$1.00` checkpoint reserve. This exceeds the previously reviewed `$40.00`
ceiling, so the plan cannot be executed. The blocked plan is
`coupled-data/phase1-raw-episode-provider-plan-v6-v10-blocked-40usd-20260820.json`,
SHA-256
`5de3936693bb49e79216fe790fadf45ef54ac96029367173fc2875939996e607`.
No credentials, provider call, personal-data transfer, or paid operation was
used during this preflight. A newly reviewed ceiling requires a freshly hashed
provider plan; the runner requires the command-line maximum to match it
exactly.

The 224-example corpus is developmental and mixes historical sensor regimes:
148 targets came from three-second READ / 35-percent-bottom-crop sessions,
three from one-second / 35-percent, and 73 from one-second / 10-percent.
Historical screenshots can be recropped when retained, but their capture time
cannot be moved retroactively. This qualification does not break causal
ordering, but later prospective confirmation should use homogeneous
one-second READ / ten-percent crop sessions.

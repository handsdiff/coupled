# Coupled

Coupled is a small, local-only macOS experiment for inspecting the signals that
might eventually support read/write inference. The current recommended starting
point is deliberately narrow: verify raw input triggers before attempting to
interpret content.

## Trigger-first workflow

The `triggers` command has one path: a global `CGEventTap` receives an event and
Coupled immediately appends one flat record to `triggers.jsonl`. It performs no
Accessibility queries, settling, coalescing, viewport traversal, editable-field
diffing, or read/write inference.

```sh
./scripts/package-app.sh
./scripts/coupled doctor --prompt-permissions
./scripts/coupled triggers \
  --output ./coupled-data/trigger-test \
  --pause-file ./.coupled-pause
```

Grant `dist/Coupled.app` **Input Monitoring** permission. Accessibility is not
required for this command. Always use the wrapper so Launch Services associates
the process with the app grant.

Every keyboard down/up, modifier change, mouse movement, click, drag, and scroll
event is preserved individually. Pointer and scroll records include their global
coordinates, Core Graphics display ID, and display bounds so activity on an
external display can be distinguished directly. Keyboard records include only
event direction, repeat state, and active modifiers; typed characters and raw
key codes are not retained.

Follow the compact live mirror with `./scripts/coupled logs`, stop with
`./scripts/coupled stop`, and inspect counts without dumping sensitive records.
The compact view retains only the event time, kind, application, and the fields
most useful for checking behavior. Use `./scripts/coupled logs --raw` when the
complete JSON record is needed.

```sh
jq -r '.kind' ./coupled-data/trigger-test/triggers.jsonl | sort | uniq -c
jq -r 'select(.displayID != null) | .displayID' \
  ./coupled-data/trigger-test/triggers.jsonl | sort | uniq -c
```

The interpreted collector described below remains experimental and should not be
used to evaluate content capture until the raw trigger stream is understood.

## Character writes

Once trigger capture is verified, `writes` adds two small transformations:
each key-down event's Unicode text is split into user-perceived characters, then
characters are grouped by application until no new character has arrived for
`--write-delay` seconds. One settled write record is appended to `writes.jsonl`
and mirrored to stdout.

```sh
./scripts/coupled writes \
  --output ./coupled-data/write-test \
  --write-delay 3 \
  --pause-file ./.coupled-pause
./scripts/coupled logs
```

Each record has `kind: "write"`, `provenance: "typed_character_burst"`, the
concatenated `content`, application identity, first/last character timestamps,
emission timestamp, delay, sequence, character count, key-down count, and repeat
count. Separate timers are maintained per application process, so quick app
switches do not merge their content. Command/Control shortcuts, navigation keys,
and non-writing control keys are ignored. Return and Tab remain characters.

This is temporal grouping, not field-level observation: there is still no field
lookup, before/after diff, edit interpretation, or Accessibility query. A write
means “these typed characters settled in this app,” not “this exact text was
confirmed in a document.”

This simplicity has an important privacy consequence: `writes` cannot know that
a focused field is secure and can therefore capture password characters. Pause
or stop it before entering secrets. Input methods, dictation, paste, app-driven
changes, and text inserted without a key-down event are not captured by this
stage.

## Read candidates

The `reads` command adds timing only. Clicks, scrolls, pointer movement, and
pointer drags are grouped by application process and display until no new
pointer activity has arrived for `--read-delay` seconds. It then appends one
`read_candidate` record to `reads.jsonl` and mirrors it to stdout.

```sh
./scripts/coupled reads \
  --output ./coupled-data/read-candidate-test \
  --read-delay 3 \
  --pause-file ./.coupled-pause
./scripts/coupled logs
```

Each record contains `provenance: "settled_pointer_activity"`, trigger types,
activity count, first/last activity timestamps, emission timestamp, application
identity, topmost window ID/title/bounds at the pointer, the last pointer
coordinates, and Core Graphics display ID/bounds. Separate
application/window/display timers prevent activity in two windows or on the
HDMI and built-in displays from merging.

Window attribution uses the front-to-back Core Graphics window list, not
Accessibility. Lookups are refreshed on clicks and scrolls and briefly cached
during high-rate pointer movement. A click-away is therefore attributed to the
target window under the pointer instead of relying on the pre-click frontmost
application.

This stage captures no Accessibility data or screen text. A candidate means
only “pointer activity settled over this window”; it is not yet evidence that
text was visible, attended to, or read. Window titles are metadata but may still
contain sensitive document or page names.

## Combined visible events

The `events` command combines screen-text reads with settled edits in focused
editable fields in the allowlisted applications.

```sh
./scripts/coupled events \
  --output ./coupled-data/visible-events-test \
  --read-delay 3 \
  --write-delay 3 \
  --pause-file ./.coupled-pause
./scripts/coupled logs
```

Provisional events are appended in emission order to `events.preview.jsonl` and
mirrored to the same live log. Raw evidence is the collection authority;
finalized `events.jsonl` is created later by `coupled reduce`. `Coupled.app` is headless; capture is started and stopped from
the terminal. `dist/Coupled Logs.app` is a separate, independently launchable
viewer with no collector controls. It shows the active run's immutable resolved
settings and the identical compact event stream. Open it from Finder, Spotlight,
the Dock, or with `./scripts/coupled viewer`; it may remain closed or open
independently of collection.

Both Coupled bundle identifiers are permanently excluded from READ and WRITE
collection. Keep the viewer outside captured work-window bounds because an
overlapping window can still alter the pixels in a rectangular screen capture
even when its application is not tracked. Source observations are appended
separately to `raw.jsonl`:

A mutating key is also an event boundary for a pending read on the same Core
Graphics window. The key invalidates pointer activity whose read delay has not
yet settled, so that timer cannot photograph partially authored text and emit
it before the corresponding write. The invalidated candidate remains in
`raw.jsonl` with `read_candidate_superseded_by_write`; it does not produce a
derived read. Pointer or scroll activity after the final key starts a fresh
read delay and therefore settles after the write.

- `WRITE` is attempted in Obsidian, Chrome, Codex, and VS Code by default. An active
  event tap attempts to capture a focused text area, text field, or combo box
  before returning the first mutating key. The same retained Accessibility
  element is queried after the write delay, and a minimal insertion, deletion,
  or replacement is emitted only when both states are complete and no
  event-tap timeout occurred. Confirmed `AXPlaceholderValue` text remains in
  raw evidence but is treated as an empty logical field during derivation.
  Unmodified Return settles an active single-line field immediately. In a
  multiline field it synchronously retains a pre-Return checkpoint, then keeps
  the normal delay. If terminal capture is invalid, empty, or equal to BEFORE,
  a meaningful checkpoint can supply the write; otherwise AFTER remains the
  source. Every checkpoint and its selection, timestamp, and AX errors remains
  raw. A typed checkpoint that is fully reverted before settlement is treated
  as no change rather than resurrected as output. The clipboard snapshot
  available at write onset is part of conditioning.
  Cmd-V synchronously captures the held field before paste and again 50
  milliseconds afterward, and retains a raw audit screenshot without creating
  a READ. Normally the editable transition must exactly match the conditioned
  clipboard version. If a proven Cmd-V causes the same retained editable to
  begin a new empty AX observation epoch, Coupled composes the locally settled
  prefix, grounded paste, and locally settled suffix as one WRITE completion.
  `observedNetEdit` remains the literal initial/final AX diff while
  `resolvedCompletion` and `authorshipSegments` represent the action being
  learned. Unexplained AX resets and other ambiguous spans remain ineligible.
  A removal-only burst (Backspace/Delete or Cmd-X) cannot treat newly exposed
  prompt text as user-authored insertion. Every validated write also carries a
  `conditioningState` captured synchronously before the application receives
  the first mutation. It identifies the available destination and retains up
  to 512 characters on each side of the caret, bounded selected text, both
  UTF-16 and Character selection coordinates, and field length. The distinct
  `outcome` retains the net edit operation and offset. Secure fields are
  ignored. Tune the semantic radius with `--cursor-context-characters`.
  The raw BEFORE observation also carries an experimental
  `axRangeCursorProbe`. It asks the same Accessibility element for text before,
  inside, and after its selected range using `AXStringForRange`. These strings
  become the semantic cursor context for newly compiled training examples but
  never change write reconstruction. Numeric offsets and their comparison with
  later mutations remain diagnostic evidence only. If an
  Electron-provided right extent is rejected, the raw probe retries bounded
  shorter ranges from the same provider-native selection boundary.
- `READ` captures the visible rectangle of the topmost window surface after the
  read delay, removes 10% from both the left and right, 10% from the top, and
  35% from the bottom, then uses macOS Vision recognition locally to emit its
  text with provenance `screen_ocr`. Trigger-time app/window identity is kept
  only as `triggerSurface` provenance. At settlement, the collector resolves
  the current app, window, display, title, and bounds together; only that
  capture-time surface labels the screenshot and OCR. If it differs from the
  trigger surface, the stale candidate remains raw and a fresh delay starts for
  the newly observed surface instead of immediately emitting it. Pointer
  activity is globally collapsed to one pending attention candidate, app
  activation starts a new interval using the plausible content window beneath
  the pointer or the app's largest plausible window (not narrow renderer and
  toolbar helpers), and a short post-click observation lets navigation finish
  before its interval begins. A surface change while the
  asynchronous screenshot is completing suppresses the derived read and starts
  another interval. These rules prevent old per-window timers from capturing a
  newly visible surface before it has satisfied `READ_DELAY`.

The crop now retains the middle 80% of the selected window's width and the
vertical band from 10% through 65%. It is not a claim about gaze or attention.
Tune it with `--viewport-side-crop` (zero to less than `0.5`),
`--viewport-top-crop`, and `--viewport-bottom-crop`; the top and bottom values
must sum to less than `1`. Setting all three to `0` restores full-window
capture. Each read retains `windowBounds`, the actual cropped `captureBounds`,
and all crop fractions so the transformation remains auditable.

Chrome surfaces under 300 pixels tall, plus extremely narrow surfaces under
100 pixels wide, are treated as auxiliary browser UI. Their recognized content
and a `chrome_auxiliary_surface` suppression reason remain in `raw.jsonl`, but
they do not produce derived reads or disturb primary-viewport deduplication.

Every collection starts by creating an immutable `session.json`. It contains a
random `sessionID`, start time, resolved delays and crop settings, limits,
allowlists and exclusions, OCR settings, record schema versions, application
version, and an SHA-256 digest of the collector executable. The same
`sessionID` is injected at the top level of every raw and derived JSONL record.
Coupled refuses to reuse an output directory that already contains a manifest
or nonempty collection file, preventing records from different configurations
from being silently appended together.

Collection files deliberately remain in operational emission order. Timing
semantics version 2 defines the initial causal conversion as:

```text
read.available_at        = read.capturedAt
target_write.began_at    = target_write.beganAt
prior_write.available_at = prior_write.terminalDecisionAt

context(target) = stable_sort_by_available_at(
    events where event.available_at < target_write.began_at,
    preserving emission order for equal timestamps
)
```

For reads, `settledAt` is when screenshot capture was requested, `capturedAt`
is recorded immediately when the capture callback supplies the image, and
`observedAt` is later emission after OCR. For writes, `beganAt` is recorded on
entry to the first mutating-key callback. `usedObservationCapturedAt` records
the terminal or checkpoint observation chosen for derivation, while
`terminalDecisionAt` records when the collector had enough evidence to choose
between them. `derivationObservationSource` and `fallbackReason` make that
choice explicit. `configuredWriteDelaySeconds` describes the configured quiet
period; `boundaryReason` states whether that period elapsed or Return/focus
caused earlier settlement. Raw write evidence is persisted before provisional
preview interpretation. The versioned `reduce` command constructs finalized
READ/WRITE events from `raw.jsonl`; `compile` then verifies hashes and lineage,
assigns causal `availableAt`, and constructs ordered training data without
repeating semantic reduction. A write's conditioning snapshot can complete milliseconds
after its input event was intercepted, but the active event tap has not yet
returned that mutation to the application. Its explicit capture semantics are
`synchronous_before_application_mutation`; it is query state rather than an
ordinary history event whose time is backdated.

Each raw OCR observation retains the complete recognized viewport before
overlap removal. By default it also retains the full-window source image as an
owner-only PNG under `screenshots/`, records its SHA-256 digest and dimensions,
and applies the configured crop only as Vision's recognition region. This makes
OCR and crop rules rerunnable without changing the derived stream. Disable
image retention deliberately with `--no-retain-screenshots`. Normalized line
overlap is removed from adjacent OCR viewports
only when app, window, and display all match. The event's `content` contains
newly visible lines in display order; `recognizedLineCount`, `emittedLineCount`, and
`overlapRemovedLineCount` make the transformation inspectable. Exact duplicate
viewports emit no event. An intervening write or different context resets the
comparison, preserving a later reread. Capture is allowlisted by bundle to
Obsidian (`md.obsidian`), Chrome (`com.google.Chrome`), Codex
(`com.openai.codex`), and Visual Studio Code (`com.microsoft.VSCode`), so all other
applications are ignored. Deliberately expand the boundary with
`--allow-bundle`; `--exclude-bundle` and `--exclude-app-name` can narrow it.

The default log shows each verified insertion/removal and the first eight
recognized lines of each OCR read, preserving line breaks. Use
`./scripts/coupled logs --full-text` for every recognized line in a readable
form, or `./scripts/coupled logs --raw` for the full JSONL event.

This command requires **Input Monitoring**, **Screen Recording**, and
**Accessibility** for `dist/Coupled.app`. OCR is an observation of visible
pixels, so it naturally includes occlusion and can make recognition errors.
The write collector does not poll or cache inactive editors. It retains one
focused Accessibility element and its initial value only while a burst is
active. One raw before/after audit record is stored per attempted burst. It
contains the time and mutation class of each input signal, but never the key's
character value. A 50-millisecond quiet checkpoint after mutation-capable input
preserves transient field state when the field disappears before normal
settlement. If an application does not expose a complete editable `AXValue`,
the raw attempt records the failure and no derived write is guessed. Both files
and retained screenshots can contain sensitive information; use the pause file
before handling it.

## Phase 1 reduction and causal compilation

For a release reduction, rebuild the CLI first. `./scripts/check.sh` validates
the current source but does not refresh `.build/debug/coupled`; invoking a stale
debug executable can produce artifacts from older reducer logic.

```sh
./scripts/build.sh
```

Then reduce a completed raw session and compile the finalized semantic artifact:

```sh
./scripts/coupled reduce \
  --input ./coupled-data/SESSION \
  --output ./coupled-data/SESSION-phase1-events-v6

./scripts/coupled compile \
  --input ./coupled-data/SESSION-phase1-events-v6 \
  --source ./coupled-data/SESSION \
  --output ./coupled-data/SESSION-phase1-v13

./scripts/audit-causal-dataset.sh \
  ./coupled-data/SESSION-phase1-v13 \
  ./coupled-data/SESSION-phase1-events-v6 \
  ./coupled-data/SESSION
```

The reduction directory contains only `events.jsonl`, `unresolved.jsonl`, and
`reduction.json`. Every finalized event embeds deterministic raw lineage, the
selected observation, its rule, and its decision reason. Event IDs depend on
session, lineage, and output ordinal—not reducer version. The manifest binds
the session/raw and finalized-artifact SHA-256 digests.
`events.preview.jsonl` is never read by reduction or compilation.

The fresh output directory contains:

- `events.jsonl`: the versioned causal event projection, stably ordered by
  `availableAt` and original JSONL line for ties. Each record contains compact
  model-facing `serialized` JSON and a richer `auditSerialized` projection.
- `examples.jsonl`: one example per eligible nonempty content write, with the exact causal
  prefix, pre-mutation `conditioningState`, serialized `query`, complete
  `modelInput`, structured authorship `target`, target metadata, source lines, and raw-attempt
  lineage.
- `target-exclusions.jsonl`: verified writes retained in causal history but not
  used as Phase 1 targets, including pure deletions and new writes without a
  complete semantic cursor capture. Legacy sessions retain their earlier
  conservative numeric-cursor eligibility rule.
- `context-exclusions.jsonl`: stale delayed reads omitted from causal history
  because later keyboard input superseded their pointer trigger before the
  screenshot was captured.
- `rejections.jsonl`: malformed or integrity-invalid finalized events. Semantic
  ambiguity belongs in the reducer's `unresolved.jsonl`. That file also records
  deliberate non-event dispositions such as adjacent duplicate READs and
  auxiliary browser surfaces; its count is not an ambiguity count.
- `dataset.json`: conversion rules, source digests, counts, and schema details.

The reducer verifies local observation transitions and conservatively leaves
ambiguous evidence unresolved. A delayed READ whose trigger predates a WRITE
and whose capture lands inside that WRITE is removed before viewport overlap.
Genuine new activity during a long WRITE remains eligible only when its OCR
does not contain a proven active-WRITE prefix.
Verification/credential fields and fast-start attempts without true
pre-mutation conditioning remain explicit non-events. Catastrophic terminal AX
epoch changes may use a complete post-final-input checkpoint only under a
strict large-discontinuity and locally coherent-trajectory rule. READ overlap is
then computed from `capturedAt` with every observed WRITE `beganAt` boundary,
never raw append order. Ordered mutation checkpoints may disambiguate only
equivalent minimal BEFORE-to-AFTER edits; temporary corrected text cannot enter
the target. Cut-only transitions remain in history with no authored target.
A premature post-paste checkpoint may use a later same-field observation only
when its local transition contains the exact conditioned clipboard once, with
only structural whitespace around it. If paste authorship remains ambiguous but
complete BEFORE and AFTER states prove the document transition, that WRITE stays
in later history with an explicit unresolved segment and receives no target
loss. Same-editable navigation attempts become one resulting-content WRITE when
the next BEFORE proves either an application completion or exactly unchanged
value, caret, and selection; observable cursor movement remains a boundary. The
compiler verifies raw lineage plus source and artifact hashes without
independently choosing the semantic observation.
A target's context contains only events whose
`availableAt` is strictly earlier than its `beganAt`; append/emission order is
never treated as causal order. Reads become available at `capturedAt`, and
prior writes at `terminalDecisionAt`. Records explicitly marked
`phase1Eligible: false`—including future displayed model predictions—are
excluded before contexts and targets are built.

Conversion `phase1-causal-v14` defines the supervised example as:

```text
modelInput = causal read/write history + destination/cursor/clipboard query
target     = authored-text segments + grounded paste-action segments
```

For text-only targets, v14 requires at least four user-perceived characters
after trimming surrounding whitespace. Shorter WRITEs remain in causal history
but receive no target loss. A grounded paste action bypasses this minimum, so
paste-only and mixed authored/paste targets remain eligible. The manifest pins
the threshold and every exclusion records its measured authored length.

The model-facing cursor is range-native semantic state: bounded text before the
caret, selected text, and text after it. Known empty AI prompt chrome is stored
separately as `surfacePrompt` while editable context remains empty. Numeric AX
offsets, operation, removed content, and net edit offset remain audit metadata
and receive no loss. Numeric cursor agreement never changes a write or the
eligibility of a new range-native example. Older raw sessions remain supported
under their earlier conservative numeric-cursor gate because they do not
contain range-native evidence.

Model-facing history uses a compact semantic schema. READ records retain kind,
content, application, and window. A structured WRITE retains its resolved text
exactly once in authorship segments, together with destination, operation, and
nonempty removed content; legacy unsegmented WRITEs retain top-level content.
Bundle IDs, collector provenance, numeric offsets, boundary reasons, and paste
checkpoint IDs remain in `auditSerialized` rather than consuming model context.

The compiled `modelInput` remains complete and tokenizer-independent. The
packer tokenizes each context event as one independent JSON-plus-newline block,
then retains the newest complete blocks and the complete query within `L`
tokens (`L = 32768` initially). If the oldest retained event alone crosses the
boundary, only its semantic text tail is retained with an explicit marker;
structured WRITEs preserve the surviving authorship boundaries, and the packer
never emits partial JSON. WRITE history retains resolved pasted
content and its provenance, while the current target omits pasted payloads. The
target loader tokenizes authored spans with
automatic special tokens disabled, maps each proven paste to the reserved
`<|paste|>` marker encoded by the unchanged tokenizer, appends exactly one
`tokenizer.eos_token_id`, and applies loss to authored tokens, paste-marker
tokens, and EOS—not pasted payload tokens. EOS is structural; it is not part of
captured human content. Thus tokenizer-specific packing is a deterministic
loader step, not a sensor or causality assumption.

Pack a compiled dataset with the selected Qwen tokenizer in an isolated Python
environment:

```sh
uv venv .build/tokenizer-venv
uv pip install --python .build/tokenizer-venv/bin/python \
  -r scripts/tokenizer-requirements.txt

.build/tokenizer-venv/bin/python scripts/pack-phase1-dataset.py \
  --input ./coupled-data/SESSION-phase1-v11 \
  --output ./coupled-data/SESSION-phase1-v11-qwen-pack \
  --revision 68c46c4b3498877f3ef123c856ecfde50c39f404

python3 scripts/audit-phase1-packed.py \
  ./coupled-data/SESSION-phase1-v11-qwen-pack
```

The packer refuses an existing output directory and does not modify the causal
dataset. `packed-examples.jsonl` contains the combined input/target token IDs,
attention mask, and causal-LM labels. Model-input labels are `-100`; authored
text, every existing-token ID that spells the paste marker, and the final EOS
receive loss. The saved `tokenizer/` directory has the checkpoint's unchanged
vocabulary. `packing.json` pins the tokenizer revision, marker string and token
sequence, dependency versions, file digests, truncation rule, and audit counts.
It also records that the configured input budget covers history plus query; the
target is appended outside that budget, may never be truncated, and determines
the minimum total sequence capacity required from the training harness.
No embedding resize or custom-token checkpoint is required.

At inference, only a complete decoded `<|paste|>` marker is an executable paste
action. A partial marker is invalid output. A human-authored literal marker has
the same token sequence; this rare collision is an explicit simplicity
tradeoff for the initial baseline rather than hidden tokenizer behavior.

## Experimental interpretation

The older `collect` command attempts to observe what text was plausibly visible
and changed. It is retained as a later layer, not as the baseline sensor.

It produces two append-only JSONL streams:

- `raw.jsonl`: coalesced input activity, visible Accessibility elements, and
  focused editable-field observations.
- `events.jsonl`: interpreted `read` and `write` events. The same events are
  emitted on stdout for a live debugging view.

No data leaves the machine. Raw key values are never recorded.

## Build and permissions

```sh
./scripts/package-app.sh
./scripts/coupled doctor --prompt-permissions
```

Coupled is packaged as an application with the stable bundle identifier
`com.niyant.coupled`; use this bundle for permission grants rather than the
ephemeral `.build/debug/coupled` executable.

For the experimental interpreted collector, in **System Settings → Privacy & Security**:

1. Open **Accessibility**, click **+**, press **Command-Shift-G** in the file
   chooser, paste `/Users/niyant/coupled/dist/Coupled.app`, choose **Open**, and
   enable **Coupled**.
2. Open **Input Monitoring** and repeat the same **+** flow for `Coupled.app`.
3. Quit any running Coupled process and start it again. If macOS asks you to
   restart the app, do so.

If the prompt already inserted Coupled into either list, simply turn its switch
on. Confirm both grants with:

```sh
./scripts/coupled doctor
```

Always run Coupled through `./scripts/coupled`. Directly executing either
`.build/debug/coupled` or `Coupled.app/Contents/MacOS/coupled` creates a terminal
child process that macOS does not associate with the app's privacy grant.
Repackage before testing new code. If a rebuilt bundle stops being trusted,
remove the old Coupled entry from the privacy list, package again, and add the
bundle again.

By default, Coupled asks Chromium and Electron applications to construct their
renderer Accessibility trees before each capture. Without that activation,
Chrome, Obsidian, VS Code, and similar applications may expose only a shallow
window tree with no readable or editable text. Snapshot records include an
`accessibilityActivation` result for diagnosis. Use
`--no-activate-renderer-accessibility` only when measuring the behavior without
this opt-in; disabling it can reduce those applications' Accessibility work but
will usually make their content unavailable to Coupled.

The build wrapper selects the compatible macOS 15.5 SDK already installed on
this machine because its currently selected compiler and default 26.2 SDK have
different patch versions. Set `COUPLED_SDKROOT` to override that choice.

## Collect

Start with an explicit run directory and pause switch:

```sh
./scripts/coupled collect \
  --output ./coupled-data/first-run \
  --pause-file ./.coupled-pause \
  --exclude-bundle com.apple.Terminal
```

The Terminal exclusion prevents a live `tail` or stdout view from feeding back
into the collector. Remove it when terminal activity is the subject of a test.
Follow interpreted events in another terminal with:

```sh
./scripts/coupled logs
```

Or open the independent native viewer at any time:

```sh
./scripts/coupled viewer
```

When collection is live, the viewer follows that run and displays its resolved
`session.json` configuration. Otherwise it displays the most recent launch log
and reports that no collection is active.

Add or remove the pause file without stopping the process:

```sh
touch .coupled-pause
rm .coupled-pause
```

To inspect one application without global Input Monitoring, focus its window
and run:

```sh
./scripts/coupled snapshot --output ./coupled-data/snapshot-test
```

Common tuning options:

```text
--read-delay 3          wait for pointer/scroll activity to settle
--write-delay 3         wait for keyboard activity to settle
--poll-interval 0.35    detect focus/window changes
--max-characters 30000  bound captured text per snapshot and editable field
--cursor-context-characters 512
                        retain semantic text on each side of the write caret
--max-nodes 1200        bound Accessibility-tree traversal
--read-on-write         treat the post-write viewport as a read candidate too
--no-activate-renderer-accessibility
                        leave Chromium/Electron renderer AX trees disabled
--exclude-bundle ID     exclude an app; repeat for multiple apps
--exclude-app-name NAME exclude by displayed application name
```

Run `coupled --help` for the complete interface.

## Current interpretation rules

A `read` candidate is taken after mouse, click, scroll, or focus activity has
settled. Coupled traverses the focused window's Accessibility tree, retains
text-bearing elements whose frames intersect the focused window, uses an
element's visible character range when one is exposed, and emits a new event
only when the resulting viewport text differs from the previous viewport for
that window. `newlyVisibleContent` removes exact line overlap with the
immediately previous viewport; returning after another viewport is retained as
a reread. Each coalesced activity record retains its first and last trigger
times and its originating application and window. If that context changes
before the delay settles, the activity is retained without attributing the new
window's snapshot to it.

A `write` candidate begins with the first mutating key event in a focused
editable Accessibility element. Coupled captures the field before the burst,
waits for the configured idle delay, captures it again, and emits the smallest
contiguous insertion, deletion, or replacement. Command-V, Command-X, and
Command-Z are tagged as paste, cut, and transformation signals. A pointer click
finalizes an active write before focus can move. The raw stream retains the
before/after field values used by the interpretation. The derived write keeps
the bounded semantic initial cursor context separate from its net edit outcome.

Secure text fields are excluded. A hard application deny-list, maximum capture
sizes, and a pause file are available from the first run. Output files are
created with owner-only permissions, but they remain highly sensitive.

## What to inspect first

Run short, controlled sessions before collecting normal work:

1. Type, paste, insert in the middle, delete, undo, and completely clear text.
2. Submit text in Obsidian, a browser field, and each AI-chat application.
3. Scroll slowly, scroll quickly, dwell, reread, switch tabs, and overlap
   windows.
4. Compare `raw.jsonl`, `events.preview.jsonl`, and the experience you remember;
   then inspect the reducer's finalized `events.jsonl` and `unresolved.jsonl`.
5. Record missing text, false exposure, duplicate reads, wrong authorship,
   incorrect write boundaries, and sensitive content that should be excluded.
6. Change one delay, exclusion, traversal rule, or interpretation at a time.

## Known limitations

- Accessibility visibility is a proxy for exposure, not gaze or comprehension.
- Some browser and Electron accessibility trees expose incomplete geometry or
  content beyond the literal viewport.
- Window intersection does not yet model occlusion by another window.
- Text fields that clear or disappear on submission may require a pre-key
  fallback; those events are marked with `terminal_fallback=true`.
- Voice dictation, application automation, model-authored edits, and writes
  performed without keyboard activity are not yet reliably attributed.
- Audio and video are intentionally ignored.
- A single settled burst is represented as one contiguous diff, even when it
  contains multiple semantic actions.

These limitations are visible in the raw records rather than hidden behind a
fixed training schema. The intended loop is sensor → inspect → compare with the
actual experience → revise.

## Checks

```sh
./scripts/check.sh
```

The package also contains XCTest coverage for the pure text-diff and viewport
overlap logic. The standalone check exists because the selected Command Line
Tools installation does not include a compatible XCTest module for its older
SDK.

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
  --read-delay 1 \
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

The `events` command combines screen-text reads with one deliberately narrow
write experiment: settled edits in a focused Obsidian text area.

```sh
./scripts/coupled events \
  --output ./coupled-data/visible-events-test \
  --read-delay 1 \
  --write-delay 3 \
  --pause-file ./.coupled-pause
./scripts/coupled logs
```

Derived events are appended in emission order to `events.jsonl` and mirrored to
the same live log. Source observations are appended separately to `raw.jsonl`:

- `WRITE` is attempted only for Obsidian. An active event tap attempts to
  capture the focused text area before returning the first mutating key. The
  same retained Accessibility element is queried after the write delay, and a
  minimal insertion, deletion, or replacement is emitted only when both states
  are complete and no event-tap timeout occurred.
- `READ` captures the visible rectangle of the topmost window surface after the
  read delay, removes 10% from both the left and right, 10% from the top, and
  50% from the bottom, then uses macOS Vision recognition locally to emit its
  text with provenance `screen_ocr`.

The crop now retains the middle 80% of the selected window's width and the
vertical band from 10% through 50%. It is not a claim about gaze or attention.
Tune it with `--viewport-side-crop` (zero to less than `0.5`),
`--viewport-top-crop`, and `--viewport-bottom-crop`; the top and bottom values
must sum to less than `1`. Setting all three to `0` restores full-window
capture. Each read retains `windowBounds`, the actual cropped `captureBounds`,
and all crop fractions so the transformation remains auditable.

Each raw OCR observation retains the complete recognized viewport before
overlap removal. Normalized line overlap is removed from adjacent OCR viewports
only when app, window, and display all match. The event's `content` contains
newly visible lines in display order; `recognizedLineCount`, `emittedLineCount`, and
`overlapRemovedLineCount` make the transformation inspectable. Exact duplicate
viewports emit no event. An intervening write or different context resets the
comparison, preserving a later reread. Capture is allowlisted by bundle to
Obsidian (`md.obsidian`), Chrome (`com.google.Chrome`), and Codex
(`com.openai.codex`), so the Visual Studio Code diagnostic view and all other
applications are ignored. Deliberately expand the boundary with
`--allow-bundle`; `--exclude-bundle` and `--exclude-app-name` can narrow it.

The default log shows the Obsidian insertion/removal and the first eight
recognized lines of each OCR read, preserving line breaks. Use
`./scripts/coupled logs --full-text` for every recognized line in a readable
form, or `./scripts/coupled logs --raw` for the full JSONL event. Screenshots
are processed in memory and are not saved.

This command requires **Input Monitoring**, **Screen Recording**, and
**Accessibility** for `dist/Coupled.app`. OCR is an observation of visible
pixels, so it naturally includes occlusion and can make recognition errors.
The Obsidian experiment does not poll or cache inactive editors. It retains one
focused Accessibility element and its initial value only while a burst is
active. Chrome and Codex produce reads but no derived write events. One raw
before/after audit record is stored per Obsidian burst—individual key signals
are not stored. Both files can contain sensitive visible or editable text; use
the pause file before handling sensitive information.

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
--read-delay 1          wait for pointer/scroll activity to settle
--write-delay 3         wait for keyboard activity to settle
--poll-interval 0.35    detect focus/window changes
--max-characters 30000  bound captured text per snapshot and editable field
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
before/after field values used by the interpretation.

Secure text fields are excluded. A hard application deny-list, maximum capture
sizes, and a pause file are available from the first run. Output files are
created with owner-only permissions, but they remain highly sensitive.

## What to inspect first

Run short, controlled sessions before collecting normal work:

1. Type, paste, insert in the middle, delete, undo, and completely clear text.
2. Submit text in Obsidian, a browser field, and each AI-chat application.
3. Scroll slowly, scroll quickly, dwell, reread, switch tabs, and overlap
   windows.
4. Compare `raw.jsonl` with `events.jsonl` and the experience you remember.
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

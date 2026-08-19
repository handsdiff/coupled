# Coupled

Coupled is a local macOS data collector for building auditable datasets from
ordinary computer use. It observes two kinds of events:

- **READ** — text visible in a settled application window, recognized locally
  from a screenshot.
- **WRITE** — text inserted into a focused editable field, reconstructed from
  its Accessibility state before and after the edit.

The default collector follows activity across Obsidian, Chrome, Codex, and
Visual Studio Code. It preserves raw evidence first, so event construction can
be inspected and improved without recollecting the original session.

Coupled is pre-1.0 experimental software. Its output is highly sensitive and
should be reviewed before it is used for training.

## Quick start

### Requirements

- macOS 13 or newer
- Xcode Command Line Tools with Swift Package Manager
- `jq` for the dataset audit and easier JSONL inspection

Install the command-line tools if needed:

```sh
xcode-select --install
```

Clone and package Coupled:

```sh
git clone https://github.com/handsdiff/coupled.git
cd coupled
./scripts/package-app.sh
```

The package script creates an ad-hoc-signed collector at `dist/Coupled.app`
and a separate read-only viewer at `dist/Coupled Logs.app`.

### Grant permissions

Run:

```sh
./scripts/coupled doctor --prompt-permissions
```

The collector needs three permissions in **System Settings → Privacy &
Security**:

| Permission | Why Coupled needs it |
| --- | --- |
| Input Monitoring | Observe keyboard, pointer, click, and scroll timing used to establish event boundaries. |
| Screen Recording | Capture settled window pixels for local OCR and retain screenshot evidence. |
| Accessibility | Read the focused editable field before and after a write, including its selection and semantic cursor context. |

If macOS does not add the app automatically, run:

```sh
realpath ./dist/Coupled.app
```

Add that exact application under all three permission categories. Then quit any
running Coupled process and confirm the grants:

```sh
./scripts/coupled doctor
```

Always launch Coupled through `./scripts/coupled`. Starting the executable
inside the app bundle directly can prevent macOS from associating it with the
permissions you granted.

### Start collecting

Use a new output directory for every session:

```sh
./scripts/coupled events \
  --output ./coupled-data/my-first-session \
  --read-delay 1 \
  --write-delay 3 \
  --pause-file ./.coupled-pause
```

Coupled launches in the background. Follow its compact event stream with:

```sh
./scripts/coupled logs
```

Or open the standalone viewer:

```sh
./scripts/coupled viewer
```

The viewer is excluded from semantic collection, but its window can still
cover pixels in a screenshot. Keep it on another display or outside the work
window being captured.

Check or stop the collector at any time:

```sh
./scripts/coupled status
./scripts/coupled stop
```

If you omit `--output`, the wrapper creates a timestamped directory under
`coupled-data/`.

## Pause around sensitive work

Creating the configured pause file stops capture without ending the session:

```sh
touch .coupled-pause
```

Resume by removing it:

```sh
rm .coupled-pause
```

Secure text fields are excluded when macOS identifies them correctly, but do
not rely on that as the only privacy boundary. Pause before entering passwords,
payment details, private keys, health information, or other material you do not
want retained.

Coupled's collector does not upload data. Session files are created with
owner-only permissions, but they may contain:

- screenshots and OCR text;
- complete editable-field values before and after an edit;
- selected text and nearby semantic cursor context;
- clipboard contents and pasteboard metadata;
- application names, window titles, timestamps, and pointer positions.

The event collector does **not** store typed key characters or raw key codes.
WRITE content is derived from observable field-state transitions instead.

## What a session contains

An `events` session has one authoritative raw substrate:

```text
coupled-data/my-first-session/
├── session.json
├── raw.jsonl
├── events.preview.jsonl
└── screenshots/
```

- `session.json` freezes the resolved configuration, schema versions,
  application allowlist, start time, and collector executable hash.
- `raw.jsonl` contains OCR observations, Accessibility write attempts,
  checkpoints, timing, clipboard evidence, suppressions, and unresolved sensor
  outcomes.
- `events.preview.jsonl` is a provisional live interpretation for debugging.
  It is never authoritative training input.
- `screenshots/` contains full target-window-rectangle PNG evidence by default,
  with hashes and dimensions recorded in `raw.jsonl`.

Screenshots are taken from screen coordinates rather than from an isolated
window compositor. If another window covers the target rectangle, its pixels
can appear in the captured image.

## How collection works

### READ capture

Pointer movement, clicks, scrolling, and application activation start a READ
candidate. After the configured quiet period, Coupled revalidates the current
application, window, display, title, and bounds together. A stable candidate is
captured and recognized locally with macOS Vision.

By default, OCR uses the middle 80% of the selected window's width and the
vertical band from 10% through 65%. The original screenshot and the complete
recognized crop are retained before adjacent-view overlap is removed. This is
an attention proxy, not evidence of gaze or comprehension.

### WRITE capture

On the first mutation-capable input, Coupled synchronously captures the focused
editable element before returning that input to the application. It retains the
specific element through the burst and observes it again after the configured
quiet period or an earlier proven boundary.

Raw evidence includes the field value, selection, destination, semantic text
around the cursor, clipboard state, classified input timing, short post-input
checkpoints, Return checkpoints, paste checkpoints, and Accessibility errors.
Ambiguous transitions remain inspectable rather than being reconstructed from
keystroke characters.

Cmd-V is represented separately from authored text when immediate editable
states and the conditioned clipboard version prove the paste. Clipboard changes
remain conditioning evidence; they do not create a third semantic event type.

### Default application scope

Coupled collects from these bundle identifiers by default:

| Application | Bundle identifier |
| --- | --- |
| Google Chrome | `com.google.Chrome` |
| Visual Studio Code | `com.microsoft.VSCode` |
| Codex | `com.openai.codex` |
| Obsidian | `md.obsidian` |

Add another application experimentally with `--allow-bundle`:

```sh
./scripts/coupled events \
  --output ./coupled-data/safari-session \
  --allow-bundle com.apple.Safari \
  --pause-file ./.coupled-pause
```

Support depends on the application's windows and editable fields being exposed
through macOS Accessibility. Narrow the scope with repeated `--exclude-bundle`
or `--exclude-app-name` options.

## Finalize a session

Only reduce a session after the collector has stopped. Copying or reducing a
live `raw.jsonl` can produce a stale artifact that does not match the completed
session.

```sh
./scripts/coupled stop

./scripts/coupled reduce \
  --input ./coupled-data/my-first-session \
  --output ./coupled-data/my-first-session-reduced

./scripts/coupled compile \
  --input ./coupled-data/my-first-session-reduced \
  --source ./coupled-data/my-first-session \
  --output ./coupled-data/my-first-session-dataset

./scripts/audit-causal-dataset.sh \
  ./coupled-data/my-first-session-dataset \
  ./coupled-data/my-first-session-reduced \
  ./coupled-data/my-first-session
```

Each output directory must be new. The reducer consumes only `session.json` and
`raw.jsonl`; it deliberately ignores `events.preview.jsonl`.

The reduced directory contains:

- `events.jsonl` — finalized semantic READ and WRITE events;
- `unresolved.jsonl` — ambiguous attempts and deliberate non-event
  dispositions;
- `reduction.json` — versions, counts, rules, and source/output hashes.

The compiled dataset contains:

- `events.jsonl` — causally timed event history;
- `examples.jsonl` — model inputs and structured authorship targets;
- `target-exclusions.jsonl` — valid history WRITEs that receive no target loss;
- `context-exclusions.jsonl` — events excluded from causal history;
- `rejections.jsonl` — malformed or integrity-invalid events;
- `dataset.json` — the conversion contract and artifact digests.

Every target contains only events that were available before the target WRITE
began. File append order is never used as a substitute for causal time.

The current compiler processes one finalized session at a time. Do not
concatenate independently compiled `examples.jsonl` files. Multi-session corpus
assembly requires an explicit compatibility and coverage-gap manifest and is
not yet part of the public command-line workflow.

## Inspect before training

A passing mechanical audit proves hashes, lineage, reconstruction invariants,
and causal cutoffs. It does not prove that every event matches what the user
experienced.

Before admitting a session to a training corpus, manually inspect samples from:

- finalized READs and their retained screenshots;
- finalized WRITE content and its raw before/checkpoint/after evidence;
- `unresolved.jsonl` dispositions;
- target and context exclusions;
- actual loss-bearing targets in `examples.jsonl`.

Look specifically for missing activity, incorrect application or window
attribution, OCR contamination, write-boundary disagreement, copied text marked
as authored, truncated replacement content, post-action information in context,
and application-generated text that should not receive loss.

## Common options

```text
--read-delay 1
    Quiet period before a READ candidate settles.

--write-delay 3
    Quiet period before an active WRITE settles.

--viewport-side-crop 0.1
--viewport-top-crop 0.1
--viewport-bottom-crop 0.1
    Configure the OCR recognition region.

--cursor-context-characters 512
    Retain semantic text on each side of the initial selection.

--max-characters 30000
    Bound retained OCR and editable-field text.

--no-retain-screenshots
    Disable PNG retention. This saves disk space but weakens later audit and
    OCR reprocessing.

--allow-bundle ID
--exclude-bundle ID
--exclude-app-name NAME
    Expand or narrow application capture.
```

Run `./scripts/coupled help` for the complete command reference.

## Troubleshooting

### Permissions show as missing after a rebuild

Coupled is ad-hoc signed. Rebuilding can invalidate existing macOS privacy
grants. Remove the old Coupled entry from the affected Privacy & Security list,
run `./scripts/package-app.sh`, add the new `dist/Coupled.app`, and restart it.

### Chrome, Obsidian, Codex, or VS Code exposes no editable text

Coupled asks Chromium and Electron applications to construct their renderer
Accessibility trees. Make sure Accessibility permission is enabled and do not
pass `--no-activate-renderer-accessibility` unless you are explicitly testing
behavior without it.

### The collector exits immediately

The launch command prints a diagnostics path under `.coupled-launch/`. Inspect
that file, then run:

```sh
./scripts/coupled doctor
./scripts/coupled status
```

### Disk usage grows quickly

Screenshots are retained for every raw OCR observation by default. Use shorter
sessions, archive completed sessions, or pass `--no-retain-screenshots` only if
you accept the loss of raw visual evidence.

## Sensor diagnostics

Most users should start with `events`. Lower-level commands are available when
debugging a specific sensor:

- `triggers` records individual keyboard, pointer, click, drag, and scroll
  signals without Accessibility or screen-text interpretation.
- `reads` groups pointer activity into timing-only READ candidates.
- `writes` groups Unicode key output into temporal bursts. It cannot identify
  secure fields and must not be used around passwords.
- `snapshot` captures one legacy Accessibility-tree text snapshot.
- `collect` is the older polling-based interpreted collector and is not the
  recommended data path.

Trigger records omit typed characters and raw key codes. The diagnostic
`writes` command is the exception: it intentionally stores typed Unicode text
and therefore has a different privacy profile from `events`.

## Development

Run the repository checks:

```sh
./scripts/check.sh
```

Build without packaging:

```sh
./scripts/build.sh
```

After changing collector, reducer, or compiler code, re-run
`./scripts/package-app.sh` before generating authoritative artifacts. A source
check does not refresh the executable inside `dist/Coupled.app`.

When reporting a bug, include the macOS version, application and bundle
identifier, Coupled commit, relevant session configuration, and raw record IDs.
Do not attach raw JSONL or screenshots without checking them for private data.

Implementation status, validated invariants, and known research boundaries are
tracked in [`checkpoint.md`](checkpoint.md).

## Current limitations

- READ capture measures visible pixels after activity settles; it does not
  establish attention or comprehension.
- Screenshots use a screen-coordinate rectangle, so overlapping windows can
  contaminate the image.
- OCR can be incomplete or incorrect, especially in dense interfaces.
- Rich editors and terminal-like fields can expose ambiguous Accessibility
  trajectories. The reducer is conservative, but every session still requires
  review.
- Dictation, drag-and-drop, context-menu paste, application automation, and
  other writes without a recognized keyboard trigger are not reliably
  attributed.
- Coupled currently collects and constructs data. It does not yet provide a
  live prediction or autocomplete interface.

Coupled's core design is intentionally raw-first: when interpretation changes,
the preserved session can be reduced and compiled again without rewriting the
original evidence.

## License

Coupled is available under the [MIT License](LICENSE).

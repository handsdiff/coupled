# Coupled collection guide

This guide is for a teammate who wants to collect local macOS activity with
Coupled and hand the resulting session back for later reduction and dataset
construction. Coupled records raw evidence locally; it does not upload a
session.

The supported capture surfaces are Google Chrome, Arc, Visual Studio Code,
Codex, and Obsidian. Coupled is experimental, and its output can contain
screenshots, document text, clipboard contents, window titles, and other
private material.
With the default screenshot retention, teammates should budget approximately
**3 GiB of disk space per week** for collected data. Actual usage varies with
the amount of screen activity.

## 1. Install the prerequisites

Coupled requires:

- macOS 13 or newer;
- Xcode Command Line Tools, including Swift Package Manager; and
- `jq`, used by log display and dataset audits.

Install the Apple command-line tools if needed:

```sh
xcode-select --install
```

Clone the repository and enter it:

```sh
git clone https://github.com/handsdiff/coupled.git
cd coupled
```

Build and package both local applications:

```sh
./scripts/package-app.sh
```

This creates:

```text
dist/Coupled.app
dist/Coupled Logs.app
```

`Coupled.app` is the background collector. `Coupled Logs.app` is a separate,
read-only window for watching the live preview. The applications are built
locally and are not stored in Git, so this packaging step is required after a
fresh clone.

Do not move either application out of `dist/`. In particular, the log viewer
uses its location inside the repository to find `.coupled-launch/` and the
current session.

## 2. Grant macOS permissions

Run:

```sh
./scripts/coupled doctor --prompt-permissions
```

In **System Settings → Privacy & Security**, add and enable the exact
`dist/Coupled.app` built above for:

| Permission | Purpose |
| --- | --- |
| Input Monitoring | Detect keyboard, pointer, click, and scroll timing. |
| Screen Recording | Capture settled window pixels for local OCR. |
| Accessibility | Observe editable-field state, selection, and cursor context before and after a write. |

If the application is not added automatically, print its exact path:

```sh
realpath ./dist/Coupled.app
```

Use the `+` button in each privacy panel to add that application. The separate
`Coupled Logs.app` does not need these capture permissions.

After changing permissions, quit any running Coupled collector and verify all
three grants:

```sh
./scripts/coupled stop
./scripts/coupled doctor
```

All three lines should say `granted`. Coupled is ad-hoc signed, so rebuilding
it may make macOS treat it as a new application. If a grant later appears
missing, remove the old privacy entry, rebuild, add the new `dist/Coupled.app`,
and check again.

Always start the collector through `./scripts/coupled`. Do not run the binary
inside `Coupled.app` directly: macOS may then fail to associate the process
with the permissions granted to the bundle.

## 3. Open the independent Coupled Logs window

Open the viewer with:

```sh
./scripts/coupled viewer
```

That command returns immediately. The resulting **Coupled Logs** window is an
independent macOS application, not a terminal tail and not a child window of
Terminal or VS Code. It can be moved, resized, minimized, closed, and reopened
without starting, pausing, or stopping collection.

You can also open it directly from Finder at `dist/Coupled Logs.app`, or run:

```sh
open "./dist/Coupled Logs.app"
```

The viewer may be left open between sessions. It checks for the latest Coupled
launch automatically. During a live session it displays:

- `LIVE COLLECTION` status;
- the path of the launch log being followed;
- the resolved settings from that session's `session.json`; and
- the compact live READ/WRITE preview.

When first attached, it shows only the latest few preview events and then
follows new ones. It is a debugging display, not the authoritative session
record.

The viewer's bundle is excluded from semantic collection. However, READ
screenshots use screen-coordinate rectangles: if the Logs window physically
covers a captured work window, its pixels can still appear in the screenshot.
Keep Coupled Logs on a second display, another macOS Space, or a non-overlapping
part of the desktop.

The terminal alternative is:

```sh
./scripts/coupled logs
```

That command occupies its terminal until `Ctrl-C` is pressed. Pressing
`Ctrl-C` there stops only the terminal log tail; the collector keeps running.
Use `Coupled Logs.app` when the log display needs to remain separate from the
terminal used to start and manage collection.

## 4. Start a collection session

Use a fresh output directory for every session. Replace the example name with
a unique teammate/date label:

```sh
./scripts/coupled events \
  --output ./coupled-data/alice-2026-08-21-am \
  --read-delay 1 \
  --write-delay 3 \
  --viewport-side-crop 0.1 \
  --viewport-top-crop 0.1 \
  --viewport-bottom-crop 0.1 \
  --pause-file ./.coupled-pause
```

The wrapper launches `Coupled.app` through macOS Launch Services and returns
the terminal prompt. The collector then runs in the background; closing that
terminal does not stop it. Use the printed status and stop commands to manage
the background process.

If `--output` is omitted, Coupled creates a timestamped directory under
`coupled-data/`. An existing session directory is never overwritten.

The standard settings mean:

- a READ may settle after one second of quiet activity;
- a WRITE may settle three seconds after its last relevant input;
- OCR removes 10% from each side, 10% from the top, and 10% from the bottom;
- screenshots are retained for later audit; and
- the pause-file path provides a manual privacy boundary.

The default application allowlist is Chrome, Arc, VS Code, Codex, and Obsidian.
Other applications are not captured unless explicitly added with
`--allow-bundle` and should be treated as experimental.

## 5. Verify the session before doing ordinary work

Check that the collector is live:

```sh
./scripts/coupled status
```

With Coupled Logs visible on a non-overlapping display or Space:

1. Open one of the five supported applications.
2. Move or scroll over meaningful visible text, then leave it settled for at
   least one second. A READ preview should appear.
3. Type a distinctive, non-sensitive phrase into a normal editable field and
   stop typing for at least three seconds. A WRITE preview should appear.
4. Confirm that the displayed application, window, and completion are
   plausible.

The live events are provisional. Their purpose is to catch missing permissions
or obviously broken capture early; later reduction reconstructs semantic
events from `raw.jsonl` independently of the preview.

## 6. Pause, resume, and stop

Pause before any sensitive work:

```sh
touch ./.coupled-pause
```

Resume the same session:

```sh
rm ./.coupled-pause
```

Stop and finalize collection:

```sh
./scripts/coupled stop
./scripts/coupled status
```

The final status should say that Coupled is not running. Closing
`Coupled Logs.app`, closing Terminal, or pressing `Ctrl-C` in
`./scripts/coupled logs` does **not** stop the background collector.

Do not reduce, copy, or archive a session while it is still live. The final raw
record may not have settled yet.

## 7. Handle session data safely

A completed session directory contains:

```text
session.json
raw.jsonl
events.preview.jsonl
screenshots/
```

For handoff, preserve and share the entire stopped session directory unchanged,
including screenshots. `raw.jsonl` is the authoritative evidence;
`events.preview.jsonl` is only the live monitor. The receiving researcher can
run the versioned semantic reducer and episode-construction pipeline later.

Do not commit session data to Git. `coupled-data/` is ignored by this
repository, but that is not a privacy guarantee. Review the material and use an
approved private transfer mechanism.

## Troubleshooting

### `Coupled.app is missing`

Run:

```sh
./scripts/package-app.sh
```

### A permission still says `missing`

Confirm that System Settings contains the exact application returned by
`realpath ./dist/Coupled.app`. Toggle the grant off and on, stop Coupled, and
rerun `./scripts/coupled doctor`. If the app was rebuilt, remove the stale
privacy entry and add it again.

### Coupled starts but no events appear

Run:

```sh
./scripts/coupled status
./scripts/coupled doctor
```

The start command also prints a diagnostics file under `.coupled-launch/`.
Inspect that file for launch errors. Verify activity in a default-allowlisted
application and wait for the configured delay.

### The Logs window says `NO LIVE COLLECTION`

The viewer is independent of collection. Start a session with
`./scripts/coupled events ...`; the open viewer should switch automatically.
If it does not, quit and reopen `dist/Coupled Logs.app` without moving it out of
the repository.

### The output directory already exists

Choose a new directory name. Coupled intentionally refuses to append to or
overwrite a previous session.

### Updating to a newer Coupled revision

Stop the collector before updating, then pull and rebuild:

```sh
./scripts/coupled stop
git pull --ff-only
./scripts/package-app.sh
./scripts/coupled doctor
```

Rebuilding may require granting privacy permissions again.

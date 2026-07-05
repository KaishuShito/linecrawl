# linecrawl Phase 1

`linecrawl` is a local CLI for LINE Desktop "Save chat" text exports.

It does not read LINE's encrypted `.edb` database yet. Phase 1 imports the text
files created by LINE's `Save chat` menu, stores them in SQLite, and provides
discrawl-like search and message commands.

## Disclaimer

This is an independent, unofficial personal project. It is **not affiliated
with, endorsed by, or connected to** LINE Corporation, LY Corporation, NAVER,
or any of their subsidiaries. "LINE" and related marks belong to their
respective owners and are used here only to describe interoperability.

- **Your data, your responsibility.** `linecrawl` operates only on chat data
  you already own and can already read on your own machine. You are responsible
  for handling that data — and any other person's messages contained in it — in
  accordance with applicable privacy laws and LINE's Terms of Service.
- **Local only.** The tool reads local LINE exports / the DOM of your own
  logged-in LINE session and writes to a local SQLite database. It does not send
  your data to any remote server. The only network call it makes is to a local
  Chrome DevTools endpoint (`http://127.0.0.1:9222`) that you opt into.
- **Automation and reverse-engineering caveats.** The Web route automates the
  LINE Chrome extension's DOM, and the Phase 2 notes describe probing LINE's
  local encrypted storage. These are best-effort, may break at any time, and may
  be contrary to a service's Terms of Service in some contexts. Use them only
  where you are permitted to, and at your own risk.
- **No warranty.** This software is provided "as is", without warranty of any
  kind. See [LICENSE](LICENSE). The authors are not liable for any data loss,
  account action, or other damage arising from its use.

## Usage

```bash
linecrawl import-downloads
linecrawl desktop-save-current --import
linecrawl desktop-save-current --menu-already-open --pre-click-delay 5 --import
linecrawl web-import-current --scroll-steps 5
linecrawl web-watch-current --interval 30 --scroll-steps 1
linecrawl launchd-install-web --interval 30 --scroll-steps 1
linecrawl web-import-current --method cdp --chrome-debug-url http://127.0.0.1:9222
linecrawl web-dump-current --with-media --output ~/Downloads/line-web-dump.json
linecrawl web-import-json ~/Downloads/line-web-dump.json
linecrawl web-doctor
linecrawl --json doctor
linecrawl stats
linecrawl chats
linecrawl search "相談"
linecrawl messages --chat "%Podcast%" --limit 20
linecrawl media --chat "%Podcast%" --limit 10
linecrawl sql "select count(*) as messages from messages;" --json
linecrawl watch
linecrawl launchd-install --interval 10 --verbose
linecrawl launchd-status
linecrawl edb-doctor
linecrawl edb-import --dry-run
```

By default the database is:

```text
~/.linecrawl/linecrawl.db
```

You can use another DB for testing:

```bash
linecrawl --db ./linecrawl.test.db import-downloads
```

## Install

The source project lives at:

```text
~/src/linecrawl
```

Install the command on PATH:

```bash
cd ~/src/linecrawl
make install-local
```

This creates:

```text
~/.local/bin/linecrawl -> ~/src/linecrawl/linecrawl.py
```

No auth or network access is required. `linecrawl` only reads local LINE
exports and writes the local SQLite database.

## JSON Policy

Use global `--json` before the subcommand:

```bash
linecrawl --json doctor
linecrawl --json import ~/Downloads/'[LINE]Example.txt'
linecrawl --json search "相談"
```

Successful JSON commands return an object with `ok: true` plus command-specific
keys such as `results`, `messages`, `chats`, or `stats`.

Error JSON returns:

```json
{
  "ok": false,
  "error": {
    "code": "no_files_matched",
    "message": "No files matched."
  }
}
```

## Imported Format

The parser expects LINE Desktop text exports like:

```text
2026.01.06 Tuesday
17:57 Sender Photos
17:58 Sender Multi-line message
continues here
```

Message IDs are stable hashes of chat, timestamp, sender, content, and source
line. Re-running `import-downloads` skips unchanged files.

## Commands

- `import <paths...>`: import one or more LINE text exports.
- `import-downloads`: import `~/Downloads/[LINE]*.txt`.
- `desktop-save-current`: use the LINE Desktop UI to run Save chat for the currently open chat, then optionally import the newly saved export.
- `web-dump-current`: use the logged-in LINE Web tab in Google Chrome to dump currently visible chat DOM messages as JSON.
- `web-import-current`: dump the open LINE Web Chrome tab and import the normalized messages into the same SQLite tables.
- `web-import-json`: import a saved `web-dump-current` JSON payload. This is useful for debugging extraction without touching Chrome again.
- `web-watch-current`: poll the open LINE Web tab and import changed DOM dumps.
- `web-doctor`: check LINE Web tab discovery, CDP availability, AppleScript JavaScript permission, and extension local storage paths.
- `media`: list captured media files with absolute local paths.
- `watch`: poll Downloads and import new or changed exports.
- `launchd-install`: install `watch` as a macOS LaunchAgent.
- `launchd-install-web`: install `web-watch-current` (with image capture) as a macOS LaunchAgent.
- `launchd-status`: show the LaunchAgent status and log paths.
- `launchd-uninstall`: remove the LaunchAgent.
- `edb-doctor`: inspect LINE Desktop `.edb` files without decrypting them.
- `edb-import`: snapshot `.edb` files and import them only when they are readable SQLite files with a supported message-shaped schema.
- `doctor`: show database status.
- `stats`: show aggregate database statistics.
- `chats`: list chats, counts, and date spans.
- `search <query>`: full-text search, falling back to `LIKE` for Japanese text.
- `messages`: print messages, optionally filtered by chat and days.
- `sql <query>`: run a read-only SQLite query against the database (the
  connection is opened read-only, so this command cannot mutate your data).

## LaunchAgent

The default LaunchAgent label is:

```text
com.linecrawl.watch
```

It watches:

```text
~/Downloads/[LINE]*.txt
```

Logs are written to:

```text
~/.linecrawl/logs/watch.out.log
~/.linecrawl/logs/watch.err.log
```

To remove the watcher:

```bash
linecrawl launchd-uninstall
```

## LINE Web Import

`web-import-current` is the Chrome-session route for chats that are visible in
the LINE Chrome extension:

```bash
linecrawl web-import-current --scroll-steps 5
```

It intentionally borrows the user's already logged-in Chrome session instead of
extracting LINE credentials. By default it first tries Chrome DevTools Protocol
at `http://127.0.0.1:9222`, then falls back to AppleScript.

The Web route is passive: it does not activate Chrome, move windows, switch
tabs, click, type, or scroll the user's visible UI through macOS automation.
Commands that perform visible UI automation are limited to the explicit
`desktop-save-current` Save Chat route.

For the CDP path, launch Chrome with remote debugging enabled:

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222
```

Note: Chrome 136+ refuses `--remote-debugging-port` on the default user profile
(the port answers but every DevTools endpoint returns 404). If you see that,
either enable the AppleScript route (`View > Developer > Allow JavaScript from
Apple Events`) or run a dedicated debugging profile with `--user-data-dir` that
is logged into the LINE extension.

For the AppleScript fallback, Chrome must allow AppleScript JavaScript execution:

```text
Chrome menu > View > Developer > Allow JavaScript from Apple Events
```

The importer reads the open `chrome-extension://ophjlpahpchlmihnnnihgmmeilfjmjjc`
LINE tab, normalizes visible bubbles into the same message shape used by Save
Chat and EDB imports, and updates the same FTS index. Re-runs are idempotent
when the DOM dump has not changed. Changed dumps for the same chat URL are kept
as separate sources so older visible messages are not deleted when a later dump
contains a different viewport.

For lightweight automatic capture while a LINE Web chat is open:

```bash
linecrawl web-watch-current --interval 30 --scroll-steps 1
```

To keep it running across logins as a LaunchAgent
(label `com.linecrawl.webwatch`, logs in `~/.linecrawl/logs/webwatch.*.log`):

```bash
linecrawl launchd-install-web --interval 30 --scroll-steps 1
linecrawl launchd-status --label com.linecrawl.webwatch
linecrawl launchd-uninstall --label com.linecrawl.webwatch
```

For inspection or test fixtures:

```bash
linecrawl web-doctor
linecrawl web-dump-current --scroll-steps 5 --output ~/Downloads/line-web-dump.json
linecrawl web-import-json ~/Downloads/line-web-dump.json
```

## Image Capture (Web route)

The web commands (`web-import-current`, `web-watch-current`, and
`launchd-install-web`) capture message images by default; pass `--no-media` to
disable. `web-dump-current` needs an explicit `--with-media`.

How it works, still fully passive (no tab switching, clicking, or visible
scrolling):

1. The DOM dump collects visible `img` elements in the message pane (48px+ so
   avatars are skipped) alongside text bubbles.
2. Image bytes are read in-page: on CDP via `fetch(blobUrl)` +
   `FileReader` (with a canvas fallback), on AppleScript via a synchronous
   canvas re-encode. Results are cached in the page per blob URL, so watch
   polls do not refetch unchanged images.
3. Decoded images are stored under `~/.linecrawl/media/<chat>/<sha>.<ext>`,
   deduplicated by content SHA-256. Blob URLs change every Chrome session, but
   the same image bytes always map to the same file and the same `[Photo]`
   message id, so re-dumps never duplicate photos.
4. The `media` table links each file to its chat and message. `messages --json`
   returns a `media` array with absolute local paths, and `media` lists recent
   captures:

```bash
linecrawl --json messages --chat "%現場%" --limit 10
linecrawl --json media --chat "%現場%" --limit 10
```

By default captured images are the thumbnails LINE renders in the DOM.

`--json doctor` reports the pipeline health: `media` (count), `media_full`,
`media_dir`, `media_latest_captured`, `media_files_missing`,
`web_watch_running`, and LaunchAgent load state for both watchers.

### Full-resolution capture (`--full-media`)

`web-import-current`, `web-watch-current`, `web-dump-current`, and
`launchd-install-web` accept an opt-in `--full-media` flag:

```bash
linecrawl --json web-import-current --full-media
```

For each visible image it clicks the thumbnail **inside the page** (JavaScript
`element.click()`, CDP only), waits for the photo viewer's full-resolution
image to load, captures its bytes, then closes the viewer with synthesized
Escape/close-button events — one image at a time.

This is an explicit exception to the passive policy, like
`desktop-save-current`: it never moves the OS mouse, types, switches tabs, or
focuses Chrome, but the photo viewer briefly opens and closes inside the LINE
tab. If that tab is in the background nothing visible changes on screen; if
you are looking at the tab you will see the viewer flash. If a viewer fails to
close, remaining captures are skipped and the error is reported as
`full_media_error`.

Quality upgrades happen in place: the `[Photo]` message and `media` row are
keyed by the thumbnail's content hash, so re-importing with `--full-media`
replaces the stored thumbnail file with the full-resolution bytes
(`media.quality` becomes `full`) without duplicating messages or files. A
later thumbnail-only import never downgrades a stored full-resolution capture.

The AppleScript route cannot run this flow (no async viewer polling); it
reports `full_media_error` and keeps thumbnails.

This route is DOM-based and should be treated as best-effort when LINE changes
its extension markup. The Save Chat route remains the conservative fallback.

The Codex Chrome plugin can discover the user's LINE extension tab, but direct
automation of `chrome-extension://ophjlpahpchlmihnnnihgmmeilfjmjjc/...` pages may
be blocked by the browser automation URL policy. In that case use `web-doctor`
and prefer the CDP or AppleScript routes above rather than trying to bypass the
policy.

## Phase 2 EDB Probe

The Phase 2 investigation notes are in:

```text
PHASE2_EDB_REPORT.md
```

Current finding: LINE's `.edb` files are encrypted or wrapped, not plain SQLite.
The likely chat DB is `qweXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX.edb`, but direct import
needs the LINE StorageService key path and page format.

`edb-import` implements the safe bridge surface for Phase 2. It copies the
target `.edb` family (`.edb`, `-wal`, and `-shm`) into
`~/.linecrawl/edb_snapshots/`, opens the snapshot read-only, and refuses to
write to the linecrawl database unless it finds a table with message content and
timestamp columns. On the current encrypted LINE files it should report
`unsupported-encrypted-or-wrapped`; that is expected until the StorageService
format is decoded.

If Phase 2 succeeds, the goal is not to replace the current text-export path.
The goal is to add a future import bridge:

```text
LINE local .edb data
  -> safe read/extract layer
  -> normalize into the same message shape as Save chat exports
  -> import into ~/.linecrawl/linecrawl.db
  -> use the same chats/search/messages/sql commands
```

In other words, the CLI should keep one user-facing interface while supporting
two input routes: the already-working `Save chat` text route, and a carefully
validated direct local database route if it becomes feasible.

## Test

Run the fixture-backed suite:

```bash
cd ~/src/linecrawl
make test
```

Smoke test the installed command from another directory:

```bash
cd /tmp
linecrawl --help
linecrawl --json doctor
```

## License

MIT — see [LICENSE](LICENSE).

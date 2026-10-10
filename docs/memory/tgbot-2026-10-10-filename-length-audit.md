# 2026-10-10 — 15-day log audit: on-disk filename length overflow + system status

Session scope: re-read the code, scan the retained bot logs for the last ~15 days
(Sep 25 – Oct 10; on-disk rotation `bot.log{,.1,.2,.3}` reaches back to
**2026-09-23**), check every monitoring/subsystem, and fix the errors the
operator noticed. The operator specifically reported an **X/Twitter
direct-forwarder error "file name being too long or invalid"**. One live defect
was found and fixed at its root, plus a second latent instance of the same class
in the direct-file path. Everything the previous session (2026-10-08) fixed was
confirmed still-in-effect.

## The reported bug — `OSError [Errno 36] File name too long` (X relay)

The operator saw one tweet **permanently skipped** on 2026-10-09 12:39–12:49 UTC:

```
[DirectForward/X] bridge message 2108536104957923328 failed (attempt 1..3):
  [Errno 36] File name too long:
  'cache/x_0bd05999/GAYROCK2026 🌋🌈🏳️‍🌈(6️⃣0️⃣K) 👬😀🎵🎶 - 🌈🌈🌈… 🌋🌋....jpg'
[DirectForward/X] bridge message 2108536104957923328 failed 3x — skipping to unblock the queue (cursor advanced).
```

### Root cause

`utils/downloader/download.py::download_media` built the yt-dlp output template as

```python
out_tmpl = f"{task_dir}/%(title)s.%(ext)s"
```

yt-dlp **sanitizes illegal characters but does not bound the length**. A tweet
whose title is a wall of emoji is fine per-character but each emoji is **4 UTF-8
bytes**, so the title overflowed Linux's **255-byte** per-component `NAME_MAX`.
yt-dlp died writing the thumbnail (`_write_thumbnails` → `open(thumb_filename)`)
with `OSError [Errno 36]`, the download pipeline classified it and retried, the
relay's 3-strike cap skipped the message, and the cursor advanced. It is a *drop*,
not a transient.

Note this is distinct from the 2026-10-08 `.NA` non-ASCII bug (which was about a
missing/unknown extension); here the *title length* itself is the problem.

### Fix — byte-bounded output template (`utils/downloader/download.py`)

```python
_MAX_TITLE_FILENAME_BYTES = 150
out_tmpl = f"{task_dir}/%(title).{_MAX_TITLE_FILENAME_BYTES}B.%(ext)s"
```

yt-dlp's `%(field).<N>B` conversion is **byte**-based: it encodes the value,
slices with `%.<N>s`, then decodes ignoring a partial trailing code point. So the
title contribution is guaranteed ≤ 150 bytes regardless of script, and the final
component (title + ext + yt-dlp's `.fNNN`/`.part` intermediates + our
`<stem>_thumb.jpg` sibling) stays far below 255. The bound applies to **every**
template type because the plain `outtmpl` string is used for `default`,
`thumbnail`, etc. `prepare_filename` (used after the download to locate the file)
uses the same template, so the reported path and the on-disk path still match.

This is the single central download path (`download_media`), so it fixes the X
relay, the generic interactive downloader, and playlists at once.

### Second (latent) instance — `utils/security.py::safe_task_filename`

The direct-file path (`download_direct_file`) and the interactive display-name
paths sanitize via `safe_task_filename`, which capped *neither* length. A long
URL path basename could therefore still hit `Errno 36` when written to disk.
It now bounds the sanitized name to **150 chars** (the sanitizer maps every
non-`[A-Za-z0-9._-]` byte to `_`, so the result is pure ASCII → char cap == byte
cap), preserving a short trailing extension (≤ 10 chars).

### Verification

- Pathological title (`~60` emoji `+` dots): old template `prepare_filename`
  returned **328 bytes** (> 255, the bug); new template returns **151 bytes** for
  both the `default` and `thumbnail` template types.
- Full pipeline smoke: `download_media` on a real SoundCloud track completed and
  produced a normal file (`Flickermood.m4a`) — no regression from the template
  change. (YouTube smoke needs the PO provider runtime flag; verified the
  provider process is up instead.)
- `python3 -m py_compile` on both changed modules; `cd cmd/tgbot-monitor && go
  test ./...` green.

## Full 15-day error inventory (Sep 23 – Oct 10)

| Source | Count | Verdict |
|---|---|---|
| `modules.direct_forward.instagram` ERROR | 55 | **expected** — dead IG session (Oct 1–8); the 2026-10-08 fix stopped the auto-relogin storm; IG is now paused (`IG_AUTH_ENABLED=false`) |
| `aiogram.dispatcher` TelegramNetworkError / ServerError | 33 | transient (Bale frontend / network), self-healing |
| `utils.queue_manager` task failures | 7 | 3 = the filename-too-long bug above (**fixed**); 3 = the 2026-10-08 `.NA` bug (already fixed); 1 = YouTube HTTP 403 transient |
| `modules.direct_forward.twitter` ERROR | 3 | the filename-too-long bug above (**fixed**) |
| `modules.direct_forward.tiktok` ERROR | 3 | WS ping-timeout reconnects, self-healing |
| `utils.cookie_refresher` WARNING | 3 | device-identity denylist doing its job (expected) |

Live confirmation: the **current** `bot.log` (post-rotation) has **0 ERROR**
lines; the only warnings are pyrogram upload throttle notices and the expected
cookie-refresher device-identity warnings.

## Monitoring / subsystem status — all healthy

- `tgbot.service` — active (restarted 19:35 UTC to apply the fix; clean start).
- `tgbot-xchat-bridge.service` — active; the Deno sidecar recovers from
  transient `cycletls` dead connections internally and its inbox cursor matches
  the worker's (`cache/xchat_inbox.jsonl` last seq == `state.x` cursor).
- `tgbot-monitor` — Go binary spawned by the bot, pidfile/liveness OK
  (`is_running()` True). `tgbot-monitor.service` is installed-but-disabled by
  design. Its recent `>=80%` warnings are the OpenCode agent session itself on a
  2-vCPU box, not the bot.
- `cookie-watch.service` — active; `cookie_watch.log` shows only atomic
  temp+rename writes from `python main.py` (no external tamper).
- `fail2ban`, `nginx` — active.
- Cookie jars fresh (yt/tt refreshed Oct 10 06:xx, xcookies write-back Oct 10
  09:39); `freshness_warnings()` empty. IG jar Oct 8 and intentionally idle
  (paused).
- Direct-forward: X polling + TikTok WS connected; friend-media ran and detected
  no new material (IG work correctly skipped while paused).
- Updater: yt-dlp nightly check running; no pending update.

## Operator follow-ups

- Nothing required for the filename fix; it applies to all future downloads.
- If a direct-forward message is ever dropped again, the append-only inbox plus
  `tools/recover_x_gap.py` can replay it (see the 2026-10-08 note).
- IG remains paused pending a fresh `igcookies.txt` + Admin → 🔐 IG Auth re-enable.

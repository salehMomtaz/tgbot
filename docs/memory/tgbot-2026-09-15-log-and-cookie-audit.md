# 2026-09-15 — 10-day log audit + Instagram cookie-jar corruption analysis

Session scope: read the code, scan the retained bot logs for the last ~10 days
(Sep 7-15; the on-disk rotation only reaches back to Sep 7, see caveat below),
diagnose the recent log-channel errors, and trace the Instagram cookie jar's
corruption to its cause. Two root causes were found and fixed; one one-time
data repair was applied.

## 1. The log-channel error burst was a thread-exhaustion cascade (OUR bug)

`logs/bot.log` for 2026-09-15 shows, in the 02:00 hour, 38×
`asyncio | Task exception was never retrieved ... RuntimeError("can't start new
thread")` plus 165 `upload.SaveFilePart ... after 10 retries` from 02:00 to
14:22. The SaveFilePart timeouts are environmental — the VPS's route to
`api.telegram.org` degraded (Iran throttling); the same TCI-network signatures
appear in `cookies/instagram/igcookies.txt` (foreign `internet.tci.ir` cookies).

The `can't start new thread` cascade was **ours**: both log-channel handlers
(`utils/logger/telegram.py`, `utils/logger/bale.py`) did
`threading.Thread(target=post, daemon=True).start()` **per log record**. Under
throttling each POST blocks for its 5 s timeout (+5 s plain-text fallback),
while the friend-media IG archive was emitting thousands of `private_request`
lines — so thread count climbed past the process limit (`run.sh
ulimit -u 512`, `tgbot.service LimitNPROC=512`). Once at the cap, every
`run_in_executor` (including pyrogram's session restart) raised, and the bot's
network sessions crash-looped. It recovered only after the log volume dropped.

### Fix
`utils/logger/queued_channel.py` — `QueuedChannelHandler`: a bounded
`queue.Queue` (main 1000, bale 500) drained by a **fixed** pool of daemon
workers (main 2, bale 1). `emit()` formats and `put_nowait`s (O(1), never
spawns a thread after warmup); on a full queue records are **dropped** (counted,
occasional stderr notice) rather than piling up. Threads are now constant
regardless of volume or network state. `TelegramChannelHandler` /
`BaleChannelHandler` are thin subclasses, so the strict Telegram/bale_log split
and the public names are unchanged. Verified: 2000 rapid records → 3 threads
total (base + 2), 992 dropped under an artificially saturated queue.

## 2. The Instagram cookie jar corruption (OUR refresher + overlay bugs)

### What the jar looked like
`cookies/instagram/igcookies.txt` was 30 lines = **two complete `.instagram.com`
blocks** with diverging values, plus foreign `google.com` and `internet.tci.ir`
cookies. `cookies/instagram/igcookies.txt.autobak` (Aug 13) already had the
doubled block, so the operator's export format emits the block twice and the
bot's write-back had been preserving (and half-updating) it ever since.

### Two independent defects, both provably ours

1. **Overlay updated only the LAST duplicate.** `_parse_cookie_lines` kept
   every duplicate line, and `overlay_cookies` built `latest_idx` keyed by
   `(domain, name)` holding only the **last** index — so the first copy of each
   cookie was frozen at upload time while the last copy rotated. Observed
   directly: `csrftoken` first half `VJgc…` (stale), last half `Yjfk…` (live).
   Reproduced in isolation: `overlay_cookies(... 'csrftoken':'NEW')` changed one
   line and left the other stale. An HTTP jar cannot even hold two cookies for
   the same `(domain,path,name)` — Python's cookiejar keeps the last — so the
   duplicates were pure corruption.

2. **The headless refresher injected foreign-domain cookies.** It built its
   overlay `updates` from **every** cookie in the browser context
   (`for c in new_cookies`, no domain filter) and `overlay_cookies` *appends*
   unknown `(domain,name)` pairs. That is where `.google.com __Secure-ENID` and
   `internet.tci.ir PHPSESSID`/`cookiesession1` (ISP-injected) came from.
   Compounding it, the refresher's strongest logged-in check called
   `page.content()` **after** `await context.close()` → always raised → the
   "anonymous login form" tell silently never fired, leaving only the
   (documented-as-insufficient) sessionid+URL check. That is the exact class of
   bug the 2026-09-04 gate was added to prevent, regressed by ordering.

### NOT the cause
Instagram did **not** corrupt the jar as a *sharable identity* (the `sessionid`
fingerprint is byte-identical before/after the repair), and `login_by_sessionid`
only ever consumes the `sessionid` **string** — so the jar cleanup cannot affect
login. The session death itself is Instagram-side, the same class as
2026-09-05: the 12:34 `ig_session_dead` (`FriendMedia/IG`, "archives paused:
login_required") came from a single `403` on `POST feed/reels_media/`
(`highlight_info_v1`) mid-archive, with the SAME client still getting `200`s on
`users/…/info`, `feed/user/…/story` and `feed/user/…` immediately after (so that
particular trip was a transient/rate 403 on a live session). But by the
post-restart login at 14:26 the `sessionid` itself was rejected outright
("Exceeded 30 redirects.", this codebase's dead-sessionid signature) and both
consumers (DirectForward/IG, FriendMedia/IG) reported it. Conclusion: the
structural corruption was **ours** and is fixed; the **sessionid death is
Instagram's** and can only be recovered by the operator uploading a fresh
`igcookies.txt` (Admin → 🍪 Cookie Jars → Instagram → ✏️ Replace).

### Fixes
- `cookie_manager._parse_cookie_lines` now **deduplicates by the full
  `(domain, path, name)` triple**, keeping the last (freshest) line at the
  first position; distinct variants (different path/domain) survive. This is
  correct jar semantics and makes divergence impossible.
- `overlay_cookies` rewrites **every** matching line (per path) and its index
  ignores a leading dot, so a fresh value can never sit beside a stale one.
- `_merge_snapshot_into` now **appends** cookies the site newly issued during a
  yt-dlp run (previously counted as "changed" but silently dropped).
- `cookie_refresher._SITES` gained a **per-site domain allowlist**
  (`instagram.com`; `x.com,twitter.com`; `tiktok.com`; `youtube.com,google.com`);
  only allowlisted domains are compared/written back.
- The refresher's `page.content()` / final-URL capture moved **before**
  `context.close()`, so the anonymous-login-form gate works again.
- `cookie_manager.domain_matches` + `normalize_jar` (the one operation allowed
  to *remove* lines) back a repair tool.

### One-time repair applied
`venv/bin/python tools/normalize_cookie_jar.py ig` → the IG jar went 30 → 11
cookies: duplicates collapsed to the freshest values, foreign domains dropped,
`sessionid` intact, mode `0o444` preserved. Backup at
`/tmp/opencode/igcookies.pre-normalize.*.txt` (outside the repo). Other jars
were inspected (tiktok 38→34, youtube 58→57, x 14) but left alone — their
duplicates are identical-value and the refresher no longer re-adds foreign
cookies, so they self-heal on the next write.

## 3. A separate functional bug (unhandled `MessageNotModified`)

`modules/downloader_handler.py::dl_callback_handler` did
`await callback_query.message.edit_text("⏳ Request enqueued…")` unguarded. A
double-tap re-sends identical text → `MessageNotModified` escaped the handler,
so the job was **never enqueued** (seen 2026-09-08 17:15). The cosmetic edit and
the `answer()` are now wrapped so a stale/double callback can't abort the
enqueue.

## 4. Observation (not changed): "Abort Operations" disables the DM relays

At 03:44 the operator pressed **💥 Abort Operations**; it calls
`signal_all_stop()` (`utils/shared.py`), and the direct-forward loops treat the
flag as a permanent exit (`return`), the flag only being cleared by an
admin-restart/bot startup. IG exited 03:45, X 03:49 (TikTok lingered until its
WS closed, 07:48) — so the relays were down for hours. This is the documented
design ("stop flag set for all workers", reset on restart), so it was left
as-is; the bot restart at the end of this session restores them. **Operators:
after Abort Operations, restart the bot (Admin → 🔄 Restart) if you want the
DM relays to resume.**

## Caveat on log coverage
`logs/bot.log` rotates at 5 MB × 3, so on 2026-09-15 the retained window starts
**2026-09-07 06:44**, not a full 10 days. Older lines exist only in the
Telegram log channel. Everything in the retained window was scanned: the
non-thread errors are all environmental (aiogram/tapi.bale.ai unreachable, X
`Send failed`, transient IG DNS/500, tiktok WS close) except the two fixed
above and the queue `FilePartMissing` retries caused by the same Telegram
throttling.

## Verification
- `python -m py_compile` clean repo-wide (incl. the two new files);
  `bash -n install.sh run.sh uninstall.sh` clean; `go test ./...` in
  `cmd/tgbot-monitor` ok.
- Cookie unit checks (temp files): dedupe on parse, overlay updates the single
  line + appends, snapshot merge appends new keys, HttpOnly round-trip,
  `normalize_jar` prune — all pass.
- Logger unit check: thread count bounded to base+WORKERS under 2000 rapid
  emits with a saturated queue.
- Live: bot restarted; IG jar reads back as 11 clean `.instagram.com` cookies;
  the headless refresher then ran under the new code and wrote only the 11
  allowlisted Instagram cookies (13 context cookies seen, foreign ones skipped),
  confirming the allowlist. DM relays resumed (X polling, TikTok WS connected,
  IG retrying) — **IG is down only because its `sessionid` is dead and needs a
  fresh operator upload; that is not code-fixable.**

## 5. Follow-up: why downtime items were missing + hardening shipped

After the operator uploaded a fresh `igcookies.txt` (15:40) and still reported
"nothing from downtime on IG/X/TikTok", the relay paths were traced:

- **IG was working again** — the fresh jar logged in at 15:45 and the gap fetch
  replayed 22 pending items (paced relays from 15:49). The earlier silence was
  purely the dead `sessionid`.
- **X lost its backlog to the Abort cache wipe.** At 03:44:42 "Abort
  Operations" ran `shutil.rmtree("cache")`, which deletes
  `cache/xchat_bridge_state.json` **even though the hourly cache cleaner
  protects it**. The Deno bridge restarted at 03:45:26, saw no state file, and
  logged `first run — cursor primed to <newest>, backlog skipped` — so
  everything before that point was silently dropped. (The X cursor lives in
  the XChat `sequenceId` space, shared with the worker's `last_id`; a wiped
  bridge cursor cannot be reconstructed.)
- **TikTok is push-only.** `_tt_run_ws` connects, sends the cmd-1001
  `get_stranger_conversation_list` frame and then only relays live cmd-500
  pushes; there is no history fetch, so a long disconnect has no backfill.
  Not a regression — a protocol limitation (a future enhancement could request
  the conversation page explicitly).
- **IG now also alerts on the startup login-failure path** (it previously only
  logged; the 2-consecutive-failure DM only covered the mid-poll re-login path).

### Hardening shipped this follow-up
1. `Abort Operations` is a **transient pause**: `signal_all_stop()` +
   `reset_stop_flag()` after `ABORT_AUTO_CLEAR_SECONDS` (30 s); the
   direct-forward / friend-media / cookie-refresher loops use
   `wait_if_stopped()` (pause) instead of `return`. An abort can no longer
   disable the relays until a restart.
2. The abort cache purge now preserves `PROTECTED_CACHE_FILES` (shared with the
   hourly cleaner) — no more XChat-cursor deletion.
3. **Relay watchdog** (`modules/direct_forward/supervisor.py`): workers stamp
   `mark_worker_alive(platform)`; the supervisor DMs the operator when an
   enabled platform is silent > 30 min (15 min boot grace), once per streak.
4. The Telegram log channel drops the per-file `Retrying "upload.SaveFilePart"`
   WARNING spam (keeps the terminal ERROR + the lossless file mirror).
5. **X is at-least-once**: the cursor is not advanced past a failed relay, with
   a 3-strike cap so one poison message can't block the queue forever.

Verified: `wait_if_stopped` resumes ~2 s after the flag clears; heartbeats
update; the protected-file purge keeps `xchat_bridge_state.json` and removes
job dirs; `py_compile` + `bash -n` + `go test` all clean.

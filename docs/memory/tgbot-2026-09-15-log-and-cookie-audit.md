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
Instagram did **not** corrupt the jar, and the session was **not** actually
dead. The 12:34 `ig_session_dead` (`FriendMedia/IG`, "archives paused:
login_required") came from a single `403` on `POST feed/reels_media/`
(`highlight_info_v1`) mid-archive; the SAME client kept getting `200`s on
`users/…/info`, `feed/user/…/story` and `feed/user/…` immediately after. So
that was instagrapi's `LoginRequired` classification of a transient/rate 403 —
the conservative friend-media breaker tripped for 60 min on a live session.

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
  DM relays (IG/X/TikTok) resume after the restart.

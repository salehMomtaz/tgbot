# 2026-10-08 — 15-day log audit: IG cookie corruption, no-auto-relogin, X bridge fix

Session scope: read the code, scan the retained bot logs for the last ~15 days
(Sep 23 – Oct 8; on-disk rotation `bot.log{,.1,.2,.3}` reaches back to Sep 23),
diagnose every error the operator saw in the log channel, trace the recurring
Instagram session deaths to their cause, make the IG worker stop attempting
automated logins, restore the X/Twitter direct-forwarder, and confirm the
monitoring stack. Four defects were found and fixed; one root cause was proven
from the cookie snapshots.

## Error inventory (15 days)

| Source | Count | Verdict |
|---|---|---|
| `modules.direct_forward.instagram` ERROR/WARNING | 51 / 71 | **OUR bug** — re-login retry storm (fixed) |
| `modules.direct_forward.twitter` WARNING | 57 | mostly expected (Suspended / No-video / BounceDeleted); one real bug (fixed) |
| `modules.direct_forward.supervisor` | 47 | relay watchdog alerting because IG was stuck (consequence) |
| `modules.friend_media.admin` | 53 | IG archive circuit-breaker "session dead" (consequence) |
| `aiogram.dispatcher` TelegramNetworkError | 33+33 | transient network (Telegram timeout), self-healing |
| `pyrogram.session.session` | 534 | normal session/keepalive chatter |
| `utils.queue_manager` | 5 | X photo relay crash on non-ASCII filename (fixed) |
| `modules.direct_forward.tiktok` | 4 | transient WS ping timeouts (self-healing) |

## 1. Instagram cookie corruption — OUR headless refresher (root cause proven)

The repeated `ig_session_dead` / "Exceeded 30 redirects" events (25 on Oct 1,
32 on Oct 2, …) were **not** Instagram expiring the session on its own. The
cookie history snapshots prove our own `utils/cookie_refresher.py` corrupted it.

Comparing `cookies/history_snapshots/igcookies.txt.*` across time:

```
2026-09-30 04:17  mid=artpHQAEAAGmQJ  ig_did=1225AE3E  sessionid=<constant>
2026-09-30 08:11  mid=arzEGgAEAAGHue  ig_did=A788425F  sessionid=<constant>   <-- mid rotated
2026-09-30 10:38  mid=arzmdAAEAAFLqT  ig_did=255719A2  sessionid=<constant>
2026-10-01 10:05  mid=ar4wQQAEAAFcrL  ig_did=F66CA281  sessionid=<constant>   <-- refresher overlay
2026-10-02 10:06  mid=ar-CHQAEAAHM2B  ig_did=418BB50C  sessionid=<constant>
...  (mid/ig_did change on EVERY refresher run; sessionid never changes)
```

The `sessionid` stays byte-identical while `mid`, `ig_did` and `datr` rotate on
every refresher pass. **Instagram binds a `sessionid` to the device `mid`.** The
headless Chromium launches with a fresh profile, Instagram sees a *new device*,
issues a different `mid`, and the refresher overlaid that new mid back into the
jar while keeping the operator's original `sessionid` — so the session became
self-invalidating and died within a day. The first mid rotation (Sep 30 08:11)
coincides exactly with the start of the death streak. Uploading a fresh jar only
bought ~a day before the next refresher run re-corrupted it.

### Fix — `utils/cookie_refresher.py`
Added `_DEVICE_IDENTITY` (per-jar set): `igcookies.txt → {mid, ig_did, datr}`,
`ttcookies.txt → {ttwid}`. `_refresh_one` now (a) excludes these from the
rotation-diff and (b) never writes them back in the overlay — the operator's
uploaded device identity is authoritative. The session-rotation cookies
(`csrftoken`, `rur`, `sessionid`, …) are still overlaid as before. This keeps
the jar warm without breaking the device binding.

> Note: `utils/ig_anti_detect.write_back_session` still overlays the `mid` that
> Instagram issued to the **authenticated** instagrapi private session. That mid
> is consistent with the sessionid (same device the session was minted on), so
> it is safe and was left alone.

## 2. IG worker attempted automated re-login (operator explicitly forbade this)

The operator wants **no automated login attempt when the IG cookie is
invalidated** — only a fresh jar upload. The code did the opposite: on a dead
session it retried login on an exponential-backoff cadence forever (8 retries
logged Oct 7 alone), and mid-poll it re-logged in and slept 1 h, logging
"session expired — attempting re-login." repeatedly.

### Fix — `modules/direct_forward/instagram.py`
- Removed the exponential-backoff retry loop at startup and the mid-poll
  re-login. On ANY login failure the worker now logs once, DMs the operator once,
  and calls the new `_ig_wait_for_fresh_jar(cl, loop)`, which polls the jar's
  mtime every 60 s and only attempts a login after the operator uploads a new
  file (mtime change). `relogin_failures` resets after a successful fresh login
  so a later death re-alerts.
- `_ig_native_deliver_once` no longer re-logs on a mid-delivery `LoginRequired`;
  it raises so the caller falls back, and the poll loop's `LoginRequired`
  handler takes over.
- `FriendMedia/IG` already had a circuit breaker (no re-login) — unchanged.

## 3. X/Twitter direct-forwarder down — XChat bridge crash loop

The operator sent content to the bot's X account and received nothing. Root
cause: the Deno XChat sidecar (`xchat_bridge.mjs`) was crash-looping since the
Oct 7 reboot with:

```
OnDemandFileUrlResolutionError: Unable to resolve the X ondemand chunk URL from the homepage runtime.
  at ClientTransaction.initialize (node_modules/x-client-transaction-id/...)
```

`x-client-transaction-id@0.3.1` could not resolve X's ondemand JS chunk. Since
the sidecar never connected, `cache/xchat_inbox.jsonl` stopped growing and the
worker had no messages to relay. (After the 0.3.2 bump the crash became
`Failed to initialize CycleTLS` — fixed by `emusks@2.3.15`.)

### Fix — `package.json` + `node_modules`
- `x-client-transaction-id` 0.3.1 → **0.3.2** (added as a direct dep so the pin
  is explicit).
- `emusks` 2.3.6 → **2.3.15**.
- Restarted `tgbot-xchat-bridge.service`; it logged in as the bot's X account,
  recovered the XChat identity, and immediately emitted the 136-message backlog.
  The worker relayed the pending tweets (45 relays observed within minutes).

## 4. X relay crash on non-ASCII (Persian) filenames

`utils.queue_manager` logged 5 task failures:
`ValueError: Failed to decode "cache/x_.../<persian-title>.NA"` from
`pyrogram ... send_photo`. Two compounding causes:
1. `yt-dlp`'s `prepare_filename` returns a path that does not match the file on
   disk for long non-ASCII titles (filesystem truncation) and unknown `.NA`
   extensions (photo entries).
2. `probe_video_dimensions` returns `(320, 320, 0)` on ANY ffmpeg error, so a
   missing file was misclassified as a photo and routed to `send_photo`, which
   then tried to parse the path as a Telegram file_id.

### Fix — `modules/direct_forward/twitter.py::_x_deliver_tweet`
- If `result["file_path"]` does not exist, resolve the real media file from the
  per-download task dir (`cache/<cache_id>`, holds exactly this download).
- The `320×320` photo heuristic now also requires `os.path.exists(file_path)`.
- A final guard: if the media still can't be located, fall back to the share's
  own mp4, else post a clear "Download failed" note (never crash the queue).

## 5. Monitoring stack — all healthy

- `tgbot.service` — active.
- `tgbot-xchat-bridge.service` — active (was crash-looping; fixed).
- `cookie-watch.service` (inotifywait tamper monitor) — active.
- `fail2ban` — active.
- System monitor (Go binary `build/tgbot-monitor`) — running (spawned detached
  by the bot; `tgbot-monitor.service` is installed-but-disabled by design).
  It was correctly firing `>=80%` CPU warnings — the load is the OpenCode agent
  session itself (2 vCPU box), not the bot; it clears when the agent exits.

## 6. New master pause: `IG_AUTH_ENABLED` (Admin → 🔐 IG Auth)

Added a console toggle (`admin_toggle_ig_auth`, button on the main console)
that pauses EVERY Instagram feature needing an authenticated session, so the
account can sit untouched while an appeal/suspension is pending or the
sessionid is dead. Persisted to `.env`; default true.

When `IG_AUTH_ENABLED=false`:
- The direct-forward supervisor does **not** start the IG worker → no login is
  ever attempted (this is what the operator wanted).
- `modules/friend_media/instagram.py::_ig_client()` raises
  `IGUnavailable("Instagram is paused…")`; `_run_archives` treats IG as
  `ig_paused` so no per-friend IG calls or inter-friend pauses happen.
- `utils/cookie_refresher.py` does not visit instagram.com at all.
- The relay watchdog ignores `ig` (no false "stalled" alert).
- Turning it back ON restarts the bot to start the worker.

The Direct-Forward and Friend Media menus show the paused state. This box's
`.env` was set to `IG_AUTH_ENABLED=false` at the end of this session (the
account was mid-appeal and the sessionid was dead), so the IG worker is not
running and no login is being attempted.

## 7. Second pass — X backlog gap: the cursor advanced on ENQUEUE, not on RELAY (2026-10-08, later)

After the sidecar fix above, the operator reported that a specific X range
(https://x.com/TV3JP/status/2104818326098886708 … https://x.com/panteradrop50k/status/2107715135133409437,
inclusive) **still never arrived** in the Telegram chat. The section-3 note
("45 relays observed within minutes") was the incomplete picture: the worker
raced through the 136-message burst but only the relays that happened to run
before the next restart were delivered.

### Root cause

`utils/shared.queue` (`DownloadQueue`) is **in-memory only**; a restart discards
every not-yet-run job. `_enqueue_relay` (modules/direct_forward/common.py) was
fire-and-forget — it `create_task`ed the enqueue and returned immediately — and
the workers advanced their dedup cursor the moment the relay was **queued**:

```
_X_read_inbox → for line: _x_process_bridge_line()   # only ENQUEUES
                          _advance(line)              # cursor moves NOW
```

So the worker advanced the peer-conversation cursor to the last line within
seconds while the queue was still draining earlier messages. The bot restarted
several times that morning (the fix session); every relay not yet run was lost,
and because the cursor was already past those lines, `_x_read_inbox` never
returned them again. Evidence: the worker's cursor sat at `2108156385708154880`
(the last line) while the log's last in-gap relay was `…/DomKinggMalcolm/2104888932899205541`
(seq `2105129464476684288`, 09-30 02:55) — **79 messages** (09-30 tail + the
10-05, 10-07, 10-08 02:xx batches) were below the cursor yet never relayed.

The same latent bug existed in the IG worker (`_ig_process_message` →
`_enqueue_ig_relay`) and the TikTok worker (marked the push `seen` *before*
relaying). The documented "at-least-once" cursor rule was therefore not actually
enforced for relay failures — it only caught enqueue errors, which never happen.

### Fix

- `modules/direct_forward/common.py::_enqueue_relay` now returns an
  `asyncio.Future` that resolves once the job has actually RUN (or raised);
  a module-level `_relay_submit_tasks` set keeps the submit task from being GC'd.
  New `_await_relays(futures)` awaits a batch with `return_exceptions=True` and
  re-raises the first failure (so no "exception never retrieved" warning).
- All three workers now await the relay before advancing their cursor:
  `_x_process_bridge_line` / `_x_process_message` (try/finally + `_await_relays`),
  `_ig_process_message` (same), `_tt_process_message` (awaits, and
  `_tt_run_ws` marks the push `seen` only AFTER a successful relay — new
  `_tt_persist_seen`). A genuine relay failure now leaves the cursor behind so
  the next poll retries it.
- Because a big backlog now blocks the worker while each relay runs,
  `_twitter_worker` refreshes `mark_worker_alive("x")` per bridge line so the
  relay watchdog can't false-alarm during a long replay.

### Recovery

The bridge inbox is append-only, so the dropped lines were still on disk. A new
tool `tools/recover_x_gap.py` rewinds a conversation cursor to the last
delivered seq and optionally drops already-delivered lines at/above a boundary:

```
python tools/recover_x_gap.py \
  --conv 1743868576920928256:2095053127040876548 \
  --resume 2105129464476684288 --drop-from 2108135431178989568
```

It backs both files up as `*.pre-recover.<ts>`. Run it with the bot (and the
bridge) stopped, then start them; the worker replays the gap on its next poll.
Applied here: **79 messages replayed** and delivered 12:51–12:59 UTC (TV3JP …
panteradrop, inclusive), 18 already-delivered lines dropped to avoid duplicates.

## Operator follow-ups

- **If a direct-forward range ever goes missing again:** the fix means a relay
  failure now retries instead of vanishing, but if a restart still drops
  something, `tools/recover_x_gap.py` recovers it from `cache/xchat_inbox.jsonl`
  (stop bot+bridge first). Check `direct_forward_state.json` →
  `x.cursors[<conv>]` vs the newest inbox line to spot a cursor that ran ahead.
- **When the appeal resolves and you have fresh cookies:** upload the new
  `igcookies.txt` (Admin → 🍪 Cookie Jars → Instagram → ✏️ Replace), then tap
  **Admin → 🔐 IG Auth** to turn Instagram back ON (the bot restarts and the IG
  worker logs in once).
- **Upload a fresh `igcookies.txt`** (Admin → 🍪 Cookie Jars → Instagram →
  ✏️ Replace). With `IG_AUTH_ENABLED=true`, the worker (if parked in
  `_ig_wait_for_fresh_jar`) logs in automatically the moment the jar's mtime
  changes — no restart needed. (Currently paused, so re-enable first.)
- Keep the new `x-client-transaction-id` / `emusks` pins; re-bump only if X
  changes its homepage runtime again (the crash signature is
  `OnDemandFileUrlResolutionError`).

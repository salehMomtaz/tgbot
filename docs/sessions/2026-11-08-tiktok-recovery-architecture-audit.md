# Session — TikTok relay recovery + full architecture audit

Date: 2026-11-08 (UTC) · Box: Ubuntu 24.04, `tgbot.service` (systemd, user `dev`)

## 1. Executive summary

**TikTok root cause (evidence-backed, two independent incidents):**

1. **Pre-2026-11-07 (push-only ingestion).** The worker only consumed the IM
   WebSocket push channel. TikTok delivers each self-DM message exactly once, so
   any share sent while the socket was down was lost with no recovery path. The
   confirmed casualty is the **15-share batch of 2026-10-23 19:53–20:31 UTC**:
   the operator→bot chat contains **no message at all** between 17:50 and
   02:22 the next day, while TikTok history still holds all 15 shares. The
   history reconciler landed only on 2026-11-07 (`cd14fea`, `cb4246b`,
   `eb2246e`), i.e. after that loss.
2. **2026-11-08 04:44–05:17 (photo-post crash).** Photo-mode posts relayed
   through `deliver_tiktok_photo` raised `NameError: name 'send_rich_album' is
   not defined` — a leftover from the rich-media removal refactor. ItemId
   `7703976903874481441` failed three relays and was auto-abandoned. Fixed at
   HEAD by `62c350f` (05:38) and recovered at 05:42 by an operator replay.
   `send_rich_album` no longer exists anywhere in the tree.

**Recovery result:** 14 of the 15 missing shares were re-delivered and verified
in the Telegram chat; 1 (`7698281977794874631`) is **deleted on TikTok's side**
(`status code 203008`), not a bot failure.

**Additional live bugs found and fixed by the audit (all were silent data
loss, not cosmetics):** a dropped queue relay left its future pending forever
(wedging the DM relays after "Abort Operations"); a queue-full rejection did the
same; a TikTok photo post that delivered nothing was recorded as delivered and
deduped forever; a first-run TikTok prime exited the worker permanently; X's
3-strike give-up counter was never persisted (one bad tweet wedged the X relay
forever); `_full_text` was referenced but not imported (NameError on every
text-only tweet); `xma_entries` was undefined in the IG story/highlight path;
and `database.json` corruption was silently overwritten with an empty DB.

## 2. Baseline (before any change)

| item | value |
|---|---|
| branch / commit | `main` @ `45d057a` (clean, 0 ahead / 0 behind `origin/main`) |
| service | `active`, MainPID 658, `NRestarts=0`, up since 07:31:32 UTC (box reboot) |
| memory | 3.9 GB total, 161 MB free, 2.2 GB swap used (bot RSS ≈ 169 MB) |
| disk | 96 GB, 27 % used |
| log coverage | `bot.log` 2026-11-04 19:33 → now; `.1` 11-02→11-04; `.2` 10-28→11-02 |
| TikTok config | `TIKTOK_DIRECT_ENABLED=true`, reconcile 900 s ±40 %, jitter 40 % |
| TikTok state | `primed=true`, `seen_msg_ids=214`, `relayed_item_ids=4`, watermark `2026-11-08 04:47 UTC` |

## 3. TikTok path trace and failure timeline

Production path: self-DM → WS push (`/ws/v2`, cmd-1001 ack) **and** HTTP
`get_by_conversation` → self-DM filter (`0:1:{uid}:{uid}`) → tolerant protobuf
walk → `itemId` → `seen_msg_ids` / `history_watermark_us` / `relayed_item_ids`
→ `_enqueue_relay` Future → single-worker queue → `download_media` /
`deliver_tiktok_photo` → `classify_media_file` → Telegram → `_state_save_owned({"tiktok"})`.

| time (UTC) | event | evidence |
|---|---|---|
| 2026-10-23 19:53–20:31 | 15 shares sent to the self-DM; nothing delivered | chat has zero messages 17:50→02:22; history still lists all 15 |
| 2026-11-07 12:28–17:47 | 8 shares; all delivered (push path) | chat contains every itemId |
| 2026-11-08 04:03–04:47 | 20 shares; 18 delivered live, 2 photo posts failed | log + chat |
| 04:44:40 | photo `7703976903874481441` → `NameError: send_rich_album` | `logs/bot.log` traceback |
| 05:03:27 / 05:17:34 | same post retried, failed; 3 strikes → abandoned | "giving up … after 3 failed relays" |
| 05:26 / 05:38 | caption refactor + rich-media removal (`ef6b785`, `62c350f`) | git log |
| 05:42:21 | operator replay; post + neighbours delivered | "photo post …: 2 image(s), audio sent" |
| 07:31:46 | service restart; WS connected, reconciler active, nothing new to do | log |

**Historical accessibility:** yes — `get_by_conversation` returns the **entire**
self-DM conversation (118 shares, oldest 2026-08-19) within `_TT_HISTORY_PAGES`
(100 pages × 20 rows); the walk terminated naturally, so no history is beyond
reach of the reader.

**Delivery ground truth method:** the operator's Telethon user session was used
**read-only** to scan the bot chat and extract every TikTok itemId present in
message text/captions. That is how "delivered vs missing" was decided — not log
lines alone.

## 4. Recovery (targeted, duplicate-free)

The existing `tools/tiktok_replay.py --since` mode rewinds the watermark **and
clears `relayed_item_ids`**, so it re-sends every already-delivered post in the
window — unusable for a surgical recovery. Added `--ids <itemId,...>`:

* writes `{"item_ids": [...]}` (no watermark rewind, no dedupe clear),
* the worker relays only those ids wherever they sit in history, skips any
  already in `relayed_item_ids`, and leaves the watermark untouched,
* the pending list is persisted **before** the first relay (crash-durable),
* an unavailable id is retried 3× then explicitly abandoned with a warning,
* a targeted item's failure does **not** block the other requested ids.

Run: 15 requested → 15 found → 15 not delivered → **14 delivered, 1 deleted**.

| result | count | ids |
|---|---|---|
| recovered | 14 | 7687526049688685845, 7682921505453313294, 7696029177476058382, 7698812768383798560, 7696621830039211271, 7698635542191246606, 7696391605510507797, 7697716357625908502, 7692461497519197462, 7699181452780621063, 7696431955071487239, 7686183333025336608, 7698519711570611488, 7692630320377646343 |
| duplicate-skipped | 0 | — |
| failed (deleted on TikTok, `203008`) | 1 | 7698281977794874631 |
| blocked | 0 | — |

Verification: Telethon chat scan finds **14/15** itemIds in the chat after the
run; the only absent one is the deleted post.

## 5. Code changes (TikTok + relay correctness)

| file | change |
|---|---|
| `modules/direct_forward/tiktok.py` | photo relay records success only when `deliver_tiktok_photo` really delivered; prime connect falls through to the relay loop instead of returning; targeted `replay_item_ids` support (persisted before first relay, watermark untouched); replay request applied only after the conversation resolves; replay failures don't block sibling ids |
| `tools/tiktok_replay.py` | new `--ids` targeted mode + honest `--since` duplicate warning |
| `modules/direct_forward/common.py` | `_enqueue_relay` resolves its future on queue-drop and on queue-full rejection (`_RelayAborted`) |
| `utils/queue_manager.py` | `QueueTask.on_cancel`; queued-drop hook |
| `modules/admin/callback_dispatch.py` | Abort resolves dropped relay futures and no longer clears `_active` (which spawned a second worker mid-download) |
| `modules/direct_forward/twitter.py` | import `_full_text`; persist the 3-strike counter before `break` |
| `modules/direct_forward/instagram.py` | `xma_entries` → `entries` (pk-only story/highlight NameError) |
| `utils/gate.py` | corrupt `database.json` is quarantined (timestamped copy, 0600) before defaults are used |
| `utils/cookie_refresher.py` | `direct_ig_session.json` is cleared only when the jar's `sessionid` VALUE changed (AGENTS invariant #13 device-churn) |
| `tests/test_relay_regressions.py` | 9 new tests pinning every fix above |

## 6. Deployment evidence

* 08:31 restart (pre-recovery code) → active, `NRestarts=1`, TikTok WS connected.
* 08:34 restart → targeted replay applied, recovery began.
* 08:37 restart (replay-failure isolation) → active, `NRestarts=3`.
* Fresh startup log: POT provider healthy on `127.0.0.1:4417`, 6 handler tasks,
  supervisor started 3 platforms, IG "Resumed persisted direct session",
  TikTok "WS connected + cmd-1001 sent", no traceback.

(Continued in §7+ — audit findings, validation, secret scan.)

## 7. Fifteen-day operational error groups

Log coverage available on this box is 2026-10-28 15:50 → now (11 days; older
rotations are gone). 1,300 warning/error lines collapse to these signatures:

| signature | n | classification |
|---|---|---|
| `upload.SaveBigFilePart` "waiting N seconds" (pyrogram) | 1,139 | **expected/external** — Telegram upload throttle |
| `Unable to connect / Connection failed` (pyrogram) | 79 | **expected/external** — network blips, self-heals |
| CookieRefresh "headless profile seen as a NEW device (ttwid/datr/ig_did/mid differ)" | 16 | **expected** — device-identity exclusion working as designed |
| `[DirectForward/X] … No video could be found in this tweet` | 8 | **expected/external** — text-only tweets; the *relay* was broken (see F-04, fixed) |
| `[DirectForward/TT] could not resolve author … skipped` | 7 | **already resolved** (2026-11-07, `cd14fea`); chat scan proves delivery |
| `[DirectForward/X] self-DM conversation fetch failed` | 5 | **expected/external** — transient X API; retried |
| Cookie jar "last warm 26-30d ago" | 9 | **deferred** — IG session freshness; operator jar upload |
| `[TikTokPhoto] embed <id> -> HTTP ?` | 4 | **expected/external** — the deleted post (203008) |
| `[DirectForward/X] … relaying text-only` | 3 | **fixed** (F-04) |
| `send_rich_album is not defined` | 3 | **already resolved** at HEAD (`62c350f`); F-01 hardened the silent-success sibling |
| `[TikTok] Video not available 203008` | 3 | **expected/external** — deleted source |
| `item … failed: name 'xma_entries' is not defined` | 2 | **fixed** (F-05) — real IG relay loss on 11-08 03:30/03:37 |
| `model fallback fetch failed: 'urls'` | 2 | **deferred** — twikit model quirk; the GraphQL walk is the primary path and handled it |
| `backlog: giving up on itemId … after 3 failed relays` | 2 | **expected** — deliberate 3-strike abandonment (one is the deleted post) |
| `Instagram post read failed: Instagram required/refused` | 2 | **expected/external** — login-walled IG post, documented message |

## 8. Source coverage checklist

| batch | files | status |
|---|---|---|
| entry/config | `main.py`, `config.py`, `.env.example`, `requirements.txt`, `utils/updater.py` | ✅ |
| downloader runtime | `modules/downloader_handler.py`, `utils/downloader/*` | ✅ |
| streaming/subscription | `stream_handler.py`, `stream_interceptor.py`, `modules/subscription/*` | ✅ |
| admin console | `modules/admin/*` | ✅ |
| friend media | `modules/friend_media/*` | ✅ |
| extras | `modules/github|youtube|translate|web/*` | ✅ |
| direct-forward | `tiktok/instagram/twitter/common/state/supervisor`, `xchat_bridge.mjs`, bridge wrapper | ✅ |
| utilities | `queue_manager`, `uploader_handler`, `cookie_manager/refresher/history`, `logger/*`, `security`, `rich_media/stream`, `filenames`, `shared`, `gate`, `pot_provider`, `ig_anti_detect`, `premium_session` | ✅ |
| operations | `install.sh`, `run.sh`, `uninstall.sh`, `deploy/*.service`, `cmd/tgbot-monitor/*`, `utils/system_monitor.py` | ✅ |
| tests | all 9 `tests/test_*.py` | ✅ |

Mechanical scans run as leads: `py_compile` (clean), `pyflakes` (19 findings →
all undefined names fixed; remainder are unused locals, P3), `ruff --select
F,E9,B`, `vulture`, plus targeted greps for TODO/FIXME, removed-runtime
references (Bale/aiogram/admin_webapp/MQTT: **0 runtime hits**), config drift and
protected-cache files.

## 9. Findings (deduplicated)

Format: `ID | severity | status | file:line | evidence | impact | recommendation | risk/effort`

| ID | sev | status | file:line | evidence | impact | recommendation | risk/effort |
|---|---|---|---|---|---|---|---|
| F-01 | P1 | fixed | `modules/direct_forward/tiktok.py:562` | `deliver_tiktok_photo` returns False on "no files"; queue propagates only exceptions | photo post recorded as relayed → deduped forever | capture the return value | low/low |
| F-02 | P1 | fixed | `tiktok.py:859` | prime branch ended with `return`; supervisor starts the worker once | TikTok relay dead after first enable until a 2nd restart | fall through to the loop | low/low |
| F-03 | P1 | fixed | `modules/direct_forward/twitter.py:722` | `_full_text` used, not imported (pyflakes) | NameError on every text-only tweet | import it + test | low/low |
| F-04 | P1 | fixed | `twitter.py:1012` | `break` before the only `_state_save_owned` | strike counter reset each poll → X cursor wedged forever | persist before break | low/low |
| F-05 | P1 | fixed | `modules/direct_forward/instagram.py:103` | `xma_entries` undefined (pyflakes); log 11-08 03:30/03:37 | pk-only story/highlight relay crashes | use `entries` | low/low |
| F-06 | P1 | fixed | `modules/admin/callback_dispatch.py:81` | `_pending.clear()` + `_active=False`, futures resolved only inside the task | Abort left workers blocked forever + double worker | `on_cancel` hook; leave `_active` | low/low |
| F-07 | P1 | fixed | `modules/direct_forward/common.py:455` | `add_task` returns "" on depth cap; future never resolved | relay caller waits forever | resolve with `_RelayAborted` | low/low |
| F-08 | P1 | fixed | `utils/gate.py:42` | corrupt DB → `return default_db`, next mutation persists it | silent wipe of authorized/blacklist/premium | quarantine copy + loud log | low/low |
| F-09 | P1 | fixed | `utils/cookie_refresher.py:405` | deleted `direct_ig_session.json` on every IG refresh (log 07:40) | new randomized device per day → AGENTS #13 churn/ban risk | clear only when sessionid changes | low/low |
| F-10 | P2 | fixed | `modules/admin/register.py:526` | `startswith("waiting_for_replace_")` also matches per-site state; first handler breaks the group | per-site jar upload unreachable | exclude the per-site prefix | low/low |
| F-11 | P2 | fixed | `tools/tiktok_replay.py:132` | `--since` clears `relayed_item_ids` and rewinds the watermark | uncontrolled duplicate replay | new `--ids` targeted mode | low/low |
| F-12 | P2 | fixed | `tiktok.py:627` | replay request deleted before the conv resolved | stale session swallows the request | apply after conv resolve | low/low |
| F-13 | P2 | fixed | `tiktok.py:882` | "IM WebSocket: can authenticate" derived from wid only | misleading health check | honest wording | low/low |
| F-14 | P2 | fixed | `utils/downloader/download.py:308` | `e` used after its except block (PEP 3110) | UnboundLocalError on empty yt-dlp message | use `error_msg` | low/low |
| F-15 | P2 | fixed | `tools/start_xchat_bridge.sh:126` | `BRIDGE_RUNNING=1` forever after a Deno `exit` | X relay silently dead until unit restart | `kill -0` liveness check | low/low |
| F-16 | P2 | fixed | `modules/direct_forward/instagram.py:937` | `delivered_in_run` initialised once, never reset | IG backlog never drains after the cap | reset per poll | low/low |
| F-17 | P3 | fixed | `xchat_bridge.mjs:75` | `writeFileSync(STATE, …)` non-atomic | truncated cursor → re-prime past backlog | tmp + rename | low/low |
| F-18 | P2 | deferred | `modules/direct_forward/instagram.py:998` | `activity[act_key] = act_now` saved before the relay | an item that fails is not retried until the thread sees new activity | restore the prior stamp when any item failed | medium/medium |
| F-19 | P2 | deferred | `modules/friend_media/instagram.py:770` | watermark advances to newest pk when `delivered` (any success) | a mid-batch failure is skipped forever | advance per delivered pk | medium/medium |
| F-20 | P2 | deferred | `utils/ig_anti_detect.py:552` | `cold_start_jitter` awaits blocking instagrapi calls on the loop | freezes pyrogram + X/TikTok for 60-90 s per IG login | `run_in_executor` | low/medium |
| F-21 | P2 | deferred | `modules/direct_forward/instagram.py:521` | `_ig_resolve_user_id` (private API) called inline in `_ig_process_message` | 2-4 s loop stall per IG DM | executor + cache | low/medium |
| F-22 | P2 | deferred | `utils/cookie_refresher.py:75`, `utils/cookie_history.py:79`, `utils/ig_anti_detect.py:99` | `#HttpOnly_` lines skipped before the prefix test, so the HttpOnly branch is unreachable | Chrome-exported jars lose HttpOnly cookies (incl. `sessionid` fingerprint) | strip the prefix before the `#` skip | low/low |
| F-23 | P2 | deferred | `modules/admin/callback_dispatch.py:558` | restore/savebackup ignore `cookie_key`, always use the YT backup | a stale IG "restore" button copies the YouTube backup over the IG jar | per-jar backup paths | low/medium |
| F-24 | P2 | deferred | `run.sh:78` | unconditional `pip install -r requirements.txt` before `exec` | a PyPI blip at boot crash-loops the unit | guard/fail-soft | medium/low |
| F-25 | P2 | deferred | `uninstall.sh:58` | does not disable `tgbot-xchat-bridge`, remove `build/`, `node_modules/`, the LE hook | incomplete uninstall leaves a running unit | mirror install.sh | low/low |
| F-26 | P3 | deferred | `utils/system_monitor.py:43` | pidfile check is `os.kill(pid,0)` only; docstring claims a cmdline check | recycled PID → monitor never spawns | implement the `/proc` cmdline check | low/low |
| F-27 | P3 | deferred | `utils/logger/queued_channel.py:112` | `plain` fallback reuses the rich (>4096) text | the documented plain fallback cannot succeed for long entries | truncate the plain text | low/low |
| F-28 | P3 | deferred | 9 sites | unused locals (`pyflakes`) | none at runtime | remove when touched | low/low |

### Dead code with proof

Zero non-definition references (`grep -rn` across tracked `*.py`, excluding
`learn/`): `utils/shared.py::get_setting_bytes`, `utils/shared.py::wait_for_stop`,
`utils/security.py::is_safe_username`, `utils/cookie_history.py::format_events`,
`modules/direct_forward/instagram.py::_ig_jar_mtime`,
`tools/tiktok_replay.py::STATE_FILE`, `utils/ig_anti_detect.py` `loop_holder`.
Duplicates that can drift (do **not** mass-delete): `_activity_stamp` (common.py
vs state.py), `_has_real_cookie_line` (admin/cookies.py vs cookie_manager), three
image-magic predicates (`thumbnails.py`, `tiktok_photo.py`,
`direct_forward/common.py`). Removals deliberately deferred: several are
re-exported by `modules/direct_forward/__init__.py` and the AGENTS invariant list
still names them.

## 10. Validation evidence

```
venv/bin/python -m py_compile $(git ls-files '*.py')      → OK
for f in tests/test_*.py; do venv/bin/python "$f"; done   → 9/9 files, 68 tests pass
bash -n install.sh run.sh uninstall.sh tools/start_xchat_bridge.sh → OK
node --check xchat_bridge.mjs                              → OK
cd cmd/tgbot-monitor && go test ./...                      → ok tgbot/monitor
import smoke of every edited module                        → IMPORT_OK
git diff --check                                           → OK
executable bits install.sh/run.sh/uninstall.sh/bridge      → 100755
```

## 11. Deployment health

| check | result |
|---|---|
| `systemctl is-active tgbot` | active |
| MainPID / NRestarts | 21271 / 4 (one per deliberate code deploy) |
| startup | 6 handler tasks ×2 sessions, POT healthy on `127.0.0.1:4417`, supervisor 3 platforms, IG resumed persisted session, TikTok WS connected |
| new tracebacks after deploy | none beyond the expected deleted-post `203008` |
| queue | idle, no stuck worker |
| TikTok recovery state | `replay_pending=None`, `relayed=19`, `fails={}`, watermark unchanged |

## 12. Secrets

* `git ls-files | grep -E '\.(env|session)$|cookies/|database\.json|direct_forward_state|direct_ig_session|telethon_session'` → **empty**.
* `git status` before commit showed only source, tests and docs; no jar, session,
  state or env file.
* No secret value was printed during the session; the Telethon probe reported
  only counts and itemIds.
* `.env` remains git-ignored; the session report contains no credentials.

## 13. Deferred work, by impact

1. **F-18** IG thread-activity watermark before the relay (one failed item waits
   for new thread activity). Default: restore the previous stamp on any failure.
2. **F-19** Friend-Media IG watermark advance (a failed mid-batch post is skipped).
3. **F-20/F-21** two blocking instagrapi calls on the event loop (60-90 s and
   2-4 s stalls).
4. **F-23** per-jar cookie restore/savebackup.
5. **F-22** `#HttpOnly_` parsing in the refresher/history/sessionid fingerprint.
6. **F-24/F-25** boot-time `pip install` hardening and uninstall symmetry.
7. **F-26/F-27/F-28** P3 cleanups.

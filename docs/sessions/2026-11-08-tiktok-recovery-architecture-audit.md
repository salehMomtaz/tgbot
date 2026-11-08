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

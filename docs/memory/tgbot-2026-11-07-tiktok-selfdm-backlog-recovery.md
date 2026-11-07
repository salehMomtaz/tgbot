# 2026-11-07 — TikTok self-DM relay dead + backlog recovery via the HTTP IM API

Operator report: *"I am not receiving TikTok posts I send to my self-DM.
Investigate, fix it, and find out whether messages already sent can be
accessed — a previous LLM said we miss them because the workaround is
push-based."*

## Snapshot / rollback

- Baseline commit: `4dba7f2` (branch `main`, clean tree). `tgbot` active,
  PID 652, `NRestarts=0`, up since 06:38 UTC.
- Rollback: `git checkout 4dba7f2 -- .` (or `git revert` the two commits) and
  send SIGTERM to the unit; `Restart=always` relaunches.
- Deployed commits: `cd14fea` (fix + tests), `c1f0ce0` (docs).

## Symptom and evidence

`logs/bot.log` showed the TikTok WebSocket connected and pushes arriving, but
every share was dropped:

```
[DirectForward/TT] could not resolve author for itemId 7704453223486196998 — skipped
... 7 occurrences 12:28–12:41 UTC
```

## Root cause (two bugs, one outage)

1. **Dead author lookup.** `_tt_oembed_author` built the yt-dlp URL as
   `https://www.tiktok.com/@<author>/video/<id>`, so it treated a missing
   author as fatal. TikTok's public oEmbed endpoint stopped returning
   `author_url`/`author_name` in Nov 2026 — a live probe returns only
   `version/type/width/height/html/provider_*/embed_*`. **Every** share after
   that hit `if not author: return`.
2. **Failure marked as success.** The caller added the `server_message_id` to
   `seen_msg_ids` after `_tt_process_message` returned normally, so each skipped
   push was recorded as handled. The push channel delivers each message
   **exactly once** (verified: a fresh probe connection after the outage
   received only the cmd-1001 ack, no replay), so those 7 shares were lost.

The author was never actually required: `_to_tiktok_embed_url` rewrites any
TikTok video URL to `https://www.tiktok.com/embed/<id>`, yt-dlp's
`TikTokIE._VALID_URL` accepts the embed form, and `download_media` already
skips cookies for it. A live `download_media(embed_url)` fetched a 2.53 MiB
MP4 + thumbnail in 2.2 s.

## Backlog question — YES, already-sent messages are readable

The old doc claim ("cmd 300/301 history reads return 200001, push is the only
path") is true only for the **WebSocket**. The separate HTTP IM API works and
needs no signature:

```
POST https://im-api-sg.tiktok.com/v1/message/get_by_conversation
  Accept/Content-Type: application/x-protobuf
  ?aid=1988&version_code=1.0.0&app_name=tiktok_web&device_platform=web_pc
   &msToken=<cookie>&X-Bogus=<random, unvalidated>
body: GetByConversationRequest {1:301, 2:1, 3:"1.6.0", 5:3, 8:{301:query}, 11:"web", 15:[metadata], 18:1}
query: {1:conv_id, 2:1, 3:source_id, 4:1, 5:cursor_ts_us, 6:20}
```

- `POST /v2/message/get_by_user_init` (type 203, sub_command 10006) resolves the
  self-DM `conversation_id` (`0:1:{uid}:{uid}`) and its `source_id`.
- This is the same wire schema as `httpjamesm/matrix-tiktok`
  (`pkg/libtiktok`, protobuf `im.pb.go`) — cross-checked field numbers there.
- A read-only probe returned the full 20-message page, including all 7 dropped
  shares, proving the backlog was recoverable.

## Fix

`modules/direct_forward/tiktok.py`:

- `_tt_relay_share(item_id, content, ...)` builds the embed URL directly and
  returns a bool; `_tt_resolve_author` is now cosmetic (oEmbed then the embed
  page's `"uniqueId"`), never a gate.
- `_tt_process_message` returns `True` only when handled; the caller bumps
  `seen` only then. A false return leaves the push unseen for reconciliation.
- `_tt_reconcile_backlog` (new): inbox → history pages → relay shares newer
  than `history_watermark_us` and absent from `relayed_item_ids`, oldest
  first. Runs on every WS connect and every
  `TIKTOK_DIRECT_RECONCILE_SECONDS` (900, jittered) while idle. Watermark
  advances only past handled messages; a failed share is retried and abandoned
  after `_TT_HISTORY_MAX_FAILS` (3) attempts so one poison item cannot wedge
  the cursor. First-ever run seeds the watermark to
  `now - TIKTOK_DIRECT_BACKFILL_MINUTES` (1440).
- State additions (same shared `direct_forward_state.json`, merge-only saves):
  `conv_id`, `source_id`, `history_watermark_us`, `relayed_item_ids`,
  `relay_fail_counts`. `_tt_persist_seen` now mutates the worker's live state
  dict (a throwaway copy would be clobbered by the next owned save — the same
  bug class as the X pairing write).

Config: `TIKTOK_DIRECT_RECONCILE_SECONDS=900`,
`TIKTOK_DIRECT_BACKFILL_MINUTES=1440` (`config.py` + `.env.example`).

## Verification

- `venv/bin/python tests/test_tiktok_backlog.py` — 7/7 (plus all other suites
  green; `py_compile` on every tracked file; `bash -n` on the scripts).
- Live download of the embed URL: 2.53 MiB MP4, thumbnail present, classified
  `video`.
- Deploy restart at 14:15:47 → first reconcile at 14:16 recovered **all 6
  distinct shares** (one itemId was sent twice) and delivered each to the
  relay chat:
  `✅ relayed https://www.tiktok.com/embed/<id> -> 7429671248 (video)` ×6.
- State after: watermark advanced to the newest row, `relayed_item_ids` holds
  the 6 ids, `relay_fail_counts` empty, service active, `NRestarts=1` (the
  intentional deploy).

## Notes / limits

- Reconciliation is a poll (default 15 min, jittered) — not instant; the push
  path remains the low-latency delivery route.
- History reads use the jar's `sessionid`/`msToken`/`s_v_web_id`; a dead jar
  affects both push and history equally.
- Do not reintroduce an author gate, an itemId-blind relay, or `seen`-on-failure.

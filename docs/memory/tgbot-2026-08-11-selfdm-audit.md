# tgbot self-DM audit — TikTok / X / Instagram (2026-08-11)

A full audit of the three direct-forward self-DM mechanisms
(`modules/direct_forward.py`) surfaced **five** hidden problems, all now fixed.
This document records what was found and why the fixes look the way they do.

Background: the same file carries three independent workers that read the
account's own DMs and relay media into `DIRECT_FORWARD_CHAT_ID`:

- **Instagram** — instagrapi private API poll (`_instagram_worker`).
- **X/Twitter** — twikit legacy DM poll (`_twitter_worker`) **plus** the Deno
  `xchat_bridge.mjs` E2EE sidecar whose canonical lines feed `_x_read_inbox`.
- **TikTok** — persistent IM WebSocket push (`_tiktok_worker` / `_tt_run_ws`).

## 1. The 2 h cache cleaner purged the XChat bridge cursor (data-loss window)

`main.py::auto_clean_cache_directory` swept **everything** under `cache/`
older than `max_cache_age_hours` (default 2 h), including
`cache/xchat_bridge_state.json` (the Deno bridge's `last_seq` cursor) and
`cache/xchat_inbox.jsonl` (the canonical line inbox the X worker consumes).

Deleting the **state** file is the dangerous one: on its next poll the bridge
sees no saved `last_seq`, re-primes it to the newest message and **skips
everything older** — messages that arrived while the bridge was down are
silently lost. The inbox deletion alone is harmless (the bridge re-creates it
next poll).

**Fix:** a `protected_files` skip-list in `auto_clean_cache_directory`
(`xchat_bridge_state.json`, `xchat_inbox.jsonl`). The platform cursor file
`direct_forward_state.json` lives at repo root, outside `cache/`, so it was
already safe.

Invariant: **persistent cursor files are never age-swept.** If you add a new
`cache/` file that holds a cursor/session position, add it to the skip-list.

## 2. Photo-only pasted-tweet links silently failed (twikit `KeyError: 'urls'`)

When a tweet is **pasted as a text URL** (not shared as a DM attachment) and
the tweet is photo-only (no video stream), the pipeline was:

`yt-dlp extract` → no videos → `_x_fallback_photos` → twikit
`client.get_tweet_by_id` → **`KeyError: 'urls'`** → `[]` →
`raise RuntimeError("tweet …: no video formats (photo-only tweet handled
natively)")` → the queue task failed and the media was **silently lost** (only
a failed-task log line).

Root cause is a **twikit 2.3.3 bug**, not a bot bug: `twikit/user.py:102`
`User.__init__` does `legacy['entities']['description']['urls']` with a direct
index (no `.get`). While parsing a tweet's author, any user whose bio
`entities.description` exists but has no `urls` key raises inside the whole
`get_tweet_by_id` call. (The same pattern exists at `guest/user.py:96-97`.)
The bot's `Tweet.urls` property is safe (`.get`); `media` reads
`_legacy['entities'].get('media', [])`.

**Fix (two layers, both applied):**
- `_x_fallback_photos` now catches the parse exception and, in addition to the
  `getattr(t, "media")` property walk, does a raw `t._data['legacy']` walk of
  `extended_entities`/`entities` → `media` (collecting `media_url` /
  `media_url_https` / `url`), so photos are found even when twikit's `media`
  property path is quirky.
- `_x_deliver_tweet` **never raises** for a no-media tweet any more: it sends a
  text-only relay ("⚠️ *No downloadable media* — this tweet exposes no video
  stream to yt-dlp and the photo fallback failed.") instead of dropping it.

Note the DM-*share* path was already fine (photo-only shares go native via
`_x_deliver_share_photos`); only pasted text URLs hit the failing path.

## 3. Blocking `requests.get` on the asyncio event loop (TikTok worker)

`_tt_wid` (TikTok `web-cookie-privacy/config`, 30 s timeout) and
`_tt_oembed_author` (TikTok oEmbed, 30 s timeout) are **synchronous**
`requests.get` calls invoked directly inside async coroutines
(`_tt_run_ws`, `_tt_process_message`). A slow TikTok endpoint froze the whole
event loop: pyrogram updates, the IG worker and the X worker all stalled for up
to 30 s. The admin "Test TikTok connection" button hit the same wall because it
called the blocking `_tt_wid` directly.

**Fix:** both call sites now go through
`loop.run_in_executor(None, …)`. `test_tiktok_connection` was converted to
`async def` (executor-wrapped `_tt_wid`), and `admin.py` awaits it.
`test_x_connection` stays sync — it is a pure local jar check, no network.

Invariant (reaffirmed): **no blocking network I/O on the event loop.** Any
sync `requests`/`urllib` call inside an async worker must be offloaded via
`run_in_executor`.

## 4. TikTok reconnect cadence used the wrong knob (dead config)

`_tiktok_worker` slept `_poll_interval()` — built from
`DIRECT_FORWARD_POLL_SECONDS` / `DIRECT_FORWARD_POLL_JITTER_PCT` — while its log
line, `config.py`, `.env.example` and the docs all claimed
`TIKTOK_DIRECT_POLL_SECONDS` / `TIKTOK_DIRECT_POLL_JITTER_PCT`.
`TIKTOK_DIRECT_POLL_SECONDS` was defined but **never used anywhere** (dead knob).

**Fix:** added `_tt_poll_interval()`, mirroring `_poll_interval()` but honoring
`TIKTOK_DIRECT_POLL_SECONDS` (fallback `DIRECT_FORWARD_POLL_SECONDS`) and
`TIKTOK_DIRECT_POLL_JITTER_PCT`, and wired it into `_tiktok_worker`. The TikTok
knobs are now real. IG/X keep the shared `_poll_interval`.

## 5. X worker read the cookie jar once at boot (restart required)

The X worker loaded `cookies/twitter/xcookies.txt` once at boot
(`_x_jar_cookies` → `client.set_cookies`) and never re-read it — so a mid-run
jar re-upload (or switching the relay to a different X account) required a bot
restart. IG re-reads the sessionid from its jar on every login retry, and
TikTok re-reads its jar on every reconnect; X was the odd one out.

**Fix:** the worker now re-reads the jar at the top of every poll, compares a
signature (`tuple(sorted(cookies.items()))`) against the applied set, and
re-applies only on change (`client.set_cookies(fresh)` + log). If the `twid`
changed (different account), it rebuilds the X client and re-primes the cursor.
The jar stays read-only (`0o444`); the throwaway-`httpx` invariant in
`_x_fetch_auth_bytes` is untouched.

## What did NOT change

- The merge-only shared-state rule from the duplicate-delivery incident
  (`_state_save_owned` / `_merge_state_save`) — still enforced everywhere.
- The XChat bridge protocol, canonicalize shape, or the
  bridge-seq-ids == legacy-DM-ids identity (`cache/xchat_bridge_state.json`
  `last_seq` == `x.last_id` still holds).
- The TikTok protobuf walker, the throwaway-httpx cookie isolation, the
  magic-byte media validation, or any cookie lifecycle invariant.

## Verify

- `python3 -m py_compile modules/direct_forward.py main.py modules/admin.py`
- Bot restart shows: `[DirectForward] started -> chat …, 3 platform(s)`,
  X polling `~300s`, TikTok `WS connected + cmd-1001 sent`, IG resumed session.
- A photo-only pasted tweet now delivers photos (or a text note, worst case).

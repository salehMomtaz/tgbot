# Direct-forward (IG / X / TikTok DM → Telegram) — history

All direct-forward incident and design notes merged. The user-facing setup stays in `docs/DIRECT_FORWARD_SETUP.md`; this file is the chronological history.

## Sources consolidated

- `docs/memory/tgbot-2026-08-08-x-pairing.md`
- `docs/memory/tgbot-2026-08-09-xchat-bridge.md`
- `docs/memory/tgbot-2026-08-11-selfdm-audit.md`
- `docs/memory/tgbot-2026-08-11-x-duplicate-delivery-state-race.md`
- `docs/memory/tgbot-2026-08-11-x-photo-paste-fix.md`
- `docs/memory/tgbot-instagram-risky-and-push-2026-08-13.md`
- `docs/memory/vps-two-bots-runtime-state.md`

---

---

## 1. Source: `docs/memory/tgbot-2026-08-08-x-pairing.md`

# tgbot-2026-08-08-x-pairing.md

## Feature: X/Twitter DM pairing — handshake + manual ID, entirely in chat

### What changed

Before this change, X direct-forward relied on the static env var
`X_DIRECT_FROM_USER_ID` as its only protection ("X cannot offer an inbox-wide
pairing handshake"). Now the X worker supports the same in-chat pairing as
Instagram:

- **🔗 Pair X/Twitter** (Admin Console → 📨 Direct-Forward) issues a one-time
  6-digit code. You DM the code to the bot's X account; the worker scans the
  bot's X DM inbox and binds the pair to your numeric X user id automatically.
- **⌨️ Set X ID manually** — type your numeric X user id directly; validated as
  digits, up to 19 digits (X ids are far past Telegram's 11-digit id gate, so
  this state is dispatched *before* `is_valid_telegram_id`).
- **💔 Unpair X/Twitter** — forgets the pair **and cancels any pending code**
  (improvement to `unpair_platform`, previously the pending handshake lingered).
- `X_DIRECT_FROM_USER_ID` remains as the static pre-pair / fallback; the
  persisted pairing wins over it. `X_DIRECT_ENABLED` + username/password are
  still required to start the worker.

### The mechanism (how the inbox scan works)

twikit has **no inbox-listing API** (only `get_dm_history(user_id)` for a known
conversation). The workaround, verified against twikit 2.3.3 on this box:

- `_x_fetch_inbox` hits X's own `Endpoint.DM_INBOX`
  (`https://x.com/i/api/1.1/dm/inbox_initial_state.json`) through twikit's v11
  client (`client.v11.base.get(..., headers=client.v11.base._base_headers)`).
  The full `_x_inbox_params` (dm_secret_conversations_enabled=false,
  cards_platform=Web-12, etc.) mirrors the battle-tested fork
  `adhikasp/mcp-twikit` PR #13 (`jrejaud/mcp-twikit`).
- **The inbox snapshot's `entries` are STALE** — the fork author added a commit
  for exactly this. So `_x_conversation_messages` re-fetches each conversation
  fresh via `client.v11.dm_conversation(conv_id, None)`.
- **Message requests are a separate bucket**: first-time DMs from accounts you
  don't follow land in `filter=low_quality`, NOT the trusted inbox. `_x_pairing_scan`
  scans **both** (trusted + message requests).
- On a code match the pair is locked, the cursor is bumped past the code message
  (so a fresh pair doesn't replay its pre-pairing backlog), the pending code is
  cleared, and Telegram confirms.

### X Chat (E2EE) caveat

X's 2025 "X Chat" rollout encrypts DMs behind a 4-digit passcode **when both
participants opt in**. twikit's legacy DM API cannot decrypt those (twikit
issue #401). The pairing scan and the relay both read the legacy API, so the
handshake conversation MUST stay in the normal (unencrypted) inbox. This is
called out in the Pair-X instructions, the module docstring, and
`docs/DIRECT_FORWARD_SETUP.md`. The `dm_secret_conversations_enabled=false`
param keeps the scan pinned to the legacy inbox.

### Files touched

- `modules/direct_forward.py` — `_x_inbox_params`, `_x_fetch_inbox`,
  `_x_inbox_conversations`, `_x_conversation_messages`, `_x_pairing_scan`,
  `set_platform_pair`, reworked `_twitter_worker` (env id optional; re-reads
  pairing state each poll; runs the pairing scan while a code is pending),
  docstring update.
- `modules/admin.py` — Direct-Forward menu keyboard (Pair X / Unpair X /
  Set X ID manually), callback handlers, `waiting_for_x_pair_id` text state
  (before the Telegram-id gate), menu render now shows X pairing status.
- `.env.example`, `docs/DIRECT_FORWARD_SETUP.md` — X pairing documented.
- `unpair_platform` now also cancels a pending handshake.

### Verification

- `python3 -m py_compile` on all `.py` — clean.
- Unit test (stub config/utils, fake twikit client): `set_platform_pair`
  round-trip, `pairing_status` pending text, unpair (returns True/False and
  cancels code), `_x_inbox_params` shape, `_x_pairing_scan` binds sender id +
  username (stored without `@`), bumps cursor to the code message, sends the
  "X/Twitter paired!" confirmation. All green.
- Live via Telethon (bot restarted, `[DirectForward] started -> chat
  7429671248, 1 platform(s)`):
  - Console → Direct-Forward shows `X/Twitter: not paired` + the new buttons.
  - Pair X → issues code + full handshake text incl. E2EE caveat.
  - Set X ID manually → typed `44196397` → "✅ X/Twitter partner set to id
    `44196397`", persisted in `direct_forward_state.json` as
    `x.paired.user_id`.
  - Invalid id `44x196397` → "❌ Invalid X user id. Enter digits only".
  - Menu showed `paired with @ (id 44196397) (code ... pending)` while both
    existed.
  - Unpair X → "💔 X/Twitter pairing removed.", state back to `{}`.
  - Test messages cleaned up after.
- Zero new errors/tracebacks in `logs/bot.log` after restart (the only
  warnings are benign `upload.SaveBigFilePart` throttle lines).

### Notes

- The live handshake **could not be fully exercised**: `X_DIRECT_ENABLED` is
  `false` in this deployment's `.env` (no X bot account credentials), so
  `_twitter_worker` never runs and the real inbox scan wasn't hit against live
  X. The unit test covers the logic with a fake client; the endpoint params are
  taken verbatim from the proven mcp-twikit fork. When X credentials are added,
  drive Pair X → DM the code → expect the confirmation.
- If twikit's v11 API surface changes (`Endpoint.DM_INBOX`,
  `v11.base.get`, `_base_headers`, `dm_conversation`), the module logs loud
  warnings from the try/except paths — verify those patch points still exist.

---

## 2. Source: `docs/memory/tgbot-2026-08-09-xchat-bridge.md`

# 2026-08-09 XChat (encrypted self-DM) bridge — the X direct-forward reads it via Deno sidecar

Live end-to-end pass proving the encrypted self-DM path for the X direct-forward
worker. The 2025 X Chat rollout means the operator's own "Message Yourself"
conversation can be **XChat-encrypted** (4-digit passcode, enabled by both
parties). twikit's legacy DM API (`dm_conversation`) returns those messages
**empty/encrypted**, so media you send to your own encrypted self-DM simply
never arrives. This pass wired a **Deno sidecar** (`xchat_bridge.mjs`) that
decodes the XChat thrift stream and hands the messages to the bot over a file.

## Architecture (data flow)

```
X self-DM (encrypted) 
  → xchat_bridge.mjs (Deno, auto-started tgbot-xchat-bridge.service)
      • logs into X with the SHARED cookies/twitter/xcookies.txt jar (auth_token+twid)
      • recovers the XChat identity from Juicebox realms with the operator's PIN (XCHAT_PIN)
      • polls ONLY the self-conversation <self_id>-<self_id> on a RANDOM 10–600 s window
      • decodes each new event (seq > cache/xchat_bridge_state.json last_seq):
          XChat-encrypted via conversation key (eciesUnwrap + decryptBody), or
          plaintext thrift for legacy-sent messages
      • appends one canonical JSON line per relayable message to cache/xchat_inbox.jsonl
  → modules/direct_forward.py _twitter_worker (primary source)
      • _x_read_inbox(bot_cursor) parses inbox lines with id > cursor, ascending
      • _x_process_bridge_line → same relay pipelines as twikit path (tweet/media/text)
      • bot cursor (direct_forward_state.json x.last_id) advances per line
  → shared DownloadQueue → yt-dlp → upload to DIRECT_FORWARD_CHAT_ID
```

**Cursor semantics (critical):** the XChat `sequenceId` lives in the SAME id
space as the legacy DM message id, so the bot's existing `x.last_id` cursor
dedupes bridge lines against any twikit-sourced messages. The bridge keeps its
OWN `last_seq` in `cache/xchat_bridge_state.json`; first boot primes it to the
newest message (backlog skipped), later boots resume so messages that arrived
while the bridge was down are still emitted. The bot NEVER truncates the inbox
— it filters by cursor, so re-delivery is impossible even though the file grows.

**Canonical line schema** (one JSON object per line, see `xchat_bridge.mjs`):
- `{"id","at","kind":"tweet","url","text"}` — tweet link
- `{"id","at","kind":"media","media_url","is_photo","text"}` — DM attachment
- `{"id","at","kind":"text","text"}` — plain text
Non-relayable events (reactions, deletes, edits, read receipts, key changes) are
skipped. Corrupt/partial lines are skipped by `_x_read_inbox`, never fatal.

## What was proven live (2026-08-09)

- Bridge boots clean as PID 322452: identity recovered (@therabenschwarz,
  `1743868576920928256`), cycletls listening on `0.0.0.0:19220`, random-window
  polling, zero WS errors in `/tmp/opencode/xchat_bridge.log`.
- Bridge emitted the operator's real tweet self-DM:
  `{"id":"2086336756970921984","at":"1786256419781","kind":"tweet","url":"https://x.com/i/status/2085955705731735718","text":""}`.
- Bot relayed it at `2026-08-09 06:59:29`: `✅ relayed tweet ... (top 1280p) ->
  7429671248`, a **7.7 MB mp4** (8,077,052 B), msg id **88946**, caption
  `📥 X DM from x-user 1743868576920928256`.
- **Verification gotcha:** `DIRECT_FORWARD_CHAT_ID=7429671248` is the
  OPERATOR's user id. From the bot's side the send target is the operator's
  chat, which the operator sees as the **bot chat** ("Angela Balzac", dialog id
  `7665239058` = the bot's own user id), NOT the self-chat dialog. Searching the
  self-chat for the relayed message finds nothing — look in the bot-chat dialog.
  Confirmed present there (photo relays ids 88923–88929 from IG + X all land in
  the same bot-chat dialog).

## Bugs found + fixed during the pass

1. **cycletls orphaned Go binary holds port 9119 → "WebSocket server not
   connected".** emusks' cycletls (v2.0.5, CJS) defaults to port 9119
   (`src/cycletls.js` reads `process.env.CYCLETLS_PORT`). Killing the bridge
   orphans the Go helper (`node_modules/cycletls/dist/index`), which keeps
   listening; the next instance's WS client can't attach. Fix: always launch
   with an explicit `CYCLETLS_PORT` (production uses `19220`). The bot's own
   PO provider deno (`bgutil-provider/server`, port 4417) is unrelated — never
   kill it.

2. **thrift string fields arrive as byte-maps, not JS strings.** Canonicalize
   dropped tweet/media lines because `post.data?.[2]` (the URL) is a thrift
   byte-map. Fix: run every attachment field through `thriftStr()`.

3. **Fixed short polling is a fingerprint.** Poll window is random-uniform
   10–600 s, re-rolled every cycle (`XCHAT_POLL_MIN_SECONDS` /
   `XCHAT_POLL_MAX_SECONDS`), never a fixed cadence — same hygiene rationale as
   the IG pacing (fixed short polling is what flagged the first IG account).

## Over-ceiling quality buttons (2 GB bot / 4 GB Premium) — whole-size threshold

`_x_deliver_tweet` (`modules/direct_forward.py`) implements the behavior you
asked about:

- ceiling = `_PREMIUM_HARD` (4 GB) when a Premium userbot session is configured,
  else `_BOT_HARD` (2 GB) — `utils/uploader_handler.py` constants.
- `top = videos[0]` (highest res). If `top["bytes"] <= ceiling` → auto-download
  the best quality, no keyboard.
- If `top["bytes"] > ceiling` → post an inline keyboard
  (`build_format_keyboard(cache_key, videos, audios, premium_allowed=True)`) so
  the operator picks a quality below the ceiling; the `dl:` callback does the
  rest.

**The threshold compares the WHOLE final file, never the video-only stream.**
`extract_formats` (`utils/downloader.py`) already sizes each video button as the
merged result:
- X/Twitter's `http-*` progressive streams are **muxed** (acodec set) → their
  `bytes` IS the full file already (`muxed=True`, no audio merge added).
- If a site (YouTube, IG DASH) exposes video-only streams, `best_audio_bytes`
  is added to `v['bytes']` before the button is built (downloader.py ~line
  849–862).
So even though `download_media` merges `{video}+bestaudio` on download, the
sizing, the button, and the ceiling check all agree on the merged whole. Do NOT
change the ceiling check to compare against a video-only `filesize`.

## Advice if X changes again (future breakage playbook)

- **Twikit legacy DM API stops returning message bodies** (X Chat rollout,
  API churn): this is exactly what the bridge exists for. The worker reads the
  inbox FIRST and only falls back to `dm_conversation` when no bridge file has
  ever been produced. If twikit dies entirely, the bot still works as long as
  the sidecar runs.
- **Bridge stops decrypting:** check (a) `XCHAT_PIN` matches the current
  passcode, (b) `cookies/twitter/xcookies.txt` is fresh (write-back keeps it
  warm; an expired `auth_token` fails login), (c) the XChat key/device state —
  `direct_ig_session.json`-style persisted sessions do NOT exist here; identity
  is re-derived from realms every boot.
- **`node_modules/` and Deno drift:** the bridge imports emusks from local
  `./node_modules` (not npm-global). After a `pip`/system upgrade that pulls a
  new emusks, verify the `xchat-crypto.js` helpers and `cycletls.js` port-read
  still exist. `node_modules/` is git-ignored.
- **Orphaned cycletls after a bridge kill** ("WebSocket server not connected"):
  find the Go listener on the CYCLETLS_PORT and kill by PID, then relaunch.
- **Never** add `ulimit -v`, never touch the PO provider's 127.0.0.1 binding,
  never widen the YouTube no-auth fallback (bot invariants #1/#2/#3).

## Operational notes

- The bridge is now a **systemd unit, not a manual sidecar** (2026-08-09
  follow-up): `deploy/tgbot-xchat-bridge.service` → wrapper
  `tools/start_xchat_bridge.sh` (parses `.env` dotenv-style — never `source` —
  gates on `X_DIRECT_ENABLED` + `XCHAT_PIN`, `exec`s Deno). install.sh installs
  AND enables it; an unconfigured bridge exits 0 so the unit never crash-loops.
  `XCHAT_PIN` belongs in `.env` (it is git-ignored; only `XCHAT_PIN=` empty
  appears in `.env.example`). After editing `.env`: `sudo systemctl restart
  tgbot-xchat-bridge`. Logs: `sudo journalctl -u tgbot-xchat-bridge -f`.
  npm deps (`emusks`/`cycletls`) are installed by install.sh (`npm install`
  → `node_modules/`, git-ignored). Runtime is Deno (already installed).
- Kill hygiene: `pgrep -x deno` + read `/proc/<pid>/cmdline` to pick the bridge
  (avoid `pkill -f` matching your own shell). Never kill the PO provider deno.
- Verification tool: operator's Telethon StringSession (`telethon_session.txt`,
  user `7429671248`) against the **bot-chat dialog** (`7665239058`), not the
  self-chat.
- Telethon install here is from **Codeberg**: the installed 1.44.0 METADATA
  declares `Project-URL: Source, https://codeberg.org/Lonami/Telethon`
  (GitHub is only a mirror and may be deleted — see their README). pip resolves
  to codeberg automatically.

## 2026-08-10 follow-up — X relay fully activatable in-chat, no SSH

The admin console now covers the whole X self-DM lifecycle, closing the two
gaps that previously forced SSH:

1. **In-chat PIN entry.** `Admin → 📨 Direct-Forward → 🔑 Set X Chat PIN`
   (`admin_direct_set_x_pin`) sets `USER_STATES = "waiting_for_x_pin"` (a
   free-form text state, dispatched before the `is_valid_telegram_id` gate in
   `admin_state_message_handler`, alongside the premium states). The operator
   sends the 4-digit passcode; the handler validates `\d{4}`, writes it with
   `dotenv.set_key(".env", "XCHAT_PIN", pin)` and refreshes `config.XCHAT_PIN`
   so the menu status updates without a restart. `config.py` now reads
   `XCHAT_PIN` (before, only the bridge wrapper did).

2. **Bridge self-activates/reloads — no `systemctl`.** `tools/start_xchat_bridge.sh`
   is now a **resident supervisor** instead of an exit-0-when-unconfigured
   wrapper: it re-parses `.env` every ~5 s and (re)spawns the Deno sidecar as
   soon as `X_DIRECT_ENABLED` + `XCHAT_PIN` + the xcookies jar all hold, and
   stops it when a gate drops. It restarts the sidecar when the PIN or jar
   mtime changes (the sidecar reads those at startup). The sidecar runs under
   `setsid` so a group `kill -- -$PID` tears down deno AND its cycletls Go
   child together (no orphaned port 19220 holder). Because it re-reads `.env`,
   the toggle-X/PIN-set flows need no ssh/systemctl: the wrapper picks the
   change up within ~5 s. The unit stays `Restart=on-failure`; the supervisor
   never exits on its own, so only a crash triggers a unit-level restart, and
   `KillMode=control-group` handles `systemctl stop` teardown.

3. **Menu status.** The Direct-Forward menu now shows `X Chat PIN: ✅ set
   (hidden) / ⚠️ not set — E2EE self-DM can't be read`.

**Verified live (2026-08-10):** after an accidental `pkill -f xchat_bridge.mjs`,
systemd `Restart=on-failure` relaunched the unit, which ran the NEW supervisor,
re-read `.env`, re-spawned the sidecar (deno PID restarted, logged in as
`@therabenschwarz` 1743868576920928256), and cycletls re-attached to port
19220 — proving the resident-supervisor path end-to-end on the production box.

---

## 3. Source: `docs/memory/tgbot-2026-08-11-selfdm-audit.md`

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

---

## 4. Source: `docs/memory/tgbot-2026-08-11-x-duplicate-delivery-state-race.md`

# 2026-08-11 X self-DM duplicate deliveries — shared-state race

Live incident on the production bot (chat `7429671248`, `tgbot.service`, box
clock Sun 2026-08-11, all timestamps UTC unless noted). The operator self-DM'd
a few X posts and each relayed **twice, then four times**. The failure mode,
root cause and the invariant-preserving fix are documented here so nobody
re-introduces a full-dict state write.

## Symptom

One self-DM'd media post (caption `📥 X DM from x-user 1743868576920928256`,
link `https://x.com/i/status/2085032034943017419`) was delivered to the relay
chat four times:

| # | bot.log UTC | operator local (Iran, UTC+3:30) |
|---|---|---|
| 1 | 04:18:15 | 07:48 |
| 2 | 04:27:44 | 07:57 |
| 3 | 04:34:00 | 08:04 |
| 4 | 06:02:57 | 09:32 |

Crucially it was **not** one duplicated message — the **whole self-DM backlog
(≈14 tweets) re-relayed in waves**, and the waves were not aligned with bot
restarts (only 4 boots in the whole log window, 5 relay waves in one boot).

## What the logs showed

Relay waves in `logs/bot.log` on 2026-08-10 UTC:

- `04:17:54–04:18:23` wave 1 — 7 tweets incl. `2085032034943017419`
- `04:22:04–04:22:52` wave 2 — 4 *different* tweets
- `04:27:25–04:28:21` wave 1 + 2 again (with `04:27:44` = delivery #2)
- `04:33:39–04:34:36` wave 1 + 2 again (with `04:34:00` = delivery #3)
- `04:34 → 05:56` **silence** (no X relays at all, despite polls continuing)
- `05:56:20` one genuinely new tweet relayed once
- `06:02:36–06:03:32` *entire* backlog again (with `06:02:57` = delivery #4)
- then silence again until the next boot

So the X worker's cursor advanced to the newest id, held for a while, then got
**reset backwards** and the whole backlog re-relayed. The "held for a while"
gap is the key tell: a per-boot cursor bug would re-relay every poll, not in
clusters with quiet gaps between.

## Root cause: concurrent read-modify-write on a shared state file

`modules/direct_forward.py` keeps ALL three direct-forward platforms (IG, X,
TikTok) in ONE JSON file `direct_forward_state.json`. Each platform runs its
own worker coroutine:

- `_instagram_worker` — loaded the state dict **once** at startup and held
  that stale in-memory copy for the process lifetime. On every poll where it
  had new DMs or a changed `thread_activity` watermark it called
  `_save_state(state)`, which writes the **whole dict** via tmp+`os.replace`.
- `_tiktok_worker` / `_tt_run_ws` — reloaded and saved the whole dict on every
  WS push.
- `_twitter_worker` — loaded fresh per poll (correct) but its
  `_bump_cursor` + `_save_state(state)` write could be **clobbered** by any
  other worker's full-dict write.

The race: IG's stale in-memory copy still held the **boot-time** `x.last_id`.
Every time IG saved, it wrote that old `x.last_id` back over X's advanced
cursor. Next X poll: everything newer than the boot-time cursor looked "new"
again → whole backlog re-relayed. X's cursor was correct until the *next* IG
save, then reverted.

**Log correlation that pins it:** every X re-relay wave immediately followed
an IG save. `04:22` IG processed new DMs → `04:27` X re-relayed. `04:28` IG
relayed (saved state) → `04:33` X re-relayed again. `04:34→05:56` IG was idle
(no `thread_activity` change → no save) → **X held its cursor and stayed
silent**. IG activity ~`06:00` → `06:02` X re-relayed everything. Textbook
reader-writer stampede on the shared file.

There was no locking anywhere (`_ig_api_lock` is unrelated; it only guards the
instagrapi API client).

## Why the previous fix (5c2bab5 "per-message cursor persist") didn't help

That commit fixed the *restart-mid-batch* case: the X worker used to save the
cursor once after the batch, so a crash mid-batch lost progress and the whole
batch re-relayed after restart. Saving per-message fixed that. It did **not**
address concurrent clobbering by the other two workers — which is why the bug
"returned" after looking fixed. Two distinct failure classes, same file.

## The fix: merge-only, per-platform, under a lock

`modules/direct_forward.py` (all paths in the same event loop):

- Added module-level `_STATE_LOCK = asyncio.Lock()`.
- Added `_merge_state_save(state, owned: set[str])` — **synchronous** (cannot
  be interleaved by other coroutines): re-reads the freshest on-disk state,
  applies **only the caller's `owned` platform sections**, writes back
  atomically, then refreshes the caller's in-memory dict in place with the
  merged result.
- Added `async def _state_save_owned(state, owned)` wrapping that under the
  lock for the async workers.
- Rewired **every** worker save site to `_state_save_owned(state, {own_platform})`
  (IG/X/TikTok priming, per-message cursor bumps, batch saves) and the two
  sync admin pairing helpers to `_merge_state_save`.
- IG worker now also `_load_state()`s **fresh at the top of every poll** so
  admin pairing / other-platform cursor changes land within one interval.

After the change the only remaining `_save_state` caller is inside
`_merge_state_save` itself. No full-dict write can ever clobber a sibling
platform's cursor again.

## Invariant for the future (now in AGENTS.md #13)

> `direct_forward_state.json` is SHARED by the IG, X and TikTok workers.
> Never call `_save_state(state)` (full-dict write) from a worker — it will
> clobber another platform's cursor and re-trigger duplicate relay of the
> backlog. Always persist through `_state_save_owned(state, {own_platform})`
> (async workers) or `_merge_state_save(state, {own_platform})` (sync admin
> helpers). Keep the helpers synchronous and keep all cursor bumps inside the
> per-platform owned write.

## Verification

- `python3 -m py_compile modules/direct_forward.py` passes; grep confirms no
  stray full-dict saves.
- Bot restarted (`systemctl restart tgbot`); clean startup log:
  `[DirectForward] started -> chat 7429671248, 3 platform(s)`, X worker
  polling, TikTok WS connected, IG session resumed.
- Monitoring: watch `logs/bot.log` for an X relay wave following an IG
  poll/relay with no new self-DM — should now never happen.

## Files touched

- `modules/direct_forward.py` — the fix.
- `AGENTS.md` — invariant #13 expanded with the shared-state rule.
- `docs/memory/tgbot-2026-08-11-x-duplicate-delivery-state-race.md` — this doc.

---

## 5. Source: `docs/memory/tgbot-2026-08-11-x-photo-paste-fix.md`

# X/Twitter direct-forward: photo-only pasted tweets now deliver (2026-08-11)

## Symptom

Pasting a **photo-only tweet URL** into the X self-DM produced a
`⚠️ No downloadable media — this tweet exposes no video stream to yt-dlp and the
photo fallback failed.` instead of the pictures. Recurring log lines:

```
[DirectForward/X] tweet <url> extract failed: Extraction failed:
    ERROR: [twitter] <id>: No video could be found in this tweet
[DirectForward/X] tweet <url> photo fallback fetch failed: 'urls'
[DirectForward/X] tweet <url> photo fallback fetch failed: 'pinned_tweet_ids_str'
[DirectForward/X] tweet <url>: no video and no photo fallback — relaying text-only
```

Affected tweet IDs observed in `logs/bot.log` (2026-08-11): `2087076300485783598`,
`2086847519795884349`, `2086785291470209496`, `2086863091942236642`,
`2086906780743852039`, `2086703591675535665`, `2086820144232206512`,
`2086753127668089269`.

## Root cause

The photo fallback (`_x_fallback_photos` in `modules/direct_forward/twitter.py`)
called `client.get_tweet_by_id(tweet_id)`, which builds twikit `Tweet`/`User`
**model objects**. twikit 2.3.3's `User.__init__` (`venv/.../twikit/user.py`)
reads `legacy['entities']['description']['urls']` and
`legacy['pinned_tweet_ids_str']` **without a `.get`**. Any author missing those
keys raises `KeyError('urls')` / `KeyError('pinned_tweet_ids_str')`, and the
exception aborts the **whole** `get_tweet_by_id` call before any data is
returned — so the fallback could never see the tweet's own media, and the
`t._data` walk found nothing → empty list → text-only relay.

Note the DM-*share* path was already fine (photo-only shares go native via
`_x_share_media` + `_x_deliver_share_photos`); only the **pasted text-URL** path
through `_x_deliver_tweet` → `_x_fallback_photos` was broken.

## Fix

Rewrote `_x_fallback_photos` (modules/direct_forward/twitter.py:237) to bypass
the broken model layer:

1. **Primary — raw GraphQL walk.** Calls `client.gql.tweet_detail(target_id,
   None)` directly (twikit `GQLClient`), which returns raw GraphQL JSON with
   **no model building**, so the `User.__init__` bug cannot fire. A
   `_focal_subtree` helper finds the subtree whose `entryId == 'tweet-<id>'`
   (mirroring twikit's own `get_tweet_by_id` matching), then a recursive walk
   collects dicts with `type == "photo"` and a `media_url_https`/`media_url`
   key (`_photo_from_media_dict` prefers `media_url_https`, never the generic
   `url` which matches t.co shortlinks). **Focal scoping is required**: the
   tweet_detail response also carries thread replies/quote tweets; a global
   walk over-collects photos that do NOT belong to the shared tweet.
2. **Secondary — old model path.** `get_tweet_by_id` → `t.media` walk → raw
   `t._data['legacy']['extended_entities'/'entities']['media']` walk, kept for
   tweets the raw walk misses (works for most authors).

Verified live: all 8 failing tweet IDs now resolve to their true photo counts
(1,1,2,1,1,1,1,2). Delivery itself uses the existing `_x_fetch_auth_bytes`
(throwaway `httpx.AsyncClient` + session cookie copy) and `_x_media_payload_ok`
(magic-bytes validation) then `_x_deliver_share_photos` (single `send_photo` or
grouped `send_media_group`).

## Deployment

No new dependencies, env vars, or systemd units. Restart the service:

```bash
sudo systemctl restart tgbot
```

## Related docs

- `docs/memory/tgbot-2026-08-11-selfdm-audit.md` — the earlier audit that
  first hit the `KeyError('urls')` failure mode (its fallback description is
  superseded by this doc's raw-GraphQL primary path).
- `docs/memory/tgbot-2026-08-08-x-selfdm-health-pass.md` — prior X self-DM
  health pass (DM-share + DM-photo paths).

---

## 6. Source: `docs/memory/tgbot-instagram-risky-and-push-2026-08-13.md`

# Instagram weekly risky_contactpoint + push vs polling — 2026-08-13

## Weekly `update_risky_contactpoint` (~7 days)

**User symptom:** `igcookies.txt` refreshed via Chrome incognito + cookie-editor, works ~1 week, then IG forces `https://www.instagram.com/accounts/update_risky_contactpoint/?challenge_context=...` (email change). This is NOT a `challenge_required` code entry — it's `SelectContactPointRecoveryForm` (Instagram deems the linked email/phone risky/reused/breached/disposable/unverified).

**Why it recurs despite fresh cookies:**
- Instagram scores *identity consistency*, not just cookie age. Chrome-incognito refresh resets `sessionid` but leaves other signals divergent, so trust decays over ~7 days (accumulated risk, not burst).
- Top signals per `docs/memory/tgbot-ig-anti-detection.md` and Tavily sources:
  1. **TLS/JA3 mismatch** — `instagrapi` default `requests` = Python OpenSSL JA3 under Android `Pixel 8 Pro / 428.0.0.47.67` UA. WAF flags instantly. tgbot already mitigates via `utils/ig_anti_detect.py:CurlCffiAdapter chrome136` + `_patch_private_retry`; verify `curl-adapter>=1.2.1` still installed.
  2. **DC ASN + geo mismatch** > rotation > polling volume. Jittered `300s±40%` + `delay_range [2,4]` + `last_activity_at` watermark (idle = 0 fetches) already minimal; volume is NOT the driver (7-day delay proves cadence tolerated).
  3. **Unverified / risky contact point** — no verified email + phone + TOTP = soft trigger for exactly this path. Disposable/shared email = instant risky.
  4. **Missing echo headers** — server sends `ig-set-ig-u-rur/shbid/shbts`, `x-ig-set-www-claim`, `ig-set-x-mid`; app must echo `IG-U-RUR/SHBID/WWW-Claim`. `instagrapi 2.18.12` drops `shbid/shbts` on `dump_settings`; tgbot patches `base_headers`/`get_settings`/`private_request` in `utils/ig_anti_detect.py`.
  5. **Session churn** — deleting `direct_ig_session.json` forces new device UUIDs. `utils/cookie_manager.py` snapshot+merge is correct; don't re-upload stale jar mid-session.

**Checklist (hardening, priority order):**
- Verify primary email (non-disposable, not breached) + verify phone + enable TOTP 2FA. This challenge cannot be passed by `challenge_code_handler`; human must pass in official app, then freeze 3-5h.
- One sticky residential/mobile proxy for life of account (`DIRECT_FORWARD_PROXY=socks5://...`, same city/ASN as owner). Never rotate. VPS IP (e.g. US/PA) must match account home geo (`IG_DIRECT_COUNTRY`, `COUNTRY_CODE`, `LOCALE`, `TZ`).
- Keep `curl-adapter>=1.2.1` + `chrome136`; watch logs for `[IG anti-detect] transport install degraded` after `instagrapi` bump.
- Keep echo capture (`IG-U-RUR/SHBID/SHBTS/WWW-Claim/X-MID`) and `pin_geo`.
- Never delete `direct_ig_session.json`; `dump_settings` per poll persists.
- Keep `300s±40%` (floor ≥60s), `delay_range [2,4]`, watermarks — do not lower below 300s.
- Warmup after resume (`account_info` + `direct_threads(5)`×3 paced 2-5s).
- Warn: `2026-08-13 04:32` logs show `[IG anti-detect] set_timezone_offset failed` + `_configure_private_session_retry` missing — indicates `instagrapi` update changed `PrivateRequestMixin` API; patch needs re-verify on next `instagrapi` bump (currently still geo-pinned).

## Push vs polling

**Question:** After fixing cookies you saw a push-notification popup. Since you send posts from another account to the dedicated bot account, you get a push each time — can we listen steady instead of polling?

**Instagram web push:**
- Instagram web + app are **MQTT-native** (`MQTToT` on `mqtt-mini.facebook.com` / `edge-mqtt.facebook.com`), not WebSocket. The `PushManager` ServiceWorker toast you saw is **encrypted/minimal** (no `pk`, no media URL) — even with a headless Chromium holding the ServiceWorker you'd still need MQTT/private-API to fetch the share.
- Reverse-engineered: `instagram_mqtt` / `instagrapi.realtime` wraps `MQTToT` topics `/ig_realtime_sub` (`realtimeSub`), `/ig_message_sync` (`message`/`iris`), `FBNS` (`fbpushnotif` `direct_v2_message`). Small-code path exists:
  ```py
  cl.realtime_on("message", handler)
  rt = cl.realtime_connect(); rt.direct_subscribe()
  while True: cl.realtime_read_once()  # emits parsed payloads
  ```
  No Playwright needed; pure Python `SocketMQTToTTransport` (~5 MB RAM). **Warning:** `RealtimeClient` is experimental, stateful (ping keepalive, `clientConfigUpdate` disconnects, half-open TCP stall); needs reconnect + backoff + shared `_state_save_owned` lock; only lightweight DM actions over MQTT.

**TikTok:** Already push-optimal — `wss://im-ws-sg.tiktok.com/ws/v2` `cmd 500 NEW_MSG_NOTIFY` protobuf, prime swallow, dedupe on `server_message_id`. History returns `200001`; push is the only reliable path. No change.

**X/Twitter self-DM:**
- No push for DMs. Official `Filtered Stream v2` is HTTP chunked, covers Posts only, not DMs. User Stream deprecated 2018. `Account Activity API v2` does push `direct_message_events` via webhook but requires Business/Enterprise $$$ + public HTTPS + CRC, with reported 10-90 min tails and no `XCHAT` E2EE coverage (needs `xchat_bridge.mjs` anyway).
- Private `twikit` streaming (`/dm_update/{id}` via `ct0`/`auth_token`) is undocumented, fragile, breaks on X deploy. `twikit` REST polling (`get_dm_conversation` 300s±40% + `xchat_bridge.mjs` Deno sidecar for XChat E2EE) is deterministic, ~0.003 req/s, KB RAM, no browser. `tweetstream.io` bridges filtered stream to WS but still no DMs.
- Headless browser (`Playwright` +300 MB RAM) would push this 1 GB VPS (961 MB + 2 GB swap, `MemoryMax=1500M`) into swap thrash (peak already 518 MB with `ffmpeg`+`Deno`).

**Recommendation for tgbot (1 vCPU / 1-2 GB):**
- **Keep jittered polling as production default** (`300s±40%`, floor 60s, watermarked, `CurlCffiAdapter`, echo). Proven, survived 2026-08-05 checkpoint.
- **Optional hybrid if sub-5s latency wanted:** Add `IG_DIRECT_MQTT_ENABLED` (default false). After `_ig_login`+`warmup`, spawn `realtime_connect` task alongside poll loop; on `message` callback → same `_ig_process_message` + `_state_save_owned`; poller stays as fallback heartbeat + cursor reconciler (300s). On `realtime_read_once` error / ping fail → disconnect, sleep 5-20s jitter, reconnect. Reuse `direct_ig_session.json` + `ig_proxy` + `CurlCffiAdapter`.
- **Do NOT add Playwright/Chromium**: +150-250 MB RAM + 300 MB download, same veto as `docs/memory/tgbot-tiktok-direct-dm.md:207`. Keep `MemoryMax`/`swap` as-is (AGENTS.md #1 V8 cage).

**Verification sketch (staging account, no secrets in repo):**
```bash
venv/bin/python -c "from instagrapi import Client; c=Client(); c.load_settings('direct_ig_session.json'); c.realtime_on('message', print); rt=c.realtime_connect(); rt.direct_subscribe(); import time; [c.realtime_read_once() for _ in range(10)]"
```

## What changed for this deployment

- `modules/downloader_handler.py`: free tier (Telegram only) now passes `https://www.instagram.com/reel/DVjNXkOkVxC/` — alt account `8022375512` hit `is_authorized` block after `gate_and_quota_check` passed; fixed to only enforce `is_authorized` when subscription mode OFF. Creator `7429671248` already passed; free alt now gets format keyboard.
- `modules/bale/runner.py`: removed early `@dp.message()` catch-all stealing all updates before `/start`; fixed `F.text.func` → `lambda m: m.text ...`; verified `tapi.bale.ai` base, `getUpdates` limit/offset/timeout, manual drain (Bale `deleteWebhook` NOOP), 20 MB real limit, LIMITED admin, NO Bale log channel, SSRF guard.
- Bale intentionally has **NO free tier** (per operator): Bale runner only checks `is_authorized` or `BALE_SYSTEM_CREATOR_ID`, never `subscription.store`.

---

## 7. Source: `docs/memory/vps-two-bots-runtime-state.md`

# VPS two-bots runtime state

The test VPS (SSH to `<vps-ip>` on port `<ssh-port>` as `<vps-user>`) hosts BOTH
bots side by side: **balebot** in `/home/<vps-user>/balebot` and **tgbot** in
`/home/<vps-user>/tgbot`. This is the VPS used for live testing.

Systemd state (as of 2026-07-18):

- `balebot.service` — **enabled**, auto-starts on boot. Its PO-token provider
  (Deno) listens on **127.0.0.1:4416**.
- `tgbot.service` — **enabled** (as of 2026-07-18), auto-starts on boot and
  auto-restarts on crash (`Restart=always`). Its PO provider listens on
  **127.0.0.1:4417**. `install.sh` installs the unit but does **not** enable it —
  enabling is a deliberate manual step (`sudo systemctl enable --now tgbot`) so a
  first-run can be watched interactively first.
- `cookie-watch.service` — **enabled**, auto-starts on boot. Runs
  `/home/<vps-user>/tgbot/tools/cookie_watch.sh` (inotifywait; `auditd` is inert
  on this host). Harmless monitor.

**Diagnostic gotcha:** `ps` shows a bare `python main.py` — on this host that is
**balebot**, not tgbot (both projects have a `main.py`). Distinguish them via
cgroup (`/proc/<pid>/cgroup` → `balebot.service` vs `tgbot.service`) or cwd
(`readlink /proc/<pid>/cwd` → `/home/<vps-user>/balebot` vs
`/home/<vps-user>/tgbot`). A healthy tgbot run shows: recent `bot.log` writes,
port 4417 listening, and "Started 5 HandlerTasks" + "Provider is healthy on
127.0.0.1:4417" in `/home/<vps-user>/tgbot/logs/bot.log`.

**"No response from tgbot" after a reboot** used to mean "tgbot wasn't running"
(the unit was disabled, so the bot was down — not crashed). Now that the unit is
enabled, check the service first:
`sudo systemctl status tgbot` / `sudo journalctl -u tgbot -f`. If for any reason
it must be started ad-hoc without systemd, stop the service first and run it
detached from the repo dir with `./run.sh`. See
[Cookie protection & monitor](tgbot-cookie-protection-and-monitor.md) and
[tgbot ↔ balebot integration](tgbot-balebot-integration.md).

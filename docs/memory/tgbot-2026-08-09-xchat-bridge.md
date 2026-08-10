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

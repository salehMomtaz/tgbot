# 4 GB uploads: Premium userbot + per-user admin whitelist

**Date:** 2026-08-05 · **Area:** `utils/uploader_handler.py`,
`utils/gate.py`, `modules/downloader_handler.py`, `modules/admin.py`,
`modules/direct_forward.py`, `config.py`

## The hard constraint (research findings)

Bots **cannot** upload more than 2 GB — this is not a "setting", it's enforced
server-side. Telegram's own tdlib/telegram-bot-api team confirmed it in
[tdlib/telegram-bot-api#583](https://github.com/tdlib/telegram-bot-api/issues/583):

> "Bot API server supports uploading of files of any size allowed by Telegram,
> but the user must be a Premium user to be able to upload files bigger than
> 2000 MB. **Bots can't be Premium users**, therefore they aren't allowed to
> upload files bigger than 2000 MB."

Options evaluated for the 4 GB path:

| Option | 4 GB capable? | Why |
|---|---|---|
| Bot API (public HTTP) | ❌ | Hard 2 GB server-side cap for bot tokens |
| Local Bot API server (self-hosted tdlib) | ❌ | Same bot-account cap — the userbot is a user, the bot is a bot |
| **Pyrogram user session (`PREMIUM_STRING_SESSION`)** | ✅ | MTProto **user** account; a Premium user can upload 4 GB |
| Telethon user session | ✅ | Identical MTProto limits, no advantage over pyrogram |
| Passing a `file_id` of an already-uploaded big file | ❌ | You'd still need a premium user to do the original upload |

**Conclusion: the only viable 4 GB path is a Premium *user* account over MTProto.**
The bot already had this wired (`premium_app` in `main.py`, built from
`PREMIUM_STRING_SESSION`); Telethon adds nothing over the existing pyrogram
client (same protocol, same limits, same session-string mechanics). A local Bot
API server also cannot lift the 2 GB bot ceiling. So no library swap — the work
was controlling *who* gets to use the premium path.

## The bug being fixed

Before this change the premium uploader client was used **globally**: any file
over 2 GB went through the Premium userbot for **every** user, as long as a
session was configured. There was no way to restrict 4 GB uploads to specific
users, and non-whitelisted users saw the full format list including impossible
>2 GB options.

## Implementation

- **`utils/gate.py`** — new `premium_users` list in `database.json` (auto-
  migrated for existing DBs) + `is_premium_user` / `add_premium_user` /
  `remove_premium_user`. `SYSTEM_CREATOR_ID` is implicitly premium.
- **`utils/uploader_handler.py`** — `process_split_and_upload` and
  `send_single_media` take an explicit `premium_allowed: bool | None = None`.
  `None` → inferred from `is_premium_user(chat_id)` (in a private chat
  `chat_id == user_id`). Both the split-size choice and the send client use the
  same flag, so the button size and the actual upload always agree.
- **`modules/downloader_handler.py`** — the `>2 GB` format guard at `dl:`
  dispatch now requires `premium_app` AND `is_premium_user(user_id)`.
  `build_format_keyboard` locks (🔒) >2 GB options for non-whitelisted users and
  routes them to a "Premium required" answer; the header notes the 🔒 meaning.
- **`modules/admin.py`** — new "👑 Premium Uploads" console button (badge shows
  whether a session is configured), menu with Add/Remove Premium by ID,
  mirroring the existing Add/Remove User state flow. The menu explicitly warns
  when `PREMIUM_STRING_SESSION` is empty (4 GB disabled).
- **`modules/direct_forward.py`** — the operator's own DM-relay pipeline passes
  `premium_allowed=True` explicitly: the relay chat (`DIRECT_FORWARD_CHAT_ID`)
  may differ from the creator's id, and the operator configured the userbot
  themselves, so relays are not gated on the whitelist.

## Decisions worth keeping

- **The creator is always premium** — they own the session; without this the
  operator could lock themselves out of 4 GB uploads.
- **Relay is always premium** — the relay chat is the operator's own pipeline;
  do not gate it on the interactive whitelist.
- **The whitelist is the whole point.** The 4 GB path must stay per-user; if a
  future change makes it global again, that's a regression (and the admin
  console becomes decorative).

## In-chat session-string generation (2026-08-05)

The terminal `generate_session.py` flow is gone; the admin generates the
`PREMIUM_STRING_SESSION` entirely from the 👑 Premium menu
(`🔑 Generate Session`):

- **`utils/premium_session.py`** — thin wrapper over a temporary **in-memory**
  pyrogram client (`Client(":memory:", api_id=…, api_hash=…)`) that runs the
  interactive login: `send_code` (→ `phone_code_hash`), `sign_in` (raises
  `SessionPasswordNeeded` when 2FA is on), `check_password`, then
  `export_session_string`. `save_session_string` persists it to `.env` via
  `dotenv.set_key` (dotenv-style quoting — exactly what `run.sh`'s parser and
  `config.py` expect) and refreshes `config.PREMIUM_STRING_SESSION` in memory.
  The temp client **never writes a session file** (":memory:") and is always
  disconnected via `discard_client`.
- **`modules/admin.py`** — `admin_premium_gen` starts the flow, then the three
  states `waiting_for_premium_phone` / `_code` / `_password` accept free-form
  text. They are dispatched **before** the `is_valid_telegram_id` gate (they are
  not user IDs). Every step carries a **❌ Abort Session Generation** button
  (`admin_premium_gen_abort`); a finished flow shows the string in a code block
  with **💾 Save to .env** (`admin_premium_gen_save`, writes via
  `save_session_string`) or **❌ Discard**.
- **Cleanup invariants** — the temp client is disconnected on: completion
  (before the result is shown), abort, `/start` escape, leaving to `admin_main`,
  reopening the premium menu, or TTL expiry. `PREMIUM_GEN[user_id]` holds
  `{client, phone, phone_code_hash, result, expires_at}` (15-min login TTL, 5-min
  result TTL). `sweep_stale_generations(client)` is a module-level background
  sweep driven by `utils.keyboard_expiry.expiry_loop` so a dangling temp login
  can never leak even if the admin walks away mid-flow.
- **Closure gotcha (fixed 2026-08-05):** `register_admin_handlers(app)` names
  its closure parameter `app`, NOT `client`. A first-pass implementation wrote
  `purge_active_prompt(user_id, client)` inside `_premium_gen_cleanup`, which
  threw `NameError: name 'client' is not defined` on every
  `admin_premium_gen` / `_abort` callback — the button looked completely dead
  (unanswered callback = stuck spinner). Always reference the client via the
  enclosing scope's real parameter (`app`). Message/callback handlers define
  their own `client` parameter, so `client` is only valid *inside* them.
- **Callback safety net:** `admin_callback_handler` is now a thin try/except
  wrapper over `_admin_callback_dispatch` — any dispatch error logs to the
  channel and answers the callback with an alert instead of hanging the
  spinner. Admin callback branches are a huge elif-chain; keep new branches
  inside the dispatch.
- **"🧹 Cleanup Stale Gen" is its own callback** (`admin_premium_gen_clean`).
  It originally shared `admin_premium_gen_abort`, so clicking it on the menu
  re-edited the PREMIUM menu into an "aborted" message (and repeated presses
  produced `MessageNotModified`). The menu button now re-renders the menu after
  sweeping; the abort callback is only for the in-flow Abort button.
- The generated string is **sensitive** (full account access) and is shown in
  the private chat; a restart is required after saving before the Premium
  userbot actually uses it.

## Dial-pad code entry — never type the login code in chat (2026-08-06)

The login code must be entered via a **numeric dial pad**, not as chat text.

- **Root cause:** Telegram's anti-account-sharing detection. Typing the code as
  a chat message caused a security notice and `PHONE_CODE_EXPIRED` seconds after
  `send_code`: *"The code was entered correctly, but sign in was not allowed,
  because this code was previously shared by your account."* The digits travel
  in **callback data**, never as chat text, so the detection is not triggered.
- **`_gen_dial_pad_markup`** — a 3×4 numeric keypad (rows 1-9, then
  `⌫ / 0 / ✓`, plus a full-width **❌ Abort** row). Callback data:
  `admin_premium_gen_digit:<d>`, `admin_premium_gen_bksp`, `admin_premium_gen_enter`.
- **`PREMIUM_GEN`** gains `"code_buffer": ""`. `_premium_gen_pad_text` re-renders
  the Step 2/3 message with an "Entered so far:" line; it tolerates
  `MessageNotModified`. The prompt id is re-registered in `ACTIVE_PROMPTS` so the
  dial pad stays alive while text flows.
- **`waiting_for_premium_code` text input now rejects typed codes** with an
  explanatory reply ("don't type the code, use the keypad") instead of accepting
  them — a typed code would burn the login. The 2FA step (`waiting_for_premium_password`)
  stays free-form text (a password is not a login code).
- **Callback semantics:** digits append (cap 6, `>4` required), backspace pops,
  enter validates ≥4 digits; on `SessionPasswordNeeded` it switches to the 2FA
  step, otherwise it verifies the code and proceeds to export. On any exception
  the buffer is reset and the pad re-rendered — the flow never dies from a bad
  entry. On success the dial-pad message is edited to "✅ Code accepted — logging
  in and exporting the session string…" before `_finish_premium_gen`.
- Verified working live on the VPS (2026-08-06); the flow generates and exports
  the session string entirely from the phone.

## Self-restart after saving — no shell access needed (2026-08-06)

`admin_premium_gen_save` previously printed `sudo systemctl restart tgbot`,
forcing an SSH+sudo session — the exact thing the in-chat flow existed to remove.

- **`main.py::schedule_self_restart(delay=3.0)`** — after the "saved" message is
  rendered, the bot restarts **itself**:
  - Under systemd (`INVOCATION_ID` is set for systemd services — verified on the
    VPS), it sends `SIGTERM` to its own PID. `main.py`'s existing `_on_sigterm`
    handler turns that into `KeyboardInterrupt`, which drives the same graceful
    teardown systemd uses on `systemctl restart`: pyrogram drains, the PO-token
    provider stops (`PotProviderManager.stop`), cookie locks are released. The
    process exits and `Restart=always` + `run.sh` relaunch it, re-reading `.env`
    — so the fresh `PREMIUM_STRING_SESSION` is picked up.
  - Without systemd (tmux/foreground dev), it falls back to `os.execv` in place.
- **Call sites:** `admin_premium_gen_save` (after `save_session_string`) and the
  admin console's **🔄 Restart Bot** button (see below). The 3 s delay lets the
  confirmation message and log line flush before teardown.
- Verified on the VPS: `kill -TERM <MainPID>` → `KeyboardInterrupt` graceful path
  → systemd relaunches with a new MainPID and restart counter +1. The dial pad +
  save flow itself was tested live by the operator.

## Admin console "Restart Bot" button (2026-08-06)

The main admin console now exposes the same self-restart to the operator, so the
bot can be rebooted entirely from chat — no SSH, no `systemctl`.

- **`build_console_keyboard`** gains a `🔄 Restart Bot` row paired with
  `❌ Close Console` (`admin_restart`).
- **`admin_restart`** renders a confirmation dialog ("Restart the bot? …Any
  running download will be interrupted and the queue cleared.") with
  `✅ Yes, restart now` (`admin_restart_confirm`) and `↩️ Cancel` (`admin_main`).
  It also pops any stale `USER_STATES`/`ACTIVE_PROMPTS` first.
- **`admin_restart_confirm`** edits the message to "🔄 Restarting the bot…",
  logs to the channel, answers the callback, then calls
  `schedule_self_restart(delay=3.0)` — the exact mechanism above.
- The dispatch branch lives inside `_admin_callback_dispatch` right after
  `admin_premium_gen_save`; it references only `callback_query`/`user_id`/
  `log_event`/`schedule_self_restart` (never `client` directly), so it is immune
  to the `app`-vs-`client` closure gotcha.
- Verified live on the VPS: the SIGTERM path produced `Stopping bot
  gracefully...` in the journal, systemd scheduled the restart, and the bot came
  back active with a fresh MainPID. The button itself (edit → confirm → restart)
  exercises that same code path.

## >2 GB delivery: stage-to-log-channel + bot relay (2026-08-06)

The first live 3.1 GB test failed on two independent bugs, then the delivery
path itself was redesigned.

### Root cause #1: `dl:` callbacks carried labels, not tokens

Format buttons were registered with `dl:<cache_id>:<emoji>`-style data, but the
`dl:` dispatcher switched on `:v:` / `:a:`. The emoji→`v`/`a` fix (`f609b38`)
made the video/audio path route correctly. This surfaced while re-testing the
premium flow.

### Root cause #2: `from main import premium_app` created a zombie client

`modules/downloader_handler.py` did `from main import premium_app` at import
time. Since `main.py` runs as `__main__`, this re-imported it as a *separate*
`main` module whose module-level `premium_app` was a second, **never-started**
pyrogram Client whose `.me` is `None`. Premium uploads died instantly
(`AUTH_KEY_UNREGISTERED`-class errors). Proof: `m.premium_app is ns['premium_app']`
→ `False`.

**Fix:** `register_downloader_handlers(app, premium_app)` now receives the
already-started instance from `main_engine()` (the import line is gone).
Verified: incremental uploads work after restart.

### The redesign: bots can't upload >2 GB, so the bot relays a copy

Bots are hard-capped at 2 GB; only a Premium *user* can push 4 GB. The old
design sent the big file **as the premium userbot** — correct upload, wrong
sender, and it bypassed the reply-to-link quoting.

New path in `utils/uploader_handler.py::_stage_and_relay` (used when
`use_premium and config.LOG_CHANNEL_ID`):

1. The premium userbot uploads the raw file to `LOG_CHANNEL_ID` with a
   "📦 Staged for delivery" caption. The operator is the channel admin, so the
   file is visible there and doubles as a permanent record.
2. The bot calls `copy_message(chat_id, from_chat_id=LOG_CHANNEL_ID,
   message_id=staged.id, caption=<user caption>)`. `copy_message` forwards by
   `file_id` — **no size limit**, the file already lives on Telegram's CDN. The
   sender shows as the bot, and `reply_to_message_id` makes it quote-reply to the
   user's link (invariant #14).
3. If staging throws for any reason, `send_single_media` logs a warning and
   falls back to the **direct premium send** so the file still reaches the user.

The staged message deliberately stays in the log channel ("Keep in log channel"
choice). Verified end-to-end with a 3.1 GB video (`C6Q2ZjyKxa0`, 3277923411 B):
staged as log msg 14894, delivered as bot msg 88093 replying to 88086 (the
user's link), sender 7665239058 (the bot).

### Test driver (tools/)

`tools/telethon_login.py` (one-time operator login → `telethon_session.txt`,
git-ignored) + `tools/telethon_drive.py` (send a link/message, press inline
buttons by substring, pick v/a from the live keyboard, assert size ranges;
handles both `NewMessage` and `MessageEdited` since tier keyboards arrive as
edits). All flows below were exercised with it on the production box:

- single video (`aqz-KE-bpKQ` 480p → 59.2 MB, msg 88103, reply-to link),
- audio (`a:258` → 30.7 MB m4a),
- playlist tiers (`pl:*:whole` → `pl:*:vl` → 2 videos),
- direct-file download (README.md doc),
- format-menu Cancel (dismisses, no job),
- the whole admin console (List Users, Blacklist, PO Token, Direct-Forward,
  Cookie Jars, Premium menu, Doc Mode toggle, Abort Transfer, Close, and a live
  Restart-Bot cycle: self-SIGTERM → systemd relaunch → bot responds, `NRestarts=1`,
  no crash loop).

## Inline-keyboard auto-expiration (2026-08-05)

`utils/keyboard_expiry.py` strips unused inline keyboards so chat history does
not accumulate dead buttons:

- Registry keyed by `(chat_id, message_id)` — message ids are **only unique per
  chat**, so a single-`message_id` key would let two users' keyboards collide
  (both chats frequently land on the same small message id).
- `watch(chat_id, message_id)` is called by `main.py`'s send/edit monkeypatches
  whenever a message carries a `reply_markup`; `touch(chat_id, message_id)`
  resets the 20-min deadline from the group `-2` callback interceptor (every
  button press keeps the keyboard alive); `expiry_loop` runs every 30 s,
  strips up to 50 expired `reply_markup`s per tick, and also drives
  `sweep_stale_generations` above.

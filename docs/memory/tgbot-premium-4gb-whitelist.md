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

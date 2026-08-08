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

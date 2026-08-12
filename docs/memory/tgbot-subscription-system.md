# Subscription system (2026-08-12/13) — toggleable, Stars/TON, free-tier channel gate, WebApp, priority queue

Operator asked for a full subscription redesign: `whitelist/blacklist` stays as before, but a **toggleable** subscription layer sits on top. Those who deployed the bot without wanting subscriptions keep the old block-if-not-whitelisted behaviour (intruder → blacklist; admin copies numeric id from blacklist and whitelists — auto-removing the blacklist entry). When the toggle is ON, the bot shows subscription/channel prompts instead of blacklisting strangers.

This doc captures the design + gotchas so the next change doesn't re-break it.

## 1) Toggle + datastore

- Store lives **inside `database.json`** (no new file): `sub_settings`, `subscriptions`, `usage`, `sub_used_tx`.
- `utils/subscription/store.py` is the only writer (thread-lock + `utils/gate` save path). `config.py` seeds defaults from `.env` (`SUB_ENABLED`, `SUB_FREE_ENABLED`, `SUB_CHANNEL_*`, `SUB_TON_*`) but DB is the source of truth at runtime.
- `DEFAULT_SUB_SETTINGS`: `enabled`, `free_enabled`, `channel_id`, `channel_username`, `channels[]`. Legacy single fields migrate into `channels` on first load (so old installs don't lose their channel). `channels` is a list `[{id,int, username,str}]` — multi-channel force-join (see §3). Admin console always writes `channels`; legacy fields are kept in sync (first entry).
- Three paid tiers (`utils/subscription/tiers.py`): `basic 100/d 100⭐`, `plus 500/d 250⭐`, `pro 2500/d 500⭐` (30 d each). `free` is 5/d, priority 0. TON equivalents (`price_ton`) map 1:2 ratio for fee math. `priority`: free 0, basic 1, plus 2, pro 3; creator is implicit pro (until `9999999999`).
- `set_subscription()` extends from existing expiry if active, **auto-removes from blacklist** and **adds to `authorized`** — the whitelist/blacklist invariant: *subscribing (or whitelisting) never leaves a blacklist entry*. `utils/gate.add_user()` already did `remove blacklist`; this mirrors it.
- Creator is always active (no quota). Every other `is_subscription_active()` is `until > now`.

## 2) Security gate — the blacklist vs. subscription tension

Historical gate (`modules/admin/register.py` group -1) did: `if not authorized → blacklist + drop`. That kills subscriptions: a free/paid user who isn't in `authorized` would be blacklisted on first message and never see a prompt.

Fix: **subscription-aware gate** (see `register.py:security_gate`).

- If `sub_settings.enabled == false` → legacy: same as before.
- If enabled: only `blacklisted` is dropped. For everyone else:
  - `is_subscription_active` → pass.
  - `free_enabled` → pass (channel check is deferred to the downloader gate / Group 1 welcome, not here — we can't `get_chat_member` without `client` in the gate without blocking).
  - `is_authorized` → pass.
  - Otherwise → **do NOT blacklist**; let the message continue to Group 1 so `check_access()` / `gate_and_quota_check()` can render a proper `🔒`/channel-join UI instead of a silent drop.
  - Non-link chatter (hi/start) now shows a subscription prompt via `admin_start_text_handler` (it calls `check_access` again and shows tier keyboard or missing-channels list). Links go via `gate_and_quota_check` in `downloader_handler`.

This preserves the old admin-friendly “friend messages, I copy id from blacklist” workflow when the toggle is OFF, and avoids spamming blacklist when it's ON.

## 3) Free tier: two modes + multi-channel membership

Spec: free tier either **no channel** (just 5/d) or **must join channel(s)**; bot must list which channels the user hasn't joined.

- Single-channel legacy: `SUB_CHANNEL_ID`/`SUB_CHANNEL_USERNAME` (resolved via `get_chat` to id for `get_chat_member`). Now `channels[]` holds N entries.
- Membership check (`utils/subscription/access.py:check_all_channels`): iterates `get_channels()`, calls `is_channel_member(client, user_id, channel_id)` (or resolves username→id if only username stored). Returns `(all_joined, missing_list)`.
- `check_access()` → `need_channel` when any missing. Downloader gate and the Group 1 welcome both call `check_all_channels` to enumerate missing channels, build per-channel `t.me/<handle>` join buttons, and show a prompt like “Free access requires joining: @ch1, @ch2 — join all then retry, or /subscription”.
- Admin console: Subscription menu now shows `Channels: a,b` and has **➕ Add channel / ➖ Remove channel** (states `waiting_for_sub_channel_add|remove`). `waiting_for_sub_channel` (legacy name) is kept as alias to Add. `0`/`clear` clears all. New helper `add_channel`/`remove_channel` in store dedupes and keeps legacy single fields in sync.

## 4) Quota + priority

- `utils/subscription/quota.py`: `_daily_limit_for()` reads tier or free limit; `check_quota()` / `remaining_quota()` / `increment_quota()`.
- Bug fixed: `increment_quota` previously allocated a **new** `threading.Lock()` per call (no mutual exclusion). Now uses module-level `_QUOTA_LOCK`. Pruning previously had nested loop deleting `0..len-8` inside `for d in keys` (deleted repeatedly); now keeps at most 7 per user sorted.
- `utils/queue_manager.py:DownloadQueue.add_task()` resolves `priority` from subscription tier (free 0 < basic 1 < plus 2 < pro 3, FIFO within same priority, higher jumps ahead). Creator / tier `pro` goes first. If subscription mode off, default priority 1 (legacy). Checked via `is_subscription_active` at enqueue time; actual download also re-checks quota at execution (queue may have waited past midnight).
- `downloader_handler` calls `gate_and_quota_check` before any download, and `check_quota` + `increment_quota` after successful upload/playlist item. Playlist job per-video checks quota so hitting limit mid-playlist aborts remaining items with a clear message.

## 5) Payments: Stars (XTR) + TON/Gram

- **Stars** (`utils/subscription/payments_stars.py`): raw Bot API `sendInvoice` with `currency=XTR`, `provider_token=""`, `payload=sub:<user_id>:<tier>:<HMAC16>`. HMAC key is `BOT_TOKEN`; sig window current + previous hour. `create_stars_invoice()` is called from `/subscription` → tier button → stars. Pre-checkout (`UpdateBotPrecheckoutQuery`) is handled via `app.on_raw_update` (pyrogram has no high-level filter) — verifies payload, user match, answers `ok`. `successful_payment` handler verifies amount ≥ price, activates tier for `duration_days`, logs.
- **TON** (`payments_ton.py`): inbound-only verification via `toncenter.com/api/v2/json/getTransactions` for `SUB_TON_ADDRESS`. User sends `price_ton` to address with **memo = user_id** (digits). `verify_ton_payment(user_id, tier)` scans last 50 tx, checks `value >= price`, memo matches, tx hash not in `sub_used_tx` (dedup), then `set_subscription(..., granted_by="ton")` and marks tx used. Called via `/subscription` → TON button → instruction card (address + memo) → “I sent it — Verify” → runs in executor (sync urllib). `X-API-Key` via `SUB_TON_API_KEY` optional. No private key needed.
- Both flows share `tiers.py` amounts; `handlers.py:_tiers_keyboard` shows stars rows always and TON rows only when `TON_ADDRESS` set.

## 6) WebApp / Mini App (admin + user portal) — https://tgbot.southpark.ir:8080 (direct TLS)

Spec asked for an enhanced admin console as Telegram Mini App (https://core.telegram.org/bots/webapps), admin-only if possible, otherwise a user status portal.

- **Direct TLS (no nginx)**: wildcard `*.southpark.ir` cert at `/etc/letsencrypt/live/southpark.ir/{fullchain,privkey}.pem` (valid `tgbot.southpark.ir` via `*.southpark.ir`), copied to `certs/fullchain.pem`+`privkey.pem` by `install.sh` + renewal hook `/etc/letsencrypt/renewal-hooks/deploy/tgbot-copy.sh` (auto `chown dev:dev`, `chmod 600` + `systemctl try-restart tgbot`). `DOMAIN=https://tgbot.southpark.ir:8080`, `SSL_CERT_PATH`/`SSL_KEY_PATH` point to `certs/` (so `uvicorn` terminates TLS on `:8080` directly; `avistel.ir` wildcard was previous, now `southpark.ir` because `*.avistel.ir → 94.159.109.54` not this VPS `38.45.80.233`). `deploy/tgbot.southpark.ir.conf` kept as nginx reference, not enabled.
- **FastAPI mount** (`modules/subscription/webapp.py:mount`): mounts on the existing port-8080 app (already shared with `stream_handler`+`pot_provider`), now also `GET /` landing with Telegram auto-redirect, `GET /api/botinfo` (`getMe` cached 1h). The three HTML pages share `_SHARED_UI` (safe-area `safeAreaInset` + `env(safe-area-inset)`, sticky header `padding-right:56px` to avoid TG close button, `viewport-fit=cover`, `backdrop-blur`, `color-scheme:dark`, toast-stack + modal + `UI.showPopup` native fallback + `HapticFeedback`).
  - `GET /` → `HTML_ROOT` landing: outside Telegram shows Welcome + plans (`/api/tiers`) + `https://t.me/<bot>` + `/app`/`/admin` links; inside Telegram (`tg.initData` present) auto-fetches `/api/user/status` and redirects creator → `/admin/subscription`, others → `/app`.
  - `GET /admin/subscription` → `HTML_ADMIN` panel: toggle enabled/free, multi-channel textarea, tier table, active subs list, token box (`/admin_token`). Auth: `X-Admin-Token == HMAC(BOT_TOKEN, "admin-sub")[0:16]` (`/admin_token` command) OR Telegram `initData` where `user.id == SYSTEM_CREATOR_ID` (HMAC-SHA256 via `WebAppData` key, per docs). GET `/admin/subscription/api` now **requires admin auth** (previously open); returns settings/tiers/active. Errors now show bounded `⛔` card (`border-color:rgba(255,69,58,.35)`) + native `tg.showPopup` fallback to custom modal + toast, with 3-step how-to, not raw `{"detail":…}` string. Header no longer shows `tgbot.southpark.ir:8080` on top-right; subtitle only.
  - `GET /app` → `HTML_USER` portal: any Telegram user via WebApp (valid `initData` required). Shows tier badge, until, quota `remaining/limit`, used today, 7-day history, upgrade cards. “Buy” routes to bot (`/subscription`) + `UI.toast`. Outside browser shows friendly `🔒 Telegram auth required` card with public tier table, not raw JSON.
  - `GET /api/tiers` (public), `GET /api/user/status` (valid initData, any user) → subscription/quota/history/settings. User status does **not** leak admin-only data.
  - `GET /api/botinfo` (public, cached `getMe`) → `username` for landing `https://t.me/<bot>` link.
  - `POST /admin/subscription/api` admin write with full channel list sanitization; logs via `log_event`.
- Telegram side: Bot exposes WebApp via `MenuButton` URL `https://tgbot.southpark.ir:8080/` (root auto-redirects) or inline `web_app={"url":"https://tgbot.southpark.ir:8080/app"}` for users and `.../admin/subscription` for admin; both work with direct TLS on `:8080` (wildcard `*.southpark.ir`), no nginx.

## 7) Instagram fallback credentials

Operator supplied `IG_DIRECT_USERNAME=salehmomtaz03`, `IG_DIRECT_PASSWORD`, `IG_DIRECT_TOTP_SEED=6OX5IDHO2CL67FAC3XB5G5MXRGSOYYY7` (base32 2FA). These are stored **only in `.env`** (git-ignored); `.env.example` shows placeholders. `config.py` already wired `IG_DIRECT_USERNAME/PASSWORD/TOTP_SEED`; the IG worker (`modules/direct_forward/instagram.py`) uses them as fallback when `igcookies.txt` jar is stale (after `instagrapi` fails to validate session). No file is committed. (Memory file redacts the password.)

## 8) Security hardening applied

- `utils/security.py`: per-user flood tracker `is_flood(user_id, 60s, limit)` tier-aware (free 5/min, basic 8, plus 12, pro 20, creator 30) applied in `downloader_handler.text_link_handler` before any download; `is_safe_url`, `redact_token` (bot-token regex `[REDACTED_TOKEN]` in logs), webapp IP rate limit helper.
- SSRF guard already existed (`_is_ssrf_target` in downloader_handler) — kept.
- Log redaction: `utils/logger.py:TelegramChannelHandler.emit` now calls `redact_token` before escaping/sending (so token never hits log channel even if a traceback includes it).
- WebApp initData validates `hash` via HMAC-SHA256 (`WebAppData` → `BOT_TOKEN`) and optional `auth_date` freshness; admin write requires creator id.
- `install.sh` keeps `ulimit -v` absent, wildcard-only PO provider, etc. — invariants untouched.
- `admin_token` command gives short-lived HMAC token for non-Telegram WebApp access.

## 9) Invariants preserved

- Whitelist add removes blacklist (both `gate.add_user` and `store.set_subscription`).
- Subscription toggle off = legacy blacklist behaviour, no prompt.
- Multi-channel set via admin console uses `add_channel`/`remove_channel` (merge-only), not a blind overwrite, so concurrent direct-forward state race fixes stay intact.
- Queue priority never starves free indefinitely — free still runs, just last.
- Cookie/PO invariants untouched; WebApp + nginx are extra routes on same 8080 port, no extra process.

See also: `config.py` (SUB_*), `docs/UBUNTU_VPS_SETUP.md` (§ subscription), `blueprint.md` Phase 21, `deploy/tgbot.southpark.ir.conf` (reference, direct TLS is primary), `modules/subscription/webapp.py` (`_SHARED_UI`, `HTML_ROOT`/`HTML_ADMIN`/`HTML_USER`).


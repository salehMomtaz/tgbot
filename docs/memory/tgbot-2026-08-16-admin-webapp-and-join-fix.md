# Full admin console as a Telegram Mini App + channel force-join fix (2026-08-16)

## Part A — channel force-join bug: `is_channel_member` never matched (fixed)

### Symptom

With subscription mode ON and the free tier requiring a channel join, a user
who had **actually joined** the force-join channel still got "not verified"
from the "✅ I joined — verify" (`chkjoin:`) button and from the download gate.
The operator's test user `8022375512` was confirmed a real member of `@mclib`
(`ChannelParticipant`), and the bot was confirmed an admin
(`ChannelParticipantAdmin`) — yet access was denied.

### Root cause

`utils/subscription/access.py::is_channel_member` compared

```python
s = str(status).lower()
return s in ("member", "administrator", "creator", "owner", "restricted")
```

pyrogram 2.x returns a `ChatMemberStatus` **enum**, whose `str()` is
`"ChatMemberStatus.MEMBER"` — `.lower()` is `"chatmemberstatus.member"` and
never equals `"member"`. So every `get_chat_member` call "succeeded" but the
membership test always returned `False`. Verified live:

```python
str(ChatMemberStatus.MEMBER)   # 'ChatMemberStatus.MEMBER'
ChatMemberStatus.MEMBER.value  # 'member'
```

### Fix

Compare against the enum's `.value` (`"member"`, `"administrator"`, ...) with a
`str()` fallback, and also accept `"restricted"` (slow-mode-limited users are
still members):

```python
s = (getattr(status, "value", None) or str(status)) if status else ""
return s.lower() in ("member", "administrator", "creator", "owner", "restricted")
```

Verified end-to-end with a live pyrogram client against the real channel:
`is_channel_member(client, 8022375512, -1001443485983) == True`.

## Part B — Full admin console as a Telegram Mini App (`/admin`)

The entire in-chat admin console (`modules/admin/*`, inline-keyboard driven)
now has a full SPA mirror served by the same FastAPI process.

### What was built

- **`utils/webapp_auth.py`** — shared Mini App auth (extracted out of the
  subscription webapp): `admin_token()` = `HMAC(BOT_TOKEN,"admin-sub")[:16]`
  (never stored), `verify_init_data()` per Telegram's official spec,
  `is_admin_auth()`/`require_admin()` (403 on failure). Both webapps now import
  from this one module.
- **`modules/admin_webapp/actions.py`** — transport-free server-side core.
  Deliberately reuses the same storage/utility code the in-chat console uses
  (`utils.gate`, `utils.subscription.store`, `modules.admin.cookies` /
  `_write_cookie_jar`, `modules.admin.cookie_test::_run_cookie_test_sync`,
  `utils.premium_session`, `modules.direct_forward.*`, `utils.shared.queue`)
  so the two UIs can never drift. Long probes (cookie test / PO diagnose / X
  test) run through `run_in_executor`.
- **`modules/admin_webapp/api.py`** — 46 FastAPI routes under `/admin/api`,
  every one gated by `require_admin`. Notably: `/premium/gen` uses its OWN
  `WEB_PREMIUM_GEN` dict (keyed `str(uid)`, own TTL) — never the in-chat
  `modules/admin/state.py::PREMIUM_GEN` — so the two generation flows can't
  clobber each other.
- **`modules/admin_webapp/ui.py`** — one-page SPA (dark, `safeAreaInset`
  aware, Telegram theme-aware, native `showPopup` fallback modal/toast). Tabs:
  Overview, Users, Cookies (download/replace/test/backup/restore + per-site
  jars), PO Token (start/stop/diagnose/test), Premium (whitelist + full
  session-generation flow phone→code→2FA→save with in-page inputs),
  Subscriptions (toggle/free/channels/grant/revoke/list), Direct
  (IG/X/TikTok toggles, IG pairing code, X Chat PIN, tests), System (abort
  queue, restart). Download endpoints are fetched as blobs (auth header can't
  ride `window.location`).
- **`modules/admin_webapp/__init__.py::mount(fastapi_app)`** — includes the
  router, serves the SPA at `/admin`, plus a public `/admin/api/health`.
- **`main.py`** — mounts `modules.admin_webapp` right after the subscription
  webapp (both in the same try/except so a webapp fault never kills the bot).
- **`modules/admin/keyboards.py`** — the console keyboard now carries a
  **🌐 WebApp Console** button (`web_app=WebAppInfo(url=...)`) opening
  `https://tgbot.southpark.ir:8080/admin`.
- **`modules/subscription/webapp.py`** — the `/` landing now redirects the
  creator to `/admin` (was `/admin/subscription`); the "🛠 Admin" link in the
  subscription UI now points at `/admin`. The legacy `/admin/subscription`
  routes still work.

### Auth model

- Telegram Mini App open → `tg.initData` header; `verify_init_data` only
  admits `user.id == SYSTEM_CREATOR_ID`.
- Browser / outside Telegram → `X-Admin-Token` header; token printed by the
  admin `/admin_token` command (already shipped earlier), stored in
  `localStorage`.
- Everything else → 403.

### Verification (all live)

- `python3 -m py_compile` on all touched files; JS extracted from `ui.py`
  passes `node --check`.
- FastAPI `TestClient`: health 200, no-auth/bad-token `/admin/api/state` 403,
  valid token + crafted initData 200.
- Live HTTPS: `/admin` serves the SPA (200), `/admin/api/health` 200,
  `/admin/api/cookies/ytcookies/download` 200 (real jar bytes), unauth
  `/admin/api/state` 403.
- Telethon drive: console keyboard shows the 🌐 WebApp Console button; `chkjoin`
  logic verified against the real channel (above).
- Bot restarted cleanly via systemd (NRestarts=0), `[POT] Provider is healthy`
  in log.

### Gotchas worth remembering

- `python-multipart` had to be installed in the venv for the `UploadFile`/
  `Form` cookie-upload endpoints — the subscription webapp never used
  multipart, so it wasn't a dependency before.
- `pot_state()` originally called two undefined helpers (`pot_running()` /
  `pot_available()`) — fixed to `_pot_running()` from `pot_menu` +
  `shared.POT_AVAILABLE`. Run a `TestClient` smoke pass before deploying any
  new `/admin/api` route.
- `from .cookies import _write_cookie_jar` inside `actions.py` was a wrong
  relative import (no `modules/admin_webapp/cookies.py` exists) — it must be
  `from modules.admin.cookies import _write_cookie_jar`.
- `config.DOMAIN` is the source of truth for the webapp base URL, with an
  HTTPS fallback so the button always opens a Telegram-valid HTTPS webapp.
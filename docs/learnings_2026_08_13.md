# Project Learnings — 2026-08-13

## Summary
This document captures the key technical learnings from the 2026-08-13 session:
**(1)** two pyrogram dispatch-propagation bugs and the shared helper that fixes
them, **(2)** the logger refactor (monolith → `utils/logger/` package, Rich
`32768` truncation limit, strict Telegram/Bale log split), and **(3)** the
channel force-join verification UI now embedded in the `/start` greeting.

---

## 1. Dispatch-Propagation Bugs & `utils/propagation.py`

### Background
pyrogram dispatches handlers in **ordered groups**:
`-2` log interceptors → `-1` security gate → `0` state machine + extras
(GitHub/YouTube/Translate/Web) → `1` text router (downloader, greeting) →
`2` callback dispatcher. Control flow uses `message.stop_propagation()`,
`message.continue_propagation()`, or `raise ContinuePropagation`.

### Bug A — bare `except Exception` swallows `StopPropagation`
- **Symptom**: pasting a `github.com/...` link produced **duplicate replies**
  (the GitHub panel AND a direct-file upload attempt).
- **Root cause**: `StopPropagation` and `ContinuePropagation` are **`Exception`
  subclasses**, so `try: message.stop_propagation() except Exception: pass`
  silently swallowed the signal. The group-0 GitHub handler returned normally →
  the dispatcher `break`ed the group and control flowed to the *next* group,
  where the group-1 downloader grabbed the same link as a direct-file upload.
- **Fixes**: (a) the group-0 extras now use the shared helpers; (b) the
  group-1 greeting guard (`admin_start_text_handler`) uses `stop(message)` so
  `/subscription` no longer "double-greets".

### Bug B — `RawUpdateHandler` mid-group starves later handlers
- **Symptom**: after porting the balebot extras, `/tr`, `/yt`, `/search`,
  github links and `/web` **silently ignored all input**.
- **Root cause**: a `@app.on_raw_update(...)` handler (Stars `pre_checkout`)
  was registered **in the middle of group 0**. pyrogram's dispatcher treats any
  `RawUpdateHandler` as matching *every* update; when its callback returns
  normally the dispatcher `break`s the whole group, so every handler registered
  AFTER it in that group never saw an update.
- **Fix**: the raw handler now `raise pyrogram.ContinuePropagation` when it
  does not own the update (`modules/subscription/handlers.py::_raw_precheckout`),
  so the group iterator keeps going.

### The shared helper (the correct pattern)
```python
# utils/propagation.py
def stop(message):   # replaces message.stop_propagation()
def continue_(message):  # replaces message.continue_propagation()
```
Both re-raise the real propagation signal while still swallowing
genuinely-unexpected (non-propagation) errors. Every handler in
`modules/{admin,subscription,github,youtube,translate,web}/` should use them.
Never wrap `stop_propagation()`/`continue_propagation()` in a bare
`except Exception`/`except: pass`.

**Deploy a new `on_raw_update` in a shared group with care** — prefer an
isolated group or a raise-on-mismatch, or every later handler in that group
starves. Full write-up: `docs/memory/tgbot-2026-08-13-propagation-dispatche-bugs.md`.

---

## 2. Logger Refactor — `utils/logger/` package + Rich 32768

### The split
`utils/logger.py` (monolith) → `utils/logger/` **package**:
- `local.py` — `ensure_local_log_handler` (`logs/bot.log`, 5 MB × 3).
- `telegram.py` — `TelegramChannelHandler` → `LOG_CHANNEL_ID`.
- `bale.py` — `BaleChannelHandler` → `BALE_LOG_CHANNEL_ID`.
- `__init__.py` — backwards-compat re-exports (`from utils.logger import ...`
  still works), so call sites did not change.

### Truncation limit: 32768, not 3500
The Rich Bot API endpoint (`sendRichMessage`) accepts **32768** chars; the
earlier 3500/6000/8000 cuts were premature and **broke detailed log lines**
(a full admin-console dump of 17,003 chars was chopped mid-escape). Both
handlers now truncate at 31500 + a `[TRUNCATED at 32768 rich limit]` marker.
Keep the `sendMessage` fallback byte-compatible with the pre-rich format.

### Strict log split (`d723798`)
- The main Telegram log channel (`LOG_CHANNEL_ID`) gets **pyrogram /
  direct-forward / queue** lines.
- The `bale_log` channel gets **ONLY Bale/aiogram** lines — both at the same
  INFO level. This lets the Bale frontend be monitored without drowning the
  Telegram channel in duplicate Bale noise. Don't merge the streams.

### `bale_log` is a TELEGRAM channel (important correction)
`BaleChannelHandler` sends to `https://api.telegram.org` with the **same
`BOT_TOKEN`** — NOT to `tapi.bale.ai`. Reason: Bale is government-owned, so
Bale-side logs containing sensitive info must never cross into `tapi.bale.ai`
(security hole). They land in a separate private **Telegram** channel named
`bale_log`. Both handlers are Telegram-API; they are kept separate only so the
two log streams stay isolated. Earlier docs that said "bale_log on Bale via
`tapi.bale.ai`" were wrong and were corrected in README / USER_GUIDE /
UBUNTU_VPS_SETUP.

---

## 3. Channel Force-Join Verification in the Greeting

### What changed
`modules/subscription/join.py` (new) provides:
- `_greeting_text(user_id)` — the intro guide text.
- `build_greeting_keyboard(user_id)` — access prompt + **"✅ I joined — verify"**
  button when subscription mode is on and the user must join channels.
- `register_join_handlers(app)` — wires the `chkjoin:` callback that
  re-checks channel membership live.

`modules/admin/register.py` now sends **one self-contained greeting** that
always carries the normal intro guide + subscription access prompt + keyboard
in a single message (previously the subscription prompt was a second message).
`main.py` registers the join handlers at startup.

### Access gate
`utils/subscription/access.py`:
- `is_subscription_enabled()`, `is_free_allowed()`, `is_channel_member()`.
- `check_all_channels(user_id)` → `(all_joined, missing_list)` via
  `get_chat_member` status in `{member, administrator, creator, owner}`.

---

## 4. Deployment & Operational Notes

- **PO port**: config default is `YTDLP_POT_PORT=4416`; the production VPS
  overrides to `4417` in `.env`. Docs default to 4416 unless describing a
  specific deployment.
- **Playwright provisioning gap**: the sequential cookie refresher
  (`utils/cookie_refresher.py`, Phase 26) needs `playwright` + a Chromium
  browser, but neither was in `requirements.txt` nor provisioned by
  `install.sh` (it was installed manually on this VPS). See the
  requirements.txt / install.sh changes in this session.
- **Cookie refresher invariants** (Phase 26): one Chromium at a time
  (~300 MB peak — safe on 4 GB+8swap), 24 h ± 1 h cadence, atomic Netscape
  write `0o444`, clears `direct_ig_session.json` for IG, proxy-aware.
- **Service lifecycle**: `tgbot.service` is installed but NOT enabled by
  install.sh (must `sudo systemctl enable --now tgbot` after first success);
  `tgbot-xchat-bridge.service` IS enabled (resident supervisor);
  `tgbot-monitor.service` installed but disabled.
- **Scripts must stay executable** (`100755`): `run.sh`/`install.sh`/
  `uninstall.sh` — systemd calls `run.sh` directly; a lost exec bit crash-loops
  with `status=203/EXEC`.

---

## 5. Action Items

### Documentation (this session)
- [x] `docs/learnings_2026_08_13.md` — this document.
- [x] `AGENTS.md` — logger package path, `bale_log` truth, file-map rows for
  `utils/logger/` + `modules/subscription/join.py`.
- [x] `blueprint.md` — directory map (logger package, `propagation.py`,
  `join.py`), Phase 28 progress entry.
- [x] `README.md` — last-verified banner, Logs section (32768, `bale_log` is
  a Telegram channel), channel-join verification feature.
- [x] `docs/USER_GUIDE.md` — bale_log correction, truncation 32768,
  channel-join verify in the greeting, subscription table update.
- [x] `docs/UBUNTU_VPS_SETUP.md` — bale_log channel truth.
- [x] `docs/memory/README.md` — index updated + date bumped.

### Future
- [ ] Add remaining missing memory-doc entries to `docs/memory/README.md`
  (e.g. `tgbot-tiktok-direct-dm.md`, `tgbot-premium-4gb-whitelist.md`).

---

## 6. Files Modified / Created (2026-08-13 session)

| File | Change |
|------|--------|
| `utils/logger/` package | Split from `utils/logger.py` (local/telegram/bale + re-exports) |
| `utils/propagation.py` | `stop()`/`continue_()` helpers for dispatch control flow |
| `modules/subscription/join.py` | **New**: channel force-join verification UI + greeting text/keyboard |
| `main.py` | Registers join handlers at startup |
| `modules/admin/register.py` | Single self-contained greeting with access prompt + verify button |
| `AGENTS.md` | Logger + subscription file-map rows, `bale_log` truth |
| `blueprint.md` | Directory map + Phase 28 progress entry |
| `README.md` | Last-verified banner, Logs section, features |
| `docs/USER_GUIDE.md` | Logs + subscriptions + greeting updates |
| `docs/UBUNTU_VPS_SETUP.md` | bale_log channel truth |
| `docs/memory/README.md` | Index + date |
| `docs/learnings_2026_08_13.md` | **Created**: this document |

---

*Generated during the tgbot dispatch-propagation hardening + logger package
split + channel-join verification session.*

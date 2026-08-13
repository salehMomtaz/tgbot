# Bale.ai frontend hardening — 2026-08-13

## Context (why harden)

Bale.ai is owned by Iranian government infrastructure. Operator reports `tapi.bale.ai` traffic is untrusted and could be used to probe the VPS. The Telegram bot (`pyrogram` on `t.me`) is the primary, trusted frontend. Bale is an **optional, LIMITED mirror** that shares expensive core (PO provider, queue, yt-dlp, cookies) but must not leak secrets or allow VPS infiltration.

User requested:
- **No Bale log channel** (Telegram `LOG_CHANNEL_ID` stays private; Bale must not ship logs to a Bale channel)
- **Bale admin console extremely limited** — cookies, premium session, POT provider, direct-forward, subscriptions must NOT be exposed on Bale. File partitioning limits can stay.
- **Bale file limit reality is 20 MB** (docs claims 50 MB, but `General` API caps document/Video at 20 MB; Bale's own `image` 10 MB, `video` MPEG4-only; our measured upload fails at ~20 MB). Telegram keeps 2 GB/4 GB.

## Design adopted (one process, two frontends)

`docs/memory/tgbot-balebot-integration.md` proposed **one `tgbot.service` process hosting both pollers**:
- `main_engine()` starts Telegram `pyrogram.Client` *always*, and if `BALE_TOKEN` is set, spawns `modules/bale/runner.py::start_bale_bot()` as an isolated `asyncio` task (broad `try/except`, Bale crash never kills Telegram).
- Both frontends share: `DownloadQueue` (one worker), `POT_PROVIDER` (one Deno on 127.0.0.1), `cookie_manager` snapshots + write-back, `supported_sites.is_ytdlp_supported`, SSRF guard, flood guard. No second PO port.
- Bale is **inert by default**: `BALE_TOKEN` empty → zero Bale code runs, Telegram behavior byte-for-byte identical. Add `BALE_TOKEN` + `BALE_SYSTEM_CREATOR_ID` to activate.

## What was built

### Config (`config.py:240`)
```py
BALE_TOKEN = os.getenv("BALE_TOKEN","")
BALE_SYSTEM_CREATOR_ID = get_env_int("BALE_SYSTEM_CREATOR_ID",0)
BALE_HARD_LIMIT_MB = get_env_int("BALE_HARD_LIMIT_MB",20)  # real, not 50
BALE_SPLIT_TARGET_MB = get_env_int("BALE_SPLIT_TARGET_MB",19)
BALE_DIRECT_DOWNLOAD = os.getenv("BALE_DIRECT_DOWNLOAD","true") ...
```
`.env.example` documents each, notes government ownership, no Bale log channel, 20 MB truth.

`requirements.txt` adds `aiogram==3.12.0` (Bale transport). Telegram still uses `pyrogram`.

### Bale transport (`modules/bale/runner.py`)
- Aiogram `Bot(token, session=AiohttpSession(api=TelegramAPIServer.from_base("https://tapi.bale.ai")))` — HTTP only, token only, no MTProto.
- Startup: drain Bale `getUpdates` backlog manually (`deleteWebhook` is NOOP on Bale per balebot's `main.py` comment — we loop `get_updates(offset=last+1)` until empty). Prevents replay spam on every restart.
- `Bale API limits respected`: captions sanitized via `clean_caption_text` (strip `* _ ` [ ] ( )`, 150 chars), filenames sanitized to ≤40 chars (`sanitize_filename_for_bale`), MPEG4 only for `sendVideo` (others fallback to `sendDocument`), markdown auto-parse quirks handled.

### Bale uploader (`modules/bale/uploader.py`)
Ported from `balebot/operators/uploader.py` but pointed at `config.BALE_TOKEN` and 20 MB ceiling:
- `upload_file_direct_to_bale()` → raw `aiohttp.FormData` POST to `https://tapi.bale.ai/bot<token>/sendDocument|sendVideo|sendAudio` with proxy support.
- `process_split_and_upload_bale()` → `split_video_by_size_generator(file, 19 MB, 20 MB)` for media, `split_file_generator` for documents, sequential one-part-at-a-time (VPS disk cap = one chunk), deletes part immediately.
- Telegram's `process_split_and_upload` (1900/3900 MB target) is **not reused** for Bale — Bale's 20 MB path is separate.

### Bale admin console (`modules/bale/admin.py` + `runner.py` handlers)
**Limited set only** (see `get_bale_console_keyboard`):
- `👥 List / ➕ Add / ➖ Remove / 🚫 Blacklist / 🔓 Unban` (user management)
- `📄 Doc Mode` toggle
- `⚙️ Size Limits: 19/20MB` (`waiting_for_setlimit` → `bale_hard_limit_mb`, `bale_split_target_mb`, `binary_chunk_mb`, `max_cache_age_hours`)
- `💥 Abort Transfer`
- `❌ Close`

**Explicitly NOT exposed on Bale** (even if Telegram admin crafts `callback_data`):
```
bale_admin_cookie* , bale_admin_pot* , bale_admin_premium* , bale_admin_direct* , bale_admin_sub*
→ answered "❌ Not available on Bale (secrets hidden). Use Telegram admin console."
```
Also: no `Cookie Jars`, no `PO Token`, no `Premium Uploads`, no `Direct-Forward`, no `Subscriptions`. File split knobs are the *only* tuning exposed on Bale, per operator request ("bale file limit and other things you think are necessary there can stay").

State stores are isolated: `BALE_USER_STATES` / `BALE_ACTIVE_PROMPTS` separate from Telegram's `USER_STATES`, per integration doc namespacing guidance (avoid `bl:123` colliding with `tg:123` in shared stores).

### Security posture (Bale side)

| Concern | Mitigation in Bale runner |
|---|---|
| **Log exfiltration** | **NO `BaleChannelHandler`** — `setup_system_logger` only attaches `TelegramChannelHandler` + local `logs/bot.log`. Bale never ships logs to a Bale channel (government could read). Python `logging` for Bale goes to same Telegram log channel + local file, not to Bale. |
| **Admin impersonation** | `_is_bale_admin(uid) = uid == BALE_SYSTEM_CREATOR_ID` (separate var from `SYSTEM_CREATOR_ID` = Telegram creator 7429671248). Bale token's admin is 1058935006 per user-provided `BALE_SYSTEM_CREATOR_ID`. All `bale_admin_*` callbacks check this first (`Access Denied` otherwise). |
| **Secret exposure** | Limited keyboard + explicit block on secret callbacks (above). Even a crafted `admin_cookie_select:ytcookies` via Bale is rejected. Cookies live under `cookies/` on disk, never sent to Bale chat (Telegram admin's `📤 Download` is disabled on Bale). |
| **Request flood / probe** | Tight rate limit: `_rate_ok(uid, window=60, limit=4)` on Bale (Telegram free tier 5/min). Exceed → `⏳ Too fast`. No auto-blacklist storm on Bale (government probing could deliberately trigger blacklist of legitimate users). |
| **SSRF** | Reused `_is_ssrf_target(url)` from Telegram's `downloader_handler`: refuses `loopback/private/link-local/multicast/reserved` — protects `127.0.0.1:4417` PO provider and internal services even if Bale user sends `http://127.0.0.1/...`. Same `is_safe_url` (http/https + ≤2048 chars) guard. |
| **Input injection** | All free-form Bale admin inputs (`waiting_for_setlimit`, `waiting_for_add_user` etc) are validated via `is_valid_telegram_id` or key allow-list (`bale_hard_limit_mb` etc) before any `setattr(config, ...)` or `shared.set_setting`. `setlimit` only allows known keys, positive ints. No arbitrary code. |
| **File limit lie** | Bale docs claim 50 MB, real is 20 MB (`upload_file_direct_to_bale` would 400+ beyond 20). Buttons show `🔒 (>20MB)` and format selector locks >20 MB. Split at 19/20. Don't widen to 50. |
| **Isolation** | Bale poller wrapped as `_bale_wrapper()` with `try/except logging.exception` — Bale crash is caught, logged to Telegram log channel, and does not propagate to `asyncio.gather(*tasks)` so Telegram poller + PO health loop + cache cleaner keep running. SIGTERM also drains both. |
| **Payload size** | Bale direct-download timeout 600s (vs Telegram 1800) to avoid long-held connections from untrusted side; still uses 512 KB chunks. |
| **No premium path** | Bale has no Premium concept — no `is_premium_user` check, no 4 GB staging via log channel. All Bale uploads are via direct multipart, never via Telegram log channel. |

### File map

| Want to... | Edit |
|---|---|
| Bale credentials / limits | `config.py` (`BALE_TOKEN`, `BALE_SYSTEM_CREATOR_ID`, `BALE_HARD_LIMIT_MB`) + `.env.example` |
| Bale upload / sanitizers | `modules/bale/uploader.py` (`sanitize_filename_for_bale`, `clean_caption_text`, `upload_file_direct_to_bale`) |
| Bale limited admin | `modules/bale/admin.py` (`get_bale_console_keyboard`, `BALE_USER_STATES`, `_is_bale_admin`) |
| Bale poller + download callbacks | `modules/bale/runner.py` (`create_bale_dispatcher`, `start_bale_bot`, `is_link`, `_rate_ok`, `is_ytdlp_supported`, `process_split_and_upload_bale`) |
| Wire Bale into boot | `main.py` → isolated `_bale_wrapper()` task gated on `BALE_TOKEN` |

### Verification

```bash
python3 -m py_compile $(git ls-files '*.py')  # clean
bash -n install.sh run.sh uninstall.sh
venv/bin/pip install -q -r requirements.txt  # pulls aiogram 3.12.0
BALE_TOKEN set → systemctl restart tgbot → journalctl: "[Bale] Polling started (tapi.bale.ai), admin LIMITED, no log channel"
BALE_TOKEN empty → "[Bale] BALE_TOKEN empty — Bale frontend disabled"
# Functional (on Bale @angelabalzacbot):
#  /start as Bale admin (1058935006) → LIMITED console (no 🍪, no 👑, no 🔐, no 📨)
#  /start as non-admin Bale user → welcome with 20 MB note
#  Paste https://www.youtube.com/watch?v=... → format keyboard (locked >20 MB) → tap → delivers via Bale split 19/20 (caption stripped of markdown)
#  Try crafted callback "bale_admin_cookie_select:ytcookies" → "Not available on Bale"
#  Paste http://127.0.0.1:4417/ping → "Refusing private network address" (SSRF guard)
```

### Open follow-ups (not blocking)

- Consider adding Bale-side `/tr` / `/yt` / `/search` extras behind same limited security gate if operator wants parity with Telegram extras on Bale (currently only download works on Bale; Telegram extras stay Telegram-only per request).
- If Bale ID space collision with Telegram becomes observable, namespace `authorized`/`blacklisted` with `tg:`/`bl:` prefix as per integration doc §6.4.

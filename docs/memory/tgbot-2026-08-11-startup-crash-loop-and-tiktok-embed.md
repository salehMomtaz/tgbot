# Startup crash-loop + undefined-name bug sweep + TikTok embed workaround

**Date:** 2026-08-11 · **Area:** `modules/admin/*`, `modules/direct_forward/*`,
`utils/downloader/*`, `main.py`

## Part 1 — the crash-loop (why the bot was silently dead)

### Symptom

The bot was down even though systemd said `active (running)`. `journalctl -u
tgbot` showed a fresh traceback every ~8 s, each ending in:

```
NameError: name 'CallbackQuery' is not defined
  File "modules/admin/register.py", line 428, in admin_callback_handler
```

`bot.log` mirrored it: the startup banner `[Logger] Standalone Telegram Logging
Service linked to Root Logger.` appeared again and again every ~8 s — a dead
giveaway that the process was crash-looping at **startup**, never reaching the
update loop. The handler signature `async def admin_callback_handler(client,
callback_query: CallbackQuery)` annotated the parameter with a type that was
never imported, so the `def` itself failed at module load.

### Fix

`from pyrogram.types import Message, InlineKeyboardMarkup,
InlineKeyboardButton` → added `CallbackQuery`. Verified with `python3 -m
py_compile` and by restarting the unit. (The crash-loop also meant every admin
console button press timed out silently — this single missing import had
disabled the whole admin UI.)

## Part 2 — the undefined-name sweep (found with pyflakes)

While the bot was dead we ran a full pyflakes scan over every module. It found
**8 more latent `NameError`s** — none crashed the bot yet, but all would fire
the first time their code path ran (a callback press, a cookie test, a TikTok
relay). All are fixed and import-verified:

| File | Bug | Fix |
|---|---|---|
| `modules/admin/register.py` | `add_premium_user` / `remove_premium_user` used but never imported | added to the `from utils.gate import (...)` block |
| `modules/admin/register.py` | `blacklist_user` used in `security_gate` but not imported | added to the same block |
| `modules/admin/callback_dispatch.py` | `get_direct_menu_keyboard` undefined | imported from `.keyboards` (defined at `keyboards.py:74`) |
| `modules/admin/callback_dispatch.py` | `direct_forward` undefined at module level (was only imported locally in two handlers) | added module-level `from modules import direct_forward` |
| `modules/admin/cookie_test.py` | `log_event` undefined | `from main import log_event` |
| `modules/admin/premium_gen.py` | `log_event` undefined | `from main import log_event` |
| `modules/direct_forward/instagram.py` | `random` undefined | added `import random` |
| `modules/direct_forward/instagram.py` | `_cursor` / `_bump_cursor` undefined | added to the `from .state import (...)` block (`state.py:57` / `state.py:64`) |
| `modules/direct_forward/tiktok.py` | `_tt_poll_interval` undefined | added to the `from .common import (...)` block (`common.py:45`) |
| `utils/downloader/formats.py` | `os` referenced at line 26 before the local `import os` inside the function body — a **runtime** NameError on every `extract_formats` call | moved to a top-level `import os` |
| `utils/downloader/formats.py` | `_apply_pot_options` undefined | added to the `from .url_normalize import ...` line |
| `utils/downloader/cookies.py` | `_apply_pot_options` undefined | added `from .url_normalize import _apply_pot_options` |

### Lessons

- **`from main import log_event` is safe and is the established pattern** —
  `main.py` defines it at module level and `main_engine()` imports `modules.*`
  lazily *inside the function*, so there is no circular import risk.
- **Signature annotations that reference unimported names kill the module at
  import time**, not at call time. A type-hint on a handler parameter is
  executed when the `def` runs. This is why the crash-loop started at startup.
- **A local `import os` inside a function does not protect an earlier use of
  `os` at module scope.** Keep stdlib imports at the top of the file.
- The **venv import smoke test** (`source venv/bin/activate && python -c
  "import <module>..."` on every edited module) catches both circular-import
  and undefined-name-at-import problems that `py_compile` cannot. Use it after
  any import-related change.

## Part 3 — TikTok embed workaround (yt-dlp#17403)

### What changed on TikTok's side

yt-dlp's `www.tiktok.com/@user/video/<id>` webpage fetch now frequently hits
the **anti-bot challenge page** instead of the rehydration JSON. Symptom in
logs:

```
ERROR: [TikTok] ...: Unexpected response from webpage request
```

This is tracked upstream as **yt-dlp issue #17403** — it's a site change, not
a bug in our code, so it is deliberately **not** "fixed" by us beyond a
workaround. The oEmbed endpoint still serves metadata without the challenge.

### Workaround (mirrors the direct-forward path's earlier commit `e48b060`)

1. **`_to_tiktok_embed_url`** in `utils/downloader/url_normalize.py` rewrites
   `https://www.tiktok.com/@user/video/<id>` →
   `https://www.tiktok.com/embed/<id>`. The embed page serves the challenge-free
   JSON. `normalize_url` now chains `_resolve_tiktok_short_url` → `_to_tiktok_embed_url`.
2. **`_apply_pot_options`** sets `opts["http_headers"] = {"User-Agent":
   _TIKTOK_EMBED_UA}` for `tiktok.com/embed/` URLs (Chrome 140 UA; the embed
   page needs it). YouTube's PO-token branch is unchanged, other sites pass
   through untouched.
3. **`download_media`** in `utils/downloader/download.py` skips cookies for
   TikTok embed URLs (`use_cookies_now = bool(site_jar) and not is_instagram
   and not is_tiktok_embed`) — the embed path needs no session.

### Gotchas / don'ts

- The embed workaround lives at the **`normalize_url` layer**, so it applies
  to both `extract_formats` and `download_media` automatically. Don't add a
  second copy in the download pipeline.
- Do **not** treat the `Unexpected response from webpage request` error as a
  bot bug while the challenge is live — check `git log` for the upstream
  fix. The embed rewrite is our hedge, not a guarantee; if TikTok hardens the
  embed page too, wait for the yt-dlp fix rather than piling on more layers.
- Keep the Chrome UA tied to the embed URL only. A blanket TikTok UA change
  is not needed and would burn the browser-like fingerprint on the main site.

## Part 4 — other log findings (already expected, no code change)

- **Instagram 400s** (`[Instagram] <code>: Video info extraction failed: HTTP
  Error 400`): stale/expired session on the cookies path; IG extraction stays
  no-auth-first (AGENTS.md invariant #3). Cookie write-back (invariant #4)
  keeps the jar warm; re-upload via Admin → Cookies when it recurs.
- **X direct-forward photo fallbacks** (`No video could be found in this tweet`
  → `'urls'` / `'withheld_in_countries'` KeyErrors inside twikit's `User`
  parsing): already handled by the `_x_fallback_photos` walk + text-only
  note fallback. Tweeting at X (280 chars) makes the `'urls'` key optional —
  don't rely on twikit's strict parsing.
- **Dailymotion `Access forbidden`**: site-side, intermittent.
- **`0.0.0.0:8080` FastAPI is internet-exposed**; uvicorn access logs show
  routine scanner noise (`/.env`, `/.aws/credentials`, `/actuator/env`, …) all
  404. Harmless, but the health/status endpoints stay unauthenticated by
  design — do not add secrets to them.

## Verification

```bash
python3 -m py_compile $(git ls-files '*.py')        # all OK
bash -n install.sh run.sh uninstall.sh             # all OK
cd cmd/tgbot-monitor && go test ./...              # ok
source venv/bin/activate && python -c "import utils.downloader.formats; import utils.downloader.cookies; import modules.admin.callback_dispatch; import modules.direct_forward.instagram; import modules.direct_forward.tiktok; import modules.admin.premium_gen; import modules.admin.cookie_test; import modules.admin.register"
sudo systemctl restart tgbot                        # stable, no crash-loop
```

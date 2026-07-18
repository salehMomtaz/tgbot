# AGENTS.md — notes for contributors & AI agents working on tgbot

This is a pyrogram-based Telegram media downloader/streamer. Its sibling project
**balebot** (aiogram v3, Bale.ai) is the reference implementation many patterns
were ported from. This file captures the **non-obvious invariants** so you don't
have to rediscover them.

## Orientation

- **Framework:** pyrogram (NOT aiogram). Handlers are registered on a `Client`
  with ordered groups (`group=-2` log interceptors, `-1` security gate, `0` state
  machine, `1` text router, `2` callback dispatcher). `message.stop_propagation()`
  / `continue_propagation()` / `raise ContinuePropagation` control flow.
- **Config:** `config.py` reads everything from `.env` via `python-dotenv` at
  import time (`load_dotenv()` is called at the top of `config.py` so it's
  self-contained regardless of which module imports first). Never hardcode
  secrets. `.env.example` is the template; `.env` is git-ignored.
- **Entry point:** `main.py` → `main_engine()`. It wires logger → cookie init+lock
  → disk guard → pyrogram clients → PO provider → FastAPI/uvicorn → background
  tasks (updater, cache cleaner, PO health loop). SIGTERM is translated to
  `KeyboardInterrupt` for graceful teardown.

## File map (what to edit for what)

| Want to… | Edit |
|---|---|
| Add/change a config value | `config.py` + `.env.example` (+ blueprint) |
| Change download/extract logic | `utils/downloader.py` |
| Change upload/splitting | `utils/uploader_handler.py` |
| Change the PO-token provider lifecycle | `utils/pot_provider.py` |
| Add an admin-console feature | `modules/admin.py` (+ keyboard helpers there) |
| Change how links/messages are handled | `modules/downloader_handler.py` |
| Change streaming | `modules/stream_handler.py` / `stream_interceptor.py` |
| Change install/provisioning | `install.sh` / `run.sh` / `deploy/tgbot.service` |

## Critical invariants (do not break these)

1. **No `ulimit -v`.** Anywhere. On 64-bit Linux `-v` caps *virtual address
   space*, not RAM. V8 reserves a ~4 GB pointer-compression "cage" at startup; a
   4 GB `-v` cap aborts it with "Fatal OOM: CagedHeap" before Deno boots. RAM
   protection = swap (`install.sh`) + `MemoryMax=` (`systemd`), never `-v`. This
   is already correct in `run.sh` and `deploy/tgbot.service` — keep it that way.

2. **The PO provider must bind 127.0.0.1 only.** `utils/pot_provider.py::
   _patch_localhost` rewrites the upstream `host: "::"` / `0.0.0.0` to
   `127.0.0.1` (idempotent via the `// TGBOT_LOCALHOST_PATCH` marker). On a public
   VPS the PO endpoint must never be internet-reachable. If you bump the provider
   ref and the markers stop matching, the manager logs a loud warning — verify it.

3. **YouTube = cookies + PO token, no fallback.** `utils/downloader.py::
   _apply_pot_options` raises `RuntimeError` when the provider is down for a
   YouTube URL. Do not add a cookies-only/no-auth fallback for YouTube. Other
   sites keep the strategy ladder (cookies → no-auth).

4. **Cookie jars are read-only at rest.** `main.py::initialize_cookie_jars` locks
   `ytcookies.txt` to `0o444`. yt-dlp rewrites jars on exit; we prevent that and
   hand each download a *snapshot* from `get_cookies_for_url`. When you need to
   write a jar (admin replace/restore), unlock to `0o644`, write, re-lock to
   `0o444`, then call `_purge_cookie_snapshots`. See `modules/admin.py::
   _write_cookie_jar`.

5. **Keep `[default]` on yt-dlp upgrades.** `utils/updater.py` runs
   `pip install -U --pre "yt-dlp[default]"` — plain `yt-dlp` would silently strip
   the certifi/curllib extras. The `--pre` channel is what keeps it on nightly.

6. **`.env` parsing in `run.sh` must stay dotenv-style**, never `source .env`.
   Values like `YTDLP_USER_AGENT` contain characters bash treats as code. The
   line-by-line reader in `run.sh` is intentional. systemd has **no**
   `EnvironmentFile=` for the same reason — `run.sh` owns `.env` parsing.

7. **systemd unit is a template.** `deploy/tgbot.service` has `__USER__`,
   `__GROUP__`, `__PROJECT_DIR__`, `__MEMORY_MAX__` placeholders rendered by
   `install.sh` from the real user/path/RAM. Don't hardcode paths in the unit.

## Running / testing

There's no test suite. Verify changes with:

```bash
# Python syntax for every module
python3 -m py_compile $(git ls-files '*.py')

# Bash syntax for the scripts
bash -n install.sh run.sh uninstall.sh

# Local smoke (needs a valid .env) — NOT on a bot that's already polling Telegram
source venv/bin/activate
python main.py
```

Never run two bot instances against the same `BOT_TOKEN` at once — Telegram errors
with "terminated by other getUpdates". Stop tmux/systemd before the other starts.

### Service lifecycle (systemd) — the "it died after reboot" trap

`install.sh` renders and installs `tgbot.service` but deliberately does **not**
enable it. The unit must be enabled by hand once the bot is confirmed working:

```bash
sudo systemctl enable --now tgbot   # start now + auto-start on every boot
```

Until that runs, the bot only stays up for the lifetime of the tmux/`run.sh`
session that launched it — so **after a reboot the bot is simply down (not
crashed)**. Symptom: "tgbot stopped working / no response" right after a reboot,
with a `bot.log` that ends cleanly (no traceback). Fix: enable the unit (above),
or if you're mid-debug, check `sudo systemctl status tgbot` before assuming a
code bug. `Restart=always` means an enabled service also self-heals on crash.

`cookie-watch.service` (the inotifywait tamper monitor) is a separate, harmless
enabled unit — leave it running.

## Logging

Root logger gets two handlers (`main.py::setup_system_logger`): the
`TelegramChannelHandler` (→ `LOG_CHANNEL_ID`) and a local rotating file mirror
(`logs/bot.log`, 5 MB × 3). Both only attach when `LOG_CHANNEL_ID != 0`; the file
mirror is added regardless inside `ensure_local_log_handler`. New code should use
`logging.getLogger(__name__)` / `await log_event(...)`, not `print`.

## Gotchas

- **pyrogram `Peer id invalid`** is monkey-patched in `main.py`
  (`get_peer_type_patched`) and the log channel peer is resolved at startup
  (`app.get_chat`). Don't remove either.
- **Send-method logging** is monkey-patched in `main.py::patch_pyrogram_send_methods`
  and explicitly skips the log channel to avoid a self-logging loop.
- **Upload ceilings** (`utils/uploader_handler.py`): Bot API 2 GB / Premium 4 GB,
  with a *target* below the limit and a *hard* ceiling because the ffmpeg
  keyframe splitter can overshoot. Don't tighten target past the hard ceiling.
- **`RUNTIME_SETTINGS`** in `utils/shared.py` only holds `max_cache_age_hours` and
  `max_disk_usage_pct` — housekeeping knobs, NOT upload-size knobs (the 2 GB / 4 GB
  boundary is picked per-file in the uploader). Do not add Bale's `bale_hard_limit_mb`
  etc.; they don't apply to Telegram.

## When porting from balebot

balebot is the more mature reference. When porting a feature:
- aiogram `Router`/`F` filters → pyrogram ordered groups + `filters.*`.
- `bot.send_message(chat_id=, text=)` → `client.send_message(chat_id=, text=)` or
  `callback_query.message.edit_text(...)` / `message.reply_text(...)`.
- `callback_query.answer(text, show_alert=True)` works the same in pyrogram.
- Blocking yt-dlp work → `asyncio.get_event_loop().run_in_executor(None, fn)`
  (pyrogram has no `bot.loop`; use the event loop directly).
- Do **not** port the Bale uploader (direct multipart for a 20 MB limit) —
  pyrogram's native `send_video`/`send_audio`/`send_document` handle Telegram's
  2 GB / 4 GB natively. Only port the splitter logic and disk guards.
- Do **not** add GitHub-explorer or translate features — tgbot doesn't have them.

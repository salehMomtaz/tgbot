# Blueprint: Private Media Downloader & Streamer Telegram Bot

A pyrogram-based Telegram bot that downloads media from YouTube, Instagram,
TikTok, X/Twitter, and every other site supported by **yt-dlp nightly**, uploads
the result to Telegram (splitting across the 2 GB / 4 GB ceiling as needed), and
can hand out direct streaming links for files you forward to it.

This blueprint is the architectural source of truth. The step-by-step
*how-to-run-it* guide lives in [`docs/UBUNTU_VPS_SETUP.md`](docs/UBUNTU_VPS_SETUP.md);
the user-facing overview is [`README.md`](README.md).

---

## 🎯 Design principles (ported from the sibling Bale project)

1. **No Docker reliance.** Docker is still *supported* (legacy `Dockerfile` /
   `docker-compose.yml`) but is no longer the supported path. The supported path
   is a bare-metal install via `./install.sh` + `./run.sh` or a `systemd` unit —
   exactly like the Bale bot. One language stack, one process tree, one log
   stream, no container overhead on a 1 GB VPS.
2. **YouTube = cookies + PO token, no fallback.** YouTube now requires a
   proof-of-origin (PO) token. The bot runs a local `bgutil-ytdlp-pot-provider`
   Deno server that mints them; every YouTube extraction uses cookies + PO token.
   Other sites use cookies with a graceful no-auth fallback.
3. **Cookies are precious and protected.** yt-dlp rewrites cookie jars on exit, so
   each download gets a *disposable snapshot*; the live YouTube jar is locked
   read-only and backed up. See [Cookie protection model](#-cookie-protection-model).
4. **Fail loudly, never silently.** Site-aware error classification turns opaque
   yt-dlp exceptions into actionable messages. A down PO-token provider degrades
   YouTube only — the rest of the bot keeps working.
5. **Bounded resource use.** A 2 GB swap file, a disk-usage guard, and a
   systemd `MemoryMax=` cgroup keep the bot from locking out SSH on a small VPS.

---

## 📂 System layout

- **Runtime:** Ubuntu 24.04, Python 3 venv, FFmpeg, and the **Deno ≥ 2.0** runtime
  for the PO-token provider. No Node.js / npm / tsc anywhere.
- **Double-client MTProto engine:** a standard Bot API client for the console,
  links, and streaming, alongside an optional Userbot client (driven by a Premium
  session string) that lifts the per-file upload ceiling from 2 GB → 4 GB.
- **PO-token provider:** a localhost-only Deno HTTP server (`127.0.0.1:4416`)
  supervised by `utils/pot_provider.py`; the yt-dlp plugin
  (`bgutil-ytdlp-pot-provider`, pip-installed) talks to it automatically.
- **Web stream server:** a FastAPI bridge on port 8080 that pipes forwarded
  Telegram files to a browser on the fly with a 24-hour token — no local buffering.
- **Reverse-proxy ready:** optional Nginx routing over HTTPS via the `/tgbot/`
  path, or native HTTPS on the FastAPI port when `SSL_CERT_PATH` / `SSL_KEY_PATH`
  are set.

---

## 🔐 The PO-token provider (the YouTube solution)

YouTube rejects extractions that lack a proof-of-origin token. The bot solves this
with the open-source **bgutil-ytdlp-pot-provider**:

- `install.sh` clones the provider source at a pinned git ref
  (`bgutil-provider/`) and runs `deno install --allow-scripts` to build its native
  `canvas` FFI (used to solve the browser fingerprint challenge).
- At startup, `utils/pot_provider.py` (`PotProviderManager`):
  1. verifies Deno ≥ 2.0,
  2. patches `server/src/main.ts` so the server binds **`127.0.0.1` only**
     (upstream binds the wildcard `::` / `0.0.0.0` — on a public VPS we never want
     the PO endpoint reachable from the internet),
  3. launches `deno run … src/main.ts --port 4416` and waits for `/ping`,
  4. supervises it with a health-check + backoff restart loop.
- The yt-dlp plugin that actually requests tokens is **pip-installed**
  (`bgutil-ytdlp-pot-provider` in `requirements.txt`) and auto-discovered by
  yt-dlp — no symlink hack.
- `utils/downloader.py::_apply_pot_options` injects `player_client=mweb` + the
  plugin's PO-token options into every YouTube extraction. For YouTube it **raises
  `RuntimeError`** when the provider is down (no silent fallback).

State is mirrored into `utils.shared.POT_AVAILABLE` / `pot_manager_instance`, and
the **Admin Console → PO Token** menu can start/stop/restart/diagnose it live.

---

## 🍪 Cookie protection model

yt-dlp rewrites cookie jars on exit. If two downloads share one jar, or yt-dlp
crashes mid-write, the jar is corrupted. The bot therefore:

- **Locks the live YouTube jar read-only** (`chmod 444`) at startup
  (`main.py::initialize_cookie_jars`). yt-dlp can read it but never overwrite it.
- **Hands each download a disposable snapshot** copied into `cache/cookies/`
  (`utils/downloader.py::get_cookies_for_url`). Snapshots are purged on a timer
  and whenever a jar is replaced.
- **Backs up the YouTube jar** to `ytcookies.backup` (also read-only). The Admin
  Console can **Save Backup** and **Restore Backup**, and a missing/empty live jar
  is auto-restored from the backup at boot.
- **Unlocks only to write:** when the admin replaces a jar (text paste **or**
  `.txt` document), `modules/admin.py::_write_cookie_jar` briefly `chmod 644`s the
  target, writes, re-locks to `444`, and purges stale snapshots.

A **live cookie test** (`diagnose_youtube_access`) probes YouTube three ways — no
auth, cookies-only, cookies+PO — and reports real-format counts so you know
*exactly* whether your cookies / PO stack are healthy.

---

## 🪜 Strategy ladder & error classification

`utils/downloader.py::extract_formats` walks a strategy ladder so the bot degrades
gracefully per site:

- **YouTube:** cookies + PO token only (no lower rungs).
- **Other sites:** best format with cookies → if that yields only
  storyboard/preview formats (a soft block), retry without cookies → if it still
  fails with a *sign-in required* error, surface "this needs login cookies";
  otherwise fall back to a no-auth attempt.

`_classify_ytdl_error` maps the exception to one of:
`sign_in_required`, `geo_blocked`, `rate_limited`, `private_deleted`,
`live_or_storyboard`, `network`, `unknown` — each with a human-readable hint.

---

## 📤 Upload ceilings & splitters

Telegram limits: **2 GB** via the Bot API, **4 GB** via a Premium userbot. The
uploader (`utils/uploader_handler.py`) picks the right boundary per file:

| Mode        | Target per segment | Hard ceiling |
|-------------|--------------------|--------------|
| Bot API     | ~1900 MB           | 2000 MB      |
| Premium     | ~3900 MB           | 4000 MB      |

Video/audio containers are split with `ffmpeg -c copy` on keyframes
(`split_video_by_size_generator`) so every part stays independently playable;
arbitrary files are binary-chunked (`split_file_generator`). Both honor a **hard
ceiling** because the keyframe splitter can overshoot. Upload is **on-demand &
sequential**: only one extra segment ever lives on disk at a time.

---

## 📁 Directory structure

```text
tgbot/
├── install.sh                # One-shot provisioning (apt, Deno, venv, provider, swap, systemd unit)
├── run.sh                    # Safe startup wrapper (loads .env, venv, PATH; exec python main.py)
├── uninstall.sh              # Reverses install.sh (prompts for each step)
├── deploy/
│   └── tgbot.service         # systemd unit TEMPLATE (rendered by install.sh with real user + RAM)
├── .env.example              # Copy to .env and fill in tokens
├── requirements.txt          # pyrogram stack + yt-dlp[default] + bgutil PO plugin + dotenv
├── config.py                 # Reads .env via python-dotenv; all settings live here
├── generate_session.py       # Local utility: generate a Premium userbot session string
├── main.py                   # Bootloader: logger, cookie init+lock, PO provider, FastAPI, SIGTERM
├── database.json             # Whitelisted / blacklisted / settings registries (runtime)
├── ytcookies.txt … xcookies.txt  # Site cookie jars (live jars are read-only)
├── cookies.txt               # Global fallback cookie jar
├── bgutil-provider/          # PO-token provider source (cloned by install.sh, git-ignored)
├── utils/
│   ├── pot_provider.py       # PotProviderManager: install/patch/start/supervise the Deno server
│   ├── downloader.py         # Strategy ladder, PO injection, snapshots, diagnosis, splitters
│   ├── uploader_handler.py   # On-demand sequential splitter + 2 GB / 4 GB uploader
│   ├── logger.py             # TelegramChannelHandler + local rotating file mirror
│   ├── updater.py            # 6-hour yt-dlp nightly updater (preserves [default] extras)
│   ├── shared.py             # In-memory registries: queue, caches, PO state, runtime settings
│   ├── queue_manager.py      # Non-blocking serializing task queue
│   ├── gate.py               # Security access control + settings registry
│   └── id_validator.py       # Telegram ID format checks
└── modules/
    ├── admin.py              # Admin Console: users, cookies (test/backup/restore), PO Token menu
    ├── downloader_handler.py # Link & direct-URL queue worker + format grid selector
    ├── stream_interceptor.py # Forwarded-file → stream-link generator (24h validity)
    └── stream_handler.py     # FastAPI stream bridge (24h token check)
```

---

## ⚙️ Configuration

All runtime secrets/flags live in **`.env`** (copy from `.env.example`). `config.py`
loads them with `python-dotenv` at import time. Key groups:

- **Required:** `API_ID`, `API_HASH`, `BOT_TOKEN`, `SYSTEM_CREATOR_ID`.
- **Logging:** `LOG_CHANNEL_ID` (private channel, bot as admin).
- **4 GB uploads:** `PREMIUM_STRING_SESSION` (from `generate_session.py`).
- **Streaming:** `DOMAIN`, `SSL_CERT_PATH`/`SSL_KEY_PATH`.
- **Proxy (only on blocked networks):** `SOCKS5_PROXY` / `ALL_PROXY` / `HTTP_PROXY` / `HTTPS_PROXY`.
- **PO provider (leave defaults):** `YTDLP_POT_ENABLED`, `YTDLP_POT_PORT=4416`, `YTDLP_POT_PROVIDER_REF`.

`utils.shared.RUNTIME_SETTINGS` holds two admin-adjustable housekeeping knobs:
`max_cache_age_hours` (auto-clean threshold) and `max_disk_usage_pct` (download
guard). These are **not** upload-size knobs — the 2 GB / 4 GB boundary is picked
per-file by the uploader.

---

## 🛠 Progress log

- [x] Phase 1 — Base environment & provisioning (`install.sh` / `run.sh` / `uninstall.sh` / systemd unit)
- [x] Phase 2 — Security gate & morphing Admin Console
- [x] Phase 3 — yt-dlp strategy-ladder extractor & dynamic sizing
- [x] Phase 4 — Format grid selector & metadata embedder
- [x] Phase 5 — FastAPI stream bridge (24h token)
- [x] Phase 6 — Serializing job queue & event logs
- [x] Phase 7 — Premium integration & on-demand keyframe splitting (2 GB / 4 GB)
- [x] Phase 8 — State-machine finalization & storage cleanups
- [x] Phase 9 — Standalone logger (Telegram channel + local rotating mirror)
- [x] Phase 10 — **PO-token provider** (bgutil/Deno), localhost-patched & supervised
- [x] Phase 11 — **Cookie protection** (read-only lock, snapshots, backup/restore, live test)
- [x] Phase 12 — **Site-aware error classification** & storyboard-only detection
- [x] Phase 13 — **Docker-withdrawal:** bare-metal install path as the default; docs rewritten

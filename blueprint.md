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
- **Backs up the YouTube jar** to `cookies/youtube/ytcookies.backup` (read-only).
  The Admin Console can **Save Backup** and **Restore Backup**, and a missing/empty
  live jar is auto-restored at boot.
- **Cookie jar layout (new):** `cookies/youtube/ytcookies.txt`,
  `cookies/instagram/igcookies.txt`, `cookies/tiktok/ttcookies.txt`,
  `cookies/twitter/xcookies.txt`, and `cookies/ytdlp/<site>.txt` for all other
  yt-dlp sites, plus `cookies/ytdlp/cookies.txt` as the global fallback.
  Per-site jars are uploaded via the admin console (`➕ Per-Site Jar`).
- **Unlocks only to write:** when the admin replaces a jar (`.txt` document or
  text — text paste is rejected for YouTube, accepted for per-site jars),
  `_write_cookie_jar` briefly unlocks (`chmod 644`), writes atomically
  (`os.replace`), re-locks (`chmod 444`), and purges stale snapshots.

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

## 📐 Download size accuracy (button vs. uploaded file)

Users sometimes notice the size on a format button doesn't exactly match the
file they receive. **The size math is correct** (`estimate_format_size`); the
only thing that can cause a real mismatch is the **download selector resolving to
a different stream than the one that was sized**. This mirrors how
[ytdlnis](https://github.com/deniscerci/ytdlnis) handles it.

- **Sizes are per-format.** yt-dlp exposes `filesize` (exact `clen`/
  content-length), `filesize_approx` (bitrate × duration), and `tbr`/`vbr`/`abr`.
  For a merged `video+audio` download yt-dlp does **not** report the combined
  size, so the button is built as `video_stream + best_audio`
  (`v['bytes'] += best_audio_bytes`).
- **Only `filesize`/`clen` is exact.** Everything else is an estimate that tends
  to **overshoot**, so the real file is often a little *smaller* than the
  number. Those buttons carry a `~` prefix to say "approximate"; that gap is
  expected, not a bug — **except** when no usable metadata exists at all; see
  the next bullet.
- **Blind-guess formats get an exact CDN probe** (`_apply_cdn_size_probes`):
  Instagram DASH reels (and similar direct-CDN sites) expose formats with no
  `filesize`, no `tbr`, and frequently no `duration`, so the 60-second fallback
  heuristic overshot real files by 2–3× (measured: button `~5M`, upload `2 MB`).
  For button-visible formats in that class the extractor HEADs the stream URL
  (Range-GET fallback) and uses the real `Content-Length` — the button turns
  exact and drops the `~`. YouTube/TikTok (which report stream metadata) are
  never probed, so no extra traffic is wasted there.
- **The historical defect (fixed `5003d78`):** the old single-video selector
  `{format_id}+bestaudio/best` could **silently** collapse to the final muxed
  `/best` — a single low-res stream far smaller and lower-quality than the
  button. The selector is now
  `{format_id}+bestaudio / bestvideo[height<=H]+bestaudio / best[height<=H] / best`,
  so every fallback stays **merged and height-capped** until the absolute
  last-resort muxed stream. A tap can no longer silently drop to a tiny file.
- **Muxed sites have no merge overhead.** TikTok reels are single streams with
  audio inside; the selector `+bestaudio` finds nothing and the fallback chain
  lands on the same muxed stream, so the button's exactness rule is
  `video.exact` alone (the joint video+audio rule only applies when a separate
  best-audio stream exists).
- **Diagnostic rule for any size complaint:** inspect the selector's fallback
  chain first (`download_media`), not the estimator. See
  `docs/memory/tgbot-ytdlnis-size-approach.md` and AGENTS.md invariant #11.

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
    ├── pot_provider.py       # PotProviderManager: install/patch/start/supervise the Deno server
    ├── downloader/           # Strategy ladder, PO injection, snapshots, diagnosis, splitters
    │   ├── cookies.py        # Cookie resolution, YouTube diagnosis, site context
    │   ├── url_normalize.py  # TikTok shortlinks, IG highlights, PO options
    │   ├── sizing.py         # Size estimation, CDN probes, disk space
    │   ├── errors.py         # yt-dlp error classification
    │   ├── formats.py        # Format extraction & sorting (extract_formats)
    │   ├── playlists.py      # Playlist metadata & tier selectors
    │   ├── thumbnails.py     # Thumbnails, ffmpeg metadata, video probing
    │   ├── download.py       # Single-media download pipeline (download_media)
    │   └── split.py          # Binary & video splitting generators
    ├── uploader_handler.py   # On-demand sequential splitter + 2 GB / 4 GB uploader
    ├── logger.py             # TelegramChannelHandler + local rotating file mirror
    ├── updater.py            # 6-hour yt-dlp nightly updater (preserves [default] extras)
    ├── shared.py             # In-memory registries: queue, caches, PO state, runtime settings
    ├── queue_manager.py      # Non-blocking serializing task queue
    ├── gate.py               # Security access control + settings registry
    └── id_validator.py       # Telegram ID format checks
└── modules/
    ├── admin/                # Admin Console: users, cookies (test/backup/restore), PO Token menu
    │   ├── keyboards.py      # Console/premium/cookies/PO/direct keyboards
    │   ├── state.py          # Module-level state (USER_STATES, PREMIUM_GEN, etc.)
    │   ├── premium_gen.py    # In-chat Premium session generation flow
    │   ├── cookies.py        # Cookie jar validation & atomic write
    │   ├── cookie_test.py    # Live cookie-jar test (yt-dlp probe)
    │   ├── pot_menu.py       # PO Token Provider menu & actions
    │   ├── direct_menu.py    # Direct-Forward menu rendering
    │   ├── callback_dispatch.py  # Callback query dispatcher (admin UI)
    │   └── register.py       # Handler registration & text/command routing
    ├── downloader_handler.py # Link & direct-URL queue worker + format grid selector
    ├── stream_interceptor.py # Forwarded-file → stream-link generator (24h validity)
    └── stream_handler.py     # FastAPI stream bridge (24h token check)
```

---

## 🔄 Direct-forward feature (Instagram / X DM relay)

**What it is:** A background worker (`modules/direct_forward/`) that polls
the **DM inbox** of the bot's own Instagram and/or X account and relays
anything you DM it — photos, videos, reels, story shares, tweet shares, or
plain links — into your Telegram chat.

**Why it exists:** You (the user) described this exact need: instead of copying
every post link into Telegram manually, you open the chat with the bot account
on Instagram/X and **forward the post right there**. The bot detects it
automatically. (This replaces the old saved/liked-feed relay, which
misread the requirement.)

**How to use it:**

1. **Set up the accounts.** Instagram uses a **dedicated bot account**
   (e.g. `@mybot_ig`); open a DM thread with it from your personal account. X
   uses the **self-DM method**: no bot account — you send tweets/photos/videos
   to your OWN "Message Yourself" conversation, and the worker polls that one
   thread authenticated with the shared `xcookies.txt` jar.
2. **Provide cookies for the account(s).** Upload `igcookies.txt` /
   `xcookies.txt` via the Admin Console — the same jars used for manual
   downloads. The Instagram DM client bootstraps its login directly from the
   jar's `sessionid` (usually **no password login is needed**); the X worker
   rides `auth_token`+`twid` from the xcookies jar, so that jar must hold a
   live session (yt-dlp write-back keeps it warm).
3. **Configure `.env`:**
   ```
   DIRECT_FORWARD_CHAT_ID=YOUR_NUMERIC_TELEGRAM_ID
   DIRECT_FORWARD_POLL_SECONDS=300   # ≥300 recommended; jittered ±40%
   IG_DIRECT_ENABLED=true
   IG_DIRECT_FROM_USERNAME=your_personal_ig_handle   # only your DMs are relayed
   X_DIRECT_ENABLED=true
   XCHAT_PIN=1234      # optional: only if your self-DM uses X Chat E2EE (passcode)
   ```
4. **Restart the bot:** `sudo systemctl restart tgbot`. Look for
   `[DirectForward] started -> chat ...` in the logs. Unconfigured pieces log
   a clear reason and never block the bot.
5. **DM the relay.** On Instagram, share a reel, story, post, photo, video or
   paste a link into the DM thread with the bot account. On X, send tweet
   links, photos or videos to your own "Message Yourself" conversation. If you
   enabled X Chat (E2EE passcode) on that conversation, the
   `tgbot-xchat-bridge` Deno sidecar decrypts it (set `XCHAT_PIN`); otherwise
   twikit reads it directly. Within one poll interval it lands in your Telegram
   chat with the caption `📥 <Platform> DM from @you`.

**What it supports:** DM photo/video attachments (sent directly as Telegram
photo/video), post/reel/clip shares, story shares, tweet shares, and plain
links in any DM text (routed through the standard yt-dlp pipeline with cookie
jars, so login-walled content works). Only your whitelisted account's DMs are
processed. The first run primes the cursor and skips backlog; state lives in
`direct_forward_state.json`. All three workers (IG/X/TikTok) share that one file
and write it **merge-only per platform** (`_state_save_owned`), never a
full-dict overwrite, so one worker can't clobber another's cursor (a stale
full-dict save used to reset the X cursor and re-relay the whole self-DM
backlog — fixed 2026-08-11). A 2026-08-11 audit hardened the mechanisms
further: `cache/xchat_bridge_state.json` + `cache/xchat_inbox.jsonl` are
exempt from the hourly cache cleaner (deleting the bridge cursor re-primes
`last_seq` to newest and silently skips older messages); photo-only pasted
tweets are delivered natively via a hardened `_x_fallback_photos` (primary: a
raw `client.gql.tweet_detail` GraphQL walk scoped to the focal `tweet-<id>`
entry so thread replies/quote tweets are not over-collected, bypassing twikit
2.3.3's `User.__init__` `KeyError('urls')`; the old model path is the secondary
fallback) or a text-only note — never a failed queue task; the
TikTok worker's `_tt_wid`/`_tt_oembed_author` network calls run through
`run_in_executor` so a slow endpoint can't freeze the event loop, and its
reconnect cadence honours `TIKTOK_DIRECT_POLL_SECONDS` via `_tt_poll_interval`
(no longer the shared `_poll_interval`); the X worker live-reloads the
xcookies jar every poll (hash-compare + re-apply, rebuild client + re-prime
cursor on `twid` change) so a jar re-upload needs no restart. See
`docs/memory/tgbot-2026-08-11-selfdm-audit.md` and
`docs/memory/tgbot-2026-08-11-x-photo-paste-fix.md`. The IG worker never exits on a login failure — it
retries each poll, so a mid-run `igcookies.txt` replace is picked up without a
bot restart (only real checkpoint challenges freeze it for 3–5 h). Post/reel
shares are delivered via the Instagram-native path; a carousel containing an
empty/invalid CDN resource is degraded to its healthy items instead of failing
the whole send. Full guide: `docs/DIRECT_FORWARD_SETUP.md`.

**How to disable:** Set `DIRECT_FORWARD_CHAT_ID=0` or all `*_DIRECT_ENABLED`
to `false` in `.env`, then restart.

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
- [x] Phase 13 — **Docker-withdrawal:** bare-metal install path as the default; docs rewritten.
- [x] Phase 14 — **Cookie folder reorganization** (`cookies/youtube/`, `instagram/`, `tiktok/`, `twitter/`, `ytdlp/`).
- [x] Phase 15 — **Instagram no-auth-first fix** (`extract_formats` tries no-auth for Instagram; cookies trigger HTTP 400 when session is stale/flagged).
- [x] Phase 16 — **Direct-forward DM relay** (`modules/direct_forward.py`): poll the bot's own Instagram / X DM inboxes and relay DMed media (photos, videos, reels, story shares, tweet shares, links) to `DIRECT_FORWARD_CHAT_ID`. See [Direct-forward feature](#-direct-forward-feature-instagram--x-dm-relay) below.
- [x] Phase 17 — **Learn course** (`learn/`): 19-lesson Python curriculum using this bot as the case study.

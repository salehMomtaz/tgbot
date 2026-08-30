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
- `utils/downloader/url_normalize.py::_apply_pot_options` injects `player_client=mweb` + the
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
   (`utils/downloader/cookies.py::get_cookies_for_url`). Snapshots are purged on a timer
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

### Per-site cookie jars for all yt-dlp sites

With full yt-dlp support, any of the 1,700+ supported sites may need cookies
for login-walled content. The bot provides a **cookie jar per site**:

- **Naming pattern**: `cookies/ytdlp/<site>.txt` where `<site>` = first label of
  the hostname (e.g., `pornhub.com` → `pornhub.txt`, `vimeo.com` → `vimeo.txt`).
- **Auto-generated on boot**: `main.py::initialize_cookie_jars` creates empty
  jars with a Netscape header for every known yt-dlp extractor domain.
- **Admin upload**: Admin Console → 🍪 Cookie Jars → **➕ Per-Site Jar** → type
  the site identifier → send `.txt` document. The bot validates (real cookie
  lines, not header-only) and writes atomically.
- **Resolution**: `_resolve_jar_path()` in `utils/downloader/cookies.py` extracts
  the domain from the URL, strips `www.`, takes the first label, and looks up
  `cookies/ytdlp/<site>.txt`. Falls back to `cookies/ytdlp/cookies.txt` (global).
- **Special cases**: Some sites don't fit the simple pattern (multi-domain
  sites, adult sites with age gates, Chinese sites requiring CN IP, DRM sites).
  See [`docs/COOKIES.md`](docs/COOKIES.md)
  for the full reference.

### Direct-forward workers & cookie freshness

The IG / X / TikTok DM workers use the **shared primary jars** (`igcookies.txt`,
`xcookies.txt`, `ttcookies.txt`) but **do not trigger cookie write-back**
(they use `instagrapi`/`twikit` directly, not yt-dlp). If a site is *only*
accessed via direct-forward (no yt-dlp downloads), its jar **will go stale**.
Fix options:
1. Upload fresh cookies periodically via Admin Console.
2. Set fallback credentials in `.env` (`IG_DIRECT_USERNAME`/`PASSWORD`,
   `X_DIRECT_*` — X already uses shared jar; TikTok uses shared jar).
3. Future: add a periodic yt-dlp "cookie refresh" job for Instagram.

---

## 🪜 Strategy ladder & error classification

`utils/downloader/formats.py::extract_formats` walks a strategy ladder so the bot degrades
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
  `docs/DOWNLOADER.md` and AGENTS.md invariant #11.

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
│   ├── tgbot.service         # systemd unit TEMPLATE (rendered by install.sh with real user + RAM)
│   ├── tgbot-monitor.service # System monitor (standalone Go binary, detached)
│   └── tgbot-xchat-bridge.service # XChat E2EE sidecar (Deno, resident supervisor)
├── .env.example              # Copy to .env and fill in tokens (Telegram + optional Bale)
├── requirements.txt          # pyrogram + aiogram (Bale) + yt-dlp[default,curl-cffi] + bgutil PO plugin
├── config.py                 # Reads .env via python-dotenv; all settings live here (BALE_*, GITHUB_TOKEN, etc.)
├── generate_session.py       # Local utility: generate a Premium userbot session string
├── main.py                   # Bootloader: logger (dual Telegram+Bale), cookie init+lock, PO provider, FastAPI, SIGTERM
├── database.json             # Whitelisted / blacklisted / settings registries (runtime)
├── cookies/                  # Live jars (read-only at rest, write-back via snapshots)
│   ├── youtube/ytcookies.txt # YouTube (plus .backup)
│   ├── instagram/igcookies.txt
│   ├── tiktok/ttcookies.txt
│   ├── twitter/xcookies.txt
│   └── ytdlp/<site>.txt      # Per-site jars (90+ auto-generated) + cookies.txt fallback
├── direct_ig_session.json    # Instagram private-API session (cleared on fresh igcookies upload)
├── direct_forward_state.json # DM cursors (merge-only per platform, shared IG/X/TikTok)
├── cache/                    # Download snapshots, splits, xchat inbox (protected), github cache
├── logs/bot.log              # Local rotating mirror (5 MB x3)
├── bgutil-provider/          # PO-token provider source (cloned by install.sh, git-ignored)
├── cmd/tgbot-monitor/        # Go system monitor (standalone, survives bot)
├── utils/
│   ├── pot_provider.py       # PotProviderManager: install/patch/start/supervise Deno 127.0.0.1
│   ├── cookie_manager.py     # Snapshot + overlay merge + meta.json freshness
│   ├── cookie_refresher.py   # Sequential headless Playwright refresher (1 tab at a time, 24h, 300MB)
│   ├── downloader/           # Strategy ladder, PO injection, snapshots, diagnosis, splitters
│   │   ├── cookies.py        # Cookie resolution, YouTube diagnosis, site context
│   │   ├── url_normalize.py  # TikTok shortlinks (vt/vm/vn -> embed), IG highlights, PO options
│   │   ├── sizing.py         # Size estimation, CDN probes, disk space
│   │   ├── errors.py         # yt-dlp error classification
│   │   ├── formats.py        # Format extraction & sorting (extract_formats, language-aware audio)
│   │   ├── playlists.py      # Playlist metadata & tier selectors (PLAYLIST_TIERS)
│   │   ├── thumbnails.py     # Thumbnails, ffmpeg metadata, video probing (frame fallback)
│   │   ├── download.py       # Single-media download pipeline (download_media)
│   │   ├── split.py          # Binary & video splitting generators (keyframe -c copy)
│   │   └── supported_sites.py # 1,786 yt-dlp _VALID_URL patterns (generic excluded), is_ytdlp_supported
│   ├── uploader_handler.py   # Telegram on-demand sequential splitter + 2 GB / 4 GB (target 1900/3900)
│   ├── logger/               # Logging package (split from logger.py monolith in 6259df2)
│   │   ├── __init__.py       # Re-exports (from utils.logger import ...)
│   │   ├── local.py          # ensure_local_log_handler (logs/bot.log, 5 MB x3)
│   │   ├── telegram.py       # TelegramChannelHandler (Rich 32768 limit, LOG_CHANNEL_ID)
│   │   └── bale.py           # BaleChannelHandler (bale. child logger, BALE_LOG_CHANNEL_ID)
│   ├── propagation.py        # stop()/continue_() — re-raise real StopPropagation/ContinuePropagation
│   ├── updater.py            # 6-hour yt-dlp nightly updater (preserves [default,curl-cffi], --pre)
│   ├── shared.py             # In-memory registries: queue, DOWNLOAD_CACHE, POT_AVAILABLE, RUNTIME_SETTINGS
│   ├── queue_manager.py      # Non-blocking serializing task queue (single worker, priority)
│   ├── gate.py               # Security access control + settings registry (authorized/blacklisted/premium)
│   ├── id_validator.py       # Telegram/Bale ID format checks (5-11 digits)
│   ├── security.py           # SSRF guard (_is_ssrf_target), is_safe_url, flood, redact_token
│   ├── rich_stream.py        # RichStream (Bot API 10.1+ streaming drafts, 30s ephemeral)
│   ├── keyboard_expiry.py    # Auto-expiry of dead inline keyboards
│   ├── premium_session.py    # In-chat Premium session generation (dial pad, no chat-typed code)
│   ├── ig_anti_detect.py     # CurlCffiAdapter chrome136, echo headers, geo pin, warmup
│   ├── system_monitor.py     # Spawner for Go monitor (detached)
│   └── cookie_refresher.py   # (see above)
├── modules/
│   ├── admin/                # Admin Console (Telegram, full): users, cookies, PO, premium, direct, subs
│   │   ├── keyboards.py      # Console/premium/cookies/PO/direct/sub keyboards
│   │   ├── state.py          # USER_STATES, ACTIVE_PROMPTS, PREMIUM_GEN
│   │   ├── premium_gen.py    # Premium generation flow (phone->code dial pad->2FA)
│   │   ├── cookies.py        # Cookie jar validation & atomic write (clears IG session on fresh upload)
│   │   ├── cookie_test.py    # Live cookie-jar test (yt-dlp probe, format counts)
│   │   ├── pot_menu.py       # PO Token Provider menu & actions
│   │   ├── direct_menu.py    # Direct-Forward menu rendering
│   │   ├── callback_dispatch.py  # Callback dispatcher (admin UI, now fixed UnboundLocalError)
│   │   └── register.py       # Handler registration & text/command routing (group -1,0,1,2)
│   ├── bale/                 # Bale.ai frontend (government messenger, optional, LIMITED)
│   │   ├── runner.py         # Aiogram poller tapi.bale.ai + drain, LIMITED admin, 20MB split, extras
│   │   ├── uploader.py       # Bale direct multipart (19/20 MB, sanitize, clean_caption)
│   │   └── admin.py          # Bale limited admin keyboard (no cookies/premium/POT)
│   ├── github/               # GitHub explorer (Telegram + Bale): repo panel, ZIP, branches/tags
│   │   ├── api.py            # GitHub API (headers + PAT)
│   │   ├── keyboards.py      # Repo menu, branches/releases/tags/file explorer
│   │   └── handlers.py       # Link intercept + /search /user /trend + gh: callbacks (queue)
│   ├── youtube/              # YouTube search + transcript (Telegram + Bale via /yt)
│   │   ├── scraper.py        # search_ytdlp_flat, clean_vtt_subtitles
│   │   └── handlers.py       # /yt, /ytrecent, /ytch, /transcript (yt-dlp flat + queue)
│   ├── translate/            # Google Translate (Telegram + Bale)
│   │   ├── api.py            # google_translate_async (gtx)
│   │   └── handlers.py       # /tr src:dst text
│   ├── web/                  # Webpage -> Markdown (Telegram + Bale)
│   │   ├── api.py            # fetch_markdown_text (urltomarkdown)
│   │   └── handlers.py       # /web <url> (cache + split)
│   ├── subscription/         # Subscription (Telegram only, no Bale free tier)
│   │   ├── tiers.py          # free 5, basic 100/100⭐, plus 500/250⭐, pro 2500/500⭐
│   │   ├── store.py          # subscriptions, usage, sub_settings (channels[])
│   │   ├── quota.py          # daily limit, remaining, increment
│   │   ├── access.py         # check_access (channel force-join, free_enabled)
│   │   ├── join.py           # Channel force-join verification UI (_greeting_text, chkjoin: callback)
│   │   ├── handlers.py       # /subscription, /quota, Stars/TON, gate_and_quota_check
│   │   ├── payments_stars.py # Bot API sendInvoice, pre_checkout
│   │   ├── payments_ton.py   # toncenter inbound memo
│   │   └── webapp.py         # FastAPI mount for /app, /admin/subscription
│   ├── direct_forward/       # DM relay (shared IG/X/TikTok, merge-only state)
│   │   ├── supervisor.py     # Starts IG/X/TikTok workers
│   │   ├── state.py          # _load_state, _merge_state_save, _state_save_owned
│   │   ├── common.py         # _poll_interval, _enqueue_relay, _download_and_deliver
│   │   ├── instagram.py      # IG private API (instagrapi, gap-aware 200, MQTT hybrid optional)
│   │   ├── twitter.py        # X self-DM twikit + _is_ssrf_target, magic-bytes, xchat bridge
│   │   └── tiktok.py         # TikTok IM WSS (wss://im-ws-sg.tiktok.com, protobuf, prime)
│   ├── downloader_handler.py # Telegram link & direct-URL worker + format grid (group 1)
│   ├── stream_interceptor.py # Forwarded-file -> stream-link (24h token)
│   └── stream_handler.py     # FastAPI stream bridge (Telegram file -> HTTP chunked)
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
`docs/memory/DIRECT_FORWARD_HISTORY.md` and
`docs/memory/DIRECT_FORWARD_HISTORY.md`. The IG worker never exits on a login failure — it
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
- [x] Phase 16 — **Direct-forward DM relay** (`modules/direct_forward/`): poll the bot's own Instagram / X DM inboxes and relay DMed media (photos, videos, reels, story shares, tweet shares, links) to `DIRECT_FORWARD_CHAT_ID`. See [Direct-forward feature](#-direct-forward-feature-instagram--x-dm-relay) below.
- [x] Phase 17 — **Learn course** (`learn/`): 19-lesson Python curriculum using this bot as the case study.
- [x] Phase 18 — **TikTok self-DM relay + module package refactor** (2026-08-10/11): TikTok DM relay added over the IM WebSocket (`modules/direct_forward/tiktok.py`, self-DM `0:1:{uid}:{uid}`); the large single-file modules `modules/admin.py`, `modules/direct_forward.py` and `utils/downloader.py` were split into importable packages (`modules/admin/`, `modules/direct_forward/`, `utils/downloader/`) — behaviour-preserving, no API change. Also: X photo-only tweet native delivery via raw `gql.tweet_detail` walk, and the startup crash-loop fix (undefined names + TikTok `/embed/<id>` rewrite).
- [x] Phase 19 — **yt-dlp full site support** (2026-08-12): replaced the hardcoded ~25-domain allowlist in `is_social_media_link()` with compiled yt-dlp extractor `_VALID_URL` patterns (1,786 patterns, `generic` excluded) in `utils/downloader/supported_sites.py`. All yt-dlp sites now automatically get the format-selection keyboard; non-yt-dlp URLs stay on the direct-file path. No new system dependencies — pure Python, uses the already-installed yt-dlp.
- [x] Phase 20 — **Per-site cookie jars + IG DM cookie freshness** (2026-08-12): auto-generated empty per-site cookie jars for all 90+ yt-dlp domains at boot (`cookies/ytdlp/<site>.txt`); Admin Console "➕ Per-Site Jar" flow documented; special cases cataloged in `docs/COOKIES.md`. IG DM relay diagnosed: stale `igcookies.txt` with expired `sessionid` (duplicated entries) — worker retries login on poll cadence but needs fresh cookies or fallback credentials. Direct-forward workers don't participate in yt-dlp cookie write-back; jars for sites only accessed via DM will go stale without manual refresh.
- [x] Phase 21 — **Subscription system + WebApp + multi-channel + hardening** (2026-08-13): toggleable subscription layer with legacy whitelist fallback (intruder → blacklist preserved; toggle OFF keeps old behaviour; whitelist add auto-removes blacklist). Three tiers Basic 100/d (100⭐), Plus 500/d (250⭐), Pro 2500/d (500⭐) + free 5/d (priority 0) with queue priority 0→3; daily quota + mid-playlist limit abort; Stars via Bot API `sendInvoice`/`pre_checkout` + TON/Gram via toncenter inbound memo verification (`SUB_TON_ADDRESS`); multi-channel force-join (`channels[]`) with `check_all_channels` (missing list + per-channel join buttons); flood guard tier-aware (free 5/min) + URL sanity + log token redaction + SSRF already; flood + security in `utils/security.py`. Mini App at `https://tgbot.southpark.ir:8080` (direct TLS on `:8080` via `certs/fullchain.pem`+`privkey.pem` wildcard `*.southpark.ir`, `DOMAIN=https://tgbot.southpark.ir:8080`, no nginx — `deploy/tgbot.southpark.ir.conf` kept as reference, `install.sh` now copies `southpark.ir` wildcard via certbot hook): admin console `/admin/subscription` (HMAC `admin-sub` token or creator initData) + user portal `/app` & `/api/user/status` (any valid initData, quota/history) + landing `/` auto-redirect (Telegram→role, browser→landing). Professional fullscreen UI with `safeAreaInset`, native `showPopup`/`showAlert` + fallback modal/toast (`_SHARED_UI`). IG fallback creds wired via `.env` (`IG_DIRECT_*`, TOTP seed). See `docs/INFRA.md`; polished WebApp handles `401/403` as bounded cards + native popups.
- [x] Phase 22 — **Balebot extras merged into Telegram + subscription double-reply fix** (2026-08-13): fixed `/subscription` (and `/quota`) double-reply bug — `register_subscription_handlers` now calls `message.stop_propagation()` so the Group 1 greeting never fires after the tier keyboard, plus a defensive guard in `admin_start_text_handler` (`/start` is the only command that intentionally falls through to the greeting). Ported balebot-only modules as native pyrogram handlers: `modules/github/` (full explorer: `/search`, `/user`, `/trend`, github.com links → repo panel with branches/tags/releases/issues/PRs/commits/languages/license/readme/file explorer + ZIP delivery), `modules/youtube/` (`/yt`, `/ytrecent`, `/ytch`, `/transcript`), `modules/translate/` (`/tr src:dst text` via Google translate API), `modules/web/` (`/web <url>` → Markdown via urltomarkdown). All respect `is_authorized` (security gate) and use the shared `DownloadQueue` + `process_split_and_upload` (Telegram 2 GB / 4 GB native), not Bale's 39 MB splitter. Added `GITHUB_TOKEN` to `config.py` + `.env.example`. Bale's 50 MB claim is actually 20 MB post-2024 — documented; Telegram keeps 2 GB/4 GB. See `docs/memory/BALEBOT.md`.
- [x] Phase 23 — **Bale hardened frontend (government messenger) + optional dual logging** (2026-08-13): added `modules/bale/` (`runner.py` aiogram `tapi.bale.ai` poller with manual `getUpdates` drain, `uploader.py` 19/20 MB split + `sanitize_filename_for_bale`/`clean_caption_text`, `admin.py` LIMITED console). Bale shares PO provider, queue, yt-dlp, `is_ytdlp_supported` but has **no Bale log channel by default** (Telegram logs stay on Telegram). Now optional `BALE_LOG_CHANNEL_ID` + `BaleChannelHandler` (same `INFO` level as `TelegramChannelHandler`, plain text to `tapi.bale.ai`) — user created private `bale_log` channel with `angelbalzac` admin. Fixed Bale `no-response` (catch-all stole updates before `/start`, `F.text.func` invalid) and added Bale extras (`/search`, `/yt`, `/tr`, `/web`, `github.com` panel via 20 MB split) so `https://github.com/salehMomtaz/tgbot` now replies on Bale too. No free tier on Bale (only `is_authorized` or `BALE_SYSTEM_CREATOR_ID`). See `docs/memory/BALEBOT.md`.
- [x] Phase 24 — **Free-tier Instagram no-response fix** (2026-08-13): free users (`free_enabled=true`, not in `authorized`, 5/day) passed `gate_and_quota_check` (`check_access` ok) then were immediately dropped by legacy `if not is_authorized: return` in `downloader_handler.py` -> silent no-response for `https://www.instagram.com/reel/DVjNXkOkVxC/` on alt `8022375512`. Fixed to `if not _sub_enabled and not is_authorized` so subscription is source of truth when ON. Bale intentionally keeps no free tier.
- [x] Phase 25 — **Instagram gap recovery + hybrid MQTToT push (TikTok-like)** (2026-08-13): IG worker now paginates `direct_v2/threads/{id}/` with `cursor` until `oldest_id <= last_id` (`8 pages x25 = 200` cap, `gap fetch: X had Y new` log) and **only bumps cursor on success** (failed items stay behind for retry → at-least-once, fixing the stalled `DVjNXkOkVxC` batch of 9). Upgraded `instagrapi 2.1.2 -> 2.18.14` + `aiogram 3.12 -> 3.30` for `realtime_*` MQTToT on `edge-mqtt.facebook.com` (`~5 MB`, no headless browser). Added `IG_DIRECT_MQTT_ENABLED` (default `false`, `tools/test_ig_mqtt.py` probe). When true, `polling + MQTToT` hybrid (`60s` PleaseWait backoff for testing). X left polling per request.
- [x] Phase 26 — **Sequential headless cookie refresher (1 tab at a time, 24h, 4GB safe)** (2026-08-13): DM-only jars (IG/X/TT/YT) never get `yt-dlp` write-back, so they go stale `~7 days` and hit `update_risky_contactpoint` / `login_required` (weekly IG stall). Added `utils/cookie_refresher.py` that visits each site sequentially with `Playwright` (one Chromium at a time, `~300 MB` peak, not 4 tabs → your `4GB+8swap` stays safe, `chromium-1234` already cached). Each site: load Netscape jar -> `add_cookies` -> `goto` homepage -> `networkidle 15s + 5s settle` -> `context.cookies()` -> Netscape write via `_write_cookie_jar` (atomic, `0o444`, purge snapshots, `touch meta`, clear `direct_ig_session.json` for IG). Proxy-aware (`DIRECT_FORWARD_PROXY`/`PROXY_URL`). Scheduled as `auto_refresh_cookies_loop` in `main.py` (`300s + jitter` first run, then `24h ±1h`) as isolated task. Enabled by `COOKIE_REFRESH_ENABLED=true` (`config.py`, `.env.example`). Also fixed `fresh igcookies upload now clears stale direct_ig_session.json` so next login goes straight to new `sessionid` (your "stuck on a copy" report).
- [x] Phase 27 — **Bale logging + extras parity + docs overhaul** (2026-08-13): same `INFO` level for Bale via `BaleChannelHandler` -> `BALE_LOG_CHANNEL_ID` (`bale_log`), `TelegramChannelHandler` stays on `LOG_CHANNEL_ID`. Added `BALE_LOG_CHANNEL_ID` to `config.py` + `.env.example`. Fixed Bale `github.com` panel to reply via `process_split_and_upload_bale` (20 MB splits). Overhauled `README`, `blueprint` directory map, `docs/UBUNTU_VPS_SETUP.md` (Bale private channel steps), and added `docs/USER_GUIDE.md` (feature table + how to use each).
- [x] Phase 28 — **Dispatch-propagation hardening + logger package split + channel-join greeting** (2026-08-13): `utils/logger.py` monolith split into the `utils/logger/` package (`local.py`, `telegram.py`, `bale.py`) with backwards-compat re-exports, rich-message truncation raised to the Rich Bot API limit of **32768** (was 3500/6000/8000 premature cuts in `d2c3dcf`), and a **strict log split** so the `bale_log` Telegram channel gets ONLY Bale/aiogram lines while the main log channel gets pyrogram/direct-forward/queue lines (`d723798`, Bale full-JSON detail parity in `f14a244`, POT-flap reduction + shared logs in `36505e6`). New `utils/propagation.py` (`stop()`/`continue_()`) fixes two dispatch bugs (`e3df33c`): bare `except Exception` swallowing `StopPropagation` (caused group-0 GitHub links to also be grabbed by the group-1 downloader) and a `RawUpdateHandler` mid-group starving later handlers (fixed by `raise ContinuePropagation` on non-owned updates). Added `modules/subscription/join.py` — channel force-join verification UI: the `/start` greeting now carries a single self-contained message with the intro guide + subscription access prompt + "✅ I joined — verify" keyboard (live `chkjoin:` re-check), wired in `main.py` and `modules/admin/register.py`.

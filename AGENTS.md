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
  tasks (updater, cache cleaner, PO health loop, auto-forward). SIGTERM is translated to
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
| Change playlist tiers / detection / per-video download | `utils/downloader.py` (`PLAYLIST_TIERS`, `is_playlist_url`, `extract_playlist_meta`, `download_media(format_selector=...)`) |
| Change cookie lifecycle (snapshot/merge/freshness) | `utils/cookie_manager.py` (+ call sites in `utils/downloader.py`) |
| Change streaming | `modules/stream_handler.py` / `stream_interceptor.py` |
| Change install/provisioning | `install.sh` / `run.sh` / `deploy/tgbot.service` / `deploy/tgbot-monitor.service` |
| Change system monitoring / health reports | `utils/system_monitor.py` (+ `deploy/tgbot-monitor.service`) |
| Change DM relay (IG/X → Telegram) | `modules/direct_forward.py` + `.env` (`DIRECT_FORWARD_*`) |

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
   sites keep the strategy ladder — but **Instagram flips the ladder**:
   `extract_formats` tries `no-auth` first for Instagram and falls back to
   cookies only for login-walled content. The historical reason was that stale
   sessions made cookies trigger HTTP 400 on Instagram's authenticated API;
   that failure mode is now *prevented* by cookie write-back (invariant #4),
   not by re-ordering — the no-auth-first order stays because it also conserves
   the session (public reels should never burn login state). Download time
   mirrors the ladder (`download_media` retries a 400 with cookies).

4. **Cookie jars: snapshot per run, write-back on success, locked at rest.**
   `main.py::initialize_cookie_jars` locks the four primary jars
   (`cookies/{youtube,instagram,tiktok,twitter}/*.txt`) to `0o444`. Every
   yt-dlp run gets a *per-run snapshot* from `utils/cookie_manager.py::acquire`
   (never the real path), and the caller MUST finish with
   `cookie_manager.commit(snapshot, success=...)`:
   - success → **overlay merge** of the snapshot into the real jar (atomic
     temp+rename, lock re-applied). This captures the site's session rotation
     (`Set-Cookie` on every response) and is *the* fix for jars dying in days.
     The merge NEVER deletes keys and refuses empty snapshots, so the
     "yt-dlp wiped the jar on an invalid session" failure mode is impossible.
   - failure → snapshot discarded; auth-classified errors are recorded in
     `cookies/meta.json` for the watchdog.
   Read `utils/cookie_manager.py`'s module docstring before touching any of
   this. Admin replace/restore still goes through `_write_cookie_jar`
   (modules/admin.py) which unlocks-by-replace (os.replace works on 0o444),
   re-locks, purges snapshots, and stamps `last_upload` in meta.json.
   Freshness: `freshness_warnings()` powers the startup watchdog + the admin
   Cookies menu status line; knob: `COOKIE_STALE_WARNING_DAYS` (default 21).

5. **Keep `[default,curl-cffi]` on yt-dlp upgrades.** `utils/updater.py` runs
   `pip install -U --pre "yt-dlp[default,curl-cffi]"` — plain `yt-dlp` would
   silently strip the certifi/curllib extras, and dropping `curl-cffi` removes
   the impersonation engine yt-dlp's TikTok proof-of-work challenge solver
   needs (its absence resurfaces as "malformed site" failures on TikTok).
   The `--pre` channel is what keeps it on nightly.

6. **`.env` parsing in `run.sh` must stay dotenv-style**, never `source .env`.
   Values like `YTDLP_USER_AGENT` contain characters bash treats as code. The
   line-by-line reader in `run.sh` is intentional. systemd has **no**
   `EnvironmentFile=` for the same reason — `run.sh` owns `.env` parsing.

7. **systemd unit is a template.** `deploy/tgbot.service` has `__USER__`,
   `__GROUP__`, `__PROJECT_DIR__`, `__MEMORY_MAX__` placeholders rendered by
   `install.sh` from the real user/path/RAM. Don't hardcode paths in the unit.

8. **Playlist vs single-video are two distinct paths.** `utils/downloader.py::
   is_playlist_url` detects any YouTube URL carrying `list=`; the handler routes
   it to the **tier keyboard** (`PLAYLIST_TIERS`), never the single-video format
   flow. Per-video playlist downloads call `download_media(format_selector=...)`
   — a yt-dlp *selector* string, not a `format_id` (ids differ per video).
   `extract_playlist_meta` uses **flat** extraction and deliberately applies **no
   PO token** (browsing a playlist page needs none; this keeps the meta pass
   resilient). Meanwhile `extract_formats` and the single-video `download_media`
   path keep `noplaylist=True` — do **not** remove it: a single video that
   happens to carry a stray `&list=` must stay a single video. A bad playlist
   entry is **skipped, not fatal** — keep the per-video try/except.

9. **Video button sizes already include the merged audio track.** In
   `extract_formats`, each video option's `bytes` is `video_stream + best_audio`
   because the download step merges `{format_id}+bestaudio` into an mp4. Do not
   "correct" the button back to the video-only size — the button must match what
   actually gets downloaded. (This is the *math*; the *selector* invariant below
   is what actually prevents mismatch.)

10. **Metadata fetches bypass the queue; only downloads serialize.** The
    single-worker `DownloadQueue` (`utils/queue_manager.py`) gates the **real
    download+upload jobs only** — `queued_transfer_job` (single video),
    `playlist_job` (whole playlist), and `direct_upload_job` (direct file).
    Format/playlist *fetches* (`show_format_selection`, `begin_playlist_flow`'s
    `meta_job`, the playlist `single`/`plx` picks) are spawned concurrently via
    `_spawn_fetch` (`asyncio.create_task`) and must **not** go through
    `queue.add_task`. Rationale: a user sending link B while link A downloads
    must see B's format buttons immediately, not be queued behind A's download.
    The blocking `extract_formats`/`extract_playlist_meta` calls are offloaded
    with `loop.run_in_executor` so concurrent fetches (and any running download)
    keep the event loop responsive — never call them inline. Downloads still run
    one-at-a-time by design so the user can queue many and collect files later.

11. **"Uploaded size ≠ button size": check the selector first, the estimator
    second, and blind metadata last.** Three distinct failure classes, in
    historical order of discovery:
    (a) **Selector collapse** (fixed `5003d78`): `{format_id}+bestaudio/best`
    silently dropped to a low-res muxed `/best`. The height-capped fallback
    chain + passing `best_audio_format_id` into `download_media` forces the
    exact merge that was sized. `estimate_format_size` itself
    (`filesize` → `filesize_approx` → `tbr`/`vbr`/`abr` × duration) is correct
    — do not rewrite it.
    (b) **Blind-guess metadata** (fixed via `_apply_cdn_size_probes`):
    Instagram DASH reels expose NO `filesize`, NO `tbr`, often NO `duration`,
    so the 60 s fallback heuristic overshot real files 2–3× (measured `~5M`
    vs 2 MB). Button-visible formats in that class now get an exact CDN
    `Content-Length` probe (HEAD, Range-GET fallback) and become `exact`.
    YouTube/TikTok report stream metadata and are NEVER probed — don't widen
    the probe scope to them; it's wasted traffic.
    (c) **Residual `~` overshoot**: ordinary tbr-based estimates still carry a
    `~` prefix and run a little high. Expected, not a bug.
    Muxed single-stream sites (TikTok) have no audio merge, so exactness is the
    video's own; the joint video+audio rule only applies when a separate
    best-audio stream exists. Sizes display rounded (nearest MB, sub-MB as KB).
    See `docs/memory/tgbot-ytdlnis-size-approach.md`.

12. **Cookies live under `cookies/<platform>/`, never at the project root.**
    Layout (`config.COOKIE_DIR`, `config.YTDLP_COOKIES_DIR`):
    ```
    cookies/youtube/ytcookies.txt      # YT working jar (+ .backup)
    cookies/instagram/igcookies.txt
    cookies/tiktok/ttcookies.txt
    cookies/twitter/xcookies.txt
    cookies/ytdlp/<sitename>.txt       # per-site jars for every other yt-dlp site
    cookies/ytdlp/cookies.txt          # global fallback for sites with no jar
    ```
    The four "always-present" jars are listed in `config.COOKIE_JARS` and
    initialised by `main.py::initialize_cookie_jars`. Per-site jars under
    `cookies/ytdlp/` are uploaded by the admin via `Admin → Cookies → ➕ Per-Site
    Jar` (state: `waiting_for_replace_per_site_<name>`); the jar is keyed off the
    URL's bare domain (`instagram.com` → `instagram`, `reddit.com` → `reddit`).
    `get_cookies_for_url` (utils/downloader.py) and `_site_cookie_context` look
    the site up from `urllib.parse.urlparse(url).netloc`, **not** a hardcoded
    switch — adding a new site is just dropping a `<site>.txt` in `cookies/ytdlp/`.
    Any pre-existing flat-root jars (`ytcookies.txt`, `igcookies.txt`, etc.) need
    `mv` into the new layout during deployment; the old paths are not honoured.

13. **Direct-forward = DM relay from the bot's own Instagram/X accounts.**
    `modules/direct_forward.py` (replacing the old saved/liked `auto_forward`)
    polls the bot account's DM inbox every `DIRECT_FORWARD_POLL_SECONDS`
    (default 300 s **± `DIRECT_FORWARD_POLL_JITTER_PCT`%**, never a fixed
    machine cadence — fixed short polling is what got the first IG account
    flagged "automated behavior"). Anti-detection posture, in order of weight:
    several-minute jittered intervals; `delay_range = [2, 4]` pacing every
    private-API call; **per-thread `last_activity_at` watermarks** so an idle
    cycle costs zero thread-item fetches; a persisted session/device
    (`direct_ig_session.json` — deleting it is the #1 checkpoint trigger);
    optional ONE stable proxy (`DIRECT_FORWARD_PROXY`, residential near the
    account owner, never rotated); checkpoint challenges freeze the worker
    3–5 h (no retry storms) until a human passes them in the official app.
    **The IG worker NEVER dies on a login failure** — it retries on the poll
    cadence with a fresh client per attempt, so a mid-run `igcookies.txt`
    re-upload is picked up without a bot restart; only real challenge errors
    trigger the multi-hour freeze. `_ig_login` validates the persisted session
    via `account_info()` (not `login()`, which demands a password).
    You DM media/links from YOUR account (`IG_DIRECT_FROM_USERNAME` /
    `X_DIRECT_FROM_USER_ID` whitelist or the pairing handshake) to the bot
    account; it relays photos, videos, reels, story shares, tweet shares and
    plain links into `DIRECT_FORWARD_CHAT_ID`. Instagram uses `instagrapi`
    (sync → always via `run_in_executor`; bootstraps its login from the
    `sessionid` in the igcookies jar, falls back to user/pass), X uses `twikit`
    (native async, logs in once, persists cookies to `direct_x_cookies.json`).
    Each platform runs in its own contained loop (`try/except` per poll;
    LoginRequired → re-login once). **No third-party APIs.** Downloads route
    through the normal yt-dlp pipeline (with cookie jars — write-back keeps
    them warm) and enqueue on the shared single-worker queue behind
    interactive downloads (invariant #10). First run primes the cursor and
    skips backlog. State: `direct_forward_state.json` (+ `thread_activity`
    watermarks). The worker starts in `main.main_engine()` after the FastAPI
    server; a misconfiguration must never block the bot.

 14. **Interactive responses quote the user's link message.** The format
     keyboard, playlist menus, skip warnings and **every uploaded file part**
     sent on behalf of a link quote-reply to that link's message
     (`origin_message_id` is captured into `DOWNLOAD_CACHE` and threaded into
     `process_split_and_upload(reply_to_message_id=...)`). A deleted origin is
     tolerated: `send_reply_safe` retries once without the quote. Direct-forward
     relays pass `None` (no origin message exists). Keep new user-facing sends
     on the same rule.

 15. **System monitor runs as its own process, zero deps.** The health monitor
     (`utils/system_monitor.py`) is `/proc`-only (no psutil/netdata/glances),
     talks to Telegram via raw Bot API `requests.post` (NOT pyrogram), and is
     meant to outlive the bot — so it keeps sending `#system` reports and 80%
     warnings (CPU/RAM/disk `SYSMON_WARN_PCT`) even when `tgbot` is down. It
     runs as `deploy/tgbot-monitor.service` (systemd template, installed but
     not auto-enabled) or a detached bot spawn; `is_running()` dedupes via
     pidfile + a `/proc` python-cmdline scan so the two never stack. Both the
     report and the warning carry the VPS local date-time. The log channel is
     MANDATORY for both the bot and the monitor (`LOG_CHANNEL_ID`, `main.py`
     refuses to start without it). Full design: `docs/memory/tgbot-system-monitor.md`.


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

- **TikTok shortlinks are pre-resolved by us, not yt-dlp.** `vt./vm./vn.tiktok.com/<code>` expands to the canonical `tiktok.com/@user/video/<id>` inside `normalize_url` (browser UA + 1 h TTL cache) because yt-dlp's own short-link extractor uses a bare `facebookexternalhit/1.1` HEAD and hits TikTok's stochastic anti-bot interstitial — surfacing as "The site changed its layout or the URL is malformed". `curl-cffi` must stay installed for yt-dlp's proof-of-work webpage solver; TikTok extractions/downloads also get one extra no-auth retry. Don't feed shortlinks straight to yt-dlp again.
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

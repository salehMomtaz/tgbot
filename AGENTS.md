# AGENTS.md — notes for contributors & AI agents working on tgbot

This is a pyrogram-based Telegram media downloader/streamer — **tgbot is the
primary bot and the reference implementation**. Its sibling **balebot**
(aiogram v3, Bale.ai) was an experiment to try Bale.ai messenger bots; it shares
the same download core (it was derived from tgbot's design), but tgbot is the
source of truth. A future optional Bale frontend inside this repo is designed in
`docs/memory/tgbot-balebot-integration.md`. This file captures the **non-obvious
invariants** so you don't have to rediscover them.

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
| Change system monitoring / health reports | `cmd/tgbot-monitor/` (Go binary → `build/tgbot-monitor` via install.sh) + `utils/system_monitor.py` spawner |
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
    (d) **Fragment-size artifact guard** (`_sane_filesize`, added 2026-08-07):
    some extractors (Dailymotion HLS) report a *single segment's* size as
    `filesize` (440 B for a real 220 MB stream), which made buttons say `0K`/`8K`.
    A `filesize`/`filesize_approx` implying <1% of the declared tbr is treated
    as absent so the estimator falls through to its tbr×duration chain — the
    estimator itself is untouched. Relatedly, HLS formats are excluded from CDN
    probing (`_is_hls_format`): an `.m3u8` probe measures the manifest, not the
    file. Don't "fix" the estimator; keep the guard upstream.
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
    **Routing gate (2026-08-07):** the message-level switch is
    `is_social_media_link` (modules/downloader_handler.py) — a URL that is NOT
    in its allowlist falls into the **direct-file** path (plain HTTP GET, no
    format selection). Adding a new site therefore needs BOTH its domain in
    that allowlist AND (optionally) a per-site jar. The direct-file path has an
    SSRF guard (`_is_ssrf_target`): it refuses loopback/private/link-local
    destinations, protecting the 127.0.0.1 PO provider and internal services.
    Only `http://`/`https://` count as links (`is_link`) — `file://`, `ftp://`
    etc. are plain text and never downloaded.

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
    **On top of the pacing, `utils/ig_anti_detect.py` closes the identity-
    correlation gaps that triggered the 2026-08-05 native checkpoint:**
    the instagrapi private session is given a **curl_cffi-backed TLS-
    impersonating transport** (`CurlCffiAdapter` from `curl-adapter>=1.2.1`
    — see requirements.txt; the private API previously rode plain Python
    requests TLS, an instant "this is a script" JA3), Instagram's **echo
    headers are captured + persisted + re-applied** (`IG-U-RUR`/`IG-U-SHBID`/
    `IG-U-SHBTS`/`X-IG-WWW-Claim`/`X-MID`; instagrapi natively drops the
    shbid/shbts pair), **geo/locale/timezone are pinned** to the account's
    home region (`IG_DIRECT_COUNTRY`/`IG_DIRECT_COUNTRY_CODE`/
    `IG_DIRECT_LOCALE`/`IG_DIRECT_TZ_OFFSET`/`IG_DIRECT_TZ_NAME`), and a short
    **paced warmup** runs after login. Every piece degrades to a no-op on
    failure. Checkpoint hits alert the relay chat directly, not just the log
    channel. One wiring gotcha: `install_token_echo` wraps the *bound*
    `cl.private_request` — do NOT re-pass `self` into it. If you upgrade
    instagrapi and the class patches stop matching, the module logs loud
    warnings — verify `_configure_private_session_retry` / `base_headers` /
    `get_settings` patch points still exist.
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

 15. **System monitor is a static Go binary, independent of the bot.** The
     health monitor lives in `cmd/tgbot-monitor/` (Go, stdlib-only — no
     psutil/netdata/glances). It ships as **prebuilt static binaries**
     (`prebuilt/tgbot-monitor-linux-amd64` and `-arm64`, built with
     `CGO_ENABLED=0`) and install.sh picks the one matching `uname -m`
     (`x86_64`→amd64, `aarch64`→arm64) and copies it to `build/tgbot-monitor`;
     only if the prebuilt is missing does install.sh lazily apt-install
     `golang-go` and `go build` from source. `build/` is gitignored; the
     prebuilt dir is committed. When you change `cmd/tgbot-monitor/`, you MUST
     rebuild BOTH prebuilt binaries (on any box with Go: `GOOS=linux
     GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o
     prebuilt/tgbot-monitor-linux-amd64 .` and the same for `arm64`) or a
     fresh install will silently ship the stale binary. `utils/system_monitor.py`
     is only a thin spawner (`spawn_detached_monitor` / `is_running`) so
     main.py can launch it. It reads `/proc` and posts to Telegram via the raw
      Bot HTTP API (NOT pyrogram), and is meant to outlive the bot — so it keeps
      sending `#system` reports and 80% warnings (CPU/RAM/disk `SYSMON_WARN_PCT`)
      even when `tgbot` is down. **Report cadence counts on a monotonic
      `reportCount`, never on `len(samples)`** — the history buffer is trimmed
      to `historySamples` (240), so `len(samples) % reportEvery == 0` pins at
      the cap and the monitor goes permanently silent after the first 240
      samples (fixed in `shouldReportReport`; keep it that way). It runs as
      `deploy/tgbot-monitor.service`
     (systemd template, `ExecStart` = the Go binary, installed but not
     auto-enabled) or a detached bot spawn. The binary writes
     `system_monitor.pid` on start and removes it on exit; the Python spawner's
     `is_running()` dedupes via that pidfile + a `/proc` cmdline scan so the
     two never stack. Reports/warnings are sent as **rich messages**
     (`sendRichMessage`, rich HTML with a bordered metrics `<table bordered>`
     and the top-N lists rendered as `<table bordered>` rows, not `<ol>` lists)
     with an automatic `sendMessage` fallback — the plain fallback text is
     byte-compatible with the pre-rich format, so the channel renders
     identically on any Bot API version. Rich tables need the `bordered`
     attribute or Telegram renders them borderless (plain HTML has no tables at
     all). Both the report and the warning carry the VPS local date-time. The
     log channel is MANDATORY for both the bot and the monitor
     (`LOG_CHANNEL_ID`, `main.py` refuses to start without it). Go port
     rationale: `docs/go-feasibility.md`; full design:
     `docs/memory/tgbot-system-monitor.md`. Tests:
     `cd cmd/tgbot-monitor && go test ./...`.

16. **User-facing "analyzing" status messages stream via `sendRichMessageDraft`.**
    `utils/rich_stream.py::RichStream` (Bot API 10.1+ streaming drafts) replaces
    the old throwaway "Analyzing link formats… / Reading playlist…" status
    messages in `modules/downloader_handler.py`. Rules:
    - The draft is a 30 s ephemeral, animated preview in a **private chat**;
      the caller MUST follow up with the real message (format keyboard, playlist
      menu, or error) via `status.close()` + `send_reply_safe`. Never leave a
      user hanging on a draft.
    - Streaming is **best-effort**: if `sendRichMessageDraft` is rejected the
      stream silently falls back to a plain quoted status message that gets
      `edit_text`-ed — the bot must keep working on any Bot API version.
    - Progress bars, uploads and download statuses stay on **real pyrogram
      messages** (they need `edit_text` by message id; a draft has no id and
      expires in 30 s). Do not widen streaming to long-running jobs.
    - Only the downloader's private-chat flows use it; the Go monitor's
      `#system` reports and the log-channel handler send **rich messages**
      (`sendRichMessage`) with a plain `sendMessage` fallback (invariant #15),
      not streaming drafts.
    - Ephemeral Messages (`receiver_user_id`, `callback_query_id`, Bot API
      10.2) are a **group-chat** feature; this bot is private-only, so they are
      intentionally not used. Don't add them for private chats.

17. **Every video upload must carry a thumbnail; discovery is magic-bytes, not
    extensions.** Telegram's server auto-generates a video thumbnail **only for
    files < 10 MB** — larger videos arrive thumbless unless the sender provides
    one. yt-dlp's `writethumbnail` writes TikTok's cover as `<title>.image`
    (not `.jpg`), so an extension-only lookup silently dropped EVERY TikTok
    thumb and only < 10 MB TikTok videos *appeared* fine (server-generated).
    Rule: `download_media` locates the cover via
    `utils/downloader.py::_find_thumbnail_file` (extension list **plus** a
    magic-byte scan of the task dir for `<stem>.*` / `<stem>_*` siblings —
    `_looks_like_image` checks JPEG/PNG/WebP/GIF/BMP magic). If no cover
    exists, `extract_video_frame_thumb` pulls a 320×320 frame from the video
    (ffmpeg `-ss 1` then `-ss 0` retry, best-effort `None`). This fallback is
    wired into `send_single_media`, `_stage_and_relay` and (resolved once,
    reused for part 1 of a split) `process_split_and_upload`, so **any** video
    upload — YouTube, TikTok, IG, X, yt-dlp sites, and the direct-forward DM
    relays (`_video_upload_kwargs`: probe width/height/duration + frame thumb)
    — is fully specified: `duration`, `width`, `height`, `thumb`,
    `supports_streaming`. Frame thumbs are named `<video>_thumb.jpg` (unique
    per file — never a shared `frame_thumb.jpg`, which two concurrent DM
    relays in `cache/` would clobber) and swept by the hourly cache cleaner /
    the relays' own `finally` blocks. Do not revert the fallback to "no thumb";
    do not rename the frame thumb to a fixed name.


## Running / testing

There's no Python test suite. Verify changes with:

```bash
# Python syntax for every module
python3 -m py_compile $(git ls-files '*.py')

# Bash syntax for the scripts
bash -n install.sh run.sh uninstall.sh

# Go monitor unit tests (the project's one test suite)
cd cmd/tgbot-monitor && go test ./...

# Local smoke (needs a valid .env) — NOT on a bot that's already polling Telegram
source venv/bin/activate
python main.py
```

Never run two bot instances against the same `BOT_TOKEN` at once — Telegram errors
with "terminated by other getUpdates". Stop tmux/systemd before the other starts.

## Secrets handling (this repo lives in-place, secrets are neighbours)

The bot is deployed **inside the git working tree** (`/home/dev/opencode/tgbot`),
so secrets are never isolated — they sit next to tracked code. The protection is
`.gitignore`, not geography. Rules that keep them out of git:

- **`.env` is the only env file, and it is git-ignored.** Never commit it, never
  copy it under another name that isn't ignored, never commit a diff or a redacted
  copy that still contains real values. `.env.example` (tracked) is the template;
  real values live only in the machine's `.env`.
- **Cookie jars, sessions, DB and forward state are ignored by layout and by
  name** (`.gitignore` lines 2–47): `cookies/`, `database.json`, `*.session`,
  `direct_forward_state.json`, `direct_ig_session.json`, `direct_x_cookies.json`,
  `*.autobak`. Anything that contains a session token must stay under one of
  these paths or names — if you introduce a new secret file, **add it to
  `.gitignore` in the same change**. New per-site jars go under
  `cookies/ytdlp/<site>.txt` (inside the ignored `cookies/` tree).
- **Verify before you trust.** `git status` should never show `.env`, jars,
  sessions or `database.json`. `git check-ignore -v <path>` confirms a rule.
  If anything secret shows as tracked/untracked, stop and fix `.gitignore`
  before continuing. Check `git ls-files | grep -E '\.(env|session)|cookies/'`
  occasionally to prove no secret ever got committed.
- **Never pipe secrets through the shell history.** Prefer `scp`/`rsync` for
  whole-dir migration (`scp -P 1605 -r dev@66.23.198.52:/path/.env .`), not
  `cat > file <<EOF`. If you must type a value interactively, it's fine; don't
  leave it in a command you re-run.
- **The remote VPS is retired** (services stopped+disabled) but still holds a
  full secret copy at `dev@66.23.198.52:1605`. Treat that box as a trusted
  backup until it's decommissioned or wiped; do not paste its credentials into
  tracked files.
- **Read-only jars are on purpose.** `main.py::initialize_cookie_jars` locks
  jars to `0o444` (invariant #4); the bot writes through snapshots. Don't chmod
  them writable for ad-hoc testing — copy to `/tmp` instead, as the bot itself
  does via `cookie_manager.acquire`.

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

The `TelegramChannelHandler` sends log lines to the channel as **rich messages**
(`sendRichMessage`, `rich_message: {"html": ...}`) with a `sendMessage` fallback
that posts the identical HTML (kept byte-compatible with the pre-rich format), so
log rendering is identical on any Bot API version.

### Why the bot's logger is NOT written in Go (unlike the system monitor)

The Go monitor exists because it must **outlive the bot** (report even when the
bot is dead). The logger has the opposite requirement: it lives *inside* the bot
process by design, and should die with it. The logger is a Python `logging`
handler (`utils/logger.py::TelegramChannelHandler`) invoked by the root logger
inside `main.py` — making it Go would mean a sidecar process, an IPC pipe, and a
reconnect protocol just to replicate a 5-line fire-and-forget `requests.post`.
There is no CPU/RAM/robustness win (the monitor's rationale) because the logger
does one tiny HTTP POST per line, is already non-blocking (daemon thread), and
must in any case be Python to hook `logging`. So: the *sender* stays Python, but
it uses the same raw Bot HTTP API + rich-message endpoint the Go monitor uses.
Don't port it to Go.

## Gotchas

- **Entrypoint scripts must stay executable — systemd calls `run.sh` directly.**
  `deploy/tgbot.service` has `ExecStart=__PROJECT_DIR__/run.sh`, so `run.sh`
  (plus `install.sh`/`uninstall.sh`, invoked as `./…`) MUST keep the exec bit.
  If `git` ever checks them out as `100644` (e.g. the mode was committed wrong,
  or a `git pull` resets a working copy), the unit crash-loops with
  `status=203/EXEC` ("executable not found") and `NRestarts` climbs every ~5 s
  — the bot goes totally silent. Symptom fingerprint: `systemctl status tgbot`
  shows `activating (auto-restart)` + `203/EXEC`, while `logs/bot.log` ends
  cleanly. These scripts are committed as `100755`; `install.sh` also runs
  `chmod +x` on them every invocation. If you re-add a script or a pull strips
  the bit, fix the git index with `git update-index --chmod=+x <file>` and
  commit the mode change — don't just chmod the VPS copy (it won't survive the
  next pull).
- **TikTok shortlinks are pre-resolved by us, not yt-dlp.** `vt./vm./vn.tiktok.com/<code>` expands to the canonical `tiktok.com/@user/video/<id>` inside `normalize_url` (browser UA + 1 h TTL cache) because yt-dlp's own short-link extractor uses a bare `facebookexternalhit/1.1` HEAD and hits TikTok's stochastic anti-bot interstitial — surfacing as "The site changed its layout or the URL is malformed". `curl-cffi` must stay installed for yt-dlp's proof-of-work webpage solver; TikTok extractions/downloads also get one extra no-auth retry. Don't feed shortlinks straight to yt-dlp again.
- **pyrogram `Peer id invalid`** is monkey-patched in `main.py`
  (`get_peer_type_patched`) and the log channel peer is resolved at startup
  (`app.get_chat`). Don't remove either.
- **Send-method logging** is monkey-patched in `main.py::patch_pyrogram_send_methods`
  and explicitly skips the log channel to avoid a self-logging loop.
- **Upload ceilings** (`utils/uploader_handler.py`): Bot API 2 GB / Premium 4 GB,
  with a *target* below the limit and a *hard* ceiling because the ffmpeg
  keyframe splitter can overshoot. Don't tighten target past the hard ceiling.
- **4 GB uploads are a per-user admin whitelist, gated on `is_premium_user`.**
  Bots are hard-capped at 2 GB by Telegram ("Bots can't be Premium users" —
  tdlib/telegram-bot-api#583); only a Premium *user* account over MTProto can
  send 4 GB. The 4 GB path is therefore only active when `PREMIUM_STRING_SESSION`
  is set, AND the recipient is Premium-whitelisted (Admin → 👑 Premium Uploads;
  the creator is always premium). The uploader takes an explicit
  `premium_allowed` flag (`None` → inferred from `is_premium_user(chat_id)`);
  the downloader locks >2 GB format buttons (🔒) for non-whitelisted users and
  rejects the >2 GB callback at `dl:` dispatch. Direct-forward relays pass
  `premium_allowed=True` (operator's own pipeline; the relay chat may differ
  from the creator id). Do NOT widen the 4 GB path back to global — the whitelist
  is the whole point.
- **The premium session string is generated in-chat, not on the terminal.**
  The 👑 Premium menu's **🔑 Generate Session** runs the interactive
  phone → code → (optional 2FA) login on a temporary **in-memory** pyrogram
  client (`utils/premium_session.py`) and exports `PREMIUM_STRING_SESSION`;
  💾 Save to .env writes it via `dotenv.set_key` (dotenv-style quoting — safe
  for `run.sh`'s parser) and refreshes `config.PREMIUM_STRING_SESSION` in memory.
  The gen flow lives in `modules/admin.py` (`PREMIUM_GEN`,
  `waiting_for_premium_phone/_code/_password`, callbacks
  `admin_premium_gen/_abort/_save`); its free-form text states are dispatched
  **before** the `is_valid_telegram_id` gate. Every step shows an
  **❌ Abort Session Generation** button, and the temp client is disconnected on
  completion/abort/`/start` escape/TTL expiry (`sweep_stale_generations` is
  driven by `utils/keyboard_expiry.expiry_loop`) — a dangling temp login is a
  bug. Do not re-add a terminal `generate_session.py`; keep the flow in the
  console. **`register_admin_handlers(app)` names its closure param `app`, not
  `client`** — inner closures like `_premium_gen_cleanup` must use `app` (or
  accept `client` as their own param); referencing `client` at that scope raises
  `NameError` on every callback and the button silently dies.
  **The login code is entered via a numeric dial pad** (`_gen_dial_pad_markup`,
  `admin_premium_gen_digit:<d>`/`_bksp`/`_enter`), never as chat text — Telegram's
  anti-account-sharing detection expires any code that is typed into a chat
  message (`PHONE_CODE_EXPIRED`, "code was previously shared by your account").
  `waiting_for_premium_code` therefore *rejects* typed codes; only the 2FA step
  stays free-form text. **Saving the string auto-restarts the bot**
  (`main.py::schedule_self_restart`): under systemd it SIGTERMs its own PID, the
  existing `_on_sigterm`→`KeyboardInterrupt` path does the graceful teardown, and
  `Restart=always` relaunches `run.sh` (re-reading `.env`) — no SSH/`systemctl`
  needed. Do not print a manual restart instruction here. The same
  `schedule_self_restart` powers the admin console's **🔄 Restart Bot** button
  (`admin_restart` → confirmation → `admin_restart_confirm`), so the operator can
  reboot the bot entirely from chat.
- **`RUNTIME_SETTINGS`** in `utils/shared.py` only holds `max_cache_age_hours` and
  `max_disk_usage_pct` — housekeeping knobs, NOT upload-size knobs (the 2 GB / 4 GB
  boundary is picked per-file in the uploader). Do not add Bale's `bale_hard_limit_mb`
  etc.; they don't apply to Telegram.

## When porting from balebot

balebot is a leaner, Bale-only variant of tgbot's own design (not a reference
ahead of tgbot). Porting *from* it to tgbot is only ever bringing a Bale-only
feature over, or the reverse — extracting shared core back out. When translating
aiogram↔pyrogram:
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

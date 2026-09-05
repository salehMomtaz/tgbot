# AGENTS.md — notes for contributors & AI agents working on tgbot

This is a pyrogram-based Telegram media downloader/streamer — **tgbot is the
primary bot and the reference implementation**. The **Bale frontend**
(`modules/bale/`, aiogram v3, Bale.ai) is NOT an experiment — it is the bot's
**lifeline during internet shutdowns in Iran**: Bale runs over the National
Information Network and stays reachable domestically when the global internet
is cut (2025-26 protests blackout from 8 Jan 2026, renewed blackout from the
28 Feb 2026 strikes, partial restore only 26 May; permanent "digital
isolation" plans reported — see `docs/memory/BALEBOT.md` §"WHY this frontend
exists"). It shares the same download core with tgbot (derived from tgbot's
design), but tgbot is the source of truth. Design + invariants:
`docs/memory/BALEBOT.md`. This file captures the **non-obvious
invariants** so you don't have to rediscover them.

## Orientation

- **Framework:** pyrogram (NOT aiogram). Handlers are registered on a `Client`
  with ordered groups. Groups iterate **numerically** (`dispatcher.py` sorts
  them), so the live layering is:

  | group | what lives there |
  |---|---|
  | `-2` | log interceptors (`main.py`: every private message + every callback query), each ending in `continue_propagation()` |
  | `-1` | security gate (`modules/admin/register.py:49`, matches **all** private messages) |
  | `0` | **default** — admin message handlers + admin state machine, the ported extras (github / youtube / translate / web), subscription commands, the stream interceptor, the `on_raw_update` Stars pre-checkout handler, **and most callback dispatchers** (`^admin_`, `^fm_`, `^dl:`, `^pl:`, `^pln:`, `^plx:`, `^sub:`) |
  | `1` | text router — admin `admin_start_text_handler` **and** the downloader's `text_link_handler` |
  | `2` | *secondary* callback group — only `^chkjoin:` and `^gh:` |

  Note the two non-obvious consequences: (a) **most callbacks are group 0, not
  group 2** — "group 2 = callback dispatcher" is only true for the two handlers
  above; and (b) the downloader's link handler is **group 1**, which is why a
  group-0 extra that calls `stop(message)` (e.g. a `github.com` link) reliably
  preempts the direct-file fallback. `message.stop_propagation()` /
  `continue_propagation()` / `raise ContinuePropagation` control flow.
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
| Change download/extract logic | `utils/downloader/` (see sub-modules below) |
| Change upload/splitting | `utils/uploader_handler.py` |
| Change the PO-token provider lifecycle | `utils/pot_provider.py` |
| Add an admin-console feature | `modules/admin/` (see sub-modules below) |
| Change the admin **WebApp** console (Mini App) | REMOVED 2026-08-26 — do not re-add without an explicit ask |
| Change how links/messages are handled | `modules/downloader_handler.py` |
| Change playlist tiers / detection / per-video download | `utils/downloader/playlists.py` (`PLAYLIST_TIERS`), `utils/downloader/url_normalize.py` (`is_playlist_url`), `utils/downloader/playlists.py` (`extract_playlist_meta`), `utils/downloader/download.py` (`download_media(format_selector=...)`) |
| Change cookie lifecycle (snapshot/merge/freshness) | `utils/cookie_manager.py` (+ call sites in `utils/downloader/cookies.py`, `utils/downloader/download.py`) |
| Change streaming | `modules/stream_handler.py` / `stream_interceptor.py` |
| Change logging / log channels / truncation | `utils/logger/` package (`local.py`, `telegram.py`, `bale.py`; re-exports from `utils/logger.py`) |
| Change subscription / channel-join verification / greeting | `modules/subscription/join.py` (`_greeting_text`, `build_greeting_keyboard`, `register_join_handlers`) + `utils/subscription/access.py` |
| Change install/provisioning | `install.sh` / `run.sh` / `deploy/tgbot.service` / `deploy/tgbot-monitor.service` / `deploy/tgbot-xchat-bridge.service` |
| Change system monitoring / health reports | `cmd/tgbot-monitor/` (Go binary → `build/tgbot-monitor` via install.sh) + `utils/system_monitor.py` spawner |
| Change DM relay (IG/X → Telegram) | `modules/direct_forward/` (see sub-modules below) + `.env` (`DIRECT_FORWARD_*`) + `xchat_bridge.mjs` / `tools/start_xchat_bridge.sh` (XChat E2EE sidecar) |
| Change GitHub explorer / YouTube search / Translate / Web Markdown (balebot extras) | `modules/github/`, `modules/youtube/`, `modules/translate/`, `modules/web/` (all pyrogram `group=0/2` handlers; see `docs/memory/BALEBOT.md`) |
| Change Bale.ai frontend (government messenger, optional) | `modules/bale/` (`runner.py` aiogram poller `tapi.bale.ai` + `uploader.py` 20MB split + `admin.py` LIMITED console) + `config.py` (`BALE_TOKEN`, `BALE_SYSTEM_CREATOR_ID`, `BALE_HARD_LIMIT_MB`) — see `docs/memory/BALEBOT.md` |
| Change Friend Media Archiver (profile pics/stories/IG archive of friends) | `modules/friend_media/` (`admin.py` console + state persistence, `telegram.py` photos/stories via premium account, `instagram.py` cached instagrapi client, `state.py`, `common.py`) + `.env` (`FRIEND_MEDIA_*`) — see `docs/INFRA.md` |

### `utils/downloader/` package (replaces `utils/downloader.py`)

| Want to… | Edit |
|---|---|
| Cookie resolution & YouTube diagnosis | `utils/downloader/cookies.py` |
| URL normalization (TikTok shortlinks + `/embed/` rewrite, IG highlights) | `utils/downloader/url_normalize.py` |
| Size estimation, CDN probes, disk space | `utils/downloader/sizing.py` |
| yt-dlp error classification | `utils/downloader/errors.py` |
| Format extraction & sorting | `utils/downloader/formats.py` |
| Playlist metadata & tier selectors | `utils/downloader/playlists.py` |
| Thumbnails, ffmpeg metadata, video probing | `utils/downloader/thumbnails.py` |
| Single-media download pipeline | `utils/downloader/download.py` |
| Binary & video splitting generators | `utils/downloader/split.py` |
| yt-dlp extractor pattern compilation & URL matching | `utils/downloader/supported_sites.py` |

### `modules/admin/` package (replaces `modules/admin.py`)

| Want to… | Edit |
|---|---|
| Keyboard builders (console, premium, cookies, PO, direct) | `modules/admin/keyboards.py` |
| In-chat Premium session generation flow | `modules/admin/premium_gen.py` |
| Cookie jar validation & atomic write | `modules/admin/cookies.py` |
| Live cookie-jar test (yt-dlp probe) | `modules/admin/cookie_test.py` |
| PO Token Provider menu & actions | `modules/admin/pot_menu.py` |
| Direct-Forward menu rendering | `modules/admin/direct_menu.py` |
| Callback query dispatcher (admin UI) | `modules/admin/callback_dispatch.py` |
| Handler registration & text/command routing | `modules/admin/register.py` |
| Module-level state (USER_STATES, PREMIUM_GEN, etc.) | `modules/admin/state.py` |

### `modules/admin_webapp/` — REMOVED (2026-08-26)

The Telegram Mini App admin console webapp (and the subscription webapp) were
removed: `modules/admin_webapp/`, `modules/subscription/webapp.py`,
`utils/webapp_auth.py` are deleted, and their mounts/buttons/commands were
stripped (`🌐 WebApp Console` button, `admin_sub_webapp` handler, `/admin_token`).
The FastAPI/uvicorn server still runs for the **streaming endpoints only**
(`modules/stream_handler.py::fastapi_app`) — the `/stream` links the downloader
hands out depend on it. Do not re-add a webapp Mini App without an explicit ask.

### `modules/direct_forward/` package (replaces `modules/direct_forward.py`)

| Want to… | Edit |
|---|---|
| State management (cursors, pairing, merge-only saves) | `modules/direct_forward/state.py` |
| Shared constants & delivery helpers | `modules/direct_forward/common.py` |
| Instagram DM worker (instagrapi) | `modules/direct_forward/instagram.py` |
| X/Twitter self-DM worker (twikit) | `modules/direct_forward/twitter.py` |
| TikTok IM WebSocket push worker | `modules/direct_forward/tiktok.py` |
| Supervisor (starts all enabled workers) | `modules/direct_forward/supervisor.py` |

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

 3. **YouTube = cookies + PO token, no fallback.** `utils/downloader/url_normalize.py::
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

   **Cookie history tracker (2026-09-04):** every jar change and every IG
   session-health event is appended to `cookies/history.jsonl`
   (`utils/cookie_history.py`; content-changing events also keep a full
   snapshot copy under `cookies/history_snapshots/`, rotated to 40 per jar).
   Events: `startup` (main.py), `admin_replace` (`_write_cookie_jar`, actor
   distinguishes operator upload vs headless refresher), `overlay`
   (instagrapi write-back), `merge` (yt-dlp write-back, with the rotated
   cookie NAMES), `commit_failure` (auth-classified errors), `restore`,
   and `ig_login_ok` / `ig_session_dead` / `ig_relogin_failed`. Values are
   NEVER logged — only `first4…last4(len)` fingerprints. View it in-chat:
   Admin → 🍪 Cookie Jars → *<jar>* → 📜 History (jar events + IG health
   interleaved, for correlating the first session-death with the nearest jar
   write). Do not remove these hooks when refactoring the cookie paths.

   **The headless cookie refresher must NEVER overwrite a jar it was logged
   out of (2026-09-04 fix).** The old refresher replaced the ENTIRE jar with
   whatever the headless Chromium context held after the visit. On 2026-09-03
   16:49 the IG session was dead, the visit produced an anonymous cookie set
   (fresh anonymous `sessionid`, login form), and the jar — with the dead
   sessionid — was replaced by it, permanently destroying the jar's contents
   ("site behaves like no cookies" in the operator's incognito test). Now
   `_refresh_one` gates every write: IG requires a `sessionid` in the
   extracted context AND no `/accounts/login` URL AND no anonymous login form
   in the DOM (`action="/accounts/login/ajax/"`); other sites refuse when the
   final URL looks like a login page. A refused visit records
   `refresher_refused` (with a snapshot of the untouched jar) and writes
   NOTHING; a verified visit applies the rotated cookies as an OVERLAY
   (`cookie_manager.overlay_cookies`, never a full replace — full replace was
   also shrinking jars 24→14 lines). Related fix: yt-dlp's Instagram ladder
   now escalates to cookies on the audience gate too — "It can't be seen by
   certain audiences" is treated like the HTTP-400 login-wall (it's what
   Instagram serves instead of a 400 for follower/age-restricted reels), and
   the reel's native fallback (`_ig_native_deliver_once` with
   `allow_clips=True`) actually delivers the clip video from the app API
   instead of no-oping to the preview image.

5. **Keep `[default,curl-cffi]` on yt-dlp upgrades AND pin `curl_cffi<0.14`.**
   `utils/updater.py` runs `pip install -U --pre "yt-dlp[default,curl-cffi]"
   "curl_cffi<0.14"` — plain `yt-dlp` would silently strip the certifi/curllib
   extras, and dropping `curl-cffi` removes the impersonation engine yt-dlp's
   TikTok proof-of-work challenge solver needs (its absence resurfaces as
   "malformed site" failures on TikTok). The `curl_cffi<0.14` pin is a HARD
   constraint that must stay: yt-dlp's TikTok extractor hardcodes
   `impersonate=True`, which resolves to curl_cffi's **newest** chrome target;
   curl_cffi >= 0.14 ships chrome142+ TLS fingerprints that TikTok blocks with a
   "Site Maintenance" page ("Unexpected response from webpage request",
   yt-dlp#17403). 0.13.x's newest chrome is 131 (136/133 are
   yt-dlp-deprioritized) and TikTok accepts it. `requirements.txt` pins
   `curl_cffi<0.14` + `curl-adapter==1.1.0` (curl-adapter 1.2.x needs
   curl_cffi>=0.14). If TikTok hardens chrome131, re-test newer targets before
   bumping the pin. The `--pre` channel is what keeps it on nightly.

5a. **ALWAYS track the newest stable kurigram release.** Upstream pyrogram
   is archived; **kurigram** is the actively-maintained drop-in fork
   (https://github.com/KurimuzonAkuma/kurigram). It is verified to ship
   under the `pyrogram/` namespace — `pip install kurigram` puts 8,037+
   files at `pyrogram/*.py` on disk (verified by inspecting
   `kurigram-X.Y.Z.dist-info/RECORD`), with NO `kurigram/` directory
   alongside. `import pyrogram` loads the kurigram code; the project's own
   README confirms: *"Kurigram is an actively maintained pyrogram fork
   … designed as a drop-in replacement for Pyrogram"* and its install
   example uses `from pyrogram import Client, filters`. The
   `Pyrogram-2.0.106.dist-info` left on disk is leftover metadata from the
   pre-migration install (commit `31f4dfd chore(deps): migrate from
   pyrogram to kurigram 2.2.24`); `pip show` reports BOTH
   `Pyrogram 2.0.106` and `Kurigram 2.2.25` as installed, but the runtime
   identity is kurigram (`pyrogram.__version__` returns the kurigram
   version string). POLICY: when a new stable kurigram ships
   (https://github.com/KurimuzonAkuma/kurigram/releases), upgrade on this
   box immediately: `pip install -U kurigram`, then verify `import
   pyrogram; pyrogram.__version__` shows the new number and the bot
   starts cleanly. `requirements.txt` pins `kurigram>=X.Y.Z` to the
   newest known good floor; do **not** re-pin to an exact older version
   — the floor's job is to keep the policy visible, not to block future
   upgrades. `utils/updater.py::auto_update_kurigram` is the auto-upgrade
   loop that runs alongside the yt-dlp nightly refresh. Verification on
   every upgrade: spot-check `pyrogram.types.InputPhoneContact.__new__`
   signature (must remain `(phone, first_name, last_name)`), the
   `pyrogram.errors.PeerIdInvalid` import (must still resolve), and the
   four filters the bot registers on (`private`, `text`, `command`,
   `regex`). If any of those break, pin to the previous good release
   and open an issue.

6. **`.env` parsing in `run.sh` must stay dotenv-style**, never `source .env`.
   Values like `YTDLP_USER_AGENT` contain characters bash treats as code. The
   line-by-line reader in `run.sh` is intentional. systemd has **no**
   `EnvironmentFile=` for the same reason — `run.sh` owns `.env` parsing.

7. **systemd unit is a template.** `deploy/tgbot.service` has `__USER__`,
   `__GROUP__`, `__PROJECT_DIR__`, `__MEMORY_MAX__` placeholders rendered by
   `install.sh` from the real user/path/RAM. Don't hardcode paths in the unit.

 8. **Playlist vs single-video are two distinct paths.** `utils/downloader/playlists.py::
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
    See `docs/DOWNLOADER.md`.

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
     `get_cookies_for_url` (utils/downloader/cookies.py) and `_site_cookie_context` look
    the site up from `urllib.parse.urlparse(url).netloc`, **not** a hardcoded
    switch — adding a new site is just dropping a `<site>.txt` in `cookies/ytdlp/`.
    Any pre-existing flat-root jars (`ytcookies.txt`, `igcookies.txt`, etc.) need
    `mv` into the new layout during deployment; the old paths are not honoured.
**Routing gate (2026-08-12):** the message-level switch is
     `is_social_media_link` (modules/downloader_handler.py) — a URL that
     does not match any compiled yt-dlp extractor `_VALID_URL` pattern
     (1,786 patterns, `generic` excluded —
     `utils/downloader/supported_sites.py`) falls into the **direct-file**
     path (plain HTTP GET, no format selection). The yt-dlp pattern set
     grows automatically with every yt-dlp upgrade — adding a new site
     requires no bot-code change, only (optionally) a per-site cookie jar
     at `cookies/ytdlp/<site>.txt`. The direct-file path has an SSRF
     guard (`_is_ssrf_target`): it refuses loopback/private/link-local
     destinations, protecting the 127.0.0.1 PO provider and internal
     services. Only `http://`/`https://` count as links (`is_link`) —
     `file://`, `ftp://` etc. are plain text and never downloaded.

13. **Direct-forward = DM relay: Instagram (dedicated bot account) + X (self-DM).**
     `modules/direct_forward/` (replacing the old saved/liked `auto_forward`)
    relays media into `DIRECT_FORWARD_CHAT_ID` on a jittered poll cadence
    (`DIRECT_FORWARD_POLL_SECONDS`, default 300 s **±
    `DIRECT_FORWARD_POLL_JITTER_PCT`%**, never a fixed machine cadence — fixed
    short polling is what got the first IG account flagged "automated
    behavior"). Instagram anti-detection posture, in order of weight:
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
    You DM media/links from YOUR account (IG: `IG_DIRECT_FROM_USERNAME`
    static pre-pair or the pairing handshake) to the IG bot account; the relay
    sends photos, videos, reels, story shares, tweet shares and plain links
    into `DIRECT_FORWARD_CHAT_ID`. Instagram uses `instagrapi` (sync → always
    via `run_in_executor`; bootstraps its login from the `sessionid` in the
    igcookies jar).     **There is NO password login fallback (removed 2026-08-26)**
    — password login hammered `accounts/login/` and deepened Instagram's 429
    rate-limit on the VPS IP, so `_ig_login` is sessionid-only (resume persisted
    session → `login_by_sessionid` from the jar → else raise, never credentials).
    The config keys `IG_DIRECT_USERNAME`/`IG_DIRECT_PASSWORD`/
    `IG_DIRECT_TOTP_SEED` were deleted from config.py + .env.example on
    2026-09-05 (nothing ever read them post-removal; stale values in a local
    `.env` are inert — delete them at will). The sessionid is either valid or
    it isn't; the ONLY recovery is the operator uploading a fresh jar.
    Each platform runs in its own
    contained loop (`try/except` per poll; LoginRequired → re-login once).
    **No third-party APIs.** Downloads route through the normal yt-dlp
    pipeline (with cookie jars — write-back keeps them warm) and enqueue on
    the shared single-worker queue behind interactive downloads (invariant
    #10). First run primes the cursor and skips backlog. State:
    `direct_forward_state.json` (+ `thread_activity` watermarks). The worker
    starts in `main.main_engine()` after the FastAPI server; a misconfiguration
    must never block the bot.
    **X = SELF-DM method, no pairing, no separate bot account (2026-08-08).**
    You send tweet links / photos / videos to your OWN X self-DM ("Message
    Yourself"); the worker polls that one conversation `<self_id>-<self_id>`
    authenticated with the SAME shared `cookies/twitter/xcookies.txt` jar
    yt-dlp downloads with (`_x_jar_cookies` → `_x_twid_user_id` from the
    `twid` cookie; `client.set_cookies`, no `client.login`). There is NO
    twikit session file — `direct_x_cookies.json` no longer exists; jar
    write-back keeps the session warm. `X_DIRECT_USERNAME`/`X_DIRECT_PASSWORD`/
    `X_DIRECT_EMAIL`/`X_DIRECT_FROM_USER_ID` are gone from config (removed).
    Tweet links/shares pick the HIGHEST quality automatically
    (`_x_deliver_tweet`: `extract_formats` → `videos[0]`, ceiling 2 GB bot /
    4 GB Premium; over-ceiling posts the format-selection keyboard, not a
    silent lower quality). Photo-only tweets (no video stream for yt-dlp) are
    delivered natively from the share's CDN URLs (`_x_share_media` +
    `_x_deliver_share_photos`). DM photo/video attachments are fetched through
    the authenticated twikit session (`_x_fetch_auth_bytes`) — the
    `ton.twitter.com` DM photo URLs 401 without cookies. Two hard-won gotchas:
    (a) **`_x_fetch_auth_bytes` MUST use a throwaway `httpx.AsyncClient`**
    (same base headers + a copy of the session cookie jar, closed after the
    fetch) — never the shared `client.http` with `follow_redirects=True`.
    ton.twitter.com is Cloudflare-fronted and its `Set-Cookie: __cf_bm` piles
    duplicate names into the shared jar, then the next `dm_conversation` poll
    dies with `httpx.CookieConflict: Multiple cookies exist with name=__cf_bm`
    and the worker goes permanently silent (`_x_fetch_self_messages` swallows
    the exception → returns `[]`). (b) **`_x_media_payload_ok` validates CDN
    payloads by MAGIC BYTES, never by size** — X serves legitimately tiny
    images (a 133-byte solid PNG is a real photo), so a `<500B` guard dropped
    real media. Only HTML interstitials are rejected.
    **X Chat E2EE is handled by the bridge — the passcode is now OPTIONAL, not
    forbidden.** The 2025 X Chat rollout (4-digit passcode, only when BOTH opt
    in) encrypts the self-DM so twikit's legacy DM API returns nothing. The
    Deno sidecar `xchat_bridge.mjs` decrypts it (`XCHAT_PIN` from `.env`),
    appends canonical lines to `cache/xchat_inbox.jsonl`, and the worker reads
    that file FIRST (`_x_read_inbox` → `_x_process_bridge_line`), falling back
    to the twikit poll only when no bridge output exists. The bridge runs as
    its own `tgbot-xchat-bridge.service` systemd unit (wrapper
    `tools/start_xchat_bridge.sh`, which parses `.env` dotenv-style — never
    `source` — and is a **resident supervisor** that re-reads `.env` every ~5 s,
    (re)spawning the Deno sidecar whenever `X_DIRECT_ENABLED` + `XCHAT_PIN` +
    the xcookies jar all hold; install.sh installs AND enables it, and it is a
    harmless sleeping no-op until configured). Because the wrapper self-reloads,
    X self-DM is fully activatable in-chat (Admin → 📨 Direct-Forward →
    🔑 Set X Chat PIN) with no ssh/systemctl.
    **TikTok = the same self-DM trick over the IM WebSocket (2026-08-10).**
     The worker (`modules/direct_forward/tiktok.py::_tiktok_worker`) holds a persistent
    async WS (`wss://im-ws-sg.tiktok.com/ws/v2`, library `websockets`) to the
    account's own IM store, authenticated by the SAME `cookies/tiktok/ttcookies.txt`
    jar yt-dlp downloads with (no separate bot account, no pairing — the self-DM
    conversation `0:1:{uid}:{uid}` is only reachable by the account itself).
    Connect = send a cmd-1001 `get_stranger_conversation_list` Frame (encoder:
    `_tt_connect_frame`); the server acks and pushes pending unread + live new
    messages as **cmd-500 NEW_MSG_NOTIFY** protobuf frames. Access key:
    `_tt_access_key(wid)` = md5 of `9{APP_KEY}{wid}f8a69f1719916z` where `wid`
    comes from the web-cookie-privacy config endpoint and `APP_KEY =
    e1bd35ec9db7b8d846de66ed140b1ad9`. Two hard-won gotchas: (a) the jar stores
    `ttwid` **URL-encoded** — unquote it before `urlencode` in the WS query or
    the socket is rejected with HTTP 400; (b) the pushed MessageBody carries
    **group wire types that proto3 rejects** — `_tt_parse_push` uses a tolerant
    byte-walker (`_tt_walk`) and only reads the wanted fields, never recursing
    into the JSON at field 8. Dedupe on `server_message_id` (field 3), not the
    ext `s:client_message_id`. **First run primes and skips backlog** (a 15 s
    `prime=True` connect consumes pushes without relaying, then the relay loop
    takes over) — mirroring X. Shares resolve the author via the public oEmbed
    endpoint (`tiktok.com/oembed?url=.../video/<itemId>` → `author_url`) and
    download through the normal yt-dlp pipeline, whose fresh-cookie retry
    ladder already handles TikTok's stochastic anti-bot challenge — **no
    headless browser is needed** (see the doc's headless-browser analysis).
    The bot's poll cadence is WS-push-driven, but reconnects still respect the
    jittered `TIKTOK_DIRECT_POLL_SECONDS` interval (via `_tt_poll_interval`),
    never a fixed cadence. `_tt_wid` and
    `_tt_oembed_author` are SYNC `requests.get` calls — they must always run
    through `loop.run_in_executor`, never inline on the event loop (a slow
    TikTok endpoint would freeze pyrogram + the IG/X workers). The reconnect
    knob is `_tt_poll_interval()` (TIKTOK_DIRECT_POLL_SECONDS/JITTER), NOT the
    shared `_poll_interval()` (DIRECT_FORWARD_*) used by IG/X — keep them
    separate.
    Full protocol + decode details: `docs/TIKTOK.md`.
    Bridge cursor semantics: its `last_seq` (in `cache/xchat_bridge_state.json`)
    lives in the SAME id space as the legacy DM ids, so the shared `x.last_id`
    cursor dedupes; first boot primes and skips backlog. **The worker live-
    reloads the jar every poll** (`_x_cookies_signature` hash-compare in
    `_twitter_worker`): a mid-run xcookies re-upload is re-applied via
    `client.set_cookies` on the next poll — no restart; if the `twid` changed
    (different account), it rebuilds the X client and re-primes the cursor.
    The watched conversation is `<twid-uid>-<twid-uid>`: the account that
    self-DMs MUST be the one whose session is in the jar, or its messages land
    in a thread the worker never reads (media silently never arrives).
    **`cache/xchat_bridge_state.json` + `cache/xchat_inbox.jsonl` are
    protected from the hourly cache cleaner** (`main.py::auto_clean_cache_directory`
    skip-list) — deleting the bridge state mid-run would make the bridge re-prime
    `last_seq` to newest and SKIP older messages (data-loss window). A photo-only
    pasted tweet (no video stream) is delivered natively from `_x_fallback_photos`
    — its PRIMARY path is a **raw `client.gql.tweet_detail` GraphQL walk** scoped
    to the focal `tweet-<id>` entry (`_focal_subtree`, mirroring twikit's own
    `get_tweet_by_id` matching), collecting `type == "photo"` media nodes via
    `media_url_https`/`media_url`. This bypasses twikit 2.3.3's `User.__init__`
    (reads `legacy['entities']['description']['urls']` /
    `legacy['pinned_tweet_ids_str']` WITHOUT `.get`, so `get_tweet_by_id` raises
    `KeyError('urls')` on some authors and aborts the whole call); the old model
    path is kept only as a secondary fallback. Focal scoping is REQUIRED — the
    tweet_detail response also contains thread replies/quote tweets, and a global
    walk over-collects photos that don't belong to the shared tweet. If even that
    finds nothing, `_x_deliver_tweet` sends a text-only note instead of failing
    the queue task. See `docs/memory/DIRECT_FORWARD_HISTORY.md`.

    **State file = shared across the three direct-forward workers; save
    merge-only (2026-08-11).** `direct_forward_state.json` is written by
    three concurrent coroutines (IG `_instagram_worker`, X `_twitter_worker`,
    TikTok `_tt_run_ws`/`_tiktok_worker`), each doing read-modify-write of the
    WHOLE dict. A full-dict `_save_state(state)` from any worker lets a stale
    in-memory snapshot clobber another platform's `last_id` — the IG worker
    once held its boot-time copy for the whole process and reverted X's
    cursor on every save, so the entire X self-DM backlog re-relayed in waves
    after each IG poll (the "X posts received 2× then 4×" incident; see
    `docs/memory/DIRECT_FORWARD_HISTORY.md`).
    Therefore: **never call `_save_state(state)` from a worker.** Always
    persist through `_state_save_owned(state, {own_platform})` (async workers)
    or `_merge_state_save(state, {own_platform})` (sync admin pairing helpers)
    — both re-read the freshest on-disk state and apply ONLY the caller's own
    platform section, then refresh the caller's in-memory dict. The helpers
    are deliberately synchronous (cannot be interleaved on the event loop)
    and serialized by `_STATE_LOCK` in the async variant. Keep all cursor
    bumps inside the owned write. The only remaining `_save_state` caller is
    inside `_merge_state_save` itself — keep it that way.

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
     rationale: `docs/INFRA.md`; full design:
     `docs/INFRA.md`. Tests:
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
     `utils/downloader/thumbnails.py::_find_thumbnail_file` (extension list **plus** a
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

18. **`best_audio_format_id` must prefer the ORIGINAL-language track on
    multi-audio videos.** Multi-audio YouTube videos expose 7+ parallel audio
    tracks per itag; the extractor marks each with `language_preference`
    (10 = original, 5 = default, −1/unset = dubbed) and yt-dlp's own
    `bestaudio` sorts by it. The bot's single-video merge uses its OWN
    `best_audio_format_id`, so `extract_formats` sorts audio options by
     `(language_preference, bitrate)` both descending (`utils/downloader/formats.py`) —
    originals first, then default, then dubs, bitrate only within a class.
    Do NOT collapse back to a pure-bitrate sort: on dubbed videos the original
    is usually the LOWEST-bitrate track, so a bitrate-only sort merges a Hindi
    AI dub into the video (see `docs/DOWNLOADER.md`).
    The `+bestaudio` fallback selectors and `PLAYLIST_TIERS` are already
    language-aware via yt-dlp's sort — leave them alone.

19. **`stop_propagation()` / `continue_propagation()` must NEVER be wrapped in a
    bare `except Exception`/`except: pass`.** Both `pyrogram.StopPropagation`
    and `pyrogram.ContinuePropagation` are **`Exception` subclasses**, so a
    `try: message.stop_propagation() except Exception: pass` silently swallows
    the signal and the dispatcher never acts on it. Consequences, depends on
    where it happens:
    - **group 0**: the handler returns normally → dispatcher `break`s the group
      and control flows to the *next* group, so a link handled in group 0 (e.g.
      the GitHub explorer) is ALSO grabbed by the group-1 downloader as a
      direct-file upload → duplicate replies/processing.
    - **group 1 greeting**: `/subscription` appears to "double-greet" (a
      swallowed stop lets the command fall through to the welcome path; only the
      group-1 `/`-swallow guard masked it).
    Correct pattern — use the shared helper `utils/propagation.py`:
    `from utils.propagation import stop, continue_` then `stop(message)` /
    `continue_(message)`. The helper re-raises the real signal while still
    swallowing genuinely-unexpected (non-propagation) errors. Applies to every
    handler in `modules/{admin,subscription,github,youtube,translate,web}/`.

20. **A `RawUpdateHandler` placed in the MIDDLE of a handler group starves every
    handler registered AFTER it in that group.** pyrogram's dispatcher treats any
    `RawUpdateHandler` (e.g. an `@app.on_raw_update(...)` for Stars
    pre_checkout) as matching *every* update; if its callback returns normally
    the dispatcher `break`s the whole group, so later handlers in the SAME group
    never see the update. This is why the ported extras (`/tr`, `/yt`,
    `/search`, github links, `/web`) silently ignored all input: they were
    registered after the `on_raw_update` Stars pre_checkout handler in group 0.
    A raw handler that does not own the current update MUST `raise
    pyrogram.ContinuePropagation` so the group iterator keeps going (see
    `modules/subscription/handlers.py::_raw_precheckout`). Deploy a new
    `on_raw_update` in a shared group with this in mind — prefer an isolated
    group or a raise-on-mismatch.


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
  `direct_forward_state.json`, `direct_ig_session.json`, `*.autobak`. Anything
  that contains a session token must stay under one of
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

`tgbot-xchat-bridge.service` (the XChat E2EE sidecar) is the one unit
`install.sh` DOES enable automatically. Its wrapper
(`tools/start_xchat_bridge.sh`) is a **resident supervisor**: it re-reads `.env`
every ~5 s and (re)spawns the Deno sidecar when the gates pass (X relay enabled
+ `XCHAT_PIN` set + xcookies jar present), and stops it when any gate drops.
This is what makes X self-DM fully activatable from the bot console — the admin
toggles X and enters the PIN in-chat (Admin → 📨 Direct-Forward → 🔑 Set X Chat
PIN, a `waiting_for_x_pin` free-form state that `dotenv.set_key`s the value), and
the wrapper picks up the `.env` change with **no ssh/systemctl**. When
unconfigured it simply sleeps — a harmless no-op instead of a crash loop — and
it "just works" the moment `XCHAT_PIN` is set. Don't disable it. `tgbot-monitor.service` is installed but disabled by
default (the bot spawns a detached monitor at startup); enabling it makes the
monitor survive reboots unconditionally.

## Logging

The logger is a Python package `utils/logger/` (split out of the old `utils/logger.py`
monolith; re-exports keep `from utils.logger import ...` working):
`local.py` (`ensure_local_log_handler`), `telegram.py`
(`TelegramChannelHandler` → `LOG_CHANNEL_ID`), `bale.py`
(`BaleChannelHandler` → `BALE_LOG_CHANNEL_ID`). All channel handlers send **rich
messages** (`sendRichMessage`, `rich_message: {"html": ...}`) with a `sendMessage`
fallback (byte-compatible with the pre-rich format) and truncate at **32768**
chars (Rich Bot API limit — NOT 3500/6000/8000; those premature cuts broke log
lines, fixed in `d2c3dcf`).

Root logger gets two handlers (`main.py::setup_system_logger`): the
`TelegramChannelHandler` (→ `LOG_CHANNEL_ID`) and a local rotating file mirror
(`logs/bot.log`, 5 MB × 3). Both only attach when `LOG_CHANNEL_ID != 0`; the file
mirror is added regardless inside `ensure_local_log_handler`. New code should use
`logging.getLogger(__name__)` / `await log_event(...)`, not `print`.

**Strict split (`d723798`):** the main `bale_log` Telegram channel gets ONLY
Bale/aiogram logger lines, while the regular Telegram log channel gets
pyrogram/direct-forward/queue lines — both at the same INFO level. Don't merge
the streams. The split lives in `utils/logger/bale.py` (a `bale.`-prefixed child
logger) and is what lets the Bale frontend be monitored without drowning the
Telegram channel in duplicate Bale noise.

### Why the bot's logger is NOT written in Go (unlike the system monitor)

The Go monitor exists because it must **outlive the bot** (report even when the
bot is dead). The logger has the opposite requirement: it lives *inside* the bot
process by design, and should die with it. The logger is a Python `logging`
handler (`utils/logger/telegram.py::TelegramChannelHandler`) invoked by the root logger
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
- **TikTok video URLs are rewritten to the `/embed/<id>` page (yt-dlp#17403).** Since the anti-bot challenge started intercepting the main `www.tiktok.com/@user/video/<id>` webpage fetch ("Unexpected response from webpage request"), `normalize_url` additionally converts canonical TikTok video URLs to `https://www.tiktok.com/embed/<id>` via `_to_tiktok_embed_url`, which serves challenge-free JSON. `_apply_pot_options` sets a Chrome 140 `User-Agent` via `http_headers` for embed URLs (never `user_agent`, the Python API ignores it), and `download_media` skips cookies for embed URLs (`not is_tiktok_embed`). This mirrors the direct-forward fix already shipped in commit `e48b060`. Do NOT add further workaround layers — if TikTok hardens the embed page too, wait for the upstream yt-dlp fix; the embed rewrite is a hedge, not a guarantee. Full writeup: `docs/TIKTOK.md`.
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
   The gen flow lives in `modules/admin/` (`PREMIUM_GEN`,
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
- **`from main import X` must never re-execute main.py.** Running as a script,
  main.py is `__main__`; a submodule importing `main` would execute a SECOND
  module copy whose pyrogram Clients were never started ("Client has not been
  started yet"). Guard: `sys.modules.setdefault('main', sys.modules[__name__])`
  immediately after the imports in main.py. Consequence: the `if __name__ ==
  "__main__":` entry block MUST stay at the VERY BOTTOM of main.py (after
  `schedule_self_restart` is defined) — a partially-initialized duplicate
  module otherwise breaks `from main import schedule_self_restart` and the bot
  crash-loops on every start (seen: ~31 restarts during friend-media testing).
- **aiogram's `start_polling` installs its own SIGTERM handler** and would
  overwrite main.py's `_on_sigterm`, so SIGTERM only stopped Bale polling and
  the process hung alive (Restart Bot button dead, systemd restarts hung).
  `modules/bale/runner.py` therefore passes **`handle_signals=False`** — keep it.
  On this box there is no passwordless sudo; restart via
  `kill -TERM $(systemctl show tgbot --property=MainPID --value)` and let
  `Restart=always` relaunch (~20 s).
- **Friend Media Archiver (`modules/friend_media/`) NEVER messages anyone** —
  the only friend-touching call is a silent `add_contact`. Reads ride the
  premium user session (`premium_app`), delivery goes through the BOT to the
  configured destination. TM delivery = the premium user account uploads to
  `LOG_CHANNEL_ID` (archive), then the BOT `copy_message`s it to the creator DM
  — `copy_message` re-uses the file_id (no re-download, no size limit) and
  carries NO "Forwarded from" header (sender shows as the BOT), and it can
  quote-reply to an existing bot-chat message. Do NOT use `forward_messages`
  (it stamps the "Forwarded from" header). This mirrors the >2 GB premium-upload
  path in `utils/uploader_handler.py::_stage_and_relay`. All state lives in
  `cache/friend_media_state.json` (exempt from the cache cleaner): seen
  photo/story/IG-pk ids + IG posts watermark — the first IG run primes the
  watermark and delivers NOTHING (never fetch older IG content). Archives
  serialize behind one asyncio.Lock; the watcher task starts unconditionally
  and self-gates each cycle on live `FRIEND_MEDIA_ENABLED` /
  `FRIEND_MEDIA_SCHEDULE_MINUTES` (default 60 min, jittered sleep, no fixed
  cadence). Console: friends list is SPLIT into "TG Friends" / "IG Friends"
  (derived from `telegram_user_id` vs `ig_username`); each list has ☑️ Select
  (multi-select) + 🗑 Delete all. Per-friend "🗂 Archive (zip)" downloads the IG
  profile pic + posts/reels + highlights (with per-step jitter) into a zip. IG
  client re-fetches the jar sessionid and retries once on a mid-run session
  rotation (`_ig_client_retry`). Console settings persist via
  `dotenv.set_key('.env')` + `setattr(config)`. Kurigram treats digit-string
  ids as PHONE numbers — pass user ids as `int`. `search_contacts` returns a
  `FoundContacts` object (use `.users`, not a bare list).
  **IG circuit breaker (2026-09-05):** when the IG session dies
  auth-classified (`LoginRequired` / redirect-loop / checkpoint —
  `_ig_auth_failure`), `modules/friend_media/instagram.py::_ig_breaker_trip`
  grounds ALL friend-media IG calls for 1 h (one probe per cycle, re-arms the
  moment the jar's mtime changes = operator re-upload). Without it a dead
  session meant 12 friends × 2 login attempts × 4 private-API 403s per hour
  (~100 auth'd requests/h of checkpoint fuel — the 02:55 incident). Mid-archive
  bursts abort on the first auth failure or 3 consecutive non-auth highlight
  failures. The operator gets ONE DM per dead-session streak
  (`_IG_DEAD_ALERTED` in admin.py), and `archive_instagram_full`'s remaining
  posts/highlights are skipped, not hammered. Related: instagrapi's
  dead-session fallback probe (`user_stream_by_id_v1`) logs ERROR+traceback
  per attempt — `_install_login_noise_filter` (utils/ig_anti_detect.py,
  installed from `install_transport`) drops exactly those records so the log
  channel carries OUR single actionable line instead of 158 tracebacks.
  Do NOT remove the breaker or "simplify" it back to per-friend retries.
  **Direct-forward photo routing is magic-bytes, not extensions**
  (`sniff_image_extension` / `normalize_photo_file` in
  modules/direct_forward/common.py, used by `_download_and_deliver` and
  `_x_deliver_tweet`): yt-dlp photo posts can carry no/bogus extensions, and
  the old 320×320+extension heuristic misrouted them into `send_photo` →
  `[400 PHOTO_EXT_INVALID]` (TikTok photo share, 2026-09-04 13:39). Sniff
  first; rename the file to the sniffed extension before send_photo.
  **IG archiver pacing (2026-09-05, operator requirement — do not tighten):**
  the archiver is explicitly NOT time-sensitive. While `archive_instagram_full`
  runs it opens a pacing window (`pace_window_enter`, refcounted, closed in a
  `finally`); the client's `private_request` is wrapped once per build
  (`_pace_gated` in `_build`, installed AFTER `install_token_echo`) so EVERY
  private-API call during the window — including `user_medias_v1`'s internal
  pagination, which `delay_range` does NOT pace — first sleeps a random pause
  in `FRIEND_MEDIA_ARCHIVE_PACE_MIN..MAX` (default 4-10 s). Per-item/
  inter-highlight sleeps scale from the same range (items 1.5-6 s, between
  highlight reels 2-10 s, ALWAYS between reels — the 02:55 incident ran
  failing highlights back-to-back with no gap). Hourly story/post cycles get
  bumped per-item bases (1.2-2.7 s / 1.5-3.6 s) and `_run_archives` inserts a
  `FRIEND_MEDIA_FRIEND_GAP_MIN..MAX` (default 20-60 s) pause between two
  consecutive friends that both have IG work. A full archive of a friend with
  many posts/highlights can take 30+ minutes — that is by design, not a bug.

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
- The balebot extras (GitHub explorer, YouTube search/recent/channel/transcript,
  Google Translate, web→Markdown) are ALREADY ported into this repo as shared,
  transport-free modules under `modules/github|youtube|translate|web/` — used by
  the Telegram pyrogram side AND the Bale `modules/bale/runner.py`. Do not
  duplicate an extra on the Telegram side; extend the shared module and let both
  endpoints keep one copy.

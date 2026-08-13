# tgbot ↔ balebot merge + subscription greeting fix — 2026-08-13

## Subscription double-reply bug (reported as “/subscription then greeting again”)

**Symptom:** unauthorized test account sends `/subscription` → receives the correct tier keyboard, **then immediately** a second `👋 Welcome…` greeting.

**Root cause:** pyrogram handler groups. `register_subscription_handlers` registered
`/subscription`, `/quota`, `/admin_token` at `group=0`. The generic welcome handler
`admin_start_text_handler` lives at `group=1` with filter `filters.text & private`.
A `/subscription` message matches **both**: group 0 handled it and replied, but **never called `message.stop_propagation()`**, so the dispatcher continued to group 1 and the greeting fired a second time.

That pattern was already correct in `modules/admin/register.py` (state handler calls
`stop_propagation()` after consuming a state) and in `modules/downloader_handler.py`'s
download flow — the subscription module simply omitted it.

**Fix:**

- `modules/subscription/handlers.py::register_subscription_handlers` — every command
  handler now calls `message.stop_propagation()` after replying (and `admin_token`
  also stops when the caller is not creator).
- `modules/admin/register.py::admin_start_text_handler` — added a **defensive early
  guard**: any `text.startswith("/")` where `cmd not in ("/start",)` immediately
  calls `stop_propagation()` and returns without sending a greeting. This makes a
  missed `stop` in any future `group=0` command harmless; `/start` remains the only
  command intentionally handled by the greeting path (console vs welcome).

Group diagram after fix:
```
-2 log interceptor  → continue
-1 security gate    → continue if authorized / sub-mode, else blacklist + stop
 0 state machine / subscription / github / youtube / translate / web (commands)
     each calls stop_propagation() after replying
 1 text router (link vs console vs welcome) — never sees a command already stopped
 2 callback dispatcher (dl:, pl:, sub:, gh:, admin_)
```

## balebot → tgbot integration

### Why

The sibling repo `https://github.com/salehMomtaz/balebot` (aiogram v3, Bale.ai HTTP
`tapi.bale.ai`) was an experiment to try Bale messenger bots. It shares the same
download core as tgbot (yt-dlp strategy, cookie jars, PO provider) because it was
**derived from tgbot**, not the other way. Bale is an inferior Telegram clone
(~v6 Bot API, lie-documented 50 MB limit → actually 20 MB since 2024,
`sendVideo` MPEG4-only, no Premium 4 GB, no HTML, no rich tables/drafts,
no streaming). Telegram is supreme. So the merge direction is
**balebot extras → tgbot**, never the reverse. Design doc:
`docs/memory/tgbot-balebot-integration.md`.

### What was ported

Balebot had four pure-HTTP feature modules with no Bale transport dependency:

| Bale module | Feature | New location (pyrogram) | Telegram command |
|---|---|---|---|
| `modules/github/` (1101 lines) | GitHub explorer: repo link → control panel, branches/tags/releases/issues/PRs/discussions/commits/contributors/info/languages/license/clone/readme/files explorer + folder ZIP | `modules/github/{api,keyboards,handlers}.py` | paste `github.com/owner/repo`, `github.com/.../issues/123`, `gist.github.com/...`, `/search`, `/user`, `/trend` |
| `modules/youtube/` | YouTube search + channel recent + channel search + transcript extraction | `modules/youtube/{scraper,handlers}.py` | `/yt`, `/ytrecent`, `/ytch`, `/transcript` |
| `modules/translate/` | Google Translate `gtx` API | `modules/translate/{api,handlers}.py` | `/tr src:dst text` |
| `modules/direct_dl/` | Webpage → Markdown via urltomarkdown | `modules/web/{api,handlers}.py` | `/web <url>` |

All four were rewritten from **aiogram Router/F** to **pyrogram Client + ordered groups**,
kept small and modular (one concern per file). No file exceeds ~350 lines.

### Key translation decisions

- Framework: `Router + F` → `app.on_message(filters.command(...) & private, group=0)` +
  `message.stop_propagation()` + `on_callback_query(filters.regex(r"^gh:"), group=2)`.
  The group numbers mirror the existing tgbot pipeline (subscription is also group 0,
  downloader is group 1, callbacks are group 2) so ordering stays predictable.
- GitHub link interceptors run at **group 0 with a `filters.create(lambda ... RE)`**,
  **before** the downloader's `group=1` link handler. That matters: `is_social_media_link`
  would otherwise route `github.com/...` as a direct-file HTTP GET (github is not a
  yt-dlp site) and fetch raw HTML. At group 0 the explorer consumes the link and
  stops propagation, so the downloader never sees it.
- Upload path: balebot's `operators/uploader.py` bypassed aiogram and POSTed
  `multipart/form-data` to `tapi.bale.ai` with a 39 MB split (Bale hard limit 20–50 MB).
  **Not ported.** Telegram uses pyrogram-native `send_video/send_audio/send_document`
  with `utils/uploader_handler.py::process_split_and_upload` (Bot 2 GB / Premium 4 GB,
  target 1900/3900 MB, hard ceiling 2000/4000 MB, ffmpeg keyframe splitter). Every
  GitHub ZIP / folder ZIP / transcript / web Markdown delivery uses that path via
  `queue.add_task(user_id, status_msg, job)` — the single worker serializes uploads,
  status edits are coalesced by `progress_bar_handler`.
- Config: added `GITHUB_TOKEN` (optional) to `config.py` + `.env.example`. Empty →
  anonymous rate-limited (60/hr); with PAT → 5000/hr + private repos. The operator's
  real token (`ghp_…`) stays in `.env` (git-ignored) — never committed.
- File partitioning: balebot exposed an admin knob `split_target_mb / bale_hard_limit_mb`
  because Bale's 20 MB ceiling demands aggressive splits. **Not ported to Telegram.**
  `RUNTIME_SETTINGS` stays `{max_cache_age_hours, max_disk_usage_pct}` only (AGENTS.md
  invariant); Telegram's 2 GB/4 GB ceiling is chosen per-file by the uploader.
- Bale limits explicitly documented and **not** applied to Telegram:
  `docs` unchanged except for this file + blueprint/README notes. Bale's Toman
  payment + toman Stars equivalent is irrelevant on Telegram (Telegram uses Stars +
  TON). No payment code was ported.

### Not ported (intentionally)

- Bale payment (`docs.bale.ai/#پرداخت`, Toman) — Telegram has Stars/TON already.
- Bale's 20 MB upload ceiling + `sanitize_filename_for_bale` / `clean_caption_text`
  Markdown-punctuation stripping — Telegram keeps native filenames/captions.
- `⚙️ Set Size Limits` admin knob — Bale-only, gated behind Bale frontend flag per
  integration doc §4.2.
- Docker/bale-specific `deploy/balebot.service` — tgbot keeps single `tgbot.service`.

### Wiring

`main.py::main_engine()` now registers the four extras after the subscription
handlers, each in a `try/except` so a single module failure never blocks the bot:

```py
register_github_handlers(app, premium_app)
register_youtube_handlers(app, premium_app)
register_translate_handlers(app)
register_web_handlers(app, premium_app)
```

All four use `filters.private` so they are private-chat only, matching the rest of
the bot. Authorisation reuses `utils/gate.is_authorized` (security gate already
blacklists strangers; handlers also check before queuing). Subscription quota is
**not** decremented for these explorer jobs — they are not “downloads” in the
subscription sense. If quota enforcement is later desired, hook `gate_and_quota_check`.

### Deployment

- `install.sh` unchanged — `aiogram` remains a Bale-only dep; new modules use
  `aiohttp`/`yt_dlp`/`pyrogram` already in `requirements.txt`. No new system
  package.
- `.env.example` updated (`GITHUB_TOKEN`).
- Existing `tgbot.service` / `run.sh` dotenv parser already handles `GITHUB_TOKEN`.
- Update on VPS: `git pull origin main && sudo systemctl restart tgbot`
  (no `install.sh` re-run needed).

### Verification

```
python3 -m py_compile $(git ls-files '*.py')  # → no output (clean)
bash -n install.sh run.sh uninstall.sh
cd cmd/tgbot-monitor && go test ./...
```

Manual: as the test account, `/subscription` now shows **one** reply only;
`/search python`, `/yt hello`, `/tr fa:en سلام`, `/web https://example.com`,
and a `https://github.com/salehMomtaz/balebot` link each reply without a trailing
greeting. As creator, `console` still shows the Admin Console.

### Secrets hygiene

The operator supplied real Bale secrets (`BALE_TOKEN`, `SYSTEM_CREATOR_ID`,
`LOG_CHANNEL_ID`, `GITHUB_TOKEN`) for local testing. They were **never written to
tracked files** — only used to populate the machine's `.env` (git-ignored). Verified
with `git check-ignore -v .env` and `git ls-files | grep -E '\.(env|session)|cookies/'`
(no secret tracked). This file contains no real tokens.

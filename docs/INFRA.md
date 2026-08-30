# Infra & architecture — decisions, monitors, subscriptions & hardening

All infra/architecture/ops notes merged. This file absorbs the scattered `go-feasibility`, `architecture-ptb`, `kurigram`, `learnings_*` and the remaining memory incident notes.

## Sources consolidated

- `docs/go-feasibility.md`
- `docs/architecture-ptb-vs-pyrogram.md`
- `docs/kurigram-open-issues.md`
- `docs/learnings_2026_08_12.md`
- `docs/learnings_2026_08_13.md`
- `docs/memory/tgbot-system-monitor.md`
- `docs/memory/tgbot-silent-203-exec.md`
- `docs/memory/tgbot-migrate-to-3845233.md`
- `docs/memory/tgbot-2026-08-13-propagation-dispatche-bugs.md`
- `docs/memory/tgbot-subscription-system.md`
- `docs/memory/tgbot-premium-4gb-whitelist.md`
- `docs/memory/tgbot-friend-media.md`
- `docs/memory/tgbot-copy-message-vs-forward.md`
- `docs/memory/tgbot-2026-08-16-admin-webapp-and-join-fix.md`
- `docs/memory/tgbot-2026-08-21-tiktok-pin-and-webapp.md`

---

---

## 1. Source: `docs/go-feasibility.md`

# Go as a complementary language for tgbot — feasibility report

**Status:** analysis / recommendation (no code written)
**Date:** 2026-08-04
**Scope:** should any part of tgbot be (re)written in Go? This report looks at
Go as a *complementary* language — a separate process next to the Python bot —
NOT a wholesale rewrite.

> **UPDATE (2026-08-04, after this report was approved):** recommendation §3.1
> was implemented. The system monitor is now a static Go binary at
> `cmd/tgbot-monitor/` → `build/tgbot-monitor`; `utils/system_monitor.py` is a
> thin spawner. Output stays byte-identical to the old Python version. See
> `docs/memory/tgbot-system-monitor.md`. Everything in §3.2–3.5 remains "not
> recommended" as of this writing.

---

## 1. TL;DR

- **A full Go rewrite is not feasible and should not be attempted.** The bot is
  a thin orchestration layer over three Python-first dependencies
  (`yt-dlp`, `instagrapi`, `twikit`) plus `pyrogram`, and the platform-specific
  anti-detection logic is battle-tested Python. There is no Go equivalent of
  yt-dlp's 1500+ site extractors, and reimplementing TikTok/IG proof-of-work
  and cookie rotation would be years of work for a strictly worse result.
- **Go is a good fit for exactly one current component:** the standalone
  **system monitor** (`utils/system_monitor.py`). It is already /proc-only,
  zero-dependency, single-process, and meant to outlive the bot — which is the
  textbook profile for a tiny static Go binary. This is the recommended first
  (and probably only) Go piece.
- **Go could later host the splitter/uploader** if it ever needs to outlive the
  bot, but today the ffmpeg subprocess does all the work and Python's wrapper
  is not the bottleneck. Low priority.

---

## 2. What the project is actually made of

| Component | Tech | Why that choice matters for Go |
|---|---|---|
| Telegram client | `pyrogram` (+`tgcrypto`) | MTProto implementation in Python; no maintained Go MTProto client of equal maturity for a bot |
| Download engine | `yt-dlp[default,curl-cffi]` | **Python by definition** — the largest extractor ecosystem in existence; impossible to replace |
| PO-token provider | Deno (bgutil plugin + `utils/pot_provider.py`) | Already non-Python; Go would not improve it (Deno does its job) |
| Upload/split | `ffmpeg-python` wrapping ffmpeg binary | Work happens in ffmpeg; wrapper is glue |
| HTTP streaming | FastAPI / uvicorn | Fine as-is; not a bottleneck at 1 core |
| IG/X DM relay | `instagrapi`, `twikit` | Private-API implementations, anti-detection tuned over months in Python |
| System monitor | `utils/system_monitor.py` | **The one pure /proc + HTTP process — Go's natural habitat** |
| Cookie lifecycle | `utils/cookie_manager.py` | Python dict/list logic; no reason to move |

Host facts (test VPS): **1 vCPU, 961 MB RAM, 8.7 GB disk (77% used)** — every
decision below is weighted by how much memory and disk it would save on a box
this small.

---

## 3. Candidate-by-candidate analysis

### 3.1 System monitor (`utils/system_monitor.py`) — ✅ RECOMMENDED

Current state: Python, ~27 MB RSS, reads `/proc` (stat/meminfo/loadavg/uptime/
pid/cmdline), posts via raw Bot API `requests.post`, runs as its own process or
a systemd unit. It is deliberately zero-dependency.

Go would give:

- **One static binary, no interpreter.** `go build` → ~2–4 MB file, ~4–8 MB
  RSS. Saves ~20 MB RAM on a box where 739 MB is already in use. No `venv`,
  no `python3` on PATH, no `.pyc`, nothing to import-break on Python upgrades.
- **True independence for free.** Go's `runtime` + `os/signal` handle
  daemonization/signals without the subprocess/fork trick in
  `spawn_detached_monitor()`. `syscall.Getrusage`, `/proc`, `syscall.ClockGettime`
  are stdlib — no psutil equivalent needed, same /proc discipline.
- **The `/proc` scan in `is_running()` becomes natural.** Reading
  `/proc/*/cmdline` in Go is a 10-line stdlib loop with no false-positive
  concerns beyond what we already handle.
- **Systemd unit stays identical.** `ExecStart=/usr/local/bin/tgbot-monitor`
  instead of `venv/bin/python -m utils.system_monitor`. The unit is already a
  template; only the `ExecStart` line changes.

**Effort:** 1–2 focused sessions (~400–600 LOC), no new infra. The format
functions (`format_report`, `format_warning`, HTML escaping, top-N rows) port
1:1. **Risk:** low. It has no shared state with the bot — it is already a
separate process. Worst case we keep the Python one running; they dedupe via
`is_running()`.

**Tradeoff to preserve:** the monitor and the bot's `is_running()` scan must
agree on how to detect "a monitor is alive". If the Go binary names its module
string differently, the bot's `/proc` scan won't see it. **Plan:** keep the
marker string (`utils.system_monitor` or a shared constant) in the Go binary's
argv so dedup keeps working, or switch dedup to a fixed pidfile convention.
This is the main cross-language coupling point.

### 3.2 Upload/splitter (`utils/uploader_handler.py`) — ⏳ LATER, low priority

The heavy lifting is ffmpeg (keyframe splitting) and pyrogram's `send_video`
(Telegram's native 2 GB/4 GB support). Python is glue. A Go rewrite would buy
nothing today and risk the precisely-tuned size/ceiling logic (invariants #9,
#11 in AGENTS.md). **Revisit only if** the splitter needs to become a
standalone service that keeps running when the bot is down — same argument as
the monitor.

### 3.3 PO-token provider (`utils/pot_provider.py`) — ❌ NOT RECOMMENDED

Already Deno/TypeScript. Go would add nothing; the complexity is in the
browser-proof-of-work protocol, which yt-dlp's plugin ecosystem already solves.

### 3.4 Download engine / IG/X relay — ❌ NOT FEASIBLE

`yt-dlp` is Python. `instagrapi`/`twikit` are Python private-API libraries with
the anti-detection posture (jittered polling, session watermarks, challenge
freezes) described in AGENTS.md — that logic is the product, and porting it to
Go would recreate bugs we already fixed. Nothing in this tier moves.

### 3.5 Streaming (FastAPI/uvicorn) — ❌ NOT RECOMMENDED

uvicorn at 1 core is not the bottleneck (the disk/ffmpeg/Telegram upload are).
Go's HTTP would only matter at scales this project will never reach.

---

## 4. Where Go genuinely helps (bottom line)

1. **Memory** — the monitor is one of the few resident processes; a Go binary
   saves ~20 MB on a 961 MB box that currently sits at ~77% used. Small, but
   real, and it compounds with the bot + PO provider + monitor all resident.
2. **Fragility** — one fewer Python process means one fewer thing that breaks
   on a `venv` rebuild, a Python upgrade, or a missing dep. The monitor's whole
   point is "outlive the bot"; a static binary outlives everything.
3. **Ops simplicity** — `install.sh` gains a `go build` step (or a prebuilt
   release binary); the systemd unit's `ExecStart` gets simpler.

## 5. Costs / risks to weigh

- **Two languages to maintain** in a small project. The monitor is small, so
  this is tolerable, but it is a real tax on every future contributor.
- **Dedup coupling** (3.1): the bot's `/proc` `is_running()` scan must recognize
  the Go process. Needs a deliberate shared convention (argv marker or pidfile).
- **No test suite** in this repo (AGENTS.md: "There's no test suite") — Go
  would bring `go test` for free, which is a *pro* for the monitor (it has the
  most testable pure functions: formats, averages, top-N).
- **Sign-off needed from the platform-risk side:** none for the monitor, since
  it never touches IG/X/YT — it only reads `/proc` and posts to Telegram.

## 6. Recommendation

**Do 3.1 (monitor) in Go, nothing else.** It is the only component whose
profile (long-lived, resident, /proc-only, no shared library, must outlive the
bot) matches Go's strengths exactly, and the only one where the memory savings
matter on this VPS. Keep the Python monitor's output format byte-identical
(`#system`, `VPS time:`, top-N blocks) so the channel readership doesn't
change. Land it as a sidecar — ship the Go binary, keep the Python version
until the Go one has been reporting clean for a week, then remove the Python
file and update `install.sh`/`deploy/tgbot-monitor.service`.

Everything else stays Python; a full rewrite is rejected on feasibility (yt-dlp
is irreplaceable) and on cost/benefit (orchestration glue has no Go advantage).

---

## 7. If we do it — first steps

1. Scaffold `cmd/tgbot-monitor/main.go` mirroring `system_monitor.py`'s
   functions 1:1 (report format, warning format, top-N, `VPS time:` line).
2. Keep `is_running()` compat: emit the same marker in argv, or move dedup to a
   pidfile convention understood by both languages.
3. `go vet ./... && go test ./...`; add unit tests for `format_report`/
   `format_warning` (this repo has no Python tests — Go gives us a free first
   test suite).
4. Update `deploy/tgbot-monitor.service` `ExecStart` → the binary path;
   `install.sh` builds/installs it.
5. A/B on the VPS: both monitors running; the bot's dedup picks one; compare
   channel output; then delete `utils/system_monitor.py`.

---

## 2. Source: `docs/architecture-ptb-vs-pyrogram.md`

# Architecture Comparison: PTB + Telethon vs Pyrogram (current)

## Current: Single MTProto library (Pyrogram)

```
tgbot (single process)
├── Pyrogram (MTProto)
│   ├── Bot API: handlers, uploads, streaming
│   ├── Premium user session: 4 GB uploads, user API
│   ├── File streaming: direct MTProto file reference streaming
│   └── Direct-Forward delivery: uploads via bot MTProto
└── FastAPI: streaming bridge, webapps
```

**Pros:**
- Single MTProto connection for everything
- 4 GB Premium uploads work natively (user MTProto session)
- Zero-disk streaming via direct MTProto file handles
- Atomic session management (one string session for bot + premium user)
- Simpler deployment, single dependency

**Cons:**
- Original Pyrogram repo archived (maintained via forks: kurigram, pyrofork)

---

## Proposed Alternative: PTB + Telethon (split architecture)

```
tgbot (bot process)
├── python-telegram-bot (Bot API / HTTP)
│   ├── Bot handlers, commands, inline keyboards
│   ├── 2 GB upload limit (Bot API hard cap)
│   └── Webhook/polling
└── FastAPI: webapps, streaming bridge (downloads files first)

Telethon user process (separate or same)
├── Telethon (MTProto)
│   ├── Premium user session: 4 GB uploads, user API
│   ├── File streaming: direct MTProto file handles
│   └── User account features (join channels, read history)
└── Coordination layer (Redis, IPC, or HTTP)
```

**Pros:**
- PTB is actively maintained, excellent Bot API wrapper
- Telethon is actively maintained, excellent MTProto user library
- Clear separation of concerns

**Cons for tgbot specifically:**

| Feature | Current (Pyrogram) | PTB + Telethon |
|---------|-------------------|----------------|
| **4 GB Premium uploads** | ✅ Native (user MTProto) | ❌ Requires Telethon process + coordination |
| **2 GB bot uploads** | ✅ Native | ✅ Native (PTB) |
| **Zero-disk streaming** | ✅ Direct MTProto file refs | ⚠️ Telethon can stream, but bot must proxy via HTTP |
| **User session for premium** | ✅ Single string session | ✅ Telethon handles, but separate |
| **Bot + user same process** | ✅ Simple | ❌ Two processes or IPC |
| **Dependencies** | 1 MTProto lib | 2 libs (PTB + Telethon) |
| **Deployment** | Single service | Multiple services or complex single process |
| **Session management** | Atomic (one session) | Distributed (coordination needed) |

---

## Specific tgbot invariants that would break

From AGENTS.md:

1. **4 GB Premium uploads** — "Bots are hard-capped at 2 GB; only a Premium user account over MTProto can send 4 GB"
   - PTB (Bot API) → 2 GB hard limit
   - Would need Telethon process to do the actual upload, bot just coordinates

2. **Premium session generation in-chat** — interactive phone→code→2FA on temp Pyrogram client
   - Works same with Telethon, but separate process

3. **Zero-disk streaming** — `stream_handler` pipes Telegram file → HTTP without disk
   - Pyrogram: `iter_download()` yields bytes directly from MTProto
   - Telethon: `iter_download()` same capability, but bot must proxy via HTTP endpoint

4. **Single-worker queue** — downloads serialize, metadata concurrent
   - Works same, but coordination between PTB bot (metadata) and Telethon (download) adds latency

5. **Direct-Forward delivery** — uploads via bot MTProto (premium allowed = true)
   - Would need Telethon to do the actual 4 GB upload

---

## Verdict

**Do NOT migrate to PTB + Telethon for tgbot.** The split architecture introduces:
- Operational complexity (multiple processes, IPC, session coordination)
- Loss of atomicity (bot session + premium user session must stay in sync)
- Same MTProto dependency (Telethon) just moved to a separate process
- No benefit for tgbot's use case — Pyrogram's API already handles both bot and user

**Recommended path:** Upgrade to an actively maintained Pyrogram fork:
```bash
pip install -U kurigram  # or pyrofork
# Code changes: essentially zero (same API, same imports)
```

This preserves all invariants while getting active maintenance.

---

## 3. Source: `docs/kurigram-open-issues.md`

# Kurigram Open Issues — Reference for Future LLM Maintainers

> Source: https://github.com/KurimuzonAkuma/kurigram/issues (fetched 2026-08-16)
> 15 open issues as of fetch date. This document preserves the full context for future agents.

---

## Issue #347: Filters still cannot be used on three update types after #346

**Created:** 2026-08-13  
**Labels:** (none)  
**Impact:** Filters broken on `PurchasedPaidMedia`, `ManagedBotUpdated`, `MessageReactionUpdated`, `BusinessConnection`

**Summary:**
Three update types (`PurchasedPaidMedia`, `ManagedBotUpdated`, `MessageReactionUpdated`, `BusinessConnection`) break when filters like `filters.group` or `filters.me` are applied.

**Root causes:**
1. **Missing `Update` base class** — `PurchasedPaidMedia`, `ManagedBotUpdated`, `BusinessConnection` subclass `Object` alone, so the defaults from #346 never apply. Their handlers still run filters → `AttributeError`.
2. **Inconsistent sender attribute** — `MessageReactionUpdated`, `ManagedBotUpdated`, `BusinessConnection` name it `user`; others use `from_user`. On `MessageReactionUpdated` (which subclasses `Update`), `filters.me` silently returns `False` because `from_user` resolves to `None`.
3. **Duplicate `"me"`/`"self"` spelling** — `resolve_peer` and `filters` maintain separate hardcoded lists that must stay in sync.

**Workaround:** Avoid filters on these update types until fixed.

---

## Issue #331: Message.chat nullability not handled in Message.__parse_reply

**Created:** 2026-07-15  
**Labels:** (none)  
**Impact:** Crash when `Message.chat` is `None` in `__parse_reply`

**Traceback:**
```
File ".../pyrogram/types/messages_and_media/message.py", line 1872, in __parse_reply
    key = (parsed_message.chat.id, parsed_message.reply_to_message_id)
AttributeError: 'NoneType' object has no attribute 'id'
```

**Context:** `parsed_message.chat` can be `None` but `__parse_reply` accesses `.id` unguarded.

---

## Issue #286: IPv6 proxies don't work (regression from 2.2.9)

**Created:** 2026-02-24  
**Labels:** (none)  
**Impact:** IPv6 SOCKS5 proxy connections fail with `Network unreachable`

**Regression:** Introduced by commit `d32477c` "Fix default server address values, test mode and ipv6". Works in 2.2.9, broken in dev.

**Reproduction:**
```python
proxy = {"scheme": "socks5", "hostname": "11.22.33.44", "port": 1234, "username": "u", "password": "p"}
app = Client("my_account", proxy=proxy, ipv6=True)
app.run()  # ProxyError: Network unreachable
```

---

## Issue #249: Stop Transmission not working (`StopTransmission` / `stop_transmission()`)

**Created:** 2025-10-16  
**Labels:** (none)  
**Impact:** Raising `StopTransmission` or calling `app.stop_transmission()` doesn't cancel download/upload — only hides progress temporarily.

**Worked in** original pyrogram, broken in kurigram.

---

## Issue #224: Video upload speed regression (2.2.10+)

**Created:** 2025-09-17  
**Labels:** (none)  
**Impact:** Upload starts at 10-15 MB/s then drops to 1.5-2 MB/s after a few seconds. 2.2.9 sustains 10-15 MB/s.

**Likely cause:** Session/connection pool changes in 2.2.10.

---

## Issue #202: Missing InlineQueryResult types

**Created:** 2025-07-23  
**Labels:** (none)  
**Missing:**
- `InlineQueryResultCachedGif`
- `InlineQueryResultCachedMpeg4Gif`
- `InlineQueryResultGame`
- `InlineQueryResultGif`
- `InlineQueryResultMpeg4Gif`

`InlineQueryResultAnimation` / `InlineQueryResultCachedAnimation` are redundant.

---

## Issue #185: Porting Guide needed

**Created:** 2025-06-12  
**Labels:** enhancement  
**Request:** Migration guide from pyrogram → kurigram.

---

## Issue #180: `force_document=True` ignored for webp files (sent as sticker)

**Created:** 2025-06-02  
**Labels:** (none)  
**Impact:** `send_document(..., force_document=True)` sends webp as sticker instead of document.

---

## Issue #174: Missing `TRIGGER_EMOJI_ANIMATION` in ChatAction enums

**Created:** 2025-05-20  
**Labels:** enhancement  
**Missing enum value** referenced in Bot API docs.

---

## Issue #160: Session layer problems (media sessions stop working)

**Created:** 2025-04-07  
**Labels:** (none)  
**Critical:** Long-running bots (uploading/downloading) stop working after hours.

**Root causes:**
1. **Ignored bad server salt in Ping** — server salt updates from Ping responses are ignored, so salt never refreshes.
2. **Persistent media sessions** — kurigram reuses media sessions, but Telegram recommends separate connections for uploads/downloads and salt expires after ~1 hour of inactivity.

**Result:** Uploads/downloads fail with `TimeoutError` after hours. Never happened in 2.0.106.

**Reproduction:** Send 1 GB file in loop; fails after ~100 files with `Request timed out` on `upload.SaveBigFilePart`.

---

## Issue #150: Module still named `pyrogram` instead of `kurigram`

**Created:** 2025-02-28  
**Labels:** enhancement  
**Impact:** IDE confusion (PyCharm), confusing imports (`import pyrogram` for kurigram package).

---

## Issue #143: Markdown unparse/parse inconsistency

**Created:** 2025-02-20  
**Labels:** (none)  
`message.text.markdown` (ParseMode.DISABLED) → `send_message(..., parse_mode=MARKDOWN)` produces different rendering.

---

## Issue #112: Surrogate pair handling crashes on certain emoji (👉)

**Created:** 2024-11-26  
**Labels:** (none)  
**Crash:** `UnicodeDecodeError: 'utf-16-le' codec can't decode bytes in position 2-3: unexpected end of data` on `👉` (pointing finger emoji).

**Location:** `parser/utils.py` `remove_surrogates()` uses `surrogatepass` which fails on lone surrogates.

**Workaround:** Change error handling to `"ignore"` in `remove_surrogates()`.

---

## Issue #109: Pyrogram Rework (dispatcher redesign)

**Created:** 2024-11-20  
**Labels:** enhancement  
**Scope:** Major architectural proposal:
- Error handling / middlewares (currently missing)
- Router-based dispatch instead of integer groups
- Flood wait synchronization
- Retry mechanism for failed invokes (preserving message ID to avoid duplicates)

---

## Issue #62: `download_media()` returns 0-byte files

**Created:** 2024-05-17  
**Labels:** (none)  
**Impact:** Some photos/videos download as 0 bytes with `Request timed out` on `upload.GetFile`.

---

---

## Migration Risk Assessment for tgbot

| Issue | Risk to tgbot | Mitigation |
|-------|---------------|------------|
| #160 Session layer | **HIGH** — tgbot does heavy uploads/downloads, runs for days | Test long-running uploads; consider pinning 2.0.106 if regression |
| #249 StopTransmission | **MEDIUM** — admin abort queue uses similar logic | Verify abort queue works; may need custom implementation |
| #224 Upload speed | **MEDIUM** — large file uploads | Benchmark before/after |
| #331 Message.chat None | **LOW** — tgbot handles private chats mostly | Add guard if processing forwarded messages |
| #112 Surrogate emoji | **LOW** — rare emoji | Apply `"ignore"` workaround if needed |
| #143 Markdown | **LOW** — tgbot uses minimal markdown | Test reply flows |
| #202 InlineQueryResult | **NONE** — tgbot doesn't use inline queries | N/A |

---

## Recommended Migration Steps

1. **Branch** current `main` → `feat/kurigram-migration`
2. **Pin** `kurigram==2.2.9` (last known stable for speed/session) or test latest dev
3. **Run** full test suite (downloads, uploads, streaming, queue, direct-forward)
4. **Watch** for:
   - Session timeouts on long uploads
   - Upload speed regression
   - StopTransmission in abort queue
4. **Apply** workarounds for #112, #143, #331 if encountered
5. **Document** any additional findings in this file

---

*Generated 2026-08-16 for tgbot migration tracking.*

---

## 4. Source: `docs/learnings_2026_08_12.md`

# Project Learnings — 2026-08-12

## Summary
This document captures the key technical learnings from implementing **full yt-dlp site support** and debugging **Instagram DM relay issues** in tgbot.

---

## 1. Full yt-dlp Site Support

### What Changed
- **Before**: Only ~25 hardcoded domains routed to yt-dlp format selection; everything else fell through to direct-file (plain HTTP GET) path.
- **After**: All 1,786 yt-dlp extractor patterns (generic excluded) are compiled at startup. Any URL matching an extractor goes through the full yt-dlp pipeline (format selection, quality merge, cookie auth).

### Implementation Details
- **Module**: `utils/downloader/supported_sites.py`
- **Key function**: `is_ytdlp_supported(url)` — returns True if URL matches any compiled `_VALID_URL` pattern.
- **Performance**: ~0.6s one-time compile, ~0.01ms per URL thereafter. Lazy compilation on first call.
- **Routing gate**: `is_social_media_link` in `modules/downloader_handler.py` now uses `is_ytdlp_supported()`.

### Cookie Architecture for New Sites
- **Layout**: `cookies/ytdlp/<site>.txt` where `<site>` = first label of hostname (e.g., `pornhub.com` → `pornhub.txt`).
- **Resolution**: `_resolve_jar_path()` in `utils/downloader/cookies.py` extracts domain, strips `www.`, takes first label.
- **Admin UI**: "➕ Per-Site Jar" in Cookies menu → type site name → upload `.txt` document.
- **Fallback**: `cookies/ytdlp/cookies.txt` (global) used when no per-site jar exists or is empty.

### Special Cases (see `docs/cookie_site_special_cases.md`)
- Multi-domain sites (Google, Facebook, Microsoft, Amazon) share cookies across subdomains.
- Adult sites need age-verification cookies from browser session.
- Chinese sites (Bilibili, IQIYI, Douyin) require CN IP/phone verification.
- DRM streaming sites (Netflix, Disney+, etc.) need Widevine — **not supported** by this bot.
- Dedicated jars (YouTube, Instagram, TikTok, X) live outside `ytdlp/` and take priority.

---

## 2. Instagram DM Relay — Root Cause & Fix

### Symptom
> "I am not receiving the Instagram DMs"

### Log Evidence (from `logs/bot.log`)
```
2026-08-12 13:14:31,096 | INFO     | private_request | None [403] GET https://i.instagram.com/api/v1/direct_v2/inbox/...
2026-08-12 13:14:31,098 | WARNING  | modules.direct_forward.instagram | [DirectForward/IG] session expired — attempting re-login.
2026-08-12 13:14:36,152 | INFO     | modules.direct_forward.instagram | [DirectForward/IG] Persisted session unusable (login_required); trying sessionid.
2026-08-12 13:14:54,427 | WARNING  | modules.direct_forward.instagram | [DirectForward/IG] sessionid login failed (Exceeded 30 redirects.); trying password.
2026-08-12 13:14:54,430 | ERROR    | modules.direct_forward.instagram | [DirectForward/IG] re-login failed: No usable IG session. Upload a fresh igcookies.txt (Admin → Cookies) or set IG_DIRECT_USERNAME/IG_DIRECT_PASSWORD in .env.. Sleeping 1h.
```

### Root Cause
1. **Stale cookies**: The `cookies/instagram/igcookies.txt` jar contained expired `sessionid` (duplicated entries, old timestamps).
2. **No fallback credentials**: `IG_DIRECT_USERNAME` / `IG_DIRECT_PASSWORD` not set in `.env`.
3. **Worker behavior**: The IG worker (`_instagram_worker`) retries login on poll cadence but **cannot recover without fresh cookies or credentials**.

### Why Cookies Went Stale
- Instagram rotates `sessionid`/`csrftoken` on **every response** via `Set-Cookie`.
- The bot's **cookie write-back** mechanism (`utils/cookie_manager.py`) captures these rotations *only on successful yt-dlp runs*.
- **Direct-forward IG worker uses `instagrapi` directly**, not yt-dlp — so it **never triggers cookie write-back**.
- Result: The shared `igcookies.txt` jar never gets refreshed from the DM worker's activity.

### Fix Options (pick one)

| Option | Description | Effort |
|--------|-------------|--------|
| **A. Upload fresh cookies** (immediate) | Admin → Cookies → Replace `igcookies.txt` with fresh browser export. Worker picks it up on next poll (≤5 min). | 1 min |
| **B. Set credentials in .env** (semi-permanent) | Add `IG_DIRECT_USERNAME` + `IG_DIRECT_PASSWORD` (+ `IG_DIRECT_TOTP_SEED` if 2FA) to `.env`. Worker falls back to password login when cookies fail. | 2 min |
| **C. Share write-back from yt-dlp** (architectural) | Make IG DM worker use yt-dlp for cookie-refreshing runs, or add a periodic "cookie refresh" yt-dlp job for Instagram. | ~1 hr |

### Recommended: Option A + B
1. **Now**: Upload fresh `igcookies.txt` via Admin Console.
2. **Soon**: Add `IG_DIRECT_USERNAME`/`IG_DIRECT_PASSWORD` to `.env` as safety net.
3. **Future**: Consider architectural fix (Option C) if cookies keep going stale.

### Key Insight
> **The direct-forward workers (IG/X/TikTok) do not participate in cookie write-back.** They consume the shared jars but only yt-dlp runs refresh them. If a site is *only* accessed via direct-forward (no yt-dlp downloads), its jar **will go stale**.

---

## 3. Cookie Write-Back Mechanism — Deep Dive

### How It Works (`utils/cookie_manager.py`)
1. **Acquire**: `acquire(jar_path)` → creates per-run snapshot in `cache/cookies/`.
2. **Run**: yt-dlp uses snapshot (writable); applies `Set-Cookie` headers from responses.
3. **Commit**: `commit(snapshot, success=True)` → overlay-merges snapshot into real jar:
   - Keys `(domain, path, name)` from snapshot overwrite real jar.
   - **Never deletes** keys from real jar.
   - Atomic write (temp + `os.replace`), mode restored (0o444 for primary jars).
4. **Meta tracking**: `cookies/meta.json` records `last_success`, `last_merge`, `merge_count`.

### Critical Invariants (from AGENTS.md)
- **Invariant #4**: Snapshots per run, write-back on success, locked at rest.
- **Invariant #5**: `pip install -U --pre "yt-dlp[default,curl-cffi]"` — `curl-cffi` required for TikTok PoW solver.
- **Invariant #11**: Button sizes = video + best audio (merged); CDN probes for Instagram DASH.

### Why IG DM Worker Doesn't Trigger Write-Back
- Uses `instagrapi` (Python Instagram Private API wrapper), not yt-dlp.
- `instagrapi` manages its own session (`direct_ig_session.json`), separate from cookie jar.
- The `sessionid` from `igcookies.txt` is only used for **initial bootstrap** (`login_by_sessionid`).
- Subsequent rotations happen in `instagrapi`'s internal state, never written back to `igcookies.txt`.

---

## 4. Direct-Forward State Race Condition (Historical)

### Incident (2026-08-11)
- **Symptom**: X self-DM posts relayed 2×, then 4×, exponentially.
- **Root cause**: Three workers (IG/X/TikTok) shared `direct_forward_state.json`. IG worker loaded state **once at boot** and each `_save_state()` (full-dict write) reverted X's `last_id` cursor to boot value.
- **Fix**: `_merge_state_save()` + `_state_save_owned()` — merge-only per owned platform section, with `_STATE_LOCK` for async safety.
- **Invariant**: Never call `_save_state()` from workers; always `_state_save_owned(state, {own_platform})`.

---

## 5. Deployment & Operational Notes

### Cookie Freshness Watchdog
- Runs at startup (`main.py::initialize_cookie_jars` → `cookie_manager.freshness_warnings()`).
- Warns if jar hasn't had successful auth run in `COOKIE_STALE_WARNING_DAYS` (default 21).
- **Admin upload** (`touch_cookie_uploaded`) resets the clock without needing a successful run.

### Service Lifecycle
- `install.sh` renders `tgbot.service` but **does not enable it**.
- Must run: `sudo systemctl enable --now tgbot` after verifying bot works.
- `tgbot-xchat-bridge.service` **is enabled** by install.sh (supervisor pattern).
- `tgbot-monitor.service` installed but disabled (bot spawns detached monitor).

### Scripts Must Stay Executable
- `run.sh`, `install.sh`, `uninstall.sh` must keep `100755` mode.
- `git update-index --chmod=+x <file>` if mode lost.

---

## 6. Action Items

### Immediate
- [ ] Upload fresh `igcookies.txt` via Admin Console → Cookies → Replace `igcookies`.
- [ ] Add `IG_DIRECT_USERNAME` / `IG_DIRECT_PASSWORD` to `.env` (restart bot after).
- [ ] Verify IG DM relay works (send test DM to bot's IG account).

### Short-term
- [ ] Consider periodic yt-dlp "cookie refresh" job for Instagram (e.g., daily extract of a public reel).
- [ ] Add `onlyfans`, `patreon`, `fantia`, `fanbox`, `booth`, `gumroad` to per-site cookie list.
- [ ] Remove alias entries from auto-generated list (`instances`, `player-api`, `members`, `arhiiv`).

### Documentation
- [x] Create `docs/cookie_site_special_cases.md`
- [x] Create this learnings document
- [ ] Update `README.md` with full yt-dlp site support feature
- [ ] Update `blueprint.md` with new cookie architecture
- [ ] Update `AGENTS.md` if any invariants changed

---

## 7. Files Modified / Created

| File | Change |
|------|--------|
| `utils/downloader/supported_sites.py` | Already existed (compiles all yt-dlp patterns) |
| `utils/downloader/cookies.py` | Already existed (per-site jar resolution) |
| `modules/admin/register.py` | Already existed (per-site upload handler) |
| `cookies/ytdlp/*.txt` | **Created**: 90 empty per-site cookie jars with header + instructions |
| `docs/cookie_site_special_cases.md` | **Created**: Special cases reference for admin |
| `docs/learnings_2026_08_12.md` | **Created**: This document |

---

*Generated during tgbot full yt-dlp support rollout and IG DM debugging session.*

---

## 5. Source: `docs/learnings_2026_08_13.md`

# Project Learnings — 2026-08-13

## Summary
This document captures the key technical learnings from the 2026-08-13 session:
**(1)** two pyrogram dispatch-propagation bugs and the shared helper that fixes
them, **(2)** the logger refactor (monolith → `utils/logger/` package, Rich
`32768` truncation limit, strict Telegram/Bale log split), and **(3)** the
channel force-join verification UI now embedded in the `/start` greeting.

---

## 1. Dispatch-Propagation Bugs & `utils/propagation.py`

### Background
pyrogram dispatches handlers in **ordered groups**:
`-2` log interceptors → `-1` security gate → `0` state machine + extras
(GitHub/YouTube/Translate/Web) → `1` text router (downloader, greeting) →
`2` callback dispatcher. Control flow uses `message.stop_propagation()`,
`message.continue_propagation()`, or `raise ContinuePropagation`.

### Bug A — bare `except Exception` swallows `StopPropagation`
- **Symptom**: pasting a `github.com/...` link produced **duplicate replies**
  (the GitHub panel AND a direct-file upload attempt).
- **Root cause**: `StopPropagation` and `ContinuePropagation` are **`Exception`
  subclasses**, so `try: message.stop_propagation() except Exception: pass`
  silently swallowed the signal. The group-0 GitHub handler returned normally →
  the dispatcher `break`ed the group and control flowed to the *next* group,
  where the group-1 downloader grabbed the same link as a direct-file upload.
- **Fixes**: (a) the group-0 extras now use the shared helpers; (b) the
  group-1 greeting guard (`admin_start_text_handler`) uses `stop(message)` so
  `/subscription` no longer "double-greets".

### Bug B — `RawUpdateHandler` mid-group starves later handlers
- **Symptom**: after porting the balebot extras, `/tr`, `/yt`, `/search`,
  github links and `/web` **silently ignored all input**.
- **Root cause**: a `@app.on_raw_update(...)` handler (Stars `pre_checkout`)
  was registered **in the middle of group 0**. pyrogram's dispatcher treats any
  `RawUpdateHandler` as matching *every* update; when its callback returns
  normally the dispatcher `break`s the whole group, so every handler registered
  AFTER it in that group never saw an update.
- **Fix**: the raw handler now `raise pyrogram.ContinuePropagation` when it
  does not own the update (`modules/subscription/handlers.py::_raw_precheckout`),
  so the group iterator keeps going.

### The shared helper (the correct pattern)
```python
# utils/propagation.py
def stop(message):   # replaces message.stop_propagation()
def continue_(message):  # replaces message.continue_propagation()
```
Both re-raise the real propagation signal while still swallowing
genuinely-unexpected (non-propagation) errors. Every handler in
`modules/{admin,subscription,github,youtube,translate,web}/` should use them.
Never wrap `stop_propagation()`/`continue_propagation()` in a bare
`except Exception`/`except: pass`.

**Deploy a new `on_raw_update` in a shared group with care** — prefer an
isolated group or a raise-on-mismatch, or every later handler in that group
starves. Full write-up: `docs/memory/tgbot-2026-08-13-propagation-dispatche-bugs.md`.

---

## 2. Logger Refactor — `utils/logger/` package + Rich 32768

### The split
`utils/logger.py` (monolith) → `utils/logger/` **package**:
- `local.py` — `ensure_local_log_handler` (`logs/bot.log`, 5 MB × 3).
- `telegram.py` — `TelegramChannelHandler` → `LOG_CHANNEL_ID`.
- `bale.py` — `BaleChannelHandler` → `BALE_LOG_CHANNEL_ID`.
- `__init__.py` — backwards-compat re-exports (`from utils.logger import ...`
  still works), so call sites did not change.

### Truncation limit: 32768, not 3500
The Rich Bot API endpoint (`sendRichMessage`) accepts **32768** chars; the
earlier 3500/6000/8000 cuts were premature and **broke detailed log lines**
(a full admin-console dump of 17,003 chars was chopped mid-escape). Both
handlers now truncate at 31500 + a `[TRUNCATED at 32768 rich limit]` marker.
Keep the `sendMessage` fallback byte-compatible with the pre-rich format.

### Strict log split (`d723798`)
- The main Telegram log channel (`LOG_CHANNEL_ID`) gets **pyrogram /
  direct-forward / queue** lines.
- The `bale_log` channel gets **ONLY Bale/aiogram** lines — both at the same
  INFO level. This lets the Bale frontend be monitored without drowning the
  Telegram channel in duplicate Bale noise. Don't merge the streams.

### `bale_log` is a TELEGRAM channel (important correction)
`BaleChannelHandler` sends to `https://api.telegram.org` with the **same
`BOT_TOKEN`** — NOT to `tapi.bale.ai`. Reason: Bale is government-owned, so
Bale-side logs containing sensitive info must never cross into `tapi.bale.ai`
(security hole). They land in a separate private **Telegram** channel named
`bale_log`. Both handlers are Telegram-API; they are kept separate only so the
two log streams stay isolated. Earlier docs that said "bale_log on Bale via
`tapi.bale.ai`" were wrong and were corrected in README / USER_GUIDE /
UBUNTU_VPS_SETUP.

---

## 3. Channel Force-Join Verification in the Greeting

### What changed
`modules/subscription/join.py` (new) provides:
- `_greeting_text(user_id)` — the intro guide text.
- `build_greeting_keyboard(user_id)` — access prompt + **"✅ I joined — verify"**
  button when subscription mode is on and the user must join channels.
- `register_join_handlers(app)` — wires the `chkjoin:` callback that
  re-checks channel membership live.

`modules/admin/register.py` now sends **one self-contained greeting** that
always carries the normal intro guide + subscription access prompt + keyboard
in a single message (previously the subscription prompt was a second message).
`main.py` registers the join handlers at startup.

### Access gate
`utils/subscription/access.py`:
- `is_subscription_enabled()`, `is_free_allowed()`, `is_channel_member()`.
- `check_all_channels(user_id)` → `(all_joined, missing_list)` via
  `get_chat_member` status in `{member, administrator, creator, owner}`.

---

## 4. Deployment & Operational Notes

- **PO port**: config default is `YTDLP_POT_PORT=4416`; the production VPS
  overrides to `4417` in `.env`. Docs default to 4416 unless describing a
  specific deployment.
- **Playwright provisioning gap**: the sequential cookie refresher
  (`utils/cookie_refresher.py`, Phase 26) needs `playwright` + a Chromium
  browser, but neither was in `requirements.txt` nor provisioned by
  `install.sh` (it was installed manually on this VPS). See the
  requirements.txt / install.sh changes in this session.
- **Cookie refresher invariants** (Phase 26): one Chromium at a time
  (~300 MB peak — safe on 4 GB+8swap), 24 h ± 1 h cadence, atomic Netscape
  write `0o444`, clears `direct_ig_session.json` for IG, proxy-aware.
- **Service lifecycle**: `tgbot.service` is installed but NOT enabled by
  install.sh (must `sudo systemctl enable --now tgbot` after first success);
  `tgbot-xchat-bridge.service` IS enabled (resident supervisor);
  `tgbot-monitor.service` installed but disabled.
- **Scripts must stay executable** (`100755`): `run.sh`/`install.sh`/
  `uninstall.sh` — systemd calls `run.sh` directly; a lost exec bit crash-loops
  with `status=203/EXEC`.

---

## 5. Action Items

### Documentation (this session)
- [x] `docs/learnings_2026_08_13.md` — this document.
- [x] `AGENTS.md` — logger package path, `bale_log` truth, file-map rows for
  `utils/logger/` + `modules/subscription/join.py`.
- [x] `blueprint.md` — directory map (logger package, `propagation.py`,
  `join.py`), Phase 28 progress entry.
- [x] `README.md` — last-verified banner, Logs section (32768, `bale_log` is
  a Telegram channel), channel-join verification feature.
- [x] `docs/USER_GUIDE.md` — bale_log correction, truncation 32768,
  channel-join verify in the greeting, subscription table update.
- [x] `docs/UBUNTU_VPS_SETUP.md` — bale_log channel truth.
- [x] `docs/memory/README.md` — index updated + date bumped.

### Future
- [ ] Add remaining missing memory-doc entries to `docs/memory/README.md`
  (e.g. `tgbot-tiktok-direct-dm.md`, `tgbot-premium-4gb-whitelist.md`).

---

## 6. Files Modified / Created (2026-08-13 session)

| File | Change |
|------|--------|
| `utils/logger/` package | Split from `utils/logger.py` (local/telegram/bale + re-exports) |
| `utils/propagation.py` | `stop()`/`continue_()` helpers for dispatch control flow |
| `modules/subscription/join.py` | **New**: channel force-join verification UI + greeting text/keyboard |
| `main.py` | Registers join handlers at startup |
| `modules/admin/register.py` | Single self-contained greeting with access prompt + verify button |
| `AGENTS.md` | Logger + subscription file-map rows, `bale_log` truth |
| `blueprint.md` | Directory map + Phase 28 progress entry |
| `README.md` | Last-verified banner, Logs section, features |
| `docs/USER_GUIDE.md` | Logs + subscriptions + greeting updates |
| `docs/UBUNTU_VPS_SETUP.md` | bale_log channel truth |
| `docs/memory/README.md` | Index + date |
| `docs/learnings_2026_08_13.md` | **Created**: this document |

---

*Generated during the tgbot dispatch-propagation hardening + logger package
split + channel-join verification session.*

---

## 6. Source: `docs/memory/tgbot-system-monitor.md`

# tgbot system monitor — design & invariants

The standalone system monitor is a **static Go binary** (`cmd/tgbot-monitor/` →
`build/tgbot-monitor`, built by install.sh) that reports the VPS health to the
bot's log channel. It is a /proc-only health reporter that runs as **its own
process** — either the `tgbot-monitor.service` systemd unit (installed by
`install.sh`, survives reboots) or a detached fork spawned by `main.py` on bot
startup (`utils/system_monitor.py::spawn_detached_monitor`, deduped so it never
stacks a duplicate).

It is the project's **one Go component**. The port rationale is in
`docs/go-feasibility.md`; this file captures the operational invariants.

## Why it exists / what it replaces

The old approach was an in-process uvicorn log line (`[System] Disk usage ...`)
— only visible when the bot was up. If the bot hung or crashed, nobody learned
the box was hot. The monitor keeps reporting even when the bot is dead, which
is exactly the failure mode it observes. It was originally written in Python
(`utils/system_monitor.py` had the full engine) and ported to Go because it is
the one component whose profile — long-lived, resident, /proc-only, no shared
library, must outlive the bot — matches Go's strengths: ~5 MB static binary vs
~27 MB CPython RSS on a 961 MB VPS, no venv/interpreter to break, and a real
test suite (`go test`).

## Non-obvious invariants (do not break these)

1. **The engine lives in Go; the Python file is only a spawner.**
   `utils/system_monitor.py` is deliberately thin: `spawn_detached_monitor()`
   and `is_running()`. All sampling/formatting/sending is in
   `cmd/tgbot-monitor/`. Do NOT re-add a Python monitoring engine there.

2. **Zero dependencies (Go side).** Stdlib + `/proc` only. No psutil/gopsutil,
   no database, no dashboard. Measured on a 1-core/1 GB VPS: a fraction of a
   percent of a core and a few MB RSS.

3. **Independent of the bot.** The binary talks to Telegram via a plain
   `net/http` POST to the Bot API (like `utils.logger`), NOT pyrogram, NOT the
   bot's event loop. This is why it must stay a separate process, not a task
   inside `main.py`.

4. **Output format is a contract.** The `#system` report and the warning carry
   the **VPS local date-time** (`VPS time:`) line and are byte-identical to
   what the channel already showed (verified by diffing the Go formatter
   against the old Python one). Do not change spacing/emoji/format specifiers
   without a reason. Tests pin the exact strings.

5. **Reports every `SYSMON_REPORT_INTERVAL` samples** (default 60 × 15 s =
   15 min) and **warns at `SYSMON_WARN_PCT`** (default 80) on CPU/RAM/disk,
   repeating every `SYSMON_WARN_SECONDS` (default 60) **until every metric drops
   back below** — nag-until-fixed, never floods, stops on recovery.

6. **Config is self-contained via a minimal dotenv reader.** The systemd unit
   deliberately has no `EnvironmentFile=` (run.sh owns .env parsing for the
   bot); the Go binary parses `.env` itself (real env wins, python-dotenv
   semantics), so it stays standalone. If `BOT_TOKEN` or `LOG_CHANNEL_ID` are
   missing, `run()` exits 2 with a stderr message rather than silently sampling
   forever.

7. **Per-process CPU% needs deltas.** `/proc/<pid>/stat` utime+stime are
   cumulative since process start, so the scanner keeps a pid→(ticks, wall)
   snapshot from the previous poll and diffs (top's method). First sample of a
   new pid is 0%. `procPrev` is a package global — reset it if you add a way to
   restart the scan.

8. **Never blocks the sampler; rich message with plain fallback.** Each send is
   fire-and-forget in a goroutine with an 8 s timeout (`sendTelegram`), and a
   bad sample is skipped — the loop sleeps `POLL_SECONDS` and continues. Sends
   go out via **`sendRichMessage`** with `rich_message: {"html": ...}` (bordered
   `<table bordered>` for the metrics and the top-N process lists, headings
   `<h3>`/`<h4>`, `<footer>#system</footer>`); if the rich endpoint rejects the
   payload the same goroutine retries with `sendMessage` + the plain HTML, so
   the channel works on any Bot API version. The plain formatter
   (`formatReport`/`formatWarning`) stays byte-compatible with the pre-rich
   output — only the rich variants (`formatReportRich`/`formatWarningRich`) use
   tables. Do NOT change the plain fallback.

9. **Dedup: pidfile + /proc scan, understood by BOTH languages.** The Go binary
   writes project-root `system_monitor.pid` on start and removes it on exit.
   The Python `is_running()` checks that pidfile (stale/recycled-pid safe) and
   also scans `/proc/*/cmdline` for an argv0 containing `tgbot-monitor`, so the
   systemd unit and the bot's detached spawn can never stack. If you change the
   binary's argv0 or pidfile path, update BOTH sides.

10. **The systemd unit is a template** (`deploy/tgbot-monitor.service`) with the
    same `__USER__` / `__GROUP__` / `__PROJECT_DIR__` placeholders as
    `tgbot.service`, rendered by install.sh, `ExecStart` = the Go binary path.
    It has **no** `__MEMORY_MAX__` placeholder — the monitor is tiny
    (MemoryMax=256M is hardcoded headroom, NOT a tight cap; see AGENTS.md #1
    re `ulimit -v`). Installed but **not auto-enabled**; enable with
    `systemctl enable --now tgbot-monitor`.

## Operational notes

- Live log: `journalctl -u tgbot-monitor -f`; the detached path (non-systemd)
  writes `logs/system_monitor.log`.
- If the bot and the systemd unit both run, only the systemd instance samples —
  the bot's `spawn_detached_monitor` sees `is_running() == True` and returns.
- Env knobs: `SYSMON_POLL_SECONDS`, `SYSMON_REPORT_INTERVAL`,
  `SYSMON_WARN_PCT`, `SYSMON_WARN_SECONDS`, `SYSMON_TOP_N`,
  `SYSMON_HISTORY_SAMPLES`, `SYSMON_DISK_PATHS` (all in `config.py` +
  `.env.example`; the Go binary reads the same names).
- The repo ships **prebuilt static binaries** (`prebuilt/tgbot-monitor-linux-
  amd64` / `-arm64`); install.sh copies the one matching `uname -m` to
  `build/tgbot-monitor` and only lazily apt-installs `golang-go` + builds from
  source if the prebuilt is missing. When you change `cmd/tgbot-monitor/`,
  rebuild BOTH prebuilts (`GOOS=linux GOARCH=amd64|arm64 CGO_ENABLED=0 go
  build -trimpath -ldflags="-s -w"`) or fresh installs ship the stale binary.
- Reports/warnings go out as **rich messages** (`sendRichMessage`, `rich_message:
  {"html": ...}`) with bordered `<table bordered>` metrics and top-N process
  tables; the goroutine falls back to `sendMessage` with the plain HTML if the
  rich endpoint is rejected, so the channel works on any Bot API version.
- Rich-message gotchas (from `telegram-bot-api.md` / the rich-formatting guide):
  tables need the `bordered` attribute or Telegram renders them borderless;
  table cells only support **inline** formatting (no `<p>`, `<ol>`, `<pre>` in a
  cell); `#system` uses `<footer>` so it sits at the bottom of the card.
- Build manually: `cd cmd/tgbot-monitor && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o ../../build/tgbot-monitor .`
- Tests: `cd cmd/tgbot-monitor && go test ./...` (this is the project's one
  test suite — the Python side still has none).

---

## 7. Source: `docs/memory/tgbot-silent-203-exec.md`

# Silent bot outage: systemd 203/EXEC (lost exec bit on run.sh)

**Date:** 2026-08-04 · **Area:** `deploy/tgbot.service`, `run.sh`, `install.sh`,
`uninstall.sh`, git file modes

## Symptom

The bot went **completely silent** — no replies, no log-channel activity.
`logs/bot.log` ended cleanly (last line was a normal relay). The service was
not crashed in the "python died" sense; it was crash-looping invisibly:

```
$ systemctl status tgbot
Active: activating (auto-restart) (Result: exit-code) since …
Process: 2396 ExecStart=/home/dev/tgbot/run.sh (code=exited, status=203/EXEC)
```

`NRestarts` climbed ~5/s (817 in under 2 h). The Go monitor noticed (the VPS
was hot from the restart storm) and fired 80% warnings — the first *visible*
signal of the problem.

## Root cause

`deploy/tgbot.service` has `ExecStart=__PROJECT_DIR__/run.sh` — systemd exec's
`run.sh` **directly**, so it needs the executable bit. `run.sh` (and
`install.sh`/`uninstall.sh`) had been committed to git as **mode `100644`
(non-executable)**. Any `git pull` resets the working-copy mode to the tracked
mode; when the repo was pulled on the VPS that morning, `run.sh` came back
`0644` and every subsequent service start failed with `203/EXEC`.

`203/EXEC` = "executable not found or not executable" — **not** a Python
traceback, so `bot.log` stays clean. The unit was fine; the file it launches
had lost its exec bit.

## Fix

1. **VPS (immediate):** `chmod +x ~/tgbot/run.sh`; `systemctl restart tgbot`.
2. **Repo (permanent):** commit the exec bit so pulls can't strip it again —
   `chmod +x run.sh install.sh uninstall.sh` then
   `git update-index --chmod=+x <file>` for each, and commit the mode change
   (`100644 → 100755`).
3. **Hardening:** `install.sh` now runs `chmod +x run.sh install.sh uninstall.sh`
   every invocation (right after `PROJECT_DIR` is set), so a bad pull self-heals
   on the next install/upgrade run.

## Verification

- `git ls-files -s run.sh` → `100755 …` (was `100644`).
- On the VPS: `systemctl is-active tgbot` → `active`; `NRestarts` frozen at 817
  (0 failures since the fix); pyrogram sessions start; a live Instagram DM was
  relayed seconds after restart.
- Both `python main.py` processes are correct (PID 2999 cwd=~/tgbot,
  PID 560 cwd=~/balebot in its own cgroup) — no getUpdates conflict.

## Fingerprint / diagnosis checklist

- Bot silent but `systemctl status tgbot` shows `activating (auto-restart)` +
  `203/EXEC`, and `logs/bot.log` ends cleanly → **check file mode of run.sh
  first** (`ls -l`, compare `git ls-files -s`).
- Also check balebot: it uses its own unit; its `run.sh` was still `0755`, so
  only tgbot went down.
- Don't just chmod the VPS copy — the tracked mode was `100644`, so the next
  pull would break it again. Fix the git index and commit (AGENTS.md Gotchas).

## References

- AGENTS.md → Gotchas: **"Entrypoint scripts must stay executable — systemd
  calls `run.sh` directly"** (added from this incident).
- `install.sh` chmod-hardening block right after `PROJECT_DIR=`.

---

## 8. Source: `docs/memory/tgbot-migrate-to-3845233.md`

# Migration: remote VPS → new machine (38.45.80.233)

The bot's production home moved from the small remote VPS
(`dev@66.23.198.52:1605`) to this machine (`/home/dev/opencode/tgbot`,
host `ubuntu`, IP `38.45.80.233`). Motivation: the old VPS's 8.7 GB disk
(3.6 GB free) could not survive a ~3.1 GB YouTube download — the merge step
peaks at ~2× the final file, ffmpeg died with
`ERROR: Postprocessing: WARNING: unable to obtain file audio codec with ffprobe`,
and the user's media never uploaded. The new box has 96 GB disk (~83 GB free),
3.8 GiB RAM + 4.0 GiB swap, `MemoryMax=2500M` in the systemd unit.

## Root cause (corrected)

The original VPS diagnosis blamed disk space. That was **wrong** — the same
error reappeared on this machine with 82 GB free. The real cause was
commit `f67e576` (2026-08-05, deployed before both failures): it rewrote the
`dl:` callback action token from `v`/`a` to the **button emoji**
(`dl:{cache_id}:🎥:{format_id}`), but `dl_callback_handler` still branched on
`action == 'v'`/`'a'`. So every 🎥 video button fell through to the **audio**
path (`FFmpegExtractAudio`), which ran ffprobe on a video-only stream and died
with `WARNING: unable to obtain file audio codec with ffprobe`. Audio buttons
happened to work by coincidence (they hit the `else` branch). The `target_fmt`
lookup silently returned `None` on the emoji, so the 2 GB premium gate and the
new disk pre-check were both bypassed.

**Fix:** `build_format_keyboard`'s `_btn()` now takes an explicit action token
(`"v"`/`"a"`) for the callback while keeping the emoji only in the button label.
The playlist path (`pl:` `vh`/`ah` tokens) was already correct and is untouched.

## What changed

1. **Size-aware disk pre-check** (commit `d9042af`) — `utils/downloader.py`:
   `required_merge_headroom(final_bytes) = final * 2 + 500 MB` models the merge
   peak (video part + audio + merged mp4 + metadata temp copy). `download_media`
   gained an `expected_size_bytes` param; the pre-download check and the metadata
   embed check now use it. The `dl:` dispatch in
   `modules/downloader_handler.py` checks disk **before** enqueueing and answers
   the callback with a user-facing alert if insufficient. On this machine the
   same 3.1 GB case passes: needs ~6.7 GB peak, we have ~82 GB free. This is a
   defensive hardening — it was NOT the actual cause of the failure (see Root
   cause above), but it stays because the pre-check was previously dead code
   (bypassed via the `target_fmt=None` emoji bug) and now runs for real.

2. **Provisioning** — `./install.sh` installed ffmpeg + deps, Deno 2.9.4
   (`~/.deno/bin/deno`, added to PATH by `run.sh` — the bot process finds it,
   but a bare interactive shell does NOT, so standalone yt-dlp tests need
   `export PATH="$HOME/.deno/bin:$PATH"`), python venv with yt-dlp
   2026.07.04 + bgutil provider ref 1.3.1, and the prebuilt Go monitor.

3. **Secrets copy** — `.env` (DOMAIN updated to `http://38.45.80.233:8080`),
   `cookies/` jars, `database.json`, `direct_forward_state.json`,
   `direct_ig_session.json` copied from the remote via `scp`. All jars re-locked
   `0o444` by the bot at startup. The remote has no `direct_x_cookies.json`
   (X relay was off).

4. **Remote retired** — `tgbot`, `tgbot-monitor`, `cookie-watch` on
   `66.23.198.52` are stopped + disabled. It still holds a full secret copy and
   is treated as a trusted backup until decommissioned. Do NOT paste its
   credentials into tracked files.

5. **cookie-watch fix** — `tools/cookie_watch.sh` was adapted to local paths
   and needs the `inotify-tools` package (`inotifywait`); without it the unit
   crash-loops in `activating`. After `apt-get install inotify-tools` +
   `systemctl restart cookie-watch` it is `active` ("Setting up watches.").

6. **Temp sudoers drop-in removed** — `/etc/sudoers.d/99-tgbot-dev`
   (`dev ALL=(ALL) NOPASSWD:ALL`) was created for install.sh and deleted
   afterwards; passwordless sudo is gone.

## Verified end-to-end

- `ss -tlnp`: python on `0.0.0.0:8080`, deno PO provider on `127.0.0.1:4417`
  only (localhost-bound per invariant #2).
- Bot log: 3× `Session initialized: Layer 158`, PO provider v1.3.1 healthy,
  Uvicorn "Application startup complete", DirectForward started (chat
  7429671248), IG anti-detect warmup ok.
- `curl http://127.0.0.1:4417/ping` → `{"server_uptime":...,"version":"1.3.1"}`.
- Full pipeline test (`PATH` incl. `~/.deno/bin`): extraction of
  `C6Q2ZjyKxa0` returned 25 video-only + 12 audio-only formats (2160p60 top);
  a real `format='137+140/best'` download + mp4 merge completed in ~25 s.

## Gotchas found

- The PO provider and the `n` challenge solver BOTH need deno on PATH. The bot
  inherits it from `run.sh`, but standalone yt-dlp calls from a bare shell fail
  extraction with `Requested format is not available` (n challenge unsolved →
  formats missing). Symptom is misleading — it's the missing JS runtime, not the
  URL.
- The read-only jar (`0o444`) makes yt-dlp throw `PermissionError` if you pass
  the real path; always copy to `/tmp` for manual tests, as the bot does via
  `cookie_manager.acquire`.

## Rollback

If this machine must give up production: copy the same secret files back to
`66.23.198.52`, re-enable + start its units, and stop `tgbot` here. The
remote `.env` DOMAIN would need its original `http://66.23.198.52:8080`.

---

## 9. Source: `docs/memory/tgbot-2026-08-13-propagation-dispatche-bugs.md`

# Telegram extras were silently dead under the "merge" — two pyrogram dispatch bugs — 2026-08-13

## Symptom (reported)

Testing the subscription implementation with a second account: `/start` → greeting,
`/subscription` → plans, **then the greeting again**. Separately: `/tr`, `/yt`,
`/search`, `/web` and pasted `github.com/...` links all returned **nothing** on
Telegram (same commands worked on the Bale frontend).

## Two independent root causes (both live in pyrogram's dispatcher)

### 1. `stop_propagation()`/`continue_propagation()` swallow — `utils/propagation.py`

`pyrogram.StopPropagation` and `pyrogram.ContinuePropagation` are BOTH
`Exception` subclasses (MRO: `… -> StopAsyncIteration -> Exception`). The ported
handlers all used:

```python
try: message.stop_propagation()
except Exception: pass
```

which **swallows the signal entirely**. The handler returns normally → the
dispatcher `break`s its group → control flows to the NEXT group. Two visible
results:

- **group 0 github link** (new) also processed as a **group-1 direct-file
  upload** → duplicate replies ("📥 Received URL. Queueing job…" + a wasted
  HTTP GET of github.com HTML).
- **group-1 greeting** re-fired the welcome after `/subscription` (the old
  "double greeting"); only the group-1 `/`-swallow guard masked it.

**Fix (systemic):** new `utils/propagation.py` with `stop()` / `continue_()`
that call the method but **re-raise** the real propagation signals
(`StopPropagation`/`ContinuePropagation`) while still swallowing genuinely
unexpected errors. Refactored all **37** buggy `try: …stop_propagation() except
Exception: pass` sites in `modules/{admin,subscription,github,youtube,
translate,web}` to use the helper.

### 2. `RawUpdateHandler` stalls a handler group (`_raw_precheckout`)

pyrogram's dispatcher treats a `RawUpdateHandler` as matching **every** update
(feeds it `(update, users, chats)` unconditionally); if its callback returns
normally the dispatcher `break`s **the whole group**, so any handler registered
AFTER it in the same group never runs.

The Stars `@app.on_raw_update(group=0)` pre_checkout handler lived in the
**middle** of group 0 (right after the subscription commands). The ported extras
(translate/web/github/youtube) re-registered later in group 0 → **starved →
silently ignored all input**. `/subscription` still worked because it was
registered BEFORE the raw handler.

**Fix:** `_raw_precheckout` now `raise pyrogram.ContinuePropagation` both for
non-pre_checkout updates (let later group-0 handlers run) and after handling a
real pre_checkout (harmless continue). The raise is OUTSIDE the helper's try
block since `ContinuePropagation` is an `Exception`.

## Verification (driven live via Telethon)

| Action | Before | After |
|---|---|---|
| `/tr fa:en سلام` | no reply | `🈯 Translation (fa → en) hello` |
| `/subscription` | plans + greeting | plans only |
| `github.com/owner/repo` | browser panel + **Direct Upload** | browser panel only |
| `/search <q>` | (starved) no reply | reaches handler + results |
| `/web <url>` | (starved) | `🌐 Webpage: Example Domain …` Markdown |

`/search salehMomtaz` returning "No repositories found" is CORRECT (that's a
user, not a repo); `/search django` works.

## POT provider "goes down" — answered (no code change)

Saw `[POT] Provider process is gone (returncode=-15); will restart`. `-15` =
SIGTERM. Two distinct causes:

- **Ancient flap (already fixed in HEAD `36505e6`, 07:17):** `health_check_loop`
  slept 10s and restarted on "proc gone" without logging returncode → rapid
  restart loops under load (Bale + Telegram share one Deno POT on 4417). Now:
  sleep 30s + returncode logged + backoff.
- **Expected SIGTERM-on-restart:** every `systemctl restart tgbot` SIGTERMs the
  bot → graceful teardown calls `pot_manager.stop()` → `proc.terminate()` →
  deno exits `-15` → next boot's health loop restarts it ~5s later. The 09:50 /
  09:56 `-15`s during this session were the redeploys. Provider is healthy on
  `127.0.0.1:4417` (loopback-only, invariant #2 intact).

## Architecture note (shared vs divided)

Confirmed the intended split — this is a SHARED core, two THIN frontends, no
duplicated shared logic:

**Shared (one copy, both endpoints):** `config`, `utils/shared.queue`,
`utils/gate`, `utils/downloader/*` (extract/download/normalize/playlists/
cookies), `utils/cookie_manager`, `utils/pot_provider`, `utils/queue_manager`,
and the transport-free extras `modules/{github,youtube,translate,web}`.

**Divided (per-endpoint, never duplicated):** Telegram transport
`modules/{admin,downloader_handler,subscription,stream_handler}` (pyrogram, FULL
console); Bale transport `modules/bale/` (aiogram, LIMITED console, own 20 MB
uploader `clean_caption_text`/`sanitize_filename_for_bale`, own `BALE_*`
creator id). The Bale runner imports the shared downloader + shared extras.

## Files changed

- `utils/propagation.py` (new)
- `modules/{admin,subscription,github,youtube,translate,web}` (37 swallow-sites → helper; `_raw_precheckout` raise-on-mismatch)
- `AGENTS.md` (new invariants 19 + 20; reworked "When porting from balebot")

---

## 10. Source: `docs/memory/tgbot-subscription-system.md`

# Subscription system (2026-08-12/13) — toggleable, Stars/TON, free-tier channel gate, WebApp, priority queue

Operator asked for a full subscription redesign: `whitelist/blacklist` stays as before, but a **toggleable** subscription layer sits on top. Those who deployed the bot without wanting subscriptions keep the old block-if-not-whitelisted behaviour (intruder → blacklist; admin copies numeric id from blacklist and whitelists — auto-removing the blacklist entry). When the toggle is ON, the bot shows subscription/channel prompts instead of blacklisting strangers.

This doc captures the design + gotchas so the next change doesn't re-break it.

## 1) Toggle + datastore

- Store lives **inside `database.json`** (no new file): `sub_settings`, `subscriptions`, `usage`, `sub_used_tx`.
- `utils/subscription/store.py` is the only writer (thread-lock + `utils/gate` save path). `config.py` seeds defaults from `.env` (`SUB_ENABLED`, `SUB_FREE_ENABLED`, `SUB_CHANNEL_*`, `SUB_TON_*`) but DB is the source of truth at runtime.
- `DEFAULT_SUB_SETTINGS`: `enabled`, `free_enabled`, `channel_id`, `channel_username`, `channels[]`. Legacy single fields migrate into `channels` on first load (so old installs don't lose their channel). `channels` is a list `[{id,int, username,str}]` — multi-channel force-join (see §3). Admin console always writes `channels`; legacy fields are kept in sync (first entry).
- Three paid tiers (`utils/subscription/tiers.py`): `basic 100/d 100⭐`, `plus 500/d 250⭐`, `pro 2500/d 500⭐` (30 d each). `free` is 5/d, priority 0. TON equivalents (`price_ton`) map 1:2 ratio for fee math. `priority`: free 0, basic 1, plus 2, pro 3; creator is implicit pro (until `9999999999`).
- `set_subscription()` extends from existing expiry if active, **auto-removes from blacklist** and **adds to `authorized`** — the whitelist/blacklist invariant: *subscribing (or whitelisting) never leaves a blacklist entry*. `utils/gate.add_user()` already did `remove blacklist`; this mirrors it.
- Creator is always active (no quota). Every other `is_subscription_active()` is `until > now`.

## 2) Security gate — the blacklist vs. subscription tension

Historical gate (`modules/admin/register.py` group -1) did: `if not authorized → blacklist + drop`. That kills subscriptions: a free/paid user who isn't in `authorized` would be blacklisted on first message and never see a prompt.

Fix: **subscription-aware gate** (see `register.py:security_gate`).

- If `sub_settings.enabled == false` → legacy: same as before.
- If enabled: only `blacklisted` is dropped. For everyone else:
  - `is_subscription_active` → pass.
  - `free_enabled` → pass (channel check is deferred to the downloader gate / Group 1 welcome, not here — we can't `get_chat_member` without `client` in the gate without blocking).
  - `is_authorized` → pass.
  - Otherwise → **do NOT blacklist**; let the message continue to Group 1 so `check_access()` / `gate_and_quota_check()` can render a proper `🔒`/channel-join UI instead of a silent drop.
  - Non-link chatter (hi/start) now shows a subscription prompt via `admin_start_text_handler` (it calls `check_access` again and shows tier keyboard or missing-channels list). Links go via `gate_and_quota_check` in `downloader_handler`.

This preserves the old admin-friendly “friend messages, I copy id from blacklist” workflow when the toggle is OFF, and avoids spamming blacklist when it's ON.

## 3) Free tier: two modes + multi-channel membership

Spec: free tier either **no channel** (just 5/d) or **must join channel(s)**; bot must list which channels the user hasn't joined.

- Single-channel legacy: `SUB_CHANNEL_ID`/`SUB_CHANNEL_USERNAME` (resolved via `get_chat` to id for `get_chat_member`). Now `channels[]` holds N entries.
- Membership check (`utils/subscription/access.py:check_all_channels`): iterates `get_channels()`, calls `is_channel_member(client, user_id, channel_id)` (or resolves username→id if only username stored). Returns `(all_joined, missing_list)`.
- `check_access()` → `need_channel` when any missing. Downloader gate and the Group 1 welcome both call `check_all_channels` to enumerate missing channels, build per-channel `t.me/<handle>` join buttons, and show a prompt like “Free access requires joining: @ch1, @ch2 — join all then retry, or /subscription”.
- Admin console: Subscription menu now shows `Channels: a,b` and has **➕ Add channel / ➖ Remove channel** (states `waiting_for_sub_channel_add|remove`). `waiting_for_sub_channel` (legacy name) is kept as alias to Add. `0`/`clear` clears all. New helper `add_channel`/`remove_channel` in store dedupes and keeps legacy single fields in sync.

## 4) Quota + priority

- `utils/subscription/quota.py`: `_daily_limit_for()` reads tier or free limit; `check_quota()` / `remaining_quota()` / `increment_quota()`.
- Bug fixed: `increment_quota` previously allocated a **new** `threading.Lock()` per call (no mutual exclusion). Now uses module-level `_QUOTA_LOCK`. Pruning previously had nested loop deleting `0..len-8` inside `for d in keys` (deleted repeatedly); now keeps at most 7 per user sorted.
- `utils/queue_manager.py:DownloadQueue.add_task()` resolves `priority` from subscription tier (free 0 < basic 1 < plus 2 < pro 3, FIFO within same priority, higher jumps ahead). Creator / tier `pro` goes first. If subscription mode off, default priority 1 (legacy). Checked via `is_subscription_active` at enqueue time; actual download also re-checks quota at execution (queue may have waited past midnight).
- `downloader_handler` calls `gate_and_quota_check` before any download, and `check_quota` + `increment_quota` after successful upload/playlist item. Playlist job per-video checks quota so hitting limit mid-playlist aborts remaining items with a clear message.

## 5) Payments: Stars (XTR) + TON/Gram

- **Stars** (`utils/subscription/payments_stars.py`): raw Bot API `sendInvoice` with `currency=XTR`, `provider_token=""`, `payload=sub:<user_id>:<tier>:<HMAC16>`. HMAC key is `BOT_TOKEN`; sig window current + previous hour. `create_stars_invoice()` is called from `/subscription` → tier button → stars. Pre-checkout (`UpdateBotPrecheckoutQuery`) is handled via `app.on_raw_update` (pyrogram has no high-level filter) — verifies payload, user match, answers `ok`. `successful_payment` handler verifies amount ≥ price, activates tier for `duration_days`, logs.
- **TON** (`payments_ton.py`): inbound-only verification via `toncenter.com/api/v2/json/getTransactions` for `SUB_TON_ADDRESS`. User sends `price_ton` to address with **memo = user_id** (digits). `verify_ton_payment(user_id, tier)` scans last 50 tx, checks `value >= price`, memo matches, tx hash not in `sub_used_tx` (dedup), then `set_subscription(..., granted_by="ton")` and marks tx used. Called via `/subscription` → TON button → instruction card (address + memo) → “I sent it — Verify” → runs in executor (sync urllib). `X-API-Key` via `SUB_TON_API_KEY` optional. No private key needed.
- Both flows share `tiers.py` amounts; `handlers.py:_tiers_keyboard` shows stars rows always and TON rows only when `TON_ADDRESS` set.

## 6) WebApp / Mini App (admin + user portal) — https://tgbot.southpark.ir:8080 (direct TLS)

Spec asked for an enhanced admin console as Telegram Mini App (https://core.telegram.org/bots/webapps), admin-only if possible, otherwise a user status portal.

- **Direct TLS (no nginx)**: wildcard `*.southpark.ir` cert at `/etc/letsencrypt/live/southpark.ir/{fullchain,privkey}.pem` (valid `tgbot.southpark.ir` via `*.southpark.ir`), copied to `certs/fullchain.pem`+`privkey.pem` by `install.sh` + renewal hook `/etc/letsencrypt/renewal-hooks/deploy/tgbot-copy.sh` (auto `chown dev:dev`, `chmod 600` + `systemctl try-restart tgbot`). `DOMAIN=https://tgbot.southpark.ir:8080`, `SSL_CERT_PATH`/`SSL_KEY_PATH` point to `certs/` (so `uvicorn` terminates TLS on `:8080` directly; `avistel.ir` wildcard was previous, now `southpark.ir` because `*.avistel.ir → 94.159.109.54` not this VPS `38.45.80.233`). `deploy/tgbot.southpark.ir.conf` kept as nginx reference, not enabled.
- **FastAPI mount** (`modules/subscription/webapp.py:mount`): mounts on the existing port-8080 app (already shared with `stream_handler`+`pot_provider`), now also `GET /` landing with Telegram auto-redirect, `GET /api/botinfo` (`getMe` cached 1h). The three HTML pages share `_SHARED_UI` (safe-area `safeAreaInset` + `env(safe-area-inset)`, sticky header `padding-right:56px` to avoid TG close button, `viewport-fit=cover`, `backdrop-blur`, `color-scheme:dark`, toast-stack + modal + `UI.showPopup` native fallback + `HapticFeedback`).
  - `GET /` → `HTML_ROOT` landing: outside Telegram shows Welcome + plans (`/api/tiers`) + `https://t.me/<bot>` + `/app`/`/admin` links; inside Telegram (`tg.initData` present) auto-fetches `/api/user/status` and redirects creator → `/admin/subscription`, others → `/app`.
  - `GET /admin/subscription` → `HTML_ADMIN` panel: toggle enabled/free, multi-channel textarea, tier table, active subs list, token box (`/admin_token`). Auth: `X-Admin-Token == HMAC(BOT_TOKEN, "admin-sub")[0:16]` (`/admin_token` command) OR Telegram `initData` where `user.id == SYSTEM_CREATOR_ID` (HMAC-SHA256 via `WebAppData` key, per docs). GET `/admin/subscription/api` now **requires admin auth** (previously open); returns settings/tiers/active. Errors now show bounded `⛔` card (`border-color:rgba(255,69,58,.35)`) + native `tg.showPopup` fallback to custom modal + toast, with 3-step how-to, not raw `{"detail":…}` string. Header no longer shows `tgbot.southpark.ir:8080` on top-right; subtitle only.
  - `GET /app` → `HTML_USER` portal: any Telegram user via WebApp (valid `initData` required). Shows tier badge, until, quota `remaining/limit`, used today, 7-day history, upgrade cards. “Buy” routes to bot (`/subscription`) + `UI.toast`. Outside browser shows friendly `🔒 Telegram auth required` card with public tier table, not raw JSON.
  - `GET /api/tiers` (public), `GET /api/user/status` (valid initData, any user) → subscription/quota/history/settings. User status does **not** leak admin-only data.
  - `GET /api/botinfo` (public, cached `getMe`) → `username` for landing `https://t.me/<bot>` link.
  - `POST /admin/subscription/api` admin write with full channel list sanitization; logs via `log_event`.
- Telegram side: Bot exposes WebApp via `MenuButton` URL `https://tgbot.southpark.ir:8080/` (root auto-redirects) or inline `web_app={"url":"https://tgbot.southpark.ir:8080/app"}` for users and `.../admin/subscription` for admin; both work with direct TLS on `:8080` (wildcard `*.southpark.ir`), no nginx.

## 7) Instagram fallback credentials

Operator supplied `IG_DIRECT_USERNAME=salehmomtaz03`, `IG_DIRECT_PASSWORD`, `IG_DIRECT_TOTP_SEED=6OX5IDHO2CL67FAC3XB5G5MXRGSOYYY7` (base32 2FA). These are stored **only in `.env`** (git-ignored); `.env.example` shows placeholders. `config.py` already wired `IG_DIRECT_USERNAME/PASSWORD/TOTP_SEED`; the IG worker (`modules/direct_forward/instagram.py`) uses them as fallback when `igcookies.txt` jar is stale (after `instagrapi` fails to validate session). No file is committed. (Memory file redacts the password.)

## 8) Security hardening applied

- `utils/security.py`: per-user flood tracker `is_flood(user_id, 60s, limit)` tier-aware (free 5/min, basic 8, plus 12, pro 20, creator 30) applied in `downloader_handler.text_link_handler` before any download; `is_safe_url`, `redact_token` (bot-token regex `[REDACTED_TOKEN]` in logs), webapp IP rate limit helper.
- SSRF guard already existed (`_is_ssrf_target` in downloader_handler) — kept.
- Log redaction: `utils/logger.py:TelegramChannelHandler.emit` now calls `redact_token` before escaping/sending (so token never hits log channel even if a traceback includes it).
- WebApp initData validates `hash` via HMAC-SHA256 (`WebAppData` → `BOT_TOKEN`) and optional `auth_date` freshness; admin write requires creator id.
- `install.sh` keeps `ulimit -v` absent, wildcard-only PO provider, etc. — invariants untouched.
- `admin_token` command gives short-lived HMAC token for non-Telegram WebApp access.

## 9) Invariants preserved

- Whitelist add removes blacklist (both `gate.add_user` and `store.set_subscription`).
- Subscription toggle off = legacy blacklist behaviour, no prompt.
- Multi-channel set via admin console uses `add_channel`/`remove_channel` (merge-only), not a blind overwrite, so concurrent direct-forward state race fixes stay intact.
- Queue priority never starves free indefinitely — free still runs, just last.
- Cookie/PO invariants untouched; WebApp + nginx are extra routes on same 8080 port, no extra process.

See also: `config.py` (SUB_*), `docs/UBUNTU_VPS_SETUP.md` (§ subscription), `blueprint.md` Phase 21, `deploy/tgbot.southpark.ir.conf` (reference, direct TLS is primary), `modules/subscription/webapp.py` (`_SHARED_UI`, `HTML_ROOT`/`HTML_ADMIN`/`HTML_USER`).

---

## 11. Source: `docs/memory/tgbot-premium-4gb-whitelist.md`

# 4 GB uploads: Premium userbot + per-user admin whitelist

**Date:** 2026-08-05 · **Area:** `utils/uploader_handler.py`,
`utils/gate.py`, `modules/downloader_handler.py`, `modules/admin.py`,
`modules/direct_forward.py`, `config.py`

## The hard constraint (research findings)

Bots **cannot** upload more than 2 GB — this is not a "setting", it's enforced
server-side. Telegram's own tdlib/telegram-bot-api team confirmed it in
[tdlib/telegram-bot-api#583](https://github.com/tdlib/telegram-bot-api/issues/583):

> "Bot API server supports uploading of files of any size allowed by Telegram,
> but the user must be a Premium user to be able to upload files bigger than
> 2000 MB. **Bots can't be Premium users**, therefore they aren't allowed to
> upload files bigger than 2000 MB."

Options evaluated for the 4 GB path:

| Option | 4 GB capable? | Why |
|---|---|---|
| Bot API (public HTTP) | ❌ | Hard 2 GB server-side cap for bot tokens |
| Local Bot API server (self-hosted tdlib) | ❌ | Same bot-account cap — the userbot is a user, the bot is a bot |
| **Pyrogram user session (`PREMIUM_STRING_SESSION`)** | ✅ | MTProto **user** account; a Premium user can upload 4 GB |
| Telethon user session | ✅ | Identical MTProto limits, no advantage over pyrogram |
| Passing a `file_id` of an already-uploaded big file | ❌ | You'd still need a premium user to do the original upload |

**Conclusion: the only viable 4 GB path is a Premium *user* account over MTProto.**
The bot already had this wired (`premium_app` in `main.py`, built from
`PREMIUM_STRING_SESSION`); Telethon adds nothing over the existing pyrogram
client (same protocol, same limits, same session-string mechanics). A local Bot
API server also cannot lift the 2 GB bot ceiling. So no library swap — the work
was controlling *who* gets to use the premium path.

## The bug being fixed

Before this change the premium uploader client was used **globally**: any file
over 2 GB went through the Premium userbot for **every** user, as long as a
session was configured. There was no way to restrict 4 GB uploads to specific
users, and non-whitelisted users saw the full format list including impossible
>2 GB options.

## Implementation

- **`utils/gate.py`** — new `premium_users` list in `database.json` (auto-
  migrated for existing DBs) + `is_premium_user` / `add_premium_user` /
  `remove_premium_user`. `SYSTEM_CREATOR_ID` is implicitly premium.
- **`utils/uploader_handler.py`** — `process_split_and_upload` and
  `send_single_media` take an explicit `premium_allowed: bool | None = None`.
  `None` → inferred from `is_premium_user(chat_id)` (in a private chat
  `chat_id == user_id`). Both the split-size choice and the send client use the
  same flag, so the button size and the actual upload always agree.
- **`modules/downloader_handler.py`** — the `>2 GB` format guard at `dl:`
  dispatch now requires `premium_app` AND `is_premium_user(user_id)`.
  `build_format_keyboard` locks (🔒) >2 GB options for non-whitelisted users and
  routes them to a "Premium required" answer; the header notes the 🔒 meaning.
- **`modules/admin.py`** — new "👑 Premium Uploads" console button (badge shows
  whether a session is configured), menu with Add/Remove Premium by ID,
  mirroring the existing Add/Remove User state flow. The menu explicitly warns
  when `PREMIUM_STRING_SESSION` is empty (4 GB disabled).
- **`modules/direct_forward.py`** — the operator's own DM-relay pipeline passes
  `premium_allowed=True` explicitly: the relay chat (`DIRECT_FORWARD_CHAT_ID`)
  may differ from the creator's id, and the operator configured the userbot
  themselves, so relays are not gated on the whitelist.

## Decisions worth keeping

- **The creator is always premium** — they own the session; without this the
  operator could lock themselves out of 4 GB uploads.
- **Relay is always premium** — the relay chat is the operator's own pipeline;
  do not gate it on the interactive whitelist.
- **The whitelist is the whole point.** The 4 GB path must stay per-user; if a
  future change makes it global again, that's a regression (and the admin
  console becomes decorative).

## In-chat session-string generation (2026-08-05)

The terminal `generate_session.py` flow is gone; the admin generates the
`PREMIUM_STRING_SESSION` entirely from the 👑 Premium menu
(`🔑 Generate Session`):

- **`utils/premium_session.py`** — thin wrapper over a temporary **in-memory**
  pyrogram client (`Client(":memory:", api_id=…, api_hash=…)`) that runs the
  interactive login: `send_code` (→ `phone_code_hash`), `sign_in` (raises
  `SessionPasswordNeeded` when 2FA is on), `check_password`, then
  `export_session_string`. `save_session_string` persists it to `.env` via
  `dotenv.set_key` (dotenv-style quoting — exactly what `run.sh`'s parser and
  `config.py` expect) and refreshes `config.PREMIUM_STRING_SESSION` in memory.
  The temp client **never writes a session file** (":memory:") and is always
  disconnected via `discard_client`.
- **`modules/admin.py`** — `admin_premium_gen` starts the flow, then the three
  states `waiting_for_premium_phone` / `_code` / `_password` accept free-form
  text. They are dispatched **before** the `is_valid_telegram_id` gate (they are
  not user IDs). Every step carries a **❌ Abort Session Generation** button
  (`admin_premium_gen_abort`); a finished flow shows the string in a code block
  with **💾 Save to .env** (`admin_premium_gen_save`, writes via
  `save_session_string`) or **❌ Discard**.
- **Cleanup invariants** — the temp client is disconnected on: completion
  (before the result is shown), abort, `/start` escape, leaving to `admin_main`,
  reopening the premium menu, or TTL expiry. `PREMIUM_GEN[user_id]` holds
  `{client, phone, phone_code_hash, result, expires_at}` (15-min login TTL, 5-min
  result TTL). `sweep_stale_generations(client)` is a module-level background
  sweep driven by `utils.keyboard_expiry.expiry_loop` so a dangling temp login
  can never leak even if the admin walks away mid-flow.
- **Closure gotcha (fixed 2026-08-05):** `register_admin_handlers(app)` names
  its closure parameter `app`, NOT `client`. A first-pass implementation wrote
  `purge_active_prompt(user_id, client)` inside `_premium_gen_cleanup`, which
  threw `NameError: name 'client' is not defined` on every
  `admin_premium_gen` / `_abort` callback — the button looked completely dead
  (unanswered callback = stuck spinner). Always reference the client via the
  enclosing scope's real parameter (`app`). Message/callback handlers define
  their own `client` parameter, so `client` is only valid *inside* them.
- **Callback safety net:** `admin_callback_handler` is now a thin try/except
  wrapper over `_admin_callback_dispatch` — any dispatch error logs to the
  channel and answers the callback with an alert instead of hanging the
  spinner. Admin callback branches are a huge elif-chain; keep new branches
  inside the dispatch.
- **"🧹 Cleanup Stale Gen" is its own callback** (`admin_premium_gen_clean`).
  It originally shared `admin_premium_gen_abort`, so clicking it on the menu
  re-edited the PREMIUM menu into an "aborted" message (and repeated presses
  produced `MessageNotModified`). The menu button now re-renders the menu after
  sweeping; the abort callback is only for the in-flow Abort button.
- The generated string is **sensitive** (full account access) and is shown in
  the private chat; a restart is required after saving before the Premium
  userbot actually uses it.

## Dial-pad code entry — never type the login code in chat (2026-08-06)

The login code must be entered via a **numeric dial pad**, not as chat text.

- **Root cause:** Telegram's anti-account-sharing detection. Typing the code as
  a chat message caused a security notice and `PHONE_CODE_EXPIRED` seconds after
  `send_code`: *"The code was entered correctly, but sign in was not allowed,
  because this code was previously shared by your account."* The digits travel
  in **callback data**, never as chat text, so the detection is not triggered.
- **`_gen_dial_pad_markup`** — a 3×4 numeric keypad (rows 1-9, then
  `⌫ / 0 / ✓`, plus a full-width **❌ Abort** row). Callback data:
  `admin_premium_gen_digit:<d>`, `admin_premium_gen_bksp`, `admin_premium_gen_enter`.
- **`PREMIUM_GEN`** gains `"code_buffer": ""`. `_premium_gen_pad_text` re-renders
  the Step 2/3 message with an "Entered so far:" line; it tolerates
  `MessageNotModified`. The prompt id is re-registered in `ACTIVE_PROMPTS` so the
  dial pad stays alive while text flows.
- **`waiting_for_premium_code` text input now rejects typed codes** with an
  explanatory reply ("don't type the code, use the keypad") instead of accepting
  them — a typed code would burn the login. The 2FA step (`waiting_for_premium_password`)
  stays free-form text (a password is not a login code).
- **Callback semantics:** digits append (cap 6, `>4` required), backspace pops,
  enter validates ≥4 digits; on `SessionPasswordNeeded` it switches to the 2FA
  step, otherwise it verifies the code and proceeds to export. On any exception
  the buffer is reset and the pad re-rendered — the flow never dies from a bad
  entry. On success the dial-pad message is edited to "✅ Code accepted — logging
  in and exporting the session string…" before `_finish_premium_gen`.
- Verified working live on the VPS (2026-08-06); the flow generates and exports
  the session string entirely from the phone.

## Self-restart after saving — no shell access needed (2026-08-06)

`admin_premium_gen_save` previously printed `sudo systemctl restart tgbot`,
forcing an SSH+sudo session — the exact thing the in-chat flow existed to remove.

- **`main.py::schedule_self_restart(delay=3.0)`** — after the "saved" message is
  rendered, the bot restarts **itself**:
  - Under systemd (`INVOCATION_ID` is set for systemd services — verified on the
    VPS), it sends `SIGTERM` to its own PID. `main.py`'s existing `_on_sigterm`
    handler turns that into `KeyboardInterrupt`, which drives the same graceful
    teardown systemd uses on `systemctl restart`: pyrogram drains, the PO-token
    provider stops (`PotProviderManager.stop`), cookie locks are released. The
    process exits and `Restart=always` + `run.sh` relaunch it, re-reading `.env`
    — so the fresh `PREMIUM_STRING_SESSION` is picked up.
  - Without systemd (tmux/foreground dev), it falls back to `os.execv` in place.
- **Call sites:** `admin_premium_gen_save` (after `save_session_string`) and the
  admin console's **🔄 Restart Bot** button (see below). The 3 s delay lets the
  confirmation message and log line flush before teardown.
- Verified on the VPS: `kill -TERM <MainPID>` → `KeyboardInterrupt` graceful path
  → systemd relaunches with a new MainPID and restart counter +1. The dial pad +
  save flow itself was tested live by the operator.

## Admin console "Restart Bot" button (2026-08-06)

The main admin console now exposes the same self-restart to the operator, so the
bot can be rebooted entirely from chat — no SSH, no `systemctl`.

- **`build_console_keyboard`** gains a `🔄 Restart Bot` row paired with
  `❌ Close Console` (`admin_restart`).
- **`admin_restart`** renders a confirmation dialog ("Restart the bot? …Any
  running download will be interrupted and the queue cleared.") with
  `✅ Yes, restart now` (`admin_restart_confirm`) and `↩️ Cancel` (`admin_main`).
  It also pops any stale `USER_STATES`/`ACTIVE_PROMPTS` first.
- **`admin_restart_confirm`** edits the message to "🔄 Restarting the bot…",
  logs to the channel, answers the callback, then calls
  `schedule_self_restart(delay=3.0)` — the exact mechanism above.
- The dispatch branch lives inside `_admin_callback_dispatch` right after
  `admin_premium_gen_save`; it references only `callback_query`/`user_id`/
  `log_event`/`schedule_self_restart` (never `client` directly), so it is immune
  to the `app`-vs-`client` closure gotcha.
- Verified live on the VPS: the SIGTERM path produced `Stopping bot
  gracefully...` in the journal, systemd scheduled the restart, and the bot came
  back active with a fresh MainPID. The button itself (edit → confirm → restart)
  exercises that same code path.

## >2 GB delivery: stage-to-log-channel + bot relay (2026-08-06)

The first live 3.1 GB test failed on two independent bugs, then the delivery
path itself was redesigned.

### Root cause #1: `dl:` callbacks carried labels, not tokens

Format buttons were registered with `dl:<cache_id>:<emoji>`-style data, but the
`dl:` dispatcher switched on `:v:` / `:a:`. The emoji→`v`/`a` fix (`f609b38`)
made the video/audio path route correctly. This surfaced while re-testing the
premium flow.

### Root cause #2: `from main import premium_app` created a zombie client

`modules/downloader_handler.py` did `from main import premium_app` at import
time. Since `main.py` runs as `__main__`, this re-imported it as a *separate*
`main` module whose module-level `premium_app` was a second, **never-started**
pyrogram Client whose `.me` is `None`. Premium uploads died instantly
(`AUTH_KEY_UNREGISTERED`-class errors). Proof: `m.premium_app is ns['premium_app']`
→ `False`.

**Fix:** `register_downloader_handlers(app, premium_app)` now receives the
already-started instance from `main_engine()` (the import line is gone).
Verified: incremental uploads work after restart.

### The redesign: bots can't upload >2 GB, so the bot relays a copy

Bots are hard-capped at 2 GB; only a Premium *user* can push 4 GB. The old
design sent the big file **as the premium userbot** — correct upload, wrong
sender, and it bypassed the reply-to-link quoting.

New path in `utils/uploader_handler.py::_stage_and_relay` (used when
`use_premium and config.LOG_CHANNEL_ID`):

1. The premium userbot uploads the raw file to `LOG_CHANNEL_ID` with a
   "📦 Staged for delivery" caption. The operator is the channel admin, so the
   file is visible there and doubles as a permanent record.
2. The bot calls `copy_message(chat_id, from_chat_id=LOG_CHANNEL_ID,
   message_id=staged.id, caption=<user caption>)`. `copy_message` forwards by
   `file_id` — **no size limit**, the file already lives on Telegram's CDN. The
   sender shows as the bot, and `reply_to_message_id` makes it quote-reply to the
   user's link (invariant #14).
3. If staging throws for any reason, `send_single_media` logs a warning and
   falls back to the **direct premium send** so the file still reaches the user.

The staged message deliberately stays in the log channel ("Keep in log channel"
choice). Verified end-to-end with a 3.1 GB video (`C6Q2ZjyKxa0`, 3277923411 B):
staged as log msg 14894, delivered as bot msg 88093 replying to 88086 (the
user's link), sender 7665239058 (the bot).

### Test driver (tools/)

`tools/telethon_login.py` (one-time operator login → `telethon_session.txt`,
git-ignored) + `tools/telethon_drive.py` (send a link/message, press inline
buttons by substring, pick v/a from the live keyboard, assert size ranges;
handles both `NewMessage` and `MessageEdited` since tier keyboards arrive as
edits). All flows below were exercised with it on the production box:

- single video (`aqz-KE-bpKQ` 480p → 59.2 MB, msg 88103, reply-to link),
- audio (`a:258` → 30.7 MB m4a),
- playlist tiers (`pl:*:whole` → `pl:*:vl` → 2 videos),
- direct-file download (README.md doc),
- format-menu Cancel (dismisses, no job),
- the whole admin console (List Users, Blacklist, PO Token, Direct-Forward,
  Cookie Jars, Premium menu, Doc Mode toggle, Abort Transfer, Close, and a live
  Restart-Bot cycle: self-SIGTERM → systemd relaunch → bot responds, `NRestarts=1`,
  no crash loop).

## Inline-keyboard auto-expiration (2026-08-05)

`utils/keyboard_expiry.py` strips unused inline keyboards so chat history does
not accumulate dead buttons:

- Registry keyed by `(chat_id, message_id)` — message ids are **only unique per
  chat**, so a single-`message_id` key would let two users' keyboards collide
  (both chats frequently land on the same small message id).
- `watch(chat_id, message_id)` is called by `main.py`'s send/edit monkeypatches
  whenever a message carries a `reply_markup`; `touch(chat_id, message_id)`
  resets the 20-min deadline from the group `-2` callback interceptor (every
  button press keeps the keyboard alive); `expiry_loop` runs every 30 s,
  strips up to 50 expired `reply_markup`s per tick, and also drives
  `sweep_stale_generations` above.

---

## 12. Source: `docs/memory/tgbot-friend-media.md`

# Friend Media Archiver — design, invariants & hardening (2026-08-24)

`modules/friend_media/` archives **friends'** media using the operator's
PREMIUM user-account session (`premium_app`) as the *reader* and the BOT as the
*delivery* channel. Bot accounts cannot read another user's profile-photo
history; only a real user account can. The premium session is already used for
4 GB uploads; here it doubles as the archiver's eyes.

## What it does

Per friend record (key `tg:<handle>` or `<id>`, or `ig:<username>`):

- **Telegram profile pictures**: full backfill (oldest→newest, one friend had
  1927) + incremental checks that deliver only *new* photos.
- **Telegram stories**: deduped via `seen_story_ids`.
- **Instagram stories + posts** (best-effort): posts are gated by a
  `last_ig_media_pk` **watermark — the first run primes the watermark and
  delivers NOTHING**. This enforces the operator's constraint: never fetch
  older IG content; only new posts after the friend was added.
- **Full archive (zip)** (per-friend "🗂 Archive" button): downloads the IG
  profile picture (HD), every feed post/carousel/reel, and every highlight
  (each story media inside), zips them, and delivers the single zip. Every
  fetch/download step is paced with a human-ish jitter (`_jitter`,
  `random.uniform(0.4, 1.6)`) to avoid automation-flagging.
- Delivery: default destination is the **log channel**. The premium user
  account uploads the media to `LOG_CHANNEL_ID` (the archive), then the BOT
  `copy_message`s it to the creator DM — **no "Forwarded from" header** (sender
  shows as the BOT), no re-download, no size limit, and it can quote-reply to an
  existing bot-chat message. `forward_messages` was the ORIGINAL approach and
  stamped the unwanted header — see `docs/memory/tgbot-copy-message-vs-forward.md`;
  `copy_message` is the corrected pattern, mirroring
  `utils/uploader_handler.py::_stage_and_relay` (the >2 GB premium path).
  Alternatives: premium account's Saved Messages, or an explicit chat id.

## Non-negotiable rules baked into the code

1. **NEVER message anyone.** The only friend-touching operation is a silent
   `add_contact` (so the account can see restricted profiles). No DMs, no
   comments, no reactions — ever.
2. **Incremental, not re-spam.** Every photo/story/post id is recorded in
   `cache/friend_media_state.json` (`seen_photo_ids`, bounded at 5000,
   `seen_story_ids`, `seen_ig_story_pks`, `last_ig_media_pk`). Re-checking a
   backfilled friend delivers "0 new" — verified live.
3. **Backfill is crash-safe**: state checkpoints every 10 deliveries while
   walking oldest→newest, so an interrupted backfill resumes where it stopped.
4. **IG watermark**: `archive_instagram_posts()` refuses to deliver anything
   before `last_ig_media_pk` is primed. Deleting the state file would re-prime
   from newest — acceptable (still never fetches old content).
5. **instagrapi client is cached** module-level (30-min TTL, invalidated when
   the igcookies jar mtime changes). Never build+login per call.
6. **All archives serialize** behind `_ARCHIVE_LOCK` (asyncio.Lock); the
   auto-loop self-gates each cycle on live `config.FRIEND_MEDIA_ENABLED`
   / `FRIEND_MEDIA_SCHEDULE_MINUTES` with jittered sleep (never fixed cadence).

## Console (Admin → 📸 Friend Media)

Everything is in-chat, persisted via `_persist_env` = `dotenv.set_key('.env')`
+ `setattr(config)` (survives restart, applies live):
enable/disable toggle, schedule minutes, destination, IG-global toggle.
Contacts tools: 📇 browse (paginated), 🔎 search, 📞 add-by-phone
(`import_contacts`). Per-friend: ⬇️ full backfill button, IG stories/posts
toggles, 🗂 Archive (zip). Adding a friend auto-starts its backfill task.

The **friends list is SPLIT** into "📋 TG Friends" (have `telegram_user_id`) and
"🟣 IG Friends" (have `ig_username`) — a friend with both appears in both.
Each list has **☑️ Select** (multi-select) + **🗑 Delete all** with confirmation;
select-mode adds **🗑 Delete selected**. Routing: `fm_list_choose` → `fm_list_view`
/ `fm_list_sel` / `fm_sel_toggle` / `fm_sel_delete` / `fm_delall_*`.

Text states dispatched before the id gate in `register.py`:
`waiting_for_friend_add|_ig:<key>|_dest|_schedule|_search|_phone`.

## Hard-won gotchas

- **`from main import X` re-executes main.py** when running as a script:
  submodules got UNSTARTED Client copies ("Client has not been started yet").
  Fix: `sys.modules.setdefault('main', sys.modules[__name__])` right after
  imports in main.py, AND the `__main__` entry block must stay at the VERY
  BOTTOM of main.py (after `schedule_self_restart` def) — otherwise a
  partially-initialized duplicate module breaks self-restart (caused a ~31
  restart crash-loop during testing).
- **Digit strings are phone numbers to kurigram**: `get_users("7665239058")`
  → PHONE_NOT_OCCUPIED. Pass numeric ids as `int`; fall back to a
  `get_contacts()` scan by id.
- **aiogram overwrote SIGTERM** (`dp.start_polling` installs its own handler),
  so SIGTERM only stopped Bale polling and the process hung — Restart Bot
  button dead, systemd restarts hung. Fix: `handle_signals=False` in
  `modules/bale/runner.py`. Keep it.
- **Restart without sudo** (this box has no passwordless sudo):
  `kill -TERM $(systemctl show tgbot --property=MainPID --value)` →
  `Restart=always` relaunches (~20 s).
- Archive-time delivery uses a minimal `_KnownPeer(.id)` so raw numeric ids
  never need re-resolution against the premium account.
- **`search_contacts` returns a `FoundContacts` object, NOT a list.** Iterating
  it directly raised `'FoundContacts' object is not iterable`, which surfaced to
  the operator as the generic "❌ Something went wrong" (the text-state
  try/except in `register.py` swallows the real error). Use `found.users`.
- **IG session rotation mid-run** surfaces as "Exceeded 30 redirects" on
  `user_stories`/`user_medias_v1` (a login-wall redirect loop, not a checkpoint).
  `archive_instagram_stories`/`_posts` now invalidate the cached client and
  retry once via `_ig_client_retry()` — the old code reported "IG stories
  skipped" and dropped live stories.
- **The FM IG client MUST install the anti-detect transport.** A plain
  `IGClient()` rides Python `requests` TLS (a JA3 "this is a script"
  fingerprint) that Instagram answers with the same redirect loop. `_build()`
  now applies `ig_anti_detect.install_transport` (curl_cffi chrome136) +
  `pin_geo` + `install_token_echo`, mirroring the direct_forward worker.
- **instagrapi discards session rotation → write it back.** After every
  successful `login_by_sessionid`, `ig_anti_detect.write_back_session()` overlays
  the live `sessionid`/`csrftoken`/`ds_user_id`/`mid`/`rur` back into
  `igcookies.txt` via `cookie_manager.overlay_cookies()` (atomic, additive,
  preserves 0o444). Without this the jar's sessionid went stale within hours
  even though the file mtime looked fresh — the cookie-refresher's `mtime<20h`
  skip then never re-warmed it.

## Config knobs (.env)

`FRIEND_MEDIA_ENABLED` (0/1), `FRIEND_MEDIA_SCHEDULE_MINUTES` (default **60**;
0 = manual-only), `FRIEND_MEDIA_DESTINATION` (logchannel|saved|<chat_id>),
`FRIEND_MEDIA_MAX_PHOTOS` (backfill cap), `FRIEND_MEDIA_MAX_POSTS_PER_RUN` (10),
plus IG-global toggles written by the console. State file
`cache/friend_media_state.json` is exempt from the hourly cache cleaner.

---

## 13. Source: `docs/memory/tgbot-copy-message-vs-forward.md`

# copy_message vs forward_messages — the "Forwarded from" header (2026-08-26)

## The discovery (a gap I should have known)

While removing the `"Forwarded from tg_log"` header from Friend Media Archiver
deliveries, I initially proposed the wrong mechanism: have the BOT
**re-download the media bytes** from the log channel and re-`send_*` them to the
operator's DM. That was wrong, wasteful (a full re-download + re-upload), and
unnecessary.

The operator corrected me: the Bot API already solves this — **`copy_message`**
re-uses the message's existing `file_id`, so:

1. **No "Forwarded from" header.** `copy_message` is NOT a forward. The message
   arrives as if the bot sent it itself — sender shows as the BOT.
2. **No re-download, no re-upload, no size limit.** It copies by `file_id`, so
   it is instant and works even for files larger than the bot's own 2 GB upload
   ceiling (a bot cannot *upload* >2 GB, but it can `copy_message` a >2 GB media
   that a Premium user account already staged).
3. **It can quote-reply** (`reply_to_message_id` / `ReplyParameters`) to an
   existing message in the bot's chat with the user — so the delivery can be
   threaded as a reply, not just dropped in.

This is the *exact* pattern already used for the >2 GB premium-upload path in
`utils/uploader_handler.py::_stage_and_relay`:

```python
# Premium userbot stages the file in the log channel, then the bot:
await bot_client.copy_message(
    reply_to_message_id,          # optional quote-reply target
    chat_id=chat_id,              # the user's chat
    from_chat_id=config.LOG_CHANNEL_ID,
    message_id=staged.id,
    caption=user_caption,
)
```

My mistake was treating delivery as a "send bytes" problem and reaching for
`sender.download_media()` + `sender.send_*()`, instead of recognizing the
staged-message + `copy_message` relay that the codebase already contained as its
canonical "clean sender header" transport. `forward_messages` (what the
Friend Media delivery originally used) is what *adds* the "Forwarded from …"
header; `copy_message` does not.

## The rules

- **Want the sender to show as the BOT and no "Forwarded from" header?**
  Stage media (log channel or any bot-readable chat), then
  `bot.copy_message(chat_id, from_chat_id, message_id)`. Sender = bot, no
  forward header, optional quote-reply, no size limit.
- **Want an explicit forward (with the "Forwarded from …" header)?** Use
  `forward_messages`. That header is the point of a forward.
- **Never re-download just to clear a header.** `copy_message` does it with zero
  data movement. Re-downloading is only justified when the destination must
  receive *fresh bytes* (rare).

## Where this applies in tgbot

- `utils/uploader_handler.py::_stage_and_relay` — the reference implementation
  (premium >2 GB uploads).
- `modules/friend_media/common.py::_deliver_via_logchannel` — now uses the same
  pattern: premium user account uploads to `LOG_CHANNEL_ID`, bot `copy_message`s
  to the creator DM. (Previously `forward_messages`, hence the header.)

---

## 14. Source: `docs/memory/tgbot-2026-08-16-admin-webapp-and-join-fix.md`

# Full admin console as a Telegram Mini App + channel force-join fix (2026-08-16)

## Part A — channel force-join bug: `is_channel_member` never matched (fixed)

### Symptom

With subscription mode ON and the free tier requiring a channel join, a user
who had **actually joined** the force-join channel still got "not verified"
from the "✅ I joined — verify" (`chkjoin:`) button and from the download gate.
The operator's test user `8022375512` was confirmed a real member of `@mclib`
(`ChannelParticipant`), and the bot was confirmed an admin
(`ChannelParticipantAdmin`) — yet access was denied.

### Root cause

`utils/subscription/access.py::is_channel_member` compared

```python
s = str(status).lower()
return s in ("member", "administrator", "creator", "owner", "restricted")
```

pyrogram 2.x returns a `ChatMemberStatus` **enum**, whose `str()` is
`"ChatMemberStatus.MEMBER"` — `.lower()` is `"chatmemberstatus.member"` and
never equals `"member"`. So every `get_chat_member` call "succeeded" but the
membership test always returned `False`. Verified live:

```python
str(ChatMemberStatus.MEMBER)   # 'ChatMemberStatus.MEMBER'
ChatMemberStatus.MEMBER.value  # 'member'
```

### Fix

Compare against the enum's `.value` (`"member"`, `"administrator"`, ...) with a
`str()` fallback, and also accept `"restricted"` (slow-mode-limited users are
still members):

```python
s = (getattr(status, "value", None) or str(status)) if status else ""
return s.lower() in ("member", "administrator", "creator", "owner", "restricted")
```

Verified end-to-end with a live pyrogram client against the real channel:
`is_channel_member(client, 8022375512, -1001443485983) == True`.

## Part B — Full admin console as a Telegram Mini App (`/admin`)

The entire in-chat admin console (`modules/admin/*`, inline-keyboard driven)
now has a full SPA mirror served by the same FastAPI process.

### What was built

- **`utils/webapp_auth.py`** — shared Mini App auth (extracted out of the
  subscription webapp): `admin_token()` = `HMAC(BOT_TOKEN,"admin-sub")[:16]`
  (never stored), `verify_init_data()` per Telegram's official spec,
  `is_admin_auth()`/`require_admin()` (403 on failure). Both webapps now import
  from this one module.
- **`modules/admin_webapp/actions.py`** — transport-free server-side core.
  Deliberately reuses the same storage/utility code the in-chat console uses
  (`utils.gate`, `utils.subscription.store`, `modules.admin.cookies` /
  `_write_cookie_jar`, `modules.admin.cookie_test::_run_cookie_test_sync`,
  `utils.premium_session`, `modules.direct_forward.*`, `utils.shared.queue`)
  so the two UIs can never drift. Long probes (cookie test / PO diagnose / X
  test) run through `run_in_executor`.
- **`modules/admin_webapp/api.py`** — 46 FastAPI routes under `/admin/api`,
  every one gated by `require_admin`. Notably: `/premium/gen` uses its OWN
  `WEB_PREMIUM_GEN` dict (keyed `str(uid)`, own TTL) — never the in-chat
  `modules/admin/state.py::PREMIUM_GEN` — so the two generation flows can't
  clobber each other.
- **`modules/admin_webapp/ui.py`** — one-page SPA (dark, `safeAreaInset`
  aware, Telegram theme-aware, native `showPopup` fallback modal/toast). Tabs:
  Overview, Users, Cookies (download/replace/test/backup/restore + per-site
  jars), PO Token (start/stop/diagnose/test), Premium (whitelist + full
  session-generation flow phone→code→2FA→save with in-page inputs),
  Subscriptions (toggle/free/channels/grant/revoke/list), Direct
  (IG/X/TikTok toggles, IG pairing code, X Chat PIN, tests), System (abort
  queue, restart). Download endpoints are fetched as blobs (auth header can't
  ride `window.location`).
- **`modules/admin_webapp/__init__.py::mount(fastapi_app)`** — includes the
  router, serves the SPA at `/admin`, plus a public `/admin/api/health`.
- **`main.py`** — mounts `modules.admin_webapp` right after the subscription
  webapp (both in the same try/except so a webapp fault never kills the bot).
- **`modules/admin/keyboards.py`** — the console keyboard now carries a
  **🌐 WebApp Console** button (`web_app=WebAppInfo(url=...)`) opening
  `https://tgbot.southpark.ir:8080/admin`.
- **`modules/subscription/webapp.py`** — the `/` landing now redirects the
  creator to `/admin` (was `/admin/subscription`); the "🛠 Admin" link in the
  subscription UI now points at `/admin`. The legacy `/admin/subscription`
  routes still work.

### Auth model

- Telegram Mini App open → `tg.initData` header; `verify_init_data` only
  admits `user.id == SYSTEM_CREATOR_ID`.
- Browser / outside Telegram → `X-Admin-Token` header; token printed by the
  admin `/admin_token` command (already shipped earlier), stored in
  `localStorage`.
- Everything else → 403.

### Verification (all live)

- `python3 -m py_compile` on all touched files; JS extracted from `ui.py`
  passes `node --check`.
- FastAPI `TestClient`: health 200, no-auth/bad-token `/admin/api/state` 403,
  valid token + crafted initData 200.
- Live HTTPS: `/admin` serves the SPA (200), `/admin/api/health` 200,
  `/admin/api/cookies/ytcookies/download` 200 (real jar bytes), unauth
  `/admin/api/state` 403.
- Telethon drive: console keyboard shows the 🌐 WebApp Console button; `chkjoin`
  logic verified against the real channel (above).
- Bot restarted cleanly via systemd (NRestarts=0), `[POT] Provider is healthy`
  in log.

### Gotchas worth remembering

- `python-multipart` had to be installed in the venv for the `UploadFile`/
  `Form` cookie-upload endpoints — the subscription webapp never used
  multipart, so it wasn't a dependency before.
- `pot_state()` originally called two undefined helpers (`pot_running()` /
  `pot_available()`) — fixed to `_pot_running()` from `pot_menu` +
  `shared.POT_AVAILABLE`. Run a `TestClient` smoke pass before deploying any
  new `/admin/api` route.
- `from .cookies import _write_cookie_jar` inside `actions.py` was a wrong
  relative import (no `modules/admin_webapp/cookies.py` exists) — it must be
  `from modules.admin.cookies import _write_cookie_jar`.
- `config.DOMAIN` is the source of truth for the webapp base URL, with an
  HTTPS fallback so the button always opens a Telegram-valid HTTPS webapp.

---

## 15. Source: `docs/memory/tgbot-2026-08-21-tiktok-pin-and-webapp.md`

# TikTok chrome pin, IG decode shim, admin webapp theme (2026-08-21)

## TikTok: `curl_cffi>=0.14` breaks extraction (yt-dlp#17403)

**Symptom:** TikTok downloads fail with `"Unexpected response from webpage request"`
or render the `Site Maintenance` interstitial instead of video JSON. Repro is
stochastic (TLS fingerprint block).

**Root cause:** `utils/updater.py` ran `pip install -U --pre "yt-dlp[default,curl-cffi]"`
with no pin. yt-dlp's TikTok extractor hardcodes `impersonate=True`, which
resolves to curl_cffi's *newest* chrome target. `curl_cffi>=0.14` ships
chrome142+ fingerprints; TikTok blocks them. `0.13.x` newest is chrome131
(136/133 are yt-dlp-deprioritized) and TikTok accepts it. A fresh nightly
refresh would silently bump `curl_cffi` and re-break TikTok.

Additionally `utils/downloader/url_normalize.py::_apply_pot_options` was
injecting a custom `Chrome/140` UA via `http_headers` for
`tiktok.com/embed/` URLs. That UA *mismatched* the pinned `chrome131`
TLS fingerprint and also produced a block.

**Fix:**
- `requirements.txt`: `curl_cffi<0.14` + `curl-adapter==1.1.0` (1.2.x needs
  `curl_cffi>=0.14`, breaking the pin).
- `utils/updater.py`: `pip install ... "yt-dlp[default,curl-cffi]" "curl_cffi<0.14"`.
- `utils/downloader/url_normalize.py`: drop the custom UA override; let
  curl_cffi's impersonation supply its own matching UA.
- `AGENTS.md#5` updated as HARD constraint with research references.

**If TikTok hardens chrome131:** re-test newer chrome targets before bumping.

## IG anti-detect: `CurlStreamResponse._decode` crash on urllib3>=2.3

`utils/ig_anti_detect.py` installs `CurlCffiAdapter` for the private IG
session. On `urllib3>=2.3`, `HTTPResponse._decode` is called with
`max_length` kwarg; `curl_adapter 1.1.0`'s `CurlStreamResponse._decode`
signature is `(data, decode_content, flush_decoder)` and dies with
`TypeError: got an unexpected keyword argument 'max_length'` — killing the
IG direct-forward poller.

Fixed with `_patch_decode_signature()` that wraps `CurlStreamResponse._decode`
to swallow `max_length`. No-op on curl-adapter 1.2.x (already compatible).
Also added `normalize_browser_type` shim for older curl_cffi.

## Admin WebApp: themes + no-store + clear-cache

`modules/admin_webapp/ui.py` now has a persisted theme selector
(`localStorage admin_theme`): `system` (Telegram `themeParams` → CSS vars via
`Theme.applyTelegram()`), `light` (day palette), `dark` (AMOLED black).
`modules/admin_webapp/__init__.py` sends `Cache-Control: no-store` on the SPA
and every `/admin/api/*` response and provides a `Clear Cache` button
(`caches.delete` + reload) so operators never see a stale Mini App.

Also fixed `document.body.innerHTML +=` reserialization (duplicate script) →
`appendChild`.

## Migration note: `kurigram` (commit 31f4dfd)

`requirements.txt` now depends on `kurigram` (active pyrogram fork). Imports
stay `import pyrogram` — kurigram keeps the `pyrogram` module name. See
`docs/kurigram-open-issues.md` (15 open issues, risk assessment) and
`docs/architecture-ptb-vs-pyrogram.md` (why not PTB+Telethon).

## Verification

- `python -m py_compile $(git ls-files '*.py')` — pass.
- `bash -n install.sh run.sh` — pass.
- Bot `tgbot.service` active (running); IG/X polling healthy in `logs/bot.log`.

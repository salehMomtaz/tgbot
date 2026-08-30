# Balebot — integration, merge & hardening

All Bale.ai frontend notes merged: integration plan, ported extras (GitHub/YouTube/Translate/Web), and hardening (20 MB limit, getUpdates drain, limited admin).

## Sources consolidated

- `docs/memory/tgbot-balebot-integration.md`
- `docs/memory/tgbot-balebot-merge-2026-08-13.md`
- `docs/memory/tgbot-balebot-hardening-2026-08-13.md`

---

---

## 1. Source: `docs/memory/tgbot-balebot-integration.md`

# tgbot ↔ balebot integration (optional Bale frontend)

**Status:** design proposal · **Updated:** 2026-08-06
**Covers:** the real relationship between the two bots, every platform/API
difference, the live balebot inventory on the test VPS, and a complete plan for
running a Bale frontend *inside* tgbot as an **optional, additive feature** that
never degrades the Telegram bot.

---

## 1. The relationship (corrected)

**tgbot is the primary bot and the reference implementation. balebot was an
experiment** to try the Bale.ai messenger (an Iranian Telegram clone). The two
repos share a nearly identical download core — `extract_formats`,
`download_media`, the splitters, `get_cookies_for_url`, `_apply_pot_options`,
`probe_video_dimensions`, `estimate_format_size`, the Deno PO-token provider,
the yt-dlp auto-updater — because balebot was **derived from tgbot's design**,
not the other way around.

| Aspect | tgbot (this repo) | balebot |
|---|---|---|
| Role | **Primary bot / source of truth** | Experiment; test-Bale only |
| Messenger | Telegram | Bale.ai |
| Framework / transport | pyrogram (MTProto) | aiogram v3 → HTTP `tapi.bale.ai` |
| Upload ceiling | 2 GB bot / 4 GB premium userbot | 50 MB hard cap per message |
| Runs on | production box `38.45.80.233` | test VPS `66.23.198.52:1605` |
| PO-token provider port | 4417 | 4416 |
| Bot handle | `@AngelaBalzac_bot` (id `7665239058`) | `@angelabalzacbot` (id `1166452835`) |

The direction of any future port is **balebot's extra modules → tgbot**, or a
shared-core refactor inside tgbot. Never treat balebot as a reference that
tgbot must follow.

---

## 2. Live balebot inventory (test VPS, verified 2026-08-06)

```
ssh dev@66.23.198.52 -p 1605        # password provided by operator (sudo = same)
~ /home/dev/balebot                 # deployed, git master + a few unpushed commits
systemd: balebot.service (enabled, running, MemoryMax=2.0G, MainPID 559)
PO provider: 127.0.0.1:4416 (deno, bgutil v1.3.1)
logs: ~/balebot/logs/bot.log        # mirrored to the Bale log channel
```

`.env` keys (values redacted here — never commit them): `BALE_TOKEN`,
`SYSTEM_CREATOR_ID` (10-digit Bale user id), `LOG_CHANNEL_ID` (10-digit),
`GITHUB_TOKEN` (40-char PAT), `YTDLP_POT_ENABLED`, `YTDLP_POT_PORT`,
`YTDLP_USER_AGENT`.

`database.json`: `authorized`, `blacklisted`, `document_mode` — all empty.
Cookie jars at repo root: `cookies.txt`, `igcookies.txt`, `ttcookies.txt`,
`xcookies.txt`, `ytcookies.txt` (+ `.backup`).

**Observed quirk the operator reported:** the balebot PO provider periodically
"goes away" (visible in its log channel). The log shows the provider
crash-loop/restart pattern around startup (two `[POT] Starting server` +
`[POT] Provider is healthy` pairs inside 40 s on 2026-08-06). Root causes to
eliminate in the merge:
- **Two providers, two ports, two log loops** — each bot owns a Deno server
  (`4416`/`4417`) and its own health/restart loop. One can die without the
  other noticing. A single shared provider + one health loop fixes this.
- **`[POT stdout] Started POT server … on address [::]:4416`** — the upstream
  Deno server prints `[::]` even though the localhost patch is applied; if the
  patch markers drift on a provider-ref bump, the endpoint silently re-binds
  to all interfaces (security + "PO gone" class of symptoms). See §7.

---

## 3. Bale platform constraints (hard facts from `apiDocuments/baleAPI.md`)

Bale's Bot API is a **Telegram Bot API clone at roughly v4–6 feature level,
minus a lot**, over HTTP at `https://tapi.bale.ai/bot<token>/METHOD`.

### 3.1 Upload & file rules

| Rule | Telegram | Bale | Impact on a merge |
|---|---|---|---|
| Multipart upload | 2 GB (bot) / 4 GB (premium user) | **50 MB** per file | Bale needs aggressive splitting (balebot splits at 39 MB) |
| Upload by URL | 2 GB (media URL) | **20 MB** (non-image), **5 MB** (image) | Bale cannot pull big remote files server-side |
| Upload image | — | **10 MB** | — |
| Send by `file_id` | No limit, any bot | **No limit** | Bale also has `copyMessage`; staging/relay pattern is platform-local |
| `file_id` portability | per-bot | **per-bot, per-platform** | A TG `file_id` can never be sent to Bale or vice-versa — no cross-platform relay |
| `sendVideo` | any container | **MPEG4 only**; others must be sent as Document | Bale must demux/remux or doc-fallback |
| `sendAudio` | any | **`.MP3`/`.M4A` only** | Bale needs transcode or doc-fallback |
| Premium accounts / 4 GB | yes (userbot) | **no concept** | The premium + log-channel-staging path is Telegram-only |

### 3.2 Messaging, formatting, updates

| Feature | Telegram | Bale |
|---|---|---|
| Text parse mode | HTML / Markdown, explicit `parse_mode` | **Always auto-parsed as Markdown**, no `parse_mode`, **no HTML** |
| Markdown syntax | standard | Bold needs a **space before/after `*`**; stray `*`/`_`/backtick can reject the send (balebot strips them in `clean_caption_text`) |
| Message length | 4096 | 4096 |
| Media caption | ≤1024 (bot API) | 4096 |
| Long polling backlog | any | **2000 messages / 24 h** |
| `deleteWebhook(drop_pending_updates)` | clears polling queue | **does NOT clear it** → balebot drains manually at boot (§ of main.py) |
| `deleteMessage` | recent + own-message rules | **only messages < 48 h old**, plus role-dependent limits |
| Rich messages / streaming drafts (`sendRichMessage*`, Bot API 10.x) | yes | **absent** |
| `editMessageText/Caption/ReplyMarkup`, `answerCallbackQuery`, inline keyboards, `sendMediaGroup`, `forwardMessage`, `copyMessage`, `sendChatAction`, `getFile` | yes | present |
| `sendSticker`, `sendDice`, polls/quizzes, `setMyCommands`, `editMessageMedia`, `getChatMemberCount` | yes | **absent** |

**Consequence:** every user-facing string and every progress/status update must
be formatted per platform — rich HTML on Telegram, sanitized-Markdown (or
plain) on Bale. The **interaction model is the same** (inline keyboards,
callbacks, `edit_text`, quote-replies) so the UX patterns *do* translate; only
the rendering and delivery details differ.

---

## 4. Feature inventory — tgbot vs balebot

### 4.1 Features **only in tgbot**

| Feature | Where | Notes |
|---|---|---|
| Playlist tiers + per-video / whole-playlist download | `utils/downloader.py` (`PLAYLIST_TIERS`, `is_playlist_url`, `extract_playlist_meta`) | balebot is `noplaylist=True` everywhere — **no playlist support at all** |
| Premium 4 GB uploads (userbot whitelist) + log-channel staging relay | `utils/uploader_handler.py::_stage_and_relay`, `modules/admin.py` | Telegram-only, no Bale analog |
| >2 GB button lock + premium gate on `dl:` dispatch | `modules/downloader_handler.py` | — |
| Direct-forward DM relay (IG/X → Telegram) | `modules/direct_forward.py` | balebot has no relay |
| Stream handler / interceptor | `modules/stream_handler.py`, `stream_interceptor.py` | Bale has no streaming |
| Rich messages + streaming "analyzing" drafts | `utils/rich_stream.py` | Telegram Bot API 10.x only |
| Cookie **snapshot + merge write-back** | `utils/cookie_manager.py` | balebot only does snapshot copies + a `.backup`; no session-rotation merge |
| Per-site cookie jars under `cookies/ytdlp/` | `config.COOKIE_JARS` | — |
| Exact CDN size probes (`_apply_cdn_size_probes`) | `utils/downloader.py` | tgbot improvement |
| Keyboard auto-expiration | `utils/keyboard_expiry.py` | — |
| In-chat premium session generation + dial pad | `modules/admin.py`, `utils/premium_session.py` | — |
| Self-restart (SIGTERM→systemd) + **🔄 Restart Bot** | `main.py::schedule_self_restart` | — |
| Go system monitor (survives the bot) | `cmd/tgbot-monitor/` | Telegram log channel; independent |
| yt-dlp installed as `[default,curl-cffi]` | `utils/updater.py` | balebot uses plain `[default]` → TikTok PoW solver weaker there |

### 4.2 Features **only in balebot** (all portable to Telegram too)

| Feature | Command / trigger | Where | Would Telegram want it? |
|---|---|---|---|
| GitHub explorer | `/search <q>`, `/user <name>`, `/trend`, any `github.com/…` link → repo control panel (`gh:` callbacks: branches/tags/releases, issues/PRs, gists, ZIP download) | `modules/github/` (1101 lines) | likely — reusable as-is (pure GitHub HTTP API) |
| Google Translate | `/tr src:dst text` | `modules/translate/` | likely |
| YouTube search | `/yt <query> [n]` | `modules/youtube/` | likely |
| Channel recent uploads | `/ytrecent @chan [n]` | `modules/youtube/` | likely |
| Search inside channel | `/ytch @chan <q>` | `modules/youtube/` | likely |
| Transcript extraction → `.txt` | `/transcript <yt_url>` | `modules/youtube/` | likely |
| Webpage → Markdown | `/web <url>` (urltomarkdown.com) | `modules/direct_dl/` | maybe |
| **Admin: Set Size Limits** (runtime `split_target_mb` / `bale_hard_limit_mb` / `binary_chunk_mb`) | Admin console `⚙️ Set Size Limits` | `modules/admin/router.py` | **No** for Telegram — tgbot's `RUNTIME_SETTINGS` deliberately excludes Bale's hard-limit knobs (AGENTS.md); only meaningful if a Bale frontend exists |
| Custom filename `url \| name` | both bots | — | shared already |

### 4.3 Shared (already near-identical)

`extract_formats`, `download_media`, `split_file_generator`,
`split_video_by_size_generator`, `get_cookies_for_url`, `_apply_pot_options`,
`probe_video_dimensions`, `estimate_format_size`, Deno PO-token provider
(identical except the localhost-patch marker and port), yt-dlp auto-updater,
`queue_manager`, `gate` (authorized/blacklisted/document_mode), disk guards,
format keyboard + `dl:<cache>:<v|a>:<format_id>` callback flow, `url | name`
custom filenames, cookie snapshot reads.

---

## 5. Detailed differences (per area)

### 5.1 Framework & transport

| | tgbot | balebot |
|---|---|---|
| Client | pyrogram `Client` (MTProto, needs `API_ID`/`API_HASH` + session) | aiogram `Bot(token, session=AiohttpSession(api=TelegramAPIServer.from_base("https://tapi.bale.ai")))` — HTTP only, token only |
| Handler registration | `app.add_handler(..., group=...)` ordered groups (`-2` logs, `-1` security, `0` state, `1` router, `2` callbacks) | aiogram `Router` + middleware (`SecurityGateMiddleware` on message+callback) |
| Flow control | `message.stop_propagation()` / `ContinuePropagation` | aiogram filters / `Router` isolation |
| Message/callback objects | pyrogram `Message`, `CallbackQuery` | aiogram `Message`, `CallbackQuery` — **different classes, different attribute names** |
| Send/edit helpers | `client.send_*`, `callback_query.message.edit_text`, `message.reply_text` | `bot.send_*`, `message.edit_text`, `message.reply(...)` |
| Monkeys | `get_peer_type_patched`, `patch_pyrogram_send_methods`, `keyboard_expiry` | none |
| Blocking work | `loop.run_in_executor(None, fn)` | `loop.run_in_executor(None, fn)` — same pattern |

### 5.2 IDs & state (the silent killer)

Both platforms use **bare integer ids** for users, chats, messages. A Telegram
user `123` and a Bale user `123` are different people. Every shared store is
currently keyed by bare ints:

| Shared store | Keyed by | Collision risk |
|---|---|---|
| `database.json` (`authorized`/`blacklisted`/`document_mode`/`premium_users`) | `user_id` int | Admin could whitelist/blacklist the wrong-platform user |
| `DOWNLOAD_CACHE` | `cache_id` (random uuid8) | low, but same-namespace |
| `USER_STATES` / `ACTIVE_PROMPTS` / `PREMIUM_GEN` | `user_id` int | cross-platform prompt hijack |
| `DownloadQueue` | `(user_id, status_msg)` | queue ownership ambiguity |
| `RUNTIME_SETTINGS` | global | Bale-only knobs would leak into Telegram behavior |
| `last_upload`/`meta.json` (cookies) | global | fine |

**Required:** namespace every id at the platform boundary — e.g. `tg:7429671248`
and `bl:1234567890` — as soon as a Bale frontend is added. Do this before, not
after, the merge.

### 5.3 Upload pipeline & delivery

| Step | Telegram path | Bale path |
|---|---|---|
| `send_video` / `send_audio` / `send_document` | pyrogram native (2 GB / 4 GB, any codec) | raw aiohttp `multipart/form-data` POST to `tapi.bale.ai` (`operators/uploader.py::upload_file_direct_to_bale`) — aiogram's own serialization is bypassed on Bale |
| Splitting | only if >2 GB ceiling | **always if >39 MB** (`split_video_by_size_generator` via ffmpeg `-c copy`; documents via `split_file_generator`) |
| Split artifact | parts uploaded then deleted | parts uploaded then deleted — same idea, different thresholds |
| Force-document rule | doc mode / `action=='d'` / oversized | doc mode / `action=='d'` / **oversized non-video/audio** |
| Quote-reply to link | `reply_to_message_id` threaded through upload | aiogram `message.reply(...)` quotes the user's message naturally |
| >2 GB relay | premium userbot → log channel → bot `copy_message` | n/a (impossible) |
| Filename safety | as-is | `sanitize_filename_for_bale` (≤40 chars, strips unsafe chars) |
| Caption safety | rich HTML | `clean_caption_text` (≤150 chars, strips Markdown-punctuation) |

### 5.4 Extraction / downloader differences

| Area | tgbot | balebot |
|---|---|---|
| Format fetch queueing | **fetches bypass the queue**, run concurrently via `_spawn_fetch`; only download+upload serialize | **everything goes through `queue.add_task`** — format selection waits behind active downloads |
| Playlists | tier keyboard, per-video selector, whole-playlist | **unsupported** (`noplaylist=True`) |
| Instagram strategy | no-auth-first ladder (session-conserving), 400 retry with cookies | same ladder (derived) |
| Size estimation | `estimate_format_size` + `_apply_cdn_size_probes` for IG | `estimate_format_size` only |
| yt-dlp extras | `[default,curl-cffi]`, `--pre` | `[default]`, `--pre` |
| TikTok shortlinks | pre-resolved with browser UA + TTL cache | not pre-resolved |

### 5.5 Admin console

| tgbot console | balebot console | Notes |
|---|---|---|
| 👥 List Users / ➕ Add / ➖ Remove | same | shared concept |
| 🚫 Blacklist Logs (**+ 🔓 Unban User**) | Blacklist Logs (no unban button in keyboard list) | tgbot has unban |
| 📄 Doc Mode toggle | same | shared |
| 🍪 Cookie Jars (replace/backup/restore/download/test) + ➕ Per-Site Jar | Cookie Jars (replace/save-backup/restore/download/test) | no per-site jars in balebot |
| 🛡 PO Token (Test Stack / Diagnosis / Start / Stop / Refresh) | PO Token (Provider Status / Test Stack / Diagnosis / Start / Stop) | shared |
| 👑 Premium Uploads (add/remove/gen/cleanup) | — | TG-only |
| 📨 Direct-Forward (pair/unpair IG) | — | TG-only |
| 🔄 Restart Bot | — | TG-only |
| 💥 Abort Transfer | Abort Transfer | shared |
| ❌ Close Console | Close Console | shared |
| — | **⚙️ Set Size Limits** (`split_target_mb`/`bale_hard_limit_mb`/`binary_chunk_mb`) | Bale-only knob; hide unless Bale frontend active |

### 5.6 Cookie lifecycle

| | tgbot | balebot |
|---|---|---|
| Snapshot per run | yes (`cookie_manager.acquire`) | yes (`get_cookies_for_url`) |
| Write-back | **overlay merge** on success (atomic, never deletes keys, refuses empty) | **none** — jar stays `0o444` + `.backup` only |
| Failure bookkeeping | auth errors recorded in `cookies/meta.json` for watchdog | no meta.json |
| Admin replace/restore | `_write_cookie_jar` unlock-by-replace + re-lock + `last_upload` stamp | simple copy |

The merge should use **tgbot's** `cookie_manager` for both platforms — it is a
strict superset and fixes the "jar dies in days" failure mode.

---

## 6. Integration design (the plan)

### 6.1 Non-negotiable principles (from the operator)

1. **One process, two frontends.** Do *not* run a separate balebot service.
   Merge into tgbot: the pyrogram client (Telegram) **and** an aiogram HTTP
   client pointed at `tapi.bale.ai` (Bale) inside the same event loop.
2. **Optional and inert by default.** If `BALE_TOKEN` is absent from `.env`,
   zero Bale code runs and the Telegram bot is **byte-for-byte** the current
   bot. The feature flag gates everything at startup.
3. **Isolation.** A Bale crash (API timeout, malformed update, provider hiccup)
   must never take down the Telegram poller. Separate `try/except` + separate
   task per platform.
4. **Share the expensive stuff.** One venv, one yt-dlp, one PO-token provider,
   one cookie store, one `DownloadQueue`, one updater, one `database.json`
   (namespaced). This is the whole point — it also kills the
   "two providers, one of them dies" problem.
5. **tgbot is the reference.** The shared core stays in tgbot; balebot is
   reduced to a thin Bale-specific transport + upload layer that tgbot loads
   when enabled.

### 6.2 Target architecture

```
┌──────────────────────────── tgbot process (single systemd unit) ───────────────────────────┐
│                                                                                            │
│  main_engine()                                                                             │
│   │                                                                                        │
│   ├─ [ALWAYS] pyrogram Client(app)   → Telegram poller (group -2..2 handlers)               │
│   ├─ [IF BALE_TOKEN] aiogram Bot     → Bale poller (Router set)                            │
│   │      session=AiohttpSession(api=TelegramAPIServer.from_base("https://tapi.bale.ai"))   │
│   │      task = dp.start_polling(bale_bot) in its own try/except                           │
│   │      startup: drain Bale getUpdates backlog (deleteWebhook quirk)                       │
│   │                                                                                        │
│   ├─ [ALWAYS] PotProviderManager     → ONE Deno provider (port configurable, default 4417) │
│   │                                    serves BOTH frontends; single health_check_loop     │
│   ├─ [ALWAYS] cookie_manager         → snapshots + merge write-back for all sites           │
│   ├─ [ALWAYS] DownloadQueue          → one worker; jobs carry platform tag                 │
│   ├─ [ALWAYS] updater (yt-dlp)       → one upgrade loop, [default,curl-cffi]               │
│   ├─ [ALWAYS] system monitor (Go)    → Telegram-only (unchanged)                           │
│   └─ [ALWAYS] auto-clean cache, keyboard expiry, logger (per-platform channel)             │
│                                                                                            │
│  shared core (platform-agnostic): utils/downloader.py, cookie_manager, queue_manager,      │
│    gate (namespaced), pot_provider, updater, splitters, size estimation                    │
│                                                                                            │
│  platform layer:                                                                           │
│   └─ messenger/  (new)                                                                     │
│        sender.py        → MessageSender interface: send_text/edit/delete/reply/upload     │
│                           + TgSender (pyrogram) + BaleSender (raw aiohttp multipart)       │
│        formatting.py    → per-platform caption/status formatter (HTML-rich vs sanitized-MD)│
│        ids.py           → platform namespace: tg:…, bl:…                                  │
│        limits.py        → per-platform upload/split policy (TG 2/4 GB vs Bale 50 MB)       │
└────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 6.3 What moves where (refactor map)

| Current | Destination | Notes |
|---|---|---|
| `utils/downloader.py`, `utils/cookie_manager.py`, `utils/pot_provider.py`, `utils/updater.py`, `utils/queue_manager.py`, splitters | **stay put** — already platform-agnostic | verify no `pyrogram` imports leak in |
| `modules/downloader_handler.py` | keep for TG; factor the download/upload invocation behind a tiny "deliver(file, action, …)" seam so a Bale router can call the same logic | the seam is `process_split_and_upload`-equivalent |
| balebot `operators/downloader.py` | **do not copy** — reuse tgbot's `utils/downloader.py` (it is a superset) | only adopt `is_playlist_url` absent features if desired |
| balebot `operators/uploader.py` | port as `messenger/bale_uploader.py` (raw multipart, 39 MB splits, filename/caption sanitizers) | Telegram uploader stays pyrogram-native |
| balebot `modules/{github,translate,youtube,direct_dl}/` | port as optional tgbot modules (TG commands + Bale commands) if wanted | pure HTTP; no Bale dependency |
| balebot `main.py` startup (drain backlog, SIGTERM, PO start) | fold into `main_engine()` gated on `BALE_TOKEN` | — |

### 6.4 Namespacing (do this first)

- `gate.py`: store `tg:<id>` / `bl:<id>` in `authorized`/`blacklisted`/
  `document_mode`/`premium_users`. Add a migration that prefixes existing bare
  ints with `tg:` once.
- `DOWNLOAD_CACHE`, `USER_STATES`, `ACTIVE_PROMPTS`, `PREMIUM_GEN`: key by
  `(platform, id)` tuple or `"tg:…"`/`"bl:…"` string.
- `DownloadQueue` job: carry `platform` so progress edits target the right
  frontend.
- `RUNTIME_SETTINGS`: keep Telegram-only knobs as today; gate any Bale knob
  (`bale_hard_limit_mb` etc.) behind the Bale feature flag and *never* let it
  influence Telegram sizing.

### 6.5 Bale frontend surface (parity with current balebot)

When `BALE_TOKEN` is set, a Bale user sees exactly the current balebot feature
set, delivered through the shared core:

- media download format keyboard (YT/IG/TikTok/X + direct URLs), `url | name`
  custom filenames, Cancel;
- `⚙️ Set Size Limits` admin knob (Bale-only) present only when Bale is active;
- the balebot-only extras if the operator opts in: `/yt`, `/ytrecent`, `/ytch`,
  `/transcript`, `/web`, `/tr`, GitHub explorer;
- Bale cookie jars handled by tgbot's `cookie_manager` (paths stay per-platform:
  `cookies/bale/<site>.txt` or a Bale-specific dir — do not mix with TG jars).

Not offered on Bale (impossible or TG-specific): playlists (optional — could be
ported if wanted), premium/4 GB, streaming, rich messages/drafts, direct-forward
relay (could relay into Bale later, out of scope), system monitor.

### 6.6 Failure isolation & liveness

- Each poller runs in its own `asyncio` task with a broad `try/except` +
  `logging.exception`; a Bale `BotError`/timeout never propagates to the TG
  poller.
- `PotProviderManager` stays a **single instance**; its health loop restarts it
  with backoff for both frontends. Move the provider binary + port out of
  per-bot paths into one shared location; make the port env-tunable and
  default it to 4417 (freeing 4416).
- Bale's `deleteWebhook(drop_pending_updates)` no-op quirk: drain the backlog
  manually once at startup, exactly like balebot does today.
- SIGTERM path: teardown both pollers (same graceful drain as today).

### 6.7 Deployment & env

- `.env` additions (all optional): `BALE_TOKEN`, `BALE_SYSTEM_CREATOR_ID`,
  `BALE_LOG_CHANNEL_ID`, `BALE_POT_PORT` (or share `YTDLP_POT_PORT`).
- Single systemd unit `tgbot.service`; no second unit. `install.sh` stays the
  same (aiogram becomes a requirement only when Bale is enabled — or always,
  it is small).
- Both log channels: TG `LOG_CHANNEL_ID` via `TelegramChannelHandler`, Bale
  `BALE_LOG_CHANNEL_ID` via a new `BaleChannelHandler` (same raw-HTTP pattern
  as `utils/logger.py`, different endpoint — port from balebot `utils/logger.py`).
- `run.sh`/`.env` dotenv parser already tolerates both token styles.

### 6.8 Rollout phases

1. **Phase 0 — prep:** namespace ids in `gate`/state stores; add a migration
   test; confirm TG bot behavior unchanged (regression via `tools/telethon_drive.py`).
2. **Phase 1 — skeleton:** `messenger/` layer + `BaleSender` + feature flag;
   Bale bot boots, replies `/start`, admin console opens. TG untouched.
3. **Phase 2 — download parity:** route Bale media downloads through the shared
   core + Bale uploader (splits at 39 MB). Verify with real YT/IG/TikTok links
   on the test VPS.
4. **Phase 3 — extras:** port balebot-only modules (github/translate/yt/web/
   transcript) as shared modules available on both platforms.
5. **Phase 4 — hardening:** single PO provider with one health loop; liveness
   tests (kill provider → auto-restart; kill Bale poller → TG keeps polling);
   document invariants in AGENTS.md + this file.

### 6.9 Testing plan

| Test | Method |
|---|---|
| TG regression (no Bale env) | `tools/telethon_drive.py` suite: single video/audio/playlist/direct/cancel/admin console (already proven) |
| Bale regression | manual on test VPS + aiogram: `/start`, format keyboard, download ≤39 MB, split >39 MB |
| ID-namespace | add a `tg:` user + a `bl:` user with the same numeric id; confirm isolation |
| PO single-instance | stop provider; confirm both frontends report it, health loop restarts it once |
| Crash isolation | kill Bale poller task; confirm TG answers within a few seconds |
| Cookie merge on Bale | run a Bale IG download twice; confirm `cookies/bale/instagram/*.txt` updates, never empties |

---

## 7. The shared PO-token provider (why one instance, not two)

Both bots already run the **same** Deno provider (`bgutil-ytdlp-pot-provider`
v1.3.1). The files differ only in the localhost-patch marker string and the
port. In the merged bot there is exactly **one** provider:

- started once in `main_engine()` (regardless of which frontends are active);
- port env-tunable (`YTDLP_POT_PORT`, default 4417); the test VPS's 4416 is
  freed when balebot.service is retired;
- one `health_check_loop` with backoff restart → **no more "PO gone in the log
  channel"** because the second bot's independent (and unmonitored) provider
  cannot exist anymore;
- the localhost bind is mandatory (invariant #2). On any provider-ref bump,
  verify the `// *_LOCALHOST_PATCH` marker still matches, or the manager's loud
  warning must be treated as a release-blocker.

---

## 8. Risks & mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Bale poller crash takes down TG | medium | separate task + broad try/except; kill-test in Phase 4 |
| ID collision across platforms | **high if not namespaced** | namespace first (Phase 0), migration test |
| aiogram vs pyrogram object leak into shared core | medium | code-review gate: shared modules import no framework types; seam only at `messenger/` |
| Bale 50 MB split silently corrupts files | low | reuse tgbot's `split_video_by_size_generator` (already battle-tested) with Bale thresholds |
| Two log channels / rich-HTML leaks to Bale | medium | per-platform formatter in `messenger/formatting.py`; never send `<table>` to Bale |
| Two providers drift (today's bug) | — | eliminated by single-instance design (§7) |
| `BALE_TOKEN` in repo | — | `.env` git-ignored; never commit; `git check-ignore` after setup |
| Bale API changes break parity | medium | keep Bale transport thin; feature-flag off rather than block TG |

---

## 9. Open decisions (operator to confirm)

1. **Bale-only extras on Telegram:** bring GitHub explorer / translate / yt-search /
   transcript / `/web` into tgbot for TG users too? (Recommend: yes — they are
   pure HTTP, low risk, and tgbot gains real features.)
2. **Playlists on Bale:** port the tier-keyboard playlist flow to Bale (needs
   only the shared core, works fine) or keep Bale at single-video parity with
   the current balebot? (Recommend: yes, it is nearly free once the seam exists.)
3. **Retire balebot repo:** after the merge, balebot.service is stopped and the
   repo kept only as a feature reference; tgbot becomes the single source.
4. **Bale queue policy:** share one `DownloadQueue` across platforms (a Bale
   download serializes behind a TG one) or give Bale its own worker? (Recommend:
   share — the whole point is one of everything; a shared worker is also safer
   for the 1 vCPU box.)
5. **Provider port:** free 4416 (stop balebot.service) and keep a single 4417.

---

## 2. Source: `docs/memory/tgbot-balebot-merge-2026-08-13.md`

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

---

## 3. Source: `docs/memory/tgbot-balebot-hardening-2026-08-13.md`

# Bale.ai frontend hardening — 2026-08-13

## Context (why harden)

Bale.ai is owned by Iranian government infrastructure. Operator reports `tapi.bale.ai` traffic is untrusted and could be used to probe the VPS. The Telegram bot (`pyrogram` on `t.me`) is the primary, trusted frontend. Bale is an **optional, LIMITED mirror** that shares expensive core (PO provider, queue, yt-dlp, cookies) but must not leak secrets or allow VPS infiltration.

User requested:
- **No Bale log channel** (Telegram `LOG_CHANNEL_ID` stays private; Bale must not ship logs to a Bale channel)
- **Bale admin console extremely limited** — cookies, premium session, POT provider, direct-forward, subscriptions must NOT be exposed on Bale. File partitioning limits can stay.
- **Bale file limit reality is 20 MB** (docs claims 50 MB, but `General` API caps document/Video at 20 MB; Bale's own `image` 10 MB, `video` MPEG4-only; our measured upload fails at ~20 MB). Telegram keeps 2 GB/4 GB.

## Design adopted (one process, two frontends)

`docs/memory/tgbot-balebot-integration.md` proposed **one `tgbot.service` process hosting both pollers**:
- `main_engine()` starts Telegram `pyrogram.Client` *always*, and if `BALE_TOKEN` is set, spawns `modules/bale/runner.py::start_bale_bot()` as an isolated `asyncio` task (broad `try/except`, Bale crash never kills Telegram).
- Both frontends share: `DownloadQueue` (one worker), `POT_PROVIDER` (one Deno on 127.0.0.1), `cookie_manager` snapshots + write-back, `supported_sites.is_ytdlp_supported`, SSRF guard, flood guard. No second PO port.
- Bale is **inert by default**: `BALE_TOKEN` empty → zero Bale code runs, Telegram behavior byte-for-byte identical. Add `BALE_TOKEN` + `BALE_SYSTEM_CREATOR_ID` to activate.

## What was built

### Config (`config.py:240`)
```py
BALE_TOKEN = os.getenv("BALE_TOKEN","")
BALE_SYSTEM_CREATOR_ID = get_env_int("BALE_SYSTEM_CREATOR_ID",0)
BALE_HARD_LIMIT_MB = get_env_int("BALE_HARD_LIMIT_MB",20)  # real, not 50
BALE_SPLIT_TARGET_MB = get_env_int("BALE_SPLIT_TARGET_MB",19)
BALE_DIRECT_DOWNLOAD = os.getenv("BALE_DIRECT_DOWNLOAD","true") ...
```
`.env.example` documents each, notes government ownership, no Bale log channel, 20 MB truth.

`requirements.txt` adds `aiogram==3.12.0` (Bale transport). Telegram still uses `pyrogram`.

### Bale transport (`modules/bale/runner.py`)
- Aiogram `Bot(token, session=AiohttpSession(api=TelegramAPIServer.from_base("https://tapi.bale.ai")))` — HTTP only, token only, no MTProto.
- Startup: drain Bale `getUpdates` backlog manually (`deleteWebhook` is NOOP on Bale per balebot's `main.py` comment — we loop `get_updates(offset=last+1)` until empty). Prevents replay spam on every restart.
- `Bale API limits respected`: captions sanitized via `clean_caption_text` (strip `* _ ` [ ] ( )`, 150 chars), filenames sanitized to ≤40 chars (`sanitize_filename_for_bale`), MPEG4 only for `sendVideo` (others fallback to `sendDocument`), markdown auto-parse quirks handled.

### Bale uploader (`modules/bale/uploader.py`)
Ported from `balebot/operators/uploader.py` but pointed at `config.BALE_TOKEN` and 20 MB ceiling:
- `upload_file_direct_to_bale()` → raw `aiohttp.FormData` POST to `https://tapi.bale.ai/bot<token>/sendDocument|sendVideo|sendAudio` with proxy support.
- `process_split_and_upload_bale()` → `split_video_by_size_generator(file, 19 MB, 20 MB)` for media, `split_file_generator` for documents, sequential one-part-at-a-time (VPS disk cap = one chunk), deletes part immediately.
- Telegram's `process_split_and_upload` (1900/3900 MB target) is **not reused** for Bale — Bale's 20 MB path is separate.

### Bale admin console (`modules/bale/admin.py` + `runner.py` handlers)
**Limited set only** (see `get_bale_console_keyboard`):
- `👥 List / ➕ Add / ➖ Remove / 🚫 Blacklist / 🔓 Unban` (user management)
- `📄 Doc Mode` toggle
- `⚙️ Size Limits: 19/20MB` (`waiting_for_setlimit` → `bale_hard_limit_mb`, `bale_split_target_mb`, `binary_chunk_mb`, `max_cache_age_hours`)
- `💥 Abort Transfer`
- `❌ Close`

**Explicitly NOT exposed on Bale** (even if Telegram admin crafts `callback_data`):
```
bale_admin_cookie* , bale_admin_pot* , bale_admin_premium* , bale_admin_direct* , bale_admin_sub*
→ answered "❌ Not available on Bale (secrets hidden). Use Telegram admin console."
```
Also: no `Cookie Jars`, no `PO Token`, no `Premium Uploads`, no `Direct-Forward`, no `Subscriptions`. File split knobs are the *only* tuning exposed on Bale, per operator request ("bale file limit and other things you think are necessary there can stay").

State stores are isolated: `BALE_USER_STATES` / `BALE_ACTIVE_PROMPTS` separate from Telegram's `USER_STATES`, per integration doc namespacing guidance (avoid `bl:123` colliding with `tg:123` in shared stores).

### Security posture (Bale side)

| Concern | Mitigation in Bale runner |
|---|---|
| **Log exfiltration** | **NO `BaleChannelHandler`** — `setup_system_logger` only attaches `TelegramChannelHandler` + local `logs/bot.log`. Bale never ships logs to a Bale channel (government could read). Python `logging` for Bale goes to same Telegram log channel + local file, not to Bale. |
| **Admin impersonation** | `_is_bale_admin(uid) = uid == BALE_SYSTEM_CREATOR_ID` (separate var from `SYSTEM_CREATOR_ID` = Telegram creator 7429671248). Bale token's admin is 1058935006 per user-provided `BALE_SYSTEM_CREATOR_ID`. All `bale_admin_*` callbacks check this first (`Access Denied` otherwise). |
| **Secret exposure** | Limited keyboard + explicit block on secret callbacks (above). Even a crafted `admin_cookie_select:ytcookies` via Bale is rejected. Cookies live under `cookies/` on disk, never sent to Bale chat (Telegram admin's `📤 Download` is disabled on Bale). |
| **Request flood / probe** | Tight rate limit: `_rate_ok(uid, window=60, limit=4)` on Bale (Telegram free tier 5/min). Exceed → `⏳ Too fast`. No auto-blacklist storm on Bale (government probing could deliberately trigger blacklist of legitimate users). |
| **SSRF** | Reused `_is_ssrf_target(url)` from Telegram's `downloader_handler`: refuses `loopback/private/link-local/multicast/reserved` — protects `127.0.0.1:4417` PO provider and internal services even if Bale user sends `http://127.0.0.1/...`. Same `is_safe_url` (http/https + ≤2048 chars) guard. |
| **Input injection** | All free-form Bale admin inputs (`waiting_for_setlimit`, `waiting_for_add_user` etc) are validated via `is_valid_telegram_id` or key allow-list (`bale_hard_limit_mb` etc) before any `setattr(config, ...)` or `shared.set_setting`. `setlimit` only allows known keys, positive ints. No arbitrary code. |
| **File limit lie** | Bale docs claim 50 MB, real is 20 MB (`upload_file_direct_to_bale` would 400+ beyond 20). Buttons show `🔒 (>20MB)` and format selector locks >20 MB. Split at 19/20. Don't widen to 50. |
| **Isolation** | Bale poller wrapped as `_bale_wrapper()` with `try/except logging.exception` — Bale crash is caught, logged to Telegram log channel, and does not propagate to `asyncio.gather(*tasks)` so Telegram poller + PO health loop + cache cleaner keep running. SIGTERM also drains both. |
| **Payload size** | Bale direct-download timeout 600s (vs Telegram 1800) to avoid long-held connections from untrusted side; still uses 512 KB chunks. |
| **No premium path** | Bale has no Premium concept — no `is_premium_user` check, no 4 GB staging via log channel. All Bale uploads are via direct multipart, never via Telegram log channel. |

### File map

| Want to... | Edit |
|---|---|
| Bale credentials / limits | `config.py` (`BALE_TOKEN`, `BALE_SYSTEM_CREATOR_ID`, `BALE_HARD_LIMIT_MB`) + `.env.example` |
| Bale upload / sanitizers | `modules/bale/uploader.py` (`sanitize_filename_for_bale`, `clean_caption_text`, `upload_file_direct_to_bale`) |
| Bale limited admin | `modules/bale/admin.py` (`get_bale_console_keyboard`, `BALE_USER_STATES`, `_is_bale_admin`) |
| Bale poller + download callbacks | `modules/bale/runner.py` (`create_bale_dispatcher`, `start_bale_bot`, `is_link`, `_rate_ok`, `is_ytdlp_supported`, `process_split_and_upload_bale`) |
| Wire Bale into boot | `main.py` → isolated `_bale_wrapper()` task gated on `BALE_TOKEN` |

### Verification

```bash
python3 -m py_compile $(git ls-files '*.py')  # clean
bash -n install.sh run.sh uninstall.sh
venv/bin/pip install -q -r requirements.txt  # pulls aiogram 3.12.0
BALE_TOKEN set → systemctl restart tgbot → journalctl: "[Bale] Polling started (tapi.bale.ai), admin LIMITED, no log channel"
BALE_TOKEN empty → "[Bale] BALE_TOKEN empty — Bale frontend disabled"
# Functional (on Bale @angelabalzacbot):
#  /start as Bale admin (1058935006) → LIMITED console (no 🍪, no 👑, no 🔐, no 📨)
#  /start as non-admin Bale user → welcome with 20 MB note
#  Paste https://www.youtube.com/watch?v=... → format keyboard (locked >20 MB) → tap → delivers via Bale split 19/20 (caption stripped of markdown)
#  Try crafted callback "bale_admin_cookie_select:ytcookies" → "Not available on Bale"
#  Paste http://127.0.0.1:4417/ping → "Refusing private network address" (SSRF guard)
```

### Open follow-ups (not blocking)

- Consider adding Bale-side `/tr` / `/yt` / `/search` extras behind same limited security gate if operator wants parity with Telegram extras on Bale (currently only download works on Bale; Telegram extras stay Telegram-only per request).
- If Bale ID space collision with Telegram becomes observable, namespace `authorized`/`blacklisted` with `tg:`/`bl:` prefix as per integration doc §6.4.

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

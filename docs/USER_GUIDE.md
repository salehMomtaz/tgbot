# User Guide — what tgbot can do and how to use it

This is the *how to use it* companion to `README.md` (overview) and `blueprint.md` (architecture). It assumes your bot is already running (`tgbot.service active`). For first-time install, do `docs/UBUNTU_VPS_SETUP.md` first, then come back here.

> **One frontend, one core:** Telegram (`pyrogram`, `t.me`/`api.telegram.org`, `2 GB` bot / `4 GB` Premium userbot). The optional Bale (`aiogram`, `tapi.bale.ai`) mirror that used to run inside the same `tgbot.service` process was **removed on 2026-10-18** — no `BALE_*` settings, no `bale_log` channel, no `aiogram` dependency (history: `docs/memory/BALEBOT.md`). Same core throughout: `DownloadQueue`, PO provider `127.0.0.1:4417`, `is_ytdlp_supported` 1,786 patterns, `cookie_manager` snapshots.

---

## Quick command map

| What you want | Command or action | Where it works | What happens |
|---|---|---|---|
| **Download a video/audio from any site** | Paste `https://...` link (YouTube, Instagram, TikTok, X, 1,700+ yt-dlp sites) | Telegram | Bot replies `Format Selection` (video left, audio right, `~` = estimate, `2 GB` locked unless Premium) -> tap a format -> queued -> `Downloading...` -> auto-split -> upload |
| **Download a whole YouTube playlist** | Paste `https://www.youtube.com/playlist?list=...` or `watch?v=...&list=...` | Telegram | `Tier keyboard` (`High 1080p / Medium 720p / Low 480p` + `High/Medium/Low` audio) -> `Downloading 1/50 ...` -> one queue slot, `50` cap (`PLAYLIST_MAX_VIDEOS`), per-video `⚠️ Skipped` is non-fatal |
| **Direct file (no yt-dlp)** | Paste `https://example.com/file.mp4 | Telegram | `aiohttp GET` 30 min, `512 KB` chunks, `Content-Length` progress, `SSRF` guard refuses `127.0.0.1/private` |
| **Rename the result** | `https://... | myname.mp4` (note ` | ` + extension) | Telegram | Saved as `myname.mp4` |
| **GitHub repo** | Paste `https://github.com/owner/repo` (or `.../issues/123`, `gist.github.com/...`) | Telegram | full panel (ZIP, branches/tags/releases, issues/PRs, commits, languages, README, file explorer 8/page + folder ZIP) |
| **GitHub search** | `/search <query>` | Telegram | `api.github.com/search/repositories` `stars desc`, 5 results |
| **GitHub user** | `/user <username>` | Telegram | 5 recent repos |
| **GitHub trend** | `/trend` | Telegram | Weekly trending (`created:>7d`) |
| **YouTube search** | `/yt <query>` or `/yt <limit> <query>` | Telegram | `yt-dlp ytsearch15` flat, `search_ytdlp_flat` |
| **YouTube recent** | `/ytrecent @channel [n]` | Telegram | `https://www.youtube.com/@channel/videos` flat |
| **YouTube channel search** | `/ytch @channel <query>` | Telegram | `ytsearch` with channel prefix |
| **YouTube transcript** | `/transcript <youtube_url>` | Telegram | `writeautomaticsub` `en/fa/auto`, `clean_vtt_subtitles`, `*_Transcript.txt` via queue |
| **Translate** | `/tr src:dst text` e.g. `/tr fa:en سلام` | Telegram | `translate.googleapis.com/translate_a/single?client=gtx` |
| **Webpage -> Markdown** | `/web <url>` e.g. `/web https://example.com` | Telegram | `urltomarkdown.herokuapp.com/?url=...&title=true` -> `>3500` as `.txt` via split |
| **Show subscription** | `/subscription` | Telegram | Tier keyboard (`free 5/d`, `basic 100/100⭐`, `plus 500/250⭐`, `pro 2500/500⭐`), Stars + TON (`XCHAT_PIN` etc) |
| **My quota** | `/quota` | Telegram | `5/100/500/2500` left today |
| **Stream a file** | Forward a Telegram file (video/doc/audio/voice) to bot | Telegram only | `24h` `https://DOMAIN/stream/<token>/<filename>` via `FastAPI` `stream_media` (no local buffering) |
| **Admin console** | `/start` or `console` (as creator) | Telegram | See below |

---

## Download flow in detail

### Single video/audio

1. You send `https://www.instagram.com/reel/DVjNXkOkVxC/` (or YouTube, TikTok, X, etc).
2. Bot `POST`s to `DownloadQueue` (single worker, priority `free 0` < `basic 1` < `plus 2` < `pro 3`).
3. `extract_formats` runs off the event loop (`run_in_executor`) -- `Instagram` tries `no-auth` first (conserves session), `YouTube` is `cookies + PO token` only (`mweb` + `127.0.0.1:4416`, no fallback), others `cookies -> no-auth` with `400` retry. Each video button size is `video_stream + best_audio` (merged `+bestaudio` height-capped chain, not the old `+bestaudio/best` collapse `5003d78`).
4. For IG DASH reels with no `filesize`/`tbr`/`duration`, `_apply_cdn_size_probes` `HEAD`/`Range-GET` gets real `Content-Length` (exact, drops `~`); YouTube/TikTok are never probed.
5. You tap `🎥 1080p (120M)` or `🎵 128k (~5M)`. `>2 GB` shows `🔒` + `lock` callback unless `premium_allowed`.
6. `download_media` merges `fmt+bestaudio` to `mp4` (video) or `m4a` (audio, no re-encode bloat), `ffmpeg` square-crops thumbnail (fallback frame `320x320` if no cover, e.g. TikTok `.image` magic-bytes scan), embeds `duration`/`width`/`height`.
7. `process_split_and_upload` splits only if over ceiling (`1900/2000` for the bot, `3900/4000` for the Premium userbot, via `split_video_by_size_generator` `-c copy` keyframes). Sequential, one extra segment on disk, deletes after each part, quote-replies to your original link message (`reply_to_message_id`), deletes `progress_msg` unless playlist.

### Playlist

`is_playlist_url` (`list=`) -> `extract_playlist_meta` flat (no PO, resilient) -> `DownloadCache` `playlist` + `decision keyboard` (`⬇️ whole`, `🔎 explore 8/page`, `▶️ just this video` if `watch?v=&list=`). Tier pick (`vh/vm/vl/ah/am/al`) -> selector `PLAYLIST_TIERS` (`high 1080p/best`, `medium 720p/≤160k`, `low 480p/≤70k`) applied per video via `download_media(format_selector=...)` (not `format_id`). One rolling `progress_msg` across videos, `⚠️ Skipped` per bad entry, final `Sent M/N`.

### Direct file

`is_ytdlp_supported` false -> `is_link` true -> `a/compression (no yt-dlp, no cookies) -> `aiohttp` `GET` `512 KB` chunks, `Header X-Title` stripped, `SSRF` guard (`is_private/is_loopback/is_link_local/...` -> refuse), `Flood` tier-aware (`free 5/min`, `basic 8`, `plus 12`, `pro 20`, creator `30`).

---

## Admin consoles

### Admin console (full) -- `/start` as `SYSTEM_CREATOR_ID`

| Button | What it does | Notes |
|---|---|---|
| `👥 List / ➕ Add / ➖ Remove` | Whitelist `database.json` `authorized` | `is_valid_telegram_id` 5-11 digits, add auto-removes from blacklist |
| `🚫 Blacklist Logs / 🔓 Unban` | See/undo `blacklisted` (strangers auto-blocked when `SUB_ENABLED=false`) | When `SUB_ENABLED=true`, strangers get subscription prompt, not blacklist |
| `📄 Doc Mode` | Toggle `document_mode` for you (send as `sendDocument` vs `sendVideo/Audio`) | Per-user |
| `🍪 Cookie Jars` | `YouTube/Instagram/TikTok/X/Global` + `➕ Per-Site Jar` (`cookies/ytdlp/<site>.txt`). Per-site key = first label of `netloc` (`pornhub.com` -> `pornhub.txt`). `Download / Replace (.txt doc)` + YouTube `🧪 Test / 💾 Save Backup / ♻️ Restore`. Fresh `igcookies` now `rm direct_ig_session.json` so next IG DM login uses new `sessionid` directly. | Uses `cookie_manager` snapshots `0o444` + `merge write-back` + `meta.json` freshness `21d` |
| `🔐 PO Token` | `Start/Stop/Diagnosis/Test`, `POT_AVAILABLE` badge | Deno `127.0.0.1` only, `// TGBOT_LOCALHOST_PATCH` marker |
| `👑 Premium Uploads` | Whitelist `premium_users` for `4 GB` via userbot, `🔑 Generate Session` (phone -> dial pad code entry, never typed, then `2FA` -> `💾 Save to .env` + `schedule_self_restart`) | Bot API hard `2 GB`, Premium userbot `4 GB` via `LOG_CHANNEL_ID` staging + `copy_message` |
| `📨 Direct-Forward` | `Enable/Disable IG/X/TikTok`, `Pair IG` (6-digit DM code), `Unpair`, `Test X/TikTok`, `Set X Chat PIN` (`waiting_for_x_pin` 4 digits -> `XCHAT_PIN` + bridge supervisor `tools/start_xchat_bridge.sh` re-reads `.env` every 5s) | `DIRECT_FORWARD_CHAT_ID`, `300s±40%` jitter, `delay_range [2,4]`, `CurlCffi chrome136`, `IG-U-RUR` echo, `pin_geo US` |
| `💳 Subscriptions` | `Enable/Disable` mode, `Free tier ✅/❌`, `Add/Remove channel` (multi `channels[]`), `WebApp`, `List subs`, `Grant/Revoke` (`<user_id> <tier> [days]`) | Telegram only, free `5/d`, `basic 100/100⭐`, `plus 500/250⭐`, `pro 2500/500⭐`, priority `0-3`, `Stars` `sendInvoice` + `TON` `toncenter` memo, `free` last in queue |
| `💥 Abort Transfer` | `queue._pending.clear()`, `rm -rf cache`, `oldest_cursor` purge | |
| `🔄 Restart Bot` | `schedule_self_restart` (`SIGTERM` -> `KeyboardInterrupt` -> `systemd Restart=always`) | No SSH needed, also used after `Premium Save` |
| `❌ Close` | Delete console message | |

---


## Cookies the right way

* **Layout:** `cookies/youtube/ytcookies.txt` (`+ .backup`), `instagram/igcookies.txt`, `tiktok/ttcookies.txt`, `twitter/xcookies.txt`, `ytdlp/<site>.txt` + `cookies.txt` fallback. All `0o444` at rest, snapshots in `cache/cookies/*.snapshot` (purged hourly, `max_cache_age_hours 2`).
* **Upload:** `Get cookies.txt LOCALLY` extension -> log in where video plays -> Export `Netscape` -> `Admin -> 🍪 -> Replace` **as `.txt` document** (paste is rejected for YouTube, truncated). For per-site: `➕ Per-Site Jar` -> type `reddit` -> send `reddit.txt`.
* **Freshness:** `utils/cookie_manager.py` `overlay merge` on success (never deletes keys, refuses empty snapshot) + `freshness_warnings(21d)` -> admin `📤 Last uploaded: Xh ago` / `✅ Last success: Xh ago`. Direct-forward workers don't trigger write-back, so DM-only jars go stale -- that's why `utils/cookie_refresher.py` sequential headless (`one Chromium at a time, ~300 MB peak, not 4 tabs` on `4GB+8swap`) visits each site every `24h ±1h` (`COOKIE_REFRESH_ENABLED=true`) via `Playwright` `chromium-1234` (`add_cookies -> goto -> networkidle -> context.cookies() -> Netscape`).

---

## Direct-forward DM relay in depth

One `direct_forward_state.json` shared `IG/X/TikTok`, written **merge-only per platform** (`_state_save_owned` + `_STATE_LOCK`) -- never full-dict overwrite, so one worker can't clobber another's `last_id` (fixed `2026-08-11` duplicate waves). Cursors are `item_id` (IG) / `twid` `last_id` (X, shared with `xchat_bridge` `last_seq`) / `server_message_id` (TikTok). `thread_activity` watermark makes idle polls `0` private calls. `xchat_bridge_state.json` + `xchat_inbox.jsonl` are protected from `auto_clean_cache`.

* **Instagram:** `instagrapi` `2.18.14` `delay_range [2,4]`, `CurlCffi chrome136`, `IG-U-RUR/SHBID/SHBTS/WWW-Claim/X-MID` echo, `pin_geo US`. `poll 300s±40%` + `warmup` (`account_info` + `direct_threads(5)x3`). Gap recovery paginates `direct_v2/threads/{id}/` with `cursor` until `oldest_id <= last_id` (`8x25=200` cap) and **only bumps cursor on success** (failed stays behind for retry). Optional `IG_DIRECT_MQTT_ENABLED` hybrid `MQTToT` `edge-mqtt.facebook.com` (`~5 MB`, no browser) alongside polling when set.
* **X:** self-DM `twikit` `twid` `auth_token`, `client.set_cookies` from `xcookies.txt` (live-reloaded via `hash-compare` + rebuild on `twid` change), `ton.twitter.com` DM photos fetched via throwaway `httpx.AsyncClient` (avoids `__cf_bm` `CookieConflict`), `magic-bytes` not size for media, `gql.tweet_detail` focal `tweet-<id>` for photo-only pastes. `XCHAT_PIN` `4` digits -> `xchat_bridge.mjs` Deno sidecar (`tgbot-xchat-bridge.service` resident supervisor, re-reads `.env` every `5s`) decrypts to `cache/xchat_inbox.jsonl`.
* **TikTok:** `wss://im-ws-sg.tiktok.com/ws/v2` `cmd 500 NEW_MSG_NOTIFY` protobuf (`access_key=md5("9"+APP_KEY+wid+"f8a69...")`), `ttwid` unquoted, prime `15s` swallow backlog, dedupe `server_message_id`, `oembed` author via `run_in_executor`, jittered `TIKTOK_DIRECT_POLL_SECONDS`.

---

## Subscriptions & quotas (Telegram only)

`SUB_ENABLED=false` -> legacy `is_authorized` only (stranger -> `blacklist`). `true` -> `check_access` (`is_subscription_active` or `free_enabled` + `check_all_channels` `is_channel_member` `member/administrator/creator` via `get_chat`/`get_chat_member`). The `/start` greeting shows one self-contained message (intro guide + access prompt) with a **"✅ I joined — verify"** button (`chkjoin:` callback, live membership re-check) when the user must join channels for free access.

Tiers (`tiers.py`): `free 5/d` `priority 0`, `basic 100/d 100⭐` `1`, `plus 500/250⭐` `2`, `pro 2500/500⭐` `3`. Daily `usage` `YYYY-MM-DD` per `user_id` in `database.json`, `increment_quota` thread-safe, prune `7` days. `DownloadQueue` priority `0->3` (free last). `Stars` via `sendInvoice` + `pre_checkout` raw `UpdateBotPrecheckoutQuery`, `TON` via `toncenter` inbound memo (`user_id`).

Flood: `is_flood(user, window 60s, limit)` tier-aware `free 5`, `basic 8`, `plus 12`, `pro 20`, creator `30`.

---

## Logs you will see

* `sudo journalctl -u tgbot -f` (systemd) + `tail -f logs/bot.log` (local `5 MB x3`) + **Telegram** `LOG_CHANNEL_ID` (required, `TelegramChannelHandler` `sendRichMessage` + `sendMessage` fallback, `html` escaped, `32768` Rich limit). There is exactly one log channel since the 2026-10-18 Bale removal (`bale_log`/`BALE_LOG_CHANNEL_ID` are gone). System monitor `Go` (`build/tgbot-monitor`) posts `#system` `15 min` + `80%` warnings to that channel, even when the bot is down.

---

## Troubleshooting quick

* **YouTube `PO-token provider` `403`:** `Admin -> PO Token -> Diagnosis` (`no-auth / cookies / cookies+PO` counts) -> `Start/Stop`. `127.0.0.1` only, `Deno >=2.0`, `bgutil 1.3.1`.
* **`storyboard-only` / zero formats:** bot-flagged cookies -> re-export from browser where video *plays*, `Replace`, `Test`.
* **DM not relayed:** `Admin -> Direct-Forward` `Pair/Test`, check `direct_ig_session.json` `0600`, `freshness_warnings`, `Please wait` in `logs/bot.log` -> pass checkpoint in official app, `restart`.
* **Disk full:** `Abort Transfer` or `rm -rf cache/*`, `RUNTIME_SETTINGS` `max_disk_usage_pct 95`.

For install, see `UBUNTU_VPS_SETUP.md:9` (log channel creation) and `CONFIGURATION` in `blueprint.md`.


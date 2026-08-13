# tgbot — Private Media Downloader & Streamer for Telegram

A private, secure, resource-efficient Telegram bot that downloads media from
**YouTube, Instagram, TikTok, X/Twitter, and every other site supported by
yt-dlp nightly**, uploads it to Telegram (auto-split across the 2 GB / 4 GB
ceiling), and can hand out **direct HTTP stream links** for any file you forward
to it — piped straight from Telegram's servers with zero local buffering.

Built on **pyrogram**. Provisioned with a one-shot `./install.sh` (no Docker
required). Runs as a `systemd` service that survives reboots.

> **Last verified:** 2026-08-13 — **Bale.ai frontend now live** (optional `tapi.bale.ai` mirror, same `INFO` level logging to `bale_log`), **GitHub/YouTube/Translate/Web on both Telegram & Bale**, **free-tier Instagram fix**, **IG gap recovery (200) + optional MQTToT push**, **sequential headless cookie refresher (1 tab at a time, 24h, 4GB-safe)**. See [`docs/memory/tgbot-balebot-hardening-2026-08-13.md`](docs/memory/tgbot-balebot-hardening-2026-08-13.md), [`docs/memory/tgbot-instagram-risky-and-push-2026-08-13.md`](docs/memory/tgbot-instagram-risky-and-push-2026-08-13.md).
> Full yt-dlp site support (1,786 patterns, generic excluded) + dual Telegram/Bale logging at same level.

> **New to this?** The complete, beginner-friendly walkthrough — from "I just
> bought a VPS" to "the bot is live" — lives in
> [`docs/UBUNTU_VPS_SETUP.md`](docs/UBUNTU_VPS_SETUP.md). This README is the
> overview; the architecture deep-dive is in [`blueprint.md`](blueprint.md).

---

## ✨ Key features

- **🔑 PO-token YouTube support.** A local `bgutil-ytdlp-pot-provider` Deno server
  mints proof-of-origin tokens so YouTube extractions keep working. Cookies + PO
  token, no silent fallback — and a one-click **diagnose** in the Admin Console.
- **🍪 Protected cookie jars.** Each download gets a disposable snapshot; the live
  jars are locked read-only and backed up, with session-rotation **write-back**
  on every successful run (so they don't go stale). Test / Save Backup / Restore
  Backup / per-site jars from the console.
- **🛡️ Granular security gate.** Three tiers: System Creator (you), dynamically
  whitelisted users, and everyone else (auto-ignored). Intruders are blacklisted.
- **🧩 Morphing Admin Console.** An inline-button console to manage users, cookie
  jars, the PO-token provider, document mode, direct-forward pairing, and the
  transfer queue.
- **🎚️ Two-column format selector.** Paste a link → video formats on the left,
  audio on the right, sorted by quality, labeled with estimated file sizes.
- **✨ Streaming status drafts.** While a link is being analyzed, the bot
  streams an animated "thinking…" preview (Bot API 10.1+ `sendRichMessageDraft`)
  that morphs into the real menu — with a graceful fallback for older clients.
- **🎞️ Metadata & thumbnails.** `ffmpeg` square-crops thumbnails (Telegram
  requirement) and embeds duration / resolution / title so media plays natively.
- **⬆️ Big-file uploads.** On-demand keyframe splitting keeps every part
  independently playable; the Bot API handles 2 GB, a Premium userbot lifts it to
  4 GB. Only one extra segment ever sits on disk.
- **📨 Direct-forward DM relay.** DM a video, reel, story, tweet share, or link to
   the bot's own Instagram / X / TikTok accounts and it relays into a Telegram chat — driven
   by the platform's private APIs, no third-party services. X and TikTok use the
   **self-DM** method (you DM yourself — no separate bot account); X even
   works when the conversation is X Chat-encrypted (a Deno sidecar decrypts it).
- **🔄 Auto-updating engine.** A background loop upgrades `yt-dlp` to its nightly
  build every 6 hours (preserving the `[default]` extras).
- **🔗 Zero-disk streaming.** Forward a Telegram file → get an HTTP stream link.
  Files pipe from Telegram to your browser on the fly via a FastAPI bridge.
- **🩺 Site-aware errors.** Opaque yt-dlp exceptions become clear messages:
  sign-in required, geo-blocked, rate-limited, private/deleted, live/storyboard.
- **💳 Subscriptions (Stars/TON/Gram).** Toggleable 3-tier plans (Basic 100/d 100⭐, Plus 500/d 250⭐, Pro 2500/d 500⭐) + free tier (5/d, or join channels). Telegram Stars invoices + TON inbound via memo (`/api/user/status`), daily quota, multi-channel force-join, priority queue (free last), and a WebApp at `https://tgbot.southpark.ir:8080` (direct TLS, wildcard `*.southpark.ir`): landing `/` auto-redirects (Telegram→role), user portal `/app`, admin `/admin/subscription` — fullscreen, `safeAreaInset` aware, native `showPopup` + fallback modal/toast.
- **🐙 GitHub explorer + 🔍 YouTube search + 🈯 Translate + 🌐 Web→Markdown (Telegram + Bale).** Paste any `github.com/owner/repo` → repo panel (ZIP via 20 MB splits on Bale, 2 GB/4 GB on Telegram; branches/tags/releases, issues/PRs, commits, languages, README, file explorer with folder ZIP); `/search <q>`, `/user <u>`, `/trend`; `/yt <q> [n]`, `/ytrecent @chan [n]`, `/ytch @chan <q>`, `/transcript <yt_url>`; `/tr src:dst text`; `/web <url>` (Markdown). All queue through the single worker; Bale uses sanitized 20 MB splits. See `docs/USER_GUIDE.md` for usage table.
- **🇮🇷 Bale.ai frontend (optional, hardened, same log level).** One `tgbot.service` hosts both `pyrogram` (Telegram) and `aiogram 3.30` (`tapi.bale.ai`). Bale is government-owned, so: **no Bale log channel by default** (Telegram logs stay on Telegram), optional separate `bale_log` private channel (`BALE_LOG_CHANNEL_ID`) at same `INFO` level; **LIMITED admin** on Bale (users, blacklist, doc mode, size limits `19/20 MB`, abort -- no cookies/premium/POT/direct-forward); **20 MB real hard limit** (docs lie 50, split `19/20`); `getUpdates` drain (Bale `deleteWebhook` is NOOP). When `BALE_TOKEN` empty, zero Bale code runs.
- **🕸️ Headless cookie refresher (1 tab at a time, 24h).** DM-only jars (IG/X/TT/YT) never get `yt-dlp` write-back, so they go stale. `utils/cookie_refresher.py` visits each site **sequentially** with `Playwright` (one `chromium-1234` `~300 MB` peak, not 4 tabs -> your `4GB+8swap` stays safe) every `24h ±1h`, `add_cookies -> goto -> networkidle -> context.cookies() -> Netscape write` (atomic, `0o444`, `touch meta`, clears `direct_ig_session.json` for IG). Proxy-aware. First run `5-10 min` after boot.
- **🩹 Instagram gap recovery + optional MQTToT push (TikTok-like).** IG worker now paginates `direct_v2/threads/{id}/` with `cursor` until `oldest_id <= last_id` (`8x25=200` cap, `gap fetch: X had Y new` log) and **only bumps cursor on success** (at-least-once). Missed batch from stalled `update_risky_contactpoint` now replays. Optional `IG_DIRECT_MQTT_ENABLED` (`instagrapi 2.18.14` `edge-mqtt.facebook.com`, `~5 MB`, no browser) hybrid push alongside polling.
- **🖥️ Standalone system monitor.** A tiny static Go binary (`cmd/tgbot-monitor/`)
  posts `#system` reports and 80% CPU/RAM/disk warnings to **both** log channels (when `BALE_LOG_CHANNEL_ID` set) -- even when the bot itself is down.

---

## 📋 Requirements

1. **An Ubuntu VPS** — Ubuntu 24.04 LTS is the main focus. A 1 GB box works
   (the installer provisions a 2 GB swap file); 2 GB+ is comfortable.
2. **Telegram credentials (required):**
   - `API_ID` + `API_HASH` from [my.telegram.org](https://my.telegram.org).
   - `BOT_TOKEN` from [@BotFather](https://t.me/BotFather).
   - Your numeric Telegram user ID (message [@userinfobot](https://t.me/userinfobot)).
   - A private log channel ID (`LOG_CHANNEL_ID` Telegram, `-100...`) — **required**: the bot refuses to start without it.
3. **Optional Bale (only if you want the `tapi.bale.ai` mirror):**
   - `BALE_TOKEN` from Bale `@botfather` (`116645...:xxxx`), `BALE_SYSTEM_CREATOR_ID` (your Bale numeric ID), and a private `bale_log` channel (`BALE_LOG_CHANNEL_ID`, same level as Telegram, `angelbalzac` admin). When empty, zero Bale code runs. Bale is government-owned, so logs are separate.
4. **That's it.** `install.sh` installs everything else (git, python, ffmpeg,
   tmux, Deno, `chromium-1234` for cookie refresher, node/npm for XChat bridge, PO-token provider, Go monitor, swap). `playwright 1.62.0` is pre-cached.

---

## 🚀 Quick start

```bash
# 1. Get the code
git clone https://github.com/salehMomtaz/tgbot.git
cd tgbot

# 2. Provision the server (apt, Deno, venv, PO-token provider, Go monitor, swap, systemd units)
./install.sh

# 3. Edit .env with your real tokens
nano .env
#    → fill in API_ID, API_HASH, BOT_TOKEN, SYSTEM_CREATOR_ID, LOG_CHANNEL_ID

# 4. Start as a managed service (survives reboot, auto-restarts on crash)
sudo systemctl enable --now tgbot

# 5. (Recommended) start the standalone system monitor too — it keeps sending
#    #system reports + 80% warnings even when the bot is down
sudo systemctl enable --now tgbot-monitor

# 6. Watch it come up
sudo journalctl -u tgbot -f
```

Then open **Telegram** (and Bale if you set `BALE_TOKEN`), message your bot, send `/start` (or `console`), open the
**🛠 Admin System Console** (Telegram full, Bale LIMITED `19/20 MB` -- no cookies/premium/POT), and upload your YouTube cookies
(**Cookie Jars → YouTube → Replace**). See the
[VPS setup guide](docs/UBUNTU_VPS_SETUP.md) for the fully-explained version,
including Premium session, **dual log channels** (`LOG_CHANNEL_ID` Telegram + `bale_log` `BALE_LOG_CHANNEL_ID`), and cookies.
**New here?** See `docs/USER_GUIDE.md` for a complete feature table (every command, what it does, how to use) and Bale quirks (20 MB, `sendVideo` MPEG4-only, Markdown auto-parse).

---

## 🛠 The Admin Console

Send `console` (or `/start`) to the bot as the System Creator. You get an
inline-button console:

| Button | What it does |
|---|---|
| 👥 List / ➕ Add / ➖ Remove Users | Manage the whitelist of people who can use the bot. |
| 🚫 Blacklist Logs | See (and unban) auto-blocked intruders. |
| 📄 Doc Mode | Toggle sending media as plain documents (no re-encode). |
| 🍪 Cookie Jars | Per-site jars: **Download / Replace**, and for YouTube also **Test / Save Backup / Restore Backup**. Add jars for any other site (per-site). |
| 🔐 PO Token | Start / stop / restart / diagnose the PO-token provider; live status badge. |
| 📨 Direct-Forward | Pair / unpair the bot's Instagram account for the DM relay. |
| 💳 Subscriptions | Toggle mode, free tier + multi-channel add/remove, Stars/TON tiers, grant/revoke, WebApp at `https://tgbot.southpark.ir:8080` (`/` landing auto-redirect, `/app` user, `/admin/subscription` admin, fullscreen `safeAreaInset`). Queue priority 0→3. **Telegram only, no Bale free tier.** |
| 💥 Abort Transfer | Cancel everything in the queue and purge the cache. |
| 🇮🇷 Bale LIMITED | Same `tgbot.service` but **LIMITED**: `List/Add/Remove`, `Blacklist`, `Doc Mode`, `Size Limits 19/20`, `Abort`, `Close` -- **no** 🍪/👑/🔐/📨/💳. Create private `bale_log` channel, add `angelbalzac` admin, set `BALE_LOG_CHANNEL_ID` for same `INFO` level as Telegram (otherwise Bale logs stay local only). |

---

## 🧭 How the bot handles a message

Every update pyrogram receives is routed through an ordered **handler
pipeline**. Handlers are grouped, and groups run in ascending order
(`-2 → -1 → 0 → 1`). A handler that matches usually consumes the update;
`stop_propagation()` kills it entirely, `ContinuePropagation` hands it to the
next handler in the same group. This is how the bot distinguishes **you**
(the System Creator), **whitelisted users**, and **strangers** — and silently
locks the door on the last group.

### User tiers

| Tier | Who | Result at the security gate | What they can do |
|---|---|---|---|
| 🟣 **System Creator** | `SYSTEM_CREATOR_ID` in `.env` | Always passes | Everything a user can do **+ the Admin Console** |
| 🟢 **Whitelisted user** | ID listed in `database.json` → `authorized` | Passes | Download/upload links, forward files for stream links |
| 🔴 **Stranger / intruder** | Anyone not in the two rows above | **Auto-blacklisted**, logged as `⚠️ Intruder Blocked`, dropped | Nothing — silently ignored from now on |

> Blacklisted users (including every stranger who ever messaged the bot) are
> dropped before any handler logic runs. The Creator can review and unban them
> via **Blacklist Logs** in the console.

### Message flow (vertical)

```mermaid
flowchart TD
    U([Incoming update: Message or CallbackQuery]) --> G2["GROUP -2 · Log interceptor<br/>logs raw JSON → continue"]
    G2 --> Kind{Message or<br/>callback?}
    Kind -- Callback --> CB["Callback dispatcher<br/>by callback_data prefix"]
    Kind -- Message --> G1["GROUP -1 · Security gate<br/>(messages only)"]
    G1 --> Q1{from_user<br/>missing?}
    Q1 -- yes --> DROP([🔴 DROP])
    Q1 -- no --> Q2{blacklisted?}
    Q2 -- yes --> DROP
    Q2 -- no --> Q3{authorized?<br/>Creator OR whitelist}
    Q3 -- no --> BL["auto-blacklist + log<br/>'⚠️ Intruder Blocked'"] --> DROP
    Q3 -- yes --> G0["GROUP 0 · State machine + file interceptor"]
    G0 --> S1{text & in admin<br/>state?}
    S1 -- yes --> SM([process add / remove / unban ID<br/>pasted cookies rejected])
    S1 -- no --> S2{doc & Creator &<br/>replace-state?}
    S2 -- yes --> CR([swap cookie jar from .txt])
    S2 -- no --> S3{document / video /<br/>audio / voice?}
    S3 -- yes --> SL([mint 24h HTTP stream link])
    S3 -- no --> GP1["GROUP 1 · Text router"]
    GP1 --> T1{is a link?}
    T1 -- yes --> DL([format-selection keyboard<br/>or direct-URL download])
    T1 -- no --> T2{is Creator?}
    T2 -- yes --> AC([🛠 Admin Console])
    T2 -- no --> WL([👋 Welcome message])
    CB --> C1{^admin_ prefix?}
    C1 -- yes --> C2{is Creator?}
    C2 -- no --> DN([answer 'Access Denied'])
    C2 -- yes --> CA([console action:<br/>users / cookies / PO token / queue])
    C1 -- no --> C3{^dl: prefix?}
    C3 -- yes --> CB2([enqueue selected format / cancel])
```

The same pipeline in plain text (renders anywhere, terminal included):

```
                  ┌──────────────────────────────────┐
  Telegram update │   MESSAGE  or  CALLBACK QUERY    │
                  └────────────────────┬─────────────┘
                                       │
            ┌──────────────────────────▼──────────────────────────┐
  GROUP -2  │  LOG INTERCEPTOR — logs raw JSON, then continues    │
            └──────────────────────────┬──────────────────────────┘
                                       │
            ┌──────────────┴──────────────┐
            │ MESSAGE                     │ CALLBACK
            ▼                             ▼
  ┌─────────────────────┐     ┌───────────────────────────────────┐
  │ GROUP -1  SECURITY  │     │ CALLBACK DISPATCHER (by prefix)   │
  │ GATE (msgs only)    │     │                                   │
  │                     │     │ ^admin_ + not Creator → "Denied"  │
  │ no from_user ──►DROP│     │ ^admin_ + Creator ─► console      │
  │ blacklisted ──►DROP │     │   action (users/cookies/POT/...)  │
  │ not authorized ──►  │     │ ^dl: ──► enqueue chosen format    │
  │   auto-blacklist +  │     └───────────────────────────────────┘
  │   "Intruder" ──►DROP│
  │ else ──► continue   │
  └─────────┬───────────┘
            │ (authorized)
  ┌─────────▼───────────┐
  │ GROUP 0  STATE +    │  text + admin state  ─► add/remove/unban ID
  │ FILE INTERCEPTOR    │  doc + Creator +      ─► swap cookie jar
  │                     │    replace-state        from uploaded .txt
  │                     │  document/video/      ─► mint 24h stream link
  │                     │  audio/voice
  └─────────┬───────────┘
            │
  ┌─────────▼───────────┐  is a link? ──► format keyboard or direct
  │ GROUP 1  TEXT ROUTER│                download (defense-in-depth
  │                     │                 auth re-check inside)
  │                     │  Creator  ──► 🛠 Admin Console
  │                     │  anyone   ──► 👋 Welcome message
  └─────────────────────┘
```

### Notes

- The **security gate only applies to messages**, not callback button presses.
  Callbacks are protected per-handler instead: admin buttons check for the
  Creator; download buttons (`dl:`) are only ever handed to a user who already
  passed the gate and received a format keyboard.
- A forwarded **file** (video / audio / voice / document) never reaches the
  text router — the file interceptor in Group 0 turns it into a stream link
  first.
- **Document Mode** (toggled in the console) changes *how* a finished file is
  uploaded (plain document vs. re-encoded media), not *whether* it flows through
  this pipeline.

### How a link is routed & downloaded

Only one handler owns text-that-is-a-link (`downloader_handler.py`, Group 1).
It fires **only when the message starts with `http://` or `https://`**. Inside,
the first fork decides almost everything: **is the URL known to yt-dlp, or
not?** (checked against all 1,786 compiled yt-dlp `_VALID_URL` patterns,
generic excluded — `utils/downloader/supported_sites.py`). The two branches
share almost nothing.

| What you send | Detected as | Path taken |
|---|---|---|
| `youtube.com` / `youtu.be` | yt-dlp → YouTube | yt-dlp · cookies **+ PO token** (only strategy) · format keyboard |
| `youtube.com/playlist?list=…` (or `watch?v=…&list=…`) | yt-dlp → **YouTube playlist** | flat-extract list → **tier keyboard** (3 video + 3 audio: low/med/high) → download & upload each video |
| `instagram.com` | yt-dlp → Instagram | yt-dlp · `igcookies.txt` → no-auth fallback · format keyboard |
| `tiktok.com` | yt-dlp → TikTok | yt-dlp · `ttcookies.txt` → no-auth fallback · URL rewritten to `/embed/<id>` (yt-dlp#17403 hedge) · format keyboard |
| `twitter.com` / `x.com` | yt-dlp → X | yt-dlp · `xcookies.txt` → no-auth fallback · format keyboard |
| `nicovideo.jp`, `pornhub.com`, `clips.twitch.tv`, `vimeo.com`, `soundcloud.com`, `bilibili.com`, `bandcamp.com`, `reddit.com`, … (all other yt-dlp sites) | yt-dlp → that site | yt-dlp · per-site jar `cookies/ytdlp/<site>.txt` (if you added one) or global fallback · format keyboard |
| **non-yt-dlp URL** (`example.com/video.mp4`, `…/archive.zip`, any generic file or unsupported host) | **direct file** | raw HTTP download — **no yt-dlp, no cookies, no format choice** |

> A genuine direct file (`…/clip.mp4`, `…/archive.zip`, `…/report.pdf`) that
> lives on a page yt-dlp doesn't know about correctly stays on the direct-file
> path and the bot does a plain `GET` on exactly the link you pasted. Only
> **page URLs** that match a yt-dlp extractor (1,700+ sites) take the format
> keyboard path — that routing is automatic (yt-dlp upgrades add new sites with
> no bot-code change). You can also append ` | custom-name.ext` to any link to
> rename the result. The admin **Cookie Jars** menu can add per-site jars for
> any extra host.

```mermaid
flowchart TD
    M([Authorized user sends text]) --> L{starts with<br/>http / https ?}
    L -- no --> X1([Admin Console / welcome<br/>not a download])
    L -- yes --> P["split on the '|' character<br/>url · optional custom name"]
    P --> SOC{"URL matches a yt-dlp extractor?<br/>(1,786 compiled _VALID_URL patterns,<br/>generic excluded)"}

    SOC -- YES · yt-dlp site --> JAR["pick cookie jar by host<br/>(snapshot copy — live jar stays read-only)"]
    JAR --> YL{YouTube?}
    YL -- yes --> YS["cookies + PO token — the ONLY strategy<br/>provider must be running or it errors<br/>(no cookies-only / no-auth fallback)"]
    YL -- no --> OS["cookies first, then fall back to no-auth"]
    YS --> PLQ{URL carries<br/>list= ?}
    PLQ -- yes · playlist --> PFLAT["flat-extract playlist<br/>(titles + count only, no per-video formats)"]
    PLQ -- no · single video --> XF
    PFLAT --> PTK["reply: playlist · video count · TIER keyboard<br/>🎥 video low/med/high · 🎵 audio low/med/high<br/>(a yt-dlp selector picked once, applied per video)"]
    PTK --> PTIER{tier tapped?<br/>or ▶️ just this video}
    PTIER -- ▶️ just this video --> XF["extract_formats · yt-dlp lists<br/>real video + audio formats"]
    PTIER -- a tier --> PLOOP["enqueue ONE queue task<br/>loop each video: download_media(selector)<br/>→ split + upload · one rolling status msg<br/>a bad video is skipped, the rest continue"]
    PLOOP --> OK3([✅ sent M / N videos])
    OS --> XF
    XF --> KB["reply: title · duration · keyboard<br/>🎥 top-5 resolutions · 🎵 top-5 bitrates<br/>(button size = video stream + merged best audio)"]
    KB --> TAP{button tapped?}
    TAP -- ❌ Cancel --> XC([drop cached session])
    TAP -- pick a format --> BIG{> 2 GB and<br/>no Premium userbot?}
    BIG -- yes --> XBIG([⚠️ blocked — pick another<br/>or connect your userbot])
    BIG -- no --> Q2([enqueue in job queue])
    Q2 --> DM["download_media via yt-dlp<br/>video: fmt+bestaudio → merge mp4<br/>audio: extract m4a (no re-encode bloat)<br/>ffmpeg: square thumb + embed metadata"]
    DM --> UP1([split if over the limit → upload])
    UP1 --> OK1([✅ done])

    SOC -- NO · not yt-dlp --> DF["treat as a DIRECT FILE URL"]
    DF --> GET["aiohttp GET · 30-min timeout<br/>name = last path segment (URL-decoded)<br/>or your custom name"]
    GET --> BODY["stream body to disk · 512 KB chunks<br/>live progress bar"]
    BODY --> UP2([split if needed → upload<br/>as a plain document])
    UP2 --> OK2([✅ done])
```

### Playlists

A YouTube link that carries `list=…` is a **playlist** and is handled differently
from a single video:

- `youtube.com/playlist?list=…` → straight to the **tier keyboard**.
- `youtube.com/watch?v=…&list=…` (a video you reached *via* a playlist) → same
  tier keyboard, plus a **▶️ Just this video** escape button (ytdlnis-style) that
  drops into the normal single-video flow.

Because per-video `format_id`s differ across a playlist, you don't pick a
specific stream. Instead you pick a **tier** *before* anything downloads, and the
bot applies it to every video:

| Tier | Video (merged mp4) | Audio (m4a) |
|---|---|---|
| **High** | best ≤ 1080p | best available |
| **Medium** | best ≤ 720p | ≤ 160 kbps |
| **Low** | best ≤ 480p | ≤ 70 kbps |

Each tier is a yt-dlp format *selector* with a `/best` fallback, so a video that
lacks (say) a 480p stream still downloads at the next-best instead of failing.
The whole playlist occupies **one queue slot**; videos are processed one-by-one,
each going through the same split/2 GB-4 GB-ceiling/upload pipeline as a single
download. A video that fails (private, removed, region-blocked) is **skipped, not
fatal** — you get a `⚠️ Skipped` message and the run continues, ending with a
`Sent M/N` summary.

> `PLAYLIST_MAX_VIDEOS` (default `50`, env-configurable) caps how many videos a
> single playlist run will download — a guard against pasting a 1,000-video list.

---

## 🍪 Cookies

YouTube and age/login-restricted sites need browser cookies. The easiest path:
install a "Get cookies.txt" browser extension, log in to the site, export, and
**Replace** the jar in the console (paste the text, *or* send a `.txt` file).

The bot protects your jars:
- Live jars live under `cookies/` (`cookies/youtube/`, `cookies/instagram/`, …),
  and are **read-only at rest** so yt-dlp can't corrupt them.
- Each download uses a **snapshot copy** that's auto-purged later.
- On a successful run, rotated session cookies are **merged back** into the live
  jar (atomic, never deletes keys) — this is what keeps Instagram/Google
  sessions alive for months instead of dying in days.
- **Save Backup** freezes the current jar; **Restore Backup** brings it back. A
  trashed/empty live jar auto-restores from the backup on boot.
- **Test** runs a real extraction against a public video and tells you exactly how
  many formats the site returned (or if the jar is bot-flagged).
- **Per-site jars:** With full yt-dlp support, any of the 1,700+ sites may need
  cookies. The bot auto-generates `cookies/ytdlp/<site>.txt` for all known
  domains at boot (e.g., `pornhub.txt`, `vimeo.txt`, `reddit.txt`). Use the
  Cookie Jars menu → **➕ Per-Site Jar** → type the site name (e.g., `pornhub`)
  → send the `.txt` document. The downloader picks it up by URL domain
  automatically. Special cases (multi-domain sites, adult age gates, Chinese
  sites, DRM) are documented in
  [`docs/cookie_site_special_cases.md`](docs/cookie_site_special_cases.md).

> **Direct-forward note:** The IG/X/TikTok DM workers consume the shared primary
> jars (`igcookies.txt`, `xcookies.txt`, `ttcookies.txt`) but **do not trigger
> cookie write-back** (they use `instagrapi`/`twikit`, not yt-dlp). If a site is
> *only* accessed via DM relay (no manual yt-dlp downloads), its jar will go
> stale — upload fresh cookies periodically or set fallback credentials in `.env`.

---

## 📨 Direct-Forward (DM relay)

You can DM media to the bot's **own Instagram, X, and TikTok accounts** and have it
 delivered into a Telegram chat (`DIRECT_FORWARD_CHAT_ID`):

- Send photos, videos, reels, story shares, tweet shares, or plain links from
   your personal account (whitelist or pairing handshake) to the bot's account.
- TikTok uses a persistent IM-WebSocket connection (`websockets`) to your own
   self-DM (`0:1:{uid}:{uid}`); shares resolve to authors via the public oEmbed
   endpoint and download through the normal yt-dlp pipeline.
- The bot relays them into your Telegram chat; links go through the normal
  yt-dlp pipeline (with cookie jars), enqueued behind interactive downloads.
- Instagram uses `instagrapi`; X uses `twikit` against the **self-DM** method
  (you DM yourself — no separate bot account). When your self-DM is X
  Chat-encrypted, a Deno sidecar (`xchat_bridge.mjs`, run as the auto-enabled
  `tgbot-xchat-bridge` unit) decrypts it — set `XCHAT_PIN` in `.env`. The
  platform's own private APIs are used, no third-party services. Sessions are
  persisted so deleting the wrong file is the #1 way to trigger a checkpoint
  challenge.
- Configure via `.env` (`DIRECT_FORWARD_*`, see
  [`docs/DIRECT_FORWARD_SETUP.md`](docs/DIRECT_FORWARD_SETUP.md)); unconfigured,
  the feature self-disables. Pair / unpair the Instagram account from the Admin
  Console → 📨 Direct-Forward.
- All three relay workers (IG/X/TikTok) share one `direct_forward_state.json`
  and write it **merge-only per platform** — a worker can never clobber another
  platform's cursor, so each DM is delivered exactly once (see the
  [state-race postmortem](docs/memory/tgbot-2026-08-11-x-duplicate-delivery-state-race.md)).
- A 2026-08-11 audit of the three self-DM mechanisms hardened edge cases: the
  XChat bridge cursor + inbox are protected from the hourly cache cleaner, the
  X worker live-reloads its cookie jar (no restart on re-upload), photo-only
  pasted tweets are delivered natively instead of silently failing, and the
  TikTok worker's network calls no longer block the event loop (see the
  [self-DM audit](docs/memory/tgbot-2026-08-11-selfdm-audit.md) and the
  [X photo-paste fix](docs/memory/tgbot-2026-08-11-x-photo-paste-fix.md)).

---

## 📜 Logs

Four streams, same `INFO` level:

- **Service log** (stdout/stderr): `sudo journalctl -u tgbot -f` (and `sudo journalctl -u tgbot -b` for boot)
- **Bot's own log** (timestamped, rotated at 5 MB × 3): `tail -f logs/bot.log`
- **Telegram log channel** (required): `LOG_CHANNEL_ID` (`-100...`, Telegram private channel, `angelbalzac` admin). The bot refuses to start without it. All `INFO`/`WARNING`/`ERROR` mirror there via `TelegramChannelHandler` (rich `sendRichMessage` + `sendMessage` fallback, `html` escaped, `3500` truncated, async daemon thread).
- **Bale log channel** (optional, same level): `BALE_LOG_CHANNEL_ID` (separate private `bale_log` channel on **Bale**, `angelbalzacbot` admin, via `tapi.bale.ai`). When `BALE_TOKEN` + `BALE_LOG_CHANNEL_ID` set, `BaleChannelHandler` mirrors the **same** `INFO` level there (plain text, no HTML, `bale_log` is government-side, so we keep it separate -- Telegram logs never go to Bale unless you set both). When `0`, Bale logs stay local only. Create it like Telegram: new private channel `bale_log` -> add `angelbalzacbot` admin -> forward a channel message to `@userinfobot` on Bale or `https://tapi.bale.ai/bot<token>/getUpdates` after posting to get numeric ID -> put in `.env` `BALE_LOG_CHANNEL_ID=-...` -> `systemctl restart tgbot`. Verify: `[Logger] Bale logging linked to BALE_LOG_CHANNEL_ID ...` in `logs/bot.log`.
- **System monitor**: `cmd/tgbot-monitor` Go binary posts `#system` reports every 15 min and `80%` warnings to **both** log channels when set (otherwise Telegram only) -- even when the bot is down. `sudo journalctl -u tgbot-monitor -f`.

---

## 🔄 Updating the bot

```bash
cd ~/tgbot
git pull origin main
sudo systemctl restart tgbot          # picks up new code
# (install.sh is idempotent — re-run it after a big change to refresh deps/provider)
```

`yt-dlp` updates itself to nightly every 6 hours automatically; no restart needed
for extractor fixes.

---

## 🐳 Docker (optional / legacy)

Docker is **supported but no longer the recommended path**. The bare-metal
`install.sh` + `systemd` flow above is simpler, lighter on a 1 GB VPS, and is
what this project targets. If you still want Docker, the image is self-contained
— it installs Deno, the Python deps, **and** clones + builds the PO-token provider
so YouTube works out of the box:

```bash
cp .env.example .env && nano .env   # fill in your secrets first
docker compose up --build -d
docker compose logs -f --tail=50
```

The provider is built into the image under `/opt` (outside the `.:/app` bind-mount)
and `YTDLP_POT_PROVIDER_PATH` is set automatically. Provide your secrets in `.env`
(secrets are **not** baked into the image).

---

## 🔒 Security & privacy

`.gitignore` already protects your secrets. **Never commit** (or paste publicly):

- `.env` — your Bot Token, API keys, session string.
- `database.json` — whitelisted user IDs.
- `*cookies.txt` / `ytcookies.backup` — live browser sessions.
- `*.session` / `*.session-journal` — active bot/userbot authorization.

The PO-token provider is patched to bind **`127.0.0.1` only** — it is never
reachable from the internet.

---

## 📚 More

- **Beginner VPS guide:** [`docs/UBUNTU_VPS_SETUP.md`](docs/UBUNTU_VPS_SETUP.md)
- **Architecture deep-dive:** [`blueprint.md`](blueprint.md)
- **Contributor / agent notes:** [`AGENTS.md`](AGENTS.md)
- **Direct-forward (DM relay) setup:** [`docs/DIRECT_FORWARD_SETUP.md`](docs/DIRECT_FORWARD_SETUP.md)
- **Cookie strategy:** [`docs/cookie-strategy.md`](docs/cookie-strategy.md)
- **Go feasibility (why the monitor is Go):** [`docs/go-feasibility.md`](docs/go-feasibility.md)
- **Agent memory notes:** [`docs/memory/`](docs/memory/)

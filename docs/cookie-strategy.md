# Cookie strategy research: lessons from active media downloader bots

This document captures what I found researching four actively maintained media
downloader Telegram bots on GitHub (last push within the last three months at
time of writing), and the resulting cookie strategy for tgbot.

## Research findings

### 1. vaaski/telegram-ytdl (TypeScript, active, 208 commits)

**Cookie approach:** Single `storage/cookies.txt` file. `cookieArgs()` checks
if the file exists and passes it via `--cookies` to yt-dlp. yt-dlp handles
atomic writes natively (`--cookies` uses `MozillaCookieJar.dump()` → temp file +
rename). No fancy protection — relies purely on yt-dlp's robust jar handling.

**Key insight:** The simplest approach works best. Don't fight yt-dlp's cookie
system; work with it. yt-dlp updates the jar on every request, keeping cookies
"live" as it iterates through the request batch.

### 2. cobalt.tools / imputnet/cobalt (Node.js, very active, 44k stars)

**Cookie approach:** **Token authentication, not cookies.** For Instagram:
sessionid token. For Twitter: auth_token + ct0 (CSRF). For YouTube: a cookie
string. The cobalt JSON passes these **directly as Authorization headers**,
not as Netscape cookie files. Their format:

```json
{"instagram": ["sessionid=<value>"], "twitter": ["auth_token=<value>; ct0=<value>"]}
```

**Key insight:** Tokens are more stable than full cookie jars. A sessionid or
auth_token differs much slower than the full cookie set from a browser. This
reduces stale-cookie surface area drastically. We keep yt-dlp for YouTube
(because PO-token), but we can extract our own lightweight resolver for
Instagram (and maybe Twitter/TikTok) using the "token auth" model.

### 3. Toshik1978/social-media-downloader (Python, active, supports Insta + X + YouTube)

**Cookie approach:** **Delegate to a third-party API.** For Instagram, uses
`instagram-looter2.p.rapidapi.com/post`, passing a user-supplied
`RAPIDAPI_KEY`. For Twitter (X), uses `xviewer.p.rapidapi.com/tweet`. For
YouTube, plain yt-dlp with `--no-playlist --no-progress -f bestvideo/bestaudio/best`.

**Key insight:** When cookies are an operational headache, outsourcing to a
service that manages cookies for you is valid. RapidAPI charges for Instagram
access but guarantees a working cookie backend. This is the same pattern as
cobalt.tools except paid.

### 4. Ula19/telegram-youtube-downloader-bot (Python, active, YouTube-only)

**Cookie approach:** **Proxy + token rotation, no cookies for most downloads.**
Uses Cloudflare WARP (SOCKS5) 1.1.1.1 containers (6+ endpoints) in a
round-robin rotation with cooldown on `ip_blocked`. Uses `bgutil-ytdlp-pot-provider`
(same PO-token approach as ours) as the primary auth for YouTube. Cookies are a
last resort. Their entire architecture is built around avoiding cookies.

**Key insight:** YouTube works best with **proxies + tokens** (same as our own
bgutil PO-token setup). For pull-through 4K/2GB files, they scale with WAN
proxies, not cookies. That mirrors our own current approach. Cookies are a
weaker auth vector than IP rotation + token mints for most big sites.

## The synthesis: what we actually ship

Why jars went stale here, precisely: Instagram/Google/TikTok/X rotate session
cookies **on every authenticated response** (`Set-Cookie`). yt-dlp applies
those updates natively and rewrites whatever jar file it was pointed at on
exit — that is the entire vaaski/telegram-ytdl strategy and it works. This bot
pointed yt-dlp at a *throwaway snapshot* and locked the real jar `0o444`, so
every rotation was discarded, the real session froze in time, and Instagram
invalidated it (HTTP 400) within days/weeks.

The shipped design (`utils/cookie_manager.py`) keeps both properties — race
isolation AND rotation capture:

1. **Snapshot per yt-dlp run** (like before): concurrent metadata fetches
   never share a file; a crashed run leaves a deletable copy, never a
   half-written real jar.
2. **Overlay merge-back on success**: when a cookie-authenticated run wins,
   the snapshot's cookie lines are overlaid onto the real jar by key
   `(domain, path, name)`; temp-file + `os.replace` atomics; the `0o444`
   read-only-at-rest lock is re-applied. The merge **never deletes keys** and
   **refuses empty snapshots**, so "yt-dlp wiped the jar on invalid session"
   is impossible by construction. This is the vaaski lesson applied without
   giving up snapshot isolation.
3. **Instagram ladder stays no-auth-first** (public reels never burn the
   session), cookies fall back for login-walled posts — now trustworthy
   because write-back keeps the session warm.
4. **Freshness watchdog**: every success/failure/upload is recorded in
   `cookies/meta.json`; startup + the Admin → Cookies menu warn when a jar
   has not been warm in `COOKIE_STALE_WARNING_DAYS` (default 21), turning
   silent expiry into an actionable notification.

## Architecture summary

```
Admin upload (_write_cookie_jar)
      │  validate → autobak → atomic write → lock 0o444 → meta.last_upload
      ▼
real jar (locked at rest)
      │  cookie_manager.acquire()  → per-run snapshot (cache/cookies/*.snapshot)
      ▼
yt-dlp run ──success──▶ cookie_manager.commit(success=True)
      │                       overlay merge into real jar (never delete,
      │                       refuse empty, re-apply 0o444) + meta.last_success
      └──failure──▶ cookie_manager.commit(success=False)
                            snapshot discarded; auth-classified errors →
                            meta.last_failure (watchdog material)
```

See `utils/cookie_manager.py` for implementation; knobs in `config.py`
(`COOKIE_WRITEBACK_ENABLED`, `COOKIE_STALE_WARNING_DAYS`).

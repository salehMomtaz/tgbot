# Cookies & IG anti-detection — jars, protection & session hygiene

All cookie-strategy, jar-protection and Instagram anti-detection notes merged. This is the companion to invariant #4 and `utils/cookie_manager.py`.

## Sources consolidated

- `docs/cookie-strategy.md`
- `docs/cookie_site_special_cases.md`
- `docs/memory/tgbot-cookie-protection-and-monitor.md`
- `docs/memory/tgbot-ig-cookie-jar-confusion.md`
- `docs/memory/tgbot-ig-anti-detection.md`

---

---

## 1. Source: `docs/cookie-strategy.md`

# Cookie strategy research: lessons from active media downloader bots

This document captures what I found researching four actively maintained media
downloader Telegram bots on GitHub (last push within the last three months at
time of writing), and the resulting cookie strategy for tgbot.

## Research findings

### 1. vaaski/telegram-ytdl (TypeScript, active, 208 commits)

**Cookie approach:** Single `storage/cookies.txt` file. `cookieArgs()` checks
if the file exists and passes it via `--cookies` to yt-dlp. yt-dlp rewrites
that jar itself on exit (`YoutubeDL.__exit__` → `save_cookies()` →
`YoutubeDLCookieJar.save()`, a plain `open(file, "w")` overwrite — see
`yt_dlp/cookies.py`). No fancy protection — relies purely on yt-dlp's jar
lifecycle.

**Key insight:** The simplest approach works best *for a sequential CLI*.
Don't fight yt-dlp's cookie system; work with it: yt-dlp merges every
`Set-Cookie` it receives into the in-memory jar and persists it on exit, so
the session stays "warm" across runs. (The actual atomic-write hardening is
ours — yt-dlp's own save is a plain overwrite, safe for them because a CLI
has a single writer at a time; a bot with concurrent extractions must not
inherit that.)

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

---

## 2. Source: `docs/cookie_site_special_cases.md`

# YT-DLP Cookie Site Special Cases Report

This document lists yt-dlp supported sites that **don't fit the simple `domain.txt` naming pattern** (where `domain` is the first label of the hostname, e.g., `pornhub.com` → `pornhub.txt`).

## Pattern Reference
- **Standard pattern**: `cookies/ytdlp/<first-label>.txt`
  - Example: `https://www.pornhub.com/video/123` → `pornhub.txt`
  - Example: `https://vimeo.com/123456` → `vimeo.txt`
  - Example: `https://m.tiktok.com/v/123` → `tiktok.txt` (subdomain stripped)

## Special Cases Requiring Manual Handling

### 1. Multi-Domain Sites (Same Cookies Across Different Domains)
These sites share cookies across multiple hostnames. The jar file should be named after the **primary brand**, but cookies may need to work for all listed domains.

| Primary Jar | Also Covers These Domains |
|-------------|---------------------------|
| `google.txt` | `youtube.com`, `youtu.be`, `youtube-nocookie.com`, `google.com`, `googleapis.com`, `googlevideo.com`, `ggpht.com` |
| `facebook.txt` | `facebook.com`, `fb.com`, `fbcdn.net`, `fbsbx.com`, `instagram.com` (partial), `messenger.com` |
| `twitter.txt` | `twitter.com`, `x.com`, `t.co`, `twimg.com`, `ton.twitter.com` |
| `microsoft.txt` | `onedrive.com`, `sharepoint.com`, `office.com`, `live.com`, `microsoft.com`, `azure.com` |
| `amazon.txt` | `amazon.com`, `amazonaws.com`, `primevideo.com`, `twitch.tv` (partial), `aiv-cdn.net` |
| `apple.txt` | `apple.com`, `icloud.com`, `podcasts.apple.com`, `music.apple.com`, `tv.apple.com` |

### 2. Sites with Significant Subdomains (Different Cookie Scopes)
These sites use different subdomains that may have **separate cookie jars** or **different login states**.

| Site | Subdomains | Notes |
|------|------------|-------|
| TikTok | `tiktok.com`, `www.tiktok.com`, `m.tiktok.com`, `vm.tiktok.com`, `vt.tiktok.com`, `vn.tiktok.com` | Short-link domains (`vm.`, `vt.`, `vn.`) redirect to canonical; cookies work across all. Bot normalizes shortlinks. |
| Instagram | `instagram.com`, `www.instagram.com`, `m.instagram.com`, `i.instagram.com` | API endpoint `i.instagram.com` uses same cookies. Bot has dedicated jar at `cookies/instagram/igcookies.txt`. |
| YouTube | `youtube.com`, `www.youtube.com`, `m.youtube.com`, `youtu.be`, `youtube-nocookie.com`, `youtubei.googleapis.com` | InnerTube API (`youtubei.googleapis.com`) uses same cookies. Bot has dedicated jar at `cookies/youtube/ytcookies.txt`. |
| Reddit | `reddit.com`, `www.reddit.com`, `old.reddit.com`, `new.reddit.com`, `amp.reddit.com`, `oauth.reddit.com` | OAuth domain may need separate token. |
| Twitch | `twitch.tv`, `www.twitch.tv`, `m.twitch.tv`, `clips.twitch.tv`, `api.twitch.tv` | Clips and API may have different auth. |
| Bilibili | `bilibili.com`, `www.bilibili.com`, `m.bilibili.com`, `api.bilibili.com`, `live.bilibili.com` | Live and API subdomains may need separate cookies. |
| Twitter/X | `twitter.com`, `x.com`, `mobile.twitter.com`, `api.twitter.com`, `ton.twitter.com` | DM media (`ton.twitter.com`) needs auth cookies. Bot has dedicated jar at `cookies/twitter/xcookies.txt`. |

### 3. Adult Sites with Age Verification / Regional Blocks
These sites often require **account cookies + age verification** and may serve different content based on IP geography.

| Site | Jar Name | Special Notes |
|------|----------|---------------|
| Pornhub | `pornhub.txt` | Requires logged-in account for 1080p+; age gate may need `age_verified=1` cookie. |
| XVideos | `xvideos.txt` | Age verification cookie may be needed. |
| XHamster | `xhamster.txt` | Age verification required for some content. |
| RedTube | `redtube.txt` | Part of MindGeek network (same as Pornhub); cookies may partially overlap. |
| YouPorn | `youporn.txt` | Part of MindGeek network. |
| XNXX | `xnxx.txt` | Age gate cookie. |
| SpankBang | `spankbang.txt` | (Not in current list; add if needed) |
| Xvideos:quickies | `xvideos.txt` | Same as main XVideos. |

**Recommendation**: For adult sites, upload cookies from a **browser session where you've already passed age verification** in the target region.

### 4. Chinese Sites (Require Local IP / Phone Verification)
These sites often **block non-CN IPs** or require **phone-verified accounts**. Cookies from foreign IPs may not work.

| Site | Jar Name | Notes |
|------|----------|-------|
| Bilibili | `bilibili.txt` | Requires CN IP or proxy; login needs phone verification. |
| IQIYI | `iqiyi.txt` | Requires CN IP; VIP content needs subscription. |
| Youku | `youku.txt` | Requires CN IP. |
| AcFun | `acfun.txt` | Requires CN IP. |
| Douyin (TikTok CN) | `douyin.txt` | Separate from TikTok; requires CN phone. |
| Weibo | `weibo.txt` | Requires CN phone verification. |
| Xigua (西瓜视频) | `xigua.txt` | ByteDance; requires CN IP. |

### 5. Streaming Services (DRM / Subscription Required)
These sites need **active subscription cookies**. Free tier cookies may not unlock HD/4K.

| Site | Jar Name | Notes |
|------|----------|-------|
| Netflix | `netflix.txt` | DRM-protected; yt-dlp can only download with Widevine CDM (not supported here). |
| Disney+ | `disney.txt` | DRM-protected. |
| HBO Max | `hbomax.txt` | DRM-protected. |
| Prime Video | `primevideo.txt` | DRM-protected. |
| Paramount+ | `paramount.txt` | DRM-protected. |
| Peacock | `peacock.txt` | DRM-protected. |
| Crunchyroll | `crunchyroll.txt` | Subscriber cookies needed for premium content. |
| Funimation | `funimation.txt` | Merged into Crunchyroll. |
| Viki | `viki.txt` | Subscriber cookies for HD. |

**Note**: DRM-protected streams **cannot be downloaded** by this bot (no Widevine support). Only clear-text HLS/DASH manifests work.

### 6. Sites with Complex Domain Structures
| Site | Jar Name | Complexity |
|------|----------|------------|
| GitHub | `github.txt` | `github.com`, `gist.github.com`, `raw.githubusercontent.com`, `githubassets.com` |
| GitLab | `gitlab.txt` | Self-hosted instances need separate jars (e.g., `gitlab.example.com` → `example.txt`). |
| Google Drive | `drive.txt` | `drive.google.com`, `docs.google.com`, `sheets.google.com`, `lh3.googleusercontent.com` (thumbnails) |
| Dropbox | `dropbox.txt` | `dropbox.com`, `dl.dropboxusercontent.com`, `content.dropboxapi.com` |
| OneDrive | `onedrive.txt` | `onedrive.live.com`, `1drv.ms`, `sharepoint.com`, `graph.microsoft.com` |
| Mega | `mega.txt` | `mega.nz`, `mega.co.nz`, `g.api.mega.co.nz` |
| MediaFire | `mediafire.txt` | `mediafire.com`, `download.mediafire.com` |
| ZippyShare | `zippyshare.txt` | `zippyshare.com`, `www*.zippyshare.com` (numbered mirrors) |

### 7. Social / Feed Sites (API vs Web Cookies Differ)
| Site | Jar Name | Notes |
|------|----------|-------|
| Reddit | `reddit.txt` | Web cookies ≠ OAuth tokens. Use web session cookies. |
| Tumblr | `tumblr.txt` | May need `tumblr.com` + `assets.tumblr.com`. |
| Pinterest | `pinterest.txt` | `pinterest.com`, `pinimg.com` (CDN). |
| Flickr | `flickr.txt` | `flickr.com`, `staticflickr.com`, `api.flickr.com`. |
| Imgur | `imgur.txt` | `imgur.com`, `i.imgur.com` (direct images need no cookies). |
| SoundCloud | `soundcloud.txt` | `soundcloud.com`, `api-v2.soundcloud.com`, `cf-media.sndcdn.com`. |
| Mixcloud | `mixcloud.txt` | `mixcloud.com`, `api.mixcloud.com`. |
| Bandcamp | `bandcamp.txt` | `bandcamp.com`, `bandcamp.com/download/...` (fan accounts). |

### 8. Sites Already Having Dedicated Jars (Outside ytdlp/)
These sites have **dedicated cookie jars** at the top level and should NOT use `cookies/ytdlp/` jars:

| Site | Dedicated Jar Path |
|------|-------------------|
| YouTube | `cookies/youtube/ytcookies.txt` |
| Instagram | `cookies/instagram/igcookies.txt` |
| TikTok | `cookies/tiktok/ttcookies.txt` |
| Twitter/X | `cookies/twitter/xcookies.txt` |

The `_resolve_jar_path()` function in `utils/downloader/cookies.py` checks these first before falling back to `cookies/ytdlp/`.

### 9. Duplicate / Alias Entries in Current List
The following entries in `yt_dlp_sites.txt` are **aliases or sub-extractors** that map to the same jar:

| Alias | Maps To | Reason |
|-------|---------|--------|
| `youtu` | `youtube.txt` | `youtu.be` short domain |
| `fb` | `facebook.txt` | `fb.com` short domain |
| `x` | `twitter.txt` | `x.com` new domain |
| `nicovideo` | `niconico.txt` | Same site, different naming |
| `instances` | (remove) | Likely a generic extractor artifact |
| `player-api` | (remove) | Generic, not a real site |
| `members` | (remove) | Generic, not a real site |
| `arhiiv` | (remove) | Likely typo for `arhiiv` (Estonian archive) |
| `ok` | `ok.txt` | OK.ru (Odnoklassniki) - keep if needed |

**Action**: Remove `instances`, `player-api`, `members`, `arhiiv` from the auto-generated list.

### 10. Sites Needing Manual Addition (Not in Current yt-dlp Extractors)
These popular sites may not appear in the extractor list but are commonly requested:

| Site | Suggested Jar Name | Domain Pattern |
|------|-------------------|----------------|
| OnlyFans | `onlyfans.txt` | `onlyfans.com` |
| ManyVids | `manyvids.txt` | `manyvids.com` |
| Clips4Sale | `clips4sale.txt` | `clips4sale.com` |
| IWantClips | `iwantclips.txt` | `iwantclips.com` |
| LoyalFans | `loyalfans.txt` | `loyalfans.com` |
| FanCentro | `fancentro.txt` | `fancentro.com` |
| JustForFans | `justforfans.txt` | `justforfans.com` |
| Patreon | `patreon.txt` | `patreon.com` |
| SubscribeStar | `subscribestar.txt` | `subscribestar.adult` / `subscribestar.com` |
| Fantia | `fantia.txt` | `fantia.jp` (JP adult) |
| Pixiv Fanbox | `fanbox.txt` | `fanbox.cc` |
| Booth.pm | `booth.txt` | `booth.pm` |
| Gumroad | `gumroad.txt` | `gumroad.com` |

---

## Admin Console Usage Guide

### Adding a Per-Site Cookie Jar
1. Open **Admin Console → 🍪 Cookies → ➕ Per-Site Jar**
2. Enter the **site identifier** (e.g., `pornhub`, `vimeo`, `reddit`)
   - The bot will create/overwrite `cookies/ytdlp/<site>.txt`
3. Send the **cookies.txt file** as a document (not pasted text)
   - Export from browser extension (e.g., "Get cookies.txt LOCALLY")
   - Must be valid Netscape format with ≥1 real cookie line
4. Bot confirms: `✅ Per-site cookie jar saved to cookies/ytdlp/<site>.txt!`

### Verifying a Jar Works
1. Admin Console → 🍪 Cookies → 🧪 **Test Cookies**
2. Select the site (or "Custom URL")
3. Bot runs a lightweight yt-dlp extraction and reports format count

### Cookie Freshness
- Jars are **locked read-only (0o444)** at rest
- On **successful** yt-dlp run: rotated session cookies are **merged back** (write-back)
- On **failed** auth: failure recorded in `cookies/meta.json`
- Startup watchdog warns if jar hasn't had a successful run in **21 days** (`COOKIE_STALE_WARNING_DAYS`)

---

## Maintenance Checklist

- [ ] Remove alias entries: `instances`, `player-api`, `members`, `arhiiv`
- [ ] Add missing popular sites: `onlyfans`, `patreon`, `fantia`, `fanbox`, `booth`, `gumroad`
- [ ] Document any site-specific cookie requirements (age gate, region, subscription)
- [ ] Test top 20 sites with real cookies to verify jar naming works
- [ ] Update this report when yt-dlp adds new extractors

---

*Generated: 2026-08-12 | Bot version: tgbot with full yt-dlp site support*

---

## 3. Source: `docs/memory/tgbot-cookie-protection-and-monitor.md`

# Cookie protection & tamper monitor

tgbot cookie corruption is solved. Two things were wrong and both are fixed
(commit `b44db54` + earlier `e326794`, then the in-memory read fix `af7fa77`):

1. **`.download()` AttributeError** (`e326794`, then `af7fa77`) — pyrogram v2
   wants `client.download_media(message=..., in_memory=True)`, not
   `client.download()`. The returned `BytesIO` has its cursor at EOF, so the
   upload handler must read it with `.getvalue()` (not `.read()`, which returns
   empty bytes). This was the past "jar replace did not work / overwrote to
   empty" symptom: the failed document handler never wrote the jar at all.

2. **Text-paste truncation** (the real remaining bug vs balebot) — Telegram
   silently truncates text messages at **4096 chars**, but a YouTube Netscape jar
   is ~17 KB. tgbot *allowed* pasting the jar as text; balebot blocks this and
   requires a `.txt` document. Fixed: tgbot now rejects text-paste in the Replace
   state and requires a document, validates ≥1 real cookie line (≥7 tab-separated
   fields), writes atomically (temp + fsync + `os.replace`), auto-backs up to
   `<file>.autobak`, re-`chmod 444`s ytcookies, and purges snapshots.

**Why yt-dlp can't corrupt the original jar:** `utils/downloader.py`
(`get_cookies_for_url` → `_cookie_snapshot`) always points yt-dlp at a disposable
copy in `cache/cookies/<basename>.snapshot`. yt-dlp rewrites *that* snapshot on
exit, never the original. The original `cookies/youtube/ytcookies.txt` is locked `0o444` at
startup (`main.py`) and after every Replace. Verified by running a full extraction
through the bot's exact code path: original md5 unchanged.

**Tamper monitor on the test VPS:** `cookie-watch.service` (systemd, enabled for
reboot, runs as the VPS user) runs `<repo>/tools/cookie_watch.sh`. It watches the
parent *directories* (`<repo>` and `<repo>/cache/cookies`) with `inotifywait` and
logs timestamp/event/file/size/md5 + the running bot processes to
`<repo>/logs/cookie_watch.log`. Watch dirs (not files) because `os.replace`
unlinks the inode, which would blind a file watch.

**Why not auditd:** `auditd` is **inert on the test VPS host** (a
container/LXC-style kernel restricts the audit netlink — `auditctl -w` looks
accepted but `ausearch` returns 0 events on real changes). Don't rely on auditd
there; use inotifywait. There may be a leftover auditd watch rule; it's harmless.

Both bots coexist on the same test VPS: balebot's POT provider on
`127.0.0.1:4416`, tgbot's on `127.0.0.1:4417`. See
[VPS two-bots runtime state](vps-two-bots-runtime-state.md) and
[tgbot ↔ balebot integration](tgbot-balebot-integration.md).

---

## 4. Source: `docs/memory/tgbot-ig-cookie-jar-confusion.md`

# Instagram cookie jar — the "Last authenticated success" confusion

**Date:** 2026-08-28 · **Operator report:** uploaded a fresh `igcookies.txt`
(~2 KB, 30 lines including `ps_l`/`ps_n`/etc.), injected it into a Chrome
incognito window, Instagram web said "no account". Re-asked: "why 63
hours ago? cookies were fresh and I uploaded them last night." The
admin menu said "✅ Last authenticated success: 63h ago · rotation
merges: 1, 📤 Last uploaded: 0h ago".

## What was happening

Two distinct timestamps are stored in `cookies/meta.json` per jar:

| Field | Updated by | Meaning |
|---|---|---|
| `last_success` | `utils/cookie_refresher._refresh_one` — only after a successful headless refresh cycle | When the headless browser last authenticated against the jar (the "live IG login via the headless Chrome") |
| `last_upload` | `modules/admin.cookies._write_cookie_jar` — only after the operator (re)uploads the jar via a Telegram document | When the operator last replaced the file via the bot's cookie admin menu |
| `last_failure` | Same as `last_success`, but on a failure | Most recent headless refresh failure |
| `merge_count` | `utils.cookie_refresher._refresh_one` | How many times a headless refresh captured rotated cookies |

A fresh operator upload sets `last_upload` but does NOT set
`last_success`. The `last_success` is the timestamp of the most
recent 24h headless refresh cycle that successfully authenticated —
which can be 63h ago if the headless cycle skipped the site (the
old code skipped IG with `mtime 6h ago (<20h)` even when the
session was already stale).

The admin menu's old label "Last authenticated success" made the
operator think their fresh upload was 63h old, when in fact:

- The `last_upload` field showed `0h ago` (the operator's just-uploaded jar).
- The `last_success` was 63h ago (the headless cycle that ran at 17:32 and
  had the old `ps_l`/`ps_n`-less `igcookies.txt`).

The two are completely different things and the menu now labels
them clearly:

- "✅ Last headless refresh: 63h ago" — when the 24h cycle last
  successfully ran against this jar.
- "📤 Last uploaded: 0h ago" — when the operator last (re)uploaded.

## The "no account" in Chrome incognito

The bigger problem. The operator uploaded a 2 KB jar with 30 lines
(Chrome DevTools "Copy as Netscape" export typically has each
cookie twice, plus the IG personalization cookies `ps_l`/`ps_n`). The
bot's `_write_cookie_jar` saved the file as-is. But the next
`utils/cookie_refresher` cycle called
`utils/cookie_manager.overlay_cookies` to merge in the rotated
sessionid. `overlay_cookies` parsed the jar into a dict keyed by
`(domain, path, name)` and wrote back. **The dict silently dropped
duplicates** (the same key written twice → only one entry survives).
Result: 30 lines → 13 entries → 1 KB on disk. The
`ps_l`/`ps_n`/`__Secure-ENID`/etc. were lost in the merge.

When the operator then injected the resulting 11-line, 1 KB jar
into Chrome incognito, Instagram web said "no account" because
the missing `ps_l`/`ps_n` cookies are the ones that tell IG "this
browser has been used by this account before" — without them, IG
treats the session as a brand-new anonymous visit.

### Why headless refresh can't recover `ps_l`/`ps_n`

The 24h headless cycle (or a manual `refresh_all_cookies_sequential`)
navigates to `/explore/`, `/accounts/edit/`, the home page, and
scrolls the feed to trigger IG's personalization XHRs. With the
recent fix it ALSO dismisses the "Save Your Login Info?" /
"Turn on Notifications?" dialogs that IG shows to fresh devices
(those clicks themselves trigger the XHRs).

But `ps_l`/`ps_n` are **device-binding** cookies. They are issued
only to the BROWSER that has a long history of the account
(specific fingerprint, local storage, IndexedDB, etc.). A fresh
headless Chromium in a VPS datacenter IP, even with all the
right cookies imported, is treated by IG as a brand-new device
and **never gets `ps_l`/`ps_n`** — IG just shows "no account" to
that device.

This is by IG's design: `ps_l`/`ps_n` are anti-fraud signals
(distinctive cookies that only the legitimate user's browser has).
The headless browser cannot reproduce them.

**The only way for Chrome-injectable cookies to work is to
re-export from the operator's MOBILE app** (not Chrome DevTools).
The mobile app has the device-binding cookies because the
operator's phone has been used with that account. Mobile exports
include `ps_l`/`ps_n` because the mobile app's NetworkSecurityConfig
is what issued them.

## The fix (commit history)

1. `utils/cookie_manager._parse_cookie_lines` now returns an
   ORDERED LIST of `(key, raw_line)` instead of a dict. The
   `merge_snapshot_into_real` and `overlay_cookies` paths walk
   this list in source order, build a key→line dict for
   in-place updates, and write the source-ordered output. Duplicates
   are preserved end-to-end. Verified: 30-line autobak now
   produces 26 entries (was 13); each duplicated key is still
   present twice in the output.

2. `utils/cookie_refresher._refresh_one` for IG no longer skips
   on `mtime < 20h` (the old skip missed the case where the operator
   uploads fresh cookies but the headless cycle is the only way to
   rotate IG's mid/rur/csrftoken). It also navigates to
   `/explore/` and `/accounts/edit/` after the home page to force
   the server to issue fresh tokens in response headers (the home
   page alone doesn't rotate them).

3. The admin menu label "Last authenticated success" was renamed
   to "Last headless refresh" to make it unambiguous that this
   metric is about the 24h headless cycle, not the jar's age.
   The companion "Last uploaded" line already shows the upload
   timestamp; the two together tell the operator exactly what
   they need to know.

## Operator recovery path (after this fix is live)

The current `igcookies.txt` on disk is the 1.5 KB / 17-line version
that already lost its `ps_l`/`ps_n`. The fix above is permanent going
forward, but for the **next** headless cycle (within 24h), the
fresh cookies in the operator's mobile app will land in the jar
fully. If the operator wants immediate recovery:

1. **Re-export cookies from the Instagram MOBILE app** (not Chrome
   DevTools — mobile export has each cookie once and includes
   `ps_l`/`ps_n`).
2. **Re-upload via Admin → 🍪 Cookie Jars → Replace.**
3. The current bot's `login_by_sessionid` works with the new
   sessionid (verified at 23:03:07 today and at every
   supervised refresh since) — Chrome's web login still won't
   recognize the session because the headless browser cannot
   reproduce `ps_l`/`ps_n` (they are device-binding — see the
   "Why headless refresh can't recover ps_l/ps_n" section above).
   The mobile-app export DOES include them, so Chrome injection
   from the operator's mobile export works.

The bot itself (DM worker, profile-pic monitor, friend-media IG
archiver) is **unaffected by ps_l/ps_n** — those are only needed
for IG's web personalization flow. instagrapi's private API works
fine with the 11-cookie minimal set.

## Reference clones (untracked, in `reference/`)

- `reference/instagrapi` — upstream 2.18.12; read `client.py`,
  `mixins/auth.py`, `mixins/private.py` for session/header mechanics.
- `reference/okgram` — the **phone-grade** reference: device pool,
  stable UUIDs, `IG-U-RUR` echo + persistence, geo auto-sync,
  `doctor` diagnostics, rate governor.
- `reference/insta-wizard` — async client with checkpoint
  taxonomy + challenge section.
- `reference/instaharvest_v2` — curl_cffi transport + challenge
  resolver + 14-layer fallback GraphQL/mobile/web.

---

## 5. Source: `docs/memory/tgbot-ig-anti-detection.md`

# Instagram anti-detection posture (the "automated behavior" flag)

**Date:** 2026-08-03 · **Area:** `modules/direct_forward.py`, `config.py`,
`docs/DIRECT_FORWARD_SETUP.md`

## What happened

The bot account's Instagram session was checkpointed with *"We suspect
automated behavior on your account"*. The DM-relay poller (`instagrapi`
private API from a datacenter VPS) was the loudest signal: a **fixed 120 s
cadence**, an inbox sweep of **20 thread items every cycle regardless of
activity**, and machine-paced requests.

## What the research says actually matters (instagrapi docs + community)

Ranked by evidence: (1) fresh device fingerprint per run → persist ONE session
file and never re-login; (2) datacenter ASNs are inherently suspicious → one
stable residential proxy if needed, never rotating; (3) login churn (`login()`
every boot) → resume `direct_ig_session.json`; (4) impossible travel (owner +
VPS in different countries) → keep the session/IP identity stable; (5) sudden
activity spikes → jitter + minimal call volume; (6) web-scraping endpoints →
stay on private API. Full source list lives in this repo's session notes and
`docs/DIRECT_FORWARD_SETUP.md` ("Avoiding checkpoints").

## Hardening implemented

| Lever | Implementation |
|---|---|
| Cadence | `DIRECT_FORWARD_POLL_SECONDS` default 120→**300**, each interval randomized **±40%** (`_poll_interval()`); never below 60 s |
| Request pacing | `cl.delay_range = [2, 4]` (was [1, 3]) — every private-API call |
| Idle volume | per-thread **`last_activity_at` watermarks** in state (`thread_activity` map): unchanged thread ⇒ **0** item fetches (was ~20/cycle) |
| Identity | unchanged: persisted session/device first, sessionid bootstrap 2nd, password last; settings re-dumped per cycle |
| IP | `DIRECT_FORWARD_PROXY` — ONE stable proxy for the account's whole life; applied to instagrapi (`set_proxy`) and twikit (guarded) |
| Checkpoint | freeze **3–5 h randomized**, log tells the human to pass it in the official app; no retry storms |
| Login resilience | **worker never dies on a login failure** — retries each poll with a *fresh* client (half-failed instagrapi login poisons state), so a mid-run `igcookies.txt` re-upload is picked up without a bot restart; challenge errors still freeze 3–5 h |

## Operational notes

- Deleting `direct_ig_session.json` is the #1 self-inflicted checkpoint
  trigger — it forces a brand-new device fingerprint.
- The igcookies jar (yt-dlp) and the DM session are the same account: heavy
  yt-dlp cookie usage ALSO feeds the account's risk score; the no-auth-first
  Instagram ladder exists partly for this reason.
- Playwright+stealth browser automation is **not** the right layer here — we
  don't scrape Instagram web; we speak the private mobile API. (Browser stealth
  would matter only for scraping instagram.com pages, which yt-dlp handles
  with its own challenge solvers.)
- X (twikit) shares the poll-jitter + proxy, but X's DM API is far less
  policed; no watermarking there (single-thread history call already).

## Second checkpoint (2026-08-05 01:05 UTC) — what the deeper research found

Seven hours of normal relayed traffic, then a **manual-verification
checkpoint** on a single `direct_v2/inbox` poll (`[400]` + `challenge_required`).
The poller did everything right (jitter, watermarks, no retry storm, froze
~3.6 h). So the trigger wasn't cadence — it was **identity correlation**.
Research from `reference/` (okgram, insta-wizard, instaharvest_v2) + instagrapi
issue/PRs:

1. **TLS/HTTP fingerprint (top suspect).** instagrapi 2.18.12 speaks the
   private API over plain `requests.Session` = **Python OpenSSL JA3/JA4 + HTTP/1.1**.
   Instagram's WAF fingerprints the TLS handshake: a "Pixel 8 Pro Android"
   UA riding Python's TLS stack is a detectable mismatch. 2026 fixes swap the
   transport for a browser/OkHttp impersonation layer (curl_cffi / httpcloak /
   tls_client). Current session confirms it: `device=Pixel 8 Pro`, UA app
   `428.0.0.47.67`, but the connection layer is Python.
2. **Routing headers not echoed.** The saved session has `ig_u_rur: False`
   (and no `shbid/shbts/direct-region-hint`). okgram treats **echoing
   `IG-U-RUR`/`X-MID`/`X-IG-WWW-Claim` from every response** as a first-class
   fix for sessionid logouts — a missing `IG-U-RUR` is a top bounce cause.
   instagrapi 2.18.12 does NOT capture/replay these.
3. **Geo coherence is currently OK** (VPS IP = US/PA, session = US/en_US/
   GMT-04:00) — so the checkpoint was NOT impossible-travel. But the IP is a
   **datacenter ASN** (Redoubt Networks); DC IPs are inherently higher-risk.
4. **Challenge type matters.** This was a *native* manual-verification
   checkpoint (`flow_render_type` native), not the email/SMS code flow that
   `challenge_code_handler` can pass — so only a human in the official app
   resolves it; freezing was correct. insta-wizard's `challenge.py` taxonomy
   (VettedDelta / UFAC / scraping-warning) is the reference for classifying.

### Concrete mitigations (all applied 2026-08-05)

Implemented in `utils/ig_anti_detect.py` (wired into `_make_client` /
`_ig_login` in `modules/direct_forward.py`). Every piece is independently
failing-safe: a missing dependency or a library change only logs a warning and
the worker keeps running on the previous behaviour.

| # | Lever | Status |
|---|---|---|
| 1 | **Impersonated transport** for instagrapi — `CurlCffiAdapter` (from the `curl-adapter` PyPI package, the same adapter instagrapi's own public-transport `curl` extra uses) mounted on `cl.private`. `cl.private` stays a `requests.Session` so cookies/proxies/verify/headers all keep working; only the TLS layer impersonates `chrome136`. Requires `pip install "curl-adapter>=1.2.1"` (added to requirements.txt) + a compat shim (curl_cffi 0.15 renamed `normalize_browser_type` → `resolve_latest_browser_type`). `_configure_private_session_retry` is patched once so every `load_settings`/`login_by_sessionid` re-applies the transport instead of silently re-mounting the stock HTTPAdapter | **done** |
| 2 | **Echo `IG-U-RUR`/`X-MID`/`X-IG-WWW-Claim` + persist them** — `install_token_echo` wraps `cl.private_request` to capture `ig-set-ig-u-rur`, `ig-set-ig-u-shbid`, `ig-set-ig-u-shbts`, `x-ig-set-www-claim`, `ig-set-x-mid` from every response into `cl.settings` (durable via the per-poll `dump_settings`), and patches `base_headers` + `get_settings` (instagrapi natively serializes rur/www_claim/mid but DROPS shbid/shbts) to re-apply captured values | **done** |
| 3 | **Sticky residential proxy** (`DIRECT_FORWARD_PROXY`) — DC ASN is the residual risk | knob exists; test VPS refreshes cookies through the SOCKS5 proxy whose egress = the bot's own IP |
| 4 | **geo/locale/device explicit sync** via `set_country/set_country_code/set_locale/set_timezone_offset` (`pin_geo`), driven by `IG_DIRECT_COUNTRY`/`IG_DIRECT_COUNTRY_CODE`/`IG_DIRECT_LOCALE`/`IG_DIRECT_TZ_OFFSET`/`IG_DIRECT_TZ_NAME` (defaults US / 1 / en_US / -14400 / GMT-04:00) | **done** |
| 5 | Keep the 3–5 h freeze on native checkpoints, **and alert the relay chat directly** (not just the log channel) with instructions to pass the verification in the official app | **done** |
| 6 | **Cold-start warmup** — `warmup()` runs a few paced benign reads (`account_info`, `direct_threads(5)` ×3) right after login so the first real poll isn't the session's first activity on a fresh IP | **done** |
| 7 | **Burst pacing on backfill** — `burst_pace(n)` returns a per-item sleep that scales with the backfill size (`base = 6 + log2(n+1) * 2` seconds, capped at 30s). Applied in the gap-fetch loop. For a 30+ item backfill (the pattern that previously triggered "we suspect automated behavior") the cumulative activity now spaces over 5-7 minutes instead of 2.5 — the live 1-item case adds <2 s | **done** |
| 8 | **Cold-start jitter** — `cold_start_jitter(cl)` runs AFTER warmup, before the first real poll: `account_info` → 60-90 s → `direct_threads(20)` → 45-90 s → `direct_threads(20)`. The first paired-thread poll is no longer the very first observable activity on a new session | **done** |
| 9 | **Public-GraphQL soft-block counter** — `record_public_soft_block()` / `public_soft_block_active()`. Counts consecutive `JSONDecodeError`s on `cl.media_pk_from_url` (the public web endpoint). After 3 strikes, skip the public path for a 10 min cooldown — hammering a throttled endpoint only deepens the block | **done** |
| 10 | **Email-change alert handler** — `install_email_change_alert(cl, alert_sink=...)` registers a `change_password_handler` that, when invoked, alerts the operator (via the direct-forward chat) and re-raises. We do NOT attempt to bypass the password reset programmatically (that deepens the flag) — we freeze the worker per the existing challenge policy and tell the operator what to do in the official app | **done** |

### Deployment facts (2026-08-05, test VPS)

- `curl-adapter==1.2.1` installed in the VPS venv; `curl_cffi==0.15.1b2` (yt-dlp
  extra, left untouched).
- Verified end-to-end in the running bot: transport swaps to `CurlCffiAdapter`
  and survives `load_settings` re-mounts; `login_by_sessionid` and the
  persisted-session resume both work through the impersonated transport;
  warmup runs after resume; worker entered the idle `~300 s ±40%` poll loop with
  `NRestarts=0`, no checkpoint, geo persisted (`US / -14400 / en_US`), `mid`
  captured. `ig_u_rur`/`shbid` were not set in that run (Instagram only sends
  them on certain responses); instagrapi's synthetic values remain the fallback
  and the capture persists real values whenever the server does send them.
- Gotcha hit during wiring: wrapping `cl.private_request` must NOT re-pass
  `self` (the captured `orig_request` is already a bound method) — passing it
  shifted the endpoint arg and blew up with `'Client' object has no attribute
  'startswith'`.

### Reference clones (untracked, in `reference/`)

- `reference/instagrapi` — upstream 2.18.12 (same as installed); read
  `auth.py`/`private.py` for the session/header mechanics.
- `reference/okgram` — the **phone-grade** reference: device pool, stable
  UUIDs, `IG-U-RUR` echo + persistence, geo auto-sync, `doctor` diagnostics,
  rate governor. Best source for what the app's fingerprint actually looks like.
- `reference/insta-wizard` — async client with checkpoint taxonomy +
  challenge section (`mobile/sections/challenge.py`), proxy rotation.
- `reference/instaharvest_v2` — curl_cffi transport + challenge resolver +
  anti-detect system (14-layer fallback GraphQL/mobile/web).

All four are gitignored via `.gitignore` → `reference/`; they're local research
material, not vendored dependencies.

### Third recurrence (2026-08-28) — burst-pacing + cold-start + soft-block + email-handler

Operator reported the **second** recurrence of "we suspect automated behavior
on your account" + forced email change. All 6 levers above were live
(verified via log lines: `private transport now impersonates chrome136`,
`geo pinned to US / en_US / GMT-04:00`, `warmup: account_info ok` × 3, etc.),
and the fresh cookie upload + cursor-gap recovery worked correctly. The
auto-check log showed: `gap fetch: thread 34028... had 51 items, 33 new
after cursor 32977...` followed by 33 items delivered at a near-uniform
4-6 s cadence over ~150 s.

**Root cause:** even with `cl.delay_range = [2, 4]`, the bot's own
`await loop.run_in_executor(None, cl.private_request, ...)` calls in
the gap-fetch loop are NOT subject to `delay_range` (delay_range only
applies to instagrapi's *internal* calls, not to the bot's direct
private_request calls). So all 33 calls fired in a tight burst, looking
exactly like scripted scraping to Instagram's behavior model.

**The four new levers (7-10) close the burst + first-activity + email
bypass paths.** Each is independently failing-safe (degrades to a
no-op on a missing dep / library change) and wired into
`_instagram_worker` in `modules/direct_forward/instagram.py`.

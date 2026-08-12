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
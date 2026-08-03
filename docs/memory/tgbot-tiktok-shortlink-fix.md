# TikTok shortlink ("malformed site") fix

**Date:** 2026-08-03 · **Area:** `utils/downloader.py` (`normalize_url`,
`extract_formats`, `download_media`), `requirements.txt`, `utils/updater.py`

## Symptom

A large share of `https://vt.tiktok.com/<code>/` links failed with yt-dlp
errors that surfaced to users as **"The site changed its layout or the URL is
malformed."** (`❌ Job Failure` in the log channel). Intermittent — the same
link could work later.

## Root cause (researched from yt-dlp source + issues)

1. yt-dlp's short-link extractor for `vt/vm/vn.tiktok.com` resolves the
   redirect with a **bare HEAD whose UA is `facebookexternalhit/1.1`**, no
   cookies, no impersonation (`TikTokShortIE`). TikTok answers a fraction of
   these with its **anti-bot interstitial** (a ~600-byte page,
   `x-tt-system-error: 3`, or a JS "Please wait…" challenge) instead of a 301.
2. The same stochastic block hits the final `www.tiktok.com` webpage fetch —
   `__UNIVERSAL_DATA_FOR_REHYDRATION__` absent → `Unable to extract webpage
   video data`. Blocks are IP/fingerprint-reputation based and flip pass/fail
   between attempts.
3. yt-dlp ≥ 2025-01 ships a **pure-Python proof-of-work challenge solver**
   (PR yt-dlp/yt-dlp#15672) — but the webpage fetch requires **curl-cffi
   impersonation**; without the extra installed, TikTok logs "no impersonate
   target available" and blocks rise again.

## Fix (three layers)

1. **Pre-resolve ourselves** — `_resolve_tiktok_short_url` inside
   `normalize_url`: real browser UA, `requests.get(stream=True)` follow
   redirects, one retry, accept only a canonical `tiktok.com/@…` hop (a
   login/interstitial hop keeps the short URL so yt-dlp's own resolver +
   challenge solver still gets its chance). 1-hour TTL cache because
   `extract_formats` and `download_media` normalize the same URL.
2. **`curl-cffi` everywhere yt-dlp goes**: `requirements.txt` and the nightly
   auto-updater both pin `yt-dlp[default,curl-cffi]` (AGENTS.md invariant #5).
3. **Stochastic-failure retries**: `extract_formats` adds one extra `no-auth`
   attempt for TikTok; `download_media` does one clean no-cookies retry when a
   TikTok attempt died (block pages are not auth failures — the watchdog stays
   quiet).

## Verified

`vt.tiktok.com/ZS4UH9uRa/` → canonical `@user/video/7649651757630508295…`
locally; extraction exact-size `6,125,053` vs delivered `6,125,002`.

## Gotchas / don'ts

- Don't drop the shortlink back into yt-dlp unresolved "because it works on
  your box" — block probability is per-IP and changes weekly.
- The mobile-app API path (`api16/api19 aweme`) is dead without X-Argus
  signing (yt-dlp banned from `api-h2`); don't chase it as a fallback —
  oEmbed (`/oembed?url=`) and `/embed/v2/{id}` are the viable last-resort
  metadata paths if the webpage solver ever breaks again.
- TikTok formats are muxed single streams — no `+bestaudio` merge; exactness
  rule is the video's own.

Sources: yt-dlp `extractor/tiktok.py` (short-IE), yt-dlp issues #15418 /
#17332, PR #15672 (+ review notes in #15644), gallery-dl PR #8850 (same solver
ported), Evil0ctal/Douyin_TikTok_Download_API (cookie/UA pairing for CDN).

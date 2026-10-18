# 2026-10-18 — Instagram photo posts (the missing image half) + 15-day log audit

Operator report: *"I sent some Instagram links to the bot yet received an error.
I noticed the posts were photos — I want the bot to be able to send me photo
posts from Instagram links I send the bot personally."*
Plus the standing maintenance ask: load the code, scan the last 15 days of logs
for errors, check every monitoring/subsystem, fix what is broken.

**Result: Instagram photo posts and carousels are now delivered (5-photo post,
mixed 3-photo + 1-video carousel and a reel all verified live). The 15-day log
audit found no unfixed failure class — every error cluster in the window was
already fixed by the 2026-10-08/10/16 work or is a transient upstream blip.**

---

## Defect 1 (reported) — Instagram photo posts were unreachable

### Symptom (from the log, 2026-10-18 10:53:32)

```
❌ Extraction Error: Failed to parse
`https://www.instagram.com/p/Dem-LAGmuqO/?img_index=4&obrf=NHdvcTlzazN3bTRx`.
Details: `Extraction failed: The site changed its layout or the URL is malformed.`
```

The same URL came back from `yt_dlp` (no bot code involved) as:

```
ERROR: [Instagram] Dem-H43ADyk: No video formats found!
```

`Dem-H43ADyk` is a **carousel entry id**, not the post's id — the giveaway that
the failure happened while yt-dlp processed the post's entries.

### Root cause (read from the installed extractor, not guessed)

`yt_dlp/extractor/instagram.py::InstagramBaseIE._extract_product_media` builds
`formats` from `video_versions` **only**; `image_versions2` is used solely for
`thumbnails`. So:

* a photo post (single image) → `_extract_product` returns a dict with
  `formats: []` → `_real_extract` calls
  `raise_no_formats('There is no video in this post')` (by design), and
* a carousel containing *any* photo → `_extract_product` returns a playlist whose
  photo entries have no formats → yt-dlp's playlist processing raises
  "No video formats found!" on the first photo entry, killing the whole post.

`utils/downloader/errors.py::_classify_ytdl_error` then mapped the resulting
"failed to parse"/"unable to extract" text to *"The site changed its layout or
the URL is malformed"*, which sent the operator hunting for a layout change.

Every `/p/` link the operator ever sent that contained a photo is in this class:
`Dem-LAGmuqO` (5 photos), `DdqJjTeApl9` (2), `DdrN08ViEpH` (4), `DePX4DyEdWm` (4),
`DeUZHSKkWhz` (3 photos + 1 video), `DePboi5Gn2w`, `DePboi5Gn2w`-class links from
Oct 12–17. **Reels were never affected** (they have videos) — the bot delivered
`Del7xLXxjII` at 10:53:46, seconds after failing the photo post next to it.

### Fix — extend yt-dlp, do not fork it

New module `utils/downloader/instagram_post.py`:

* `class InstagramIE(upstream InstagramIE)` — the **class name is the mechanism**:
  `ie_key()` = class name minus `IE`, and `YoutubeDL.add_info_extractor()` keys
  its registry by `ie_key()`, so this subclass *replaces* the built-in extractor.
  (The first attempt named the class `IGPhotoIE`; `ie_key()` became `IGPhoto`,
  the class was appended as a second extractor, and the built-in ran instead —
  a debug print proved the override was never called. Keep the name.)
  * `raise_no_formats()` → no-op (a format-less post is exactly the case we want).
  * `_extract_product_media()` → additionally records every `image_versions2`
    candidate URL as `ig_photos`, in Instagram's own order (largest first;
    `candidates[0]` is the original, and upstream *reverses* the list into
    `thumbnails`, so `thumbnails[0]` is the 150×150 crop — never use it).
  * Everything else (logged-out `PolarisLoggedOutDesktopWWWPostRootContentQuery`
    GraphQL call, LSD/CSRF tokens, webpage/embedded-JSON fallback, curl_cffi
    impersonation, cookies) stays upstream's code.
* `is_instagram_post_url()` — `/p/` and `/tv/` only (never `/reel(s)/`,
  `/stories/`, `/share/`); `canonical_post_url()` strips `?img_index=…&obrf=…`.
* `fetch_instagram_post()` — enumerates the post (no-auth first, cookie jar as
  the login-walled fallback, with `cookie_manager` write-back) and returns
  `{title, caption, author, items[], has_photo, n_photo, n_video}`. A video-only
  post is a *successful* result with `has_photo == False`.
* `download_post_photos()` — downloads the images: prefers the original,
  falls back through the smaller renditions when the original fails or exceeds
  Telegram's 10 MB `sendPhoto` ceiling, validates by **magic bytes**, runs
  survivors through `ensure_photo_path()` (no `[400 PHOTO_EXT_INVALID]`),
  and keeps an oversized original for document delivery if that is all there is.

`modules/downloader_handler.py`:

* `deliver_instagram_post()` — called by `show_format_selection()` BEFORE the
  yt-dlp flow for every IG `/p|tv/` URL. When the post has ≥1 photo it delivers
  the whole post and returns `True`: photos in one media group per 10 items
  (`send_photo` for a single one), quote-replying to the user's link
  (invariant #14), then any carousel **video** through
  `download_media(permalink, format_selector='bestvideo+bestaudio/best')` +
  `process_split_and_upload` so it keeps thumbnails/metadata and the 2 GB/4 GB
  ceilings. Photo sends fall back to individual sends if a group is rejected, and
  the task dir is always removed.
* `show_format_selection()` gained a `premium_client` parameter (the 3 in-module
  call sites pass `premium_app`) so carousel videos can use the Premium path.
* All blocking Instagram work goes through `run_in_executor` (invariant #10).

Scope deliberately kept: **video-only `/p/` posts still use the yt-dlp format
keyboard** (no loss of quality choice), and reels are untouched. The path needs
no session, so it works while `IG_AUTH_ENABLED=false`.

### Defect 1b — the error text was misleading

`_classify_ytdl_error()` now recognises Instagram's anonymous refusal
("Instagram sent an empty media response", "Failed to parse JSON" on an
instagram.com URL) and names the jar instead of blaming a layout change:

> Instagram did not return this post to an anonymous request (it may be deleted
> or login-walled, or Instagram is rate-limiting this server anonymously). A
> logged-in session is needed: send a fresh `cookies/instagram/igcookies.txt` jar
> via Admin Console → Cookies.

Non-Instagram URLs keep the generic message (verified in the tests).

### Verified live (Telethon, `tools/telethon_drive.py`, after a service restart)

| input | result |
|---|---|
| `/p/Dem-LAGmuqO/?img_index=4` (5 photos) | 5 photos, first with caption, all quote-replying; log `🖼 Instagram post delivered: … 5 photo(s), 0 video(s)` |
| `/p/DeUZHSKkWhz/?img_index=3` (3 photos + 1 video) | 3 photos in one group + the carousel video (1080×1080, 0.4 MB, `thumb_count 1`) |
| `/reel/Dce6Z8os04Y/` (regression) | format keyboard → picked 1280p → video 2.8 MB, 44 s, thumb present |

Zero `ERROR`/`CRITICAL` lines in `logs/bot.log` since the restart.

---

## 15-day log audit (2026-10-03 → 2026-10-18)

`logs/bot.log.2` + `.1` + `bot.log` (rotated 5 MB × 3 — the window is fully
covered). Every distinct ERROR/CRITICAL signature, its dates, and the verdict:

| signature | occurrences / dates | verdict |
|---|---|---|
| `[DirectForward/IG] re-login failed … Sleeping 1h` | 48, all Oct 1–3 | **already fixed** — the old retry storm; replaced 2026-10-08 by "never auto-relogin, wait for a fresh jar" (0 occurrences after Oct 3) |
| `aiogram.dispatcher Failed to fetch updates - ClientDecodeError` | 37, all Oct 11 23:00 | **transient upstream** — Bale's API answered a `503 Service Temporarily Unavailable` HTML page; aiogram retried with backoff and reconnected. No code defect, no action |
| `TelegramServerError` / `TelegramNetworkError` (aiogram) | 9 + 8, scattered | transient `tapi.bale.ai` blips, self-healed |
| `[DirectForward/X] … [Errno 36] File name too long` | 3, Oct 9 | **already fixed** — byte-bounded outtmpl (2026-10-10) + shared `sanitize_filename` (2026-10-16) |
| `queue_manager Task … failed` (FileId.decode on a Persian filename) | 3, Oct 8 07:24 | **already fixed** — `_x_deliver_tweet` resolves the real on-disk file before `send_photo` |
| `[DirectForward/TT] … [400 PHOTO_EXT_INVALID]` | 1, Oct 11 13:31 | **already fixed** — content-based `classify_media_file` (2026-10-16) |
| `[DirectForward/TT] WS error: ping timeout / no close frame` | 3 | expected WebSocket reconnect (jittered backoff by design) |
| Instagram `Extraction Error` on `/p/` links | 18 across Oct 12–18 | **THE reported defect** — fixed this session (above) |

No traceback outside those clusters. Nothing else to fix.

---

## Monitoring / subsystem audit — all healthy

| system | state |
|---|---|
| `tgbot.service` | active (restarted 14:58 for this change); `Restart=always`, `NRestarts=0`, RAM 207 MB of a 2.4 GB cap |
| PO-token provider | healthy on `127.0.0.1:4417` (`bgutil-provider` v1.3.1, Deno child of the bot) |
| detached Go monitor | running (`build/tgbot-monitor`, pidfile present), reports + 80 % warnings flowing. Two warnings today (14:33, 14:41) — CPU spikes from the friend-media cycle on a 2-vCPU box, cleared within a minute |
| `tgbot-xchat-bridge.service` | active; X self-DM polling confirmed in the log |
| `cookie-watch.service` | active (inotifywait tamper monitor) |
| `tgbot-monitor.service` | installed, **disabled** by design (the bot spawns the detached monitor) |
| disk / memory | 38 % of 96 GB; 2.3 GB used of 3.9 GB + 8 GB swap (0.9 GB in use) |
| cookie jars | YouTube/TikTok/X currently rotating (merge/overlay events Oct 16–17); **Instagram jar untouched since 2026-10-08** — the session is dead and the operator paused IG (`IG_AUTH_ENABLED=false`), which is respected everywhere (no IG worker, friend media skips IG, cookie refresher skips instagram.com) |
| cache dir | 176 KB (state files only); the hourly cleaner is protecting `xchat_bridge_state.json` / `xchat_inbox.jsonl` |
| yt-dlp / kurigram | up to date (2026.10.10.233141 / 2.2.26) |

---

## Also changed in this session (doc accuracy)

`README.md` still advertised the **Telegram Mini App / admin WebApp** and the
subscription WebApp at a public URL, which were removed on 2026-08-26
(`modules/admin_webapp/`, `modules/subscription/webapp.py`, `utils/webapp_auth.py`
are deleted; FastAPI now serves only `/stream/{token}/{file_name}`). The two
feature bullets and the admin-console table row were corrected, since a
"deployment script/docs must match the current state" review would otherwise keep
promising a removed feature.

---

## Tests

* New `tests/test_instagram_post.py` — 44 checks: URL routing (`/p/`, `/tv/`,
  username-prefixed, query-string; rejects reels/stories/share/other hosts),
  `ie_key()` shadowing + no-op `raise_no_formats`, `_build_post` for single
  photo / carousel / mixed / video-only / empty, `download_post_photos`
  (original accepted, broken→rendition, HTML payload rejected, oversized→
  rendition, all-unusable→empty, oversized-only→document), non-post URLs refused
  before any yt-dlp call, and the new error classification. Fully offline
  (fetcher monkeypatched).
* `tests/test_downloader_media.py` 7/7, `tests/test_filenames.py` 22/22.
* `python3 -m py_compile` over every tracked module.

## Operator follow-ups

* Two known-level items need **you**, not code:
  1. `IG_AUTH_ENABLED=false` (IG session dead since 2026-10-08) — every
     authenticated Instagram feature (DM relay, friend-media IG, login-walled
     reels/posts) stays off until a fresh `cookies/instagram/igcookies.txt` is
     uploaded and IG Auth is re-enabled. The new photo path needs no session and
     keeps working meanwhile.
  2. `https://www.instagram.com/p/DePboi5Gn2w/` is permanently refused
     anonymously ("empty media response", 3/3 attempts) — deleted, private, or
     anonymous-rate-limited content. It now reports the actionable jar message.
* The Telethon verification left 3 test links + their delivered media in your DM
  with the bot (the delivered media is the proof the fix works); delete them if
  you want a clean chat.

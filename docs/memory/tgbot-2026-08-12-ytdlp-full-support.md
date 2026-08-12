# yt-dlp Full Site Support — 2026-08-12

## Problem

`modules/downloader_handler.py:is_social_media_link()` gated the yt-dlp
format-selection path with a hardcoded `social_domains` list (~25 entries:
youtube, instagram, tiktok, x, soundcloud, vimeo, twitch.tv, facebook, reddit,
bilibili, etc.). Every other URL fell through to the direct-file path — a
plain `aiohttp` GET that saved the exact link as a file. That was correct for
genuine direct files (`…/video.mp4`, `…/archive.zip`) but wrong for media
*page* URLs on any of yt-dlp's other ~1,700 extractors:

- `nicovideo.jp/watch/sm…` (niconico)
- `pornhub.com/view_video.php?viewkey=…`
- `clips.twitch.tv/<clip>` (distinct extractor from `twitch.tv`)
- plus ~1,700 others

Those pages returned raw HTML instead of the real media, and the user never
got a quality keyboard, cookie jar, or the uploader's split logic.

## Solution

Compile **all** yt-dlp extractor `_VALID_URL` patterns once and use them as
the routing predicate. Generic is excluded explicitly (it matches everything,
so a direct `.mp4` link correctly stays on the direct-file path).

New module: `utils/downloader/supported_sites.py`

```python
from utils.downloader.supported_sites import is_ytdlp_supported

# True for ~1,700 page URLs, False for generic/direct files
is_ytdlp_supported("https://www.nicovideo.jp/watch/sm123")  # True
is_ytdlp_supported("https://example.com/video.mp4")          # False
```

`modules/downloader_handler.py:is_social_media_link()` now delegates to
`is_ytdlp_supported` and keeps a hardcoded fallback if `yt_dlp` is missing or
compilation throws — so a broken yt-dlp install cannot break the whole bot.

### Implementation details

- Lazy compile: `_compile_ytdlp_patterns()` builds 1,786 regexes on first call
  (`yt_dlp.extractor.gen_extractor_classes()` excluding `IE_NAME == 'generic'`).
  On the production VPS this costs ~0.6 s / ~2 MB and ~0.01 ms per URL after.
  (The report's "10 ms" was measured on a faster box; 0.6 s was measured here
  on `venv/bin/python` with yt-dlp 2026.08.04.234419 — still negligible.)
- `warm_ytdlp_patterns()` is exported for callers that want to eager-warm
  during boot (not required — first user link warms it).
- `get_ytdlp_extractor_name(url)` helper for logging/diagnosis.
- `utils/downloader/__init__.py` re-exports the three symbols so
  `from utils.downloader import is_ytdlp_supported` works.
- Name `is_social_media_link` is preserved (historical invariant #12 still
  names it); semantics are now "is yt-dlp supported".

### What stays direct-file

- `http://` / `https://` only; `ftp://`, `file://`, plain text → not a link
  at all (unchanged — `is_link` gate).
- Non-http(s) or generic-only URLs → `is_ytdlp_supported` returns False →
  direct-file path with the SSRF guard (`_is_ssrf_target`).
- A genuine CDN file URL that happens to sit on a yt-dlp domain but only the
  generic extractor would match (e.g. `https://cdn.example.org/video.mp4`)
  → False → direct file (correct).

### Cookie jars

No code change needed. `utils/downloader/cookies.py::_resolve_jar_path` already
resolves per-site jars by parsing `netloc` → `cookies/ytdlp/<sitename>.txt`
(stripping `www.`, taking the first label) with `cookies/ytdlp/cookies.txt`
as global fallback. Adding a new site is just dropping a `<site>.txt` jar
through the admin console (Per-Site Jar). See `AGENTS.md` invariant 12.

### Playlist scope

Only YouTube playlists are handled (`is_playlist_url` checks `list=` on
`youtube.com`). Other yt-dlp playlist extractors (e.g. SoundCloud sets) are
not routed as playlists — they go through the single-media format keyboard.
This matches invariant #8 ("playlist vs single-video are two distinct paths"
for YouTube). Widening that to all yt-dlp playlists is a future item; the
single-video path already works for those sites.

## Verification

```bash
venv/bin/python -c "from utils.downloader.supported_sites import is_ytdlp_supported; \
  assert is_ytdlp_supported('https://www.youtube.com/watch?v=dQw4w9WgXcQ'); \
  assert is_ytdlp_supported('https://www.nicovideo.jp/watch/sm123'); \
  assert is_ytdlp_supported('https://www.pornhub.com/view_video.php?viewkey=x'); \
  assert is_ytdlp_supported('https://clips.twitch.tv/abc'); \
  assert not is_ytdlp_supported('https://example.com/video.mp4'); print('ok')"
python3 -m py_compile $(git ls-files '*.py')
bash -n install.sh run.sh uninstall.sh
cd cmd/tgbot-monitor && go test ./...
```

Existing behaviours unchanged:
- YouTube cookies+PO, Instagram no-auth-first, TikTok embed rewrite — all still
  in `extract_formats` / `normalize_url`.
- Direct-file SSRF guard, queue vs concurrent-fetch split, size estimation
  and CDN probes — untouched.
- `example.com/video.mp4` still downloads as a direct file; generic fallback
  on failure is yt-dlp's natural error classification.

## Files changed

- Created `utils/downloader/supported_sites.py`
- Edited `modules/downloader_handler.py` — `is_social_media_link` delegates to yt-dlp
- Edited `utils/downloader/__init__.py` — re-exports
- Docs: `README.md`, `blueprint.md`, `AGENTS.md`, `docs/UBUNTU_VPS_SETUP.md` (where applicable)
- This memory note: `docs/memory/tgbot-2026-08-12-ytdlp-full-support.md`

## Lessons / gotchas retained from the original report

- The typo `niconico.jp` → real domain `nicovideo.jp` (and `nico.ms` shortlinks).
- Generic must be excluded or every URL (including direct files) would be forced
  through yt-dlp.
- Performance at `~0.6 s` on a 1 GB VPS is fine — lazy compile avoids delaying
  startup. Don't move it to import-time work that blocks the event loop.
- yt-dlp upgrades automatically add new sites — no domain list maintenance.

## Future work (not done here)

- Playlist tier handling for non-YouTube playlist extractors (low priority —
  those sites work as single-media today).
- Optional: eager warm in `main.py` after client start (warm in a thread so boot
  stays fast) instead of first-link lazy warm.

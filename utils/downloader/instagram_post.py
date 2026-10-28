"""
Instagram post media (photo posts & carousels) for the interactive downloader.

yt-dlp models Instagram media as VIDEO only.  ``InstagramIE._extract_product_media``
reads ``video_versions`` and ignores ``image_versions2``, so

  * a photo post dies in ``raise_no_formats('There is no video in this post')``, and
  * a carousel that contains ANY photo fails the WHOLE playlist while yt-dlp
    processes it ("[Instagram] <entry-id>: No video formats found!").

``extract_formats`` surfaces both as "The site changed its layout or the URL is
malformed." — so Instagram photo posts were simply unreachable through the bot
(operator report 2026-10-18: "I sent Instagram links and got an error, the posts
were photos").

This module adds the missing half WITHOUT forking the extractor.  Our subclass
is declared as ``InstagramIE`` (name-shadowing) so ``ie_key()`` is
``"Instagram"`` and ``YoutubeDL.add_info_extractor`` REPLACES the built-in
extractor for that key instead of appending a second one.  It only
(a) defuses the no-formats raise and (b) records the post's image candidates as
``ig_photos`` — the logged-out GraphQL fetch, LSD/CSRF handling, embed fallback,
curl_cffi impersonation and cookie handling all stay upstream's code, so
upstream fixes keep reaching us.  (Same spirit as the TikTok embed rewrite: a
thin hedge on top of yt-dlp, not a parallel extractor.)

Video-only posts are deliberately NOT handled here: they keep the normal
yt-dlp format-keyboard flow (quality choice + audio merge).  The caller routes
here only when the enumeration reports at least one photo.
"""

import os
import re
import logging

import requests
import yt_dlp

import config
from utils import cookie_manager

from .cookies import _resolve_jar_path
from .errors import _classify_ytdl_error
from .sizing import _SIZE_PROBE_UA
from .thumbnails import ensure_photo_path, _looks_like_image

# yt-dlp's Instagram extractor, imported under a shadowing alias: the subclass
# below is deliberately named ``InstagramIE`` so ``ie_key()`` returns
# "Instagram" and ``add_info_extractor`` replaces the built-in entry.
from yt_dlp.extractor.instagram import InstagramIE as _UpstreamInstagramIE
from yt_dlp.utils import traverse_obj, url_or_none

logger = logging.getLogger(__name__)

# Photo posts live under /p/<code> (single image or carousel) and /tv/<code>
# (IGTV).  /reel(s)/ is always video, /stories/ expires, /share/ is opaque —
# none of them are touched here.
_INSTAGRAM_POST_RE = re.compile(
    r"^https?://(?:www\.)?instagram\.com/(?:[^/?#]+/)?(?:p|tv)/(?P<code>[^/?#&]+)",
    re.IGNORECASE,
)

# yt-dlp reads the post duration from `video_duration`, but Instagram's
# logged-out GraphQL stopped shipping that field (verified 2026-10-28):
# product_media carries NO `video_duration`, so `info['duration']` was None for
# every IG link and the format keyboard read "⏱ Duration: 0m 0s" (and size
# estimates fell back to a blind 60 s heuristic). The SAME payload still ships
# `video_dash_manifest`, whose MPD root declares the real length:
#   mediaPresentationDuration="PT16.033333S"
# Parsing that is free (no extra request, no second GraphQL client, no doc_id
# to rot) and is the extractor-data source of truth. ISO-8601 durations look
# like P[nD][T[nH][nM][nS]].
_ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$",
    re.IGNORECASE,
)
_MPD_DURATION_RE = re.compile(
    r"""mediaPresentationDuration\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def _dash_duration_seconds(manifest: str | None) -> float | None:
    """Real duration (seconds) from an Instagram DASH manifest, or ``None``.

    Returns ``None`` for a missing/unparseable manifest or a zero duration so
    callers can keep yt-dlp's value untouched. Never raises.
    """
    if not manifest or not isinstance(manifest, str):
        return None
    m = _MPD_DURATION_RE.search(manifest)
    if not m:
        return None
    iso = m.group(1).strip()
    parsed = _ISO_DURATION_RE.match(iso)
    if not parsed:
        return None
    parts = parsed.groupdict()
    if not any(parts.values()):
        return None
    total = (
        float(parts["days"] or 0) * 86400
        + float(parts["hours"] or 0) * 3600
        + float(parts["minutes"] or 0) * 60
        + float(parts["seconds"] or 0)
    )
    return total if total > 0 else None


# Telegram's sendPhoto / photo-in-media-group limit is 10 MB per photo.
_PHOTO_TELEGRAM_MAX = int(9.5 * 1024 * 1024)
# Refuse absurd payloads outright rather than buffering them in RAM.
_PHOTO_HARD_MAX = 48 * 1024 * 1024
# Instagram serves tiny 150x150 crops alongside the original; a real photo
# candidate is never a few hundred bytes.
_PHOTO_MIN_BYTES = 1024


class InstagramIE(_UpstreamInstagramIE):
    """Upstream Instagram extractor, extended with image (photo) media.

    Two deliberate, minimal deviations:

    * ``raise_no_formats`` becomes a no-op.  Upstream raises it for
      "There is no video in this post", which is EXACTLY the case we are here
      for; the caller decides what to do with a format-less post.
    * ``_extract_product_media`` additionally records every ``image_versions2``
      candidate URL (largest first — Instagram orders them biggest→smallest) as
      ``ig_photos``.  Upstream keeps them only as ``thumbnails``, which mixes
      them with the real video covers and makes the original
      indistinguishable from a 150x150 crop.
    """

    def raise_no_formats(self, reason, *, expected=False, video_id=None):
        # Photo posts legitimately carry no video formats.
        self._ig_no_formats_reason = reason

    def _extract_product_media(self, product_media):
        info = super()._extract_product_media(product_media)
        candidates = traverse_obj(product_media, (
            'image_versions2', 'candidates', ...,
            {'url': ('url', {url_or_none})},
        ))
        info['ig_photos'] = [c['url'] for c in (candidates or []) if c.get('url')]
        # Instagram stopped returning `video_duration`; the DASH manifest still
        # carries the real length. Only fill a MISSING duration — a value the
        # extractor (or a future yt-dlp) did report stays authoritative.
        if not info.get('duration'):
            duration = _dash_duration_seconds(product_media.get('video_dash_manifest'))
            if duration is not None:
                info['duration'] = duration
        return info


def register_instagram_extractor(ydl) -> None:
    """Use our ``InstagramIE`` subclass on *ydl* (replaces yt-dlp's built-in).

    Every yt-dlp entry point that may extract an Instagram URL must call this:
    the class name keeps ``ie_key() == "Instagram"``, so
    ``add_info_extractor`` REPLACES the built-in extractor rather than
    appending a second one that never matches. Registration is what makes the
    duration repair (and ``ig_photos`` for photo posts) reach the regular
    format/download flows, not just :func:`fetch_instagram_post`.
    """
    ydl.add_info_extractor(InstagramIE())


# ---------------------------------------------------------------------------
# Post enumeration
# ---------------------------------------------------------------------------

def is_instagram_post_url(url: str) -> bool:
    """True for an Instagram /p/ or /tv/ URL (photo post or carousel).

    Reels, stories, highlights and share-links are excluded: reels are videos
    (the normal format flow handles them), stories/highlights expire and are
    handled by the direct-forward worker with an authenticated session.
    """
    return bool(_INSTAGRAM_POST_RE.match((url or "").strip()))


def canonical_post_url(url: str) -> str:
    """Strip Instagram's tracking query string (``?img_index=4&obrf=…``)."""
    m = _INSTAGRAM_POST_RE.match((url or "").strip())
    if not m:
        return url
    return f"https://www.instagram.com/p/{m.group('code')}/"


def _extract_info(url: str, cookie_path: str | None = None) -> dict:
    opts = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'proxy': getattr(config, "YTDLP_PROXY", None),
    }
    user_agent = getattr(config, "YTDLP_USER_AGENT", "")
    if user_agent:
        opts['user_agent'] = user_agent
    if cookie_path:
        opts['cookiefile'] = cookie_path
    with yt_dlp.YoutubeDL(opts) as ydl:
        register_instagram_extractor(ydl)
        # process=False keeps the RAW extractor result: format selection is
        # meaningless for a post with no video stream and would fail the whole
        # playlist ("Requested format is not available").
        return ydl.extract_info(url, download=False, process=False)


def _entries(info: dict) -> list[dict]:
    """Carousel → its entries; single media → a one-element list."""
    if isinstance(info, dict) and info.get("_type") == "playlist":
        return [e for e in (info.get("entries") or []) if isinstance(e, dict)]
    return [info] if isinstance(info, dict) else []


def _build_post(info: dict, url: str) -> dict:
    items: list[dict] = []
    for entry in _entries(info):
        has_video = any(f.get("url") for f in (entry.get("formats") or []))
        photos = [u for u in (entry.get("ig_photos") or []) if u]
        if has_video:
            code = entry.get("id")
            items.append({
                "kind": "video",
                "permalink": f"https://www.instagram.com/p/{code}/" if code else None,
            })
        elif photos:
            items.append({
                "kind": "photo",
                # candidates[0] is the original (Instagram orders big→small);
                # the rest are progressively smaller re-encodes used as
                # fallbacks when the original is missing or too big to send.
                "url": photos[0],
                "fallbacks": photos[1:],
            })
        else:
            logger.info("[ig_post] %s: entry %s has neither video nor image media — skipped",
                        url, entry.get("id"))

    return {
        "url": url,
        "title": (info.get("title") or "").strip() or "Instagram post",
        "caption": (info.get("description") or "").strip(),
        "author": (info.get("channel") or info.get("uploader") or "").strip(),
        "items": items,
        "n_photo": sum(1 for i in items if i["kind"] == "photo"),
        "n_video": sum(1 for i in items if i["kind"] == "video"),
        "has_photo": any(i["kind"] == "photo" for i in items),
    }


def fetch_instagram_post(url: str) -> dict:
    """Enumerate one Instagram /p|tv/ post (photos + videos).

    Returns ``{"url", "title", "caption", "author", "items", "has_photo",
    "n_photo", "n_video"}``; ``items`` preserves the post's order and holds
    ``{"kind": "photo", "url", "fallbacks"}`` / ``{"kind": "video",
    "permalink"}`` entries.

    Auth ladder mirrors :func:`extract_formats`: no-auth FIRST (public posts
    resolve anonymously and burning the session shortens its life), cookies as
    the fallback for login-walled posts.  Cookie bookkeeping goes through
    ``cookie_manager`` so a successful authenticated enumeration still merges
    the rotated session back into the jar.

    Raises only when Instagram could not be read at all — a video-only post is
    a normal, successful result (``has_photo == False``) that simply falls
    through to the yt-dlp format flow.  A non-post URL (reel/story/share) is
    refused up front with ``ValueError``.
    """
    if not is_instagram_post_url(url):
        raise ValueError(f"Not an Instagram /p|tv/ post URL: {url}")
    url = canonical_post_url(url)
    cookie_path = _resolve_jar_path(url)
    if cookie_path and not cookie_manager.has_real_cookie_lines(cookie_path):
        cookie_path = None

    attempts: list[str | None] = [None]
    if cookie_path:
        attempts.append(cookie_path)

    info = None
    last_error: Exception | None = None
    for jar in attempts:
        snap_path = cookie_manager.acquire(jar) if jar else None
        try:
            info = _extract_info(url, snap_path)
        except Exception as e:
            info = None
            last_error = e
        if info is not None:
            if snap_path:
                cookie_manager.commit(snap_path, success=True)
            break
        if snap_path:
            cookie_manager.commit(
                snap_path, success=False,
                error_text=str(last_error) if last_error else None,
            )

    if info is None:
        raise RuntimeError(
            f"Instagram post read failed: {_classify_ytdl_error(last_error, url)}")

    post = _build_post(info, url)
    logger.info("[ig_post] %s: %d photo(s), %d video(s), author=%s",
                url, post["n_photo"], post["n_video"], post["author"] or "?")
    return post


# ---------------------------------------------------------------------------
# Photo download
# ---------------------------------------------------------------------------

def _fetch_bytes(url: str) -> bytes:
    """Fetch one Instagram CDN resource (bounded size, HTML payloads rejected).

    Instagram's CDN URLs are signed and need no session cookie; they DO need
    the instagram.com referer.  Mirrors the direct-forward fetcher — a plain
    ``requests`` call with the project's user agent.
    """
    headers = {
        "User-Agent": getattr(config, "YTDLP_USER_AGENT", "") or _SIZE_PROBE_UA,
        "Referer": "https://www.instagram.com/",
    }
    proxy = getattr(config, "YTDLP_PROXY", None)
    proxies = {"http": proxy, "https": proxy} if proxy else None
    resp = requests.get(url, headers=headers, timeout=60, proxies=proxies, stream=True)
    try:
        resp.raise_for_status()
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content(64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > _PHOTO_HARD_MAX:
                raise RuntimeError(f"image exceeds {_PHOTO_HARD_MAX} bytes")
            chunks.append(chunk)
    finally:
        resp.close()
    return b"".join(chunks)


def download_post_photos(photo_items: list[dict], task_dir: str) -> list[str]:
    """Download the photo items of a post into *task_dir*.

    Returns Telegram-ready local paths.  The original candidate is preferred;
    when it cannot be fetched, is not an image (CDN interstitials), or is too
    big for ``sendPhoto`` (10 MB), the smaller re-encodes Instagram ships in the
    same ``image_versions2`` list are used instead.  Every survivor is passed
    through :func:`ensure_photo_path` so WebP/GIF/BMP payloads are transposed to
    JPEG (Telegram answers ``[400 PHOTO_EXT_INVALID]`` otherwise).

    Blocking (network + disk): call it through ``run_in_executor``.
    """
    os.makedirs(task_dir, exist_ok=True)
    out: list[str] = []
    for idx, item in enumerate(photo_items):
        candidates = [item.get("url")] + list(item.get("fallbacks") or [])[:3]
        oversized: str | None = None
        for cand in (c for c in candidates if c):
            # Neutral extension on purpose: ensure_photo_path() only inspects
            # the MAGIC when the current extension is not a photo extension, so
            # a WebP payload mislabelled ".jpg" would be sent as-is and rejected
            # by Telegram with [400 PHOTO_EXT_INVALID].
            raw_path = os.path.join(task_dir, f"ig_photo_{idx}.bin")
            try:
                data = _fetch_bytes(cand)
            except Exception as e:
                logger.warning("[ig_post] photo %d candidate fetch failed (%s): %s",
                               idx, str(cand)[:80], e)
                continue
            if len(data) < _PHOTO_MIN_BYTES:
                logger.warning("[ig_post] photo %d candidate is %d bytes — skipped",
                               idx, len(data))
                continue
            try:
                with open(raw_path, "wb") as f:
                    f.write(data)
            except OSError as e:
                logger.warning("[ig_post] photo %d write failed: %s", idx, e)
                continue
            if not _looks_like_image(raw_path):
                logger.warning("[ig_post] photo %d candidate is not an image "
                               "(magic bytes) — skipped", idx)
                os.remove(raw_path)
                continue
            if os.path.getsize(raw_path) > _PHOTO_TELEGRAM_MAX:
                # Keep it as the document-mode last resort, but prefer a
                # smaller re-encode Instagram already gave us.
                if oversized is None:
                    oversized = raw_path
                continue
            ready = ensure_photo_path(raw_path)
            if not ready:
                logger.warning("[ig_post] photo %d could not be converted to a "
                               "Telegram-acceptable image — skipped", idx)
                continue
            out.append(ready)
            break
        else:
            if oversized:
                logger.info("[ig_post] photo %d: only oversized (>%.1fMB) "
                            "renditions — delivering as a document",
                            idx, _PHOTO_TELEGRAM_MAX / 1024 / 1024)
                out.append(oversized)
    return out

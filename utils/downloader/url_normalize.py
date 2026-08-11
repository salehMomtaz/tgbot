"""
URL normalization and TikTok shortlink pre-resolution.

Mirrors the original utils/downloader.py functions exactly.
"""

import re
import time
import requests
import config


def _is_youtube(url: str) -> bool:
    """Return True if *url* is a YouTube video."""
    if not url:
        return False
    lower = url.lower()
    return "youtube.com" in lower or "youtu.be" in lower


def normalize_url(url: str) -> str:
    """Canonicalize URLs that yt-dlp does not understand natively.

    Currently:
    * Instagram highlight-share links in the base64 token form
      ``/s/<base64("highlight:"+id)>?story_media_id=...`` (what the IG app
      produces when you copy/share a highlight) → the supported
      ``/stories/highlights/<id>/`` form.
    * TikTok share shortlinks ``vt./vm./vn.tiktok.com/<code>`` → the canonical
      ``www.tiktok.com/@user/video/<id>`` URL (see :func:`_resolve_tiktok_short_url`).
    """
    m = re.match(r"^https?://(?:www\.)?instagram\.com/s/([A-Za-z0-9\-_+/=]+)", url or "")
    if m:
        try:
            import base64
            token = m.group(1)
            decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("utf-8", "replace")
            hm = re.match(r"highlight:(\d+)", decoded)
            if hm:
                return f"https://www.instagram.com/stories/highlights/{hm.group(1)}/"
        except Exception:
            pass
    return _resolve_tiktok_short_url(url)


# ---------------------------------------------------------------------------
# TikTok shortlink pre-resolution
# ---------------------------------------------------------------------------
# yt-dlp's own short-link extractor resolves vt./vm./vn.tiktok.com with a bare
# facebookexternalhit HEAD request, no impersonation, no cookies. When TikTok
# answers with its anti-bot interstitial instead of a plain 301 (stochastic,
# IP/fingerprint based), the whole extraction dies as "Unable to extract ..."
# and the user sees "The site changed its layout or the URL is malformed.".
# Resolving the redirect ourselves with a real browser UA (one retry, results
# cached for an hour) is far more consistent, and yt-dlp then receives a
# canonical www.tiktok.com URL it handles natively — challenge solver included.
_TIKTOK_SHORT_RE = re.compile(r"^https?://(?:vt|vm|vn)\.tiktok\.com/", re.IGNORECASE)
_TIKTOK_RESOLVE_TTL = 3600
_tiktok_resolve_cache: dict[str, tuple[str, float]] = {}


def _resolve_tiktok_short_url(url: str) -> str:
    """Expand a TikTok share shortlink to its canonical URL when possible.

    On any failure the original short URL is returned unchanged, so yt-dlp
    still gets its own chance through the built-in short-link extractor.
    Both ``extract_formats`` and ``download_media`` normalize the same URL —
    the TTL cache keeps that from doubling the network round-trips.
    """
    if not url or not _TIKTOK_SHORT_RE.match(url):
        return url

    now = time.time()
    cached = _tiktok_resolve_cache.get(url)
    if cached and cached[1] > now:
        return cached[0]

    headers = {"User-Agent": getattr(config, "YTDLP_USER_AGENT", "") or _SIZE_PROBE_UA}
    proxies = None
    proxy = getattr(config, "YTDLP_PROXY", None)
    if proxy:
        proxies = {"http": proxy, "https": proxy}

    resolved = url
    for _attempt in range(2):
        try:
            # stream=True + close(): we only need the redirect chain, not the body.
            resp = requests.get(url, headers=headers, timeout=10,
                                allow_redirects=True, proxies=proxies, stream=True)
            final = resp.url
            resp.close()
            # Accept only a genuine content hop (/@user/video|photo|t/...); a
            # login/interstitial hop means TikTok is playing games — keep the
            # short form and let yt-dlp's challenge solver deal with it.
            if (final and not _TIKTOK_SHORT_RE.match(final)
                    and "tiktok.com/@" in final):
                resolved = final
        except Exception:
            continue
        break

    if len(_tiktok_resolve_cache) > 500:
        _tiktok_resolve_cache.clear()
    _tiktok_resolve_cache[url] = (resolved, now + _TIKTOK_RESOLVE_TTL)
    return resolved


# Import _SIZE_PROBE_UA from sizing to avoid circular import
from .sizing import _SIZE_PROBE_UA


def _apply_pot_options(ydl_opts: dict, url: str) -> dict:
    """Inject bgutil HTTP PO-token provider options for YouTube.

    YouTube uses cookies + PO token only — there is no cookies-only or no-auth
    fallback. If the provider is not running, this raises an actionable error so
    the caller surfaces it instead of silently degrading. Non-YouTube URLs are
    returned unchanged (they never use PO tokens).
    """
    import utils.shared as shared
    if not _is_youtube(url):
        return ydl_opts

    if not getattr(shared, "POT_AVAILABLE", False):
        raise RuntimeError(
            "YouTube downloads require the PO-token provider, which is not running. "
            "Restart the bot with ./run.sh, or start it from Admin Console -> PO Token."
        )

    opts = dict(ydl_opts)
    opts.setdefault("extractor_args", {})
    opts["extractor_args"]["youtubepot-bgutilhttp"] = {
        "base_url": [f"http://127.0.0.1:{config.YTDLP_POT_PORT}"]
    }
    player_client = getattr(config, "YTDLP_POT_PLAYER_CLIENT", "mweb") or "mweb"
    opts["extractor_args"]["youtube"] = {
        "player_client": [player_client]
    }
    # yt-dlp solves YouTube's BotGuard/ nsig challenges via this JS runtime.
    # We install Deno for the provider, so use it for yt-dlp's JS too. (yt-dlp
    # already defaults to {'deno': {}}, but set it explicitly for robustness.)
    opts["js_runtimes"] = {"deno": {}}
    return opts
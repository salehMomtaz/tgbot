"""
yt-dlp supported-sites detection — pre-compiled at startup.

Replaces the hardcoded ``social_domains`` allowlist that previously gated the
format-selection path to ~25 domains. Every other URL fell through to the
direct-file (plain HTTP GET) path, so media pages on any of yt-dlp's other
1,700+ extractors (niconico, pornhub, clips.twitch.tv, etc.) returned raw HTML
instead of the real media.

This module compiles all yt-dlp extractor ``_VALID_URL`` patterns once (generic
excluded — it matches everything) and exposes a single predicate:

    is_ytdlp_supported(url) -> bool

Performance: ~0.6 s one-time compile on a 1 GB VPS (1,786 patterns), ~0.01 ms
per URL thereafter. Memory ~2 MB. The compile is lazy (first call) so bot
startup is not blocked; callers that need a warm cache can call
``warm_ytdlp_patterns()`` explicitly during boot.

Fallback: if yt_dlp is missing or compilation fails, the predicate returns
False and the caller should fall back to the direct-file path.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

_YTDLP_PATTERNS: list[tuple[str, re.Pattern]] | None = None
_COMPILE_ERROR: str | None = None


def _compile_ytdlp_patterns() -> list[tuple[str, re.Pattern]]:
    global _YTDLP_PATTERNS, _COMPILE_ERROR
    if _YTDLP_PATTERNS is not None:
        return _YTDLP_PATTERNS
    if _COMPILE_ERROR is not None:
        return []
    patterns: list[tuple[str, re.Pattern]] = []
    try:
        import yt_dlp.extractor  # local import — yt_dlp may be absent in tests
        for ie_cls in yt_dlp.extractor.gen_extractor_classes():
            if getattr(ie_cls, "IE_NAME", None) == "generic":
                continue
            valid_url = getattr(ie_cls, "_VALID_URL", None)
            if not valid_url:
                continue
            if isinstance(valid_url, str):
                try:
                    patterns.append((ie_cls.IE_NAME, re.compile(valid_url)))
                except re.error as exc:
                    log.debug("Skipping bad _VALID_URL %s: %s (%s)", ie_cls.IE_NAME, valid_url[:120], exc)
            elif isinstance(valid_url, (list, tuple)):
                for p in valid_url:
                    if isinstance(p, str):
                        try:
                            patterns.append((ie_cls.IE_NAME, re.compile(p)))
                        except re.error as exc:
                            log.debug("Skipping bad _VALID_URL %s: %s (%s)", ie_cls.IE_NAME, str(p)[:120], exc)
                    elif hasattr(p, "search"):
                        # Already compiled pattern object
                        patterns.append((ie_cls.IE_NAME, p))
            elif hasattr(valid_url, "search"):
                patterns.append((ie_cls.IE_NAME, valid_url))
    except Exception as exc:
        _COMPILE_ERROR = str(exc)
        log.warning("Failed to compile yt-dlp patterns: %s — is_ytdlp_supported will return False", exc)
        return []
    _YTDLP_PATTERNS = patterns
    log.info("Compiled %d yt-dlp extractor patterns (generic excluded)", len(patterns))
    return patterns


def warm_ytdlp_patterns() -> int:
    """Eagerly compile patterns; return count. No-op on second call."""
    return len(_compile_ytdlp_patterns())


def is_ytdlp_supported(url: str) -> bool:
    """Return True if *url* matches any yt-dlp extractor (generic excluded).

    A direct ``.mp4`` / ``.mp3`` / ``.zip`` link that happens to live on a
    yt-dlp domain (e.g. ``cdn.example.com/video.mp4`` on a generic host)
    should NOT be forced through yt-dlp when yt-dlp would only use the generic
    extractor — generic is excluded here, so those files correctly stay on the
    direct-file path. A page URL like ``nicovideo.jp/watch/sm123`` correctly
    returns True.
    """
    if not url or not isinstance(url, str):
        return False
    # Fast reject: must look like an http(s) URL; plain text / ftp / file should
    # never reach yt-dlp.
    stripped = url.strip()
    if not (stripped.startswith("http://") or stripped.startswith("https://")):
        return False
    for _, pattern in _compile_ytdlp_patterns():
        try:
            if pattern.search(url):
                return True
        except Exception:
            continue
    return False


def get_ytdlp_extractor_name(url: str) -> str | None:
    """Return the first matching extractor IE_NAME, or None."""
    if not url:
        return None
    for name, pattern in _compile_ytdlp_patterns():
        try:
            if pattern.search(url):
                return name
        except Exception:
            continue
    return None

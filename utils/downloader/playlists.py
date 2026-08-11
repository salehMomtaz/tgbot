"""
Playlist detection and metadata extraction.

Mirrors the original utils/downloader.py playlist functions exactly.
"""

import urllib.parse
import yt_dlp
import config
from .cookies import get_cookies_for_url
from .url_normalize import _is_youtube
from utils import cookie_manager


# Playlist quality tiers.
#
# Per-video format_ids differ across a playlist, so we use robust yt-dlp
# *selectors* (not fixed ids). Every selector ends with a /best fallback, so a
# tier never hard-fails when a particular video lacks that height/abr stream.
# Audio keeps the FFmpegExtractAudio postprocessor with preferredquality '0'
# (source VBR, no re-encode) — the abr<= selector does the tiering.
PLAYLIST_TIERS = {
    # (format_type, tier) -> (yt-dlp format selector, human-readable label)
    ("v", "high"):   ("bestvideo[height<=1080]+bestaudio/best[height<=1080]/best", "1080p"),
    ("v", "medium"): ("bestvideo[height<=720]+bestaudio/best[height<=720]/best",   "720p"),
    ("v", "low"):    ("bestvideo[height<=480]+bestaudio/best[height<=480]/best",   "480p"),
    ("a", "high"):   ("bestaudio/best",                                            "best"),
    ("a", "medium"): ("bestaudio[abr<=160]/bestaudio/best",                        "<=160k"),
    ("a", "low"):    ("bestaudio[abr<=70]/bestaudio/best",                         "<=70k"),
}


def is_playlist_url(url: str) -> bool:
    """True if *url* is a YouTube link carrying a playlist (``list=...``)."""
    lower = (url or "").lower()
    if not _is_youtube(lower):
        return False
    parsed = urllib.parse.urlparse(lower)
    query = urllib.parse.parse_qs(parsed.query)
    return "list" in query


def is_pure_playlist_url(url: str) -> bool:
    """True for ``/playlist?list=...`` (not a single ``watch?v=`` URL)."""
    lower = (url or "").lower()
    if not _is_youtube(lower):
        return False
    return urllib.parse.urlparse(lower).path.startswith("/playlist")


def extract_playlist_meta(url: str) -> dict:
    """Flat-extract a YouTube playlist: title + entries (url, title, duration).

    Uses ``extract_flat`` so the entries are listed WITHOUT resolving each
    video's formats — fast and light on PO tokens. Per-video extraction happens
    later inside :func:`download_media`. PO tokens are not applied here on
    purpose: browsing a playlist page does not require them, and skipping the
    provider keeps this metadata pass resilient even if the provider is briefly
    down. Cookies are still passed for age/member-restricted playlists.
    """
    cookie_path = get_cookies_for_url(url)

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
        'skip_download': True,
        'noplaylist': False,
        'proxy': getattr(config, "YTDLP_PROXY", None),
    }
    if cookie_path:
        ydl_opts['cookiefile'] = cookie_path

    user_agent = getattr(config, "YTDLP_USER_AGENT", "")
    if user_agent:
        ydl_opts['user_agent'] = user_agent

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except Exception as e:
            cookie_manager.commit(cookie_path, success=False, error_text=str(e))
            raise
    cookie_manager.commit(cookie_path, success=True)

    if not info or info.get('_type') != 'playlist':
        raise RuntimeError("This link is not a playlist, or YouTube returned no playlist data.")

    entries = []
    for e in (info.get('entries') or []):
        if not e:
            continue
        entry_url = e.get('url') or e.get('id')
        if not entry_url:
            continue
        # Flat YouTube entries often give only the video id; build a watch URL.
        if not str(entry_url).startswith("http"):
            entry_url = f"https://www.youtube.com/watch?v={entry_url}"
        entries.append({
            'url': entry_url,
            'title': e.get('title') or 'Untitled',
            'duration': e.get('duration') or 0,
        })

    if not entries:
        raise RuntimeError("YouTube returned an empty playlist (no playable videos).")

    return {
        'title': info.get('title') or 'YouTube Playlist',
        'entries': entries,
    }
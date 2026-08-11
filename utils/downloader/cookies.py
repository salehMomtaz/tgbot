"""
Cookie resolution and management helpers.

Mirrors the original utils/downloader.py cookie functions exactly.
"""

import os
import urllib.parse
import config
import utils.shared as shared
from utils import cookie_manager
from .url_normalize import _apply_pot_options


def _purge_cookie_snapshots(original_path: str | None = None) -> None:
    """Remove disposable yt-dlp snapshot copies so the next download gets a
    fresh jar. Call after any admin action that modifies a cookie jar."""
    cookie_manager.purge_snapshots(original_path)


def _resolve_jar_path(url: str) -> str | None:
    """Map *url* to its real on-disk cookie jar path (no snapshot)."""
    url_lower = url.lower()
    cookie_path = None
    if "youtube.com" in url_lower or "youtu.be" in url_lower:
        cookie_path = config.YT_COOKIES
    elif "instagram.com" in url_lower:
        cookie_path = config.IG_COOKIES
    elif "tiktok.com" in url_lower:
        cookie_path = config.TT_COOKIES
    elif "twitter.com" in url_lower or "x.com" in url_lower:
        cookie_path = config.X_COOKIES
    else:
        # Check for site-specific cookies in cookies/ytdlp/<sitename>.txt
        try:
            parsed = urllib.parse.urlparse(url)
            domain = parsed.netloc.lower()
            if domain.startswith("www."):
                domain = domain[4:]
            site_name = domain.split(".")[0]
            site_cookie_file = os.path.join(getattr(config, "YTDLP_COOKIES_DIR", "cookies/ytdlp"), f"{site_name}.txt")
            if os.path.exists(site_cookie_file) and os.path.getsize(site_cookie_file) > 0:
                cookie_path = site_cookie_file
            else:
                cookie_path = config.COOKIES_FILE
        except Exception:
            cookie_path = config.COOKIES_FILE
    return cookie_path


def get_cookies_for_url(url: str) -> str | None:
    """Return a per-run snapshot of the correct cookie jar for *url*, or None.

    The snapshot belongs to ONE yt-dlp run; the caller must end the run with
    ``cookie_manager.commit(snapshot, success=...)`` so rotated session cookies
    are merged back into the real jar (keeps Instagram/Google sessions alive)
    and the snapshot is deleted."""
    return cookie_manager.acquire(_resolve_jar_path(url))


def diagnose_youtube_access(test_url: str = "https://www.youtube.com/watch?v=jSi2LDkyKmI") -> dict:
    """
    Run three lightweight extraction probes and report how many real formats
    YouTube returns in each scenario.  This helps decide whether PO tokens are
    needed for the current IP/cookie combination.
    """

    def count_real_formats(info: dict | None) -> int:
        if not info:
            return 0
        return len([
            f for f in info.get("formats", [])
            if f.get("format_note") != "storyboard" and f.get("ext") != "mhtml"
        ])

    def extract(opts: dict) -> dict | None:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(test_url, download=False)
        except Exception:
            return None

    import yt_dlp
    base_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "all",
        "proxy": getattr(config, "YTDLP_PROXY", None),
    }
    user_agent = getattr(config, "YTDLP_USER_AGENT", "")
    if user_agent:
        base_opts["user_agent"] = user_agent

    # 1) No auth at all
    no_auth = extract(dict(base_opts))

    # 2) Cookies only
    cookie_path = get_cookies_for_url(test_url)
    cookie_opts = dict(base_opts)
    if cookie_path:
        cookie_opts["cookiefile"] = cookie_path
    cookies_only = extract(cookie_opts)
    if cookie_path:
        cookie_manager.commit(cookie_path, success=bool(cookies_only))

    # 3) Full stack: cookies + PO token + mweb (only if the provider is running).
    #    _apply_pot_options raises when the provider is down, so guard it.
    full_stack = None
    pot_available = getattr(shared, "POT_AVAILABLE", False)
    original_pot = shared.is_pot_enabled()
    shared.set_pot_enabled(True)
    try:
        if pot_available:
            full_opts = _apply_pot_options(dict(base_opts), test_url)
            snap2 = get_cookies_for_url(test_url)
            if snap2:
                full_opts["cookiefile"] = snap2
            full_stack = extract(full_opts)
            if snap2:
                cookie_manager.commit(snap2, success=bool(full_stack))
    finally:
        shared.set_pot_enabled(original_pot)

    no_auth_count = count_real_formats(no_auth)
    cookies_count = count_real_formats(cookies_only)
    full_count = count_real_formats(full_stack)

    if not pot_available:
        recommendation = (
            "PO-token provider is NOT running, so YouTube downloads will fail. "
            "Start it from Admin Console -> PO Token (or restart with ./run.sh)."
        )
    elif full_count > 0:
        recommendation = "PO-token provider is running and YouTube access works."
    elif cookies_count > 0:
        recommendation = (
            "Cookies alone return formats, but the provider's full stack did not. "
            "Check the PO-token provider logs (search [POT] in logs/bot.log)."
        )
    else:
        recommendation = (
            "YouTube is blocking this IP even with cookies + PO tokens. "
            "Try warmer cookies, a different IP, or a proxy."
        )

    return {
        "no_auth_count": no_auth_count,
        "cookies_count": cookies_count,
        "full_count": full_count,
        "recommendation": recommendation,
    }


def _site_cookie_context(url: str) -> tuple[str, str]:
    """Map a URL to (site_name, cookie_filename) for user-facing messages.

    Mirrors get_cookies_for_url's mapping so an error always names the jar the
    bot actually tried to use for that site."""
    lower = (url or "").lower()
    if "youtube.com" in lower or "youtu.be" in lower:
        return ("YouTube/Google", config.YT_COOKIES)
    if "instagram.com" in lower:
        return ("Instagram", config.IG_COOKIES)
    if "tiktok.com" in lower:
        return ("TikTok", config.TT_COOKIES)
    if "twitter.com" in lower or "x.com" in lower:
        return ("X", config.X_COOKIES)
    # Per-site jar lookup (cookies/ytdlp/<site>.txt)
    try:
        parsed = urllib.parse.urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        site_name = domain.split(".")[0]
        site_cookie_file = os.path.join(getattr(config, "YTDLP_COOKIES_DIR", "cookies/ytdlp"), f"{site_name}.txt")
        if os.path.exists(site_cookie_file) and os.path.getsize(site_cookie_file) > 0:
            return (site_name, site_cookie_file)
    except Exception:
        pass
    return ("the host site", config.COOKIES_FILE)
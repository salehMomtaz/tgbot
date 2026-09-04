"""
Error classification helpers for yt-dlp failures.

Mirrors the original utils/downloader.py error functions exactly.
"""

from .cookies import _site_cookie_context


def _is_sign_in_error(error_text: str) -> bool:
    """Detect bot/sign-in challenges from yt-dlp error text (any site)."""
    text = error_text.lower()
    markers = [
        "sign in to confirm",
        "confirm you're not a bot",
        "confirm you’re not a bot",
        "sign in to continue",
        "please sign in",
        "authentication required",
        "cookies-from-browser",
        "use --cookies",
    ]
    return any(m in text for m in markers)


def _classify_ytdl_error(exc: Exception, url: str) -> str:
    """Return a human-readable explanation for common yt-dlp failures."""
    text = str(exc).lower()

    if _is_sign_in_error(text):
        site, jar = _site_cookie_context(url)
        return (
            f"{site} is requiring sign-in / is bot-challenging this server. "
            f"Please send a fresh `{jar}` jar via Admin Console → Cookies."
        )

    # Instagram's audience gate (follower/age-restricted reels served to
    # anonymous fetches). The download ladder matches these strings for the
    # cookie retry, but a terminal failure on the same text used to fall
    # through to a raw yt-dlp dump — name the jar like every other auth wall.
    if "available to everyone" in text or "certain audiences" in text:
        site, jar = _site_cookie_context(url)
        return (
            f"{site} refused this fetch as a login-walled / audience-restricted "
            f"post (cookies didn't satisfy it). Please send a fresh `{jar}` jar "
            "from a logged-in browser that can actually view this post, via "
            "Admin Console → Cookies."
        )

    if "unexpected response from webpage request" in text and "tiktok.com" in url.lower():
        return (
            "TikTok's anti-bot challenge has changed and yt-dlp cannot solve it (known issue: "
            "https://github.com/yt-dlp/yt-dlp/issues/17403). This is a temporary upstream problem. "
            "Try again later, or use the interactive download (send link to bot) which may work better."
        )

    if "no video formats found" in text or "requested format" in text:
        return (
            "The video has no playable formats available. "
            "This usually happens for ended live streams, members-only videos, or region-blocked content."
        )

    if "unable to extract" in text or "failed to parse" in text:
        return "The site changed its layout or the URL is malformed."

    if "timed out" in text or "timeout" in text:
        return "The download timed out. The server may be slow or the file very large."

    if "network" in text or "connection" in text or "unreachable" in text:
        return "A network error occurred while contacting the video host."

    return str(exc)


def _is_live_or_storyboard_only(info: dict) -> bool:
    """Return True if the only formats are storyboards/previews."""
    formats = info.get("formats", [])
    if not formats:
        return True
    non_storyboard = [
        f for f in formats
        if f.get("format_note") != "storyboard" and f.get("ext") != "mhtml"
    ]
    return len(non_storyboard) == 0


def _storyboard_error(cookie_path: str | None) -> RuntimeError:
    """Build a clear error when yt-dlp returns only storyboards/previews."""
    if cookie_path:
        return RuntimeError(
            "YouTube accepted the cookies but only returned preview/storyboard formats. "
            "This means the cookie jar is bot-flagged, expired, or from an account that cannot watch videos. "
            "Please upload a fresh `ytcookies.txt` from a browser where you can actually play YouTube videos."
        )
    return RuntimeError(
        "YouTube is requiring sign-in from this server and no valid cookies were found. "
        "Please upload a `ytcookies.txt` jar via Admin Console → Cookies."
    )
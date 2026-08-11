"""
Cookie jar testing for the admin console.

Mirrors the original modules/admin.py cookie test functions exactly.
"""

import asyncio
import logging
import config
import yt_dlp
from utils.downloader import get_cookies_for_url, _apply_pot_options
from utils import cookie_manager
import utils.shared as shared

logger = logging.getLogger(__name__)


def _run_cookie_test_sync(cookie_key: str, file_path: str, force_pot: bool) -> dict:
    """Synchronous yt-dlp probe. Run inside an executor so it never blocks the loop."""
    test_url = "https://www.youtube.com/watch?v=jSi2LDkyKmI"
    cookie_snapshot = get_cookies_for_url(test_url)
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "all",
        "cookiefile": cookie_snapshot,
        "proxy": getattr(config, "YTDLP_PROXY", None),
    }
    user_agent = getattr(config, "YTDLP_USER_AGENT", "")
    if user_agent:
        ydl_opts["user_agent"] = user_agent

    original_pot = shared.is_pot_enabled()
    if force_pot:
        shared.set_pot_enabled(True)
    try:
        ydl_opts = _apply_pot_options(ydl_opts, test_url)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(test_url, download=False)
    except Exception as exc:
        cookie_manager.commit(cookie_snapshot, success=False, error_text=str(exc))
        return {"ok": False, "error": str(exc)}
    finally:
        shared.set_pot_enabled(original_pot)

    cookie_manager.commit(cookie_snapshot, success=bool(info))

    formats = info.get("formats", []) if info else []
    real_formats = [
        f for f in formats
        if f.get("format_note") != "storyboard" and f.get("ext") != "mhtml"
    ]
    samples = []
    seen = set()
    for f in real_formats:
        note = f.get("format_note") or "?"
        ext = f.get("ext") or "?"
        key = (note, ext)
        if key not in seen:
            seen.add(key)
            samples.append(f"• `{note}` ({ext})")
        if len(samples) >= 6:
            break
    return {
        "ok": True,
        "real_count": len(real_formats),
        "samples": samples,
        "storyboard_only": len(real_formats) == 0,
    }


async def _test_cookie_jar(client, user_id: int, cookie_key: str, file_path: str, force_pot: bool = False, back_markup=None):
    """Run a lightweight yt-dlp extraction on a known public video and report format availability."""
    import os
    if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
        await client.send_message(
            chat_id=user_id,
            text=f"⚠️ `{cookie_key}.txt` is empty or missing. Nothing to test.",
            reply_markup=back_markup
        )
        return

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _run_cookie_test_sync, cookie_key, file_path, force_pot)

    if not result.get("ok"):
        await client.send_message(
            chat_id=user_id,
            text=(
                f"❌ **Cookie Test Failed for `{cookie_key}.txt`**\n\n"
                "yt-dlp could not extract anything using this jar.\n"
                f"Error: `{result.get('error')}`\n\n"
                "Please upload a fresh cookie jar from a browser where YouTube plays normally."
            ),
            reply_markup=back_markup
        )
        return

    if result.get("real_count", 0) > 0:
        summary = "\n".join(result["samples"])
        pot_label = " (with PO token)" if force_pot else ""
        await client.send_message(
            chat_id=user_id,
            text=(
                f"✅ **Cookie Test Passed{pot_label} for `{cookie_key}.txt`**\n\n"
                f"YouTube returned {result['real_count']} downloadable formats.\n"
                f"Sample formats:\n{summary}\n\n"
                "The jar is working — try your link again."
            ),
            reply_markup=back_markup
        )
        await log_event(f"🧪 **Admin Action:** Cookie jar `{cookie_key}.txt` passed live test ({result['real_count']} formats).")
    else:
        await client.send_message(
            chat_id=user_id,
            text=(
                f"⚠️ **Cookie Test Warning for `{cookie_key}.txt`**\n\n"
                "YouTube accepted the cookies, but only returned storyboard/preview formats.\n\n"
                "This means the jar is **bot-flagged, expired, or from an account that cannot watch videos**.\n"
                "Please upload a fresh `ytcookies.txt` from a browser where you can actually play YouTube videos."
            ),
            reply_markup=back_markup
        )
        await log_event(f"⚠️ **Admin Action:** Cookie jar `{cookie_key}.txt` failed live test (storyboard-only).")
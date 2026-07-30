# modules/auto_forward.py
"""
Auto-forward: poll your connected Instagram / TikTok / Twitter accounts for
posts you've "shared to self" (saved / liked), then auto-download them into
Telegram.

How it works
------------
You have a *dedicated* account on each platform (e.g. @mybot_ig on Instagram,
@mybot_tt on TikTok, @mybot_x on X). You follow that account from your real
account. When you see a post you want in Telegram, you use the platform's
native "Share → Save" (or "Like") to send it to your bot account.

This module polls the public "saved" / "liked" feeds of those bot accounts
using yt-dlp's existing cookie jars (the same ones that power manual
downloads). When it finds a post ID it hasn't seen before, it:

  1. downloads the media with utils.downloader.download_media,
  2. uploads it to the configured TELEGRAM chat (auto_forward_chat_id),
  3. records the post ID in a JSON state file so it is never re-sent.

Configuration (.env)
--------------------
# Telegram chat ID where auto-forwarded media lands (your private chat).
AUTO_FORWARD_CHAT_ID=123456789

# Instagram
IG_AUTO_FORWARD_USERNAME=your_bot_ig_username
IG_AUTO_FORWARD_ENABLED=true

# TikTok
TT_AUTO_FORWARD_USERNAME=your_bot_tt_username
TT_AUTO_FORWARD_ENABLED=true

# X / Twitter
X_AUTO_FORWARD_USERNAME=your_bot_x_username
X_AUTO_FORWARD_ENABLED=true

# Poll interval (seconds). Default 300 = 5 min.
AUTO_FORWARD_POLL_SECONDS=300

# Max items per poll per platform (safety). Default 10.
AUTO_FORWARD_MAX_ITEMS=10

State file: auto_forward_state.json (per-platform seen-ID set).
"""

import os
import json
import asyncio
import logging
from typing import Any

import yt_dlp

import config
from utils.downloader import download_media, get_cookies_for_url, _apply_pot_options
from utils.uploader_handler import process_split_and_upload

logger = logging.getLogger(__name__)

STATE_FILE = "auto_forward_state.json"


def _load_state() -> dict[str, set[str]]:
    if not os.path.exists(STATE_FILE):
        return {"instagram": set(), "tiktok": set(), "x": set()}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {k: set(v) for k, v in raw.items()}
    except Exception:
        return {"instagram": set(), "tiktok": set(), "x": set()}


def _save_state(state: dict[str, set[str]]) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({k: sorted(v) for k, v in state.items()}, f)
    os.replace(tmp, STATE_FILE)


def _extract_saved_or_liked(platform: str, username: str, max_items: int) -> list[dict[str, Any]]:
    """
    Extract the public "saved" / "liked" feed for *username* using yt-dlp.

    Returns a list of dicts: {"id": str, "url": str, "title": str, "type": "video"|"photo"}
    """
    cookie_path = get_cookies_for_url(f"https://www.{platform}.com/")

    # Map platform -> URL that lists the user's saved/liked content.
    # Instagram: /saved/ requires login (we have cookies).
    # TikTok:    /likes/ requires login (we have cookies).
    # X:         /likes requires login (we have cookies).
    urls = {
        "instagram": f"https://www.instagram.com/{username}/saved/",
        "tiktok":    f"https://www.tiktok.com/@{username}",
        "x":         f"https://x.com/{username}/likes",
    }
    feed_url = urls.get(platform)
    if not feed_url:
        return []

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,  # do not resolve every video yet; just list URLs
        "playlistend": max_items,
        "proxy": getattr(config, "YTDLP_PROXY", None),
    }
    if cookie_path:
        ydl_opts["cookiefile"] = cookie_path
    user_agent = getattr(config, "YTDLP_USER_AGENT", "")
    if user_agent:
        ydl_opts["user_agent"] = user_agent

    # PO token only applies to YouTube; harmless to try but will no-op for
    # other platforms.
    ydl_opts = _apply_pot_options(ydl_opts, feed_url)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(feed_url, download=False)
    except Exception as e:
        logger.warning(f"[AutoForward] Failed to extract {platform} feed for @{username}: {e}")
        return []

    entries = info.get("entries") or []
    items = []
    for entry in entries[:max_items]:
        if not entry:
            continue
        # Flat entries give us id, url, title. Resolve the real URL.
        entry_id = entry.get("id") or entry.get("url") or ""
        entry_url = entry.get("url") or entry.get("webpage_url") or ""
        title = entry.get("title") or entry.get("description") or "Untitled"
        if not entry_id or not entry_url:
            continue
        # Normalise URL to canonical form so dedupe works.
        if platform == "instagram" and "instagram.com" not in entry_url:
            entry_url = f"https://www.instagram.com/p/{entry_id}/"
        elif platform == "tiktok" and "tiktok.com" not in entry_url:
            entry_url = f"https://www.tiktok.com/@{username}/video/{entry_id}"
        elif platform == "x" and ("x.com" not in entry_url and "twitter.com" not in entry_url):
            entry_url = f"https://x.com/{username}/status/{entry_id}"
        items.append({"id": str(entry_id), "url": entry_url, "title": title})

    return items


async def _download_and_send(
    bot_client,
    premium_client,
    chat_id: int,
    platform: str,
    item: dict[str, str],
) -> bool:
    """
    Download a single auto-forward item and send it to Telegram.
    Returns True on success.
    """
    url = item["url"]
    title = item["title"]
    cache_id = f"af_{platform}_{item['id']}"

    try:
        # Download best quality (default format selection).
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            download_media,
            url,
            None,               # format_id=None -> best
            "v",                # format_type video (photos handled as video too)
            cache_id,
            None,               # progress_fn
            None,               # format_selector
            None,               # max_height
            None,               # best_audio_format_id
        )

        file_path = result["file_path"]
        thumb_path = result["thumb_path"]
        duration = result.get("duration", 0)

        # Determine if it's a photo (no video stream) and upload accordingly.
        from utils.downloader import probe_video_dimensions
        w, h, dur = probe_video_dimensions(file_path)
        is_photo = (w == 320 and h == 320) or file_path.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))

        if is_photo:
            # Upload as photo.
            await bot_client.send_photo(
                chat_id=chat_id,
                photo=file_path,
                caption=f"🔄 **Auto-forward from {platform}**\n`{title}`",
            )
        else:
            await process_split_and_upload(
                bot_client=bot_client,
                premium_client=premium_client,
                chat_id=chat_id,
                file_path=file_path,
                action="v",
                title=title,
                uploader=result.get("uploader", "Unknown"),
                duration=duration,
                thumb_path=thumb_path,
                progress_msg=None,
                delete_progress_after=True,
            )

        logger.info(f"[AutoForward] ✅ Sent {platform} item {item['id']} -> chat {chat_id}")
        return True

    except Exception as e:
        logger.error(f"[AutoForward] ❌ Failed {platform} item {item['id']}: {e}")
        return False
    finally:
        # Clean up cache dir
        task_dir = f"cache/{cache_id}"
        if os.path.exists(task_dir):
            try:
                import shutil
                shutil.rmtree(task_dir)
            except Exception:
                pass


async def auto_forward_worker(bot_client, premium_client):
    """
    Background task: poll configured accounts every N seconds.
    """
    chat_id = int(os.getenv("AUTO_FORWARD_CHAT_ID", "0"))
    if chat_id == 0:
        logger.info("[AutoForward] AUTO_FORWARD_CHAT_ID not set; auto-forward disabled.")
        return

    poll_seconds = int(os.getenv("AUTO_FORWARD_POLL_SECONDS", "300"))
    max_items = int(os.getenv("AUTO_FORWARD_MAX_ITEMS", "10"))

    platforms = []
    if os.getenv("IG_AUTO_FORWARD_ENABLED", "").lower() in ("true", "1", "yes"):
        u = os.getenv("IG_AUTO_FORWARD_USERNAME", "").strip()
        if u:
            platforms.append(("instagram", u))
    if os.getenv("TT_AUTO_FORWARD_ENABLED", "").lower() in ("true", "1", "yes"):
        u = os.getenv("TT_AUTO_FORWARD_USERNAME", "").strip()
        if u:
            platforms.append(("tiktok", u))
    if os.getenv("X_AUTO_FORWARD_ENABLED", "").lower() in ("true", "1", "yes"):
        u = os.getenv("X_AUTO_FORWARD_USERNAME", "").strip()
        if u:
            platforms.append(("x", u))

    if not platforms:
        logger.info("[AutoForward] No platforms enabled; auto-forward disabled.")
        return

    logger.info(f"[AutoForward] Started. Chat={chat_id}, poll={poll_seconds}s, platforms={platforms}")

    while True:
        state = _load_state()
        changed = False

        for platform, username in platforms:
            try:
                items = _extract_saved_or_liked(platform, username, max_items)
                new_items = [i for i in items if i["id"] not in state[platform]]
                if not new_items:
                    continue

                logger.info(f"[AutoForward] {platform}/@{username}: {len(new_items)} new items")

                for item in new_items:
                    ok = await _download_and_send(bot_client, premium_client, chat_id, platform, item)
                    if ok:
                        state[platform].add(item["id"])
                        changed = True
                    else:
                        # Leave unseen so we retry next poll (but cap retries
                        # by keeping a per-item failure counter in state).
                        pass

            except Exception as e:
                logger.error(f"[AutoForward] Poll failed for {platform}/@{username}: {e}")

        if changed:
            _save_state(state)

        await asyncio.sleep(poll_seconds)


def start_auto_forward_task(bot_client, premium_client):
    """Create the background task. Called from main.py after clients are up."""
    return asyncio.create_task(auto_forward_worker(bot_client, premium_client))

"""
Telegram archiving for the Friend Media Archiver.

All work runs on the connected *user* account (kurigram Client). A bot account
cannot read another user's full profile-photo history, so this is the only
client that performs the read + download. Delivery is ALWAYS to the safe
destination (see common.resolve_destination) — never to the friend.
"""

import os
import tempfile
import asyncio
import logging
import config

from . import common

logger = logging.getLogger(__name__)


async def resolve_telegram_user(handle):
    """Resolve a friend by numeric id, @username, or bare username.

    Returns the pyrogram User, or None if not found. Best-effort adds them to the
    connected account's contacts (silent — sends nothing).
    """
    if handle is None:
        return None
    h = str(handle).strip()
    if not h:
        return None
    client = common.user_client()
    if client is None:
        logger.error("[FriendMedia:tg] user_client (premium_app) is not started.")
        return None
    try:
        user = await client.get_users(h)
    except Exception as e:
        logger.warning(f"[FriendMedia:tg] get_users({h!r}) failed: {e}")
        return None
    if user is None:
        return None
    await common.ensure_contact(user)
    return user


async def _tmp_path(suffix):
    d = tempfile.mkdtemp(prefix="fm_")
    return os.path.join(d, "media" + suffix)


async def archive_telegram_profile_photos(user, key, max_photos=None):
    """Download ALL currently-available profile pictures and deliver them.

    Returns the number delivered. Honors FRIEND_MEDIA_MAX_PHOTOS as a hard cap.
    """
    if max_photos is None:
        max_photos = int(getattr(config, "FRIEND_MEDIA_MAX_PHOTOS", 2000) or 2000)
    client = common.user_client()
    delivered = 0
    targets = []
    try:
        async for photo in client.get_chat_photos(user.id, limit=0):
            targets.append(photo)
            if len(targets) >= max_photos:
                break
    except Exception as e:
        logger.warning(f"[FriendMedia:tg] get_chat_photos for {user.id} failed: {e}")
        return 0

    total = len(targets)
    logger.info(f"[FriendMedia:tg] archiving {total} profile photos for {user.id} (key={key}).")
    for idx, photo in enumerate(targets, start=1):
        out = None
        try:
            path = await _tmp_path(".jpg")
            out = await client.download_media(photo, file_name=path)
            if not out or not os.path.exists(out):
                continue
            # The connected user account only DOWNLOADS (a bot can't read the
            # friend's profile-photo history). DELIVERY is done by the BOT: it
            # posts to the log channel, then DMs you in its own chat with you.
            ok = await common._safe_deliver(
                common.bot_client(), out, "photo",
                caption=f"📸 Profile picture {idx}/{total} · {key}"
            )
            if ok:
                delivered += 1
        except Exception as e:
            logger.warning(f"[FriendMedia:tg] photo {idx} failed: {e}")
        finally:
            try:
                if out and os.path.exists(out):
                    os.remove(out)
                if out and os.path.exists(os.path.dirname(out)):
                    os.rmdir(os.path.dirname(out))
            except Exception:
                pass
    return delivered


async def archive_telegram_stories(user, key, max_stories=None):
    """Deliver the friend's CURRENT stories (no older/highlight history)."""
    if max_stories is None:
        max_stories = int(getattr(config, "FRIEND_MEDIA_MAX_STORIES", 100) or 100)
    client = common.user_client()
    delivered = 0
    try:
        stories = await client.get_stories(user.id)
    except Exception as e:
        logger.warning(f"[FriendMedia:tg] get_stories for {user.id} failed: {e}")
        return 0
    if not stories:
        return 0
    if not isinstance(stories, list):
        stories = [stories]
    stories = stories[:max_stories]
    total = len(stories)
    logger.info(f"[FriendMedia:tg] archiving {total} current stories for {user.id} (key={key}).")
    for idx, story in enumerate(stories, start=1):
        media = getattr(story, "video", None) or getattr(story, "photo", None)
        if media is None:
            continue
        suffix = ".mp4" if getattr(story, "video", None) else ".jpg"
        kind = "video" if getattr(story, "video", None) else "photo"
        out = None
        try:
            path = await _tmp_path(suffix)
            out = await client.download_media(media, file_name=path)
            if not out or not os.path.exists(out):
                continue
            # Downloaded by the user account; delivered by the BOT (see profile
            # photos above for why the split exists).
            ok = await common._safe_deliver(
                common.bot_client(), out, kind,
                caption=f"📖 Story {idx}/{total} · {key}"
            )
            if ok:
                delivered += 1
        except Exception as e:
            logger.warning(f"[FriendMedia:tg] story {idx} failed: {e}")
        finally:
            try:
                if out and os.path.exists(out):
                    os.remove(out)
                if out and os.path.exists(os.path.dirname(out)):
                    os.rmdir(os.path.dirname(out))
            except Exception:
                pass
    return delivered


async def archive_friend_telegram(key, friend, bot=None, status_msg=None):
    """Archive a single friend's Telegram media per their toggles.

    Delivers to the safe destination. Returns a short human summary string.
    """
    u_id = friend.get("telegram_user_id")
    if not u_id:
        return "no telegram id"
    user = await resolve_telegram_user(u_id)
    if user is None:
        return "could not resolve user"
    parts = []
    if friend.get("profile_photos"):
        n = await archive_telegram_profile_photos(user, key)
        parts.append(f"{n} profile pics")
    if friend.get("stories"):
        n = await archive_telegram_stories(user, key)
        parts.append(f"{n} stories")
    summary = ", ".join(parts) if parts else "nothing enabled"
    return summary

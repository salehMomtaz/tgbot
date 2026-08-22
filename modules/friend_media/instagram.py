"""
Instagram current-stories archiving for the Friend Media Archiver.

BEST-EFFORT ONLY. Per the user's request ("for instagram we don't need older
contents"), we fetch ONLY the target's CURRENT stories — never highlights or
older history. Requires a working IG cookie jar AND that the connected IG account
is allowed to view the target's stories. Gated by FRIEND_MEDIA_IG_ENABLED.

Delivery goes through the BOT account (bot_client) to SYSTEM_CREATOR_ID — the
bot's own operator chat, never the friend. The only Instagram operation that
touches the platform is a read (user_stories) done with the operator's own
session; nothing is posted or messaged to the friend.
"""

import os
import tempfile
import asyncio
import logging
import config

from . import common

logger = logging.getLogger(__name__)

_IG_JAR = "cookies/instagram/igcookies.txt"


def _ig_sessionid_from_jar():
    try:
        with open(_IG_JAR, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                if raw.startswith("#") or not raw.strip():
                    continue
                parts = raw.rstrip("\n").split("\t")
                if len(parts) >= 7 and parts[5] == "sessionid" and parts[6]:
                    return parts[6]
    except Exception:
        pass
    return None


async def _ig_client():
    """Build + authenticate an instagrapi client from the shared IG jar, in a
    thread executor (instagrapi is blocking). Returns the client or None."""
    try:
        from instagrapi import Client as IGClient
    except Exception:
        logger.warning("[FriendMedia:ig] instagrapi not installed.")
        return None

    def _build():
        cl = IGClient()
        sid = _ig_sessionid_from_jar()
        if not sid:
            raise RuntimeError("no sessionid in igcookies.txt")
        if not cl.login_by_sessionid(sid):
            raise RuntimeError("login_by_sessionid failed")
        return cl

    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _build)
    except Exception as e:
        logger.warning(f"[FriendMedia:ig] client build failed: {e}")
        return None


async def archive_instagram_stories(key, friend, bot=None):
    """Fetch + deliver the friend's CURRENT Instagram stories. Returns count."""
    if not getattr(config, "FRIEND_MEDIA_IG_ENABLED", False):
        return 0
    ig_user = friend.get("ig_username")
    if not ig_user:
        return 0
    cl = await _ig_client()
    if cl is None:
        return 0
    max_stories = int(getattr(config, "FRIEND_MEDIA_MAX_STORIES", 100) or 100)
    delivered = 0
    try:
        loop = asyncio.get_event_loop()

        def _fetch():
            pk = cl.user_id_from_username(ig_user.lstrip("@"))
            stories = cl.user_stories(pk) or []
            return stories[:max_stories]

        stories = await loop.run_in_executor(None, _fetch)

        bot_c = bot or common.bot_client()
        dest = config.SYSTEM_CREATOR_ID

        def _download_one(story):
            d = tempfile.mkdtemp(prefix="fmig_")
            # instagrapi story_download returns the file path
            return cl.story_download(story.pk, folder=d)

        for idx, story in enumerate(stories, start=1):
            path = None
            try:
                path = await loop.run_in_executor(None, _download_one, story)
                if not path or not os.path.exists(path):
                    continue
                kind = "video" if str(path).lower().endswith((".mp4", ".mov", ".webm")) else "photo"
                ok = await common._safe_deliver_raw(
                    bot_c, dest, path, kind,
                    caption=f"📷 IG story {idx}/{len(stories)} · {key}"
                )
                if ok:
                    delivered += 1
            except Exception as e:
                logger.warning(f"[FriendMedia:ig] story {idx} failed: {e}")
            finally:
                try:
                    if 'path' in dir() and path and os.path.exists(path):
                        os.remove(path)
                    if path and os.path.exists(os.path.dirname(path)):
                        os.rmdir(os.path.dirname(path))
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"[FriendMedia:ig] archive failed for {ig_user}: {e}")
    finally:
        try:
            cl.logout()
        except Exception:
            pass
    return delivered

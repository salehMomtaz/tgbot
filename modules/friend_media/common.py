"""
Shared helpers for the Friend Media Archiver.

CRITICAL SAFETY INVARIANT (per the user's hard constraint "In no circumstance
the program may message anyone"):
  * The ONLY operation that touches a *friend* is ``add_contact`` on the
    connected user account — that adds them to the account's contacts and sends
    NOTHING (no message, no notification to the friend).
  * Every fetched media item is delivered ONLY to the configured destination,
    which is the connected account's own Saved Messages ("me"/"saved") or a
    numeric chat id the operator owns. A friend's id is NEVER passed as a send
    target anywhere in this package.
"""

import os
import asyncio
import logging
import config

logger = logging.getLogger(__name__)


def user_client():
    """The connected *user* account (kurigram Client). Bot accounts cannot read
    another user's full profile-photo history, so archiving is done by this one."""
    from main import premium_app
    return premium_app


def bot_client():
    from main import app
    return app


def resolve_destination():
    """Where archived Telegram media lands. 'saved' -> the account's own Saved
    Messages. Otherwise a numeric chat id the operator controls."""
    dest = getattr(config, "FRIEND_MEDIA_DESTINATION", "saved") or "saved"
    if dest.strip().lower() in ("saved", "me", ""):
        return "me"
    try:
        return int(dest)
    except Exception:
        return "me"


async def ensure_contact(user):
    """Add the friend to the connected account's contacts if not already. Sends
    NOTHING — this is the only call that touches the friend, and it is silent."""
    if user is None:
        return
    # Only attempt for users that look like a real contact target.
    u_id = getattr(user, "id", None)
    if not u_id:
        return
    try:
        first = getattr(user, "first_name", "") or ""
        last = getattr(user, "last_name", "") or ""
        await user_client().add_contact(user_id=u_id, first_name=first, last_name=last)
        logger.info(f"[FriendMedia] Added {u_id} ({first}) to contacts (silent).")
    except Exception as e:
        # add_contact is best-effort: if already a contact or rate-limited, ignore.
        logger.info(f"[FriendMedia] add_contact skipped for {u_id}: {e}")


async def _safe_deliver_raw(client, dest, path, kind, caption=None):
    """Deliver a local file to an EXPLICIT destination via an EXPLICIT client.

    Both args are supplied by the caller; this never derives a target from a
    friend. Used by Instagram archiving (bot -> SYSTEM_CREATOR_ID).
    """
    if not path or not os.path.exists(path):
        return False
    delay = int(getattr(config, "FRIEND_MEDIA_SEND_DELAY", 1) or 1)
    try:
        from pyrogram.errors import FloodWait
        if kind == "photo":
            await client.send_photo(dest, path, caption=caption)
        elif kind == "video":
            await client.send_video(dest, path, caption=caption)
        else:
            await client.send_document(dest, path, caption=caption)
        if delay > 0:
            await asyncio.sleep(delay)
        return True
    except FloodWait as fw:
        wait = int(getattr(fw, "value", 0) or 0)
        logger.warning(f"[FriendMedia] FloodWait {wait}s delivering {path}; backing off.")
        await asyncio.sleep(min(wait, 60) + 1)
        try:
            if kind == "photo":
                await client.send_photo(dest, path, caption=caption)
            elif kind == "video":
                await client.send_video(dest, path, caption=caption)
            else:
                await client.send_document(dest, path, caption=caption)
            if delay > 0:
                await asyncio.sleep(delay)
            return True
        except Exception as e:
            logger.warning(f"[FriendMedia] deliver retry failed {path}: {e}")
            return False
    except Exception as e:
        logger.warning(f"[FriendMedia] deliver failed {path}: {e}")
        return False


async def _safe_deliver(client, path, kind, caption=None):
    """Deliver a local file to the SAFE destination only. Never a friend id.

    kind: 'photo' | 'video' | 'document'
    """
    return await _safe_deliver_raw(client, resolve_destination(), path, kind, caption)

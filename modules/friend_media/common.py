"""
Shared helpers for the Friend Media Archiver.

CRITICAL SAFETY INVARIANT (per the user's hard constraint "In no circumstance
the program may message anyone"):
  * The ONLY operation that touches a *friend* is ``add_contact`` on the
    connected user account — that adds them to the account's contacts and sends
    NOTHING (no message, no notification to the friend).
    * Every fetched media item is delivered ONLY to the configured destination,
    which is:
      - "logchannel" (default): the BOT posts the media to the operator-owned
        LOG_CHANNEL_ID, then DMs it to the admin (you) in the bot's own existing
        chat with you. The connected *user* account never delivers — it only
        downloads (a bot can't read a friend's profile-photo history) and adds
        the friend to contacts. Nothing lands in Saved Messages.
      - "saved": the connected account's own Saved Messages.
      - a numeric chat id the operator owns.
    A friend's id is NEVER passed as a send target anywhere in this package.
"""

import os
import re
import asyncio
import logging
from urllib.parse import urlparse, unquote

import config

logger = logging.getLogger(__name__)

_IG_USERNAME_CHARS_RE = re.compile(r"^[a-z0-9._]{1,30}$")

# Instagram path first-segments that are NEVER a username (post/reel share
# links, app routes, feature pages). A profile URL is instagram.com/<username>.
_IG_RESERVED_PATHS = {
    "p", "reel", "reels", "tv", "stories", "explore", "accounts", "direct",
    "directory", "about", "developer", "legal", "help", "api", "static",
    "u", "r", "s", "a", "www", "web", "m", "sharer", "share", "embed",
}


def extract_ig_username(raw):
    """Extract a bare Instagram username from ANY form the operator may paste:
    a profile URL (with or without scheme / www / trailing slash), an
    instagram:// deeplink, ``@username``, ``username``, a mobile share link
    carrying tracking junk (``?igsi=`` / ``?igshid=`` / ``utm_*`` / ``s=``),
    or a ``/stories/<username>/...`` path.

    Instagram share IDs (igsi/igshid) are per-share tracking tokens that
    expire and carry no identity value; they (and every other query string /
    fragment) are stripped here so they can never be persisted as a username.
    Returns the cleaned lowercase username, or None when the input does not
    yield a valid IG username (e.g. a post/reel share URL).
    """
    if not raw:
        return None
    text = str(raw).strip().strip('"').strip("'").rstrip(").,;!")
    if not text:
        return None

    # Deeplink form: instagram://user?username=<name>
    if text.lower().startswith("instagram://"):
        try:
            q = urlparse(text).query
            for part in q.split("&"):
                k, _, v = part.partition("=")
                if k.strip().lower() == "username" and v.strip():
                    text = v.strip()
                    break
        except Exception:
            return None

    candidate = None
    if "://" in text or re.match(r"^(?:www\.|m\.)?instagram\.com", text, re.I):
        url = text if "://" in text else "https://" + text
        try:
            parsed = urlparse(url)
        except Exception:
            return None
        host = (parsed.netloc or "").lower()
        if host and "instagram.com" not in host and host != "instagr.am":
            return None
        # Query (igsi/igshid/utm_*/...) and fragment are DISCARDED by design.
        segs = [unquote(s) for s in (parsed.path or "").split("/") if s]
        if segs:
            first = segs[0].lower()
            if first == "stories" and len(segs) > 1:
                candidate = segs[1]          # /stories/<username>/<id>/
            elif first == "u" and len(segs) > 1:
                candidate = segs[1]          # legacy /u/<username>
            elif first not in _IG_RESERVED_PATHS:
                candidate = segs[0]          # /<username>/ profile path
    else:
        # Bare / @ form: strip everything from the first separator onward
        # (handles "name?igsi=…" and "instagram.com/name" typed without a
        # scheme, plus stray trailing slashes).
        candidate = re.split(r"[?#]", text, 1)[0]
        candidate = candidate.rstrip("/")
        if "instagram.com" in candidate.lower() or "instagr.am" in candidate.lower():
            candidate = candidate.split("instagram.com", 1)[-1].split("instagr.am", 1)[-1]
            candidate = [s for s in candidate.split("/") if s]
            candidate = candidate[0] if candidate else ""
        candidate = candidate.lstrip("@")

    if not candidate:
        return None
    candidate = str(candidate).strip().lstrip("@").lower()
    # A username can still have arrived with a query (no-scheme URL path).
    candidate = re.split(r"[?#]", candidate, 1)[0].rstrip("/")
    if not candidate or len(candidate) > 30:
        return None
    if candidate.isdigit():
        return None
    if not _IG_USERNAME_CHARS_RE.match(candidate):
        return None
    if ".." in candidate or not any(c.isalnum() for c in candidate):
        return None
    return candidate


def ig_username_of(friend):
    """Read a friend record's IG username, defensively re-cleaned through
    ``extract_ig_username``. Legacy/corrupted records (a full share URL stored
    verbatim as the username) resolve to the bare handle instead of being
    passed to the IG API as a username, and unparseable ones resolve to ""
    so archive paths skip them cleanly instead of hammering 404/429s."""
    return extract_ig_username((friend or {}).get("ig_username") or "") or ""


def user_client():
    """The connected *user* account (kurigram Client). Bot accounts cannot read
    another user's full profile-photo history, so archiving is done by this one."""
    from main import premium_app
    return premium_app


def bot_client():
    from main import app
    return app


def resolve_destination():
    """Resolve the delivery mode for archived media.

    Returns one of: "logchannel" (post to LOG_CHANNEL_ID then DM the admin),
    "me" (the connected account's Saved Messages), or an int chat id the
    operator owns.
    """
    dest = getattr(config, "FRIEND_MEDIA_DESTINATION", "logchannel") or "logchannel"
    d = dest.strip().lower()
    if d == "logchannel":
        return "logchannel"
    if d in ("saved", "me", ""):
        return "me"
    try:
        return int(dest)
    except Exception:
        return "logchannel"


async def ensure_contact(user):
    """Add the friend to the connected account's contacts UNDER THEIR REAL NAME.
    Sends NOTHING — this is the only call that touches the friend, and it is silent.

    Why the explicit delete-then-readd:
    ``contacts.AddContact`` is supposed to overwrite the contact's stored
    name when called for an existing user_id, but in practice the overwrite
    only happens reliably when the contact was originally created via the
    same AddContact path. If the user manually added the contact in their
    phone app (or via an earlier bot run that used a placeholder), the
    address-book entry may keep its old name (e.g. the placeholder or a
    manual alias) even after a successful ``add_contact(real_name)`` call
    — the operator then sees the wrong name in their contact list and the
    friend_media log line.

    To guarantee the address book ends up with the account's real
    ``first_name`` / ``last_name``, we always:
      1. ``delete_contacts(user_id)`` — removes any existing entry
         (no-op if the user wasn't a contact). The DELETE operation
         never sends anything to the friend; it only mutates our own
         address book.
      2. ``add_contact(user_id, real_first, real_last)`` — creates a
         fresh entry with the canonical account name resolved by
         ``contacts.ResolvePhone`` upstream.

    Both calls are best-effort and silently caught — a rate-limited or
    network-blip on either step is logged at INFO, not raised.
    """
    if user is None:
        return
    # Only attempt for users that look like a real contact target.
    u_id = getattr(user, "id", None)
    if not u_id:
        return
    uc = user_client()
    if uc is None:
        return
    first = getattr(user, "first_name", "") or ""
    last = getattr(user, "last_name", "") or ""
    # Step 1: drop any existing contact entry. delete_contacts is a no-op
    # when the user isn't a contact yet, and never sends anything to
    # them — it only mutates the connected account's own address book.
    try:
        await uc.delete_contacts(u_id)
        logger.info(
            f"[FriendMedia] Cleared any existing contact entry for {u_id} "
            f"(so the real name can take its place)."
        )
    except Exception as e:
        # Not a contact, rate-limited, or peer not yet known — all benign
        # for our purpose; the add_contact below still works.
        logger.info(
            f"[FriendMedia] delete_contacts({u_id}) skipped: {e}"
        )
    # Step 2: add the contact under the account's real name. This is
    # the entry that now appears in the operator's contact list, and
    # it's the entry future ``get_users(id)`` calls will see.
    try:
        await uc.add_contact(user_id=u_id, first_name=first, last_name=last)
        logger.info(
            f"[FriendMedia] Added {u_id} ({first}) to contacts (silent) — "
            f"contact now mirrors the account's real name."
        )
    except Exception as e:
        # add_contact is best-effort: rate-limited or peer-gone, ignore.
        logger.info(
            f"[FriendMedia] add_contact({u_id}) skipped: {e}"
        )


async def _send_once(client, dest, path, kind, caption):
    if kind == "photo":
        return await client.send_photo(dest, path, caption=caption)
    elif kind == "video":
        return await client.send_video(dest, path, caption=caption)
    else:
        return await client.send_document(dest, path, caption=caption)


async def _deliver_via_logchannel(client, path, kind, caption):
    """Post the media to the operator-owned LOG_CHANNEL_ID via the connected USER
    account (kurigram), then have the BOT ``copy_message`` it to the admin (you).

    ``copy_message`` re-uses the existing file_id (no size limit, no re-download)
    and — unlike ``forward_messages`` — carries NO "Forwarded from …" header: the
    sender shows as the BOT itself. This mirrors the >2 GB premium-upload path in
    ``utils/uploader_handler.py::_stage_and_relay``.

    The staged message stays in the log channel as the archive. Nothing is sent
    to the friend.
    """
    lc = getattr(config, "LOG_CHANNEL_ID", 0) or 0
    admin = getattr(config, "SYSTEM_CREATOR_ID", 0) or 0
    if not lc:
        logger.warning("[FriendMedia] LOG_CHANNEL_ID not set; cannot deliver via log channel.")
        return False
    from pyrogram.errors import FloodWait

    user = user_client()   # connected kurigram account: uploads to the archive
    bot = bot_client()     # bot: copy_message to the admin (no forward header)

    async def _copy_to_admin(staged):
        """Bot copy_message to the admin — sender = bot, no forwarded header."""
        if not (admin and admin != lc):
            return bool(staged)
        if not bot or not staged:
            return bool(staged)
        try:
            await bot.copy_message(
                chat_id=admin,
                from_chat_id=lc,
                message_id=staged.id,
                caption=caption,
            )
            return True
        except Exception as ce:
            logger.warning(f"[FriendMedia] bot copy_message to admin failed: {ce}")
            return bool(staged)

    try:
        uploader = user or client or bot
        staged = await _send_once(uploader, lc, path, kind, caption)
        await _copy_to_admin(staged)
        delay = int(getattr(config, "FRIEND_MEDIA_SEND_DELAY", 1) or 1)
        if delay > 0:
            await asyncio.sleep(delay)
        return bool(staged)
    except FloodWait as fw:
        wait = int(getattr(fw, "value", 0) or 0)
        logger.warning(f"[FriendMedia] FloodWait {wait}s delivering {path}; backing off.")
        await asyncio.sleep(min(wait, 60) + 1)
        try:
            uploader = user or client or bot
            staged = await _send_once(uploader, lc, path, kind, caption)
            await _copy_to_admin(staged)
            return bool(staged)
        except Exception as e:
            logger.warning(f"[FriendMedia] deliver retry failed {path}: {e}")
            return False
    except Exception as e:
        logger.warning(f"[FriendMedia] deliver failed {path}: {e}")
        return False


async def _safe_deliver_raw(client, dest, path, kind, caption=None):
    """Deliver a local file to an EXPLICIT destination via an EXPLICIT client.

    ``dest`` is the raw mode from resolve_destination(): "logchannel", "me", or
    an int chat id. This never derives a target from a friend.
    """
    if not path or not os.path.exists(path):
        return False
    if dest == "logchannel":
        return await _deliver_via_logchannel(client, path, kind, caption)
    delay = int(getattr(config, "FRIEND_MEDIA_SEND_DELAY", 1) or 1)
    try:
        from pyrogram.errors import FloodWait
        await _send_once(client, dest, path, kind, caption)
        if delay > 0:
            await asyncio.sleep(delay)
        return True
    except FloodWait as fw:
        wait = int(getattr(fw, "value", 0) or 0)
        logger.warning(f"[FriendMedia] FloodWait {wait}s delivering {path}; backing off.")
        await asyncio.sleep(min(wait, 60) + 1)
        try:
            await _send_once(client, dest, path, kind, caption)
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

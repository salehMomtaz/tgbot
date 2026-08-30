"""
Telegram archiving for the Friend Media Archiver.

All work runs on the connected *user* account (kurigram Client). A bot account
cannot read another user's full profile-photo history, so this is the only
client that performs the read + download. Delivery is ALWAYS to the safe
destination (see common.resolve_destination) — never to the friend.

Incremental model (per friend record):
  * ``seen_photo_ids``   — profile-photo ids already delivered. A full
                           **backfill** walks ALL pages oldest→newest, saving
                           seen ids periodically (crash-safe resume); an
                           **incremental check** fetches only the newest page
                           and delivers just the unseen ones.
  * ``backfilled``       — set once a full backfill completed; incremental runs
                           before it would spam everything, so they upgrade
                           themselves to a backfill instead.
  * ``seen_story_ids``   — bounded ring of story ids already delivered, so a
                           story that stays live across cycles is not re-sent.

pyrogram's get_chat_photos iterates NEWEST-first; backfill reverses into
oldest-first delivery so the archive reads chronologically.
"""

import os
import tempfile
import logging
import config

from . import common

logger = logging.getLogger(__name__)

# Persist seen-id progress every N deliveries during a long backfill so a crash
# never restarts the whole 1900-photo walk from zero.
_SAVE_EVERY = 10
# Ring-buffer cap for seen_story_ids / seen_photo_ids bookkeeping extras.
_SEEN_CAP = 5000


async def resolve_telegram_user(handle):
    """Resolve a friend by numeric id, @username, or bare username.

    Numeric ids that get_users can't resolve (peer unknown to the connected
    account) fall back to a scan of its own contact list by id — kurigram's
    get_contacts returns full User objects for everyone already in contacts,
    which is exactly where ensure_contact put them on a previous run.

    Returns the pyrogram User, or None if not found. Best-effort adds them to
    the connected account's contacts (silent — sends nothing).
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
    user = None
    if h.isdigit():
        # kurigram interprets DIGIT STRINGS as phone numbers (PHONE_NOT_OCCUPIED);
        # an int is required for id-based resolution.
        try:
            user = await client.get_users(int(h))
        except Exception as e:
            logger.info(f"[FriendMedia:tg] get_users({h}) failed: {e}")
    else:
        try:
            user = await client.get_users(h)
        except Exception as e:
            logger.info(f"[FriendMedia:tg] get_users({h!r}) failed: {e}")
    if user is None and h.isdigit():
        # Peer unknown to the connected account → scan its own contact list
        # (ensure_contact put them there on a previous successful resolve).
        try:
            contacts = await client.get_contacts()
            for c in contacts or []:
                if getattr(c, "id", None) == int(h):
                    user = c
                    break
        except Exception as ce:
            logger.info(f"[FriendMedia:tg] contacts scan failed: {ce}")
    if user is None:
        return None
    await common.ensure_contact(user)
    return user


def _tmp_dir():
    return tempfile.mkdtemp(prefix="fm_")


def _cleanup(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
        d = os.path.dirname(path) if path else None
        if d and os.path.exists(d):
            os.rmdir(d)
    except Exception:
        pass


def _add_seen(friend, field, new_ids):
    """Merge new ids into a bounded seen-set stored as a list on the record."""
    cur = list(friend.get(field) or [])
    cur.extend(str(i) for i in new_ids)
    if len(cur) > _SEEN_CAP:
        cur = cur[-_SEEN_CAP:]
    friend[field] = cur


async def _save_friend(key, friend):
    # Persist only archiving-owned fields via merge-friendly update_friend;
    # friend here IS the live state dict entry (update_friend mutates in place),
    # but write explicitly so concurrent admin edits are respected.
    await fm_state_safe_update(key, {
        "seen_photo_ids": friend.get("seen_photo_ids") or [],
        "seen_story_ids": friend.get("seen_story_ids") or [],
        "backfilled": bool(friend.get("backfilled")),
    })


async def fm_state_safe_update(key, patch):
    from . import state as fm_state
    await fm_state.update_friend(key, patch)


async def _fetch_photos(client, user_id, limit):
    """Collect up to ``limit`` photos (newest first). limit=0 → all.

    Re-raises ``PEER_ID_INVALID`` so the caller can surface a clear "this id
    is not a real Telegram user" hint to the operator instead of silently
    returning an empty list forever.
    """
    out = []
    try:
        async for photo in client.get_chat_photos(user_id, limit=limit):
            out.append(photo)
    except Exception as e:
        from pyrogram.errors import PeerIdInvalid
        if isinstance(e, PeerIdInvalid):
            raise
        logger.warning(f"[FriendMedia:tg] get_chat_photos({user_id}) failed: {e}")
    return out


async def _deliver_photo(client, photo, key, idx, total, friend=None):
    """Download + deliver one ChatPhoto, picking the right kind for animated
    profile pictures.

    Animated (video) profile pics expose a separate MP4 file via
    ``photo.animation.animation.file_id`` (an ``AnimatedChatPhoto`` /
    ``Animation`` chain). Telegram stores the animated variant next to a
    static JPEG frame, and the static frame is what
    ``photo.big_file_id`` decodes to. So if we only ever download
    ``big_file_id`` and send as ``photo``, the operator sees a still
    image for the latest profile pic (the regression the user hit on
    ``@Ksrakbri``). When ``photo.animation`` is present we download
    the MP4 and deliver as a video; otherwise we fall through to the
    JPEG.

    The caption is built by ``_photo_caption`` so the same format is
    used for both photo and video deliveries.
    """
    out = None
    try:
        # Detect animated (video) profile pic. ChatPhoto in 2.2.25 exposes
        # ``animation`` as a populated AnimatedChatPhoto (with an
        # ``animation.animation`` of type Animation) when the profile pic
        # is a video. Older Photo objects don't have this field; missing
        # attribute is treated as "no animation" rather than erroring.
        anim = getattr(photo, "animation", None)
        anim_file_id = (
            getattr(getattr(anim, "animation", None), "file_id", None)
            if anim is not None else None
        )

        if anim_file_id:
            # Animated profile pic — deliver the MP4 video.
            out = await client.download_media(
                anim_file_id, file_name=os.path.join(_tmp_dir(), "media.mp4"))
            if not out or not os.path.exists(out):
                return False
            caption = _photo_caption(idx, total, friend, photo=photo,
                                     kind="🎞 Profile video")
            ok = await common._safe_deliver(
                common.bot_client(), out, "video", caption=caption)
            return ok

        # Static (JPEG) profile pic — the existing path.
        # kurigram 2.2.25 changed get_chat_photos to yield ``ChatPhoto``
        # objects (small_file_id/big_file_id); the prior 2.2.24 yielded
        # ``Photo`` objects (file_id). ``download_media`` only auto-detects
        # objects with a ``file_id`` attribute, so a bare ``ChatPhoto`` falls
        # through to ``ValueError("This message doesn't contain any
        # downloadable media")``. Pass the big_file_id string instead —
        # ``download_media``'s ``isinstance(media, str)`` branch handles it.
        # For 2.2.24 Photo objects, prefer the file_id attribute (works on
        # both shapes) and fall back to the whole object for back-compat.
        media_arg = (
            getattr(photo, "big_file_id", None)
            or getattr(photo, "file_id", None)
            or photo
        )
        out = await client.download_media(
            media_arg, file_name=os.path.join(_tmp_dir(), "media.jpg"))
        if not out or not os.path.exists(out):
            return False
        caption = _photo_caption(idx, total, friend, photo=photo,
                                 kind="📸 Profile picture")
        ok = await common._safe_deliver(
            common.bot_client(), out, "photo", caption=caption)
        return ok
    finally:
        _cleanup(out)


def _photo_caption(idx, total, friend, photo=None, kind="📸 Profile picture"):
    """Build the standard caption for a TG profile photo/video delivery.

    Format: ``{kind} hash {idx}/{total} · @{handle} nid: {user_id}``

    Where:
      * ``kind`` is the leading emoji/label — "📸 Profile picture" for
        still images, "🎞 Profile video" for animated (MP4) profile pics.
      * ``handle`` is the friend's @username when known, otherwise the
        first_name, otherwise the numeric id as a last resort.
      * ``user_id`` is the Telegram numeric user id (``nid:`` is the
        user-facing label the operator asked for).
      * ``idx``/``total`` is the per-cycle position counter; the operator
        asked for the literal label "hash" in front of it.
    """
    nid = ""
    handle = ""
    if friend:
        nid = friend.get("telegram_user_id") or ""
        handle = (friend.get("username") or "").strip() or \
                 (friend.get("first_name") or "").strip() or \
                 (friend.get("handle") or "").strip()
    # Prefix the handle with @ when it looks like a username (no spaces,
    # ASCII), otherwise use the bare name. Empty handle = show the nid
    # in place of the handle.
    if handle and all(c.isalnum() or c in "_." for c in handle):
        handle_disp = "@" + handle
    else:
        handle_disp = handle
    # The "·" separator between the position counter and the @handle is
    # kept so the existing visual layout is preserved; only the new
    # "hash …" + "nid:" labels are added.
    parts = [f"{kind} hash {idx}/{total}"]
    if handle_disp:
        parts.append(handle_disp)
    if nid:
        parts.append(f"nid: {nid}")
    return " · ".join(parts)


async def archive_telegram_profile_photos(user, key, friend, full=None,
                                          progress_cb=None):
    """Archive profile photos incrementally (or fully when ``full``).

    Returns (delivered, total_known). ``full=True`` walks every available photo
    oldest→newest, checkpointing seen ids every _SAVE_EVERY deliveries.
    Incremental mode fetches only the newest page and delivers unseen ids.
    """
    max_photos = int(getattr(config, "FRIEND_MEDIA_MAX_PHOTOS", 2000) or 2000)
    client = common.user_client()
    seen = {str(x) for x in (friend.get("seen_photo_ids") or [])}
    if full is None:
        # No prior backfill → an "incremental" request IS the first backfill.
        full = not bool(friend.get("backfilled"))

    if full:
        targets = await _fetch_photos(client, user.id, max_photos)
    else:
        targets = await _fetch_photos(client, user.id, 50)

    total = len(targets)
    if total == 0:
        if full and not friend.get("backfilled"):
            friend["backfilled"] = True
            await _save_friend(key, friend)
        return 0, 0

    order = list(reversed(targets)) if full else targets  # oldest→newest for backfill
    delivered = 0
    scanned = len(order)
    for idx, photo in enumerate(order, start=1):
        # kurigram 2.2.25 ChatPhoto exposes small_photo_unique_id /
        # big_photo_unique_id; kurigram 2.2.24 Photo had id / file_unique_id.
        # Walk the field list in priority order so the seen-set dedupes across
        # the upgrade boundary (an id from 2.2.24 won't match a 2.2.25 id,
        # but a stable unique_id does — both Photo.file_unique_id and
        # ChatPhoto.big_photo_unique_id are stable per Telegram).
        pid = str(
            getattr(photo, "file_unique_id", "")
            or getattr(photo, "big_photo_unique_id", "")
            or getattr(photo, "small_photo_unique_id", "")
            or getattr(photo, "id", "")
        )
        if pid and pid in seen:
            continue
        label_idx = idx if full else f"+{delivered + 1}"
        cap_total = scanned if full else "new"
        ok = False
        try:
            ok = await _deliver_photo(client, photo, key, label_idx, cap_total,
                                       friend=friend)
        except Exception as e:
            logger.warning(f"[FriendMedia:tg] photo {label_idx} for {key} failed: {e}")
        if pid:
            _add_seen(friend, "seen_photo_ids", [pid])
        if ok:
            delivered += 1
        if delivered % _SAVE_EVERY == 0 and delivered > 0:
            await _save_friend(key, friend)
            if progress_cb:
                try:
                    await progress_cb(delivered, scanned)
                except Exception:
                    pass

    if full:
        friend["backfilled"] = True
    await _save_friend(key, friend)
    return delivered, scanned


async def archive_telegram_stories(user, key, friend):
    """Deliver the friend's CURRENT stories not already sent (deduped across
    cycles — stories stay live ~24 h and would otherwise repeat every run).

    Uses ``get_chat_stories(chat_id)`` — the high-level wrapper around
    ``stories.GetPeerStories`` — which returns an async generator of every
    NON-EXPIRED story the target has live. The plain ``get_stories`` only
    fetches a story BY ID (it raises ``ValueError("Invalid story_ids.")`` when
    called without one), and is the wrong tool for "what's live right now".
    """
    max_stories = int(getattr(config, "FRIEND_MEDIA_MAX_STORIES", 100) or 100)
    client = common.user_client()
    seen = {str(x) for x in (friend.get("seen_story_ids") or [])}
    try:
        # get_chat_stories is an AsyncGenerator; collect eagerly so we can
        # surface the count up the stack and so exceptions land here.
        collected = []
        async for s in client.get_chat_stories(user.id):
            collected.append(s)
        stories = collected
    except Exception as e:
        logger.info(f"[FriendMedia:tg] get_chat_stories for {user.id} failed: {e}")
        return 0
    if not stories:
        return 0
    fresh = []
    for s in stories[:max_stories]:
        sid = str(getattr(s, "id", "") or "")
        if sid and sid in seen:
            continue
        fresh.append(s)
    if not fresh:
        return 0
    total = len(fresh)
    logger.info(f"[FriendMedia:tg] delivering {total} new stories for {user.id} (key={key}).")
    delivered = 0
    for idx, story in enumerate(fresh, start=1):
        media = getattr(story, "video", None) or getattr(story, "photo", None)
        if media is None:
            continue
        suffix = ".mp4" if getattr(story, "video", None) else ".jpg"
        kind = "video" if getattr(story, "video", None) else "photo"
        path = os.path.join(_tmp_dir(), "media" + suffix)
        out = None
        try:
            out = await client.download_media(media, file_name=path)
            if not out or not os.path.exists(out):
                continue
            caption = _photo_caption(idx, total, friend, kind="📖 Story")
            ok = await common._safe_deliver(
                common.bot_client(), out, kind, caption=caption
            )
            if ok:
                delivered += 1
                sid = str(getattr(story, "id", "") or "")
                if sid:
                    _add_seen(friend, "seen_story_ids", [sid])
        except Exception as e:
            logger.warning(f"[FriendMedia:tg] story {idx} failed: {e}")
        finally:
            _cleanup(out)
    await _save_friend(key, friend)
    return delivered


class _KnownPeer:
    """Minimal stand-in for a resolved User: archiving only needs ``.id``.
    Avoids re-resolution entirely — get_chat_photos/get_stories accept the
    raw numeric id, and the peer is already in the account's dialogs/contacts
    (add_contact ran when the friend was linked)."""

    def __init__(self, u_id):
        self.id = u_id


async def archive_friend_telegram(key, friend, bot=None, status_msg=None,
                                  full=False):
    """Archive a single friend's Telegram media per their toggles.

    Delivers to the safe destination. Returns a short human summary string.
    """
    client = common.user_client()
    if client is None or not getattr(client, "is_initialized", False):
        return "user account not ready (premium session missing or still starting)"
    u_id = friend.get("telegram_user_id")
    if not u_id:
        # Friend was stored UNRESOLVED (e.g. added while the user account was
        # down): re-resolve by handle/@username now and persist the id so this
        # repair runs at most once per friend.
        handle = friend.get("handle") or friend.get("username")
        if not handle:
            return "no telegram id"
        resolved = await resolve_telegram_user(handle)
        new_id = getattr(resolved, "id", None) if resolved else None
        if not new_id:
            return f"could not resolve {handle} to a Telegram account"
        u_id = int(new_id)
        friend["telegram_user_id"] = u_id
        patch = {"telegram_user_id": u_id}
        fn = getattr(resolved, "first_name", None)
        if fn:
            friend["first_name"] = fn
            patch["first_name"] = fn
        un = getattr(resolved, "username", None)
        if un:
            friend["username"] = un
            patch["username"] = un
        await fm_state_safe_update(key, patch)
    # A friend that was never backfilled upgrades ANY check into its one-time
    # full backfill (auto-backfill-on-add may have been interrupted).
    if not full and not friend.get("backfilled"):
        full = True
    user = _KnownPeer(int(u_id))
    parts = []
    if friend.get("profile_photos"):
        async def _prog(done, total):
            if status_msg is not None:
                try:
                    await status_msg.edit_text(
                        f"⏳ `{key}`: {done}/{total} profile pics archived…")
                except Exception:
                    pass
        try:
            n, known = await archive_telegram_profile_photos(
                user, key, friend, full=full, progress_cb=_prog)
            mode = "backfilled" if full else "new"
            parts.append(f"{n} new profile pics ({mode}; {known if full else 'page'} scanned)")
        except Exception as e:
            from pyrogram.errors import PeerIdInvalid
            if isinstance(e, PeerIdInvalid):
                # The stored id is bogus (typically a phone number added via
                # the "by handle" flow before the >10-digit guard existed).
                # Tell the operator clearly what to do instead of silently
                # logging a warning forever.
                handle = friend.get("handle") or friend.get("username") or str(u_id)
                looks_like_phone = (str(u_id).isdigit() and len(str(u_id)) > 10)
                hint = (" — looks like a phone number; re-add via **➕ Add Friend → "
                        "📞 By phone number** so the real id is recorded."
                        if looks_like_phone else
                        " — id is not a real Telegram user; re-add via **➕ Add Friend**.")
                return f"⚠️ peer invalid (id {u_id} for {handle}){hint}"
            raise
    if friend.get("stories"):
        n = await archive_telegram_stories(user, key, friend)
        parts.append(f"{n} new stories")
    summary = ", ".join(parts) if parts else "nothing enabled"
    return summary

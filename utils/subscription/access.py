"""Subscription access gate.

Used by downloader_handler and admin security gate.

Rules:
 - If subscription_mode disabled → legacy: only authorized users pass (existing behavior).
 - If enabled:
    - blacklisted always blocked
    - subscribed (active until > now) → pass
    - creator always passes
    - free_enabled + channel member → pass as free (quota-limited)
    - otherwise → blocked (needs subscription)
"""
from __future__ import annotations

import config
from utils.gate import is_authorized, is_blacklisted
from .store import get_settings, is_subscription_active


def is_subscription_enabled() -> bool:
    return bool(get_settings().get("enabled"))


def is_free_allowed() -> bool:
    s = get_settings()
    return bool(s.get("enabled") and s.get("free_enabled"))


async def is_channel_member(client, user_id: int, channel_id: int) -> bool:
    """Check if user is member of channel. Returns True if check passes or chat not set."""
    if not channel_id:
        return False
    try:
        member = await client.get_chat_member(channel_id, user_id)
        status = getattr(member, "status", None)
        # pyrogram 2.x returns a ChatMemberStatus enum whose str() is
        # "ChatMemberStatus.MEMBER" — str(status).lower() never equals "member",
        # so a joined user was always reported as "not joined". Compare against
        # the enum's .value ("member", "administrator", ...) instead.
        s = (getattr(status, "value", None) or str(status)) if status else ""
        # restricted users are still members (e.g. slow-mode limited).
        return s.lower() in ("member", "administrator", "creator", "owner", "restricted")
    except Exception:
        return False


async def check_all_channels(client, user_id: int) -> tuple[bool, list[dict]]:
    """Return (all_joined, missing_list). Empty missing means all joined or no channels."""
    from .store import get_channels
    chans = get_channels()
    if not chans:
        return True, []
    missing: list[dict] = []
    for ch in chans:
        cid = int(ch.get("id", 0) or 0)
        if cid:
            ok = await is_channel_member(client, user_id, cid)
        else:
            # username-only: try to resolve username to id first
            uname = ch.get("username", "")
            ok = False
            if uname:
                try:
                    chat = await client.get_chat(uname)
                    cid_resolved = int(getattr(chat, "id", 0) or 0)
                    if cid_resolved:
                        ok = await is_channel_member(client, user_id, cid_resolved)
                except Exception:
                    ok = False
        if not ok:
            missing.append(ch)
    return (len(missing) == 0), missing


async def check_access(client, user_id: int) -> tuple[bool, str]:
    """
    Returns (allowed, reason_code).
    reason_code: ok | blocked_blacklisted | need_subscription | need_channel | quota_exceeded
    Caller should map to user message.
    """
    # blacklist always wins
    if is_blacklisted(user_id):
        return False, "blocked_blacklisted"
    if user_id == getattr(config, "SYSTEM_CREATOR_ID", 0):
        return True, "ok"
    settings = get_settings()
    if not settings.get("enabled"):
        # legacy mode
        if is_authorized(user_id):
            return True, "ok"
        return False, "need_subscription"

    # subscription mode ON
    active, _ = is_subscription_active(user_id)
    if active:
        return True, "ok"

    # check free path
    if settings.get("free_enabled"):
        # if channels are set, check all memberships; else allow free directly
        from .store import get_channels
        chans = get_channels()
        if chans:
            ok, missing = await check_all_channels(client, user_id)
            if ok:
                return True, "ok"
            return False, "need_channel"
        # free enabled without channel requirement -> allow
        return True, "ok"

    return False, "need_subscription"

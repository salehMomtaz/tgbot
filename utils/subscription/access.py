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
        # status can be string or enum
        s = str(status).lower() if status else ""
        return s in ("member", "administrator", "creator", "owner")
    except Exception:
        return False


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
        # if channel is set, check membership; else allow free directly
        ch_id = settings.get("channel_id", 0)
        if ch_id:
            ok = await is_channel_member(client, user_id, ch_id)
            if ok:
                return True, "ok"
            return False, "need_channel"
        # free enabled without channel requirement -> allow
        return True, "ok"

    return False, "need_subscription"

"""Daily quota tracking."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import config
from .tiers import TIERS
from .store import get_subscription, is_subscription_active, get_settings


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _daily_limit_for(user_id: int) -> int:
    # creator unlimited
    if user_id == getattr(config, "SYSTEM_CREATOR_ID", 0):
        return 999999
    active, sub = is_subscription_active(user_id)
    if active and sub:
        tier = sub.get("tier", "free")
        return TIERS.get(tier, TIERS["free"])["daily_limit"]
    # not subscribed -> free limit if free enabled, else 0 (blocked)
    settings = get_settings()
    if not settings.get("enabled"):
        return 999999  # subscription mode off -> unlimited (legacy behavior)
    if settings.get("free_enabled"):
        return TIERS["free"]["daily_limit"]
    return 0


def _usage_for(user_id: int, date_str: str | None = None) -> int:
    from utils.gate import load_database
    date_str = date_str or _today_str()
    db = load_database()
    usage = db.get("usage", {})
    return usage.get(str(user_id), {}).get(date_str, 0)


def remaining_quota(user_id: int) -> int:
    limit = _daily_limit_for(user_id)
    used = _usage_for(user_id)
    return max(0, limit - used)


def check_quota(user_id: int) -> tuple[bool, int, int]:
    """Return (allowed, remaining, limit)."""
    limit = _daily_limit_for(user_id)
    used = _usage_for(user_id)
    allowed = used < limit
    return allowed, max(0, limit - used), limit


def increment_quota(user_id: int) -> int:
    """Increment today's usage and return new count (thread-safe)."""
    from utils.gate import load_database, save_database
    import threading
    _lock = threading.Lock()
    with _lock:
        db = load_database()
        if "usage" not in db:
            db["usage"] = {}
        uid = str(user_id)
        today = _today_str()
        if uid not in db["usage"]:
            db["usage"][uid] = {}
        db["usage"][uid][today] = db["usage"][uid].get(today, 0) + 1
        # prune old dates (> 7 days) to keep file small
        for u, dates in list(db["usage"].items()):
            for d in list(dates.keys()):
                if d != today:
                    # keep at most 7 entries
                    if len(dates) > 7:
                        # remove oldest
                        oldest = sorted(dates.keys())[0]
                        if oldest != today:
                            del dates[oldest]
        save_database(db)
        return db["usage"][uid][today]

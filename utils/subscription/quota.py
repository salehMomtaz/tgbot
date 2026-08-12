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


_QUOTA_LOCK = __import__("threading").Lock()

def increment_quota(user_id: int) -> int:
    """Increment today's usage and return new count (thread-safe)."""
    from utils.gate import load_database, save_database
    with _QUOTA_LOCK:
        db = load_database()
        if "usage" not in db:
            db["usage"] = {}
        uid = str(user_id)
        today = _today_str()
        if uid not in db["usage"]:
            db["usage"][uid] = {}
        db["usage"][uid][today] = db["usage"][uid].get(today, 0) + 1
        # prune old dates — keep at most 7 per user, oldest first
        for u, dates in list(db["usage"].items()):
            if len(dates) > 7:
                for old in sorted(d for d in dates.keys() if d != today)[: len(dates) - 7]:
                    dates.pop(old, None)
            # also drop stray empty dicts older than 30 days for hygiene
            # (cheap: if user never returns, their entry lingers ~30d)
            if not dates:
                db["usage"].pop(u, None)
        save_database(db)
        return db["usage"][uid][today]

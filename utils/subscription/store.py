"""Persistent subscription store — lives inside database.json.

Schema additions (all optional, migrated on first load):
  subscriptions: { "user_id": { tier, since, until, granted_by, price_stars, tx } }
  usage: { "user_id": { "YYYY-MM-DD": count } }
  sub_settings: {
     enabled: bool,            # master toggle
     free_enabled: bool,        # allow free tier with channel force-join
     channel_id: int,           # channel to force-join (0 = disabled)
     channel_username: str,     # @handle for join link
  }

Access is via get_settings() / set_settings() and subscription helpers.
All writes go through utils.gate save path to keep database.json consistent.
Uses file locking via fcntl where available; falls back to plain write.
"""
from __future__ import annotations

import json
import os
import time
import threading
from datetime import datetime, timezone

import config

_LOCK = threading.Lock()
DB_FILE = getattr(config, "DB_FILE", "database.json")

DEFAULT_SUB_SETTINGS = {
    "enabled": bool(getattr(config, "SUB_ENABLED", False)),
    "free_enabled": bool(getattr(config, "SUB_FREE_ENABLED", False)),
    "channel_id": int(getattr(config, "SUB_CHANNEL_ID", 0) or 0),
    "channel_username": str(getattr(config, "SUB_CHANNEL_USERNAME", "") or ""),
}


def _load_raw() -> dict:
    from utils.gate import load_database
    db = load_database()
    # migrate keys if missing
    if "subscriptions" not in db:
        db["subscriptions"] = {}
    if "usage" not in db:
        db["usage"] = {}
    if "sub_settings" not in db:
        db["sub_settings"] = dict(DEFAULT_SUB_SETTINGS)
    else:
        for k, v in DEFAULT_SUB_SETTINGS.items():
            if k not in db["sub_settings"]:
                db["sub_settings"][k] = v
    return db


def _save_raw(db: dict) -> None:
    from utils.gate import save_database
    save_database(db)


def get_settings() -> dict:
    db = _load_raw()
    return dict(db.get("sub_settings", DEFAULT_SUB_SETTINGS))


def set_settings(**kwargs) -> dict:
    with _LOCK:
        from utils.gate import load_database, save_database
        db = load_database()
        if "sub_settings" not in db:
            db["sub_settings"] = dict(DEFAULT_SUB_SETTINGS)
        for k, v in kwargs.items():
            if k in DEFAULT_SUB_SETTINGS:
                db["sub_settings"][k] = v
        # allow explicit channel fields
        if "channel_id" in kwargs:
            try:
                db["sub_settings"]["channel_id"] = int(kwargs["channel_id"])
            except Exception:
                pass
        if "channel_username" in kwargs:
            db["sub_settings"]["channel_username"] = str(kwargs["channel_username"]).strip()
        save_database(db)
        return dict(db["sub_settings"])


def get_subscription(user_id: int) -> dict | None:
    db = _load_raw()
    return db.get("subscriptions", {}).get(str(user_id))


def is_subscription_active(user_id: int) -> tuple[bool, dict | None]:
    """Return (active, sub_dict). Creator is always active (pro-equivalent)."""
    if user_id == getattr(config, "SYSTEM_CREATOR_ID", 0):
        return True, {"tier": "pro", "until": 9999999999, "is_creator": True}
    sub = get_subscription(user_id)
    if not sub:
        return False, None
    until = sub.get("until", 0)
    if until and until < int(time.time()):
        return False, sub
    return True, sub


def set_subscription(user_id: int, tier: str, duration_days: int = 30, granted_by: str = "payment", price_stars: int = 0, tx: str = "") -> dict:
    from utils.gate import load_database, save_database
    with _LOCK:
        db = load_database()
        if "subscriptions" not in db:
            db["subscriptions"] = {}
        now = int(time.time())
        until = now + duration_days * 86400
        # extend if already active with same tier
        existing = db["subscriptions"].get(str(user_id))
        if existing and existing.get("until", 0) > now:
            # extend from existing expiry
            until = existing["until"] + duration_days * 86400
        entry = {
            "tier": tier,
            "since": existing["since"] if existing else now,
            "until": until,
            "granted_by": granted_by,
            "price_stars": price_stars,
            "tx": tx,
        }
        db["subscriptions"][str(user_id)] = entry
        # auto-remove from blacklist (invariant: whitelist/subscription removes blacklist)
        if "blacklisted" in db and user_id in db["blacklisted"]:
            db["blacklisted"].remove(user_id)
        # ensure authorized (whitelist) when subscribed
        if "authorized" not in db:
            db["authorized"] = []
        if user_id not in db["authorized"]:
            db["authorized"].append(user_id)
        save_database(db)
        return entry


def remove_subscription(user_id: int) -> bool:
    from utils.gate import load_database, save_database
    with _LOCK:
        db = load_database()
        subs = db.get("subscriptions", {})
        if str(user_id) in subs:
            del subs[str(user_id)]
            db["subscriptions"] = subs
            save_database(db)
            return True
        return False


def list_subscriptions() -> dict:
    db = _load_raw()
    return dict(db.get("subscriptions", {}))

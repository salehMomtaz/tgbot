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
    "channels": [],  # list of {id:int, username:str} — multi-channel force-join
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
        # migrate legacy single channel -> channels list
        db["sub_settings"].setdefault("channels", [])
        if db["sub_settings"].get("channels") == []:
            legacy_id = int(db["sub_settings"].get("channel_id", 0) or 0)
            legacy_user = str(db["sub_settings"].get("channel_username", "") or "").strip()
            if legacy_id or legacy_user:
                db["sub_settings"]["channels"] = [{"id": legacy_id, "username": legacy_user}]
    return db


def _save_raw(db: dict) -> None:
    from utils.gate import save_database
    save_database(db)


def get_settings() -> dict:
    db = _load_raw()
    return dict(db.get("sub_settings", DEFAULT_SUB_SETTINGS))


def get_channels() -> list[dict]:
    """Return normalized channels list."""
    s = get_settings()
    chans = s.get("channels") or []
    # legacy fallback
    if not chans:
        cid = s.get("channel_id")
        cuser = s.get("channel_username")
        if cid or cuser:
            chans = [{"id": int(cid or 0), "username": str(cuser or "").strip()}]
    # sanitize
    out = []
    for c in chans:
        if not isinstance(c, dict):
            continue
        cid = int(c.get("id", 0) or 0)
        user = str(c.get("username", "") or "").strip()
        if cid or user:
            out.append({"id": cid, "username": user})
    return out


def add_channel(channel_id: int = 0, channel_username: str = "") -> list[dict]:
    with _LOCK:
        from utils.gate import load_database, save_database
        db = load_database()
        if "sub_settings" not in db:
            db["sub_settings"] = dict(DEFAULT_SUB_SETTINGS)
        db["sub_settings"].setdefault("channels", [])
        # migrate legacy if needed
        if not db["sub_settings"]["channels"] and (db["sub_settings"].get("channel_id") or db["sub_settings"].get("channel_username")):
            db["sub_settings"]["channels"] = [{"id": int(db["sub_settings"].get("channel_id", 0) or 0), "username": str(db["sub_settings"].get("channel_username", "") or "").strip()}]
        # dedup
        for c in db["sub_settings"]["channels"]:
            if (channel_id and c.get("id") == channel_id) or (channel_username and c.get("username") == channel_username):
                save_database(db)
                return list(db["sub_settings"]["channels"])
        db["sub_settings"]["channels"].append({"id": int(channel_id or 0), "username": str(channel_username or "").strip()})
        # keep legacy synced (first entry)
        if db["sub_settings"]["channels"]:
            db["sub_settings"]["channel_id"] = int(db["sub_settings"]["channels"][0].get("id", 0) or 0)
            db["sub_settings"]["channel_username"] = str(db["sub_settings"]["channels"][0].get("username", "") or "")
        save_database(db)
        return list(db["sub_settings"]["channels"])


def remove_channel(channel_id: int = 0, channel_username: str = "") -> list[dict]:
    with _LOCK:
        from utils.gate import load_database, save_database
        db = load_database()
        if "sub_settings" not in db:
            db["sub_settings"] = dict(DEFAULT_SUB_SETTINGS)
        chans = db["sub_settings"].get("channels", [])
        new = []
        for c in chans:
            if channel_id and c.get("id") == channel_id:
                continue
            if channel_username and c.get("username") == channel_username:
                continue
            new.append(c)
        db["sub_settings"]["channels"] = new
        if new:
            db["sub_settings"]["channel_id"] = int(new[0].get("id", 0) or 0)
            db["sub_settings"]["channel_username"] = str(new[0].get("username", "") or "")
        else:
            db["sub_settings"]["channel_id"] = 0
            db["sub_settings"]["channel_username"] = ""
        save_database(db)
        return list(new)


def set_settings(**kwargs) -> dict:
    with _LOCK:
        from utils.gate import load_database, save_database
        db = load_database()
        if "sub_settings" not in db:
            db["sub_settings"] = dict(DEFAULT_SUB_SETTINGS)
        db["sub_settings"].setdefault("channels", [])
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
        if "channels" in kwargs and isinstance(kwargs["channels"], list):
            # sanitize full list
            sanitized = []
            for c in kwargs["channels"]:
                if not isinstance(c, dict):
                    continue
                sanitized.append({"id": int(c.get("id", 0) or 0), "username": str(c.get("username", "") or "").strip()})
            db["sub_settings"]["channels"] = sanitized
            if sanitized:
                db["sub_settings"]["channel_id"] = int(sanitized[0].get("id", 0) or 0)
                db["sub_settings"]["channel_username"] = str(sanitized[0].get("username", "") or "")
            else:
                db["sub_settings"]["channel_id"] = 0
                db["sub_settings"]["channel_username"] = ""
        # keep channels in sync if only legacy single was changed via UI
        if "channel_id" in kwargs or "channel_username" in kwargs:
            cid = int(db["sub_settings"].get("channel_id", 0) or 0)
            cuser = str(db["sub_settings"].get("channel_username", "") or "").strip()
            # if channels empty but legacy has value, seed channels
            if not db["sub_settings"].get("channels") and (cid or cuser):
                db["sub_settings"]["channels"] = [{"id": cid, "username": cuser}]
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

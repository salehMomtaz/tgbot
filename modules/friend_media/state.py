"""
State persistence for the Friend Media Archiver.

The state lives in ``cache/friend_media_state.json`` (gitignored cache dir, and
exempted from the hourly cache cleaner in main.py). It is a SINGLE json file,
mirroring the invariant in AGENTS.md that a shared state file must be written
with *merge-only* semantics (read-modify-write the whole dict under a lock) so
concurrent admin edits + the background auto-archive loop never clobber each
other.
"""

import os
import json
import asyncio
import time
import logging

logger = logging.getLogger(__name__)

CACHE_DIR = "cache"
STATE_PATH = os.path.join(CACHE_DIR, "friend_media_state.json")

_state_lock = asyncio.Lock()
_state_cache = None


def _default_state():
    return {"friends": {}}


async def load_state():
    """Load the full state dict. Cached after first read; always refresh under lock."""
    global _state_cache
    async with _state_lock:
        if _state_cache is not None:
            return _state_cache
        if not os.path.exists(STATE_PATH):
            _state_cache = _default_state()
            return _state_cache
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or "friends" not in data:
                data = _default_state()
        except Exception as e:
            logger.exception(f"[FriendMedia:state] failed to load {STATE_PATH}: {e}")
            data = _default_state()
        _state_cache = data
        return _state_cache


async def save_state(state):
    """Persist the full state dict atomically (temp + rename) under the lock."""
    global _state_cache
    async with _state_lock:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, STATE_PATH)
            _state_cache = state
        except Exception as e:
            logger.exception(f"[FriendMedia:state] failed to save {STATE_PATH}: {e}")
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass


async def get_friend(key):
    state = await load_state()
    return state["friends"].get(key)


async def list_friends():
    state = await load_state()
    return list(state["friends"].items())


async def add_or_update_friend(key, friend):
    """Insert or merge a friend record. Preserves last_run/last_count when merging."""
    state = await load_state()
    existing = state["friends"].get(key, {})
    merged = dict(existing)
    merged.update(friend)
    merged["added_at"] = existing.get("added_at") or merged.get("added_at") or int(time.time())
    state["friends"][key] = merged
    await save_state(state)
    return merged


async def update_friend(key, patch):
    state = await load_state()
    existing = state["friends"].get(key)
    if existing is None:
        return None
    existing.update(patch)
    state["friends"][key] = existing
    await save_state(state)
    return existing


async def remove_friend(key):
    state = await load_state()
    state["friends"].pop(key, None)
    await save_state(state)

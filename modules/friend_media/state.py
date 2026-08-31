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


def _sanitize_friends(friends, path):
    """One-shot repair of legacy/corrupted IG records at load time.

    Before the share-link parser existed, pasting an Instagram *profile link*
    (``instagram.com/<user>?igsi=…`` — the ``igsi``/``igshid`` share ID is
    tracking junk) into ➕ Add IG Friend stored the WHOLE URL as the username.
    Such a record poisons every consumer: it exceeds Telegram's 64-byte
    callback_data limit (breaking the friends list with an internal error) and
    the raw URL gets sent to IG API calls as a username, producing 404s and
    429 rate-limits on the shared session.

    Here any ``ig:<x>`` record whose stored username parses to a clean handle
    is re-keyed to ``ig:<clean>`` (merging over an existing record of that
    name); a record that parses to nothing at all is dropped. Returns the
    repaired dict (the same object when nothing changed)."""
    from .common import extract_ig_username
    repaired = {}   # key -> (friend, was_corrupted)
    changed = False
    for key, friend in friends.items():
        f = friend if isinstance(friend, dict) else {}
        if not key.startswith("ig:"):
            repaired[key] = (f, False)
            continue
        clean = extract_ig_username(f.get("ig_username") or key[3:])
        if not clean:
            logger.warning(f"[FriendMedia:state] dropping corrupted IG record {key!r} from {path}")
            changed = True
            continue
        new_key = "ig:" + clean
        corrupted = (new_key != key) or (f.get("ig_username") != clean)
        if corrupted:
            logger.warning(f"[FriendMedia:state] repairing corrupted IG record {key!r} -> {new_key!r} in {path}")
            changed = True
            f = dict(f)
            f["ig_username"] = clean
            if f.get("handle") == key[3:]:
                f["handle"] = clean
            if f.get("first_name") == key[3:]:
                f["first_name"] = clean
        if new_key in repaired:
            target, t_corr = repaired[new_key]
            # A clean record always WINS over a corrupted duplicate: the
            # corrupted one only backfills fields the winner is missing
            # (its watermarks are worthless — every fetch under the bogus
            # username failed anyway).
            if corrupted and not t_corr:
                for k, v in f.items():
                    target.setdefault(k, v)
                continue
            merged = dict(f)
            for k, v in target.items():
                merged.setdefault(k, v)
            repaired[new_key] = (merged, t_corr or corrupted)
            continue
        repaired[new_key] = (f, corrupted)
    if not changed:
        return friends
    return {k: v for k, (v, _) in repaired.items()}


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
        friends = data.get("friends")
        if isinstance(friends, dict) and friends:
            data["friends"] = _sanitize_friends(friends, STATE_PATH)
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

"""
Direct-forward state management: dedup cursors, pairing handshake, merge-only saves.

This module mirrors the original modules/direct_forward.py state section exactly.
"""

import asyncio
import json
import logging
import os
import time
from typing import Any


STATE_FILE = "direct_forward_state.json"
IG_SESSION_FILE = "direct_ig_session.json"

logger = logging.getLogger(__name__)

# In-memory pending pairing codes (issued from admin console, consumed by DM workers)
_PAIR_TTL_SECONDS = 600
_pending_pairs: dict[str, dict] = {}   # platform -> {code, expires_at, requested_by}


def _activity_stamp(thread) -> str:
    """Serialize a thread's last_activity_at for the state watermark map."""
    last_act = getattr(thread, "last_activity_at", None)
    try:
        return last_act.isoformat() if last_act else ""
    except Exception:
        return ""


def _load_state() -> dict[str, Any]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    """Write the full state dict atomically (tmp+rename)."""
    tmp = f"{STATE_FILE}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.warning(f"[DirectForward] Could not save state: {e}")


def _cursor(state: dict, platform: str) -> int:
    try:
        return int(state.get(platform, {}).get("last_id", 0))
    except Exception:
        return 0


def _bump_cursor(state: dict, platform: str, new_id: int) -> None:
    state.setdefault(platform, {})["last_id"] = str(new_id)


# The three workers (IG/X/TikTok) share ONE state file. Writing the whole
# in-memory dict on every save lets a stale snapshot clobber another
# platform's cursor — the race that made X self-DM posts relay repeatedly
# (the IG worker loaded state once and each of its saves reverted the X
# cursor, so the whole X backlog re-relayed on every IG poll). Every worker
# must persist via _merge_state_save / _state_save_owned: merge ONLY its own
# platform section over the freshest on-disk state, never a full-dict write.
_STATE_LOCK = asyncio.Lock()


def _merge_state_save(state: dict[str, Any], owned: set[str]) -> dict[str, Any]:
    """Merge only the caller's *owned* platform sections over the freshest
    on-disk state and write it back atomically (tmp+rename). Refreshes
    *state* in place with the merged result so later reads see other
    workers' cursor advances. Fully synchronous, so it cannot be
    interleaved by other coroutines on the event-loop thread."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            disk = json.load(f)
    except Exception:
        disk = {}
    for plat in owned:
        if plat in state:
            disk[plat] = state[plat]
    _save_state(disk)
    state.clear()
    state.update(disk)
    return state


async def _state_save_owned(state: dict[str, Any], owned: set[str]) -> dict[str, Any]:
    """Async variant of _merge_state_save, serialized by _STATE_LOCK."""
    async with _STATE_LOCK:
        return _merge_state_save(state, owned)


def _get_pair(state: dict, platform: str) -> dict | None:
    pair = state.get(platform, {}).get("paired")
    return pair if isinstance(pair, dict) and pair.get("user_id") else None


def _set_pair(state: dict, platform: str, user_id: str | int, username: str = "") -> None:
    state.setdefault(platform, {})["paired"] = {
        "user_id": str(user_id),
        "username": username.lstrip("@"),
        "paired_at": time.time(),
    }


def request_pair_code(platform: str, requested_by: int) -> str:
    """Issue a one-time pairing code for *platform*. Called from the admin
    console; the corresponding DM worker picks it up on its next poll."""
    import random
    code = f"{random.SystemRandom().randint(0, 999999):06d}"
    _pending_pairs[platform] = {
        "code": code,
        "expires_at": time.time() + _PAIR_TTL_SECONDS,
        "requested_by": requested_by,
    }
    return code


def cancel_pairing(platform: str) -> None:
    _pending_pairs.pop(platform, None)


def unpair_platform(platform: str) -> bool:
    """Forget the paired DM contact for *platform*. Returns True when a pair
    existed. Also cancels any pending pairing handshake for the platform. The
    worker re-reads the state on its next poll, so unlinking is effective
    within one poll interval without a restart."""
    state = _load_state()
    _pending_pairs.pop(platform, None)
    if _get_pair(state, platform):
        state.get(platform, {}).pop("paired", None)
        _merge_state_save(state, {platform})
        return True
    return False


def set_platform_pair(platform: str, user_id: str | int, username: str = "") -> None:
    """Persist a paired DM contact for *platform* directly (no handshake).
    Used for the admin console's manual numeric-id entry; the worker picks it
    up on its next poll."""
    state = _load_state()
    _set_pair(state, platform, user_id, username)
    _merge_state_save(state, {platform})


def pairing_status(platform: str, state: dict) -> str:
    pair = _get_pair(state, platform)
    pending = _pending_pairs.get(platform)
    pending_txt = ""
    if pending:
        if pending["expires_at"] > time.time():
            pending_txt = f" (code {pending['code']} pending, {int(pending['expires_at'] - time.time())}s left)"
        else:
            _pending_pairs.pop(platform, None)
    if pair:
        return f"paired with @{pair.get('username', '?')} (id {pair['user_id']}){pending_txt}"
    return f"not paired{pending_txt}"
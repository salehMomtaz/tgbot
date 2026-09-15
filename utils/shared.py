# utils/shared.py
import asyncio
import time
from utils.queue_manager import DownloadQueue

# Globally shared thread-safe task queue and in-memory caches
queue = DownloadQueue()
DOWNLOAD_CACHE = {}
LAST_UPDATE_TIME = {}


# --- System-wide operations stop (set by the admin "Abort Operations"
# button). Long-running background loops (direct_forward.instagram,
# direct_forward.twitter, direct_forward.tiktok, friend_media archiver,
# friend_media archiver sub-loops) check `_should_stop()` at the top of each
# iteration. `wait_for_stop()` blocks until the flag is set.
#
# IMPORTANT (2026-09-15): the flag is a *transient pause*, not a permanent
# off switch. It used to be cleared only by a full bot restart, so "Abort
# Operations" silently disabled the DM relays for 11 h (IG exited 03:45, X
# 03:49) until the operator happened to restart. Now the abort handler clears
# it again after a short grace period, and the loops *pause* on it (via
# `wait_if_stopped`) instead of returning — so an abort cancels the current
# work without killing the relays.
_OPERATIONS_ABORTED = asyncio.Event()

#: Seconds after an abort before the flag self-clears. Long enough for
#: in-flight loops to observe it at their next check, short enough that a
#: relay is never down for more than one poll interval.
ABORT_AUTO_CLEAR_SECONDS = 30

#: cache/ files that MUST survive every purge (the hourly cleaner AND the
#: admin Abort). Deleting xchat_bridge_state.json makes the Deno bridge
#: re-prime last_seq to newest and silently SKIP the entire backlog.
PROTECTED_CACHE_FILES = {"xchat_bridge_state.json", "xchat_inbox.jsonl",
                         "friend_media_state.json"}


def signal_all_stop() -> None:
    """Set the global stop flag. Called by the admin "Abort Operations"
    button. Workers pause on it and resume when it self-clears."""
    _OPERATIONS_ABORTED.set()


def reset_stop_flag() -> None:
    """Clear the stop flag. Called by the admin restart flow, the abort
    auto-clear timer, and the bot's own startup path."""
    _OPERATIONS_ABORTED.clear()


def _should_stop() -> bool:
    """Cheap check used at the top of every long-running loop."""
    return _OPERATIONS_ABORTED.is_set()


async def wait_for_stop() -> None:
    """Block until `_OPERATIONS_ABORTED` is set. Loops that have nothing to
    do for a while can `.wait()` on this and return early when the operator
    hits "Abort Operations" — see direct_forward.instagram for the usage."""
    await _OPERATIONS_ABORTED.wait()


async def wait_if_stopped() -> None:
    """Pause while the abort flag is set, then continue once it clears.

    Background loops MUST use this instead of ``if _should_stop(): return`` so
    an Abort cancels the current cycle but never permanently disables a relay."""
    while _OPERATIONS_ABORTED.is_set():
        await asyncio.sleep(2)


# --- Worker liveness heartbeats (direct-forward watchdog) -------------------
# Each worker stamps itself at the top of a successful iteration; the
# supervisor's watchdog DMs the operator when one goes silent for too long.
# In-memory on purpose: a restart re-stamps within the first iteration, and
# the watchdog has a startup grace so it never fires on a cold boot.
_POLL_HEARTBEAT: dict[str, float] = {}


def mark_worker_alive(platform: str) -> None:
    _POLL_HEARTBEAT[platform] = time.time()


def worker_silent_seconds(platform: str) -> float | None:
    ts = _POLL_HEARTBEAT.get(platform)
    return None if ts is None else (time.time() - ts)


# --- Runtime-configurable settings (mutable at runtime via admin console) ---
# Telegram's bot upload limit is 2 GB; a Premium userbot can send 4 GB. The
# uploader picks the right boundary per file (see utils/uploader_handler.py), so
# these are NOT upload-size knobs. They govern housekeeping only.
RUNTIME_SETTINGS = {
    "max_cache_age_hours": 2,    # Auto-clean files in cache/ older than this.
    "max_disk_usage_pct": 95,    # Refuse new downloads if disk usage exceeds this.
}


# --- Safety limits (not admin-adjustable at runtime) ---
MAX_QUEUE_DEPTH = 20            # Reject new jobs if queue grows beyond this
MIN_FREE_DISK_GB = 1            # Minimum free space headroom in GB


# --- Runtime PO-token toggle (admin console can override without restart) ---
OVERRIDE_POT_ENABLED = None

# --- PO-token provider availability (set by PotProviderManager) ---
POT_AVAILABLE = False
pot_manager_instance = None


def is_pot_enabled() -> bool:
    """Return True if PO-token support should be active for YouTube downloads."""
    import config
    if OVERRIDE_POT_ENABLED is not None:
        return OVERRIDE_POT_ENABLED
    return getattr(config, "YTDLP_POT_ENABLED", False)


def set_pot_enabled(enabled: bool) -> None:
    """Set a runtime override for PO-token support. Persists until bot restart."""
    global OVERRIDE_POT_ENABLED
    OVERRIDE_POT_ENABLED = bool(enabled)


def get_setting_bytes(key: str) -> int:
    """Return a RUNTIME_SETTINGS value (stored in MB) as bytes."""
    return int(RUNTIME_SETTINGS[key]) * 1024 * 1024


def set_setting(key: str, value: int) -> None:
    """Admin-console helper: update a runtime setting (value as integer)."""
    if key not in RUNTIME_SETTINGS:
        raise KeyError(f"Unknown setting: {key}")
    RUNTIME_SETTINGS[key] = int(value)

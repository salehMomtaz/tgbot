# utils/shared.py
from utils.queue_manager import DownloadQueue

# Globally shared thread-safe task queue and in-memory caches
queue = DownloadQueue()
DOWNLOAD_CACHE = {}
LAST_UPDATE_TIME = {}


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

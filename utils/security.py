"""Security hardening helpers — rate-limit, input sanitization, log redaction.

No external deps; cheap per-user flood tracking via monotonic clock.
"""
from __future__ import annotations

import time
import re
import threading

_LOCK = threading.Lock()
# user_id -> list[timestamps]
_FLOOD: dict[int, list[float]] = {}


def is_flood(user_id: int, window: int = 60, limit: int = 8) -> bool:
    """True if user exceeded *limit* messages within *window* seconds. Counts call as hit."""
    now = time.monotonic()
    with _LOCK:
        lst = _FLOOD.get(user_id, [])
        # drop old
        lst = [t for t in lst if now - t < window]
        if len(lst) >= limit:
            _FLOOD[user_id] = lst
            return True
        lst.append(now)
        _FLOOD[user_id] = lst
        return False


# Input sanitization helpers
_SAFE_USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_]{4,32}$")
def is_safe_username(s: str) -> bool:
    return bool(_SAFE_USERNAME_RE.fullmatch(s.strip()))


def redact_token(text: str) -> str:
    """Redact anything that looks like a bot token in log lines."""
    if not text:
        return text
    # bot tokens like 123456:ABC...
    return re.sub(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b", "[REDACTED_TOKEN]", text)


def is_safe_url(url: str) -> bool:
    """Basic URL sanity: http/https only, length capped."""
    if not url or len(url) > 2048:
        return False
    return url.startswith("http://") or url.startswith("https://")


def safe_task_filename(value: str | None, fallback: str, ext: str = "") -> str:
    """Sanitize a user-supplied filename so it can never escape its task dir.

    Strips directories (``../``, absolute paths), collapses unsafe characters,
    and re-appends *ext* when the name doesn't already carry it. The result is
    always a bare filename — ``os.path.join(task_dir, result)`` stays inside
    ``task_dir``.
    """
    name = (value or "").strip()
    # Drop any directory components first (../, /abs/path, C:\\...).
    import os as _os
    name = _os.path.basename(name.replace("\\", "/"))
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or fallback
    if ext and not name.lower().endswith(ext.lower()):
        name = f"{name}{ext}"
    return name

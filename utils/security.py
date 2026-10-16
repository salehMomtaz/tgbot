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


# Maximum length (in **bytes**) of a sanitized filename. The sanitizer below
# runs in strict ASCII mode (every non-``[A-Za-z0-9._-]`` char becomes ``_``),
# so char count == byte count; the cap keeps ``os.path.join(task_dir, name)``
# well under Linux's 255-byte NAME_MAX even after ``ext`` and the uploader's
# ``_partN`` suffix are appended. A URL path basename or a user-supplied display
# name can be arbitrarily long; without the cap, ``open()`` dies with
# ``OSError [Errno 36] File name too long``. The real logic lives in
# ``utils/filenames.py`` (byte-accurate, Windows-reserved-name aware); this
# module keeps the historical name + default for its callers.
_MAX_FILENAME_CHARS = 150


def safe_task_filename(value: str | None, fallback: str, ext: str = "") -> str:
    """Sanitize a user-supplied filename so it can never escape its task dir.

    Delegates to :func:`utils.filenames.sanitize_filename` in strict-ASCII mode
    with the legacy 150-byte budget. Strips directories (``../``, absolute
    paths), collapses unsafe characters, bounds the length in bytes, avoids
    Windows device names, and re-appends *ext* when the name doesn't already
    carry it. The result is always a bare filename —
    ``os.path.join(task_dir, result)`` stays inside ``task_dir``.
    """
    from utils.filenames import sanitize_filename

    return sanitize_filename(
        value,
        fallback=fallback,
        ext=ext,
        max_bytes=_MAX_FILENAME_CHARS,
        ascii_only=True,
        strip_dirs=True,
    )

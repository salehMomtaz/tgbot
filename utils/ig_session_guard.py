# utils/ig_session_guard.py
"""Persistent hard stop for ALL authenticated Instagram traffic.

Operator requirement (2026-11-09, verbatim): *"do not allow any kind of attempt
when bot faces any problem related to instagram cookies — it won't do relogin
attempt, it fucking notifies me once and fucking waits until I upload fresh
cookies."*

Before this module the bot made several authenticated touches after a session
was already rejected:

* ``_ig_login`` validated ``direct_ig_session.json`` with ``account_info()``
  (403 login_required) and then **unconditionally** called
  ``login_by_sessionid()`` with the *same dead sessionid*. instagrapi's own
  ladder (``user_info_v1`` -> ``_user_short_from_private_stream`` ->
  ``user_short_gql``) then followed Instagram's login redirect until it raised
  ``Exceeded 30 redirects`` — the "second attempt" the operator saw on
  2026-11-09 10:53.
* Friend Media probed the same dead persisted session again.
* The headless cookie refresher kept visiting instagram.com with the dead
  sessionid every hour (a brand-new Chromium profile each time = a new device).

The latch below is keyed on the **sessionid VALUE signature** (never mtime —
the refresher/yt-dlp rotate other cookies and bump mtime while the same dead
sessionid stays). While the latch matches the current jar:

* every caller (:mod:`modules.direct_forward.instagram`,
  :mod:`modules.friend_media.instagram`, :mod:`utils.cookie_refresher`,
  :mod:`utils.downloader.cookies`) must make **zero** network calls to
  Instagram;
* the operator is notified **once** (``mark_dead`` returns True only for the
  first caller that armed the latch — the flag survives restarts);
* replacing the sessionid in ``igcookies.txt`` (a real fresh jar) auto-clears
  the latch and allows exactly ONE bootstrap login.

Only auth-classified failures arm the latch. Network/timeout errors are
transient and never arm it (see :func:`is_auth_failure`).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time

import config

logger = logging.getLogger(__name__)

# Protected from the hourly cache cleaner (utils/shared.PROTECTED_CACHE_FILES).
LATCH_PATH = os.path.join("cache", "ig_session_dead.json")

# Serializes the "check persisted session -> bootstrap login" sequence between
# the DM worker and Friend Media so a fresh jar produces ONE device identity,
# not one per worker.
_BOOTSTRAP_LOCK = threading.RLock()

# Serializes latch reads/writes between asyncio executor threads.
_LATCH_LOCK = threading.Lock()


class IGSessionDead(RuntimeError):
    """Raised instead of touching Instagram while the latch is armed."""


def _jar_path() -> str:
    return getattr(config, "IG_COOKIES", "cookies/instagram/igcookies.txt") or \
        "cookies/instagram/igcookies.txt"


def sessionid_sig(jar_path: str | None = None) -> str:
    """sha256 of the jar's ``sessionid`` VALUE ("" when absent)."""
    from utils import ig_anti_detect
    try:
        return ig_anti_detect.sessionid_sig(jar_path or _jar_path())
    except Exception:
        return ""


def jar_sessionid(jar_path: str | None = None) -> str | None:
    """Return the raw ``sessionid`` value from the jar (None when absent)."""
    path = jar_path or _jar_path()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                if raw.startswith("#") or not raw.strip():
                    continue
                parts = raw.rstrip("\n").split("\t")
                if len(parts) >= 7 and parts[5] == "sessionid" and parts[6]:
                    return parts[6]
    except Exception:
        pass
    return None


def persisted_sessionid(path: str) -> str | None:
    """Return the ``sessionid`` stored in an instagrapi settings file."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        return (data.get("cookies") or {}).get("sessionid")
    except Exception:
        return None


def persisted_matches_jar(path: str, jar_path: str | None = None) -> bool:
    """True when a persisted instagrapi session uses the SAME sessionid as the
    jar (i.e. it is not "older" than the jar — a failure means the jar's
    session is dead, not merely stale)."""
    sid = persisted_sessionid(path)
    return bool(sid) and sid == jar_sessionid(jar_path)


def is_auth_failure(exc: BaseException) -> bool:
    """True when *exc* means Instagram rejected the session/identity.

    Checkpoint and redirect-loop failures count: they are cookie/session
    problems and the operator's rule is "stop, notify once, wait". Timeouts,
    connection resets and 5xx blips do NOT count.
    """
    if type(exc).__name__ in ("LoginRequired", "ChallengeRequired",
                              "PleaseWaitFewMinutes"):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in (
        "login_required", "login required", "exceeded 30 redirects",
        "login_by_sessionid", "login by sessionid", "no sessionid",
        "checkpoint", "login wall", "challenge_required",
        "checkpoint_required", "user_has_logged_out",
        "please wait a few minutes",
    ))


def _atomic_write(data: dict) -> None:
    os.makedirs(os.path.dirname(LATCH_PATH) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".ig_dead.", dir=os.path.dirname(LATCH_PATH) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, LATCH_PATH)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except Exception:
            pass


def clear(reason: str = "") -> None:
    """Remove the latch (operator uploaded a fresh sessionid, or manual)."""
    with _LATCH_LOCK:
        try:
            if os.path.exists(LATCH_PATH):
                os.unlink(LATCH_PATH)
                logger.info(f"[IG guard] session-dead latch cleared{(': ' + reason) if reason else ''}")
        except Exception as e:
            logger.warning(f"[IG guard] failed to clear latch: {e}")


def read_latch() -> dict | None:
    """Return the latch dict when it matches the CURRENT jar sessionid.

    A latch whose signature no longer matches is stale: the operator replaced
    the sessionid, so it is deleted here and the caller may attempt ONE
    bootstrap login.
    """
    try:
        with open(LATCH_PATH, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    sig = sessionid_sig()
    if not sig:
        # Jar missing/empty: no session to protect. Leave the file; callers
        # will fail before any network call anyway.
        return None
    if data.get("sig") != sig:
        logger.info("[IG guard] jar sessionid changed — clearing the stale "
                    "session-dead latch (fresh jar uploaded).")
        clear("sessionid changed")
        return None
    return data


def is_dead() -> bool:
    """True while authenticated Instagram traffic must stay at ZERO."""
    return read_latch() is not None


def dead_reason() -> str:
    latch = read_latch() or {}
    return str(latch.get("reason") or "session rejected")


def mark_dead(reason: object, actor: str = "unknown") -> bool:
    """Arm the latch for the CURRENT jar sessionid.

    Returns True **only** when this call armed it (first detection), so callers
    can send exactly ONE operator notification even across restarts. Returns
    False when the latch was already armed for this sessionid (or when the jar
    has no sessionid to key on).
    """
    sig = sessionid_sig()
    if not sig:
        return False
    with _LATCH_LOCK:
        if read_latch() is not None:
            return False
        data = {
            "sig": sig,
            "reason": str(reason)[:300],
            "actor": actor,
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        try:
            _atomic_write(data)
        except Exception as e:
            logger.error(f"[IG guard] could not write session-dead latch: {e}")
            return False
        logger.error(f"[IG guard] ARMED: all authenticated Instagram traffic is "
                     f"stopped until the jar's sessionid is replaced "
                     f"(reason: {data['reason']}; actor: {actor})")
        return True


def bootstrap_lock() -> threading.RLock:
    """Lock covering "validate persisted session -> bootstrap login".

    Both IG workers share one process; holding this across the check+login
    prevents a fresh jar from minting two Android device identities.
    """
    return _BOOTSTRAP_LOCK

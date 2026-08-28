# utils/cookie_manager.py
"""
Cookie lifecycle manager: snapshots for race isolation, write-back for freshness.

Why this module exists — the "stale Instagram cookies" root cause
-----------------------------------------------------------------
Instagram (and Google/TikTok/X) *rotate* their session cookies server-side: a
healthy session is continually re-issued fresh ``sessionid``/``csrftoken``
values via ``Set-Cookie`` response headers. yt-dlp already handles this
natively — its ``YoutubeDLCookieJar`` applies every Set-Cookie it sees and, on
exit, atomically rewrites the jar file it was pointed at (this is exactly how
vaaski/telegram-ytdl keeps one jar alive for months: it lets yt-dlp own the
jar).

This bot historically pointed yt-dlp at a *throwaway snapshot* of the real jar
and locked the real jar read-only (0o444). That prevented corruption, but every
rotation the site issued was discarded — the real jar's session froze in time
and inevitably died (Instagram then returns HTTP 400 on authenticated API
endpoints). The stage "no-auth first for Instagram" fixed public reels but left
cookie-only content (login-walled posts, private follows, stories) permanently
broken.

The design used here keeps the race isolation of snapshots and recovers the
freshness of live jars:

  1. Every yt-dlp run still gets a per-site SNAPSHOT (concurrent metadata
     fetches must never fight over one file — see AGENTS.md invariant #10).
  2. When a cookie-backed yt-dlp run SUCCEEDS, the snapshot is OVERLAY-MERGED
     back into the real jar: keys ``(domain, path, name)`` present in the
     snapshot overwrite the real jar's copies. Nothing is ever deleted from
     the real jar, and an empty/invalid snapshot is refused outright, so the
     failure mode "yt-dlp wiped the jar on invalid session" is impossible by
     construction.
  3. The real jar is written atomically (temp file + os.replace) and its
     previous file mode (e.g. 0o444 for the YouTube jar) is restored, keeping
     the "read-only at rest" invariant.
  4. Freshness is tracked in ``cookies/meta.json`` (last success/failure per
     jar). ``freshness_warnings()`` powers the startup watchdog + admin "cookies
     are stale" nudges so silent expiry becomes a loud, actionable warning.

Concurrency note: yt-dlp runs happen in executor *threads*. The merge is
read-modify-write, so it is guarded by an in-process RLock. Downloads serialize
via the single-worker queue anyway; concurrent paths are the metadata fetches.
"""

import json
import os
import shutil
import threading
import time
from typing import Any

SNAPSHOT_DIR = os.path.join("cache", "cookies")
META_FILE = os.path.join("cookies", "meta.json")

_LOCK = threading.RLock()
_NETSCAPE_HEADER = "# Netscape HTTP Cookie File"


# =========================================================================
# Metadata (freshness) tracking
# =========================================================================

def _load_meta() -> dict[str, Any]:
    with _LOCK:
        if not os.path.exists(META_FILE):
            return {}
        try:
            with open(META_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}


def _save_meta(meta: dict[str, Any]) -> None:
    with _LOCK:
        os.makedirs(os.path.dirname(META_FILE) or ".", exist_ok=True)
        tmp_path = f"{META_FILE}.tmp.{os.getpid()}"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
            os.replace(tmp_path, META_FILE)
        except Exception:
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def touch_cookie_success(cookie_path: str) -> None:
    meta = _load_meta()
    rec = meta.setdefault(cookie_path, {})
    rec["last_success"] = time.time()
    rec.pop("last_failure", None)
    rec.pop("failure_reason", None)
    _save_meta(meta)


def touch_cookie_failure(cookie_path: str, reason: str) -> None:
    meta = _load_meta()
    rec = meta.setdefault(cookie_path, {})
    rec["last_failure"] = time.time()
    rec["failure_reason"] = reason[:300]
    _save_meta(meta)


def touch_cookie_uploaded(cookie_path: str) -> None:
    """Record that the admin (re)uploaded this jar just now. Used by the
    freshness watchdog so a fresh upload is treated as warm without faking a
    successful authenticated run."""
    meta = _load_meta()
    rec = meta.setdefault(cookie_path, {})
    rec["last_upload"] = time.time()
    _save_meta(meta)


def mark_merge(cookie_path: str, changed: int) -> None:
    meta = _load_meta()
    rec = meta.setdefault(cookie_path, {})
    rec["last_merge"] = time.time()
    rec["merge_count"] = int(rec.get("merge_count", 0)) + 1
    rec["last_merge_changed"] = changed
    _save_meta(meta)


def get_meta_record(cookie_path: str) -> dict[str, Any]:
    return _load_meta().get(cookie_path, {})


def freshness_warnings(warn_after_days: int = 21, jar_paths: list[str] | None = None) -> list[str]:
    """Human-readable warnings for jars that have not seen a successful,
    cookie-authenticated run in *warn_after_days* (or ever). Empty/missing jars
    are skipped — an optional jar simply isn't in use."""
    now = time.time()
    warn_seconds = warn_after_days * 86400
    warnings: list[str] = []
    for jar in jar_paths or []:
        if not jar or not os.path.exists(jar) or os.path.getsize(jar) == 0:
            continue
        if not has_real_cookie_lines(jar):
            continue
        rec = get_meta_record(jar)
        # A jar is "warm" when it last AUTHENTICATED successfully or was
        # freshly uploaded by the admin, whichever is newer.
        last_warm = max(rec.get("last_success") or 0, rec.get("last_upload") or 0)
        if not last_warm:
            warnings.append(
                f"Cookie jar `{jar}` has never completed an authenticated run and was "
                f"never explicitly uploaded. It works only as long as the site's "
                f"un-rotated session survives."
            )
            continue
        age_days = int((now - last_warm) / 86400)
        if (now - last_warm) > warn_seconds:
            warnings.append(
                f"Cookie jar `{jar}` was last warm {age_days}d ago. "
                f"Sessions expire when write-back cannot keep them warm — consider "
                f"uploading a fresh jar (Admin → Cookies)."
            )
    return warnings


# =========================================================================
# Netscape jar parsing / rendering (purposefully not http.cookiejar:
# we need byte-exact overlay semantics, not httplib policy re-hydration)
# =========================================================================

def _parse_cookie_lines(path: str) -> "list[tuple[tuple[str, str, str], str]]":
    """Return an ORDERED list of ``(key, raw_line)`` for every valid cookie line.

    Why an ordered list, not a dict: real-world exports (Chrome DevTools
    "Copy as Netscape" and some mobile-app exports) emit the same cookie
    twice when the same key exists with different paths or HTTP-only
    variants. The previous dict-based parser keyed by
    ``(domain, path, name)`` silently dropped the duplicates on write-back,
    losing cookies like ``ps_l``/``ps_n`` that Instagram's web auth needs.
    See the operator's report after uploading a 2 KB jar: only 1 KB survived
    a headless refresh cycle because the dict overwrote 13 of 26 lines.

    The list preserves the order and the duplicate count of the source. The
    overlay code walks the list, applies updates per-key, and writes
    everything back in the same order — so a future headless refresh sees
    the SAME cookies (in the same order) the operator uploaded.
    """
    out: "list[tuple[tuple[str, str, str], str]]" = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.rstrip("\n").rstrip("\r")
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                # (domain, path, name) — same triple as before; duplicates
                # with the same triple are kept in the list (most recent
                # wins in the overlay step; the rest stay for completeness).
                out.append(((parts[0], parts[2], parts[5]), line))
    except Exception:
        pass
    return out


def has_real_cookie_lines(path_or_content: str, *, is_content: bool = False) -> bool:
    if is_content:
        content = path_or_content
    else:
        try:
            with open(path_or_content, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception:
            return False
    for raw in content.splitlines():
        line = raw.rstrip("\r")
        if not line or line.startswith("#"):
            continue
        if len(line.split("\t")) >= 7:
            return True
    return False


def ensure_netscape_header(path: str) -> None:
    """Create the jar with a Netscape header if missing; prepend the header to
    a header-less jar. Never overwrite existing cookies."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        _atomic_write(path, _NETSCAPE_HEADER + "\n")
        return
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception:
        return
    if not content.strip().startswith("# Netscape"):
        prev_mode = os.stat(path).st_mode & 0o777
        _atomic_write(path, _NETSCAPE_HEADER + "\n" + content, mode=prev_mode)


# =========================================================================
# Atomic file writes & jar locking
# =========================================================================

def _atomic_write(path: str, content: str, mode: int | None = None) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp_path = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)
    if mode is not None:
        try:
            os.chmod(path, mode)
        except Exception:
            pass


def get_jar_mode(path: str) -> int | None:
    try:
        return os.stat(path).st_mode & 0o777
    except Exception:
        return None


def lock_jar(path: str) -> None:
    """Make a jar read-only at rest (0o444). Best-effort."""
    try:
        if os.path.exists(path):
            os.chmod(path, 0o444)
    except Exception:
        pass


# =========================================================================
# Snapshot lifecycle: acquire → yt-dlp run → commit
# =========================================================================

_snapshot_registry: dict[str, str] = {}  # snapshot_path -> original jar path


def acquire(cookie_path: str | None) -> str | None:
    """Return a disposable snapshot of *cookie_path* for one yt-dlp run, or
    None when the jar is missing/empty. The snapshot is refreshed on every
    call so a run always starts from the newest rotated jar."""
    if not cookie_path or not os.path.exists(cookie_path) or os.path.getsize(cookie_path) == 0:
        return None
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    snap_path = os.path.join(
        SNAPSHOT_DIR,
        f"{os.path.basename(cookie_path)}.{os.getpid()}.{threading.get_ident()}.snapshot",
    )
    try:
        shutil.copy(cookie_path, snap_path)
        os.chmod(snap_path, 0o644)  # yt-dlp must be able to rewrite the copy
    except Exception:
        return None
    _snapshot_registry[snap_path] = cookie_path
    return snap_path


def acquire_for_url(site_cookie_path: str | None) -> str | None:
    """Alias kept for call-site readability."""
    return acquire(site_cookie_path)


def commit(snapshot_path: str | None, *, success: bool, error_text: str | None = None) -> None:
    """Finish a yt-dlp cookie session.

    success=True  → overlay-merge the snapshot's cookies back into the real jar
                    (captures session rotation), then delete the snapshot.
    success=False → delete the snapshot; classify the error and, when it looks
                    like an auth/session failure, record it on the jar's meta
                    record so the admin watchdog can warn.
    """
    if not snapshot_path:
        return
    original = _snapshot_registry.pop(snapshot_path, None)
    try:
        if success and original:
            changed = _merge_snapshot_into(original, snapshot_path)
            if changed >= 0:
                mark_merge(original, changed)
            touch_cookie_success(original)
        elif not success and original:
            if classify_auth_error(error_text or ""):
                touch_cookie_failure(original, (error_text or "")[:300])
    finally:
        try:
            if os.path.exists(snapshot_path):
                os.remove(snapshot_path)
        except Exception:
            pass


def _merge_snapshot_into(original_path: str, snapshot_path: str) -> int:
    """Overlay snapshot cookies onto the real jar. Returns the number of
    cookie lines updated/added, or -1 when the merge was refused (empty or
    unreadable snapshot/jar). NEVER deletes cookie lines from the real jar:
    rotation only ever *updates values*, so overlay semantics are sufficient
    and the "yt-dlp wiped the jar" failure mode is impossible."""
    with _LOCK:
        snap_entries = _parse_cookie_lines(snapshot_path)
        if not snap_entries:
            return -1  # refuse: never let an empty jar overwrite state
        real_entries = _parse_cookie_lines(original_path)
        if not real_entries and os.path.exists(original_path) and os.path.getsize(original_path) > 0:
            return -1  # real jar unparseable but non-empty: don't guess

        # Build a (domain, path, name) -> latest-line dict from the real
        # jar so we can apply snapshot overrides. The list form preserves
        # the source order + duplicates; the dict form is the lookup
        # table we apply updates against.
        real_by_key: dict[tuple[str, str, str], str] = {}
        for key, line in real_entries:
            real_by_key[key] = line  # last-write wins per key, but the LIST keeps all entries

        changed = 0
        for key, line in snap_entries:
            if real_by_key.get(key) != line:
                real_by_key[key] = line
                changed += 1
        if changed == 0:
            return 0

        # Walk the real_entries list in source order. For each entry, use
        # the (possibly-updated) real_by_key value. This preserves the
        # source order AND the count of duplicate entries (so a jar that
        # had NID twice stays NID twice after the overlay).
        prev_mode = get_jar_mode(original_path)
        lines = [real_by_key.get(key, line) for key, line in real_entries]
        content = _NETSCAPE_HEADER + "\n" + "\n".join(lines) + "\n"
        _atomic_write(original_path, content, mode=prev_mode)
        return changed


def purge_snapshots(original_path: str | None = None) -> None:
    """Remove leftover snapshots (all of them, or only those derived from
    *original_path*). Called after admin jar replacement."""
    if not os.path.isdir(SNAPSHOT_DIR):
        return
    prefix = f"{os.path.basename(original_path)}." if original_path else None
    for entry in os.scandir(SNAPSHOT_DIR):
        if entry.name.endswith(".snapshot") and (prefix is None or entry.name.startswith(prefix)):
            try:
                os.remove(entry.path)
                _snapshot_registry.pop(entry.path, None)
            except Exception:
                pass


def overlay_cookies(cookie_path: str, updates: dict[tuple[str, str], str]) -> int:
    """Overlay specific cookie value updates into a Netscape jar, atomically.

    ``updates`` maps ``(domain, name) -> new_value``. Only the named cookies are
    rewritten (rotated session tokens — e.g. the ``sessionid`` an instagrapi
    login just re-issued); every other line is preserved byte-for-byte, and no
    cookie is ever deleted. The jar's previous file mode (0o444 at rest) is
    restored. Returns the number of lines actually changed, or -1 when the jar
    was missing/unreadable and nothing could be safely written."""
    if not os.path.exists(cookie_path) or os.path.getsize(cookie_path) == 0:
        return -1
    entries = _parse_cookie_lines(cookie_path)
    if not entries:
        return -1
    # Build a lookup of the latest (domain, name) -> line index so we can
    # update in place while preserving the source order and duplicate count
    # of the original jar.
    latest_idx: dict[tuple[str, str], int] = {}
    for i, (key, line) in enumerate(entries):
        latest_idx[(key[0], key[2])] = i  # last-write wins per (domain, name)
    changed = 0
    new_entries: "list[tuple[tuple[str, str, str], str]]" = list(entries)
    for (domain, name), value in updates.items():
        idx = latest_idx.get((domain, name))
        if idx is not None:
            key, line = new_entries[idx]
            parts = line.split("\t")
            if len(parts) >= 7 and parts[6] != value:
                parts[6] = value
                new_entries[idx] = (key, "\t".join(parts))
                changed += 1
        else:
            # Cookie absent from the jar: append a new entry (session cookie form).
            new_key = (domain, "/", name)
            new_line = f"{domain}\tTRUE\t/\tTRUE\t0\t{name}\t{value}"
            new_entries.append((new_key, new_line))
            latest_idx[(domain, name)] = len(new_entries) - 1
            changed += 1
    if changed == 0:
        return 0
    prev_mode = get_jar_mode(cookie_path)
    lines = [line for _, line in new_entries]
    content = _NETSCAPE_HEADER + "\n" + "\n".join(lines) + "\n"
    _atomic_write(cookie_path, content, mode=prev_mode)
    return changed


# =========================================================================
# Error classification (drives failure bookkeeping only, not control flow)
# =========================================================================

_AUTH_MARKERS = (
    "sign in to confirm",
    "confirm you're not a bot",
    "confirm you’re not a bot",
    "please sign in",
    "login required",
    "session expired",
    "use --cookies",
    "cookies-from-browser",
    "http error 400",
    "http error 401",
    "http error 403",
    "unauthorized",
    "account required",
)


def classify_auth_error(text: str) -> str | None:
    """Return the matched auth/stale-session marker, else None."""
    lower = (text or "").lower()
    for marker in _AUTH_MARKERS:
        if marker in lower:
            return marker
    return None

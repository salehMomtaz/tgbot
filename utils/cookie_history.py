# utils/cookie_history.py
"""
Append-only cookie-jar history tracker (the "who touched the jar, when, and
what changed" audit trail).

Why this module exists
----------------------
The Instagram jar has died repeatedly (LoginRequired → reels degrade to preview
images → operator re-uploads a fresh jar), and every time the post-mortem question
was the same: *what changed in the jar, and by whom, right before the session
died?* Until now nothing recorded that — ``cookies/meta.json`` keeps only the
LAST merge/upload timestamp, so history was overwritten by the very event we
needed to see. This module appends one JSON line for every jar change or IG
session-health event to ``cookies/history.jsonl`` and, for content-changing
events, keeps a full snapshot copy under ``cookies/history_snapshots/`` so the
operator can diff "the jar that worked" against "the jar that died".

What gets recorded (never a full cookie VALUE):
  * ``ts``       — UTC ISO timestamp
  * ``jar``      — jar basename (``igcookies.txt``) or ``null`` for health events
  * ``platform`` — ``instagram`` for IG session-health events
  * ``event``    — see EVENTS below
  * ``actor``    — who wrote it (``admin_upload`` / ``cookie_refresher`` /
                   ``yt_dlp_writeback`` / ``instagrapi_writeback`` / ``restore_backup``)
  * ``sha16``    — first 16 hex chars of the file's SHA-256 (fingerprint chain)
  * ``size``/``lines`` — jar size and cookie-line count
  * ``keys``     — fingerprints of session-critical cookies, e.g.
                   ``{"sessionid": "abcd…wxyz(32)", "ds_user_id": "12670677287"}``
  * ``names``    — sorted cookie NAMES present (no values) — spots shrinkage
  * ``changed``  — cookie names actually modified by this write (merge/overlay)
  * ``note``     — free text (e.g. the refused-write reason)

Snapshot rotation: at most ``KEEP_PER_JAR`` snapshots per jar; oldest deleted.

Secrets: values are NEVER written — only first4…last4 fingerprints and lengths,
same discipline as the rest of the repo (no secrets in tracked or log files).
``cookies/`` is already fully git-ignored (AGENTS.md invariant: secrets are
ignored by layout), so the JSONL and snapshots stay off GitHub.
"""

import hashlib
import json
import os
import shutil
import threading
import time
from typing import Any

HISTORY_FILE = os.path.join("cookies", "history.jsonl")
SNAPSHOT_DIR = os.path.join("cookies", "history_snapshots")

# Snapshot copies are kept for content-changing jar events only.
_SNAPSHOT_EVENTS = {"admin_replace", "refresher_write", "overlay", "merge", "refresher_refused"}

KEEP_PER_JAR = 40

# Cookie names whose (fingerprinted) values matter for session forensics.
_KEY_COOKIES = ("sessionid", "ds_user_id", "csrftoken", "auth_token", "SID")

_LOCK = threading.Lock()


def _fingerprint(value: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    if len(v) <= 8:
        return f"…({len(v)})"
    return f"{v[:4]}…{v[-4:]}({len(v)})"


def _parse_lines(path: str) -> list[list[str]]:
    """Cookie lines split into their 7 Netscape fields (skips comments)."""
    out: list[list[str]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.rstrip("\n").rstrip("\r")
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    out.append(parts)
    except Exception:
        pass
    return out


def _cookie_names(path: str) -> list[str]:
    return sorted({p[5] for p in _parse_lines(path)})


def _key_fingerprints(path: str) -> dict[str, str]:
    fp: dict[str, str] = {}
    for parts in _parse_lines(path):
        name = parts[5]
        if name in _KEY_COOKIES and name not in fp:
            fp[name] = _fingerprint(parts[6])
    return fp


def _sha16(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except Exception:
        return None


def _prune_snapshots(jar_basename: str) -> None:
    try:
        prefix = f"{jar_basename}."
        snaps = sorted(
            e for e in os.listdir(SNAPSHOT_DIR) if e.startswith(prefix)
        )
        for old in snaps[: max(0, len(snaps) - KEEP_PER_JAR)]:
            os.remove(os.path.join(SNAPSHOT_DIR, old))
    except Exception:
        pass


def record(jar: str | None, event: str, *, platform: str | None = None,
           actor: str | None = None, note: str | None = None,
           changed: list[str] | None = None, snapshot: bool | None = None) -> None:
    """Append one history entry. Best-effort: history must never break the bot.

    ``jar=None`` records a session-health event (no file snapshot). For jar
    events, ``snapshot=None`` means "auto": snapshot when the event is in
    ``_SNAPSHOT_EVENTS`` and the jar file exists.
    """
    try:
        entry: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": event,
        }
        if jar:
            entry["jar"] = os.path.basename(jar)
            if os.path.exists(jar):
                entry["sha16"] = _sha16(jar)
                entry["size"] = os.path.getsize(jar)
                entry["lines"] = len(_parse_lines(jar))
                entry["keys"] = _key_fingerprints(jar)
                entry["names"] = _cookie_names(jar)
        if platform:
            entry["platform"] = platform
        if actor:
            entry["actor"] = actor
        if changed:
            entry["changed"] = sorted(set(changed))
        if note:
            entry["note"] = note[:300]

        want_snapshot = snapshot if snapshot is not None else (event in _SNAPSHOT_EVENTS)
        if jar and want_snapshot and os.path.exists(jar) and os.path.getsize(jar) > 0:
            os.makedirs(SNAPSHOT_DIR, exist_ok=True)
            base = os.path.basename(jar)
            dst = os.path.join(
                SNAPSHOT_DIR,
                f"{base}.{time.strftime('%Y%m%d-%H%M%S')}.{int(time.time() * 1000) % 1000:03d}.txt",
            )
            try:
                shutil.copyfile(jar, dst)
                _prune_snapshots(base)
                entry["snap"] = os.path.basename(dst)
            except Exception:
                pass

        with _LOCK:
            os.makedirs(os.path.dirname(HISTORY_FILE) or ".", exist_ok=True)
            # Size rotation: keep one .1 generation — the JSONL is append-only
            # but yt-dlp merges can number thousands per jar per year; 5 MB is
            # roughly a year of heavy merging.
            try:
                if (os.path.exists(HISTORY_FILE)
                        and os.path.getsize(HISTORY_FILE) > 5 * 1024 * 1024):
                    os.replace(HISTORY_FILE, HISTORY_FILE + ".1")
            except Exception:
                pass
            with open(HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def recent(jar: str | None = None, limit: int = 25,
           platform: str | None = None) -> list[dict[str, Any]]:
    """Last *limit* entries, newest first. ``jar=None`` + ``platform`` filters
    health events; passing a jar path/basename filters jar events."""
    base = os.path.basename(jar) if jar else None
    out: list[dict[str, Any]] = []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            for raw in f:
                try:
                    e = json.loads(raw)
                except Exception:
                    continue
                if base is not None and e.get("jar") != base:
                    continue
                if base is None and platform and e.get("platform") != platform:
                    continue
                out.append(e)
    except Exception:
        return []
    return list(reversed(out[-limit:]))


_EVENT_GLYPHS = {
    "startup": "🚀",
    "admin_replace": "📤",
    "refresher_write": "🌐",
    "refresher_visit": "👁",
    "refresher_refused": "🛑",
    "overlay": "🔁",
    "merge": "⏪",
    "commit_failure": "❌",
    "restore": "♻️",
    "ig_login_ok": "🔓",
    "ig_session_dead": "💀",
    "ig_relogin_failed": "🚫",
}


def _fmt_ts(iso: str) -> str:
    return iso[5:16].replace("T", " ") if iso else "?"  # MM-DD HH:MM


def format_events(jar: str | None = None, limit: int = 20,
                  platform: str | None = None) -> str:
    """Human-readable timeline for the admin console (Telegram-safe, compact).
    One line per event: `<MM-DD HH:MM> <glyph> <event> [actor] — key facts`."""
    lines: list[str] = []
    for e in recent(jar, limit, platform):
        glyph = _EVENT_GLYPHS.get(e.get("event", ""), "•")
        bits = [f"`{_fmt_ts(e.get('ts', ''))}` {glyph} **{e.get('event', '?')}**"]
        if e.get("actor"):
            bits.append(f"by {e['actor']}")
        if e.get("changed"):
            bits.append("changed: " + ", ".join(e["changed"][:6]))
        keys = e.get("keys") or {}
        if "sessionid" in keys:
            bits.append(f"sessionid {keys['sessionid']}")
        if e.get("lines"):
            bits.append(f"{e['lines']} lines")
        if e.get("sha16"):
            bits.append(f"sha {e['sha16']}")
        if e.get("snap"):
            bits.append(f"snap {e['snap']}")
        if e.get("note"):
            bits.append(e["note"])
        lines.append(" · ".join(bits))
    return "\n".join(lines)


def format_timeline(jar_basename: str, platform: str, limit: int = 22) -> str:
    """Jar events AND platform session-health events interleaved, newest first.

    This is the correlation view: the operator reads the FIRST session-death
    (``💀 ig_session_dead``) and looks at the jar writes immediately above it
    to see which change (if any) preceded it."""
    base = jar_basename
    out: list[dict[str, Any]] = []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            for raw in f:
                try:
                    e = json.loads(raw)
                except Exception:
                    continue
                if e.get("jar") == base or e.get("platform") == platform:
                    out.append(e)
    except Exception:
        return "_No history recorded yet._"
    lines: list[str] = []
    for e in reversed(out[-limit:]):
        glyph = _EVENT_GLYPHS.get(e.get("event", ""), "•")
        bits = [f"`{_fmt_ts(e.get('ts', ''))}` {glyph} **{e.get('event', '?')}**"]
        if e.get("actor"):
            bits.append(f"by {e['actor']}")
        if e.get("changed"):
            bits.append("changed: " + ", ".join(e["changed"][:6]))
        keys = e.get("keys") or {}
        if "sessionid" in keys:
            bits.append(f"sessionid {keys['sessionid']}")
        if e.get("lines"):
            bits.append(f"{e['lines']} lines")
        if e.get("sha16"):
            bits.append(f"sha {e['sha16']}")
        if e.get("snap"):
            bits.append(f"snap {e['snap']}")
        if e.get("note"):
            bits.append(e["note"])
        lines.append(" · ".join(bits))
    return "\n".join(lines) if lines else "_No history recorded yet._"

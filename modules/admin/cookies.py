"""
Cookie jar handling for the admin console.

Mirrors the original modules/admin.py cookie functions exactly.
"""

import os
import shutil
import logging
import config
from utils import cookie_manager

logger = logging.getLogger(__name__)

# Map callback string shortcuts to physical filenames
COOKIE_MAP = {
    "ytcookies": config.YT_COOKIES,
    "igcookies": config.IG_COOKIES,
    "ttcookies": config.TT_COOKIES,
    "xcookies": config.X_COOKIES,
    "cookies": config.COOKIES_FILE,
}


def _has_real_cookie_line(content: str) -> bool:
    """True if *content* contains at least one valid Netscape cookie line.

    A real cookie line has 7 tab-separated fields (domain, flag, path,
    secure, expiration, name, value). This rejects header-only, empty,
    or Telegram-truncated jars so we never persist a broken file.
    """
    for raw in content.splitlines():
        line = raw.rstrip("\n").rstrip("\r")
        if not line or line.startswith("#"):
            continue
        if len(line.split("\t")) >= 7:
            return True
    return False


def _write_cookie_jar(cookie_key: str, file_path: str, content: str) -> None:
    """Validate and atomically write a cookie jar, keeping primary jars read-only.

    Defense in depth so the live jar can never be corrupted:
      * reject header-only / truncated / malformed jars up front;
      * back up the existing jar to <file>.autobak before touching it;
      * write to a temp file, fsync, then os.replace (atomic) so a crash
        mid-write cannot leave a truncated jar behind;
      * re-lock primary jars (YouTube/Instagram/TikTok/X) to 0o444 — yt-dlp
        never writes these paths directly; rotation write-back happens only
        through cookie_manager's atomic merge, which re-applies the lock;
      * purge stale yt-dlp snapshots so the next download uses the fresh jar;
      * record the upload in cookies/meta.json so the freshness watchdog
        treats the jar as warm from now on.
    """
    from utils.downloader import _purge_cookie_snapshots

    normalized = content
    if not normalized.strip().startswith("# Netscape"):
        normalized = f"# Netscape HTTP Cookie File\n{content}"

    if not _has_real_cookie_line(normalized):
        raise ValueError(
            "no valid Netscape cookie lines found — the file looks empty, "
            "truncated, or is not a real cookie jar"
        )

    # Cheap insurance: snapshot the current jar before overwriting it.
    if os.path.exists(file_path):
        try:
            shutil.copy(file_path, f"{file_path}.autobak")
        except Exception:
            pass

    # os.replace is a directory-level rename, so it succeeds even when the
    # existing file is 0o444 (read-only). The old inode is unlinked and a
    # fresh one takes its place; we then re-lock that fresh inode.
    is_primary = file_path in (config.YT_COOKIES, config.IG_COOKIES,
                               config.TT_COOKIES, config.X_COOKIES)
    tmp_path = f"{file_path}.tmp.{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(normalized)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, file_path)
    if is_primary:
        os.chmod(file_path, 0o444)
    _purge_cookie_snapshots(file_path)
    cookie_manager.touch_cookie_uploaded(file_path)
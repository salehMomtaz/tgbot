"""
Size estimation, CDN probes, and disk space helpers.

Mirrors the original utils/downloader.py sizing functions exactly.
"""

import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
import requests
import config


def estimate_format_size(fmt: dict, duration_seconds: int) -> int:
    """Estimates the file size of a format in bytes using bitrate or resolution mappings.

    ``duration_seconds`` is the authoritative post duration. When Instagram
    (and some other extractors) do not expose it, we fall back to
    ``DEFAULT_POST_DURATION`` so every format still gets a rough size —
    otherwise every button says ``??``.
    """
    DEFAULT_POST_DURATION = 60  # Instagram reels / TikTok / short-form posts

    size = fmt.get('filesize') or fmt.get('filesize_approx') or 0
    if size > 0:
        return size

    tbr = fmt.get('tbr') or fmt.get('vbr') or fmt.get('abr') or 0
    if tbr > 0:
        effective_duration = duration_seconds if duration_seconds > 0 else DEFAULT_POST_DURATION
        return int((effective_duration * (tbr * 1000)) / 8)

    height = fmt.get('height')
    if duration_seconds > 0:
        duration_minutes = duration_seconds / 60
        if height:
            if height >= 1080:
                mb_per_min = 15
            elif height >= 720:
                mb_per_min = 8
            elif height >= 480:
                mb_per_min = 4
            else:
                mb_per_min = 2
        else:
            mb_per_min = 1.5

        return int(duration_minutes * mb_per_min * 1024 * 1024)

    # Duration unknown, tbr unknown — make at least a heuristic guess from height.
    if height:
        if height >= 1080:
            return 15 * 1024 * 1024
        elif height >= 720:
            return 8 * 1024 * 1024
        elif height >= 480:
            return 4 * 1024 * 1024

    return 0


def _sane_filesize(size: int | None, duration_seconds: int, tbr: float) -> int:
    """Guard against per-fragment filesize artifacts before estimating.

    Some extractors (Dailymotion HLS most notably) report a *single segment's*
    byte count as ``filesize`` — e.g. ~8 KB for a real ~17 MB stream. If the
    reported size implies a bitrate that is physically impossible for the
    declared tbr/duration (<1% of tbr), treat it as absent so
    :func:`estimate_format_size` falls through to its tbr × duration chain
    (invariant #11) instead of lying about a fragment.
    """
    size = size or 0
    if size <= 0 or duration_seconds <= 0 or not tbr or tbr <= 0:
        return size
    implied_kbps = (size * 8) / duration_seconds / 1000
    return size if implied_kbps >= tbr * 0.01 else 0


def _is_hls_format(fmt: dict) -> bool:
    """True for HLS (m3u8) formats — their 'url' is a playlist manifest, so a
    CDN Content-Length probe would measure the manifest, not the file."""
    protocol = (fmt.get("protocol") or "")
    fmt_id = (fmt.get("format_id") or "")
    return protocol.startswith("m3u8") or fmt_id.lower().startswith("hls")


def format_size_short(size_bytes: int) -> str:
    """Format file size into short, compact strings to prevent glass button text cuts."""
    if size_bytes <= 0:
        return "??"
    size_mb = size_bytes / (1024 * 1024)
    if size_mb >= 1024:
        return f"{round(size_mb / 1024, 1)}G"
    if size_mb >= 1:
        return f"{round(size_mb)}M"
    return f"{round(size_bytes / 1024)}K"


# ---------------------------------------------------------------------------
# Exact CDN size probes
# ---------------------------------------------------------------------------
# Some sites (Instagram DASH reels above all) expose formats with NO filesize,
# NO tbr, and often NO duration — the estimator's 60-second default heuristic
# then overshoots the real file by 2-3x, which is the "button says ~5M but the
# upload is 2M" bug. These streams are direct CDN URLs, so a cheap HEAD (or a
# 1-byte Range GET when HEAD is rejected) returns the EXACT content-length and
# the button becomes exact instead of a guess.
_SIZE_PROBE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


def _probe_remote_size(url: str | None, http_headers: dict | None = None, timeout: int = 8) -> int | None:
    """Return the exact byte size of a direct stream URL, or None when unknown.

    HEAD first; if the CDN answers without a usable content-length, a 1-byte
    Range GET exposes the total via ``Content-Range: bytes 0-0/<total>``.
    Never raises — estimation stays the fallback.
    """
    if not url:
        return None

    headers = dict(http_headers or {})
    if "User-Agent" not in headers:
        headers["User-Agent"] = getattr(config, "YTDLP_USER_AGENT", "") or _SIZE_PROBE_UA

    proxies = None
    proxy = getattr(config, "YTDLP_PROXY", None)
    if proxy:
        proxies = {"http": proxy, "https": proxy}

    try:
        resp = requests.head(url, headers=headers, timeout=timeout,
                             allow_redirects=True, proxies=proxies)
        length = int(resp.headers.get("Content-Length") or 0)
        if resp.status_code == 200 and length > 0:
            return length

        range_headers = dict(headers, Range="bytes=0-0")
        resp = requests.get(url, headers=range_headers, timeout=timeout,
                            stream=True, proxies=proxies)
        resp.close()
        content_range = resp.headers.get("Content-Range", "")
        m = re.match(r"bytes \d+-\d+/(\d+)", content_range)
        if m:
            return int(m.group(1))
        length = int(resp.headers.get("Content-Length") or 0)
        return length if length > 0 else None
    except Exception:
        return None


def _apply_cdn_size_probes(items: list[dict]) -> None:
    """Fill exact byte sizes (in place) for button formats that need probing.

    *items* are the clipped, button-visible video/audio dicts. Only entries
    flagged ``probe`` at collection time (no filesize / filesize_approx / tbr,
    or a video whose post duration yt-dlp could not tell) are probed — sites
    like YouTube that already report stream metadata keep their estimates and
    we never waste ~10 extra requests on them.
    """
    targets = [i for i in items if i.get("probe") and i.get("probe_url")]
    if not targets:
        return
    workers = min(4, len(targets))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        sizes = list(pool.map(
            lambda i: _probe_remote_size(i["probe_url"], i.get("probe_headers")),
            targets,
        ))
    for item, size in zip(targets, sizes):
        if size and size > 0:
            item["bytes"] = size
            item["exact"] = True


def _disk_usage_percent(path: str) -> float:
    """Return disk usage percentage for the filesystem containing *path*."""
    try:
        usage = shutil.disk_usage(path)
        return (usage.used / usage.total) * 100
    except Exception:
        return 0.0


def required_merge_headroom(final_bytes: int) -> int:
    """Total free disk required to download-and-merge a file of *final_bytes*.

    A ``{video}+bestaudio`` download peaks at roughly **2x the final merged
    file** on disk: yt-dlp holds the separate video part + audio part while
    ffmpeg writes the merged mp4 (peak V + A + M ≈ 2M), and metadata embedding
    then writes a temp copy next to the final file. The uploader's splitter
    keeps at most one extra part on disk at a time, which fits under the 2M
    peak for files large enough to be split. 500 MB of headroom is added for
    ffmpeg temp buffers. Use this before downloading so a too-small VPS fails
    fast with a clear message instead of mid-merge ffprobe errors.
    """
    return final_bytes * 2 + 500 * 1024 * 1024


def _ensure_disk_space(path: str, needed_bytes: int = 0) -> None:
    """Raise RuntimeError if disk is critically full or cannot accommodate *needed_bytes*."""
    # disk_usage needs an existing path (statvfs); walk up to the nearest
    # ancestor so the early dl: dispatch check works before the task dir exists.
    check_path = path
    while check_path and not os.path.exists(check_path):
        parent = os.path.dirname(check_path)
        if parent == check_path:
            break
        check_path = parent
    try:
        usage = shutil.disk_usage(check_path or ".")
    except Exception as exc:
        raise RuntimeError(f"Cannot check disk space: {exc}")

    free_bytes = usage.free
    # Always require at least 500 MB headroom for temp/work files
    required_free = needed_bytes + (500 * 1024 * 1024)
    if free_bytes < required_free:
        free_gb = free_bytes / (1024 * 1024 * 1024)
        needed_gb = needed_bytes / (1024 * 1024 * 1024)
        raise RuntimeError(
            f"Insufficient disk space (free {free_gb:.2f} GB). "
            f"At least {needed_gb + 0.5:.2f} GB free is required for this operation."
        )

    usage_pct = (usage.used / usage.total) * 100
    if usage_pct > 95:
        raise RuntimeError(
            f"Disk is critically full ({usage_pct:.1f}% used). "
            "Clean up space before running downloads to avoid locking the server."
        )
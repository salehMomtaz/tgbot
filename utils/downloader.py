# utils/downloader.py
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
import requests
import yt_dlp
import ffmpeg
import config

# Cookie snapshots + rotation write-back live in utils/cookie_manager.
# Keep these thin wrappers so existing imports (admin console etc.) stay stable.
from utils import cookie_manager


def _purge_cookie_snapshots(original_path: str | None = None) -> None:
    """Remove disposable yt-dlp snapshot copies so the next download gets a
    fresh jar. Call after any admin action that modifies a cookie jar."""
    cookie_manager.purge_snapshots(original_path)


def _resolve_jar_path(url: str) -> str | None:
    """Map *url* to its real on-disk cookie jar path (no snapshot)."""
    url_lower = url.lower()
    cookie_path = None
    if "youtube.com" in url_lower or "youtu.be" in url_lower:
        cookie_path = config.YT_COOKIES
    elif "instagram.com" in url_lower:
        cookie_path = config.IG_COOKIES
    elif "tiktok.com" in url_lower:
        cookie_path = config.TT_COOKIES
    elif "twitter.com" in url_lower or "x.com" in url_lower:
        cookie_path = config.X_COOKIES
    else:
        # Check for site-specific cookies in cookies/ytdlp/<sitename>.txt
        try:
            parsed = urllib.parse.urlparse(url)
            domain = parsed.netloc.lower()
            if domain.startswith("www."):
                domain = domain[4:]
            site_name = domain.split(".")[0]
            site_cookie_file = os.path.join(getattr(config, "YTDLP_COOKIES_DIR", "cookies/ytdlp"), f"{site_name}.txt")
            if os.path.exists(site_cookie_file) and os.path.getsize(site_cookie_file) > 0:
                cookie_path = site_cookie_file
            else:
                cookie_path = config.COOKIES_FILE
        except Exception:
            cookie_path = config.COOKIES_FILE
    return cookie_path


def get_cookies_for_url(url: str) -> str | None:
    """Return a per-run snapshot of the correct cookie jar for *url*, or None.

    The snapshot belongs to ONE yt-dlp run; the caller must end the run with
    ``cookie_manager.commit(snapshot, success=...)`` so rotated session cookies
    are merged back into the real jar (keeps Instagram/Google sessions alive)
    and the snapshot is deleted."""
    return cookie_manager.acquire(_resolve_jar_path(url))


def diagnose_youtube_access(test_url: str = "https://www.youtube.com/watch?v=jSi2LDkyKmI") -> dict:
    """
    Run three lightweight extraction probes and report how many real formats
    YouTube returns in each scenario.  This helps decide whether PO tokens are
    needed for the current IP/cookie combination.
    """
    import utils.shared as shared

    def count_real_formats(info: dict | None) -> int:
        if not info:
            return 0
        return len([
            f for f in info.get("formats", [])
            if f.get("format_note") != "storyboard" and f.get("ext") != "mhtml"
        ])

    def extract(opts: dict) -> dict | None:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(test_url, download=False)
        except Exception:
            return None

    base_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "all",
        "proxy": getattr(config, "YTDLP_PROXY", None),
    }
    user_agent = getattr(config, "YTDLP_USER_AGENT", "")
    if user_agent:
        base_opts["user_agent"] = user_agent

    # 1) No auth at all
    no_auth = extract(dict(base_opts))

    # 2) Cookies only
    cookie_path = get_cookies_for_url(test_url)
    cookie_opts = dict(base_opts)
    if cookie_path:
        cookie_opts["cookiefile"] = cookie_path
    cookies_only = extract(cookie_opts)
    if cookie_path:
        cookie_manager.commit(cookie_path, success=bool(cookies_only))

    # 3) Full stack: cookies + PO token + mweb (only if the provider is running).
    #    _apply_pot_options raises when the provider is down, so guard it.
    full_stack = None
    pot_available = getattr(shared, "POT_AVAILABLE", False)
    original_pot = shared.is_pot_enabled()
    shared.set_pot_enabled(True)
    try:
        if pot_available:
            full_opts = _apply_pot_options(dict(base_opts), test_url)
            snap2 = get_cookies_for_url(test_url)
            if snap2:
                full_opts["cookiefile"] = snap2
            full_stack = extract(full_opts)
            if snap2:
                cookie_manager.commit(snap2, success=bool(full_stack))
    finally:
        shared.set_pot_enabled(original_pot)

    no_auth_count = count_real_formats(no_auth)
    cookies_count = count_real_formats(cookies_only)
    full_count = count_real_formats(full_stack)

    if not pot_available:
        recommendation = (
            "PO-token provider is NOT running, so YouTube downloads will fail. "
            "Start it from Admin Console -> PO Token (or restart with ./run.sh)."
        )
    elif full_count > 0:
        recommendation = "PO-token provider is running and YouTube access works."
    elif cookies_count > 0:
        recommendation = (
            "Cookies alone return formats, but the provider's full stack did not. "
            "Check the PO-token provider logs (search [POT] in logs/bot.log)."
        )
    else:
        recommendation = (
            "YouTube is blocking this IP even with cookies + PO tokens. "
            "Try warmer cookies, a different IP, or a proxy."
        )

    return {
        "no_auth_count": no_auth_count,
        "cookies_count": cookies_count,
        "full_count": full_count,
        "recommendation": recommendation,
    }


def _is_youtube(url: str) -> bool:
    """Return True if *url* is a YouTube video."""
    if not url:
        return False
    lower = url.lower()
    return "youtube.com" in lower or "youtu.be" in lower


def normalize_url(url: str) -> str:
    """Canonicalize URLs that yt-dlp does not understand natively.

    Currently:
    * Instagram highlight-share links in the base64 token form
      ``/s/<base64("highlight:"+id)>?story_media_id=...`` (what the IG app
      produces when you copy/share a highlight) → the supported
      ``/stories/highlights/<id>/`` form.
    * TikTok share shortlinks ``vt./vm./vn.tiktok.com/<code>`` → the canonical
      ``www.tiktok.com/@user/video/<id>`` URL (see :func:`_resolve_tiktok_short_url`).
    """
    m = re.match(r"^https?://(?:www\.)?instagram\.com/s/([A-Za-z0-9\-_+/=]+)", url or "")
    if m:
        try:
            import base64
            token = m.group(1)
            decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("utf-8", "replace")
            hm = re.match(r"highlight:(\d+)", decoded)
            if hm:
                return f"https://www.instagram.com/stories/highlights/{hm.group(1)}/"
        except Exception:
            pass
    return _resolve_tiktok_short_url(url)


# ---------------------------------------------------------------------------
# TikTok shortlink pre-resolution
# ---------------------------------------------------------------------------
# yt-dlp's own short-link extractor resolves vt./vm./vn.tiktok.com with a bare
# facebookexternalhit HEAD request, no impersonation, no cookies. When TikTok
# answers with its anti-bot interstitial instead of a plain 301 (stochastic,
# IP/fingerprint based), the whole extraction dies as "Unable to extract ..."
# and the user sees "The site changed its layout or the URL is malformed.".
# Resolving the redirect ourselves with a real browser UA (one retry, results
# cached for an hour) is far more consistent, and yt-dlp then receives a
# canonical www.tiktok.com URL it handles natively — challenge solver included.
_TIKTOK_SHORT_RE = re.compile(r"^https?://(?:vt|vm|vn)\.tiktok\.com/", re.IGNORECASE)
_TIKTOK_RESOLVE_TTL = 3600
_tiktok_resolve_cache: dict[str, tuple[str, float]] = {}


def _resolve_tiktok_short_url(url: str) -> str:
    """Expand a TikTok share shortlink to its canonical URL when possible.

    On any failure the original short URL is returned unchanged, so yt-dlp
    still gets its own chance through the built-in short-link extractor.
    Both ``extract_formats`` and ``download_media`` normalize the same URL —
    the TTL cache keeps that from doubling the network round-trips.
    """
    if not url or not _TIKTOK_SHORT_RE.match(url):
        return url

    now = time.time()
    cached = _tiktok_resolve_cache.get(url)
    if cached and cached[1] > now:
        return cached[0]

    headers = {"User-Agent": getattr(config, "YTDLP_USER_AGENT", "") or _SIZE_PROBE_UA}
    proxies = None
    proxy = getattr(config, "YTDLP_PROXY", None)
    if proxy:
        proxies = {"http": proxy, "https": proxy}

    resolved = url
    for _attempt in range(2):
        try:
            # stream=True + close(): we only need the redirect chain, not the body.
            resp = requests.get(url, headers=headers, timeout=10,
                                allow_redirects=True, proxies=proxies, stream=True)
            final = resp.url
            resp.close()
            # Accept only a genuine content hop (/@user/video|photo|t/...); a
            # login/interstitial hop means TikTok is playing games — keep the
            # short form and let yt-dlp's challenge solver deal with it.
            if (final and not _TIKTOK_SHORT_RE.match(final)
                    and "tiktok.com/@" in final):
                resolved = final
        except Exception:
            continue
        break

    if len(_tiktok_resolve_cache) > 500:
        _tiktok_resolve_cache.clear()
    _tiktok_resolve_cache[url] = (resolved, now + _TIKTOK_RESOLVE_TTL)
    return resolved


def _apply_pot_options(ydl_opts: dict, url: str) -> dict:
    """Inject bgutil HTTP PO-token provider options for YouTube.

    YouTube uses cookies + PO token only — there is no cookies-only or no-auth
    fallback. If the provider is not running, this raises an actionable error so
    the caller surfaces it instead of silently degrading. Non-YouTube URLs are
    returned unchanged (they never use PO tokens).
    """
    import utils.shared as shared
    if not _is_youtube(url):
        return ydl_opts

    if not getattr(shared, "POT_AVAILABLE", False):
        raise RuntimeError(
            "YouTube downloads require the PO-token provider, which is not running. "
            "Restart the bot with ./run.sh, or start it from Admin Console -> PO Token."
        )

    opts = dict(ydl_opts)
    opts.setdefault("extractor_args", {})
    opts["extractor_args"]["youtubepot-bgutilhttp"] = {
        "base_url": [f"http://127.0.0.1:{config.YTDLP_POT_PORT}"]
    }
    player_client = getattr(config, "YTDLP_POT_PLAYER_CLIENT", "mweb") or "mweb"
    opts["extractor_args"]["youtube"] = {
        "player_client": [player_client]
    }
    # yt-dlp solves YouTube's BotGuard/ nsig challenges via this JS runtime.
    # We install Deno for the provider, so use it for yt-dlp's JS too. (yt-dlp
    # already defaults to {'deno': {}}, but set it explicitly for robustness.)
    opts["js_runtimes"] = {"deno": {}}
    return opts


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


def _is_sign_in_error(error_text: str) -> bool:
    """Detect bot/sign-in challenges from yt-dlp error text (any site)."""
    text = error_text.lower()
    markers = [
        "sign in to confirm",
        "confirm you’re not a bot",
        "confirm you're not a bot",
        "sign in to continue",
        "please sign in",
        "authentication required",
        "cookies-from-browser",
        "use --cookies",
    ]
    return any(m in text for m in markers)


def _site_cookie_context(url: str) -> tuple[str, str]:
    """Map a URL to (site_name, cookie_filename) for user-facing messages.

    Mirrors get_cookies_for_url's mapping so an error always names the jar the
    bot actually tried to use for that site.
    """
    lower = (url or "").lower()
    if "youtube.com" in lower or "youtu.be" in lower:
        return ("YouTube/Google", config.YT_COOKIES)
    if "instagram.com" in lower:
        return ("Instagram", config.IG_COOKIES)
    if "tiktok.com" in lower:
        return ("TikTok", config.TT_COOKIES)
    if "twitter.com" in lower or "x.com" in lower:
        return ("X", config.X_COOKIES)
    # Per-site jar lookup (cookies/ytdlp/<site>.txt)
    try:
        parsed = urllib.parse.urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        site_name = domain.split(".")[0]
        site_cookie_file = os.path.join(getattr(config, "YTDLP_COOKIES_DIR", "cookies/ytdlp"), f"{site_name}.txt")
        if os.path.exists(site_cookie_file) and os.path.getsize(site_cookie_file) > 0:
            return (site_name, site_cookie_file)
    except Exception:
        pass
    return ("the host site", config.COOKIES_FILE)


def _classify_ytdl_error(exc: Exception, url: str) -> str:
    """Return a human-readable explanation for common yt-dlp failures."""
    text = str(exc).lower()

    if _is_sign_in_error(text):
        site, jar = _site_cookie_context(url)
        return (
            f"{site} is requiring sign-in / is bot-challenging this server. "
            f"Please send a fresh `{jar}` jar via Admin Console → Cookies."
        )

    if "no video formats found" in text or "requested format" in text:
        return (
            "The video has no playable formats available. "
            "This usually happens for ended live streams, members-only videos, or region-blocked content."
        )

    if "unable to extract" in text or "failed to parse" in text:
        return "The site changed its layout or the URL is malformed."

    if "timed out" in text or "timeout" in text:
        return "The download timed out. The server may be slow or the file very large."

    if "network" in text or "connection" in text or "unreachable" in text:
        return "A network error occurred while contacting the video host."

    return str(exc)


def _is_live_or_storyboard_only(info: dict) -> bool:
    """Return True if the only formats are storyboards/previews."""
    formats = info.get("formats", [])
    if not formats:
        return True
    non_storyboard = [
        f for f in formats
        if f.get("format_note") != "storyboard" and f.get("ext") != "mhtml"
    ]
    return len(non_storyboard) == 0


def _storyboard_error(cookie_path: str | None) -> RuntimeError:
    """Build a clear error when yt-dlp returns only storyboards/previews."""
    if cookie_path:
        return RuntimeError(
            "YouTube accepted the cookies but only returned preview/storyboard formats. "
            "This means the cookie jar is bot-flagged, expired, or from an account that cannot watch videos. "
            "Please upload a fresh `ytcookies.txt` from a browser where you can actually play YouTube videos."
        )
    return RuntimeError(
        "YouTube is requiring sign-in from this server and no valid cookies were found. "
        "Please upload a `ytcookies.txt` jar via Admin Console → Cookies."
    )


def extract_formats(url: str) -> dict:
    import utils.shared as shared

    url = normalize_url(url)
    _ensure_disk_space(os.getcwd())
    # Real (on-disk) jar path for this site. Each cookie-authenticated attempt
    # acquires a FRESH snapshot, so a failed attempt never poisons the next one.
    # A jar with no real cookie lines (missing/header-only) counts as absent.
    cookie_path = _resolve_jar_path(url)
    if cookie_path and not cookie_manager.has_real_cookie_lines(cookie_path):
        cookie_path = None

    base_opts = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'format': 'all',
        'proxy': getattr(config, "YTDLP_PROXY", None),
    }

    user_agent = getattr(config, "YTDLP_USER_AGENT", "")
    if user_agent:
        base_opts['user_agent'] = user_agent

    # Build the strategy ladder.
    #
    # YouTube: cookies + PO token is the ONLY strategy — no cookies-only or
    # no-auth fallback. The PO-token provider must be running; if it isn't,
    # _apply_pot_options raises an actionable error before any extraction.
    #
    # Instagram: no-auth FIRST. Public reels/posts resolve anonymously, and
    # burning the session on public content only shortens its life. Cookies
    # are the fallback for login-walled content (private follows, sensitive
    # posts). Rotation write-back (cookie_manager.commit) keeps the jar fresh,
    # which is what makes this fallback trustworthy. Do not re-order — stale
    # sessions made cookies trigger HTTP 400 on Instagram's authenticated API;
    # that failure mode is why the ladder was flipped.
    #
    # Other sites: cookies first (fast path), no-auth as fallback.
    original_pot = shared.is_pot_enabled()
    if _is_youtube(url):
        strategies = [("cookies+pot", True)]
    elif "instagram.com" in url.lower():
        strategies = [("no-auth", None)]
        if cookie_path:
            strategies.append(("cookies", False))
    else:
        strategies = []
        if cookie_path:
            strategies.append(("cookies", False))
        strategies.append(("no-auth", None))
        if "tiktok.com" in url.lower():
            # TikTok's anti-bot interstitial is stochastic — the same URL flips
            # between pass and fail across attempts. One extra no-auth retry is
            # cheap and converts many "site changed its layout" failures.
            strategies.append(("no-auth", None))

    info = None
    last_error = None

    for label, pot_state in strategies:
        # Temporarily flip the runtime POT toggle when comparing strategies.
        # None means "leave the global setting alone" (used for no-auth).
        if pot_state is not None:
            shared.set_pot_enabled(pot_state)

        ydl_opts = dict(base_opts)
        snap_path = None
        if label != "no-auth" and cookie_path:
            snap_path = cookie_manager.acquire(cookie_path)
            if snap_path:
                ydl_opts['cookiefile'] = snap_path
        ydl_opts = _apply_pot_options(ydl_opts, url)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            last_error = e
            info = None
        finally:
            if pot_state is not None:
                shared.set_pot_enabled(original_pot)

        if info is None:
            if snap_path:
                cookie_manager.commit(snap_path, success=False,
                                      error_text=str(last_error) if last_error else None)
            continue

        if _is_live_or_storyboard_only(info):
            # Cookies were accepted but YouTube is withholding real formats.
            # Treat this as a failure and fall back to the next strategy.
            last_error = _storyboard_error(cookie_path if label != "no-auth" else None)
            info = None
            if snap_path:
                cookie_manager.commit(snap_path, success=False, error_text=str(last_error))
            continue

        # Real formats found; stop climbing the ladder. A cookie-backed win
        # merges any rotated session cookies back into the real jar.
        if snap_path:
            cookie_manager.commit(snap_path, success=True)
        break

    if info is None:
        raise RuntimeError(f"Extraction failed: {_classify_ytdl_error(last_error, url)}")

    duration_seconds = info.get('duration', 0)
    formats = info.get('formats', [])

    video_options = []
    audio_options = []

    for fmt in formats:
        # Skip storyboards / preview images entirely
        if fmt.get('format_note') == 'storyboard' or fmt.get('ext') == 'mhtml':
            continue

        tbr = fmt.get('tbr') or fmt.get('vbr') or fmt.get('abr') or 0
        # Some extractors (Dailymotion HLS) report a single segment's size as
        # filesize (~8 KB for a real ~17 MB stream). Sanitize those so the
        # estimator falls back to the tbr × duration chain; a sanitized size is
        # no longer an exact content-length.
        fmt_eff = dict(fmt)
        fmt_eff['filesize'] = _sane_filesize(fmt.get('filesize'), duration_seconds, tbr)
        fmt_eff['filesize_approx'] = _sane_filesize(fmt.get('filesize_approx'), duration_seconds, tbr)
        size = estimate_format_size(fmt_eff, duration_seconds)
        # Only fmt['filesize'] is a real content-length (clen for YouTube). Anything
        # else (filesize_approx from tbr, or the height/bitrate heuristic) is an
        # ESTIMATE that tends to run high, so the real file is often smaller.
        exact = bool(fmt_eff['filesize'])
        # Mark formats whose size would otherwise be a blind guess for an exact
        # CDN content-length probe (see _apply_cdn_size_probes). Instagram DASH
        # reels are the driver: no filesize, no usable tbr, often no duration.
        # HLS formats are excluded: their 'url' is a manifest, so a probe would
        # measure the .m3u8, not the file.
        has_stream_meta = bool(
            fmt.get('filesize_approx') or fmt.get('tbr') or fmt.get('vbr') or fmt.get('abr')
        )
        probe_worthy = (not exact) and (duration_seconds <= 0 or not has_stream_meta) and not _is_hls_format(fmt)

        if fmt.get('vcodec') == 'none' and fmt.get('acodec') not in (None, 'none'):
            abr = fmt.get('abr') or 0
            audio_options.append({
                'format_id': fmt['format_id'],
                'quality': f"{int(abr)}k",
                'bytes': size,
                'bitrate': abr,
                'lang_pref': fmt.get('language_preference', -1),
                'exact': exact,
                'probe': probe_worthy,
                'probe_url': fmt.get('url') if probe_worthy else None,
                'probe_headers': fmt.get('http_headers') if probe_worthy else None,
            })

        elif fmt.get('vcodec') != 'none':
            resolution = fmt.get('height')
            if resolution:
                # MUXED detection: a format that already carries its own audio
                # (acodec is a real codec like 'aac', or is None/unknown as on
                # Twitter's progressive `http-*` streams). The current stream is
                # the FINAL file — no `+bestaudio` merge, so no audio is added
                # to its size. Only acodec == 'none' is a truly video-only DASH
                # stream that gets merged with best_audio below.
                muxed = (fmt.get('acodec') or '') != 'none'

                # NOTE: for video-only streams `size` is the VIDEO-ONLY size; the
                # merged size (video + best audio) is finalized below. For muxed
                # streams the size IS the final file, and since those streams'
                # metadata (Twitter's tbr/filesize_approx is ~2.3x the real file)
                # is unreliable, a muxed stream whose size is not a real
                # content-length gets an exact CDN probe.
                probe_worthy = (not exact) and (
                    muxed or (duration_seconds <= 0 or not has_stream_meta)
                ) and not _is_hls_format(fmt)
                video_options.append({
                    'format_id': fmt['format_id'],
                    'quality': f"{resolution}p",
                    'bytes': size,
                    'height': resolution,
                    'exact': exact,
                    'muxed': muxed,
                    'probe': probe_worthy,
                    'probe_url': fmt.get('url') if probe_worthy else None,
                    'probe_headers': fmt.get('http_headers') if probe_worthy else None,
                })

    video_options = sorted(video_options, key=lambda x: x['height'], reverse=True)
    # Multi-audio YouTube videos expose the original track with
    # `language_preference` 10 (5 = default, -1/unset = dubbed). yt-dlp's own
    # `bestaudio` already prefers the original, but our pure-bitrate sort was
    # overriding that and merging a higher-bitrate DUBBED track (e.g. Hindi AI
    # dub) into the video. Sort by language preference FIRST, then bitrate, so
    # the original-language track always wins the merge and the audio buttons.
    audio_options = sorted(
        audio_options,
        key=lambda x: (x['lang_pref'], x['bitrate']),
        reverse=True,
    )

    unique_videos = []
    seen_heights = set()
    for v in video_options:
        if v['height'] not in seen_heights:
            unique_videos.append(v)
            seen_heights.add(v['height'])

    unique_audios = []
    seen_bitrates = set()
    for a in audio_options:
        if a['quality'] not in seen_bitrates:
            unique_audios.append(a)
            seen_bitrates.add(a['quality'])

    # Clip to what the keyboard shows BEFORE probing — only displayed buttons
    # deserve the extra CDN requests.
    unique_videos = unique_videos[:5]
    unique_audios = unique_audios[:5]

    # Exact CDN content-length probes for blind-guess formats (IG DASH reels
    # with no duration/bitrate metadata). Upgrades bytes/exact in place, so the
    # merged button below sums exact parts and drops the "~" prefix.
    _apply_cdn_size_probes(unique_videos + unique_audios)

    # When the user taps a VIDEO button, the bot downloads "{video}+bestaudio" and
    # merges them into an mp4. The button therefore shows the *merged* size —
    # video stream + the best audio stream — not the video-only size.
    #
    # Only a real content-length (`filesize`, or a successful CDN probe) is
    # exact. filesize_approx (tbr) and the height/bitrate fallbacks are
    # ESTIMATES that tend to overshoot, so a "~" prefix warns the user the real
    # file may come out smaller than the number.
    best_audio = unique_audios[0] if unique_audios else None
    best_audio_format_id = best_audio['format_id'] if best_audio else None
    best_audio_bytes = best_audio['bytes'] if best_audio else 0
    best_audio_exact = best_audio['exact'] if best_audio else False
    for v in unique_videos:
        # Muxed streams (Twitter http-*, TikTok, most short-form sites) already
        # contain audio — their size IS the final file. Only video-only DASH
        # streams (YouTube, IG DASH) get the best-audio track merged in.
        if not v.get('muxed'):
            v['bytes'] = v['bytes'] + best_audio_bytes
            # When there is no separate audio stream (muxed downloads like TikTok),
            # nothing gets merged in — the video's own exactness decides the prefix.
            exact_total = v['exact'] and (best_audio_exact if best_audio else True)
        else:
            exact_total = v['exact']
        prefix = "" if exact_total else "~"
        warn_flag = " ⚠️" if v['bytes'] > (2000 * 1024 * 1024) else ""
        v['size_str'] = f"{prefix}{format_size_short(v['bytes'])}{warn_flag}"
    for a in unique_audios:
        prefix = "" if a['exact'] else "~"
        a['size_str'] = f"{prefix}{format_size_short(a['bytes'])}"

    return {
        'title': info.get('title', 'Unknown Title'),
        'duration': duration_seconds,
        'thumbnail': info.get('thumbnail'),
        'videos': unique_videos,
        'audios': unique_audios,
        'best_audio_format_id': best_audio_format_id
    }


# Playlist quality tiers.
#
# Per-video format_ids differ across a playlist, so we use robust yt-dlp
# *selectors* (not fixed ids). Every selector ends with a /best fallback, so a
# tier never hard-fails when a particular video lacks that height/abr stream.
# Audio keeps the FFmpegExtractAudio postprocessor with preferredquality '0'
# (source VBR, no re-encode) — the abr<= selector does the tiering.
PLAYLIST_TIERS = {
    # (format_type, tier) -> (yt-dlp format selector, human-readable label)
    ("v", "high"):   ("bestvideo[height<=1080]+bestaudio/best[height<=1080]/best", "1080p"),
    ("v", "medium"): ("bestvideo[height<=720]+bestaudio/best[height<=720]/best",   "720p"),
    ("v", "low"):    ("bestvideo[height<=480]+bestaudio/best[height<=480]/best",   "480p"),
    ("a", "high"):   ("bestaudio/best",                                            "best"),
    ("a", "medium"): ("bestaudio[abr<=160]/bestaudio/best",                        "<=160k"),
    ("a", "low"):    ("bestaudio[abr<=70]/bestaudio/best",                         "<=70k"),
}


def is_playlist_url(url: str) -> bool:
    """True if *url* is a YouTube link carrying a playlist (``list=...``)."""
    lower = (url or "").lower()
    if not _is_youtube(lower):
        return False
    parsed = urllib.parse.urlparse(lower)
    query = urllib.parse.parse_qs(parsed.query)
    return "list" in query


def is_pure_playlist_url(url: str) -> bool:
    """True for ``/playlist?list=...`` (not a single ``watch?v=`` URL)."""
    lower = (url or "").lower()
    if not _is_youtube(lower):
        return False
    return urllib.parse.urlparse(lower).path.startswith("/playlist")


def extract_playlist_meta(url: str) -> dict:
    """Flat-extract a YouTube playlist: title + entries (url, title, duration).

    Uses ``extract_flat`` so the entries are listed WITHOUT resolving each
    video's formats — fast and light on PO tokens. Per-video extraction happens
    later inside :func:`download_media`. PO tokens are not applied here on
    purpose: browsing a playlist page does not require them, and skipping the
    provider keeps this metadata pass resilient even if the provider is briefly
    down. Cookies are still passed for age/member-restricted playlists.
    """
    cookie_path = get_cookies_for_url(url)

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
        'skip_download': True,
        'noplaylist': False,
        'proxy': getattr(config, "YTDLP_PROXY", None),
    }
    if cookie_path:
        ydl_opts['cookiefile'] = cookie_path

    user_agent = getattr(config, "YTDLP_USER_AGENT", "")
    if user_agent:
        ydl_opts['user_agent'] = user_agent

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except Exception as e:
            cookie_manager.commit(cookie_path, success=False, error_text=str(e))
            raise
    cookie_manager.commit(cookie_path, success=True)

    if not info or info.get('_type') != 'playlist':
        raise RuntimeError("This link is not a playlist, or YouTube returned no playlist data.")

    entries = []
    for e in (info.get('entries') or []):
        if not e:
            continue
        entry_url = e.get('url') or e.get('id')
        if not entry_url:
            continue
        # Flat YouTube entries often give only the video id; build a watch URL.
        if not str(entry_url).startswith("http"):
            entry_url = f"https://www.youtube.com/watch?v={entry_url}"
        entries.append({
            'url': entry_url,
            'title': e.get('title') or 'Untitled',
            'duration': e.get('duration') or 0,
        })

    if not entries:
        raise RuntimeError("YouTube returned an empty playlist (no playable videos).")

    return {
        'title': info.get('title') or 'YouTube Playlist',
        'entries': entries,
    }


def embed_metadata_ffmpeg(file_path: str, title: str, artist: str, thumb_path: str | None, media_type: str) -> str:
    """
    Embed metadata (and cover art for audio) into *file_path* using ffmpeg.
    Returns the path of the file with embedded metadata (may be the same path).
    """
    if not os.path.isfile(file_path):
        return file_path

    ext = os.path.splitext(file_path)[1].lower()
    # Containers that reliably support metadata
    supported_audio = {'.m4a', '.mp3', '.mp4', '.ogg', '.opus', '.flac', '.wav'}
    supported_video = {'.mp4', '.mkv', '.mov', '.avi', '.webm'}

    if media_type == 'a' and ext not in supported_audio:
        return file_path
    if media_type == 'v' and ext not in supported_video:
        return file_path

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=ext, prefix="meta_", dir=os.path.dirname(file_path))
    os.close(tmp_fd)

    cmd = [
        'ffmpeg', '-y',
        '-i', file_path,
        '-metadata', f'title={title}',
        '-metadata', f'artist={artist}',
        '-metadata', f'comment=Downloaded via Downloader Bot',
    ]

    if media_type == 'a' and thumb_path and os.path.isfile(thumb_path):
        # For M4A/MP4/ALAC embed cover art via video stream; for MP3 use attached picture
        if ext in {'.m4a', '.mp4', '.f4a', '.f4b'}:
            cmd += [
                '-i', thumb_path,
                '-map', '0:a', '-map', '1:v',
                '-c:a', 'copy', '-c:v', 'copy',
                '-disposition:v:0', 'attached_pic',
            ]
        elif ext == '.mp3':
            cmd += [
                '-i', thumb_path,
                '-map', '0:a', '-map', '1:v',
                '-c:a', 'copy', '-c:v', 'copy',
                '-id3v2_version', '3',
                '-metadata:s:v', 'comment=Cover (front)',
            ]
        else:
            cmd += ['-c', 'copy']
    else:
        cmd += ['-c', 'copy']

    cmd.append(tmp_path)

    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        if os.path.getsize(tmp_path) > 0:
            os.replace(tmp_path, file_path)
        else:
            os.remove(tmp_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    return file_path


def convert_thumbnail_to_jpeg(input_path: str, cache_id: str) -> str:
    """Uses FFmpeg to crop and pad the thumbnail into a standard 320x320 black-padded square JPEG inside the task folder."""
    output_path = f"cache/{cache_id}/thumb.jpg"
    try:
        cmd = [
            'ffmpeg', '-y',
            '-i', input_path,
            '-vf', 'scale=w=320:h=320:force_original_aspect_ratio=decrease,pad=320:320:(ow-iw)/2:(oh-ih)/2:black',
            output_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return output_path
    except Exception:
        return input_path


_IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp', '.image', '.jfif', '.gif', '.bmp')


def _looks_like_image(path: str) -> bool:
    """True when the file's magic bytes look like JPEG/PNG/WebP/GIF/BMP.

    Some extractors (TikTok) write the writethumbnail cover with a nonstandard
    extension (``.image``), so extension matching alone is unreliable. The
    bytes are the ground truth — ffmpeg (convert_thumbnail_to_jpeg) already
    relies on them rather than the filename.
    """
    try:
        with open(path, 'rb') as f:
            head = f.read(16)
    except Exception:
        return False
    if not head:
        return False
    if head[:3] == b'\xff\xd8\xff':            # JPEG
        return True
    if head[:8] == b'\x89PNG\r\n\x1a\n':       # PNG
        return True
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':  # WebP
        return True
    if head[:3] in (b'GIF',):                  # GIF
        return True
    if head[:2] == b'BM':                      # BMP
        return True
    return False


def _find_thumbnail_file(base_path: str, task_dir: str) -> str | None:
    """Locate the writethumbnail cover for a downloaded media file.

    Tries the known image extensions first, then falls back to a magic-byte
    scan of *task_dir* for any sibling whose stem matches the media stem —
    TikTok names its cover ``<title>.image``, which the extension list alone
    would never match and which used to leave every TikTok upload thumbless.
    """
    for ext in _IMAGE_EXTENSIONS:
        test_path = f"{base_path}{ext}"
        if os.path.isfile(test_path) and _looks_like_image(test_path):
            return test_path
    stem = os.path.basename(base_path)
    try:
        entries = os.listdir(task_dir)
    except Exception:
        entries = []
    for name in entries:
        if name == os.path.basename(base_path):
            continue
        if name.startswith(stem + '.') or name.startswith(stem + '_'):
            candidate = os.path.join(task_dir, name)
            if os.path.isfile(candidate) and _looks_like_image(candidate):
                return candidate
    return None


def extract_video_frame_thumb(video_path: str) -> str | None:
    """Generate a 320x320 JPEG thumbnail by extracting a frame from the video.

    Fallback when a platform provides no usable cover file: guarantees every
    video upload carries a thumbnail. Best-effort — returns None on any
    failure so a corrupt/non-seekable file still uploads (without a thumb,
    exactly as before this fallback existed).
    """
    base = os.path.splitext(os.path.basename(video_path))[0]
    out = os.path.join(os.path.dirname(video_path) or '.', f"{base}_thumb.jpg")
    if os.path.exists(out):
        os.remove(out)
    vf = 'scale=w=320:h=320:force_original_aspect_ratio=decrease,pad=320:320:(ow-iw)/2:(oh-ih)/2:black'
    for seek in ('1', '0'):
        try:
            subprocess.run(
                ['ffmpeg', '-y', '-ss', seek, '-i', video_path,
                 '-vf', vf, '-vframes', '1', '-q:v', '5', out],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
                timeout=60,
            )
            if os.path.isfile(out) and os.path.getsize(out) > 0:
                return out
        except Exception:
            continue
    return None


def probe_video_dimensions(file_path: str) -> tuple[int, int, int]:
    try:
        probe = ffmpeg.probe(file_path)
        video_stream = next((stream for stream in probe['streams'] if stream['codec_type'] == 'video'), None)
        duration = int(float(probe['format']['duration']))
        if video_stream:
            width = int(video_stream['width'])
            height = int(video_stream['height'])
            return width, height, duration
        return 320, 320, duration
    except Exception:
        return 320, 320, 0


def download_media(url: str, format_id: str | None = None, format_type: str = 'v', cache_id: str | None = None, progress_fn=None, format_selector: str | None = None, max_height: int | None = None, best_audio_format_id: str | None = None, muxed: bool = False, expected_size_bytes: int | None = None) -> dict:
    """Download a single media item.

    Two mutually exclusive selection modes:

    * ``format_id`` (single-video flow) — downloads ``{format_id}+bestaudio`` for
      video or the exact ``format_id`` for audio.
    * ``format_selector`` (playlist flow) — a robust yt-dlp selector string
      (see :data:`PLAYLIST_TIERS`) applied per-video, since format_ids differ
      across a playlist.

    ``format_selector`` wins when both are supplied.

    ``max_height`` caps the video fallback when ``format_id`` is no longer
    available, so the delivered file stays at the resolution advertised on the
    button instead of collapsing to a tiny muxed ``/best`` stream.
    
    ``best_audio_format_id`` is the pre-calculated ID of the best audio stream
    to force merging with ``format_id``.

    ``muxed`` marks a format that already carries its own audio (Twitter
    progressive ``http-*``, TikTok, etc.). It is downloaded as-is — no
    ``+bestaudio`` merge, which would double the audio track or make yt-dlp
    refuse the selector.

    ``expected_size_bytes`` is the merged size shown on the tapped button. It is
    used to pre-check that the VPS has room for the 2x-merge peak *before* the
    download starts, so a disk-constrained server fails with a clear message
    rather than a mid-merge ffprobe error.
    """
    url = normalize_url(url)
    task_dir = f"cache/{cache_id}"
    os.makedirs(task_dir, exist_ok=True)
    out_tmpl = f"{task_dir}/%(title)s.%(ext)s"

    # Resolve the real jar first; acquire a per-run snapshot only when this
    # attempt will actually authenticate. A header-only jar counts as absent.
    site_jar = _resolve_jar_path(url)
    if site_jar and not cookie_manager.has_real_cookie_lines(site_jar):
        site_jar = None

    # Instagram: skip cookies at download time too. extract_formats already
    # proved no-auth works; stale/expired sessions trigger HTTP 400 even when
    # downloading, not just during metadata extraction. Only retry with
    # cookies when yt-dlp returns HTTP 400 on a no-auth attempt (login-wall).
    is_instagram = "instagram.com" in url.lower()
    use_cookies_now = bool(site_jar) and not is_instagram
    cookie_path = cookie_manager.acquire(site_jar) if use_cookies_now else None

    # Conservative disk check: reserve the 2x-merge peak headroom for the
    # expected file size (falls back to 1 GB when the size is unknown). This
    # catches the "VPS too small to merge video+audio" case up front instead of
    # letting ffmpeg die mid-merge with "unable to obtain file audio codec".
    if expected_size_bytes:
        _ensure_disk_space(task_dir, required_merge_headroom(expected_size_bytes))
    else:
        _ensure_disk_space(task_dir, 1024 * 1024 * 1024)

    ydl_opts = {
        'outtmpl': out_tmpl,
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'overwrites': True,
        'keep_fragments': False,
        'proxy': getattr(config, "YTDLP_PROXY", None),
    }
    if use_cookies_now:
        ydl_opts['cookiefile'] = cookie_path

    user_agent = getattr(config, "YTDLP_USER_AGENT", "")
    if user_agent:
        ydl_opts['user_agent'] = user_agent

    ydl_opts = _apply_pot_options(ydl_opts, url)

    if format_selector:
        # Playlist path: a robust tier selector is supplied; ignore format_id.
        ydl_opts['format'] = format_selector
        if format_type == 'v':
            ydl_opts['merge_output_format'] = 'mp4'
        else:
            # Audio: extract the chosen stream as-is (no re-encode / no 320k bloat).
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'm4a',
                'preferredquality': '0',
            }]
    elif format_type == 'v':
        # Never let a stale/unmergeable format_id silently collapse to a single
        # muxed `/best` stream — that file is far smaller AND lower quality than
        # the button the user tapped, which is exactly the "uploaded file is way
        # smaller than shown" bug. Fall back to a same-or-lower-resolution MERGED
        # video+audio instead, matching the size/resolution we advertised.
        if max_height:
            fallback = f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]/best"
        else:
            fallback = "bestvideo+bestaudio/best"

        if muxed:
            # The stream already contains its own audio — request it as-is and
            # never append +bestaudio (double audio / selector refusal).
            ydl_opts['format'] = f"{format_id}/{fallback}"
        else:
            audio_part = f"+{best_audio_format_id}" if best_audio_format_id else "+bestaudio"
            ydl_opts['format'] = f"{format_id}{audio_part}/{fallback}"
        ydl_opts['merge_output_format'] = 'mp4'
    else:
        # Audio: download the selected audio format as-is; avoid re-encoding to 320kbps
        # which inflates file size. Splitting will be done by ffmpeg -c copy.
        ydl_opts['format'] = format_id
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'm4a',
            'preferredquality': '0',
        }]

    ydl_opts['writethumbnail'] = True

    if progress_fn:
        def ytdl_hook(d):
            if d['status'] == 'downloading':
                downloaded = d.get('downloaded_bytes', 0)
                total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                progress_fn(downloaded, total)
        ydl_opts['progress_hooks'] = [ytdl_hook]

    # Instagram cookies can be stale or flagged even when extraction worked via
    # no-auth: downloads fail with HTTP 400 on "Video info extraction failed".
    # If we’re on Instagram and cookies exist, try no-auth first on a 400 (most
    # reels), then retry with cookies when the post genuinely requires login.
    # For non-Instagram URLs this flag is irrelevant (cookies are already used).
    #
    # Cookie bookkeeping: `snap_in_play` is the snapshot attached to the CURRENT
    # yt-dlp attempt. On success it is merged back (rotation captures fresh
    # session cookies); on failure it is discarded and, when the error smells
    # like an auth failure, recorded against the jar for the admin watchdog.
    instagram_has_cookies = bool(is_instagram and site_jar)
    snap_in_play = cookie_path  # None unless the first attempt authenticates
    last_attempt_error: str | None = None
    info = None

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:
        error_msg = str(e).lower()
        last_attempt_error = str(e)

        # Format stale? Fall back to loose selectors (YouTube / any site).
        if "requested format" in error_msg and "not available" in error_msg:
            # Fallback to generic best-effort selectors. The original format_id may
            # have been removed or merged away between extraction and download.
            if format_type == 'a':
                fallback_format = "bestaudio/best"
            elif max_height:
                fallback_format = f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]/best"
            else:
                fallback_format = "bestvideo+bestaudio/best"
            ydl_opts['format'] = fallback_format
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
            except Exception as e2:
                last_attempt_error = str(e2)

        # Instagram-specific HTTP 400 paths.
        # Case A: we sent cookies and Instagram returned 400 → the session is
        #   stale/flagged. Retry without cookies (reels work anonymously).
        elif is_instagram and use_cookies_now and "http error 400" in error_msg:
            cookie_manager.commit(snap_in_play, success=False, error_text=last_attempt_error)
            snap_in_play = None
            retry_opts = dict(ydl_opts)
            retry_opts.pop('cookiefile', None)
            try:
                with yt_dlp.YoutubeDL(retry_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
            except Exception as e2:
                last_attempt_error = str(e2)

        # Case B: we skipped cookies but Instagram returned 400 → the post is
        #   login-walled. Retry with cookies (private content needs them).
        elif instagram_has_cookies and "http error 400" in error_msg:
            snap_in_play = cookie_manager.acquire(site_jar)
            retry_opts = dict(ydl_opts)
            if snap_in_play:
                retry_opts['cookiefile'] = snap_in_play
            try:
                with yt_dlp.YoutubeDL(retry_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
            except Exception as e2:
                last_attempt_error = str(e2)

        # TikTok's anti-bot interstitial fails stochastically between attempts
        # — a clean no-cookies retry converts many of them. The first attempt's
        # cookie snapshot is closed out as a non-auth failure (block pages are
        # not login errors, so the watchdog stays quiet).
        if info is None and "tiktok.com" in url.lower():
            if snap_in_play:
                cookie_manager.commit(snap_in_play, success=False)
                snap_in_play = None
            retry_opts = dict(ydl_opts)
            retry_opts.pop('cookiefile', None)
            try:
                with yt_dlp.YoutubeDL(retry_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
            except Exception as e2:
                last_attempt_error = str(e2)

        if info is None:
            cookie_manager.commit(snap_in_play, success=False, error_text=last_attempt_error)
            raise RuntimeError(_classify_ytdl_error(e, url))

    # The winning attempt's snapshot is merged back on success (no-op when
    # the win was a no-auth attempt: snap_in_play is None).
    cookie_manager.commit(snap_in_play, success=True)

    # Determine the expected filename from the options used for the successful download.
    filename = yt_dlp.YoutubeDL(ydl_opts).prepare_filename(info)

    if format_type == 'a':
        base, _ = os.path.splitext(filename)
        # yt-dlp may produce .m4a, .mp3, or .webm depending on source and postprocessors
        for ext in ['.m4a', '.mp3', '.webm', '.ogg', '.opus']:
            if os.path.exists(f"{base}{ext}"):
                filename = f"{base}{ext}"
                break
        else:
            filename = f"{base}.m4a"
    elif format_type == 'v':
        base, _ = os.path.splitext(filename)
        if not os.path.exists(filename):
            if os.path.exists(f"{base}.mp4"):
                filename = f"{base}.mp4"
            elif os.path.exists(f"{base}.mkv"):
                filename = f"{base}.mkv"

    base_path, _ = os.path.splitext(filename)
    thumb_path = _find_thumbnail_file(base_path, task_dir)

    clean_thumb = None
    if thumb_path:
        clean_thumb = convert_thumbnail_to_jpeg(thumb_path, cache_id)

    title = info.get('title', 'Unknown Title')
    uploader = info.get('uploader', 'Unknown Artist')

    # Embed metadata into the file itself using ffmpeg (no re-encode). The
    # embed writes a temp copy beside the final file, so require room for
    # file + temp copy (~2x file size).
    _ensure_disk_space(task_dir, required_merge_headroom(os.path.getsize(filename) if os.path.exists(filename) else 0))
    filename = embed_metadata_ffmpeg(filename, title, uploader, clean_thumb, format_type)

    # yt-dlp may leave fragment/part files on interruption; purge them after a successful download
    for leftover in os.listdir(task_dir):
        if leftover.endswith(('.part', '.part-Frag0', '.ytdl', '.tmp')):
            try:
                os.remove(os.path.join(task_dir, leftover))
            except Exception:
                pass

    return {
        'file_path': filename,
        'thumb_path': clean_thumb,
        'title': title,
        'duration': info.get('duration', 0),
        'uploader': uploader
    }


def split_file_generator(file_path: str, max_chunk_size_bytes: int, hard_limit_bytes: int | None = None):
    """
    On-Demand sequential splitter:
    Yields paths of split binary parts one-by-one.
    Caps extra disk space to just ONE part (max 2GB or 4GB) instead of duplicating storage.
    If hard_limit_bytes is provided, chunks are clamped to never exceed it (safety margin).
    """
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    if hard_limit_bytes is not None and hard_limit_bytes > 0:
        max_chunk_size_bytes = min(max_chunk_size_bytes, hard_limit_bytes)

    file_size = os.path.getsize(file_path)

    if file_size <= max_chunk_size_bytes:
        yield file_path
        return

    num_chunks = (file_size + max_chunk_size_bytes - 1) // max_chunk_size_bytes
    dir_name = os.path.dirname(file_path)
    basename = os.path.basename(file_path)

    BUFFER_SIZE = min(1024 * 1024, max_chunk_size_bytes)

    with open(file_path, "rb") as f_in:
        for part_num in range(1, num_chunks + 1):
            part_path = os.path.join(dir_name, f"{basename}.{part_num:03d}")
            bytes_remaining = max_chunk_size_bytes

            try:
                with open(part_path, "wb") as f_out:
                    while bytes_remaining > 0:
                        to_read = min(BUFFER_SIZE, bytes_remaining)
                        chunk = f_in.read(to_read)
                        if not chunk:
                            break
                        f_out.write(chunk)
                        bytes_remaining -= len(chunk)

                yield part_path

            except Exception as e:
                if os.path.exists(part_path):
                    os.remove(part_path)
                raise e


def split_video_by_size_generator(file_path: str, target_size_bytes: int, hard_limit_bytes: int):
    """
    On-Demand video splitter using ffmpeg (-c copy, keyframe cuts).
    Yields paths of independently playable segments one-by-one.
    Estimates segment duration from target size, then verifies each output
    against the hard limit and re-cuts with shorter duration if exceeded.
    """
    import subprocess, json

    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    file_size = os.path.getsize(file_path)
    if file_size <= target_size_bytes:
        yield file_path
        return

    # Probe total duration securely
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", file_path],
            capture_output=True, text=True
        )
        probe_data = json.loads(probe.stdout)
        total_duration = float(probe_data.get("format", {}).get("duration", 0.0))
    except Exception:
        total_duration = 0.0

    if total_duration <= 0.0:
        # Fallback: if we cannot probe duration, split is impossible. Yield as single part.
        yield file_path
        return

    # Average bitrate (bytes/sec) -> seconds per target chunk
    bytes_per_sec = file_size / total_duration
    base_seg_seconds = max(1.0, target_size_bytes / bytes_per_sec)

    dir_name = os.path.dirname(file_path)
    basename = os.path.basename(file_path)
    root, ext = os.path.splitext(basename)
    if not ext:
        ext = ".mp4"

    start = 0.0
    part_num = 1
    seg_seconds = base_seg_seconds

    while start < total_duration - 0.1:
        part_path = os.path.join(dir_name, f"{root}.part{part_num:03d}{ext}")
        attempt_seconds = seg_seconds

        try:
            for _ in range(5):  # retry loop to respect hard limit
                cmd = [
                    "ffmpeg", "-y", "-ss", f"{start:.3f}",
                    "-i", file_path, "-t", f"{attempt_seconds:.3f}",
                    "-c", "copy", "-avoid_negative_ts", "make_zero",
                    part_path
                ]
                subprocess.run(cmd, capture_output=True, check=True)

                if not os.path.exists(part_path) or os.path.getsize(part_path) == 0:
                    raise RuntimeError("ffmpeg produced empty segment")

                if os.path.getsize(part_path) <= hard_limit_bytes:
                    break

                # Too big (keyframe spacing); shrink and retry
                os.remove(part_path)
                attempt_seconds *= 0.75
            else:
                # Could not get under hard limit after retries
                raise RuntimeError(
                    f"Segment exceeds hard limit even after retries: {part_path}"
                )

            yield part_path

            start += attempt_seconds
            part_num += 1
            # Adapt next estimate from the actual yielded size
            actual = os.path.getsize(part_path) if os.path.exists(part_path) else target_size_bytes
            if actual > 0:
                seg_seconds = max(1.0, attempt_seconds * (target_size_bytes / actual))

        except Exception as e:
            if os.path.exists(part_path):
                os.remove(part_path)
            raise e

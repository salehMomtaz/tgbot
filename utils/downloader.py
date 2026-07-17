# utils/downloader.py
import os
import shutil
import subprocess
import tempfile
import yt_dlp
import ffmpeg
import config

_COOKIE_SNAPSHOT_DIR = "cache/cookies"


def _purge_cookie_snapshots(original_path: str | None = None) -> None:
    """
    Remove disposable yt-dlp snapshot copies so the next download gets a fresh jar.
    If *original_path* is given, only snapshots derived from it are removed.
    Call this after any admin action that modifies a cookie jar.
    """
    if not os.path.isdir(_COOKIE_SNAPSHOT_DIR):
        return
    prefix = None
    if original_path:
        prefix = f"{os.path.basename(original_path)}.snapshot"
    for entry in os.scandir(_COOKIE_SNAPSHOT_DIR):
        if entry.name.endswith(".snapshot") and (prefix is None or entry.name.startswith(prefix)):
            try:
                os.remove(entry.path)
            except Exception:
                pass


def _cookie_snapshot(original_path: str) -> str | None:
    """
    Return a disposable copy of *original_path* for yt-dlp to use.
    yt-dlp rewrites cookie files on exit; pointing it at a snapshot keeps the
    admin-uploaded original intact. Snapshots are refreshed on every call.
    """
    if not original_path or not os.path.exists(original_path) or os.path.getsize(original_path) == 0:
        return None
    os.makedirs(_COOKIE_SNAPSHOT_DIR, exist_ok=True)
    snap_path = os.path.join(_COOKIE_SNAPSHOT_DIR, f"{os.path.basename(original_path)}.snapshot")
    # If an old read-only snapshot exists, make it writable so we can refresh it.
    if os.path.exists(snap_path) and not os.access(snap_path, os.W_OK):
        try:
            os.chmod(snap_path, 0o644)
        except Exception:
            pass
    shutil.copy(original_path, snap_path)
    # yt-dlp must be able to mutate the snapshot on exit; the original stays locked.
    os.chmod(snap_path, 0o644)
    return snap_path


def get_cookies_for_url(url: str) -> str | None:
    """Return a snapshot of the correct cookie jar for *url*, or None if unavailable."""
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
        # Fallback global cookies file for all other 1,000+ yt-dlp supported sites
        cookie_path = config.COOKIES_FILE

    return _cookie_snapshot(cookie_path)


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

    # 3) Full stack: cookies + PO token + mweb (only if the provider is running).
    #    _apply_pot_options raises when the provider is down, so guard it.
    full_stack = None
    pot_available = getattr(shared, "POT_AVAILABLE", False)
    original_pot = shared.is_pot_enabled()
    shared.set_pot_enabled(True)
    try:
        if pot_available:
            full_opts = _apply_pot_options(dict(base_opts), test_url)
            if cookie_path:
                full_opts["cookiefile"] = cookie_path
            full_stack = extract(full_opts)
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
    """Estimates the file size of a format in bytes using bitrate or resolution mappings."""
    size = fmt.get('filesize') or fmt.get('filesize_approx') or 0
    if size > 0:
        return size

    tbr = fmt.get('tbr') or fmt.get('vbr') or fmt.get('abr') or 0
    if tbr > 0 and duration_seconds > 0:
        return int((duration_seconds * (tbr * 1000)) / 8)

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

    return 0


def format_size_short(size_bytes: int) -> str:
    """Format file size into short, compact strings to prevent glass button text cuts."""
    if size_bytes <= 0:
        return "??"
    size_mb = size_bytes / (1024 * 1024)
    if size_mb >= 1024:
        return f"{round(size_mb / 1024, 1)}G"
    return f"{int(size_mb)}M"


def _disk_usage_percent(path: str) -> float:
    """Return disk usage percentage for the filesystem containing *path*."""
    try:
        usage = shutil.disk_usage(path)
        return (usage.used / usage.total) * 100
    except Exception:
        return 0.0


def _ensure_disk_space(path: str, needed_bytes: int = 0) -> None:
    """Raise RuntimeError if disk is critically full or cannot accommodate *needed_bytes*."""
    try:
        usage = shutil.disk_usage(path)
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

    _ensure_disk_space(os.getcwd())
    cookie_path = get_cookies_for_url(url)

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
    # Other sites: try cookies first (fast path), then fall back to no-auth.
    original_pot = shared.is_pot_enabled()
    if _is_youtube(url):
        strategies = [("cookies+pot", True)]
    else:
        strategies = []
        if cookie_path:
            strategies.append(("cookies", False))
        strategies.append(("no-auth", None))

    info = None
    last_error = None

    for label, pot_state in strategies:
        # Temporarily flip the runtime POT toggle when comparing strategies.
        # None means "leave the global setting alone" (used for no-auth).
        if pot_state is not None:
            shared.set_pot_enabled(pot_state)

        ydl_opts = dict(base_opts)
        if label != "no-auth" and cookie_path:
            ydl_opts['cookiefile'] = cookie_path
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
            continue

        if _is_live_or_storyboard_only(info):
            # Cookies were accepted but YouTube is withholding real formats.
            # Treat this as a failure and fall back to the next strategy.
            last_error = _storyboard_error(cookie_path if label != "no-auth" else None)
            info = None
            continue

        # Real formats found; stop climbing the ladder.
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

        size = estimate_format_size(fmt, duration_seconds)
        size_str = format_size_short(size)

        if fmt.get('vcodec') == 'none' and fmt.get('acodec') != 'none':
            abr = fmt.get('abr') or 0
            audio_options.append({
                'format_id': fmt['format_id'],
                'quality': f"{int(abr)}k",
                'size_str': size_str,
                'bytes': size,
                'bitrate': abr
            })

        elif fmt.get('vcodec') != 'none':
            resolution = fmt.get('height')
            if resolution:
                warn_flag = " ⚠️" if size > (2000 * 1024 * 1024) else ""
                video_options.append({
                    'format_id': fmt['format_id'],
                    'quality': f"{resolution}p",
                    'size_str': f"{size_str}{warn_flag}",
                    'bytes': size,
                    'height': resolution
                })

    video_options = sorted(video_options, key=lambda x: x['height'], reverse=True)
    audio_options = sorted(audio_options, key=lambda x: x['bitrate'], reverse=True)

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

    return {
        'title': info.get('title', 'Unknown Title'),
        'duration': duration_seconds,
        'thumbnail': info.get('thumbnail'),
        'videos': unique_videos[:5],
        'audios': unique_audios[:5]
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


def download_media(url: str, format_id: str, format_type: str, cache_id: str, progress_fn=None) -> dict:
    """Download the file using cookies, postprocess it, and extract thumbnails into a secure task folder."""
    task_dir = f"cache/{cache_id}"
    os.makedirs(task_dir, exist_ok=True)
    out_tmpl = f"{task_dir}/%(title)s.%(ext)s"
    cookie_path = get_cookies_for_url(url)

    # Conservative disk check: reserve 1 GB + estimated size. The estimate is rough;
    # we verify again before metadata embedding.
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
    if cookie_path:
        ydl_opts['cookiefile'] = cookie_path

    user_agent = getattr(config, "YTDLP_USER_AGENT", "")
    if user_agent:
        ydl_opts['user_agent'] = user_agent

    ydl_opts = _apply_pot_options(ydl_opts, url)

    if format_type == 'v':
        ydl_opts['format'] = f"{format_id}+bestaudio/best"
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

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:
        error_msg = str(e).lower()
        if "requested format" in error_msg and "not available" in error_msg:
            # Fallback to generic best-effort selectors. The original format_id may
            # have been removed or merged away between extraction and download.
            fallback_format = "bestaudio/best" if format_type == 'a' else "bestvideo+bestaudio/best"
            ydl_opts['format'] = fallback_format
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
        else:
            raise RuntimeError(_classify_ytdl_error(e, url))

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
    thumb_path = None
    for ext in ['.jpg', '.jpeg', '.png', '.webp']:
        test_path = f"{base_path}{ext}"
        if os.path.exists(test_path):
            thumb_path = test_path
            break

    clean_thumb = None
    if thumb_path:
        clean_thumb = convert_thumbnail_to_jpeg(thumb_path, cache_id)

    title = info.get('title', 'Unknown Title')
    uploader = info.get('uploader', 'Unknown Artist')

    # Embed metadata into the file itself using ffmpeg (no re-encode)
    _ensure_disk_space(task_dir, os.path.getsize(filename) if os.path.exists(filename) else 0)
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

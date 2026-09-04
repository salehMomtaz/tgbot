"""
Single media download — the main download pipeline.

Mirrors the original utils/downloader.py download_media function exactly.
"""

import os
import yt_dlp
import config
from .cookies import _resolve_jar_path
from .url_normalize import normalize_url, _apply_pot_options
from .sizing import required_merge_headroom, _ensure_disk_space
from .errors import _classify_ytdl_error
from .thumbnails import (
    _find_thumbnail_file,
    convert_thumbnail_to_jpeg,
    embed_metadata_ffmpeg,
)
from utils import cookie_manager


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
    # TikTok embed URLs (yt-dlp#17403): embed page doesn't need cookies
    is_instagram = "instagram.com" in url.lower()
    is_tiktok_embed = "tiktok.com/embed/" in url.lower()
    use_cookies_now = bool(site_jar) and not is_instagram and not is_tiktok_embed
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
    # If we're on Instagram and cookies exist, try no-auth first on a 400 (most
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

        # Case B: we skipped cookies but Instagram refused the fetch → the post
        #   needs login. Two refusals mean login-wall here: an explicit HTTP 400
        #   on the private/web API, and the audience gate — "This content isn't
        #   available to everyone: It can't be seen by certain audiences." —
        #   which Instagram serves INSTEAD of a 400 for follower/age-restricted
        #   media. Without this match the ladder never escalated to cookies and
        #   every audience-restricted reel degraded to a preview image even
        #   though the logged-in session could see it (2026-09-04 report).
        elif instagram_has_cookies and (
            "http error 400" in error_msg
            or "available to everyone" in error_msg
            or "certain audiences" in error_msg
        ):
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
        # — two retries (no-auth, then cookies again) convert most transient
        # failures. Sensitive/NSFW content requires cookies, so the no-auth
        # retry alone is not enough.
        # NOTE: As of Aug 2026, TikTok's challenge solver in yt-dlp is broken
        # (issue #17403). Retries may not help if the challenge format changed.
        if info is None and "tiktok.com" in url.lower():
            if snap_in_play:
                cookie_manager.commit(snap_in_play, success=False)
                snap_in_play = None
            # Retry 1: no-auth (for public content blocked by interstitial)
            retry_opts = dict(ydl_opts)
            retry_opts.pop('cookiefile', None)
            try:
                with yt_dlp.YoutubeDL(retry_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
            except Exception as e2:
                last_attempt_error = str(e2)
            # Retry 2: fresh cookie snapshot (for login-walled sensitive content)
            if info is None and site_jar:
                # Add delay before retry to avoid rate limiting
                import time
                time.sleep(2)
                snap_in_play = cookie_manager.acquire(site_jar)
                retry_opts2 = dict(ydl_opts)
                if snap_in_play:
                    retry_opts2['cookiefile'] = snap_in_play
                try:
                    with yt_dlp.YoutubeDL(retry_opts2) as ydl:
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
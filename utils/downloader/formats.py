"""
Format extraction logic — the core of format detection and sorting.

Mirrors the original utils/downloader.py extract_formats function exactly.
"""

import os
import utils.shared as shared
from .cookies import _resolve_jar_path, get_cookies_for_url
from .url_normalize import normalize_url, _is_youtube, _apply_pot_options
from .sizing import (
    estimate_format_size,
    _sane_filesize,
    _is_hls_format,
    format_size_short,
    _apply_cdn_size_probes,
    _ensure_disk_space,
)
from .errors import _is_live_or_storyboard_only, _storyboard_error, _classify_ytdl_error
from utils import cookie_manager
import yt_dlp
import config


def extract_formats(url: str) -> dict:
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
        'best_audio_format_id': best_audio_format_id,
        'normalized_url': url,  # post-normalize URL (e.g. resolved TikTok shortlink)
    }
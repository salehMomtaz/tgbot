"""
utils.downloader package — public API re-exports from sub-modules.

This package replaces the original utils/downloader.py (1578 lines).
All original import paths continue to work unchanged.
"""

from .cookies import (
    _purge_cookie_snapshots,
    _resolve_jar_path,
    get_cookies_for_url,
    diagnose_youtube_access,
    _site_cookie_context,
)

from .url_normalize import (
    _is_youtube,
    _TIKTOK_SHORT_RE,
    _TIKTOK_RESOLVE_TTL,
    _tiktok_resolve_cache,
    _resolve_tiktok_short_url,
    normalize_url,
    _apply_pot_options,
)

from .sizing import (
    estimate_format_size,
    _sane_filesize,
    _is_hls_format,
    format_size_short,
    _SIZE_PROBE_UA,
    _probe_remote_size,
    _apply_cdn_size_probes,
    _disk_usage_percent,
    required_merge_headroom,
    _ensure_disk_space,
)

from .errors import (
    _is_sign_in_error,
    _classify_ytdl_error,
    _is_live_or_storyboard_only,
    _storyboard_error,
)

from .formats import (
    extract_formats,
)

from .playlists import (
    PLAYLIST_TIERS,
    is_playlist_url,
    is_pure_playlist_url,
    extract_playlist_meta,
)

from .thumbnails import (
    embed_metadata_ffmpeg,
    convert_thumbnail_to_jpeg,
    _IMAGE_EXTENSIONS,
    _looks_like_image,
    _find_thumbnail_file,
    extract_video_frame_thumb,
    probe_video_dimensions,
)

from .download import (
    download_media,
)

from .split import (
    split_file_generator,
    split_video_by_size_generator,
)

__all__ = [
    # cookies
    "_purge_cookie_snapshots",
    "_resolve_jar_path",
    "get_cookies_for_url",
    "diagnose_youtube_access",
    "_site_cookie_context",
    # url_normalize
    "_is_youtube",
    "_TIKTOK_SHORT_RE",
    "_TIKTOK_RESOLVE_TTL",
    "_tiktok_resolve_cache",
    "_resolve_tiktok_short_url",
    "normalize_url",
    "_apply_pot_options",
    # sizing
    "estimate_format_size",
    "_sane_filesize",
    "_is_hls_format",
    "format_size_short",
    "_SIZE_PROBE_UA",
    "_probe_remote_size",
    "_apply_cdn_size_probes",
    "_disk_usage_percent",
    "required_merge_headroom",
    "_ensure_disk_space",
    # errors
    "_is_sign_in_error",
    "_classify_ytdl_error",
    "_is_live_or_storyboard_only",
    "_storyboard_error",
    # formats
    "extract_formats",
    # playlists
    "PLAYLIST_TIERS",
    "is_playlist_url",
    "is_pure_playlist_url",
    "extract_playlist_meta",
    # thumbnails
    "embed_metadata_ffmpeg",
    "convert_thumbnail_to_jpeg",
    "_IMAGE_EXTENSIONS",
    "_looks_like_image",
    "_find_thumbnail_file",
    "extract_video_frame_thumb",
    "probe_video_dimensions",
    # download
    "download_media",
    # split
    "split_file_generator",
    "split_video_by_size_generator",
]
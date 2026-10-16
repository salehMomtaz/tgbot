"""
Thumbnail handling, ffmpeg metadata embedding, and video probing.

Mirrors the original utils/downloader.py thumbnail functions exactly.
"""

import os
import tempfile
import subprocess
import ffmpeg


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
        '-metadata', 'comment=Downloaded via Downloader Bot',
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


def _is_real_video_stream(stream: dict) -> bool:
    """A ``codec_type == "video"`` stream that is actually a moving picture.

    Embedded cover art lives in a video stream flagged ``attached_pic`` (our own
    ``embed_metadata_ffmpeg`` writes one into every m4a), so a naive
    ``codec_type == "video"`` check classifies an audio file as video.
    """
    if stream.get("codec_type") != "video":
        return False
    disp = stream.get("disposition") or {}
    return not disp.get("attached_pic")


# Audio container magic for the case where ffprobe is unavailable or fails, so
# classification still beats "other". Ordered longest-first where it matters.
_AUDIO_MAGIC = (
    (b"ID3",), (b"fLaC",), (b"OggS",), (b"#!AMR",),
    (b"\xff\xfb",), (b"\xff\xf3",), (b"\xff\xf2",), (b"\xff\xfa",), (b"\xff\xf1",),
)


def _looks_like_audio(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except Exception:
        return False
    if not head:
        return False
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return True
    return any(head.startswith(prefix) for prefix in _AUDIO_MAGIC)


def probe_media_info(file_path: str) -> dict | None:
    """ffprobe *file_path* and report which stream kinds it really contains.

    Returns ``None`` when ffprobe fails (missing/corrupt file). The dict has
    ``has_video`` (excluding attached cover art), ``has_audio``, ``width``,
    ``height`` and ``duration``. Unlike :func:`probe_video_dimensions` this
    never fabricates a value: callers that must decide *what a file is* need
    to distinguish "no video stream" from "a 320×320 video".
    """
    try:
        probe = ffmpeg.probe(file_path)
    except Exception:
        return None

    streams = probe.get("streams") or []
    video = next((s for s in streams if _is_real_video_stream(s)), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = 0
    fmt_duration = (probe.get("format") or {}).get("duration")
    try:
        duration = int(float(fmt_duration))
    except (TypeError, ValueError):
        duration = 0
    if not duration:
        for stream in streams:
            try:
                candidate = int(float(stream.get("duration") or 0))
            except (TypeError, ValueError):
                candidate = 0
            duration = max(duration, candidate)

    def _dim(stream, key):
        try:
            return int(stream.get(key) or 320)
        except (TypeError, ValueError):
            return 320

    return {
        "has_video": video is not None,
        "has_audio": audio is not None,
        "width": _dim(video, "width") if video else 320,
        "height": _dim(video, "height") if video else 320,
        "duration": duration,
    }


def probe_video_info(file_path: str) -> dict | None:
    """The real video stream's info, or ``None`` when the file has no video
    stream (or cannot be probed). Use this — not ``probe_video_dimensions`` —
    when deciding whether something should be sent as a video."""
    info = probe_media_info(file_path)
    if info and info["has_video"]:
        return {"width": info["width"], "height": info["height"], "duration": info["duration"]}
    return None


def classify_media_file(file_path: str) -> str:
    """What *file_path* actually is: ``"image"``/``"video"``/``"audio"``/``"other"``.

    The direct-forward relay used to infer "photo" from a 320×320 probe result;
    but ``probe_video_dimensions`` returns that same 320×320 for a file with NO
    video stream, so a TikTok photo-mode post (whose yt-dlp "best" format is the
    post's **soundtrack mp3**) was routed into ``send_photo`` and Telegram
    rejected it with ``[400 PHOTO_EXT_INVALID]`` (2026-07-11 and 2026-10-11).
    Classifying from ffprobe stream kinds + image magic is the ground truth.
    """
    if not file_path or not os.path.isfile(file_path) or os.path.getsize(file_path) == 0:
        return "other"
    if _looks_like_image(file_path):
        return "image"
    info = probe_media_info(file_path)
    if info:
        if info["has_video"]:
            return "video"
        if info["has_audio"]:
            return "audio"
        return "other"
    # ffprobe unavailable/failed — fall back to magic bytes.
    return "audio" if _looks_like_audio(file_path) else "other"


_IMAGE_EXT_BY_MAGIC = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"RIFF", ".webp"),   # needs the WEBP tag at bytes 8-12
    (b"GIF8", ".gif"),
    (b"BM", ".bmp"),
)


def _image_ext_from_magic(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except Exception:
        return None
    if not head:
        return None
    for magic, ext in _IMAGE_EXT_BY_MAGIC:
        if head.startswith(magic):
            if ext == ".webp" and b"WEBP" not in head:
                continue
            return ext
    return None


def ensure_photo_path(file_path: str) -> str | None:
    """Return a path Telegram will accept in ``send_photo``, or ``None``.

    Telegram rejects ``[400 PHOTO_EXT_INVALID]`` for an image whose *file name*
    extension it does not accept for photos (WebP/GIF/BMP/HEIC…). ``.jpg``/
    ``.jpeg``/``.png`` inputs are renamed to match their real magic if needed
    (the 2026-09-04 fix); anything else is transposed to JPEG with ffmpeg
    (first frame for animated formats). ``None`` means the caller should send
    the file as a document instead of guessing.
    """
    ext = _image_ext_from_magic(file_path)
    if not ext:
        return None
    if ext in (".jpg", ".jpeg", ".png"):
        base, current = os.path.splitext(file_path)
        if current.lower() in (".jpg", ".jpeg", ".png"):
            return file_path
        new_path = base + ext
        try:
            if os.path.exists(new_path):
                os.remove(new_path)
            os.rename(file_path, new_path)
            return new_path
        except Exception:
            return file_path
    out = os.path.splitext(file_path)[0] + ".jpg"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", file_path, "-frames:v", "1", "-q:v", "3", out],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
            timeout=120,
        )
        if os.path.isfile(out) and os.path.getsize(out) > 0:
            return out
    except Exception:
        pass
    try:
        if os.path.exists(out):
            os.remove(out)
    except Exception:
        pass
    return None


def probe_video_dimensions(file_path: str) -> tuple[int, int, int]:
    """Legacy tuple API: ``(width, height, duration)``.

    Kept for the uploader/Bale callers that only ever pass real videos. It still
    returns the historical ``(320, 320, …)`` sentinel when there is no video
    stream, so **do not** use it to decide whether a file is a photo — use
    :func:`classify_media_file` or :func:`probe_video_info` for that.
    """
    info = probe_media_info(file_path)
    if info is None:
        return 320, 320, 0
    if info["has_video"]:
        return info["width"], info["height"], info["duration"]
    return 320, 320, info["duration"]
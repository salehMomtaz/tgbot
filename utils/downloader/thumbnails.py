"""
Thumbnail handling, ffmpeg metadata embedding, and video probing.

Mirrors the original utils/downloader.py thumbnail functions exactly.
"""

import os
import tempfile
import subprocess
import ffmpeg
import config
from .sizing import _ensure_disk_space


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
"""Regression tests for media classification in ``utils/downloader``.

Guards the 2026-10-11 ``[400 PHOTO_EXT_INVALID]`` failure: a TikTok **photo-mode**
post exposes the post's *soundtrack* (.mp3) as yt-dlp's "best" format, and
``probe_video_dimensions`` returns the ``(320, 320, …)`` sentinel for any file
with no video stream. The old relay read that sentinel as "this is a photo" and
called ``send_photo`` on the mp3.

The classifier is content-based (ffprobe stream kinds + image magic). These
tests generate real media with ffmpeg and skip individual cases when ffmpeg (or
a specific encoder) is unavailable, so the suite stays runnable anywhere.

Standalone: ``venv/bin/python tests/test_downloader_media.py`` (also
pytest-compatible).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.downloader import (  # noqa: E402
    classify_media_file,
    ensure_photo_path,
    probe_media_info,
    probe_video_dimensions,
    probe_video_info,
)

_HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def _ffmpeg(*args: str) -> bool:
    """Run ffmpeg quietly; True when it produced a usable non-empty file."""
    out = args[-1]
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
        )
    except Exception:
        return False
    return proc.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 0


def _make_mp3(path: str) -> bool:
    return _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=1", path)


def _make_m4a(path: str) -> bool:
    return _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                   "-c:a", "aac", path)


def _make_mp4(path: str) -> bool:
    return _ffmpeg(
        "-f", "lavfi", "-i", "testsrc=size=64x48:rate=5",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-t", "1", "-c:v", "mpeg4", "-c:a", "aac", "-pix_fmt", "yuv420p", path,
    )


def _make_still(path: str) -> bool:
    return _ffmpeg("-f", "lavfi", "-i", "testsrc=size=64x48", "-frames:v", "1", path)


def test_mp3_is_audio_not_image():
    """THE regression: the soundtrack of a photo-mode post must never be a photo."""
    if not _HAVE_FFMPEG:
        return
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "post_sound.mp3")
        if not _make_mp3(p):
            return
        assert classify_media_file(p) == "audio"
        assert classify_media_file(p) != "image"
        # The ambiguous legacy probe still says 320x320 — which is exactly why
        # it must not be used for the routing decision.
        assert probe_video_dimensions(p)[:2] == (320, 320)
        assert probe_video_info(p) is None
        info = probe_media_info(p)
        assert info and info["has_audio"] and not info["has_video"]
        # An mp3 can never be turned into a Telegram photo.
        assert ensure_photo_path(p) is None


def test_m4a_with_cover_art_is_audio_not_video():
    """Embedded cover art is a video stream flagged attached_pic — it must not
    make the file look like a video (our own embed_metadata_ffmpeg does this)."""
    if not _HAVE_FFMPEG:
        return
    with tempfile.TemporaryDirectory() as d:
        m4a = os.path.join(d, "song.m4a")
        cover = os.path.join(d, "cover.jpg")
        tagged = os.path.join(d, "tagged.m4a")
        if not (_make_m4a(m4a) and _make_still(cover)):
            return
        ok = _ffmpeg("-i", m4a, "-i", cover, "-map", "0:a", "-map", "1:v",
                     "-c", "copy", "-disposition:v:0", "attached_pic", tagged)
        if not ok:
            return
        assert classify_media_file(tagged) == "audio"
        assert probe_video_info(tagged) is None


def test_mp4_is_video():
    if not _HAVE_FFMPEG:
        return
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "clip.mp4")
        if not _make_mp4(p):
            return
        assert classify_media_file(p) == "video"
        assert probe_video_info(p) is not None
        assert probe_video_dimensions(p)[:2] == (64, 48)


def test_jpeg_and_png_are_images():
    if not _HAVE_FFMPEG:
        return
    with tempfile.TemporaryDirectory() as d:
        for name in ("pic.jpg", "pic.png"):
            p = os.path.join(d, name)
            if not _make_still(p):
                continue
            assert classify_media_file(p) == "image"
            out = ensure_photo_path(p)
            assert out and out.lower().endswith((".jpg", ".jpeg", ".png"))
            assert os.path.isfile(out)


def test_webp_is_converted_for_send_photo():
    """Telegram rejects .webp in send_photo; it must be transposed to JPEG."""
    if not _HAVE_FFMPEG:
        return
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "cover.webp")
        if not _make_still(p):
            return
        assert classify_media_file(p) == "image"
        out = ensure_photo_path(p)
        assert out is not None
        assert out.lower().endswith(".jpg")
        assert os.path.isfile(out) and os.path.getsize(out) > 0
        assert classify_media_file(out) == "image"


def test_missing_and_empty_files_are_other():
    with tempfile.TemporaryDirectory() as d:
        missing = os.path.join(d, "nope.bin")
        assert classify_media_file(missing) == "other"
        assert probe_media_info(missing) is None
        assert probe_video_info(missing) is None
        assert probe_video_dimensions(missing) == (320, 320, 0)

        empty = os.path.join(d, "empty.mp4")
        open(empty, "wb").close()
        assert classify_media_file(empty) == "other"
        assert ensure_photo_path(empty) is None


def test_classify_never_returns_image_for_audio_magic():
    """ffprobe-free path: audio magic must still not read as an image."""
    with tempfile.TemporaryDirectory() as d:
        fake = os.path.join(d, "fake.mp3")
        with open(fake, "wb") as f:
            f.write(b"ID3\x04\x00\x00\x00\x00\x00\x23" + b"\x00" * 64)
        assert classify_media_file(fake) != "image"


def _run() -> int:
    tests = sorted(
        (n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)
    )
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback

            print(f"FAIL  {name}: {exc!r}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())

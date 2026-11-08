"""Regression tests for DM-relay captions and rich-media fallback.

Requirements (2026-11-07): every Instagram/TikTok/X DM relay must carry the
post's own text, not just the media. Telegram media captions cap at 1024 chars,
so long text goes through a Bot API 10.2 rich message with the media element
first and the full text (up to ~32k) below it.

Standalone: ``venv/bin/python tests/test_relay_captions.py`` (pytest-compatible).
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.direct_forward import common  # noqa: E402
from modules.direct_forward import instagram  # noqa: E402
from utils import rich_media  # noqa: E402


HDR = ["📥 **TikTok DM** from your TikTok self-DM", "🔗 https://example/1"]


def test_full_text_puts_post_text_first():
    assert common._full_text(["a", "b"], "body") == "body\n\na\nb"
    assert common._full_text([], "body") == "body"
    assert common._full_text(["a"], "") == "a"


def test_short_caption_stays_a_normal_caption():
    caption, followups = common._compose_caption(HDR, "short text")
    assert followups == []
    assert len(caption) <= 1024
    # A (post text) before B (relay header)
    assert caption.index("short text") < caption.index("TikTok DM")


def test_long_caption_overflow_is_one_rich_block():
    body = "x" * 8000
    caption, followups = common._compose_caption(HDR, body)
    assert len(caption) <= 1024
    # ONE remainder (rich text handles 32k), not many 4096 chunks
    assert len(followups) == 1
    assert len(followups[0]) > 4096
    # the media caption starts with the post text, never the relay header
    assert caption.startswith("x") and "TikTok DM" not in caption
    assert "TikTok DM" in followups[0]  # header travels with the overflow tail


def test_try_rich_media_skips_short_text_without_network():
    # No file / short text must short-circuit before any Bot API call.
    assert asyncio.run(common._try_rich_media(1, "/nonexistent.mp4", True, HDR, "hi")) is False
    assert asyncio.run(common._try_rich_media(1, "/nonexistent.mp4", True, HDR,
                                              "y" * 2000)) is False


def test_build_html_escapes_and_bolds():
    doc = rich_media.build_html(["📥 **X DM** <script>"], "a & b\n\nsecond")
    assert "<b>X DM</b>" in doc
    assert "&lt;script&gt;" in doc
    assert "a &amp; b" in doc
    assert doc.count("<p>") == 3
    # post text paragraph comes before the relay header paragraph
    assert doc.index("a &amp; b") < doc.index("<b>X DM</b>")


def test_build_html_truncates_at_the_api_ceiling():
    doc = rich_media.build_html([], "z" * 40000)
    assert len(doc) < 33000


def test_ig_resolve_prefers_highest_quality_rendition():
    item = {"item_type": "media", "media": {
        "video_versions": [
            {"url": "https://cdn/low.mp4", "width": 480, "height": 854},
            {"url": "https://cdn/high.mp4", "width": 1080, "height": 1920},
        ]}}
    r = instagram._ig_resolve_raw(item)
    assert r["kind"] == "attachment" and r["is_photo"] is False
    assert r["url"] == "https://cdn/high.mp4"


def test_ig_resolve_prefers_first_photo_candidate():
    item = {"item_type": "media", "media": {
        "image_versions2": {"candidates": [
            {"url": "https://cdn/orig.jpg", "width": 1440, "height": 1440},
            {"url": "https://cdn/thumb.jpg", "width": 240, "height": 240},
        ]}}}
    r = instagram._ig_resolve_raw(item)
    assert r["kind"] == "attachment" and r["is_photo"] is True
    assert r["url"] == "https://cdn/orig.jpg"


def test_ig_share_uses_post_url_for_feed_and_reel_for_clips():
    post = {"item_type": "media_share", "media_share": {"media": {
        "code": "P1", "product_type": "feed",
        "caption": {"text": "post caption"}, "user": {"username": "a"}}}}
    r = instagram._ig_resolve_raw(post)
    assert r["url"] == "https://www.instagram.com/p/P1/"
    assert r["body"] == "post caption"

    reel = {"item_type": "clip", "clip": {"media": {
        "code": "R1", "product_type": "clips", "user": {"username": "a"}}}}
    assert instagram._ig_resolve_raw(reel)["url"] == "https://www.instagram.com/reel/R1/"


def test_tiktok_photo_url_detection():
    from utils.downloader.tiktok_photo import (
        is_tiktok_photo_url, tiktok_photo_item_id)
    assert tiktok_photo_item_id(
        "https://www.tiktok.com/@mens.guidance/photo/7467226363544554770") == "7467226363544554770"
    assert is_tiktok_photo_url("https://www.tiktok.com/@a/photo/123/") is True
    assert is_tiktok_photo_url("https://www.tiktok.com/@a/video/123") is False
    assert tiktok_photo_item_id(None) is None


def test_deliver_tiktok_photo_sends_slides_then_audio():
    """Slides carry caption A then relay info B; the soundtrack is a separate
    follow-up message (the feature the operator asked for)."""
    import tempfile
    import types
    from utils.downloader import tiktok_photo as tp

    calls = []

    class FakeBot:
        async def send_photo(self, **kw):
            calls.append(("photo", kw.get("caption")))

        async def send_audio(self, **kw):
            calls.append(("audio", kw.get("title"), kw.get("performer")))

    def fake_download(info, dest):
        os.makedirs(dest, exist_ok=True)
        p = os.path.join(dest, "photo_00.jpg")
        with open(p, "wb") as f:
            f.write(b"\xff\xd8\xff" + b"0" * 100)
        a = os.path.join(dest, "audio.m4a")
        with open(a, "wb") as f:
            f.write(b"\x00" * 50)
        return [p], a, None

    orig = tp.download_photo_assets
    try:
        tp.download_photo_assets = fake_download
        info = {"is_photo": True, "images": [{"url": "x"}],
                "caption": "#post caption", "author": "a",
                "audio_title": "song", "audio_artist": "artist",
                "audio_url": "u", "audio_cover": None}
        ok = asyncio.run(common.deliver_tiktok_photo(
            FakeBot(), 1, "7467226363544554770",
            ["🔗 https://www.tiktok.com/@a/photo/7467226363544554770"], "",
            info=info))
    finally:
        tp.download_photo_assets = orig

    assert ok is True
    assert calls[0][0] == "photo"
    caption = calls[0][1]
    assert caption.index("#post caption") < caption.index("🔗")
    assert calls[1] == ("audio", "song", "artist")


def _run():
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

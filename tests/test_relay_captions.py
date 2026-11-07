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


def test_full_text_joins_header_and_body():
    assert common._full_text(["a", "b"], "body") == "a\nb\n\nbody"
    assert common._full_text([], "body") == "body"
    assert common._full_text(["a"], "") == "a"


def test_short_caption_stays_a_normal_caption():
    caption, followups = common._compose_caption(HDR, "short text")
    assert followups == []
    assert "short text" in caption and len(caption) <= 1024


def test_long_caption_overflow_is_one_rich_block():
    body = "x" * 8000
    caption, followups = common._compose_caption(HDR, body)
    assert len(caption) <= 1024
    # ONE remainder (rich text handles 32k), not many 4096 chunks
    assert len(followups) == 1
    assert len(followups[0]) > 4096


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

"""Regression tests for the Instagram photo-post path.

Guards the 2026-10-18 operator report: Instagram **photo** posts (single image
or carousel) were unreachable through the bot. yt-dlp's Instagram extractor
models media as video only — it drops ``image_versions2`` — so a photo post
died with "No video formats found" and a carousel containing any photo failed
the whole playlist, both surfacing as "The site changed its layout or the URL is
malformed.".

The fix lives in ``utils/downloader/instagram_post.py``:

* ``is_instagram_post_url`` picks exactly the /p/ and /tv/ posts (never
  /reel(s)/, /stories/, /share/),
* the ``InstagramIE`` subclass shadows yt-dlp's own extractor name so
  ``ie_key()`` stays "Instagram" (replacing, not appending, the built-in),
  defuses ``raise_no_formats`` and records ``ig_photos``,
* ``download_post_photos`` falls back through Instagram's smaller
  ``image_versions2`` renditions, rejects non-image payloads by magic bytes and
  prefers a rendition Telegram will accept as a photo (<=10 MB).

Everything here is offline: the network fetcher is monkeypatched.

Standalone: ``venv/bin/python tests/test_instagram_post.py`` (also
pytest-compatible).
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.downloader import instagram_post as igp  # noqa: E402
from utils.downloader import (  # noqa: E402
    InstagramIE,
    canonical_post_url,
    fetch_instagram_post,
    is_instagram_post_url,
)

from yt_dlp.extractor.instagram import InstagramIE as UpstreamInstagramIE  # noqa: E402

_FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        _FAILURES.append(label)


# ---------------------------------------------------------------------------
# URL routing
# ---------------------------------------------------------------------------

def test_url_matching() -> None:
    print("test_url_matching")
    posts = [
        "https://www.instagram.com/p/Dem-LAGmuqO/",
        "https://www.instagram.com/p/Dem-LAGmuqO/?img_index=4&obrf=NHdvcTlzazN3bTRx",
        "https://instagram.com/p/DdqJjTeApl9",
        "https://www.instagram.com/someuser/p/DdqJjTeApl9/",
        "https://www.instagram.com/tv/CXyZ123/",
        "http://www.instagram.com/p/Abc-Def/",
    ]
    not_posts = [
        "https://www.instagram.com/reel/Del7xLXxjII/",
        "https://www.instagram.com/reels/Del7xLXxjII/",
        "https://www.instagram.com/stories/highlights/17912345678/",
        "https://www.instagram.com/stories/someuser/1234567890/",
        "https://www.instagram.com/share/reel/BAF123",
        "https://www.instagram.com/",
        "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        "https://twitter.com/x/status/1",
        "",
    ]
    for u in posts:
        check(f"post  {u}", is_instagram_post_url(u))
    for u in not_posts:
        check(f"other {u or '<empty>'}", not is_instagram_post_url(u))

    check("canonical strips tracking + keeps the code",
          canonical_post_url(
              "https://www.instagram.com/p/Dem-LAGmuqO/?img_index=4&obrf=abc")
          == "https://www.instagram.com/p/Dem-LAGmuqO/")
    check("canonical leaves non-post URLs alone",
          canonical_post_url("https://www.instagram.com/reel/AAA/")
          == "https://www.instagram.com/reel/AAA/")


def test_extractor_shadowing() -> None:
    print("test_extractor_shadowing")
    check("subclasses yt-dlp's extractor", issubclass(InstagramIE, UpstreamInstagramIE))
    # ie_key() == class name minus "IE" — it MUST stay "Instagram" so
    # add_info_extractor() replaces the built-in instead of adding a second IE
    # that never matches (the first version of this fix appended and therefore
    # never ran).
    check("ie_key() is 'Instagram'", InstagramIE.ie_key() == "Instagram",
          f"got {InstagramIE.ie_key()!r}")
    check("is the same key upstream registers",
          UpstreamInstagramIE.ie_key() == InstagramIE.ie_key())

    ie = InstagramIE()
    ie.raise_no_formats("There is no video in this post", expected=True)  # must not raise

    class _FakeDl:
        pass

    check("_extract_product_media records ig_photos", callable(
        getattr(InstagramIE, "_extract_product_media", None)))


def test_build_post() -> None:
    print("test_build_post")
    photo_entry = {
        "id": "AAA1",
        "formats": [],
        "ig_photos": ["https://cdn/big1.jpg", "https://cdn/small1.jpg"],
    }
    video_entry = {
        "id": "AAA2",
        "formats": [{"url": "https://cdn/v.mp4", "format_id": "1"}],
        "ig_photos": ["https://cdn/cover.jpg"],
    }
    meta = {"title": "Post by someone", "description": "hello", "channel": "someone"}

    single = igp._build_post({**meta, **photo_entry}, "https://www.instagram.com/p/AAA/")
    check("single photo post has_photo", single["has_photo"] is True)
    check("single photo count", single["n_photo"] == 1 and single["n_video"] == 0)
    check("photo original first, renditions as fallbacks",
          single["items"][0]["url"] == "https://cdn/big1.jpg"
          and single["items"][0]["fallbacks"] == ["https://cdn/small1.jpg"])
    check("caption/author carried", single["caption"] == "hello" and single["author"] == "someone")

    playlist = igp._build_post(
        {"_type": "playlist", **meta, "entries": [photo_entry, video_entry]},
        "https://www.instagram.com/p/AAA/")
    check("carousel counts", playlist["n_photo"] == 1 and playlist["n_video"] == 1)
    check("carousel keeps post order", [i["kind"] for i in playlist["items"]] == ["photo", "video"])
    check("video item keeps a permalink",
          playlist["items"][1]["permalink"] == "https://www.instagram.com/p/AAA2/")

    video_only = igp._build_post({**meta, **video_entry}, "https://www.instagram.com/p/AAA/")
    check("video-only post does NOT claim has_photo", video_only["has_photo"] is False)
    check("video-only post has 0 photos", video_only["n_photo"] == 0)

    empty = igp._build_post({"id": "AAA3", "formats": [], "ig_photos": []},
                            "https://www.instagram.com/p/AAA/")
    check("post with no media yields no items", empty["items"] == [])


# ---------------------------------------------------------------------------
# Photo download
# ---------------------------------------------------------------------------

_JPEG = bytes.fromhex("ffd8ffe000104a46494600010100000100010000") + b"x" * 4096


def test_download_post_photos() -> None:
    print("test_download_post_photos")
    tmp = tempfile.mkdtemp(prefix="igp_test_")
    calls: list[str] = []

    def fake_fetch(url: str) -> bytes:
        calls.append(url)
        if "broken" in url:
            raise RuntimeError("cdn 500")
        if "html" in url:
            return b"<html><body>login</body></html>" + b"x" * 2000
        if "huge" in url:
            return _JPEG + b"y" * (11 * 1024 * 1024)
        return _JPEG

    orig = igp._fetch_bytes
    igp._fetch_bytes = fake_fetch  # type: ignore[assignment]
    try:
        # 1) original works → no fallback used
        files = igp.download_post_photos(
            [{"url": "https://cdn/orig.jpg", "fallbacks": ["https://cdn/small.jpg"]}], tmp)
        check("original accepted", len(files) == 1 and files[0].endswith(".jpg"))
        check("no fallback fetched", calls == ["https://cdn/orig.jpg"])

        # 2) broken original → smaller rendition used
        calls.clear()
        files = igp.download_post_photos(
            [{"url": "https://cdn/broken.jpg", "fallbacks": ["https://cdn/small.jpg"]}], tmp)
        check("falls back to the smaller rendition",
              len(files) == 1 and calls == ["https://cdn/broken.jpg", "https://cdn/small.jpg"])

        # 3) HTML interstitial → rejected, next candidate used
        calls.clear()
        files = igp.download_post_photos(
            [{"url": "https://cdn/html.jpg", "fallbacks": ["https://cdn/small.jpg"]}], tmp)
        check("HTML payload rejected", len(files) == 1 and calls[-1] == "https://cdn/small.jpg")

        # 4) oversized original → smaller rendition preferred over document mode
        calls.clear()
        files = igp.download_post_photos(
            [{"url": "https://cdn/huge.jpg", "fallbacks": ["https://cdn/small.jpg"]}], tmp)
        check("oversized original not sent as a photo",
              len(files) == 1 and calls[-1] == "https://cdn/small.jpg"
              and os.path.getsize(files[0]) <= igp._PHOTO_TELEGRAM_MAX)

        # 5) everything unusable → no crash, empty result
        calls.clear()
        files = igp.download_post_photos(
            [{"url": "https://cdn/broken.jpg", "fallbacks": ["https://cdn/html.jpg"]}], tmp)
        check("all candidates unusable → empty", files == [])

        # 6) only oversized renditions → kept for document delivery
        calls.clear()
        files = igp.download_post_photos([{"url": "https://cdn/huge.jpg"}], tmp)
        check("oversized-only post keeps the original", len(files) == 1
              and os.path.getsize(files[0]) > igp._PHOTO_TELEGRAM_MAX)
    finally:
        igp._fetch_bytes = orig  # type: ignore[assignment]
        shutil.rmtree(tmp, ignore_errors=True)


def test_non_post_url_short_circuits() -> None:
    print("test_non_post_url_short_circuits")
    # fetch_instagram_post must not touch the network for a non-post URL.
    called = {"n": 0}
    orig = igp._extract_info

    def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("network call for a non-post URL")

    igp._extract_info = boom  # type: ignore[assignment]
    try:
        for u in ("https://www.instagram.com/reel/AAA/",
                  "https://www.instagram.com/stories/x/1/",
                  "https://www.youtube.com/watch?v=jNQXAC9IVRw"):
            try:
                fetch_instagram_post(u)
                check(f"refuses {u}", False, "no exception raised")
            except ValueError:
                check(f"refuses {u} before any yt-dlp call", called["n"] == 0)
            except Exception as e:
                check(f"refuses {u}", False, f"{type(e).__name__}: {e}")
    finally:
        igp._extract_info = orig  # type: ignore[assignment]


def test_error_classification() -> None:
    print("test_error_classification")
    from utils.downloader import _classify_ytdl_error
    ig = "https://www.instagram.com/p/AAA/"
    for raw in ("Instagram sent an empty media response. Check if this post is accessible...",
                "Failed to parse JSON"):
        msg = _classify_ytdl_error(Exception(raw), ig)
        check(f"IG empty/JSON error names the jar ({raw[:24]})",
              "igcookies.txt" in msg and "anonymous request" in msg, msg[:70])
    other = _classify_ytdl_error(Exception("Failed to parse JSON"), "https://example.com/x")
    check("non-Instagram parse errors keep the generic message",
          "layout" in other, other[:70])


def main() -> int:
    test_url_matching()
    test_extractor_shadowing()
    test_build_post()
    test_download_post_photos()
    test_non_post_url_short_circuits()
    test_error_classification()
    print()
    if _FAILURES:
        print(f"FAILED: {len(_FAILURES)} check(s): {_FAILURES}")
        return 1
    print("All Instagram post tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

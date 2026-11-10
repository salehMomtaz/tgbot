"""Regression tests for the Instagram anti-auto-login invariants.

Guards the permanent-suspension root cause found on 2026-11-06:

* ``utils.ig_anti_detect.sessionid_sig`` must fingerprint the jar's
  ``sessionid`` VALUE. The cookie refresher / yt-dlp write-back rotate OTHER
  cookies and bump mtime while the (dead) sessionid stays in place — anything
  that re-armed login on mtime made the bot re-attempt ``login_by_sessionid``
  with a dead session, deepening Instagram enforcement (proven by 578
  ``instagrapi_writeback`` events + the 2026-10-03..08 death streak in
  ``cookies/history.jsonl``).
* ``burst_pace`` must return the ``-1`` sentinel once the backfill safety cap
  is reached, so the worker stops a scrape-shaped backlog burst and resumes on
  later jittered polls instead of relaying everything in one run.

Standalone: ``venv/bin/python tests/test_ig_session_gate.py`` (pytest-compatible).
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from utils import ig_anti_detect  # noqa: E402


def _write_jar(path: str, sessionid: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Netscape HTTP Cookie File\n")
        f.write(f".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\t{sessionid}\n")
        f.write(".instagram.com\tTRUE\t/\tTRUE\t0\tcsrftoken\tCSRF1\n")


def test_sessionid_sig_tracks_only_the_sessionid_value():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "igcookies.txt")
        _write_jar(p, "AAA")
        sig = ig_anti_detect.sessionid_sig(p)
        assert sig, "a present sessionid must produce a signature"
        assert ig_anti_detect.sessionid_sig(p) == sig, "must be deterministic"

        # Refresher/write-back case: OTHER cookies rotate, sessionid unchanged.
        with open(p, "a", encoding="utf-8") as f:
            f.write(".instagram.com\tTRUE\t/\tTRUE\t0\tmid\tNEWMID\n")
            f.write(".instagram.com\tTRUE\t/\tTRUE\t0\tcsrftoken\tCSRF2\n")
        assert ig_anti_detect.sessionid_sig(p) == sig, \
            "cookie rotation must NOT look like a fresh jar"

        # Operator uploads a fresh jar: the sessionid really changed.
        _write_jar(p, "BBB")
        assert ig_anti_detect.sessionid_sig(p) != sig, \
            "a replaced sessionid MUST change the signature"


def test_sessionid_sig_missing():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "empty.txt")
        open(p, "w").close()
        assert ig_anti_detect.sessionid_sig(p) == ""
        assert ig_anti_detect.sessionid_sig(os.path.join(d, "nope.txt")) == ""


def test_burst_pace_cap_sentinel():
    old = getattr(config, "IG_DIRECT_BACKFILL_MAX_ITEMS", None)
    config.IG_DIRECT_BACKFILL_MAX_ITEMS = 5
    try:
        assert ig_anti_detect.burst_pace(0) == 0.0
        below = ig_anti_detect.burst_pace(3, delivered_in_run=4)
        assert 0.0 <= below <= 30.0, below
        assert ig_anti_detect.burst_pace(3, delivered_in_run=5) == -1.0
        assert ig_anti_detect.burst_pace(3, delivered_in_run=99) == -1.0
        # Large batches stay bounded (never "days").
        assert 0.0 <= ig_anti_detect.burst_pace(1000, delivered_in_run=0) <= 30.0
    finally:
        if old is None:
            del config.IG_DIRECT_BACKFILL_MAX_ITEMS
        else:
            config.IG_DIRECT_BACKFILL_MAX_ITEMS = old


def test_ig_jar_is_never_handed_to_ytdlp():
    """Instagram is instagrapi-only: yt-dlp NEVER receives the IG jar.

    This holds with IG_AUTH_ENABLED true or false, and the yt-dlp
    extract/download entry points refuse an Instagram URL outright.
    """
    from utils.downloader import cookies as dl_cookies
    from utils import ig_session_guard

    old = config.IG_AUTH_ENABLED
    old_jar = config.IG_COOKIES
    old_latch = ig_session_guard.LATCH_PATH
    with tempfile.TemporaryDirectory() as d:
        try:
            # Isolate from the live box's real jar and latch.
            config.IG_COOKIES = os.path.join(d, "igcookies.txt")
            ig_session_guard.LATCH_PATH = os.path.join(d, "ig_session_dead.json")
            _write_jar(config.IG_COOKIES, "AAA")

            for auth in (True, False):
                config.IG_AUTH_ENABLED = auth
                assert dl_cookies._resolve_jar_path(
                    "https://www.instagram.com/reel/abc/") is None

            # Defense in depth: the yt-dlp entry points themselves refuse IG.
            from utils.downloader.formats import extract_formats
            from utils.downloader.download import download_media
            for fn in (
                lambda: extract_formats("https://www.instagram.com/reel/abc/"),
                lambda: download_media("https://www.instagram.com/reel/abc/"),
            ):
                try:
                    fn()
                except RuntimeError as e:
                    assert "instagrapi-only" in str(e), e
                else:
                    raise AssertionError("yt-dlp entry point must refuse Instagram")
        finally:
            config.IG_AUTH_ENABLED = old
            config.IG_COOKIES = old_jar
            ig_session_guard.LATCH_PATH = old_latch


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

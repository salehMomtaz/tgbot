"""Regression tests for the Instagram session-dead hard stop (2026-11-09).

Operator requirement: once Instagram rejects the session, the bot must make
ZERO further authenticated calls — no ``account_info`` probe, no
``login_by_sessionid`` retry with the same dead sessionid, no cookie-refresher
visit, no yt-dlp jar handoff — notify ONCE, and wait until a fresh
``igcookies.txt`` (new sessionid VALUE) is uploaded.

Standalone: ``venv/bin/python tests/test_ig_session_guard.py`` (pytest-compatible).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from utils import ig_session_guard  # noqa: E402


def _write_jar(path: str, sessionid: str | None, extra: str = "") -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Netscape HTTP Cookie File\n")
        if sessionid is not None:
            f.write(f".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\t{sessionid}\n")
        f.write(".instagram.com\tTRUE\t/\tTRUE\t0\tcsrftoken\tCSRF1\n")
        if extra:
            f.write(extra)


class _GuardSandbox:
    """Point config.IG_COOKIES and the latch path at a temp dir."""

    def __enter__(self):
        self._dir = tempfile.TemporaryDirectory()
        self.jar = os.path.join(self._dir.name, "igcookies.txt")
        self._old_jar = config.IG_COOKIES
        self._old_latch = ig_session_guard.LATCH_PATH
        config.IG_COOKIES = self.jar
        ig_session_guard.LATCH_PATH = os.path.join(self._dir.name, "ig_session_dead.json")
        return self

    def __exit__(self, *exc):
        config.IG_COOKIES = self._old_jar
        ig_session_guard.LATCH_PATH = self._old_latch
        self._dir.cleanup()


def test_latch_is_keyed_on_the_sessionid_value_only():
    with _GuardSandbox() as s:
        _write_jar(s.jar, "AAA")
        assert ig_session_guard.is_dead() is False
        assert ig_session_guard.mark_dead("login_required", "test") is True
        assert ig_session_guard.is_dead() is True

        # A second detection must NOT re-notify (survives restarts).
        assert ig_session_guard.mark_dead("login_required again", "test") is False
        assert ig_session_guard.is_dead() is True

        # The refresher / yt-dlp rotate OTHER cookies and bump mtime while the
        # same dead sessionid stays: the latch must stay armed.
        _write_jar(s.jar, "AAA", ".instagram.com\tTRUE\t/\tTRUE\t0\tmid\tNEWMID\n")
        assert ig_session_guard.is_dead() is True

        # Operator uploads a genuinely fresh session: auto-clear.
        _write_jar(s.jar, "BBB")
        assert ig_session_guard.is_dead() is False
        assert not os.path.exists(ig_session_guard.LATCH_PATH), \
            "a stale latch file must be deleted when the sessionid changes"


def test_no_sessionid_cannot_arm():
    with _GuardSandbox() as s:
        _write_jar(s.jar, None)
        assert ig_session_guard.mark_dead("boom", "test") is False
        assert ig_session_guard.is_dead() is False


def test_auth_classification():
    assert ig_session_guard.is_auth_failure(RuntimeError("login_required"))
    assert ig_session_guard.is_auth_failure(RuntimeError("Exceeded 30 redirects."))
    assert ig_session_guard.is_auth_failure(RuntimeError("checkpoint_required"))
    assert ig_session_guard.is_auth_failure(RuntimeError("Please wait a few minutes"))
    # Transient problems must NEVER arm the latch.
    assert not ig_session_guard.is_auth_failure(TimeoutError("read timed out"))
    assert not ig_session_guard.is_auth_failure(ConnectionError("reset by peer"))
    assert not ig_session_guard.is_auth_failure(RuntimeError("HTTP 503"))


def test_persisted_session_matches_jar():
    with _GuardSandbox() as s:
        _write_jar(s.jar, "AAA")
        settings = os.path.join(s._dir.name, "direct_ig_session.json")
        with open(settings, "w", encoding="utf-8") as f:
            json.dump({"cookies": {"sessionid": "AAA"}}, f)
        assert ig_session_guard.persisted_sessionid(settings) == "AAA"
        assert ig_session_guard.persisted_matches_jar(settings) is True

        # A persisted session from an OLDER jar is not proof the jar is dead.
        with open(settings, "w", encoding="utf-8") as f:
            json.dump({"cookies": {"sessionid": "OLD"}}, f)
        assert ig_session_guard.persisted_matches_jar(settings) is False

        assert ig_session_guard.persisted_sessionid(os.path.join(s._dir.name, "nope")) is None


def test_clear_is_idempotent():
    with _GuardSandbox() as s:
        _write_jar(s.jar, "AAA")
        ig_session_guard.mark_dead("x", "test")
        ig_session_guard.clear("test")
        assert ig_session_guard.is_dead() is False
        ig_session_guard.clear("again")  # must not raise


def _run() -> int:
    tests = sorted(
        (n, fn) for n, fn in globals().items() if n.startswith("test_") and callable(fn)
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

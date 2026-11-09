"""The 2026-11-09 regression: a rejected session must not be retried.

Observed on the live box at 10:53: ``_ig_login`` validated
``direct_ig_session.json`` (403 login_required) and then called
``login_by_sessionid`` with the SAME dead sessionid; instagrapi's internal
ladder ended in ``Exceeded 30 redirects`` — the operator's "second attempt".

These tests use a fake instagrapi client, so they prove the decision logic
without any network access.

Standalone: ``venv/bin/python tests/test_ig_login_no_retry.py``.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from utils import ig_session_guard  # noqa: E402
from modules.direct_forward import instagram as igmod  # noqa: E402
from instagrapi.exceptions import LoginRequired  # noqa: E402


class FakeClient:
    def __init__(self, account_info_exc=None, login_exc=None, login_ok=True):
        self.account_info_exc = account_info_exc
        self.login_exc = login_exc
        self.login_ok = login_ok
        self.calls: list[str] = []

    def load_settings(self, path):
        self.calls.append("load_settings")

    def account_info(self):
        self.calls.append("account_info")
        if self.account_info_exc:
            raise self.account_info_exc
        return object()

    def login_by_sessionid(self, sessionid):
        self.calls.append("login_by_sessionid")
        if self.login_exc:
            raise self.login_exc
        return self.login_ok


class Sandbox:
    def __enter__(self):
        self._dir = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._dir.name)
        os.makedirs("cookies/instagram", exist_ok=True)
        self.jar = os.path.join("cookies", "instagram", "igcookies.txt")
        self._old_jar = config.IG_COOKIES
        self._old_latch = ig_session_guard.LATCH_PATH
        self._old_record = igmod.cookie_history.record
        self._old_writeback = igmod.ig_anti_detect.write_back_session
        config.IG_COOKIES = self.jar
        ig_session_guard.LATCH_PATH = os.path.join(self._dir.name, "ig_session_dead.json")
        igmod.cookie_history.record = lambda *a, **k: None
        igmod.ig_anti_detect.write_back_session = lambda *a, **k: None
        return self

    def __exit__(self, *exc):
        config.IG_COOKIES = self._old_jar
        ig_session_guard.LATCH_PATH = self._old_latch
        igmod.cookie_history.record = self._old_record
        igmod.ig_anti_detect.write_back_session = self._old_writeback
        os.chdir(self._old_cwd)
        self._dir.cleanup()

    def write_jar(self, sid: str):
        with open(self.jar, "w", encoding="utf-8") as f:
            f.write("# Netscape HTTP Cookie File\n")
            f.write(f".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\t{sid}\n")

    def write_persisted(self, sid: str):
        with open("direct_ig_session.json", "w", encoding="utf-8") as f:
            json.dump({"cookies": {"sessionid": sid}}, f)


def test_dead_persisted_session_with_same_sessionid_is_not_retried():
    """The exact 2026-11-09 bug: 403 then login_by_sessionid with the same sid."""
    with Sandbox() as s:
        s.write_jar("AAA")
        s.write_persisted("AAA")
        cl = FakeClient(account_info_exc=LoginRequired("login_required"))
        try:
            igmod._ig_login(cl)
        except ig_session_guard.IGSessionDead:
            pass
        else:
            raise AssertionError("expected IGSessionDead")
        assert cl.calls == ["load_settings", "account_info"], \
            f"must not fall back to login_by_sessionid: {cl.calls}"
        assert ig_session_guard.is_dead(), "the hard-stop latch must be armed"


def test_armed_latch_makes_zero_calls():
    with Sandbox() as s:
        s.write_jar("AAA")
        s.write_persisted("AAA")
        assert ig_session_guard.mark_dead("login_required", "test") is True
        cl = FakeClient()
        try:
            igmod._ig_login(cl)
        except ig_session_guard.IGSessionDead:
            pass
        else:
            raise AssertionError("expected IGSessionDead")
        assert cl.calls == [], f"no Instagram call is allowed while latched: {cl.calls}"


def test_fresh_session_from_an_older_persisted_session_allows_one_login():
    with Sandbox() as s:
        s.write_jar("NEW")
        s.write_persisted("OLD")  # persisted device session is from an older jar
        cl = FakeClient(account_info_exc=LoginRequired("login_required"))
        igmod._ig_login(cl)
        # The OLD persisted session must NOT be probed (no account_info), but its
        # device identity IS loaded so the account keeps ONE identity across jar
        # uploads; then exactly one bootstrap login for the fresh sessionid.
        assert cl.calls == ["load_settings", "login_by_sessionid"], cl.calls
        assert not ig_session_guard.is_dead(), "a successful bootstrap clears the latch"


def test_bootstrap_rejection_arms_the_latch():
    with Sandbox() as s:
        s.write_jar("NEW")
        cl = FakeClient(login_exc=LoginRequired("login_required"))
        try:
            igmod._ig_login(cl)
        except ig_session_guard.IGSessionDead:
            pass
        else:
            raise AssertionError("expected IGSessionDead")
        assert cl.calls == ["login_by_sessionid"], cl.calls
        assert ig_session_guard.is_dead()


def test_transient_network_error_never_arms_the_latch():
    with Sandbox() as s:
        s.write_jar("AAA")
        s.write_persisted("AAA")
        cl = FakeClient(account_info_exc=TimeoutError("read timed out"))
        try:
            igmod._ig_login(cl)
        except TimeoutError:
            pass
        else:
            raise AssertionError("the transient error must propagate for a backoff")
        assert cl.calls == ["load_settings", "account_info"], cl.calls
        assert not ig_session_guard.is_dead(), "a blip must not demand a fresh jar"


def test_ig_poll_interval_uses_the_ig_specific_knob():
    old = getattr(config, "IG_DIRECT_POLL_SECONDS", None)
    old_j = getattr(config, "DIRECT_FORWARD_POLL_JITTER_PCT", 40)
    try:
        config.IG_DIRECT_POLL_SECONDS = 900
        config.DIRECT_FORWARD_POLL_JITTER_PCT = 0
        assert igmod._ig_poll_interval() == 900
        config.IG_DIRECT_POLL_SECONDS = 1800
        assert igmod._ig_poll_interval() == 1800
    finally:
        if old is None:
            del config.IG_DIRECT_POLL_SECONDS
        else:
            config.IG_DIRECT_POLL_SECONDS = old
        config.DIRECT_FORWARD_POLL_JITTER_PCT = old_j


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

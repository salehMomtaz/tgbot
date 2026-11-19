"""Mint-script burst discipline (offline, no network).

Verifies the 2026-11-19 findings about tools/mint_ig_session.py:
a single mint run used to fire ~5 pre-auth requests back-to-back
(account_info probe + usdid register + CAA process_client_data +
attestation + oauth token fetch + send_login_request), all with
login=True i.e. with NO delay_range pacing.

Covered here with fakes (no instagrapi import, no network):
- _install_login_pacer spaces BOTH request paths (private_request and
  private_graphql_www_request) with 2-5s pauses and delegates intact.
- _known_dead_session is True only when the hard-stop latch is armed
  for the CURRENT jar sessionid (so mint may skip the doomed
  account_info probe); False for fresh jars and unlatched boxes.

Standalone: ``venv/bin/python tests/test_mint_pacing.py``.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from tools import mint_ig_session as mint  # noqa: E402
from utils import ig_session_guard  # noqa: E402


class FakeClient:
    def __init__(self):
        self.calls: list[str] = []
        self.user_id = "123"

    def private_request(self, endpoint, *args, **kwargs):
        self.calls.append(f"private_request:{endpoint}")
        return {"ok": True}

    def private_graphql_www_request(self, *args, **kwargs):
        self.calls.append("graphql_www")
        return {"ok": True}


def test_pacer_spaces_both_paths_and_delegates():
    sleeps: list[float] = []
    import tools.mint_ig_session as m

    orig_sleep = m.time.sleep
    orig_uniform = m.random.uniform
    m.time.sleep = sleeps.append  # type: ignore[method-assign]
    m.random.uniform = lambda a, b: 3.0  # type: ignore[method-assign]
    try:
        cl = FakeClient()
        mint._install_login_pacer(cl)
        assert cl.private_request("x/") == {"ok": True}
        assert cl.private_graphql_www_request("y") == {"ok": True}
    finally:
        m.time.sleep = orig_sleep  # type: ignore[method-assign]
        m.random.uniform = orig_uniform  # type: ignore[method-assign]
    assert cl.calls == ["private_request:x/", "graphql_www"], cl.calls
    assert sleeps == [3.0, 3.0], f"each request must pause first: {sleeps}"


class Sandbox:
    def __enter__(self):
        self._dir = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._dir.name)
        os.makedirs("cookies/instagram", exist_ok=True)
        self._old_jar = config.IG_COOKIES
        self._old_latch = ig_session_guard.LATCH_PATH
        config.IG_COOKIES = os.path.join("cookies", "instagram", "igcookies.txt")
        ig_session_guard.LATCH_PATH = os.path.join(self._dir.name, "ig_session_dead.json")
        return self

    def __exit__(self, *exc):
        config.IG_COOKIES = self._old_jar
        ig_session_guard.LATCH_PATH = self._old_latch
        os.chdir(self._old_cwd)
        self._dir.cleanup()

    def write_jar(self, sid: str):
        with open(config.IG_COOKIES, "w", encoding="utf-8") as f:
            f.write("# Netscape HTTP Cookie File\n")
            f.write(f".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\t{sid}\n")


def test_known_dead_only_for_current_jar_sessionid():
    with Sandbox() as s:
        assert mint._known_dead_session() is False, "no latch -> probe stays"
        s.write_jar("AAA")
        assert ig_session_guard.mark_dead("login_required", "test") is True
        assert mint._known_dead_session() is True, "latched AAA jar -> skip probe"
        s.write_jar("BBB")
        assert mint._known_dead_session() is False, "fresh jar -> probe stays"
        ig_session_guard.clear("test done")
        assert mint._known_dead_session() is False, "cleared latch -> probe stays"


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

"""Offline validation of the browser-cookie checker (no network).

Covers tools/check_browser_cookies.py:
- full export -> exit 0 READY,
- missing rur -> exit 1,
- same sessionid as the armed latch -> exit 1 (don't re-upload death),
- account mismatch warns but stays exit 0.

Standalone: ``venv/bin/python tests/test_browser_cookie_check.py``.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from utils import ig_session_guard  # noqa: E402


def _export(path: str, sid: str, with_rur: bool = True, account: str = "111"):
    lines = ["# Netscape HTTP Cookie File"]
    jar = {
        "sessionid": sid,
        "ds_user_id": account,
        "csrftoken": "toktoktok",
        "mid": "midmidmid",
    }
    if with_rur:
        jar["rur"] = "rurrurrur"
    for name, val in jar.items():
        lines.append(f".instagram.com\tTRUE\t/\tTRUE\t0\t{name}\t{val}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _run_checker(export_path: str) -> tuple[int, str]:
    import tools.check_browser_cookies as c

    old = sys.argv
    sys.argv = ["check_browser_cookies.py", export_path]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = c.main()
    finally:
        sys.argv = old
    return code, buf.getvalue()


def test_full_export_is_ready():
    with tempfile.TemporaryDirectory() as d:
        old, old_jar, old_latch = os.getcwd(), config.IG_COOKIES, ig_session_guard.LATCH_PATH
        os.chdir(d)
        os.makedirs("cookies/instagram", exist_ok=True)
        config.IG_COOKIES = os.path.join("cookies", "instagram", "igcookies.txt")
        ig_session_guard.LATCH_PATH = os.path.join(d, "dead.json")
        try:
            p = os.path.join(d, "exp.txt")
            _export(p, "FRESHSID1")
            code, out = _run_checker(p)
            assert code == 0, out
            assert "READY" in out, out
        finally:
            os.chdir(old)
            config.IG_COOKIES, ig_session_guard.LATCH_PATH = old_jar, old_latch


def test_missing_cookie_is_rejected():
    with tempfile.TemporaryDirectory() as d:
        old, old_jar, old_latch = os.getcwd(), config.IG_COOKIES, ig_session_guard.LATCH_PATH
        os.chdir(d)
        os.makedirs("cookies/instagram", exist_ok=True)
        config.IG_COOKIES = os.path.join("cookies", "instagram", "igcookies.txt")
        ig_session_guard.LATCH_PATH = os.path.join(d, "dead.json")
        try:
            p = os.path.join(d, "exp.txt")
            _export(p, "FRESHSID1", with_rur=False)
            code, out = _run_checker(p)
            assert code == 1, out
            assert "rur" in out, out
        finally:
            os.chdir(old)
            config.IG_COOKIES, ig_session_guard.LATCH_PATH = old_jar, old_latch


def test_latched_sessionid_is_refused_and_mismatch_warns():
    with tempfile.TemporaryDirectory() as d:
        old, old_jar, old_latch = os.getcwd(), config.IG_COOKIES, ig_session_guard.LATCH_PATH
        os.chdir(d)
        os.makedirs("cookies/instagram", exist_ok=True)
        config.IG_COOKIES = os.path.join("cookies", "instagram", "igcookies.txt")
        ig_session_guard.LATCH_PATH = os.path.join(d, "dead.json")
        try:
            with open(config.IG_COOKIES, "w", encoding="utf-8") as f:
                f.write("# Netscape HTTP Cookie File\n"
                        ".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tDEADSID1\n")
            assert ig_session_guard.mark_dead("login_required", "test") is True
            p = os.path.join(d, "exp.txt")
            _export(p, "DEADSID1")
            code, out = _run_checker(p)
            assert code == 1, out
            assert "SAME sessionid" in out, out
            p2 = os.path.join(d, "exp2.txt")
            _export(p2, "FRESHSID1", account="222")
            with open("direct_ig_session.json", "w", encoding="utf-8") as f:
                f.write('{"authorization_data": {"ds_user_id": "111"}}')
            code2, out2 = _run_checker(p2)
            assert code2 == 0, out2
            assert "WARNING" in out2, out2
        finally:
            os.chdir(old)
            config.IG_COOKIES, ig_session_guard.LATCH_PATH = old_jar, old_latch


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

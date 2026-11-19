"""Failure logs must carry the failure (offline, no network).

Two 15-day-audit findings:
- TikTok embed fetch failures logged "HTTP ?" for every 4xx/5xx because
  ``requests.Response`` objects are falsy on error status — the real code
  was hidden. The fetch must compare against None explicitly.
- The X self-DM fetch logged a bare ``{e}``; transport errors with an empty
  message produced "fetch failed: " with nothing after it. The log must
  include the exception type.

Standalone: ``venv/bin/python tests/test_probe_logging.py``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_tiktok_photo():
    """Load tiktok_photo.py by path, bypassing the heavy package __init__."""
    import importlib.util as iu

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "utils", "downloader", "tiktok_photo.py")
    spec = iu.spec_from_file_location("tiktok_photo_standalone", path)
    assert spec and spec.loader
    mod = iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_embed_fetch_logs_real_status_code():
    tiktok_photo = _load_tiktok_photo()

    records: list[logging.LogRecord] = []

    class Cap(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger(tiktok_photo.__name__)
    cap = Cap()
    logger.addHandler(cap)
    old_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        calls: list[str] = []

        class FakeResponse:
            status_code = 403
            text = "forbidden"

        def fake_get(*args, **kwargs):
            calls.append("get")
            return FakeResponse()

        import unittest.mock as mock

        with mock.patch.object(tiktok_photo.requests, "get", fake_get):
            assert tiktok_photo.fetch_embed_info("9999999999999999999") is None
        assert calls == ["get"], calls
        body = "\n".join(r.getMessage() for r in records)
        assert "HTTP 403" in body, body
        assert "HTTP ?" not in body, body
    finally:
        logger.removeHandler(cap)
        logger.setLevel(old_level)
    print("PASS  test_embed_fetch_logs_real_status_code")


def test_x_self_dm_fetch_logs_exception_type():
    from modules.direct_forward import twitter as xmod

    records: list[logging.LogRecord] = []

    class Cap(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("modules.direct_forward.twitter")
    cap = Cap()
    logger.addHandler(cap)
    old_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        async def boom(conversation_id, _cursor):
            raise ValueError("")

        client = types.SimpleNamespace(
            v11=types.SimpleNamespace(dm_conversation=boom))
        out = asyncio.run(xmod._x_fetch_self_messages(client, "1-1"))
        assert out == [], out
        body = "\n".join(r.getMessage() for r in records)
        assert "ValueError" in body, body
    finally:
        logger.removeHandler(cap)
        logger.setLevel(old_level)
    print("PASS  test_x_self_dm_fetch_logs_exception_type")


def _run() -> int:
    tests = sorted(
        (n, fn) for n, fn in globals().items() if n.startswith("test_") and callable(fn)
    )
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback

            print(f"FAIL  {name}: {exc!r}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())

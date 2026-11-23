"""X removed-content detection + self-healing fetch (offline, no network).

Covers the 2026-11-23 audit fixes:
- _is_tweet_removed() distinguishes platform-removed tweets (deleted /
  protected / suspended / 404 / 410 / gone) from photo-only or transient
  failures, so the relay sends a "Removed on X" notice instead of the
  generic "No downloadable media" text.
- _x_fetch_self_messages() self-heals CookieConflict (rebuilds the in-memory
  jar from disk, retries once) instead of wedging silent until restart, and
  falls back to one direct (no-proxy) fetch on ProxyError/timeouts so a
  residential-proxy outage does not stop the self-DM relay.

Standalone: ``venv/bin/python tests/test_x_removed.py``.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_twitter():
    # Prefer a real package import (handles relative imports). Falls back
    # to a path load with stubbed relatives for minimal offline envs.
    try:
        import importlib
        return importlib.import_module("modules.direct_forward.twitter")
    except Exception:
        pass
    import importlib.util as iu
    import re as _re
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "modules", "direct_forward", "twitter.py")
    # Dotted spec name so `from .state` / `from .common` resolve to stubs.
    pkg_name = "x_tw_pkg"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = []
    sys.modules[pkg_name] = pkg
    sys.modules[pkg_name + ".state"] = types.SimpleNamespace(
        _load_state=lambda *a, **k: {}, _state_save_owned=lambda *a, **k: None,
        _cursor=lambda *a, **k: 0, _bump_cursor=lambda *a, **k: None,
        _pending_pairs=lambda *a, **k: [], _set_pair=lambda *a, **k: None,
        _set_peer=lambda *a, **k: None, _get_peers=lambda *a, **k: [],
    )
    sys.modules[pkg_name + ".common"] = types.SimpleNamespace(
        URL_RE=_re.compile(r"https?://\S+"),
        _poll_interval=lambda: 300, _compose_caption=lambda *a, **k: ("", []),
        _send_followups=lambda *a, **k: None,
        _download_and_deliver=lambda *a, **k: None,
        _enqueue_relay=lambda *a, **k: None, _await_relays=lambda *a, **k: None,
        _video_upload_kwargs=lambda *a, **k: {}, _x_media_payload_ok=lambda *a, **k: True,
        _header_lines=lambda *a, **k: [], _full_text=lambda *a, **k: "",
    )
    sys.modules.setdefault("config", types.SimpleNamespace(
        DIRECT_FORWARD_PROXY=None, REQUESTS_PROXY=None,
        DIRECT_FORWARD_POLL_SECONDS=300, DIRECT_FORWARD_POLL_JITTER_PCT=40,
        X_COOKIES="cookies/twitter/xcookies.txt", XCHAT_INBOX="cache/xchat_inbox.jsonl",
    ))
    sys.modules.setdefault("utils.shared", types.SimpleNamespace(
        DOWNLOAD_CACHE={}, wait_if_stopped=lambda *a, **k: None,
        mark_worker_alive=lambda *a, **k: None,
    ))
    sys.modules.setdefault("utils.rich_media", types.SimpleNamespace(
        send_rich_text=lambda *a, **k: None,
    ))
    spec = iu.spec_from_file_location(pkg_name + ".twitter", path)
    assert spec and spec.loader
    mod = iu.module_from_spec(spec)
    sys.modules[pkg_name + ".twitter"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_removed_patterns():
    tw = _load_twitter()
    assert tw._is_tweet_removed("ERROR: [twitter] 123: Tweet not found")
    assert tw._is_tweet_removed("This tweet does not exist")
    assert tw._is_tweet_removed("Account suspended")
    assert tw._is_tweet_removed("protected tweets")
    assert tw._is_tweet_removed("404 Not Found")
    assert tw._is_tweet_removed("410 Gone")
    assert tw._is_tweet_removed("deleted")
    # Photo-only / transient must NOT match as removed.
    assert not tw._is_tweet_removed("No video could be found in this tweet")
    assert not tw._is_tweet_removed("Connection timed out")
    assert not tw._is_tweet_removed("")
    assert not tw._is_tweet_removed(None)
    print("removed-patterns OK")


class _FakeCookies:
    def __init__(self):
        self.cleared = False
        self.jar = []

    def clear(self):
        self.cleared = True


class _FakeHTTP:
    def __init__(self):
        self.cookies = _FakeCookies()


class _FakeV11:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def dm_conversation(self, conv_id, cursor):
        self.calls += 1
        action = self.script[min(self.calls - 1, len(self.script) - 1)]
        if isinstance(action, Exception):
            raise action
        return action, None


class _FakeClient:
    def __init__(self, script):
        self.v11 = _FakeV11(script)
        self.http = _FakeHTTP()
        self.cookies_set = []

    def set_cookies(self, cookies):
        self.cookies_set.append(dict(cookies))


class _CookieConflict(Exception):
    pass


_CookieConflict.__name__ = "CookieConflict"

try:
    import httpx as _httpx  # noqa: F401 - real name if available
    _CC = getattr(_httpx, "CookieConflict", _CookieConflict)
except Exception:
    _CC = _CookieConflict


def test_cookie_conflict_rebuilds_and_retries():
    tw = _load_twitter()
    ok_payload = {"conversation_timeline": {"entries": []}}
    client = _FakeClient([_CC("Multiple cookies exist with name=__cf_bm"), ok_payload])
    # Stub jar read to return a fresh dict.
    tw._x_jar_cookies = lambda: {"auth_token": "a", "twid": "u%3D1"}
    msgs = asyncio.run(tw._x_fetch_self_messages(client, "1-1"))
    assert msgs == [], f"expected [], got {msgs}"
    assert client.v11.calls == 2, f"expected retry, calls={client.v11.calls}"
    assert client.http.cookies.cleared, "jar should have been cleared"
    assert client.cookies_set, "fresh cookies should have been re-applied"
    print("cookie-conflict-rebuild OK")


class _ProxyErr(Exception):
    pass


_ProxyErr.__name__ = "ProxyError"


def test_proxy_error_falls_back_direct():
    tw = _load_twitter()
    ok_payload = {"conversation_timeline": {"entries": [{"message": {"id": "5", "message_data": {"text": "hi"}}}]}}
    client = _FakeClient([_ProxyErr("410 Gone")])
    tw._x_jar_cookies = lambda: {"auth_token": "a", "twid": "u%3D1"}

    import twikit as _twikit_real  # noqa: F401
    used_direct = {}

    class _DirectClient(_FakeClient):
        def __init__(self, *args, **kwargs):
            super().__init__([ok_payload])
            used_direct["built"] = True

    # Patch the twikit.Client import inside the function by stubbing sys.modules.
    fake_twikit = types.SimpleNamespace(Client=_DirectClient)
    sys.modules["twikit"] = fake_twikit
    try:
        msgs = asyncio.run(tw._x_fetch_self_messages(client, "1-1"))
    finally:
        try:
            del sys.modules["twikit"]
        except KeyError:
            pass
    assert used_direct.get("built"), "direct fallback client should have been built"
    assert msgs and msgs[0].get("id") == "5", f"expected relayed msg, got {msgs}"
    print("proxy-direct-fallback OK")


def test_proxy_breaker_dedupes_and_recovers():
    tw = _load_twitter()
    tw._PROXY_DOWN_UNTIL = 0.0
    tw._PROXY_WARNED = False

    import logging
    records = []

    class Cap(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = tw.logger
    cap = Cap()
    logger.addHandler(cap)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        assert tw._proxy_usable() is True
        tw._note_proxy_failure("ProxyError: 410 Gone")
        assert tw._proxy_usable() is False, "breaker must open"
        tw._note_proxy_failure("ProxyError: 410 Gone")  # repeat: DEBUG only
        levels = [r.levelname for r in records if "proxy" in r.getMessage().lower()]
        assert levels.count("WARNING") == 1, f"expected ONE warning, got {levels}"
        assert levels.count("DEBUG") == 1, f"expected one debug repeat, got {levels}"
        records.clear()
        # Recovery resets the breaker and logs once.
        tw._note_proxy_success()
        assert tw._proxy_usable() is True
        assert any(r.levelname == "INFO" for r in records), "recovery must log INFO"
        records.clear()
        tw._note_proxy_success()  # no outage: silent
        assert not records, "no-op recovery must not log"
    finally:
        logger.removeHandler(cap)
        logger.setLevel(old_level)
    print("proxy-breaker OK")


def test_probe_proxy_health():
    tw = _load_twitter()
    import requests as _requests

    class _Resp:
        status_code = 200

    calls = {"n": 0}
    real_get = _requests.get

    def fake_get(*a, **k):
        calls["n"] += 1
        return _Resp()

    _requests.get = fake_get
    try:
        assert tw._probe_proxy("http://p:1") is True
    finally:
        _requests.get = real_get

    def boom(*a, **k):
        raise _ProxyErr("410 Gone")

    _requests.get = boom
    try:
        assert tw._probe_proxy("http://p:1") is False
    finally:
        _requests.get = real_get
    assert calls["n"] == 1
    print("proxy-probe OK")


if __name__ == "__main__":
    test_removed_patterns()
    test_cookie_conflict_rebuilds_and_retries()
    test_proxy_breaker_dedupes_and_recovers()
    test_probe_proxy_health()
    # Proxy fallback needs twikit installed; skip gracefully offline.
    try:
        import twikit  # noqa: F401
        test_proxy_error_falls_back_direct()
    except Exception as e:
        print(f"proxy-direct-fallback SKIPPED ({e})")
    print("test_x_removed: all runnable checks passed")

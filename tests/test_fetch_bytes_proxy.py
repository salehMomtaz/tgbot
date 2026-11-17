"""Proxy selection for split-egress DM fetches (no network, no secrets)."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config
from modules.direct_forward import common


def _set_proxies(direct=None, generic=None):
    config.DIRECT_FORWARD_PROXY = direct
    config.REQUESTS_PROXY = generic


def test_ig_hosts_prefer_direct_forward_proxy():
    _set_proxies(direct="socks5h://user:pass@residential:1080", generic=None)
    for url in [
        "https://scontent-cdn.cdninstagram.com/v/abc.mp4?oe=1",
        "https://fbcdn.net/v/abc.jpg",
        "https://www.instagram.com/p/abc/",
    ]:
        got = common._proxy_for_url(url)
        assert got == "socks5h://user:pass@residential:1080", url


def test_ig_falls_back_to_generic():
    _set_proxies(direct=None, generic="http://generic:8080")
    got = common._proxy_for_url("https://scontent-cdn.cdninstagram.com/v/a.mp4")
    assert got == "http://generic:8080"


def test_non_ig_uses_generic_only():
    _set_proxies(direct="socks5h://user:pass@residential:1080", generic="http://generic:8080")
    got = common._proxy_for_url("https://ton.twitter.com/dm/photo.jpg")
    assert got == "http://generic:8080"


def test_fetch_bytes_respects_use_proxy_false():
    _set_proxies(direct="socks5h://user:pass@residential:1080", generic="http://generic:8080")
    calls = {}

    class FakeResp:
        content = b"x"

        def raise_for_status(self):
            return None

    import requests
    orig = requests.get

    def fake_get(url, headers=None, proxies=None, timeout=None):
        calls["proxies"] = proxies
        return FakeResp()

    requests.get = fake_get
    try:
        common._fetch_bytes("https://scontent-cdn.cdninstagram.com/v/a.mp4", use_proxy=False)
        assert calls["proxies"] is None
        common._fetch_bytes("https://scontent-cdn.cdninstagram.com/v/a.mp4", use_proxy=True)
        assert calls["proxies"] == {
            "http": "socks5h://user:pass@residential:1080",
            "https": "socks5h://user:pass@residential:1080",
        }
    finally:
        requests.get = orig
        _set_proxies(None, None)


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    ok = 0
    for fn in fns:
        try:
            fn()
        except Exception as e:
            print(f"FAIL  {fn.__name__}: {e!r}")
        else:
            print(f"PASS  {fn.__name__}")
            ok += 1
    print(f"\n{ok}/{len(fns)} passed")
    if ok != len(fns):
        raise SystemExit(1)


if __name__ == "__main__":
    _run()

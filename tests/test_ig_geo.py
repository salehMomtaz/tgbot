"""Auto geo detection tests (utils/ig_geo.py, 2026-11-09).

The VPS is in Prague; pinning US headers on a CZ connection is a mismatch
Instagram correlates on. Detection must resolve CZ / 420 / cs_CZ /
Europe/Prague (+3600s), and an explicit env override must win per field.

No network: ``_detect_live`` is monkeypatched.

Standalone: ``venv/bin/python tests/test_ig_geo.py``.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from utils import ig_geo  # noqa: E402


class _GeoPatch:
    def __init__(self, detected: dict | None = None, cached: dict | None = None):
        self.detected = detected
        self.cached = cached
        self.calls = 0

    def __enter__(self):
        self._old_detect = ig_geo._detect_live
        self._old_cache = ig_geo._read_cache
        self._old_write = ig_geo._write_cache
        self._old_logged = ig_geo._last_logged
        self._old_cfg = {k: getattr(config, k) for k in (
            "IG_DIRECT_COUNTRY", "IG_DIRECT_COUNTRY_CODE", "IG_DIRECT_LOCALE",
            "IG_DIRECT_TZ_OFFSET", "IG_DIRECT_TZ_NAME")}

        def _detect():
            self.calls += 1
            return dict(self.detected) if self.detected else None

        ig_geo._detect_live = _detect
        ig_geo._read_cache = lambda: dict(self.cached) if self.cached else None
        ig_geo._write_cache = lambda data: None
        ig_geo._last_logged = ""
        for k in self._old_cfg:
            setattr(config, k, "")
        return self

    def __exit__(self, *exc):
        ig_geo._detect_live = self._old_detect
        ig_geo._read_cache = self._old_cache
        ig_geo._write_cache = self._old_write
        ig_geo._last_logged = self._old_logged
        for k, v in self._old_cfg.items():
            setattr(config, k, v)


CZ = {"ip": "38.45.0.1", "country": "CZ", "city": "Prague",
      "timezone": "Europe/Prague", "source": "ipinfo", "ts": 1.0}


def test_cz_detection_maps_country_code_locale_timezone():
    with _GeoPatch(detected=CZ):
        g = ig_geo.resolve(force=True)
    assert g["country"] == "CZ"
    assert g["country_code"] == 420
    assert g["locale"] == "cs_CZ"
    assert g["timezone_name"] == "Europe/Prague"
    assert g["timezone_offset"] in (3600, 7200), g["timezone_offset"]


def test_explicit_override_wins_per_field():
    with _GeoPatch(detected=CZ):
        config.IG_DIRECT_COUNTRY = "US"
        config.IG_DIRECT_COUNTRY_CODE = "1"
        config.IG_DIRECT_LOCALE = "en_US"
        g = ig_geo.resolve(force=True)
    assert g["country"] == "US"
    assert g["country_code"] == 1
    assert g["locale"] == "en_US"
    # Timezone was not overridden, so it still follows the egress IP.
    assert g["timezone_name"] == "Europe/Prague"
    assert "country" in g["overridden"] and "locale" in g["overridden"]


def test_fresh_cache_avoids_a_network_lookup():
    cached = dict(CZ)
    with _GeoPatch(detected=CZ, cached=cached) as p:
        # A fresh cache is honoured only when it is inside the TTL; force=False
        # and ts=1.0 would be stale, so simulate a fresh timestamp.
        cached["ts"] = __import__("time").time()
        p.cached = cached
        g = ig_geo.resolve()
    assert g["country"] == "CZ"
    assert p.calls == 0, "a fresh cache must not hit the network"


def test_detection_failure_falls_back_without_crashing():
    with _GeoPatch(detected=None, cached=None):
        g = ig_geo.resolve(force=True)
    assert g["country"] == "US"
    assert g["locale"] == "en_US"
    assert g["source"] == "fallback"


def test_apply_identity_overwrites_stale_session_settings():
    """The bug that mattered: load_settings() restores US geo after pin_geo."""
    from utils import ig_anti_detect

    class Fake:
        def __init__(self):
            self.settings = {
                "country": "US", "country_code": 1, "locale": "en_US",
                "timezone_offset": -14400, "timezone_name": "GMT-04:00",
                "public_transport": "requests",
            }
            self.public_user_agent = None

        def set_country(self, v):
            self.country = v

        def set_country_code(self, v):
            self.country_code = v

        def set_locale(self, v):
            self.locale = v

        def set_timezone_offset(self, o, n):
            self.timezone_offset, self.timezone_name = o, n

        def set_retry_config(self, **kw):
            self.retry = kw

    with _GeoPatch(detected=CZ):
        cl = Fake()
        ig_anti_detect.apply_identity(cl)

    assert cl.settings["country"] == "CZ", cl.settings
    assert cl.settings["country_code"] == 420
    assert cl.settings["locale"] == "cs_CZ"
    assert cl.settings["public_transport"] == "curl"
    assert cl.settings["public_transport_impersonate"] == "chrome136"
    assert cl.country == "CZ"
    assert cl.locale == "cs_CZ"
    assert "Chrome/136" in (cl.public_user_agent or "")


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

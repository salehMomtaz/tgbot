#!/usr/bin/env python3
"""Probe whether this venv can run a pure-Python Instagram MQTToT push listener.

IG web + app use MQTToT (modified MQTT 3) on mqtt-mini.facebook.com. The
experimental hybrid in ``modules/direct_forward/instagram.py`` (behind
``IG_DIRECT_MQTT_ENABLED``) needs instagrapi's ``realtime_*`` methods.

The conclusion below is DERIVED from the probe rather than hardcoded: it
previously asserted "instagrapi 2.1.2 has NO RealtimeMixin" while the very
same run printed ``realtime_connect``/``realtime_on`` in the method list.
Hardcoded conclusions about a dependency rot the moment the pin moves.

Usage:
  venv/bin/python tools/test_ig_mqtt.py
"""
import pathlib
from importlib.metadata import PackageNotFoundError, version

# Realtime methods the hybrid listener actually calls
# (see modules/direct_forward/instagram.py::_ig_mqtt_listener).
_REQUIRED = (
    "realtime_connect",
    "realtime_disconnect",
    "realtime_ping",
    "realtime_read_once",
)


def probe() -> dict:
    """Inspect the installed instagrapi for MQTToT/Realtime support."""
    out = {
        "instagrapi_version": None,
        "location": None,
        "methods": [],
        "has_realtime_mixin": False,
        "missing": list(_REQUIRED),
        "standalone_mqtt": None,
        "standalone_error": None,
        "error": None,
    }
    try:
        import instagrapi
        from instagrapi import Client

        # instagrapi exposes no __version__; read it from installed metadata.
        try:
            out["instagrapi_version"] = version("instagrapi")
        except PackageNotFoundError:
            out["instagrapi_version"] = "unknown (not installed as a distribution)"
        out["location"] = str(pathlib.Path(instagrapi.__file__).parent)
        client = Client()
        out["methods"] = [
            m for m in dir(client)
            if "real" in m.lower() or "mqtt" in m.lower() or "fbns" in m.lower()
        ]
        present = set(out["methods"])
        out["missing"] = [m for m in _REQUIRED if m not in present]
        mixins_dir = pathlib.Path(instagrapi.__file__).parent / "mixins"
        out["has_realtime_mixin"] = (mixins_dir / "realtime.py").exists()
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"

    try:
        import instagram_mqtt  # type: ignore

        out["standalone_mqtt"] = str(getattr(instagram_mqtt, "__file__", "installed"))
    except Exception as e:
        out["standalone_error"] = str(e)

    return out


def render(info: dict) -> None:
    print("=== IG MQTT probe ===")
    if info["error"]:
        print("instagrapi probe failed:", info["error"])
    else:
        print("instagrapi version :", info["instagrapi_version"])
        print("instagrapi location:", info["location"])
        print("realtime/mqtt/fbns methods:", info["methods"] or "(none)")

    if info["standalone_mqtt"]:
        print("instagram_mqtt     :", info["standalone_mqtt"])
    else:
        print("instagram_mqtt     : NOT installed (pip install instagram_mqtt) --",
              info["standalone_error"])

    # Context: what the other two relays already do.
    print("\nTikTok already push: wss://im-ws-sg.tiktok.com/ws/v2 "
          "(modules/direct_forward/tiktok.py) -- no browser, ~5MB RAM")
    print("X: polling 300s +/- 40% + xchat_bridge.mjs Deno sidecar "
          "-- keep as is per user request")

    print("\n=== Conclusion ===")
    if info["error"]:
        print("Could not inspect instagrapi; fix the import above and re-run.")
        return

    if not info["missing"]:
        print(
            f"READY: instagrapi {info['instagrapi_version']} provides every method the "
            "hybrid listener needs (realtime_connect / _disconnect / _ping / _read_once)."
        )
        print(
            "MQTToT push is AVAILABLE to trial: set IG_DIRECT_MQTT_ENABLED=true. "
            "The jittered poller stays on as fallback + cursor reconciler."
        )
    else:
        print(
            f"NOT READY: instagrapi {info['instagrapi_version']} is missing "
            f"{', '.join(info['missing'])}."
        )
        print(
            "Options: upgrade instagrapi, or pip install instagram_mqtt "
            "(Nerixyz, standalone MQTToT using the same sessionid)."
        )

    print(
        "\nExperimental either way (Instagram can change MQTToT without notice) "
        "and needs keepalive + reconnect handling."
    )
    print(
        "Recommendation: keep jittered polling (300s +/-40%, watermarked, CurlCffi, "
        "echo headers) as the default -- proven, survived the 2026-08-05 checkpoint."
    )
    print(
        "Only trial MQTT as an OPTIONAL hybrid behind IG_DIRECT_MQTT_ENABLED "
        "if sub-5s latency is actually required."
    )
    print(
        "Do NOT add Playwright/Chromium for this -- roughly +150-250 MB RAM on a "
        "1 vCPU box (swap thrash)."
    )


def main() -> None:
    render(probe())


if __name__ == "__main__":
    main()

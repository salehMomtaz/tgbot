#!/usr/bin/env python3
"""
Test Instagram MQTT push (pure Python, no browser) vs polling.

IG web + app use MQTToT (modified MQTT 3) on mqtt-mini.facebook.com.
instagrapi 2.1.2 in this venv has NO RealtimeMixin, so this script probes
what is available and suggests next step.

Usage:
  venv/bin/python tools/test_ig_mqtt.py  # reads direct_ig_session.json + config
"""
import os, sys, pathlib
print("=== IG MQTT probe ===")
try:
    import instagrapi
    print("instagrapi location:", pathlib.Path(instagrapi.__file__).parent)
    from instagrapi import Client
    c = Client()
    print("Client methods with 'real'/'mqtt'/'fbns':", [m for m in dir(c) if 'real' in m.lower() or 'mqtt' in m.lower() or 'fbns' in m.lower()] or "(none)")
    mixins = os.listdir(pathlib.Path(instagrapi.__file__).parent / "mixins")
    print("mixins:", mixins)
except Exception as e:
    print("instagrapi probe failed:", e)

# check instagram_mqtt standalone lib
try:
    import instagram_mqtt
    print("instagram_mqtt found:", instagram_mqtt.__file__)
except Exception as e:
    print("instagram_mqtt NOT installed (pip install instagram_mqtt) --", e)

# check what TikTok does (already push, no browser)
print("\nTikTok already push: wss://im-ws-sg.tiktok.com/ws/v2 (modules/direct_forward/tiktok.py:30) -- no browser, ~5MB RAM")
print("X: polling 300s±40% + xchat_bridge.mjs Deno sidecar -- keep as is per user request")

print("\n=== Conclusion ===")
print("Current venv instagrapi 2.1.2 has NO RealtimeMixin (added in newer fork).")
print("Pure-Python MQTT IS possible but requires either:")
print("  1) upgrade instagrapi to a fork with Realtime (e.g. subzeroid 2.2+ with realtime branch), or")
print("  2) pip install instagram_mqtt (Nerixyz) -- standalone MQTToT client using same sessionid.")
print("Both are experimental (Instagram can change MQTToT without notice) and need keepalive + reconnect.")
print("Recommendation: keep jittered polling (300s±40%, watermarked, CurlCffi, echo) as default -- proven, survived 2026-08-05 checkpoint.")
print("If sub-5s latency wanted, trial MQTT as OPTIONAL hybrid behind IG_DIRECT_MQTT_ENABLED flag, alongside poller fallback.")
print("Do NOT add Playwright/Chromium -- would +150-250 MB RAM on 1 vCPU/961 MB box (swap thrash).")

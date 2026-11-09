"""MQTT push safety tests (modules/direct_forward/ig_realtime.py, 2026-11-09).

The operator requirement is absolute: the push path must NEVER log in, never
create a second client/device, and never retry a rejected session. These tests
use a fake IGClient with NO login methods, so any accidental login call would
raise AttributeError and fail the test.

Standalone: ``venv/bin/python tests/test_ig_realtime.py``.
"""

from __future__ import annotations

import ast
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.direct_forward import ig_realtime  # noqa: E402


class FakeTransport:
    def __init__(self):
        self.sock = None


class FakeRT:
    def __init__(self, fail_connect=False, fail_sub=None):
        self.fail_connect = fail_connect
        self.fail_sub = fail_sub
        self.handlers = {}
        self.connected = False
        self.subscribed = 0
        self.transport = FakeTransport()

    def on(self, event, handler):
        self.handlers[event] = handler

    def connect(self):
        if self.fail_connect:
            raise ConnectionError("connection refused")
        self.connected = True

    def direct_subscribe(self):
        self.subscribed += 1
        if self.fail_sub:
            raise self.fail_sub

    def disconnect(self):
        self.connected = False


class FakeClient:
    """Deliberately exposes NO login method: any login attempt fails loudly."""

    def __init__(self, rt):
        self._rt = rt

    def realtime_client(self):
        return self._rt


def _make(rt, **kwargs):
    loop = types.SimpleNamespace(call_soon_threadsafe=lambda fn: fn())
    return ig_realtime.IGRealtime(FakeClient(rt), loop, on_push=lambda: None,
                                  **kwargs)


def test_start_uses_the_same_client_and_subscribes_once():
    rt = FakeRT()
    realtime = _make(rt)
    # The reader needs a real socket; stub it so start() only tests the setup.
    realtime._reader = lambda: None
    assert realtime.start() is True
    assert rt.connected is True
    assert rt.subscribed == 1
    assert set(rt.handlers) == {"message", "direct"}
    realtime.stop()


def test_connect_failure_propagates_without_subscribe_or_login():
    rt = FakeRT(fail_connect=True)
    realtime = _make(rt)
    realtime._reader = lambda: None
    try:
        realtime.start()
    except ConnectionError:
        pass
    else:
        raise AssertionError("expected ConnectionError")
    assert rt.subscribed == 0, "must not call direct_subscribe after a failed connect"
    realtime.stop()


def test_auth_rejection_is_classified_for_the_worker_latch():
    from utils import ig_session_guard

    rt = FakeRT(fail_sub=RuntimeError("login_required"))
    realtime = _make(rt)
    realtime._reader = lambda: None
    try:
        realtime.start()
    except RuntimeError as e:
        assert ig_session_guard.is_auth_failure(e), \
            "the worker must arm the session-dead latch on this error"
    else:
        raise AssertionError("expected the subscribe error")


def test_noise_filter_ignores_typing_presence_and_seen():
    noise = [
        {"path": "/direct_v2/threads/1/activities", "value": {"activity_status": "1"}},
        {"path": "/direct_v2/threads/1/presence", "value": {"is_active": True}},
        {"path": "/direct_v2/threads/1/seen", "value": {"item_id": "1"}},
        {"message": {"path": "/direct_v2/threads/1/typing", "value": "1"}},
        {"action": "mark_seen", "value": {}},
    ]
    for ev in noise:
        assert ig_realtime.IGRealtime._is_noise(ev) is True, ev

    new_item = {"message": {"path": "/direct_v2/threads/1/items/2",
                            "op": "add", "value": {"item_type": "text"}}}
    assert ig_realtime.IGRealtime._is_noise(new_item) is False


def test_connection_packet_uses_the_same_session_and_device():
    """No network: build the MQTToT CONNECT packet from a fake client."""
    from instagrapi.realtime.client import RealtimeClient
    from instagrapi.realtime.mqttot import write_connect_packet, decode_packet

    class C:
        sessionid = "2210793164%3Aabcdefghijklmnopqrstuvwxyz0123456789"
        phone_id = "4d57095e-f37c-49e9-82b7-aacb29928bf5"
        user_agent = "Instagram 428.0.0.47.67 Android (34/14; 480dpi; 1344x2992; Google/google; Pixel 8 Pro; husky; en_US; 961145276)"
        user_id = 2210793164
        locale = "cs_CZ"
        capabilities = "3brTv10="
        app_version = "428.0.0.47.67"
        proxy = None
        device_settings = {"app_version": "428.0.0.47.67"}

    rt = RealtimeClient(C(), transport=FakeTransport())
    conn = rt.build_connection()
    assert conn.client_info["userId"] == 2210793164
    assert conn.client_info["deviceId"] == C.phone_id
    assert conn.password.startswith("sessionid=")
    decoded = decode_packet(write_connect_packet(conn))
    assert decoded.packet_type == "connect"


def test_module_never_calls_a_login_method():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "modules", "direct_forward", "ig_realtime.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr in ("login", "login_by_sessionid"):
            raise AssertionError(f"forbidden login call: .{fn.attr}()")
        if isinstance(fn, ast.Name) and fn.id in ("IGClient", "Client"):
            raise AssertionError(f"forbidden client construction: {fn.id}()")


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

# modules/direct_forward/ig_realtime.py
"""MQTT (MQTToT/IRIS) push for the Instagram DM worker.

Operator requirement (2026-11-09): relay Instagram self-DM / paired-DM links in
real time WITHOUT inbox polling — but the push path must **never** log in,
create a second client/device, or retry a rejected session.

Safety model (read this before changing anything):

* Uses the **SAME authenticated ``IGClient``** the DM worker just logged in
  with: same ``sessionid``, ``phone_id``, ``user_agent``, locale and proxy.
  There is no ``IGClient()``, no ``login_by_sessionid`` and no new device
  identity anywhere in this module.
* ``start()`` opens the MQTT socket (no HTTP) and then calls
  ``direct_subscribe()`` exactly once. That is the ONLY HTTP request; it reads
  ``seq_id``/``snapshot_at_ms`` from ``direct_threads(amount=1)``.
* The reader thread performs **no HTTP** — it only reads the already-open
  socket, sends PINGREQ every ``ping_seconds`` and signals the asyncio loop.
  instagrapi's ``requests.Session`` is never touched from this thread.
* This module never retries by itself. The worker owns a **bounded** reconnect
  policy; an auth-classified failure arms the persistent session-dead latch
  (``utils.ig_session_guard``) and stops all Instagram traffic. A plain network
  failure falls back to the normal polling loop.
* Typing / presence / seen events are ignored so only a real new item triggers
  a poll.
"""

from __future__ import annotations

import logging
import select
import threading
import time

logger = logging.getLogger(__name__)


class IGRealtime:
    def __init__(self, cl, loop, on_push, on_down=None,
                 log_prefix: str = "[DirectForward/IG]", ping_seconds: int = 15):
        self.cl = cl
        self.loop = loop
        self.on_push = on_push
        self.on_down = on_down
        self.log_prefix = log_prefix
        self.ping_seconds = max(5, int(ping_seconds or 15))
        self._rt = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._healthy = False
        self._last_error: BaseException | None = None
        self.push_count = 0
        self.last_push_at = 0.0

    # ------------------------------------------------------------------ state
    @property
    def healthy(self) -> bool:
        return bool(self._healthy and self._thread and self._thread.is_alive())

    @property
    def last_error(self) -> BaseException | None:
        return self._last_error

    # -------------------------------------------------------------- lifecycle
    def start(self) -> bool:
        """Connect + subscribe. MUST run in the worker's executor thread.

        ``direct_subscribe()`` performs one authenticated HTTP request, so this
        is deliberately not called on the event loop. Raises on failure; the
        caller decides whether it is auth-classified (latch) or transient
        (bounded reconnect). Never logs in.
        """
        self.stop()
        rt = self.cl.realtime_client()          # wraps the SAME client
        # Assign before connect so a failed CONNACK still lets stop() close the
        # half-open socket (bounded retries must not leak fds).
        self._rt = rt
        rt.on("message", self._on_message)
        rt.on("direct", self._on_direct)
        rt.connect()                            # MQTT socket only, no HTTP
        try:
            rt.direct_subscribe()               # the ONE HTTP call
        except BaseException:
            try:
                rt.disconnect()
            except Exception:
                pass
            raise
        self._stop.clear()
        self._last_error = None
        self._thread = threading.Thread(
            target=self._reader, name="ig-mqtt-reader", daemon=True)
        self._thread.start()
        self._healthy = True
        logger.info(f"{self.log_prefix} MQTT push connected (same device identity, "
                    f"no polling while idle)")
        return True

    def stop(self) -> None:
        self._healthy = False
        self._stop.set()
        rt = self._rt
        self._rt = None
        try:
            if rt is not None and getattr(rt, "transport", None) is not None:
                sock = getattr(rt.transport, "sock", None)
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass
        except Exception:
            pass
        th = self._thread
        self._thread = None
        if th is not None and th.is_alive() and th is not threading.current_thread():
            try:
                th.join(timeout=3)
            except Exception:
                pass
        try:
            if rt is not None:
                rt.disconnect()
        except Exception:
            pass

    # ---------------------------------------------------------------- events
    def _signal(self) -> None:
        self.push_count += 1
        self.last_push_at = time.time()
        try:
            self.loop.call_soon_threadsafe(self.on_push)
        except RuntimeError:
            pass

    def _on_message(self, payload) -> None:
        # A new direct item / thread patch. Wake the poller immediately, unless
        # it is a read-receipt / presence patch rather than a new item.
        if self._is_noise(payload):
            return
        self._signal()

    def _on_direct(self, event) -> None:
        if self._is_noise(event):
            return
        self._signal()

    @staticmethod
    def _is_noise(event) -> bool:
        """True for typing / presence / seen events (they must not poll)."""
        if not isinstance(event, dict):
            return False
        # The 'message' event wraps the patch under {"message": {...}}.
        if isinstance(event.get("message"), dict):
            event = event["message"]
        path = str(event.get("path") or "").lower()
        action = str(event.get("action") or "").lower()
        if any(m in path for m in ("typing", "activity", "presence",
                                   "seen", "read_receipt")):
            return True
        if any(m in action for m in ("mark_seen", "indicate_activity", "typing")):
            return True
        value = event.get("value")
        if isinstance(value, dict):
            if any(k in value for k in ("activity_status", "is_active",
                                        "last_active", "seen")):
                return True
        return False

    # ---------------------------------------------------------------- reader
    def _reader(self) -> None:
        rt = self._rt
        if rt is None:
            return
        try:
            from instagrapi.realtime.mqttot import write_pingreq_packet
        except Exception as e:  # pragma: no cover - import guard
            self._fail(e)
            return
        sock = getattr(getattr(rt, "transport", None), "sock", None)
        if sock is None:
            self._fail(RuntimeError("MQTT transport has no socket"))
            return
        try:
            # A short timeout is a safety net only: select() gates every read,
            # so a timeout mid-packet means the stream is broken → reconnect.
            sock.settimeout(10.0)
        except Exception:
            pass

        last_ping = time.time()
        while not self._stop.is_set():
            try:
                readable, _, _ = select.select([sock], [], [], 1.0)
            except Exception as e:
                self._fail(e)
                return
            if self._stop.is_set():
                break
            if readable:
                try:
                    rt.read_once()
                except Exception as e:
                    self._fail(e)
                    return
            now = time.time()
            if now - last_ping >= self.ping_seconds:
                try:
                    sock.sendall(write_pingreq_packet())
                    last_ping = now
                except Exception as e:
                    self._fail(e)
                    return

    def _fail(self, exc: BaseException) -> None:
        if self._stop.is_set():
            return
        self._healthy = False
        self._last_error = exc
        logger.warning(f"{self.log_prefix} MQTT push disconnected ({exc}) — "
                       f"the worker will reconnect (bounded, NO login)")
        if self.on_down is not None:
            try:
                self.loop.call_soon_threadsafe(self.on_down)
            except RuntimeError:
                pass

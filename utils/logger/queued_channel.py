# utils/logger/queued_channel.py
"""
Shared base for the Telegram-channel log handlers (main log channel + bale_log).

WHY THIS EXISTS — the 2026-09-15 "can't start new thread" incident
-----------------------------------------------------------------
Both channel handlers used to do ``threading.Thread(target=post, daemon=True).start()``
PER LOG RECORD. That is an *unbounded* thread factory: under normal conditions
each thread finished in a few ms and vanished, but the moment the VPS's
connection to api.telegram.org degrades (Iran's intermittent throttling; the
02:20-02:28 window on 2026-09-15), every POST blocks for its full 5 s timeout
(plus another 5 s for the plain-text fallback). A busy subsystem — the IG
friend-media archive logs every private API call — emits thousands of lines,
each spawning a thread that lives for up to 10 s. Thread count exploded past
the process limit (``LimitNPROC=512``), every subsequent ``run_in_executor``
(including pyrogram's own session restart) raised ``RuntimeError("can't start
new thread")``, and the bot's network sessions began crash-looping. 38 such
errors landed in the log channel before it recovered.

THE FIX: one bounded queue + a small fixed worker pool per channel.
``emit()`` just formats the record and does ``put_nowait`` — O(1), never blocks,
never spawns a thread after warmup. A constant number of daemon workers drain
the queue and perform the HTTP POST. When the queue is full (the remote is
down / way behind), records are DROPPED instead of accumulating memory and
threads — a bounded-lag logger beats a process-killing one. Thread count is
therefore constant (WORKERS per channel) no matter the log volume or network
state.

The public surface is unchanged: subclasses keep their names, constructors and
``emit()`` semantics; the two channel handlers just inherit the plumbing.
"""

import html
import logging
import queue
import sys
import threading
import time

import requests


class QueuedChannelHandler(logging.Handler):
    """Base handler: format on the emitting thread, POST on a bounded worker pool.

    Subclasses set :attr:`WORKERS` / :attr:`MAX_QUEUE` and may override
    :meth:`_endpoint_pair` (the two Bot API URLs) — the default targets Telegram.
    """

    #: Number of POST workers. Kept tiny on purpose (2x 5-10 s posts still drain
    #: faster than a busy bot emits). More workers would only re-order lines.
    WORKERS = 2
    #: Bounded backlog. At ~1 KB/record this is well under a few MB.
    MAX_QUEUE = 1000

    def __init__(self, bot_token: str, channel_id: int):
        super().__init__()
        self.bot_token = bot_token
        self.channel_id = channel_id
        self.api_url, self.api_rich_url = self._endpoint_pair()
        self._queue: "queue.Queue[dict]" = queue.Queue(maxsize=self.MAX_QUEUE)
        self._workers: list[threading.Thread] = []
        self._workers_lock = threading.Lock()
        self._dropped = 0

    def _endpoint_pair(self) -> tuple[str, str]:
        base = f"https://api.telegram.org/bot{self.bot_token}"
        return f"{base}/sendMessage", f"{base}/sendRichMessage"

    # ------------------------------------------------------------------ emit
    def emit(self, record):
        try:
            payload = self._build_payload(record)
            if payload is None:
                return
            self._ensure_workers()
            try:
                self._queue.put_nowait(payload)
            except queue.Full:
                # Dropping is deliberate: the remote is unreachable or far
                # behind, and buffering forever is what caused the incident.
                self._dropped += 1
                if self._dropped in (1, 10, 100) or self._dropped % 1000 == 0:
                    try:
                        sys.stderr.write(
                            f"[logger] {self.__class__.__name__}: queue full, "
                            f"dropped {self._dropped} log record(s) so far\n")
                    except Exception:
                        pass
        except Exception:
            # A logger must never raise from emit().
            pass

    def _build_payload(self, record) -> dict | None:
        log_entry = self.format(record)
        try:
            from utils.security import redact_token as _redact
            log_entry = _redact(log_entry)
        except Exception:
            pass
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(record.created))
        level = record.levelname
        module = record.module
        emoji = "📝"
        if level == "WARNING":
            emoji = "⚠️"
        elif level in ("ERROR", "CRITICAL"):
            emoji = "🚨"
        escaped_entry = html.escape(log_entry)
        # Rich messages via sendRichMessage allow 32768 UTF-8 chars (vs 4096
        # for sendMessage). Keep ~100 chars for the wrapper.
        if len(escaped_entry) > 31500:
            escaped_entry = escaped_entry[:31500] + "\n... [TRUNCATED at 32768 rich limit] ..."
        rich_html = (
            f"{emoji} <b>[{level}]</b> <code>[{timestamp}]</code> <i>({module})</i>\n"
            f"<pre>{escaped_entry}</pre>"
        )
        return {
            "rich": {"chat_id": self.channel_id, "rich_message": {"html": rich_html}},
            "plain": {"chat_id": self.channel_id, "text": rich_html, "parse_mode": "HTML"},
        }

    # --------------------------------------------------------------- workers
    def _ensure_workers(self) -> None:
        if len(self._workers) >= self.WORKERS and all(t.is_alive() for t in self._workers):
            return
        with self._workers_lock:
            alive = [t for t in self._workers if t.is_alive()]
            while len(alive) < self.WORKERS:
                t = threading.Thread(
                    target=self._worker,
                    name=f"logch-{self.channel_id}-{len(alive)}",
                    daemon=True,
                )
                t.start()
                alive.append(t)
            self._workers = alive

    def _worker(self) -> None:
        while True:
            payload = self._queue.get()
            try:
                if payload is None:
                    return
                self._post(payload)
            except Exception:
                pass
            finally:
                self._queue.task_done()

    def _post(self, payload: dict) -> None:
        try:
            import config
            proxies = (
                {"http": config.REQUESTS_PROXY, "https": config.REQUESTS_PROXY}
                if getattr(config, "REQUESTS_PROXY", None) else None
            )
            resp = requests.post(self.api_rich_url, json=payload["rich"],
                                 timeout=5, proxies=proxies)
            ok = resp.status_code == 200 and resp.json().get("ok", False)
            if not ok:
                requests.post(self.api_url, json=payload["plain"],
                              timeout=5, proxies=proxies)
        except Exception:
            pass

    def close(self):
        """Stop workers on logging shutdown. Best-effort; daemon threads would
        die with the process regardless."""
        try:
            for _ in range(len(self._workers)):
                try:
                    self._queue.put_nowait(None)
                except Exception:
                    pass
        finally:
            super().close()

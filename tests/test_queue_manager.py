"""Regression tests for ``utils/queue_manager.DownloadQueue``.

Guards two production incidents-in-waiting found by the 2026-11-06 audit:

* **Wedge on worker cancellation** — ``_worker``'s ``finally`` only cleared
  ``_active`` when ``_pending`` was empty, so a cancelled/crashed worker with
  queued tasks left ``_active=True`` forever: every later ``add_task`` saw the
  engine as busy and never spawned a worker. The queue stopped draining until
  a restart. ``_active`` is now reset unconditionally and exactly one
  replacement worker is respawned when work remains.
* **Unbounded pending list** — ``MAX_QUEUE_DEPTH`` (utils/shared.py) existed on
  paper only; ``_pending`` grew without limit, pinning a pyrogram Message +
  closure per waiting job. ``add_task`` now rejects with a user-visible notice.

Standalone: ``venv/bin/python tests/test_queue_manager.py`` (pytest-compatible).
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.queue_manager import DownloadQueue  # noqa: E402


class FakeMessage:
    """Minimal pyrogram-Message stand-in: records edit_text calls."""

    def __init__(self) -> None:
        self.edits: list[str] = []

    async def edit_text(self, text: str) -> None:
        self.edits.append(text)


def test_worker_respawns_after_cancel():
    """Cancelling the worker mid-drain must not wedge the queue."""

    async def main():
        q = DownloadQueue()
        started = asyncio.Event()
        gate = asyncio.Event()
        ran: list[str] = []

        async def job(name: str):
            ran.append(f"start:{name}")
            if name == "a":
                started.set()
            await gate.wait()
            ran.append(f"done:{name}")

        messages = [FakeMessage() for _ in range(3)]
        await q.add_task(1, messages[0], lambda: job("a"))
        await q.add_task(2, messages[1], lambda: job("b"))
        await q.add_task(3, messages[2], lambda: job("c"))

        await asyncio.wait_for(started.wait(), timeout=5)
        # Job "a" is in flight; "b" and "c" are pending.
        assert len(q._pending) == 2

        q._worker_task.cancel()
        try:
            await q._worker_task
        except asyncio.CancelledError:
            pass

        # The cancelled item is intentionally lost; the QUEUE must survive.
        gate.set()
        for _ in range(500):
            if "done:b" in ran and "done:c" in ran:
                break
            await asyncio.sleep(0.01)

        assert "done:b" in ran, f"queue wedged after cancel: {ran}"
        assert "done:c" in ran, f"queue wedged after cancel: {ran}"
        await asyncio.sleep(0.05)
        assert q._active is False, "worker must leave the engine idle when drained"

    asyncio.run(main())


def test_queue_depth_cap_rejects_new_task():
    """MAX_QUEUE_DEPTH must be enforced, with a clear user-facing notice."""

    async def main():
        import utils.shared as shared

        old_depth = shared.MAX_QUEUE_DEPTH
        shared.MAX_QUEUE_DEPTH = 0  # every add is over the cap
        try:
            q = DownloadQueue()
            msg = FakeMessage()

            async def job():
                pass

            task_id = await q.add_task(7, msg, job)
            assert task_id == "", "rejected task must not pretend to be queued"
            assert q._pending == [], "rejected task must not enter the queue"
            assert any("queue is full" in e.lower() for e in msg.edits), msg.edits
        finally:
            shared.MAX_QUEUE_DEPTH = old_depth

    asyncio.run(main())


def test_priority_ordering_is_stable():
    """Pro (3) jumps ahead of basic (1); FIFO within the same tier."""

    async def main():
        q = DownloadQueue()
        q._active = True  # pretend a job is running so add_task never spawns

        async def job():
            pass

        await q.add_task(1, FakeMessage(), job, priority=0)
        await q.add_task(2, FakeMessage(), job, priority=3)
        await q.add_task(3, FakeMessage(), job, priority=1)
        await q.add_task(4, FakeMessage(), job, priority=1)

        assert [t.priority for t in q._pending] == [3, 1, 1, 0], \
            [t.priority for t in q._pending]
        assert [t.user_id for t in q._pending] == [2, 3, 4, 1]

    asyncio.run(main())


def _run() -> int:
    tests = sorted(
        (n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)
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

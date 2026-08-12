import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List
from uuid import uuid4

log = logging.getLogger(__name__)

@dataclass
class QueueTask:
    user_id: int
    message: object                       # Pyrogram Message to update with status
    coroutine: Callable[[], Awaitable]    # Zero-argument async function to run when ready
    task_id: str = field(default_factory=lambda: uuid4().hex)
    priority: int = 1                     # subscription priority (0=free, 3=pro)

class DownloadQueue:
    """
    Synthesized Queue Manager:
    Combines Claude's structural worker safety with DeepSeek's non-blocking lock performance.
    """
    def __init__(self) -> None:
        self._pending: List[QueueTask] = []
        self._lock = asyncio.Lock()       # ONLY guards the in-memory list operations
        self._active = False              # Indicates if a task is currently executing

    async def add_task(self, user_id: int, message, coroutine, priority: int | None = None) -> str:
        """Enqueue a task and run it immediately if the engine is idle.

        priority: 0 (free) .. 3 (pro). Higher priority jumps ahead of lower ones.
        If None, resolved from subscription tier (free=0, paid 1-3). Free users never
        jump ahead of paid ones, but FIFO is preserved within same priority.
        """
        if priority is None:
            try:
                from utils.subscription.tiers import TIERS
                from utils.subscription.store import is_subscription_active
                active, sub = is_subscription_active(user_id)
                if active and sub:
                    tier = sub.get("tier") or "free"
                    priority = TIERS.get(tier, {}).get("priority", 1)
                else:
                    from utils.subscription.store import get_settings
                    s = get_settings()
                    if s.get("enabled") and not active:
                        priority = 0
                    else:
                        priority = 1
            except Exception:
                priority = 1
        task = QueueTask(user_id=user_id, message=message, coroutine=coroutine, priority=int(priority or 1))

        async with self._lock:
            # insert by priority (stable): find first lower-priority item
            insert_at = len(self._pending)
            for idx, t in enumerate(self._pending):
                if t.priority < task.priority:
                    insert_at = idx
                    break
            self._pending.insert(insert_at, task)
            position = insert_at + 1
            start_worker = not self._active
            if start_worker:
                self._active = True

        # The lock is now RELEASED. We can execute network IO without blocking others.
        if start_worker:
            asyncio.create_task(self._worker())
        else:
            await self._safe_edit(
                message,
                f"⏳ Your request has been queued. Position in line: #{position}",
            )
        return task.task_id

    async def _worker(self) -> None:
        """Drains the queue sequentially using a secure, continuous worker loop."""
        try:
            while True:
                # 1. Acquire the lock briefly to pop the next task
                async with self._lock:
                    if not self._pending:
                        self._active = False
                        return
                    task = self._pending.pop(0)

                # 2. Re-number and update remaining queued users (Outside the lock!)
                await self._refresh_positions()

                # 3. Process the current active task (Outside the lock!)
                await self._safe_edit(task.message, "⚡ Now processing your request...")
                
                try:
                    await task.coroutine()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.exception("Task %s failed for user %d", task.task_id, task.user_id)
                    await self._safe_edit(
                        task.message,
                        f"❌ Your request failed: {exc}"
                    )
        finally:
            # Re-verify and ensure the system is never left wedged in an active state
            async with self._lock:
                if not self._pending:
                    self._active = False

    async def _refresh_positions(self) -> None:
        """Edit waiting status messages without blocking new incoming queue requests."""
        # Briefly copy the list under lock protection
        async with self._lock:
            waiting_copy = list(self._pending)

        # Release the lock and update each user sequentially
        for idx, task in enumerate(waiting_copy, start=1):
            await self._safe_edit(
                task.message,
                f"⏳ Your request has been queued. Position in line: #{idx}"
            )

    @staticmethod
    async def _safe_edit(message, text: str) -> None:
        """Gracefully handle Telegram API constraints without halting the system."""
        try:
            await message.edit_text(text)
        except Exception as exc:
            log.warning("Could not edit status message: %r", exc)

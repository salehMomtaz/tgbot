"""
Direct-forward supervisor: starts and manages the platform workers.

Mirrors the original modules/direct_forward.py supervisor section exactly.
"""

import asyncio
import logging

import config
from utils.shared import queue

logger = logging.getLogger(__name__)

# Watchdog tuning: after a cold boot, give workers time to log in and complete
# a first poll (IG especially: login + cold-start jitter can take minutes).
_WATCHDOG_GRACE_SECONDS = 15 * 60
_WATCHDOG_THRESHOLD_SECONDS = 30 * 60
_WATCHDOG_CHECK_SECONDS = 5 * 60


async def _relay_watchdog(bot_client, chat_id: int, platforms: list) -> None:
    """DM the operator when a direct-forward worker goes silent.

    Each worker stamps an in-memory heartbeat (``mark_worker_alive``) at the top
    of a completed iteration. If a platform that is *enabled* stops stamping for
    longer than the threshold, the relay is down even though the bot process is
    alive — exactly the 2026-09-15 case where "Abort Operations" silently killed
    IG/X and nobody noticed for 11 h. Alerts once per silent streak.
    """
    from utils.shared import worker_silent_seconds

    await asyncio.sleep(_WATCHDOG_GRACE_SECONDS)
    alerted: set[str] = set()
    while True:
        try:
            for key, label in platforms:
                silent = worker_silent_seconds(key)
                if silent is None:
                    continue
                if silent > _WATCHDOG_THRESHOLD_SECONDS and key not in alerted:
                    alerted.add(key)
                    try:
                        await bot_client.send_message(
                            chat_id,
                            (f"⚠️ **{label} direct-forward looks stalled**\n\n"
                             f"No completed poll for ~{int(silent // 60)} min. The "
                             f"relay is down, but the bot process may still be up. "
                             f"Check Admin → 📨 Direct-Forward and the log channel. "
                             f"A restart (Admin → 🔄 Restart) or a fresh cookie jar "
                             f"usually clears it."),
                        )
                        logger.warning(f"[DirectForward/watchdog] {label} silent "
                                       f"{int(silent // 60)}m — operator alerted.")
                    except Exception as e:
                        logger.warning(f"[DirectForward/watchdog] alert failed: {e}")
                elif silent <= _WATCHDOG_THRESHOLD_SECONDS and key in alerted:
                    alerted.discard(key)
        except Exception:
            pass
        await asyncio.sleep(_WATCHDOG_CHECK_SECONDS)


async def _direct_forward_supervisor(bot_client, premium_client, chat_id: int) -> None:
    workers = []
    platforms = []
    if config.IG_DIRECT_ENABLED:
        from .instagram import _instagram_worker
        workers.append(_instagram_worker(bot_client, premium_client, chat_id, queue))
        platforms.append(("ig", "Instagram"))
    if config.X_DIRECT_ENABLED:
        from .twitter import _twitter_worker
        workers.append(_twitter_worker(bot_client, premium_client, chat_id, queue))
        platforms.append(("x", "X"))
    if getattr(config, "TIKTOK_DIRECT_ENABLED", False):
        from .tiktok import _tiktok_worker
        workers.append(_tiktok_worker(bot_client, premium_client, chat_id, queue))
        platforms.append(("tiktok", "TikTok"))

    if not workers:
        logger.info("[DirectForward] No platform enabled (IG_DIRECT_ENABLED / "
                    "X_DIRECT_ENABLED / TIKTOK_DIRECT_ENABLED) — direct-forward is off.")
        return

    logger.info(f"[DirectForward] started -> chat {chat_id}, {len(workers)} platform(s)")
    if platforms:
        workers.append(_relay_watchdog(bot_client, chat_id, platforms))
        logger.info("[DirectForward] relay watchdog armed "
                    f"(silent > {_WATCHDOG_THRESHOLD_SECONDS // 60}m → operator DM)")
    # One worker crashing (network blip, bad jar) must not take the other
    # platforms down with it — each worker already loops forever, so an
    # exception here means the worker truly died; log it and keep the rest.
    results = await asyncio.gather(*workers, return_exceptions=True)
    for res in results:
        if isinstance(res, BaseException) and not isinstance(res, asyncio.CancelledError):
            logger.error(f"[DirectForward] worker died: {res!r}")


def start_direct_forward_task(bot_client, premium_client):
    """Create the background task. Called from main.py after clients are up.
    Returns the task, or None when the feature is unconfigured (no-op)."""
    chat_id = getattr(config, "DIRECT_FORWARD_CHAT_ID", 0)
    if not chat_id:
        logger.info("[DirectForward] DIRECT_FORWARD_CHAT_ID not set; direct-forward disabled.")
        return None
    if not (config.IG_DIRECT_ENABLED or config.X_DIRECT_ENABLED
            or getattr(config, "TIKTOK_DIRECT_ENABLED", False)):
        logger.info("[DirectForward] No platform enabled; direct-forward disabled.")
        return None
    return asyncio.create_task(_direct_forward_supervisor(bot_client, premium_client, chat_id))
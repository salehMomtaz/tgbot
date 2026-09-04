"""
Direct-forward supervisor: starts and manages the platform workers.

Mirrors the original modules/direct_forward.py supervisor section exactly.
"""

import asyncio
import logging

import config
from utils.shared import queue

logger = logging.getLogger(__name__)


async def _direct_forward_supervisor(bot_client, premium_client, chat_id: int) -> None:
    workers = []
    if config.IG_DIRECT_ENABLED:
        from .instagram import _instagram_worker
        workers.append(_instagram_worker(bot_client, premium_client, chat_id, queue))
    if config.X_DIRECT_ENABLED:
        from .twitter import _twitter_worker
        workers.append(_twitter_worker(bot_client, premium_client, chat_id, queue))
    if getattr(config, "TIKTOK_DIRECT_ENABLED", False):
        from .tiktok import _tiktok_worker
        workers.append(_tiktok_worker(bot_client, premium_client, chat_id, queue))

    if not workers:
        logger.info("[DirectForward] No platform enabled (IG_DIRECT_ENABLED / "
                    "X_DIRECT_ENABLED / TIKTOK_DIRECT_ENABLED) — direct-forward is off.")
        return

    logger.info(f"[DirectForward] started -> chat {chat_id}, {len(workers)} platform(s)")
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
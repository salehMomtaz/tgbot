# utils/logger/bale.py
"""
Bale-side log channel handler (``BALE_LOG_CHANNEL_ID``).

IMPORTANT: despite the name, the destination is a **Telegram** channel
(``bale_log``), sent through ``api.telegram.org`` with the same ``BOT_TOKEN`` —
NOT through ``tapi.bale.ai``. Bale is government-owned, so Bale-side records
that may contain sensitive information must never transit it. The main channel
and this one are kept isolated by the filters installed in
``main.py::setup_system_logger``; the plumbing (bounded queue + fixed worker
pool) is shared via :class:`~utils.logger.queued_channel.QueuedChannelHandler`.
"""

from .queued_channel import QueuedChannelHandler


class BaleChannelHandler(QueuedChannelHandler):
    """Same transport/format as :class:`~utils.logger.telegram.TelegramChannelHandler`,
    but for the ``bale_log`` Telegram channel and with a single worker (lower volume)."""

    WORKERS = 1
    MAX_QUEUE = 500

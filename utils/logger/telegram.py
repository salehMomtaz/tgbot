# utils/logger/telegram.py
"""
Main Telegram log-channel handler (``LOG_CHANNEL_ID``).

Formatting + POST plumbing lives in :class:`~utils.logger.queued_channel.QueuedChannelHandler`
so the channel can never exhaust process threads (see that module's docstring
for the 2026-09-15 "can't start new thread" incident). This class only pins the
public name and the main-channel worker budget.
"""

from .queued_channel import QueuedChannelHandler


class TelegramChannelHandler(QueuedChannelHandler):
    """Pipes root-logger records to ``LOG_CHANNEL_ID`` via sendRichMessage
    (sendMessage fallback), formatted/redacted/truncated/HTML-escaped, drained
    by a small fixed pool of daemon threads fed from a bounded queue."""

    # Main channel is the busy one (pyrogram + direct-forward + downloader).
    WORKERS = 2
    MAX_QUEUE = 1000

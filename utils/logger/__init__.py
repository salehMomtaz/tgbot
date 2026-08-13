# utils/logger — split architecture: local file + Telegram channel + Bale (Telegram) channel
# Each handler lives in its own file for coherency (small files > one massive file).
# Re-export for backwards compatibility: `from utils.logger import TelegramChannelHandler` still works.

from .local import ensure_local_log_handler
from .telegram import TelegramChannelHandler
from .bale import BaleChannelHandler

__all__ = ["ensure_local_log_handler", "TelegramChannelHandler", "BaleChannelHandler"]

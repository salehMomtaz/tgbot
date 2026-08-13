# utils/logger/local.py
import logging
import logging.handlers
import os

def ensure_local_log_handler(log_dir: str = "logs", log_file: str = "bot.log") -> logging.Handler:
    """
    Create and return a rotating file handler that mirrors every log line to a
    local file under logs/. This lets you (and the admin) read the same logs that
    are streamed to the Telegram log channel, without touching that channel
    forwarding. Rotation keeps the disk footprint bounded.

    Returns a NullHandler on any failure so the bot still runs even if the disk
    is read-only or the directory is unwritable.
    """
    try:
        os.makedirs(log_dir, exist_ok=True)
        file_path = os.path.join(log_dir, log_file)
        handler = logging.handlers.RotatingFileHandler(
            filename=file_path,
            maxBytes=5 * 1024 * 1024,   # 5 MB per file
            backupCount=3,              # keep bot.log, bot.log.1, bot.log.2, bot.log.3
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
        )
        handler.setLevel(logging.INFO)
        return handler
    except Exception:
        # If file logging fails for any reason, return a do-nothing handler so the bot still runs.
        return logging.NullHandler()

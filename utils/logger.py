# utils/logger.py
import logging
import logging.handlers
import os
import time
import html
import threading
import requests


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


class TelegramChannelHandler(logging.Handler):
    """
    Custom Python Logging Handler:
    Intercepts the system's root logger outputs and pipes them securely
    to your private Telegram log channel in real-time.
    Runs asynchronously inside non-blocking daemon threads to prevent network bottlenecks.
    """
    def __init__(self, bot_token: str, channel_id: int):
        super().__init__()
        self.bot_token = bot_token
        self.channel_id = channel_id
        self.api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self.api_rich_url = f"https://api.telegram.org/bot{bot_token}/sendRichMessage"

    def emit(self, record):
        try:
            log_entry = self.format(record)
            # redact any accidental token leakage
            try:
                from utils.security import redact_token as _redact
                log_entry = _redact(log_entry)
            except Exception:
                pass

            # Format timestamp and level tags
            timestamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(record.created))
            level = record.levelname
            module = record.module

            # Select appropriate indicators based on logging severity
            emoji = "📝"
            if level == "WARNING":
                emoji = "⚠️"
            elif level in ["ERROR", "CRITICAL"]:
                emoji = "🚨"

            # HTML-escape the log entry to prevent XML parsing exceptions on Telegram
            escaped_entry = html.escape(log_entry)

            # Truncate very long tracebacks so the payload stays under Telegram's 4096-char limit
            if len(escaped_entry) > 3500:
                escaped_entry = escaped_entry[:3500] + "\n... [TRUNCATED] ..."

            # Rich message (sendRichMessage) with a sendMessage fallback. The
            # plain text is kept byte-identical to the pre-rich format so log
            # channels render the same on any Bot API version.
            rich_html = (
                f"{emoji} <b>[{level}]</b> <code>[{timestamp}]</code> <i>({module})</i>\n"
                f"<pre>{escaped_entry}</pre>"
            )
            payload_rich = {
                "chat_id": self.channel_id,
                "rich_message": {"html": rich_html},
            }
            payload_plain = {
                "chat_id": self.channel_id,
                "text": rich_html,
                "parse_mode": "HTML",
            }

            # Define target post execution
            def execute_post():
                try:
                    import config
                    proxies = (
                        {"http": config.REQUESTS_PROXY, "https": config.REQUESTS_PROXY}
                        if getattr(config, "REQUESTS_PROXY", None) else None
                    )
                    resp = requests.post(self.api_rich_url, json=payload_rich, timeout=5, proxies=proxies)
                    ok = resp.status_code == 200 and resp.json().get("ok", False)
                    if not ok:
                        requests.post(self.api_url, json=payload_plain, timeout=5, proxies=proxies)
                except Exception:
                    pass

            # Dispatch the HTTP post in a background daemon thread so it never blocks the main loop
            threading.Thread(target=execute_post, daemon=True).start()

        except Exception:
            pass  # Safety: prevents logging exceptions from causing recursive crash loops

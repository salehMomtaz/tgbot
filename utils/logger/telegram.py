# utils/logger/telegram.py
import html
import logging
import threading
import time
import requests

class TelegramChannelHandler(logging.Handler):
    """
    Telegram log channel handler.
    Intercepts root logger outputs and pipes them to your private Telegram channel
    (LOG_CHANNEL_ID) in real-time via https://api.telegram.org/bot<token>/sendRichMessage.
    Falls back to sendMessage. Async daemon thread, redacted, truncated, HTML-escaped.
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
            elif level in ["ERROR", "CRITICAL"]:
                emoji = "🚨"
            escaped_entry = html.escape(log_entry)
            if len(escaped_entry) > 3500:
                escaped_entry = escaped_entry[:3500] + "\n... [TRUNCATED] ..."
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

            threading.Thread(target=execute_post, daemon=True).start()

        except Exception:
            pass

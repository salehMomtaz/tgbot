# utils/logger/bale.py
import html
import logging
import threading
import time
import requests

class BaleChannelHandler(logging.Handler):
    """
    Bale-side counterpart to TelegramChannelHandler — but it still sends to
    TELEGRAM, not to Bale. User clarified: `bale_log` IS A TELEGRAM CHANNEL
    (private, angelbalzac admin), not a Bale channel. Reason: Bale is
    government-owned, so Bale-side logs containing sensitive info must NOT go
    to `tapi.bale.ai` (security hole). They go to a separate Telegram channel
    `bale_log` via `api.telegram.org` with the same BOT_TOKEN, at the same INFO
    level as the main LOG_CHANNEL_ID. Both handlers use Telegram API; they are
    kept separate so Telegram logs -> LOG_CHANNEL_ID and Bale logs -> BALE_LOG_CHANNEL_ID
    stay isolated. If BALE_LOG_CHANNEL_ID is 0, Bale logs stay local only.
    """
    def __init__(self, bot_token: str, channel_id: int):
        super().__init__()
        self.bot_token = bot_token
        self.channel_id = channel_id
        # Even though this is "Bale logs", the destination is a Telegram channel
        # `bale_log` (private, angelbalzac admin) via api.telegram.org, not tapi.bale.ai.
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
            # Same rich HTML as Telegram handler (bale_log is a Telegram channel, so HTML is safe)
            # Rich 32768, not 4096 -- previous 3500 truncated your detailed 17003 dump
            escaped_entry = html.escape(log_entry)
            if len(escaped_entry) > 31500:
                escaped_entry = escaped_entry[:31500] + "\n... [TRUNCATED at 32768 rich limit] ..."
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

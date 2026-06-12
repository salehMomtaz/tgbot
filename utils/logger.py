# utils/logger.py
import logging
import time
import requests

class TelegramChannelHandler(logging.Handler):
    """
    Custom Python Logging Handler:
    Intercepts the system's root logger outputs and pipes them securely 
    to your private Telegram log channel in real-time.
    """
    def __init__(self, bot_token: str, channel_id: int):
        super().__init__()
        self.bot_token = bot_token
        self.channel_id = channel_id
        self.api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    def emit(self, record):
        try:
            log_entry = self.format(record)
            
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
                
            payload = {
                "chat_id": self.channel_id,
                "text": f"{emoji} **[{level}]** `[{timestamp}]` *({module})*\n\n`{log_entry}`",
                "parse_mode": "Markdown"
            }
            
            # Synchronously execute the HTTP post inside an isolated timeout
            requests.post(self.api_url, json=payload, timeout=5)
        except Exception:
            pass # Safety: prevents logging exceptions from causing recursive crash loops
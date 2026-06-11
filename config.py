import os

# Telegram API credentials (from my.telegram.org)
API_ID = int(os.getenv("API_ID", "YOUR_API_ID_HERE"))
API_HASH = os.getenv("API_HASH", "YOUR_API_HASH_HERE")
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

# Hardcoded Creator ID (Your numeric Telegram ID)
SYSTEM_CREATOR_ID = int(os.getenv("SYSTEM_CREATOR_ID", "YOUR_NUMERIC_ID_HERE"))

# Private Telegram Log Channel ID (e.g. -100123456789)
# To find it, add your bot to the channel as admin and read its chat ID
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))  # Leave 0 if not used

# Your Premium Account Session String (generated via generate_session.py)
# Leave as empty string "" if you do not want to use a Premium Account for 4GB uploads
PREMIUM_STRING_SESSION = os.getenv("PREMIUM_STRING_SESSION", "")

# FastAPI / Streaming configurations
# Example: "https://yourdomain.com/tgbot" (if using Nginx reverse proxy) or "http://YOUR_VPS_IP:8080"
DOMAIN = os.getenv("DOMAIN", "http://YOUR_VPS_IP:8080") 

# Database and Cookie paths
DB_FILE = "database.json"
YT_COOKIES = "ytcookies.txt"
IG_COOKIES = "igcookies.txt"
TT_COOKIES = "ttcookies.txt"
X_COOKIES = "xcookies.txt"

import os

# Telegram API credentials (from my.telegram.org)
API_ID = int(os.getenv("API_ID", "YOUR_API_ID_HERE"))
API_HASH = os.getenv("API_HASH", "YOUR_API_HASH_HERE")
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

# Hardcoded Creator ID (Your Telegram ID)
SYSTEM_CREATOR_ID = int(os.getenv("SYSTEM_CREATOR_ID", "YOUR_NUMERIC_ID_HERE"))

# FastAPI / Streaming configurations
DOMAIN = os.getenv("DOMAIN", "http://YOUR_VPS_IP:8080") 

# Database and Cookie paths
DB_FILE = "database.json"
YT_COOKIES = "ytcookies.txt"
IG_COOKIES = "igcookies.txt"
TT_COOKIES = "ttcookies.txt"

import os

# Load secrets from a .env file (if present) BEFORE reading any env vars.
# python-dotenv never overwrites real environment variables, so an explicit
# `export FOO=bar` (or systemd EnvironmentFile) always wins over .env. Putting
# load_dotenv() here (instead of only in main.py) makes config.py self-contained
# no matter which module imports it first.
from dotenv import load_dotenv

load_dotenv()


def get_env_int(key: str, default: int) -> int:
    """Read an integer env var, tolerating negative IDs (e.g. channel IDs)."""
    val = os.getenv(key, "")
    if val.isdigit() or (val.startswith("-") and val[1:].isdigit()):
        return int(val)
    return default


def _proxy_url() -> str | None:
    """Return a single SOCKS5/HTTP proxy URL from legacy or conventional env vars.

    Only set this if your VPS cannot reach YouTube/X/Instagram/TikTok directly
    (e.g. a regional block). On a normal foreign VPS, leave it unset.
    """
    for key in ("SOCKS5_PROXY", "ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY"):
        val = os.getenv(key, "").strip()
        if val and val.lower() != "none":
            return val
    return None


# =========================================================================
# Telegram API credentials (from https://my.telegram.org)
# =========================================================================
API_ID = get_env_int("API_ID", 0)
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Hardcoded Creator ID (your numeric Telegram user ID)
SYSTEM_CREATOR_ID = get_env_int("SYSTEM_CREATOR_ID", 0)

# Private Telegram Log Channel ID (e.g. -100123456789).
# To find it: add the bot to your private channel as admin, then read the chat ID.
LOG_CHANNEL_ID = get_env_int("LOG_CHANNEL_ID", 0)  # Leave 0 if not used

# Your Premium Account Session String (generated via generate_session.py).
# Leave empty ("") if you do not want 4 GB uploads via a Premium Userbot client.
PREMIUM_STRING_SESSION = os.getenv("PREMIUM_STRING_SESSION", "")

# ---------------------------------------------------------------------------
# FastAPI / Streaming configuration
# Example: "https://yourdomain.com/tgbot" (behind an Nginx reverse proxy) or
#          "http://YOUR_VPS_IP:8080" (direct).
# This base URL is embedded into the streaming links the bot hands out.
# ---------------------------------------------------------------------------
DOMAIN = os.getenv("DOMAIN", "http://YOUR_VPS_IP:8080")

# SSL Certificate Paths (optional). Leave both empty ("") to run the stream
# server over plain HTTP on port 8080. If both are provided, Uvicorn hosts the
# streaming links over native HTTPS.
SSL_CERT_PATH = os.getenv("SSL_CERT_PATH", "")
SSL_KEY_PATH = os.getenv("SSL_KEY_PATH", "")

# ---------------------------------------------------------------------------
# Optional proxy configuration (only needed on VPS in blocked networks).
# On a normal foreign VPS where YouTube/X/Instagram/TikTok are reachable, leave
# all of these unset.
# Examples:
#   SOCKS5_PROXY=socks5://127.0.0.1:10808
#   ALL_PROXY=socks5://127.0.0.1:10808
#   HTTP_PROXY=http://127.0.0.1:10809
# ---------------------------------------------------------------------------
PROXY_URL = _proxy_url()
AIOHTTP_PROXY = PROXY_URL   # Used by direct-URL aiohttp downloads
YTDLP_PROXY = PROXY_URL     # Passed to yt-dlp's 'proxy' option
REQUESTS_PROXY = PROXY_URL  # Used by utils.logger (Telegram log channel)
YTDLP_USER_AGENT = os.getenv("YTDLP_USER_AGENT", "")

# -----------------------------------------------------------------------------
# PO-token provider (bgutil-ytdlp-pot-provider) for YouTube.
#
# YouTube now requires a proof-of-origin (PO) token. The provider is ALWAYS ON
# by default: it is started at bot launch and every YouTube extraction uses
# cookies + PO token. There is no cookies-only or no-auth fallback for YouTube.
#
# The provider runs on the Deno runtime (no Node.js/npm). Deno >= 2.0 must be
# installed (./install.sh installs it). The yt-dlp plugin that talks to this
# provider is pulled in via requirements.txt (bgutil-ytdlp-pot-provider), so no
# symlink hack is needed.
# -----------------------------------------------------------------------------
YTDLP_POT_ENABLED = os.getenv("YTDLP_POT_ENABLED", "true").lower() in ("true", "1", "yes")
YTDLP_POT_PORT = get_env_int("YTDLP_POT_PORT", 4416)
YTDLP_POT_PROVIDER_PATH = os.getenv("YTDLP_POT_PROVIDER_PATH", "bgutil-provider/server")
# bgutil git ref to clone (pinned for reproducible deploys; bump deliberately).
YTDLP_POT_PROVIDER_REF = os.getenv("YTDLP_POT_PROVIDER_REF", "1.3.1")
YTDLP_POT_DENO_BIN = os.getenv("YTDLP_POT_DENO_BIN", "deno")
YTDLP_POT_PLAYER_CLIENT = os.getenv("YTDLP_POT_PLAYER_CLIENT", "mweb")

# =========================================================================
# Database and Cookie paths
# =========================================================================
DB_FILE = "database.json"
COOKIE_DIR = "cookies"
YT_COOKIES = "cookies/youtube/ytcookies.txt"
YT_COOKIES_BACKUP = "cookies/youtube/ytcookies.backup"
IG_COOKIES = "cookies/instagram/igcookies.txt"
TT_COOKIES = "cookies/tiktok/ttcookies.txt"
X_COOKIES = "cookies/twitter/xcookies.txt"
YTDLP_COOKIES_DIR = "cookies/ytdlp"
COOKIES_FILE = "cookies/ytdlp/cookies.txt"

# ---------------------------------------------------------------------------
# Playlist safety cap (videos per playlist). Protects the VPS from someone
# pasting a 1,000-video list. The bot downloads the first N and warns the user.
# ---------------------------------------------------------------------------
PLAYLIST_MAX_VIDEOS = get_env_int("PLAYLIST_MAX_VIDEOS", 50)

# ---------------------------------------------------------------------------
# Cookie lifecycle (utils/cookie_manager.py)
# COOKIE_WRITEBACK_ENABLED: merge rotated session cookies back into the real
#   jar after every successful authenticated yt-dlp run. THIS is what keeps
#   Instagram/Google/TikTok/X sessions from going stale — sites rotate
#   session cookies on every response, and discarding those rotations (the
#   old snapshot-only design) is what killed jars within days. Keep it on.
# COOKIE_STALE_WARNING_DAYS: warn (log + admin channel) when a jar hasn't had
#   a successful authenticated run in this many days.
# ---------------------------------------------------------------------------
COOKIE_WRITEBACK_ENABLED = os.getenv("COOKIE_WRITEBACK_ENABLED", "true").lower() in ("true", "1", "yes")
COOKIE_STALE_WARNING_DAYS = get_env_int("COOKIE_STALE_WARNING_DAYS", 21)

# =========================================================================
# Direct-forward: relay media you DM to the bot's own Instagram / X accounts
# =========================================================================
# The bot runs dedicated accounts on Instagram and/or X. From your personal
# account you DM a post / reel / story / photo / video (or just a link) to the
# bot's account; the bot polls its inbox and delivers the media to your
# Telegram chat. No third-party APIs: Instagram uses the local `instagrapi`
# library (bootstrapped from your igcookies.txt sessionid when possible),
# X/Twitter uses the local `twikit` library.
DIRECT_FORWARD_CHAT_ID = get_env_int("DIRECT_FORWARD_CHAT_ID", 0)
DIRECT_FORWARD_POLL_SECONDS = get_env_int("DIRECT_FORWARD_POLL_SECONDS", 120)

# Instagram direct-forward
IG_DIRECT_ENABLED = os.getenv("IG_DIRECT_ENABLED", "false").lower() in ("true", "1", "yes")
IG_DIRECT_USERNAME = os.getenv("IG_DIRECT_USERNAME", "")          # bot account login (fallback)
IG_DIRECT_PASSWORD = os.getenv("IG_DIRECT_PASSWORD", "")          # bot account password (fallback)
IG_DIRECT_TOTP_SEED = os.getenv("IG_DIRECT_TOTP_SEED", "")        # optional 2FA seed
IG_DIRECT_FROM_USERNAME = os.getenv("IG_DIRECT_FROM_USERNAME", "")  # YOUR IG handle (whose DMs to accept)

# X / Twitter direct-forward
X_DIRECT_ENABLED = os.getenv("X_DIRECT_ENABLED", "false").lower() in ("true", "1", "yes")
X_DIRECT_USERNAME = os.getenv("X_DIRECT_USERNAME", "")            # bot account login handle
X_DIRECT_PASSWORD = os.getenv("X_DIRECT_PASSWORD", "")
X_DIRECT_EMAIL = os.getenv("X_DIRECT_EMAIL", "")                  # sometimes required by X login
X_DIRECT_FROM_USER_ID = os.getenv("X_DIRECT_FROM_USER_ID", "")    # YOUR numeric X user id (whose DMs to accept)

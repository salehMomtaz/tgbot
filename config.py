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
DIRECT_FORWARD_POLL_SECONDS = get_env_int("DIRECT_FORWARD_POLL_SECONDS", 300)
# Humanization: Instagram flags machine-perfect fixed-cadence polling far more
# easily than jittered, several-minute intervals. The effective delay between
# inbox sweeps is POLL_SECONDS ± JITTER_PCT (0 disables jitter).
DIRECT_FORWARD_POLL_JITTER_PCT = get_env_int("DIRECT_FORWARD_POLL_JITTER_PCT", 40)
# Optional dedicated proxy for the DM sessions (http://user:pass@host:port or
# socks5h://...). One STABLE residential proxy per account is far safer than a
# datacenter IP; rotating proxies per request will get the account flagged.
# When unset, falls back to nothing (direct connection).
DIRECT_FORWARD_PROXY = os.getenv("DIRECT_FORWARD_PROXY", "").strip() or None

# Instagram direct-forward
IG_DIRECT_ENABLED = os.getenv("IG_DIRECT_ENABLED", "false").lower() in ("true", "1", "yes")
IG_DIRECT_USERNAME = os.getenv("IG_DIRECT_USERNAME", "")          # bot account login (fallback)
IG_DIRECT_PASSWORD = os.getenv("IG_DIRECT_PASSWORD", "")          # bot account password (fallback)
IG_DIRECT_TOTP_SEED = os.getenv("IG_DIRECT_TOTP_SEED", "")        # optional 2FA seed
IG_DIRECT_FROM_USERNAME = os.getenv("IG_DIRECT_FROM_USERNAME", "")  # YOUR IG handle (whose DMs to accept)
# Anti-detection (utils/ig_anti_detect.py). The private API previously rode a
# plain Python requests TLS fingerprint (a dead giveaway); these pin the
# session to a real Chrome fingerprint and the account's home region.
IG_DIRECT_TRANSPORT_IMPERSONATE = os.getenv("IG_DIRECT_TRANSPORT_IMPERSONATE", "chrome136") or "chrome136"
IG_DIRECT_COUNTRY = os.getenv("IG_DIRECT_COUNTRY", "US") or "US"
IG_DIRECT_COUNTRY_CODE = get_env_int("IG_DIRECT_COUNTRY_CODE", 1) or 1
IG_DIRECT_LOCALE = os.getenv("IG_DIRECT_LOCALE", "en_US") or "en_US"
IG_DIRECT_TZ_OFFSET = get_env_int("IG_DIRECT_TZ_OFFSET", -14400)
IG_DIRECT_TZ_NAME = os.getenv("IG_DIRECT_TZ_NAME", "GMT-04:00") or "GMT-04:00"

# X / Twitter direct-forward (self-DM method). The X worker boots from the
# shared xcookies jar (config.X_COOKIES) that yt-dlp keeps warm via write-back
# — no username/password, no separate bot account, no pairing.
X_DIRECT_ENABLED = os.getenv("X_DIRECT_ENABLED", "false").lower() in ("true", "1", "yes")
# XChat bridge (cache/xchat_inbox.jsonl) is consumed when present. The Deno
# sidecar reads the XChat-encrypted self-DM (twikit's legacy DM API cannot);
# the Python worker relays every canonical line past its own cursor. The PIN
# (the passcode set in the X app) is only meaningful to the Deno sidecar, but
# the admin console reads it here to report set/unset status and writes it via
# dotenv.set_key when the operator enters it in-chat.
XCHAT_PIN = os.getenv("XCHAT_PIN", "")
XCHAT_INBOX = os.getenv("XCHAT_INBOX", "cache/xchat_inbox.jsonl")

# TikTok direct-forward (web IM self-DM). The worker holds a persistent WS to
# the TikTok IM store (im-ws-sg.tiktok.com) and ingests cmd 500 NEW_MSG_NOTIFY
# pushes for the OWN self-DM conversation (0:1:{uid}:{uid}); every video share
# is resolved to @author/video/<itemId> via oEmbed and relayed through the
# normal yt-dlp pipeline with a fresh cookie snapshot. There is no pairing and
# no separate bot account — the session is the same web login as the ttcookies
# jar yt-dlp downloads with. Anti-detection is the same as X: a RANDOM poll
# window, never a fixed short cadence (that is what flags accounts).
TIKTOK_DIRECT_ENABLED = os.getenv("TIKTOK_DIRECT_ENABLED", "false").lower() in ("true", "1", "yes")
TIKTOK_DIRECT_POLL_SECONDS = get_env_int("TIKTOK_DIRECT_POLL_SECONDS", 300)
TIKTOK_DIRECT_POLL_JITTER_PCT = get_env_int("TIKTOK_DIRECT_POLL_JITTER_PCT", 40)

# =========================================================================
# System monitor (utils/system_monitor.py) — a lightweight, bot-independent
# watcher that reports CPU/RAM/swap/disk + top processes to the log channel
# and warns when usage exceeds a threshold.
# =========================================================================
# Sample cadence in seconds.
SYSMON_POLL_SECONDS = get_env_int("SYSMON_POLL_SECONDS", 15)
# Send a full "#system" report every N samples (15s * 60 = 15 min).
SYSMON_REPORT_INTERVAL = get_env_int("SYSMON_REPORT_INTERVAL", 60)
# Warning threshold (percent) for CPU / RAM / disk.
SYSMON_WARN_PCT = get_env_int("SYSMON_WARN_PCT", 80)
# While any metric is above the threshold, repeat the warning every N seconds.
SYSMON_WARN_SECONDS = get_env_int("SYSMON_WARN_SECONDS", 60)
# Top-N processes by CPU and by RAM in each report.
SYSMON_TOP_N = get_env_int("SYSMON_TOP_N", 20)
# Number of samples kept for time-frame averages (15s * 240 = 1 hour).
SYSMON_HISTORY_SAMPLES = get_env_int("SYSMON_HISTORY_SAMPLES", 240)
# Comma-separated list of paths whose filesystems are reported.
SYSMON_DISK_PATHS = os.getenv("SYSMON_DISK_PATHS", ".")

# =========================================================================
# Bale.ai frontend (optional, government-owned messenger — hardened)
# =========================================================================
# When BALE_TOKEN is set, a second aiogram bot (tapi.bale.ai) runs in the SAME
# process, sharing the download core, queue and PO provider. When empty, zero
# Bale code runs. See docs/memory/tgbot-balebot-integration.md for the full
# threat model: Bale traffic is treated as untrusted input; the Bale admin
# console is intentionally limited (no cookies / premium / POT / direct-forward);
# and there is NO Bale log channel — logs stay on Telegram + local file only.
BALE_TOKEN = os.getenv("BALE_TOKEN", "")
BALE_SYSTEM_CREATOR_ID = get_env_int("BALE_SYSTEM_CREATOR_ID", 0)
# Bale file ceiling is 20 MB per message (docs claims 50, real is 20). The uploader
# splits at 19 MB target / 20 MB hard, with Bale-safe filename + caption sanitizers.
BALE_HARD_LIMIT_MB = get_env_int("BALE_HARD_LIMIT_MB", 20)
BALE_SPLIT_TARGET_MB = get_env_int("BALE_SPLIT_TARGET_MB", 19)
# When true, Bale frontend accepts direct-file HTTP downloads (otherwise only yt-dlp sites)
BALE_DIRECT_DOWNLOAD = os.getenv("BALE_DIRECT_DOWNLOAD", "true").lower() in ("true", "1", "yes")

# =========================================================================
# GitHub explorer (ported from balebot — pure HTTP API, no Bale dependency)
# =========================================================================
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

# =========================================================================
# Subscription system (toggleable, 3 tiers + optional free with channel)
# =========================================================================
SUB_ENABLED = os.getenv("SUB_ENABLED", "false").lower() in ("true", "1", "yes")
SUB_FREE_ENABLED = os.getenv("SUB_FREE_ENABLED", "false").lower() in ("true", "1", "yes")
SUB_CHANNEL_ID = get_env_int("SUB_CHANNEL_ID", 0)
SUB_CHANNEL_USERNAME = os.getenv("SUB_CHANNEL_USERNAME", "")
# Stars prices are in tiers.py (XTR). TON address for Gram/TON payments.
SUB_TON_ADDRESS = os.getenv("SUB_TON_ADDRESS", "")
SUB_TON_API_KEY = os.getenv("SUB_TON_API_KEY", "")
# WebApp admin — secret for signing admin links (random if unset, but set it for persistence)
SUB_WEBAPP_SECRET = os.getenv("SUB_WEBAPP_SECRET", "")
# Rate-limit for subscription callbacks (per user, seconds)
SUB_RATE_LIMIT_SECONDS = get_env_int("SUB_RATE_LIMIT_SECONDS", 3)

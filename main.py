# main.py
import os
import time
import asyncio
import shutil
import uvicorn
from pyrogram import Client, filters, utils
import config
from utils.queue_manager import DownloadQueue

# =========================================================================
# Monkey-Patch: Resolves Pyrogram's internal 'Peer id invalid' Channel Bug
# =========================================================================
def get_peer_type_patched(peer_id: int) -> str:
    peer_id_str = str(peer_id)
    if not peer_id_str.startswith("-"):
        return "user"
    elif peer_id_str.startswith("-100"):
        return "channel"
    else:
        return "chat"

utils.get_peer_type = get_peer_type_patched

# =========================================================================
# Application Global Shared Instances
# =========================================================================

queue = DownloadQueue()
DOWNLOAD_CACHE = {}
LAST_UPDATE_TIME = {}

app = Client(
    "media_bot",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN
)

premium_app = None
if config.PREMIUM_STRING_SESSION:
    premium_app = Client(
        "premium_session",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        session_string=config.PREMIUM_STRING_SESSION
    )

# =========================================================================
# Global Shared Helpers
# =========================================================================

async def log_event(text: str):
    """Log an event locally and pipe to private Telegram channel if configured."""
    print(f"[LOG] {text}")
    if config.LOG_CHANNEL_ID != 0 and app.is_connected:
        try:
            await app.send_message(
                chat_id=config.LOG_CHANNEL_ID,
                text=f"📝 **System Log Event:**\n\n{text}"
            )
        except Exception as e:
            print(f"Failed to log event to channel: {e}")

async def progress_bar_handler(current, total, message, status_title: str):
    """Draws a visual progress bar and updates text every 5 seconds to avoid rate limiting."""
    now = time.time()
    msg_id = message.id
    if msg_id in LAST_UPDATE_TIME and now - LAST_UPDATE_TIME[msg_id] < 5:
        return
    LAST_UPDATE_TIME[msg_id] = now
    
    percentage = (current * 100 / total) if total > 0 else 0
    filled = int(percentage // 10)
    bar_str = "■" * filled + "□" * (10 - filled)
    
    current_mb = round(current / (1024 * 1024), 1)
    total_mb = round(total / (1024 * 1024), 1)
    
    text = (
        f"⏳ **{status_title}**\n"
        f"`[{bar_str}]` {percentage:.1f}%\n"
        f"📦 `{current_mb} MB / {total_mb} MB`"
    )
    try:
        await message.edit_text(text)
    except Exception:
        pass

def initialize_cookie_jars():
    """Initializes empty cookie files with the Netscape header to prevent yt-dlp warnings and enable auto-writing."""
    cookie_files = [config.YT_COOKIES, config.IG_COOKIES, config.TT_COOKIES, config.X_COOKIES, "cookies.txt"]
    for file_path in cookie_files:
        if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
            try:
                with open(file_path, "w") as f:
                    f.write("# Netscape HTTP Cookie File\n")
                print(f"[Cookies] Cookie jar initialized: {file_path}")
            except Exception as e:
                print(f"[Cookies] Warning: Could not initialize cookie jar {file_path}: {e}")

async def auto_clean_cache_directory():
    """Periodically sweeps the cache directory every hour to purge orphaned files older than 2 hours."""
    while True:
        print("[Cleaner] Running periodic cache sweep...")
        cache_dir = "cache"
        if os.path.exists(cache_dir):
            now = time.time()
            threshold = now - 7200  # 2 hours = 7200 seconds
            try:
                for entry in os.scandir(cache_dir):
                    mtime = entry.stat().st_mtime
                    if mtime < threshold:
                        if entry.is_dir():
                            shutil.rmtree(entry.path)
                            print(f"[Cleaner] Purged orphaned directory: {entry.path}")
                        else:
                            os.remove(entry.path)
                            print(f"[Cleaner] Purged orphaned file: {entry.path}")
            except Exception as e:
                print(f"[Cleaner] Exception occurred during cache sweep: {e}")
                
        await asyncio.sleep(3600)  # Wait 1 hour

# =========================================================================
# Event Loop Bootstrap & Startup Configuration
# =========================================================================

async def main_engine():
    print("Initializing services...")
    
    # 1. Initialize and format cookie files
    initialize_cookie_jars()
    
    # 2. Bind Pyrogram clients to stream handlers
    import modules.stream_handler
    modules.stream_handler.tg_client = app
    
    # 3. Import and register modular handler systems
    from modules.admin import register_admin_handlers
    from modules.downloader_handler import register_downloader_handlers
    from modules.stream_interceptor import register_stream_interceptor_handlers
    
    register_admin_handlers(app)
    register_downloader_handlers(app)
    register_stream_interceptor_handlers(app)
    
    # 4. Start Standard Bot Client
    await app.start()
    print("Telegram Bot Online.")
    
    # Resolve Log Channel Peer on startup to avoid 'Peer id invalid' exceptions
    if config.LOG_CHANNEL_ID != 0:
        try:
            await app.get_chat(config.LOG_CHANNEL_ID)
            print("Log Channel Peer resolved successfully.")
        except Exception as e:
            print(f"Warning: Could not resolve Log Channel ID: {e}")
    
    # 5. Start Premium Userbot Client if session is configured
    if premium_app:
        await premium_app.start()
        print("Premium Userbot Client connected.")
    
    # 6. Configure and launch Uvicorn (FastAPI Web Server) on port 8080
    from modules.stream_handler import fastapi_app
    
    uvicorn_args = {
        "app": fastapi_app,
        "host": "0.0.0.0",
        "port": 8080,
        "log_level": "info",
        "loop": "asyncio"
    }
    
    # Native SSL certificate configuration (Bypasses Nginx dependencies if provided)
    ssl_cert = getattr(config, "SSL_CERT_PATH", "")
    ssl_key = getattr(config, "SSL_KEY_PATH", "")
    if ssl_cert and ssl_key:
        if os.path.exists(ssl_cert) and os.path.exists(ssl_key):
            uvicorn_args["ssl_certfile"] = ssl_cert
            uvicorn_args["ssl_keyfile"] = ssl_key
            print("[Uvicorn] SSL parameters loaded. Web server will run natively on HTTPS.")
        else:
            print("[Uvicorn] Warning: SSL certificate or key file not found. Falling back to HTTP.")
            
    config_uvicorn = uvicorn.Config(**uvicorn_args)
    server = uvicorn.Server(config_uvicorn)
    
    from utils.updater import auto_update_ytdlp
    
    # Run FastAPI web server, the 6-hour updater, and the 1-hour cache cleaner concurrently
    await asyncio.gather(
        server.serve(),
        auto_update_ytdlp(),
        auto_clean_cache_directory()
    )

if __name__ == "__main__":
    import sys
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main_engine())
    except KeyboardInterrupt:
        print("Stopping bot gracefully...")
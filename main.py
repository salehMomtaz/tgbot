# main.py
import os
import sys
import time
import signal
import asyncio
import shutil
import logging
import uvicorn
from pyrogram import Client, filters, utils
from pyrogram.types import Message, CallbackQuery  # Fixed: Imported missing type annotations
import config
from utils.shared import queue, DOWNLOAD_CACHE, LAST_UPDATE_TIME

# Running as `python main.py` puts this module in sys.modules['__main__'] ONLY.
# Any later `import main` would RE-EXECUTE this file and hand back a second
# copy whose Client objects are never .start()ed (surfacing as "Client has not
# been started yet" in code that imports clients from main — e.g. the Friend
# Media Archiver's premium user session). Alias 'main' to '__main__' so every
# importer shares this one, live instance. Must run before any submodule import.
sys.modules.setdefault("main", sys.modules[__name__])

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
# Global Shared Helpers & Standalone Logging Registry
# =========================================================================

def setup_system_logger():
    """Binds channel handlers (Telegram + optional Bale) to Python's root logger at same INFO level."""
    # Local file is always added (mirrors everything)
    try:
        from utils.logger import ensure_local_log_handler
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        local_handler = ensure_local_log_handler()
        local_handler.setLevel(logging.INFO)
        # Avoid double-adding on re-entry
        if local_handler not in root_logger.handlers:
            root_logger.addHandler(local_handler)
        logging.info(f"[Logger] Local log mirror active at: {os.path.abspath('logs/bot.log')}")
    except Exception as e:
        print(f"Warning: Failed to init local log handler: {e}")
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)

    # Telegram log channel (required) — gets Telegram/pyrogram/direct_forward logs, NOT Bale
    if config.LOG_CHANNEL_ID != 0:
        try:
            from utils.logger import TelegramChannelHandler
            # Idempotence guard: a second handler instance (e.g. from a historic
            # double module execution) delivered EVERY record twice to the channel.
            already = any(isinstance(h, TelegramChannelHandler)
                          for h in logging.getLogger().handlers)
            if not already:
                channel_formatter = logging.Formatter('%(message)s')
                handler = TelegramChannelHandler(config.BOT_TOKEN, config.LOG_CHANNEL_ID)
                handler.setFormatter(channel_formatter)
                handler.setLevel(logging.INFO)
                def _tg_filter(record):
                    name = (getattr(record, "name", "") or "").lower()
                    try:
                        msg = record.getMessage()
                    except Exception:
                        msg = ""
                    msg_l = (msg.lower() if isinstance(msg, str) else "")
                    # Telegram channel must NOT get Bale logs (strict split per user request)
                    if "bale" in name:
                        return False
                    if name.startswith("aiogram"):
                        return False
                    if "[bale]" in msg_l or "bale_log" in msg_l or "bale logging linked" in msg_l:
                        return False
                    return True
                handler.addFilter(_tg_filter)
                logging.getLogger().addHandler(handler)
                logging.info("[Logger] Telegram logging linked to LOG_CHANNEL_ID")
        except Exception as e:
            print(f"Warning: Failed to initialize Telegram logger: {e}")

    # Bale log channel: user clarified `bale_log` IS A TELEGRAM CHANNEL (private, angelbalzac admin),
    # not a Bale channel. Reason: Bale is government-owned, so Bale-side logs containing
    # sensitive info must NOT go to tapi.bale.ai (security hole). They go to a *separate*
    # Telegram channel `bale_log` via api.telegram.org with BOT_TOKEN, at the same INFO level
    # as the main LOG_CHANNEL_ID. Both use Telegram API, just different channel IDs.
    # Direct forwards (Instagram/X/TikTok via pyrogram) are Telegram-only (balebot was aiogram,
    # tgbot is pyrogram) — they must NOT appear in bale_log.
    if getattr(config, "BALE_LOG_CHANNEL_ID", 0) != 0:
        try:
            from utils.logger import BaleChannelHandler
            # Same idempotence rule as the Telegram channel handler above.
            already_b = any(isinstance(h, BaleChannelHandler)
                            for h in logging.getLogger().handlers)
            if not already_b:
                bale_formatter = logging.Formatter('%(message)s')
                # Use BOT_TOKEN (Telegram) even though this is "Bale logs" — destination is Telegram channel bale_log
                b_handler = BaleChannelHandler(config.BOT_TOKEN, config.BALE_LOG_CHANNEL_ID)
                b_handler.setFormatter(bale_formatter)
                b_handler.setLevel(logging.INFO)
                # Standard architecture: split loggers — bale_log gets ONLY Bale-related records
                # at same INFO level, not the pyrogram/direct_forward spam that belongs to Telegram.
                def _bale_filter(record):
                    name = (getattr(record, "name", "") or "").lower()
                    try:
                        msg = record.getMessage()
                    except Exception:
                        msg = ""
                    msg_l = msg.lower() if isinstance(msg, str) else ""
                    # Allow: modules.bale.*, aiogram, and explicit [Bale] tagged logs
                    if "bale" in name:
                        return True
                    if name.startswith("aiogram"):
                        return True
                    if "[bale]" in msg_l or "bale_log" in msg_l or "bale logging linked" in msg_l:
                        return True
                    if "bale logging" in msg_l:
                        return True
                    # Shared logs (utils.*, pot_provider, downloader) are okay to be in both
                    # per user: "if something is shared then it is okay to be send in both channels"
                    if name.startswith("utils."):
                        return True
                    if "pot" in name or "cookie" in name:
                        return True
                    return False
                b_handler.addFilter(_bale_filter)
                logging.getLogger().addHandler(b_handler)
            logging.info(f"[Logger] Bale logging linked to BALE_LOG_CHANNEL_ID {config.BALE_LOG_CHANNEL_ID} (Telegram bale_log) — filtered to bale/aiogram only")
        except Exception as e:
            print(f"Warning: Failed to initialize Bale logger: {e}")
    elif getattr(config, "BALE_TOKEN", ""):
        logging.info("[Logger] Bale logging disabled (BALE_LOG_CHANNEL_ID=0) — Bale logs stay local only (bale_log is a Telegram channel, set BALE_LOG_CHANNEL_ID to enable)")

async def log_event(text: str):
    """Log an event locally. The standalone root logger handles automatic Telegram routing."""
    logging.info(text)

async def progress_bar_handler(current, total, message, status_title: str):
    """Draws a visual progress bar and updates text every 5 seconds to avoid rate limiting."""
    if message is None:
        return
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
    """
    Ensure cookie files exist with a Netscape header, then lock every primary
    jar read-only at rest (0o444).

    Cookie folder layout (see config.COOKIE_DIR):
        cookies/youtube/ytcookies.txt      # YT working jar + .backup
        cookies/instagram/igcookies.txt
        cookies/tiktok/ttcookies.txt
        cookies/twitter/xcookies.txt
        cookies/ytdlp/<sitename>.txt      # Per-site jars for all other yt-dlp sites
        cookies/ytdlp/cookies.txt         # Global fallback for any site without a jar

    "Read-only at rest" does NOT mean frozen: every yt-dlp run receives a
    per-run snapshot from utils.cookie_manager, and a successful run merges
    rotated session cookies BACK into the real jar with an atomic replace that
    re-applies the lock. That write-back is what keeps Instagram/Google/TikTok/X
    sessions from going stale (see utils/cookie_manager.py).
    """
    from utils import cookie_manager
    import config

    # One-time migration: historical installs kept jars at the project root
    # (ytcookies.txt, igcookies.txt, ...). Move them into the layout. `mv -n`
    # semantics: an existing layout jar always wins over a stale root file.
    legacy_map = [
        ("ytcookies.txt", config.YT_COOKIES),
        ("ytcookies.backup", config.YT_COOKIES_BACKUP),
        ("igcookies.txt", config.IG_COOKIES),
        ("ttcookies.txt", config.TT_COOKIES),
        ("xcookies.txt", config.X_COOKIES),
        ("cookies.txt", config.COOKIES_FILE),
    ]
    for legacy_name, target in legacy_map:
        if os.path.isfile(legacy_name) and not os.path.exists(target):
            try:
                os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
                shutil.move(legacy_name, target)
                print(f"[Cookies] Migrated legacy jar {legacy_name} -> {target}")
            except Exception as e:
                print(f"[Cookies] Warning: could not migrate {legacy_name}: {e}")

    # Make sure the folder layout exists so admin uploads and yt-dlp always
    # have somewhere to land.
    cookie_dirs = [
        os.path.dirname(config.YT_COOKIES),
        os.path.dirname(config.IG_COOKIES),
        os.path.dirname(config.TT_COOKIES),
        os.path.dirname(config.X_COOKIES),
        getattr(config, "YTDLP_COOKIES_DIR", "cookies/ytdlp"),
    ]
    for d in cookie_dirs:
        if d:
            os.makedirs(d, exist_ok=True)

    # The five always-present jars (four dedicated + one global fallback). Any
    # extra per-site jar inside cookies/ytdlp/ is admin-uploaded, so we leave
    # it alone — we only ensure the header is intact if the file already
    # contains something.
    cookie_files = [
        config.YT_COOKIES,
        config.IG_COOKIES,
        config.TT_COOKIES,
        config.X_COOKIES,
        config.COOKIES_FILE,
    ]

    # Also walk any pre-existing per-site jars inside cookies/ytdlp/ so an
    # admin who uploaded them before still gets a valid header prepended if
    # they accidentally pasted a header-less file.
    ytdlp_dir = getattr(config, "YTDLP_COOKIES_DIR", "cookies/ytdlp")
    per_site_jars = []
    if os.path.isdir(ytdlp_dir):
        for entry in os.scandir(ytdlp_dir):
            if entry.is_file() and entry.name.endswith(".txt"):
                per_site_jars.append(entry.path)
    cookie_files.extend(per_site_jars)

    for file_path in cookie_files:
        existed = os.path.exists(file_path) and os.path.getsize(file_path) > 0
        try:
            cookie_manager.ensure_netscape_header(file_path)
            if not existed:
                print(f"[Cookies] Initialized empty cookie jar: {file_path}")
        except Exception as e:
            print(f"[Cookies] Warning: Could not initialize cookie jar {file_path}: {e}")

    # If YouTube working jar is missing but a protected backup exists, restore it.
    backup_path = getattr(config, "YT_COOKIES_BACKUP", "ytcookies.backup")
    if (not os.path.exists(config.YT_COOKIES) or os.path.getsize(config.YT_COOKIES) == 0) \
            and os.path.exists(backup_path) and os.path.getsize(backup_path) > 0:
        try:
            # Make target writable in case a previous crash left it read-only.
            if os.path.exists(config.YT_COOKIES):
                os.chmod(config.YT_COOKIES, 0o644)
            shutil.copy(backup_path, config.YT_COOKIES)
            print(f"[Cookies] Restored {config.YT_COOKIES} from protected backup.")
        except Exception as e:
            print(f"[Cookies] Warning: Could not restore YouTube cookie backup: {e}")

    # Lock the four primary jars read-only at rest. yt-dlp never sees these
    # paths (it gets snapshots); the atomic merge in cookie_manager replaces
    # the inode and re-applies this lock after capturing session rotation.
    for file_path in (config.YT_COOKIES, config.IG_COOKIES, config.TT_COOKIES, config.X_COOKIES):
        cookie_manager.lock_jar(file_path)
    print("[Cookies] Locked primary jars read-only at rest (write-back merge keeps them fresh).")

async def auto_clean_cache_directory():
    """Periodically sweeps the cache directory every hour to purge orphaned files older than the configured age.

    Persistent state files the bot depends on are exempted from the sweep:
    ``cache/xchat_bridge_state.json`` is the Deno XChat bridge's cursor (deleting
    it makes the bridge re-prime ``last_seq`` to the newest message and silently
    SKIP everything older — a data-loss window), and ``cache/xchat_inbox.jsonl``
    is the bridge's canonical line inbox the X direct-forward worker consumes.
    The direct-forward platform state lives at the repo root
    (``direct_forward_state.json``), outside ``cache/``, so it is already safe.
    """
    from utils.shared import RUNTIME_SETTINGS
    protected_files = {"xchat_bridge_state.json", "xchat_inbox.jsonl", "friend_media_state.json"}
    while True:
        print("[Cleaner] Running periodic cache sweep...")
        cache_dir = "cache"
        if os.path.exists(cache_dir):
            now = time.time()
            max_age_hours = RUNTIME_SETTINGS.get("max_cache_age_hours", 2)
            threshold = now - (max_age_hours * 3600)
            try:
                for entry in os.scandir(cache_dir):
                    if entry.is_file() and entry.name in protected_files:
                        continue
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
# Monkey-Patch: Automatically Intercept and Log all outgoing API payloads
# =========================================================================
def patch_pyrogram_send_methods():
    """Overrides Pyrogram Client send methods to intercept and log raw JSON outputs of sent files/messages."""
    # Idempotence guard: this ran TWICE in one process once (pre-sys.modules-alias
    # double module execution) and every send was logged twice into the log
    # channel. If the wrap marker is present, the orig_* locals below would
    # capture already-wrapped methods and stack a second logging layer.
    if getattr(Client, "_tgbot_send_methods_patched", False):
        return
    Client._tgbot_send_methods_patched = True

    orig_send_message = Client.send_message
    orig_send_video = Client.send_video
    orig_send_document = Client.send_document
    orig_send_audio = Client.send_audio
    orig_edit_message_text = Client.edit_message_text

    def get_target_chat(args, kwargs) -> str:
        return str(kwargs.get("chat_id") or (args[0] if args else ""))

    async def wrapped_send_message(self, *args, **kwargs):
        sent_msg = await orig_send_message(self, *args, **kwargs)
        # Auto-expire inline keyboards so dead buttons don't pile up in history
        try:
            from utils import keyboard_expiry
            if sent_msg and sent_msg.reply_markup and getattr(sent_msg.chat, "id", None):
                keyboard_expiry.watch(sent_msg.chat.id, sent_msg.id)
        except Exception:
            pass
        target = get_target_chat(args, kwargs)
        # Prevent self-logging loop: do not log messages sent to the logging channel itself
        if target != str(config.LOG_CHANNEL_ID):
            logging.info(f"📤 **[SENT MESSAGE]**\n{str(sent_msg)}")
        return sent_msg

    async def wrapped_send_video(self, *args, **kwargs):
        sent_msg = await orig_send_video(self, *args, **kwargs)
        target = get_target_chat(args, kwargs)
        if target != str(config.LOG_CHANNEL_ID):
            logging.info(f"📤 **[SENT VIDEO]**\n{str(sent_msg)}")
        return sent_msg

    async def wrapped_send_document(self, *args, **kwargs):
        sent_msg = await orig_send_document(self, *args, **kwargs)
        target = get_target_chat(args, kwargs)
        if target != str(config.LOG_CHANNEL_ID):
            logging.info(f"📤 **[SENT DOCUMENT]**\n{str(sent_msg)}")
        return sent_msg

    async def wrapped_send_audio(self, *args, **kwargs):
        sent_msg = await orig_send_audio(self, *args, **kwargs)
        target = get_target_chat(args, kwargs)
        if target != str(config.LOG_CHANNEL_ID):
            logging.info(f"📤 **[SENT AUDIO]**\n{str(sent_msg)}")
        return sent_msg

    async def wrapped_edit_message_text(self, *args, **kwargs):
        edited = await orig_edit_message_text(self, *args, **kwargs)
        try:
            from utils import keyboard_expiry
            if edited and edited.reply_markup and getattr(edited.chat, "id", None):
                keyboard_expiry.watch(edited.chat.id, edited.id)
        except Exception:
            pass
        return edited

    # Bind wrapped methods
    Client.send_message = wrapped_send_message
    Client.send_video = wrapped_send_video
    Client.send_document = wrapped_send_document
    Client.send_audio = wrapped_send_audio
    Client.edit_message_text = wrapped_edit_message_text

# Execute monkey patch
patch_pyrogram_send_methods()

# =========================================================================
# Event Loop Bootstrap & Startup Configuration
# =========================================================================

async def main_engine():
    print("Initializing services...")

    # 0. The log channel is MANDATORY: the bot pipes all logs there and the
    # system monitor reports there too. Refuse to run without it rather than
    # silently degrading to a silent box.
    if config.LOG_CHANNEL_ID == 0:
        print("FATAL: LOG_CHANNEL_ID is required. Set it in .env (a private "
              "Telegram channel with the bot as admin) — the bot will not start "
              "without a log channel.")
        return

    # 1. Start the global system logger to pipe all container logs to your channel
    setup_system_logger()

    # 2. Initialize and format cookie files (locks the primary jars read-only)
    initialize_cookie_jars()

    # 2b. Cookie freshness watchdog: sessions die silently when a jar can't be
    # kept warm (no successful authenticated run in days). Make it a loud,
    # actionable log line before the first download fails on it.
    try:
        from utils import cookie_manager
        stale_days = getattr(config, "COOKIE_STALE_WARNING_DAYS", 21)
        for warning in cookie_manager.freshness_warnings(
            stale_days,
            [config.YT_COOKIES, config.IG_COOKIES, config.TT_COOKIES, config.X_COOKIES, config.COOKIES_FILE],
        ):
            logging.warning(f"[Cookies] ⚠️ {warning}")
    except Exception as e:
        print(f"[Cookies] Freshness check failed: {e}")

    # 3. Disk-space sanity check: refuse to run if the filesystem is critically full
    try:
        usage = shutil.disk_usage(os.getcwd())
        free_gb = usage.free / (1024 ** 3)
        used_pct = (usage.used / usage.total) * 100
        logging.info(f"[System] Disk usage: {used_pct:.1f}% used, {free_gb:.2f} GB free.")
        if used_pct > 95:
            logging.error("[System] Disk is critically full. Refusing to start to protect SSH/system access.")
            return
    except Exception as e:
        logging.warning(f"[System] Could not check disk usage: {e}")

    # 4. Bind Pyrogram clients to stream handlers
    import modules.stream_handler
    modules.stream_handler.tg_client = app

    # 5. Import and register modular handler systems
    from modules.admin import register_admin_handlers
    from modules.downloader_handler import register_downloader_handlers
    from modules.stream_interceptor import register_stream_interceptor_handlers

    register_admin_handlers(app)
    register_downloader_handlers(app, premium_app)
    register_stream_interceptor_handlers(app)
    # subscription: /subscription cmds + Stars/TON payments + channel-join verify
    try:
        from modules.subscription.handlers import register_subscription_handlers, register_subscription_payments
        from modules.subscription.join import register_join_handlers
        register_subscription_handlers(app)
        register_subscription_payments(app)
        register_join_handlers(app)
    except Exception as e:
        logging.warning(f"[Subscription] Could not register handlers: {e}")
    # balebot-originated extras now on Telegram too (pure HTTP, no Bale dep)
    try:
        from modules.github.handlers import register_github_handlers
        register_github_handlers(app, premium_app)
    except Exception as e:
        logging.warning(f"[GitHub] Could not register handlers: {e}")
    try:
        from modules.youtube.handlers import register_youtube_handlers
        register_youtube_handlers(app, premium_app)
    except Exception as e:
        logging.warning(f"[YouTube] Could not register handlers: {e}")
    try:
        from modules.translate.handlers import register_translate_handlers
        register_translate_handlers(app)
    except Exception as e:
        logging.warning(f"[Translate] Could not register handlers: {e}")
    try:
        from modules.web.handlers import register_web_handlers
        register_web_handlers(app, premium_app)
    except Exception as e:
        logging.warning(f"[Web] Could not register handlers: {e}")

    # Group -2 Incoming Update Log Interceptors
    @app.on_message(filters.private, group=-2)
    async def incoming_message_log_interceptor(client: Client, message: Message):
        """Intercepts and logs the raw JSON string of every incoming update."""
        logging.info(f"📥 **[RECEIVED UPDATE]**\n{str(message)}")
        message.continue_propagation()

    @app.on_callback_query(group=-2)
    async def incoming_callback_log_interceptor(client: Client, callback_query: CallbackQuery):
        """Intercepts and logs the raw JSON string of every inline glass button click."""
        logging.info(f"🖱 **[CALLBACK QUERY]**\n{str(callback_query)}")
        try:
            from utils import keyboard_expiry
            if callback_query.message and callback_query.message.id and getattr(callback_query.message.chat, "id", None):
                keyboard_expiry.touch(callback_query.message.chat.id, callback_query.message.id)
        except Exception:
            pass
        callback_query.continue_propagation()

    # 6. Start Standard Bot Client
    await app.start()
    print("Telegram Bot Online.")

    # Resolve Log Channel Peer on startup to avoid 'Peer id invalid' exceptions
    if config.LOG_CHANNEL_ID != 0:
        try:
            await app.get_chat(config.LOG_CHANNEL_ID)
            print("Log Channel Peer resolved successfully.")
        except Exception as e:
            print(f"Warning: Could not resolve Log Channel ID: {e}")

    # 7. Start Premium Userbot Client if session is configured
    if premium_app:
        await premium_app.start()
        print("Premium Userbot Client connected.")

    # 8. Start the PO-token provider. It is on by default and YouTube downloads
    #    require it (cookies + PO token, no fallback). A failure here is logged
    #    loudly but does NOT crash the bot — the rest of the bot keeps working,
    #    YouTube downloads will fail with an actionable message until the
    #    provider is fixed (Admin Console -> PO Token).
    pot_manager = None
    if config.YTDLP_POT_ENABLED:
        from utils.pot_provider import PotProviderManager
        import utils.shared as shared
        try:
            pot_manager = PotProviderManager()
            await pot_manager.start()
            shared.pot_manager_instance = pot_manager
            shared.POT_AVAILABLE = True
            logging.info(f"[POT] Provider started on 127.0.0.1:{config.YTDLP_POT_PORT}")
        except Exception as e:
            logging.error(
                f"[POT] Failed to start provider: {e}. "
                "YouTube downloads will be UNAVAILABLE until this is fixed "
                "(Admin Console -> PO Token, or re-run ./install.sh)."
            )
            shared.pot_manager_instance = None
            shared.POT_AVAILABLE = False
            pot_manager = None

    # 9. Configure and launch Uvicorn (FastAPI Web Server) on port 8080
    from modules.stream_handler import fastapi_app

    uvicorn_args = {
        "app": fastapi_app,
        "host": "0.0.0.0",
        "port": 8080,
        "log_level": "info",
        "loop": "asyncio"
    }

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

    # Run the FastAPI web server, the 6-hour updater, and the 1-hour cache cleaner
    # concurrently (plus the PO-token health supervisor when enabled).
    tasks = [
        server.serve(),
        auto_update_ytdlp(),
        auto_clean_cache_directory(),
    ]
    if pot_manager:
        tasks.append(pot_manager.health_check_loop())

    # Inline-keyboard auto-expiry: strips dead buttons after an idle TTL so
    # chat history doesn't accumulate leftover keyboards.
    try:
        from utils.keyboard_expiry import expiry_loop
        tasks.append(expiry_loop(app))
    except Exception as e:
        logging.warning(f"[KeyboardExpiry] Could not start: {e}")

    # Direct-forward background task: relays media you DM to the bot's own
    # Instagram / X accounts into Telegram. No-op when DIRECT_FORWARD_CHAT_ID
    # is 0 or no platform is enabled.
    try:
        from modules.direct_forward import start_direct_forward_task
        df_task = start_direct_forward_task(app, premium_app)
        if df_task:
            tasks.append(df_task)
    except Exception as e:
        logging.warning(f"[DirectForward] Could not start: {e}")

    # Friend Media Archiver background watcher. ALWAYS started — it self-gates
    # on the live FRIEND_MEDIA_ENABLED / FRIEND_MEDIA_SCHEDULE_MINUTES config
    # every cycle, so in-chat toggles apply with no restart. Requires a
    # connected user account (premium_app) for actual reads; never messages the
    # friends.
    try:
        from modules.friend_media.admin import start_friend_media_task
        fm_task = start_friend_media_task(app, premium_app)
        if fm_task:
            tasks.append(fm_task)
    except Exception as e:
        logging.warning(f"[FriendMedia] Could not start: {e}")

    # 9b. Optional Bale.ai frontend (tapi.bale.ai) — same process, shared queue / PO
    # provider, but LIMITED admin (no cookies/premium/POT/direct) and NO Bale log
    # channel (government-owned messenger, logs stay on Telegram only). Isolated
    # task: Bale crash never kills Telegram poller.
    if getattr(config, "BALE_TOKEN", ""):
        try:
            from modules.bale.runner import start_bale_bot
            # Isolated wrapper so Bale exceptions don't propagate to asyncio.gather
            async def _bale_wrapper():
                try:
                    await start_bale_bot()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logging.exception("[Bale] Poller died unexpectedly — will not auto-restart until next bot restart. Check BALE_TOKEN / network.")
            tasks.append(_bale_wrapper())
            logging.info("[Bale] Frontend enabled (bale admin limited, no log channel)")
        except Exception as e:
            logging.warning(f"[Bale] Could not start Bale frontend: {e}")
    else:
        logging.info("[Bale] BALE_TOKEN empty — Bale frontend disabled")

    # 9c. Headless cookie refresher (Playwright, sequential, 1 tab at a time).
    # DM-only jars (IG/X/TT/YT) never get yt-dlp write-back, so they go stale
    # after ~7 days and hit update_risky_contactpoint. This visits each site
    # sequentially (not 4 tabs at once) every 24h, ~300 MB peak, safe on 4GB+8swap.
    if getattr(config, "COOKIE_REFRESH_ENABLED", True):
        try:
            from utils.cookie_refresher import auto_refresh_cookies_loop
            tasks.append(auto_refresh_cookies_loop())
            logging.info("[CookieRefresh] scheduled (24h, sequential, 1 Chromium at a time)")
        except Exception as e:
            logging.warning(f"[CookieRefresh] Could not schedule: {e}")
    else:
        logging.info("[CookieRefresh] disabled via COOKIE_REFRESH_ENABLED=false")

    # 9d. Launch the standalone system monitor as a DETACHED subprocess so it
    # survives this bot's own crash/restart (it talks to the log channel via the
    # raw Bot API, exactly like the logger does). If it is already running (e.g.
    # started by tgbot-monitor.service), this is a harmless no-op.
    try:
        from utils.system_monitor import spawn_detached_monitor
        if spawn_detached_monitor():
            logging.info("[SysMon] System monitor launched as a detached process.")
    except Exception as e:
        logging.warning(f"[SysMon] Could not launch system monitor: {e}")

    try:
        await asyncio.gather(*tasks)
    finally:
        if pot_manager:
            await pot_manager.stop()

def schedule_self_restart(delay: float = 3.0) -> None:
    """Restart this process on its own after `delay` seconds.

    The bot runs under systemd with `Restart=always`. Sending SIGTERM to our own
    PID trips the KeyboardInterrupt path in the __main__ block at the very
    bottom: pyrogram drains, the PO-token provider is stopped, cookie locks are
    released, the process exits, and systemd relaunches `run.sh` — which
    re-reads `.env`, so a freshly saved PREMIUM_STRING_SESSION takes effect.
    This is exactly what `sudo systemctl restart tgbot` does, without needing
    shell access.

    When INVOCATION_ID is absent (not running under systemd, e.g. a tmux/foreground
    dev run), fall back to re-execing the process image in place.
    """
    def _restart_now():
        if os.environ.get("INVOCATION_ID"):
            logging.info("[Restart] systemd detected, sending SIGTERM to self.")
            os.kill(os.getpid(), signal.SIGTERM)
        else:
            logging.info("[Restart] No systemd; re-execing in place.")
            os.execv(sys.executable, [sys.executable, os.path.abspath("main.py")])

    async def _delayed_restart():
        await asyncio.sleep(delay)
        _restart_now()

    asyncio.get_running_loop().create_task(_delayed_restart())


if __name__ == "__main__":
    # systemd sends SIGTERM on stop/restart. Translate it into the graceful
    # KeyboardInterrupt path so pyrogram drains, the PO-token provider is torn
    # down (PotProviderManager.stop), and cookie locks are released — instead
    # of dying hard mid-request. Harmless under tmux (no SIGTERM there).
    # NOTE: this block MUST stay at the very bottom (below every def) so that
    # main_engine()'s submodule imports see a fully-populated '__main__' — the
    # sys.modules alias at the top makes `import main` resolve HERE.
    def _on_sigterm(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main_engine())
    except KeyboardInterrupt:
        print("Stopping bot gracefully...")

# modules/admin.py
import os
import shutil
import asyncio
import logging
import yt_dlp
from pyrogram import Client, filters, ContinuePropagation
from pyrogram.types import (
    CallbackQuery,
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
import config
from utils.gate import (
    load_database,
    add_user,
    remove_user,
    unblacklist_user,
    is_document_mode,
    toggle_document_mode,
    is_blacklisted,
    blacklist_user,
    is_authorized
)
from utils.id_validator import is_valid_telegram_id

logger = logging.getLogger(__name__)

# Map callback string shortcuts to physical filenames
COOKIE_MAP = {
    "ytcookies": config.YT_COOKIES,
    "igcookies": config.IG_COOKIES,
    "ttcookies": config.TT_COOKIES,
    "xcookies": config.X_COOKIES,
    "cookies": config.COOKIES_FILE
}

# In-memory dictionary to track administrative states per user
USER_STATES = {}

# In-memory registry to track and delete active ForceReply prompts on cancel or success
ACTIVE_PROMPTS = {}


def _pot_running() -> bool:
    """True if the PO-token provider manager exists and reports healthy."""
    try:
        import utils.shared as shared
        manager = getattr(shared, "pot_manager_instance", None)
        return bool(manager and manager.is_running())
    except Exception:
        return False


def build_console_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Main admin console keyboard. PO Token badge reflects live provider health."""
    doc_status = "✅" if is_document_mode(user_id) else "❌"
    pot_status = "🟢" if _pot_running() else "🔴"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 List Users", callback_data="admin_list"),
         InlineKeyboardButton("➕ Add User", callback_data="admin_add")],
        [InlineKeyboardButton("➖ Remove User", callback_data="admin_remove"),
         InlineKeyboardButton("🚫 Blacklist Logs", callback_data="admin_blacklist")],
        [InlineKeyboardButton(f"📄 Doc Mode: {doc_status}", callback_data="admin_toggle_doc"),
         InlineKeyboardButton("🍪 Cookie Jars", callback_data="admin_cookies_menu")],
        [InlineKeyboardButton(f"🔐 PO Token: {pot_status}", callback_data="admin_pot_menu"),
         InlineKeyboardButton("💥 Abort Transfer", callback_data="admin_abort_queue")],
        [InlineKeyboardButton("❌ Close Console", callback_data="admin_close")]
    ])


def get_cookie_action_keyboard(cookie_key: str) -> InlineKeyboardMarkup:
    """Per-jar action keyboard. Test/Backup/Restore are YouTube-specific."""
    rows = [[
        InlineKeyboardButton("📤 Download", callback_data=f"admin_cookie_action:{cookie_key}:download"),
        InlineKeyboardButton("✏️ Replace", callback_data=f"admin_cookie_action:{cookie_key}:replace"),
    ]]
    if cookie_key == "ytcookies":
        rows.append([
            InlineKeyboardButton("🧪 Test", callback_data=f"admin_cookie_action:{cookie_key}:test"),
            InlineKeyboardButton("💾 Save Backup", callback_data=f"admin_cookie_action:{cookie_key}:savebackup"),
        ])
        rows.append([
            InlineKeyboardButton("♻️ Restore Backup", callback_data=f"admin_cookie_action:{cookie_key}:restore")
        ])
    rows.append([InlineKeyboardButton("◀️ Back", callback_data="admin_cookies_menu")])
    return InlineKeyboardMarkup(rows)


def get_pot_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧪 Test Stack", callback_data="admin_pot_action:test"),
         InlineKeyboardButton("🔍 Run Diagnosis", callback_data="admin_pot_action:diagnose")],
        [InlineKeyboardButton("🚀 Start Provider", callback_data="admin_pot_action:start"),
         InlineKeyboardButton("🛑 Stop Provider", callback_data="admin_pot_action:stop")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="admin_pot_menu")],
        [InlineKeyboardButton("◀️ Back to Console", callback_data="admin_main")]
    ])


def register_admin_handlers(app: Client):

    from main import log_event
    from utils.shared import queue  # Fixed: Import from clean shared registry

    # Reusable "Back to Console" inline button
    back_markup = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back to Console", callback_data="admin_main")]])

    async def purge_active_prompt(user_id: int, client: Client):
        """Helper to safely delete any active ForceReply prompt bubble from the chat stream."""
        prompt_id = ACTIVE_PROMPTS.pop(user_id, None)
        if prompt_id:
            try:
                await client.delete_messages(chat_id=user_id, message_ids=prompt_id)
            except Exception:
                pass

    def _write_cookie_jar(cookie_key: str, file_path: str, content: str) -> None:
        """Write cookie content, enforcing the read-only lock on the YouTube jar.

        yt-dlp rewrites cookie jars on exit; the live ytcookies.txt is kept
        read-only so it cannot be corrupted. We unlock briefly, write, re-lock,
        and purge stale snapshots so the next download uses the fresh jar.
        """
        from utils.downloader import _purge_cookie_snapshots

        final_content = content
        if not content.strip().startswith("# Netscape"):
            final_content = f"# Netscape HTTP Cookie File\n{content}"

        if cookie_key == "ytcookies" and os.path.exists(file_path):
            os.chmod(file_path, 0o644)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(final_content)
        if cookie_key == "ytcookies":
            os.chmod(file_path, 0o444)
        _purge_cookie_snapshots(file_path)

    # =========================================================================
    # Group -1: Global Interceptor Gate (Strict Security Shield)
    # =========================================================================
    @app.on_message(filters.private, group=-1)
    async def security_gate(client: Client, message: Message):
        if not message.from_user:
            message.stop_propagation()

        user_id = message.from_user.id
        if is_blacklisted(user_id):
            message.stop_propagation()

        if not is_authorized(user_id):
            blacklist_user(user_id)
            await log_event(f"⚠️ **Intruder Blocked:** User `{user_id}` has been banned and blacklisted.")
            message.stop_propagation()

    # =========================================================================
    # Group 0: State Machine Handler (Processes text inputs ONLY if user is in an active state)
    # =========================================================================
    @app.on_message(
        filters.text &
        filters.private &
        filters.create(lambda _, __, m: m.from_user.id in USER_STATES),
        group=0
    )
    async def admin_state_message_handler(client: Client, message: Message):
        user_id = message.from_user.id
        state = USER_STATES.get(user_id)
        input_text = message.text.strip()

        # Failsafe escape: If you type /start or console triggers, clear active state and let Group 1 handle it
        if input_text.lower() in ["/start", "🛠 console", "hey", "console", "hi!"]:
            USER_STATES.pop(user_id, None)
            await purge_active_prompt(user_id, client)
            return  # Propagates downstream to Group 1

        # We do NOT delete your typed message here anymore (it remains in your history)
        prompt_id = ACTIVE_PROMPTS.pop(user_id, None)

        # 1. Handle Cookie Replacement State
        if state.startswith("waiting_for_replace_"):
            USER_STATES.pop(user_id, None)

            # Delete the bot's old prompt message
            if prompt_id:
                try:
                    await client.delete_messages(chat_id=user_id, message_ids=prompt_id)
                except Exception:
                    pass

            cookie_key = state.split("waiting_for_replace_")[1]
            file_path = COOKIE_MAP.get(cookie_key)

            if not file_path:
                await message.reply_text("❌ Error: Invalid cookie profile selected.", reply_markup=back_markup)
                message.stop_propagation()
                return

            try:
                _write_cookie_jar(cookie_key, file_path, input_text)
                await message.reply_text(f"✅ `{cookie_key}.txt` successfully replaced!", reply_markup=back_markup)
                await log_event(f"🍪 **Admin Action:** Cookie profile `{cookie_key}.txt` was replaced via chat interface.")
            except Exception as e:
                await message.reply_text(f"❌ Failed to write cookie file: {e}", reply_markup=back_markup)

            message.stop_propagation()
            return

        # 2. Handle User ID Input States (Add, Remove, Unban)
        if not is_valid_telegram_id(input_text):
            USER_STATES.pop(user_id, None)
            if prompt_id:
                try:
                    await client.delete_messages(chat_id=user_id, message_ids=prompt_id)
                except Exception:
                    pass
            await message.reply_text(
                "❌ Error: Invalid Telegram ID. Please input digits only (between 5 and 11 numbers).",
                reply_markup=back_markup
            )
            message.stop_propagation()
            return

        target_id = int(input_text)
        USER_STATES.pop(user_id, None)  # Reset state

        # Delete the bot's old prompt message
        if prompt_id:
            try:
                await client.delete_messages(chat_id=user_id, message_ids=prompt_id)
            except Exception:
                pass

        if state == "waiting_for_add_user":
            if add_user(target_id):
                await message.reply_text(
                    f"✅ User `{target_id}` has been authorized successfully.",
                    reply_markup=back_markup
                )
                await log_event(f"👥 **User Whitelisted:** Creator whitelisted User ID `{target_id}`.")
            else:
                await message.reply_text(
                    f"ℹ️ User `{target_id}` was already authorized.",
                    reply_markup=back_markup
                )

        elif state == "waiting_for_remove_user":
            db = load_database()
            if target_id not in db["authorized"]:
                await message.reply_text(
                    f"❌ Error: User ID `{target_id}` is not currently authorized.",
                    reply_markup=back_markup
                )
                message.stop_propagation()
                return

            if remove_user(target_id):
                await message.reply_text(
                    f"✅ User `{target_id}` has been removed.",
                    reply_markup=back_markup
                )
                await log_event(f"👥 **User Revoked:** Creator removed User ID `{target_id}`.")

        elif state == "waiting_for_unban":
            db = load_database()
            if target_id not in db["blacklisted"]:
                await message.reply_text(
                    f"❌ Error: User ID `{target_id}` is not found in the blacklist.",
                    reply_markup=back_markup
                )
                message.stop_propagation()
                return

            if unblacklist_user(target_id):
                await message.reply_text(
                    f"✅ User `{target_id}` has been unbanned.",
                    reply_markup=back_markup
                )
                await log_event(f"🔓 **User Unbanned:** Creator unbanned and unblacklisted User ID `{target_id}`.")

        message.stop_propagation()

    # =========================================================================
    # Group 0b: Cookie Jar Replacement via Document File (optional convenience)
    # =========================================================================
    @app.on_message(
        filters.document &
        filters.private &
        filters.create(lambda _, __, m: m.from_user.id == config.SYSTEM_CREATOR_ID) &
        filters.create(lambda _, __, m: USER_STATES.get(m.from_user.id, "").startswith("waiting_for_replace_")),
        group=0
    )
    async def admin_cookie_replace_document_handler(client: Client, message: Message):
        user_id = message.from_user.id
        state = USER_STATES.pop(user_id, None)
        cookie_key = state.split("waiting_for_replace_")[1]
        file_path = COOKIE_MAP.get(cookie_key)

        prompt_id = ACTIVE_PROMPTS.pop(user_id, None)
        if prompt_id:
            try:
                await client.delete_messages(chat_id=user_id, message_ids=prompt_id)
            except Exception:
                pass

        if not file_path:
            await message.reply_text("❌ Error: Invalid cookie profile selected.", reply_markup=back_markup)
            return

        doc = message.document
        file_name = (doc.file_name or "").lower()
        mime = (doc.mime_type or "").lower()
        if not (file_name.endswith(".txt") or mime.startswith("text/")):
            await message.reply_text(
                "❌ Invalid file type. Please send a `.txt` cookie jar file (or paste the text).",
                reply_markup=back_markup
            )
            return

        try:
            buffer = await client.download_media(message=message, in_memory=True)
            content = buffer.read().decode("utf-8", errors="replace")
        except Exception as e:
            await message.reply_text(f"❌ Failed to download file: {e}", reply_markup=back_markup)
            return

        try:
            _write_cookie_jar(cookie_key, file_path, content)
            await message.reply_text(f"✅ `{cookie_key}.txt` successfully replaced from document!", reply_markup=back_markup)
            await log_event(f"🍪 **Admin Action:** Cookie profile `{cookie_key}.txt` was replaced via document.")
        except Exception as e:
            await message.reply_text(f"❌ Failed to write cookie file: {e}", reply_markup=back_markup)

    # =========================================================================
    # Group 1: Main Text Router (Handles plain text commands and greetings)
    # =========================================================================
    @app.on_message(filters.text & filters.private, group=1)
    async def admin_start_text_handler(client: Client, message: Message):
        text = message.text.strip()
        user_id = message.from_user.id

        from modules.downloader_handler import is_link
        if is_link(text):
            # Pass link down to downloader_handler
            raise ContinuePropagation()

        if user_id == config.SYSTEM_CREATOR_ID:
            keyboard = build_console_keyboard(user_id)
            await message.reply_text(
                "🛠 **Admin System Console**\nChoose an administrative action below:",
                reply_markup=keyboard
            )
        else:
            await message.reply_text(
                "👋 **Hello! Welcome to your Private Downloader Bot.**\n\n"
                "To get started:\n"
                "• Send me any YouTube, Instagram, TikTok, or X/Twitter link to download it.\n"
                "• Send me any direct file URL to upload it directly to Telegram.\n"
                "• Forward me a Telegram file (video, document, music) to generate an instant direct stream link."
            )
        message.stop_propagation()

    # =========================================================================
    # Group 2: Callback Query Dispatcher (Console clicks)
    # =========================================================================
    @app.on_callback_query(filters.regex(r"^admin_"))
    async def admin_callback_handler(client: Client, callback_query: CallbackQuery):
        data = callback_query.data
        user_id = callback_query.from_user.id

        if user_id != config.SYSTEM_CREATOR_ID:
            await callback_query.answer("Access Denied.", show_alert=True)
            return

        if data == "admin_close":
            USER_STATES.pop(user_id, None)
            await purge_active_prompt(user_id, client)
            await callback_query.message.delete()
            await callback_query.answer("Console closed.")

        elif data == "admin_abort_queue":
            queue_len = len(queue._pending)
            queue._pending.clear()
            queue._active = False

            if os.path.exists("cache"):
                try:
                    shutil.rmtree("cache")
                    os.makedirs("cache", exist_ok=True)
                except Exception:
                    pass

            await callback_query.answer("💥 System Reset: All queue jobs aborted and cache purged!", show_alert=True)
            await log_event(f"💥 **Admin Action:** Queue reset executed. {queue_len} pending jobs aborted.")

        elif data == "admin_toggle_doc":
            state = toggle_document_mode(user_id)
            status_str = "✅" if state else "❌"
            await callback_query.answer(f"📄 Document Mode toggled to {status_str}.", show_alert=True)
            await log_event(f"⚙️ **Admin Action:** Document Mode toggled to {status_str}.")

            try:
                await callback_query.message.edit_text(
                    "🛠 **Admin System Console**\nChoose an administrative action below:",
                    reply_markup=build_console_keyboard(user_id)
                )
            except Exception:
                pass

        elif data == "admin_list":
            db = load_database()
            users = db["authorized"]
            text = "📋 **Authorized Users List:**\n" + "\n".join([f"• `{uid}`" for uid in users]) if users else "No additional users authorized."
            await callback_query.message.edit_text(text, reply_markup=back_markup)
            await callback_query.answer()

        elif data == "admin_blacklist":
            db = load_database()
            blacklisted = db["blacklisted"]
            text = "🚫 **Banned Intruders List:**\n" + "\n".join([f"• `{uid}`" for uid in blacklisted]) if blacklisted else "Blacklist registry is empty."

            keyboard_rows = []
            if blacklisted:
                keyboard_rows.append([InlineKeyboardButton("🔓 Unban User", callback_data="admin_unban")])
            keyboard_rows.append([InlineKeyboardButton("◀️ Back", callback_data="admin_main")])

            await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard_rows))
            await callback_query.answer()

        elif data == "admin_unban":
            USER_STATES[user_id] = "waiting_for_unban"
            ACTIVE_PROMPTS[user_id] = callback_query.message.id
            await callback_query.message.edit_text(
                "🔓 **Unban User**\nPlease type the numerical ID of the blocked user you want to unban directly in your text box and press send:",
                reply_markup=back_markup
            )
            await callback_query.answer()

        elif data == "admin_main":
            USER_STATES.pop(user_id, None)  # Reset state on return
            ACTIVE_PROMPTS.pop(user_id, None)

            await callback_query.message.edit_text(
                "🛠 **Admin System Console**\nChoose an administrative action below:",
                reply_markup=build_console_keyboard(user_id)
            )
            await callback_query.answer()

        elif data == "admin_add":
            USER_STATES[user_id] = "waiting_for_add_user"
            ACTIVE_PROMPTS[user_id] = callback_query.message.id
            await callback_query.message.edit_text(
                "➕ **Add Authorized User**\nPlease type the numerical ID of the user you want to authorize directly in your text box and press send:",
                reply_markup=back_markup
            )
            await callback_query.answer()

        elif data == "admin_remove":
            USER_STATES[user_id] = "waiting_for_remove_user"
            ACTIVE_PROMPTS[user_id] = callback_query.message.id
            await callback_query.message.edit_text(
                "➖ **Remove Authorized User**\nPlease type the numerical ID of the user you want to remove directly in your text box and press send:",
                reply_markup=back_markup
            )
            await callback_query.answer()

        # =========================================================================
        # Cookies Sub-Menus Configuration
        # =========================================================================
        elif data == "admin_cookies_menu":
            USER_STATES.pop(user_id, None)
            ACTIVE_PROMPTS.pop(user_id, None)
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("YouTube", callback_data="admin_cookie_select:ytcookies"), InlineKeyboardButton("Instagram", callback_data="admin_cookie_select:igcookies")],
                [InlineKeyboardButton("TikTok", callback_data="admin_cookie_select:ttcookies"), InlineKeyboardButton("X/Twitter", callback_data="admin_cookie_select:xcookies")],
                [InlineKeyboardButton("Global (cookies.txt)", callback_data="admin_cookie_select:cookies")],
                [InlineKeyboardButton("◀️ Return to Console", callback_data="admin_main")]
            ])
            await callback_query.message.edit_text(
                "🍪 **Cookie Jars Manager**\nSelect a cookie profile to view or edit:",
                reply_markup=keyboard
            )
            await callback_query.answer()

        elif data.startswith("admin_cookie_select:"):
            cookie_key = data.split(":")[1]
            await callback_query.message.edit_text(
                f"🍪 **Cookie Profile: `{cookie_key}.txt`**\nSelect an administration action:",
                reply_markup=get_cookie_action_keyboard(cookie_key)
            )
            await callback_query.answer()

        elif data.startswith("admin_cookie_action:"):
            _, cookie_key, action = data.split(":")
            file_path = COOKIE_MAP.get(cookie_key)

            if not file_path:
                await callback_query.answer("❌ Invalid cookie profile.", show_alert=True)
                return

            if action == "download":
                if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                    await callback_query.answer("Delivering file...")
                    await client.send_document(
                        chat_id=user_id,
                        document=file_path,
                        caption=f"🍪 Here is your active `{cookie_key}.txt` file."
                    )
                else:
                    await callback_query.answer("⚠️ File is empty or does not exist on VPS yet.", show_alert=True)

            elif action == "test":
                await callback_query.answer("Testing jar...")
                await _test_cookie_jar(client, user_id, cookie_key, file_path)

            elif action == "replace":
                USER_STATES[user_id] = f"waiting_for_replace_{cookie_key}"
                ACTIVE_PROMPTS[user_id] = callback_query.message.id
                await callback_query.message.edit_text(
                    f"✏️ **Replace {cookie_key}.txt**\n"
                    "Paste your fresh Netscape formatted cookies into your text box and press send, "
                    "or send them as a `.txt` document file:",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Cancel & Return", callback_data=f"admin_cookie_select:{cookie_key}")]])
                )
                await callback_query.answer()

            elif action == "restore":
                await callback_query.answer("Restoring from backup...")
                backup_path = getattr(config, "YT_COOKIES_BACKUP", "ytcookies.backup")
                if not os.path.exists(backup_path) or os.path.getsize(backup_path) == 0:
                    await callback_query.message.edit_text(
                        f"⚠️ No backup found for `{cookie_key}.txt`.",
                        reply_markup=get_cookie_action_keyboard(cookie_key)
                    )
                    return
                try:
                    from utils.downloader import _purge_cookie_snapshots
                    if os.path.exists(file_path):
                        os.chmod(file_path, 0o644)
                    shutil.copy(backup_path, file_path)
                    os.chmod(file_path, 0o444)
                    _purge_cookie_snapshots(file_path)
                    await callback_query.message.edit_text(
                        f"✅ Restored `{cookie_key}.txt` from `{backup_path}`.",
                        reply_markup=get_cookie_action_keyboard(cookie_key)
                    )
                    await log_event(f"🍪 **Admin Action:** `{cookie_key}.txt` restored from backup.")
                except Exception as e:
                    await callback_query.message.edit_text(
                        f"❌ Failed to restore backup: {e}",
                        reply_markup=get_cookie_action_keyboard(cookie_key)
                    )

            elif action == "savebackup":
                await callback_query.answer("Saving backup...")
                backup_path = getattr(config, "YT_COOKIES_BACKUP", "ytcookies.backup")
                if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
                    await callback_query.message.edit_text(
                        f"⚠️ `{cookie_key}.txt` is empty. Nothing to back up.",
                        reply_markup=get_cookie_action_keyboard(cookie_key)
                    )
                    return
                try:
                    from utils.downloader import _purge_cookie_snapshots
                    if os.path.exists(backup_path):
                        os.chmod(backup_path, 0o644)
                    # Source jar is read-only; shutil.copy only reads it, which is fine.
                    shutil.copy(file_path, backup_path)
                    os.chmod(backup_path, 0o444)
                    _purge_cookie_snapshots(file_path)
                    await callback_query.message.edit_text(
                        f"✅ Saved `{cookie_key}.txt` as protected backup `{backup_path}` (read-only).",
                        reply_markup=get_cookie_action_keyboard(cookie_key)
                    )
                    await log_event(f"🍪 **Admin Action:** `{cookie_key}.txt` saved as protected backup.")
                except Exception as e:
                    await callback_query.message.edit_text(
                        f"❌ Failed to save backup: {e}",
                        reply_markup=get_cookie_action_keyboard(cookie_key)
                    )

        # =========================================================================
        # PO Token Provider Sub-Menus
        # =========================================================================
        elif data == "admin_pot_menu":
            USER_STATES.pop(user_id, None)
            await _render_pot_menu(callback_query)

        elif data.startswith("admin_pot_action:"):
            await _handle_pot_action(client, callback_query, data.split(":")[1])

    # ------------------------------------------------------------------
    # PO Token helpers (closures over app/log_event)
    # ------------------------------------------------------------------
    async def _render_pot_menu(callback_query: CallbackQuery):
        import utils.shared as shared
        manager = getattr(shared, "pot_manager_instance", None)
        running = manager.is_running() if manager else False
        available = getattr(shared, "POT_AVAILABLE", False)
        enabled = shared.is_pot_enabled()
        try:
            await callback_query.message.edit_text(
                "🔐 **PO Token Provider**\n\n"
                "YouTube downloads require this provider (cookies + PO token, no fallback). "
                "It runs on the Deno runtime and starts automatically with the bot.\n\n"
                f"• Provider running: **{'YES ✅' if running else 'NO ❌'}**\n"
                f"• Provider available: **{'YES ✅' if available else 'NO ❌'}**\n"
                f"• PO token enabled: **{'YES ✅' if enabled else 'NO ❌'}**\n"
                f"• Endpoint: `127.0.0.1:{config.YTDLP_POT_PORT}`\n\n"
                "• **Test Stack** — live extraction with the full stack.\n"
                "• **Run Diagnosis** — compare no-auth / cookies-only / full-stack.\n"
                "• **Start/Stop Provider** — launch or shut down the Deno server.",
                reply_markup=get_pot_menu_keyboard()
            )
        except Exception:
            pass
        await callback_query.answer()

    async def _handle_pot_action(client: Client, callback_query: CallbackQuery, action: str):
        import utils.shared as shared

        if action == "start":
            await callback_query.answer("Starting provider...")
            manager = getattr(shared, "pot_manager_instance", None)
            if manager and manager.is_running():
                await callback_query.message.edit_text(
                    "🚀 Provider is already running.",
                    reply_markup=get_pot_menu_keyboard()
                )
                return
            try:
                from utils.pot_provider import PotProviderManager
                manager = manager or PotProviderManager()
                await manager.start()
                shared.pot_manager_instance = manager
                shared.POT_AVAILABLE = True
                text = (
                    "🚀 **PO Token Provider Started**\n\n"
                    f"Listening on `127.0.0.1:{config.YTDLP_POT_PORT}`.\n"
                    "Downloads will now use cookies + PO token."
                )
                await log_event("🔐 **Admin Action:** PO Token provider started from admin console.")
            except Exception as e:
                shared.POT_AVAILABLE = False
                text = (
                    f"❌ **Failed to start provider:**\n`{e}`\n\n"
                    "Make sure Deno ≥ 2.0 is installed and the provider is set up:\n"
                    "`./install.sh`\n"
                    "(installs Deno, clones bgutil-provider, builds the native canvas FFI)"
                )
            try:
                await callback_query.message.edit_text(text=text, reply_markup=get_pot_menu_keyboard())
            except Exception:
                pass
            return

        if action == "stop":
            await callback_query.answer("Stopping provider...")
            manager = getattr(shared, "pot_manager_instance", None)
            if manager:
                await manager.stop()
            shared.POT_AVAILABLE = False
            try:
                await callback_query.message.edit_text(
                    "🛑 **PO Token Provider Stopped.**\n"
                    "YouTube downloads will FAIL while it is stopped (no fallback). "
                    "Tap **Start Provider** to resume.",
                    reply_markup=get_pot_menu_keyboard()
                )
            except Exception:
                pass
            await log_event("🔐 **Admin Action:** PO Token provider stopped from admin console.")
            return

        if action == "diagnose":
            await callback_query.answer("Running diagnosis...")
            try:
                await callback_query.message.edit_text(
                    "🔍 **Running YouTube access diagnosis...**\nThis may take up to 30 seconds.",
                    reply_markup=get_pot_menu_keyboard()
                )
            except Exception:
                pass
            from utils.downloader import diagnose_youtube_access
            loop = asyncio.get_event_loop()
            try:
                report = await loop.run_in_executor(None, diagnose_youtube_access)
                text = (
                    "🔍 **YouTube Access Diagnosis**\n\n"
                    f"• No auth: `{report['no_auth_count']}` real formats\n"
                    f"• Cookies only: `{report['cookies_count']}` real formats\n"
                    f"• Cookies + PO token + mweb: `{report['full_count']}` real formats\n\n"
                    f"**Recommendation:** {report['recommendation']}"
                )
            except Exception as e:
                text = f"❌ **Diagnosis failed:**\n`{e}`"
            try:
                await callback_query.message.edit_text(text=text, reply_markup=get_pot_menu_keyboard())
            except Exception:
                pass
            return

        if action == "test":
            await callback_query.answer("Testing full stack...")
            try:
                await callback_query.message.edit_text(
                    "🧪 **Testing cookies + PO-token stack...**",
                    reply_markup=get_pot_menu_keyboard()
                )
            except Exception:
                pass
            await _test_cookie_jar(client, callback_query.from_user.id, "ytcookies", COOKIE_MAP["ytcookies"], force_pot=True)
            return

    # ------------------------------------------------------------------
    # Live cookie-jar test (yt-dlp extraction against a public video)
    # ------------------------------------------------------------------
    def _run_cookie_test_sync(cookie_key: str, file_path: str, force_pot: bool) -> dict:
        """Synchronous yt-dlp probe. Run inside an executor so it never blocks the loop."""
        from utils.downloader import get_cookies_for_url, _apply_pot_options
        import utils.shared as shared

        test_url = "https://www.youtube.com/watch?v=jSi2LDkyKmI"
        cookie_snapshot = get_cookies_for_url(test_url)
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "format": "all",
            "cookiefile": cookie_snapshot,
            "proxy": getattr(config, "YTDLP_PROXY", None),
        }
        user_agent = getattr(config, "YTDLP_USER_AGENT", "")
        if user_agent:
            ydl_opts["user_agent"] = user_agent

        original_pot = shared.is_pot_enabled()
        if force_pot:
            shared.set_pot_enabled(True)
        try:
            ydl_opts = _apply_pot_options(ydl_opts, test_url)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(test_url, download=False)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            shared.set_pot_enabled(original_pot)

        formats = info.get("formats", []) if info else []
        real_formats = [
            f for f in formats
            if f.get("format_note") != "storyboard" and f.get("ext") != "mhtml"
        ]
        samples = []
        seen = set()
        for f in real_formats:
            note = f.get("format_note") or "?"
            ext = f.get("ext") or "?"
            key = (note, ext)
            if key not in seen:
                seen.add(key)
                samples.append(f"• `{note}` ({ext})")
            if len(samples) >= 6:
                break
        return {
            "ok": True,
            "real_count": len(real_formats),
            "samples": samples,
            "storyboard_only": len(real_formats) == 0,
        }

    async def _test_cookie_jar(client: Client, user_id: int, cookie_key: str, file_path: str, force_pot: bool = False):
        """Run a lightweight yt-dlp extraction on a known public video and report format availability."""
        if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
            await client.send_message(
                chat_id=user_id,
                text=f"⚠️ `{cookie_key}.txt` is empty or missing. Nothing to test.",
                reply_markup=back_markup
            )
            return

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _run_cookie_test_sync, cookie_key, file_path, force_pot)

        if not result.get("ok"):
            await client.send_message(
                chat_id=user_id,
                text=(
                    f"❌ **Cookie Test Failed for `{cookie_key}.txt`**\n\n"
                    "yt-dlp could not extract anything using this jar.\n"
                    f"Error: `{result.get('error')}`\n\n"
                    "Please upload a fresh cookie jar from a browser where YouTube plays normally."
                ),
                reply_markup=back_markup
            )
            return

        if result.get("real_count", 0) > 0:
            summary = "\n".join(result["samples"])
            pot_label = " (with PO token)" if force_pot else ""
            await client.send_message(
                chat_id=user_id,
                text=(
                    f"✅ **Cookie Test Passed{pot_label} for `{cookie_key}.txt`**\n\n"
                    f"YouTube returned {result['real_count']} downloadable formats.\n"
                    f"Sample formats:\n{summary}\n\n"
                    "The jar is working — try your link again."
                ),
                reply_markup=back_markup
            )
            await log_event(f"🧪 **Admin Action:** Cookie jar `{cookie_key}.txt` passed live test ({result['real_count']} formats).")
        else:
            await client.send_message(
                chat_id=user_id,
                text=(
                    f"⚠️ **Cookie Test Warning for `{cookie_key}.txt`**\n\n"
                    "YouTube accepted the cookies, but only returned storyboard/preview formats.\n\n"
                    "This means the jar is **bot-flagged, expired, or from an account that cannot watch videos**.\n"
                    "Please upload a fresh `ytcookies.txt` from a browser where you can actually play YouTube videos."
                ),
                reply_markup=back_markup
            )
            await log_event(f"⚠️ **Admin Action:** Cookie jar `{cookie_key}.txt` failed live test (storyboard-only).")

# modules/admin.py
import os
import shutil
from pyrogram import Client, filters, ContinuePropagation
from pyrogram.types import (
    CallbackQuery, 
    Message, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton
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

# Map callback string shortcuts to physical filenames
COOKIE_MAP = {
    "ytcookies": config.YT_COOKIES,
    "igcookies": config.IG_COOKIES,
    "ttcookies": config.TT_COOKIES,
    "xcookies": config.X_COOKIES,
    "cookies": "cookies.txt"
}

# In-memory dictionary to track administrative states per user
USER_STATES = {}

# In-memory registry to track and delete active ForceReply prompts on cancel or success
ACTIVE_PROMPTS = {}

def register_admin_handlers(app: Client):
    
    from main import log_event, queue

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
                
            # Prepend Netscape headers automatically if missing
            final_content = input_text
            if not input_text.startswith("# Netscape"):
                final_content = f"# Netscape HTTP Cookie File\n{input_text}"
                
            try:
                with open(file_path, "w") as f:
                    f.write(final_content)
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
            doc_status = "✅" if is_document_mode(user_id) else "❌"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("👥 List Users", callback_data="admin_list"), InlineKeyboardButton("➕ Add User", callback_data="admin_add")],
                [InlineKeyboardButton("➖ Remove User", callback_data="admin_remove"), InlineKeyboardButton("🚫 Blacklist Logs", callback_data="admin_blacklist")],
                [InlineKeyboardButton(f"📄 Doc Mode: {doc_status}", callback_data="admin_toggle_doc"), InlineKeyboardButton("🍪 Cookie Jars", callback_data="admin_cookies_menu")],
                [InlineKeyboardButton("💥 Abort Transfer", callback_data="admin_abort_queue"), InlineKeyboardButton("❌ Close Console", callback_data="admin_close")]
            ])
            await message.reply_text(
                f"🛠 **Admin System Console**\nChoose an administrative action below:",
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
            
        elif data == "admin_clear_streams":
            from modules.stream_handler import STREAM_CACHE
            cleared_count = len(STREAM_CACHE)
            STREAM_CACHE.clear()
            await callback_query.answer(f"🧹 Cleared all {cleared_count} active stream states.", show_alert=True)
            await log_event("🧹 **Admin Action:** All active stream links cleared from cache.")
            
        elif data == "admin_toggle_doc":
            state = toggle_document_mode(user_id)
            status_str = "✅" if state else "❌"
            await callback_query.answer(f"📄 Document Mode toggled to {status_str}.", show_alert=True)
            await log_event(f"⚙️ **Admin Action:** Document Mode toggled to {status_str}.")
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("👥 List Users", callback_data="admin_list"), InlineKeyboardButton("➕ Add User", callback_data="admin_add")],
                [InlineKeyboardButton("➖ Remove User", callback_data="admin_remove"), InlineKeyboardButton("🚫 Blacklist Logs", callback_data="admin_blacklist")],
                [InlineKeyboardButton(f"📄 Doc Mode: {status_str}", callback_data="admin_toggle_doc"), InlineKeyboardButton("🍪 Cookie Jars", callback_data="admin_cookies_menu")],
                [InlineKeyboardButton("💥 Abort Transfer", callback_data="admin_abort_queue"), InlineKeyboardButton("❌ Close Console", callback_data="admin_close")]
            ])
            try:
                await callback_query.message.edit_text(
                    f"🛠 **Admin System Console**\nChoose an administrative action below:",
                    reply_markup=keyboard
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
            
            doc_status = "✅" if is_document_mode(user_id) else "❌"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("👥 List Users", callback_data="admin_list"), InlineKeyboardButton("➕ Add User", callback_data="admin_add")],
                [InlineKeyboardButton("➖ Remove User", callback_data="admin_remove"), InlineKeyboardButton("🚫 Blacklist Logs", callback_data="admin_blacklist")],
                [InlineKeyboardButton(f"📄 Doc Mode: {doc_status}", callback_data="admin_toggle_doc"), InlineKeyboardButton("🍪 Cookie Jars", callback_data="admin_cookies_menu")],
                [InlineKeyboardButton("💥 Abort Transfer", callback_data="admin_abort_queue"), InlineKeyboardButton("❌ Close Console", callback_data="admin_close")]
            ])
            await callback_query.message.edit_text(
                f"🛠 **Admin System Console**\nChoose an administrative action below:",
                reply_markup=keyboard
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
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📤 Download", callback_data=f"admin_cookie_action:{cookie_key}:download")],
                [InlineKeyboardButton("✏️ Replace", callback_data=f"admin_cookie_action:{cookie_key}:replace")],
                [InlineKeyboardButton("◀️ Back", callback_data="admin_cookies_menu")]
            ])
            await callback_query.message.edit_text(
                f"🍪 **Cookie Profile: `{cookie_key}.txt`**\nSelect an administration action:",
                reply_markup=keyboard
            )
            await callback_query.answer()
            
        elif data.startswith("admin_cookie_action:"):
            _, cookie_key, action = data.split(":")
            file_path = COOKIE_MAP.get(cookie_key)
            
            if action == "download":
                if os.path.exists(file_path):
                    await client.send_document(
                        chat_id=user_id,
                        document=file_path,
                        caption=f"🍪 Here is your active `{cookie_key}.txt` file."
                    )
                else:
                    await callback_query.answer("⚠️ File is empty or does not exist on VPS yet.", show_alert=True)
                await callback_query.answer()
                
            elif action == "replace":
                USER_STATES[user_id] = f"waiting_for_replace_{cookie_key}"
                ACTIVE_PROMPTS[user_id] = callback_query.message.id
                await callback_query.message.edit_text(
                    f"✏️ **Replace {cookie_key}.txt**\nPlease paste your fresh Netscape formatted cookies into your standard text box and press send:",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Cancel & Return", callback_data=f"admin_cookie_select:{cookie_key}")]])
                )
                await callback_query.answer()
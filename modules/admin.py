# modules/admin.py
import os
import shutil
from pyrogram import Client, filters, ContinuePropagation
from pyrogram.types import (
    CallbackQuery, 
    Message, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    ForceReply
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
    blacklist_user
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

def register_admin_handlers(app: Client):
    
    from main import log_event, queue

    back_markup = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back to Console", callback_data="admin_main")]])

    # =========================================================================
    # 0. Global Interceptor Gate (Completely modularized out of main.py)
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
    # 1. Standard Private Text Router (Handles /start and console text triggers)
    # =========================================================================
    @app.on_message(filters.text & filters.private)
    async def admin_start_text_handler(client: Client, message: Message):
        text = message.text.strip()
        user_id = message.from_user.id
        
        from modules.downloader_handler import is_link
        if is_link(text):
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

    # =========================================================================
    # 2. Callback Query Handler (Handles inline buttons)
    # =========================================================================
    @app.on_callback_query(filters.regex(r"^admin_"))
    async def admin_callback_handler(client: Client, callback_query: CallbackQuery):
        data = callback_query.data
        user_id = callback_query.from_user.id
        
        if user_id != config.SYSTEM_CREATOR_ID:
            await callback_query.answer("Access Denied.", show_alert=True)
            return
            
        if data == "admin_close":
            await callback_query.message.delete()
            await callback_query.answer("Console closed.")
            
        elif data == "admin_abort_queue":
            # Force reset the active download queue
            queue_len = len(queue._pending)
            queue._pending.clear()
            queue._active = False
            
            # Wipe local VPS cache folder
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
            await callback_query.message.delete()
            prompt = await client.send_message(
                chat_id=user_id,
                text="Please reply to this message with the numerical ID of the blocked user you want to unban.",
                reply_markup=ForceReply(placeholder="e.g. 123456789")
            )
            # Send an optional Cancel button directly underneath the reply prompt
            await client.send_message(
                chat_id=user_id,
                text="Or cancel the operation using the button below:",
                reply_markup=back_markup
            )
            await callback_query.answer()
            
        elif data == "admin_main":
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
            
        elif data in ["admin_add", "admin_remove"]:
            action_text = "authorize" if data == "admin_add" else "remove"
            await callback_query.message.delete()
            await client.send_message(
                chat_id=user_id,
                text=f"Please reply to this message with the numerical ID of the user you want to {action_text}.",
                reply_markup=ForceReply(placeholder="e.g. 123456789")
            )
            await client.send_message(
                chat_id=user_id,
                text="Or cancel the operation using the button below:",
                reply_markup=back_markup
            )
            await callback_query.answer()

        # =========================================================================
        # Cookies Sub-Menus Configuration
        # =========================================================================
        elif data == "admin_cookies_menu":
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
                await callback_query.message.delete()
                await client.send_message(
                    chat_id=user_id,
                    text=f"Please reply to this message with your new Netscape formatted cookies for {cookie_key}.",
                    reply_markup=ForceReply(placeholder="# Netscape HTTP Cookie File...")
                )
                await client.send_message(
                    chat_id=user_id,
                    text="Or cancel the operation using the button below:",
                    reply_markup=back_markup
                )
                await callback_query.answer()

    # =========================================================================
    # 3. ForceReply Message Handler (Handles whitelist, bans, and cookie writes)
    # =========================================================================
    @app.on_message(filters.reply & filters.private)
    async def admin_input_handler(client: Client, message: Message):
        if message.from_user.id != config.SYSTEM_CREATOR_ID:
            return
        
        reply_text = message.reply_to_message.text
        input_text = message.text.strip()

        # Handle Cookie Replacements (String inputs)
        if "new Netscape formatted cookies for" in reply_text:
            # Extract key name
            cookie_key = reply_text.split("cookies for ")[1].strip().replace(".", "")
            file_path = COOKIE_MAP.get(cookie_key)
            if not file_path:
                await message.reply_text("❌ Error: Invalid cookie profile selected.", reply_markup=back_markup)
                return
                
            # Guarantee Netscape header is present
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
            return

        # Handle User ID validations (Numerical inputs)
        if not is_valid_telegram_id(input_text):
            await message.reply_text(
                "❌ Error: Invalid Telegram ID. Please input digits only (between 5 and 11 numbers).",
                reply_markup=back_markup
            )
            return
            
        target_id = int(input_text)
        
        if "user you want to authorize" in reply_text:
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
                
        elif "user you want to remove" in reply_text:
            db = load_database()
            if target_id not in db["authorized"]:
                await message.reply_text(
                    f"❌ Error: User ID `{target_id}` is not currently authorized.",
                    reply_markup=back_markup
                )
                return
                
            if remove_user(target_id):
                await message.reply_text(
                    f"✅ User `{target_id}` has been removed.",
                    reply_markup=back_markup
                )
                await log_event(f"👥 **User Revoked:** Creator removed User ID `{target_id}`.")
                
        elif "user you want to unban" in reply_text:
            db = load_database()
            if target_id not in db["blacklisted"]:
                await message.reply_text(
                    f"❌ Error: User ID `{target_id}` is not found in the blacklist.",
                    reply_markup=back_markup
                )
                return
                
            if unblacklist_user(target_id):
                await message.reply_text(
                    f"✅ User `{target_id}` has been unbanned.",
                    reply_markup=back_markup
                )
                await log_event(f"🔓 **User Unbanned:** Creator unbanned and unblacklisted User ID `{target_id}`.")
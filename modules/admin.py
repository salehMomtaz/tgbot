# modules/admin.py
from pyrogram import Client, filters
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
    toggle_document_mode
)
from utils.id_validator import is_valid_telegram_id

def register_admin_handlers(app: Client):
    
    # Import log_event dynamically inside registration scope to avoid circular imports
    from main import log_event

    # Global reusable "Back to Console" markup
    back_markup = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back to Console", callback_data="admin_main")]])

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
            
        elif data == "admin_clear_streams":
            from modules.stream_handler import STREAM_CACHE
            cleared_count = len(STREAM_CACHE)
            STREAM_CACHE.clear()
            await callback_query.answer(f"🧹 Cleared all {cleared_count} active stream states.", show_alert=True)
            await log_event("🧹 **Admin Action:** All active stream links cleared from cache.")
            
        elif data == "admin_toggle_doc":
            state = toggle_document_mode(user_id)
            status_str = "🟢" if state else "🔴"
            await callback_query.answer(f"📄 Document Mode toggled to {status_str}.", show_alert=True)
            await log_event(f"⚙️ **Admin Action:** Document Mode toggled to {status_str}.")
            # Edit console message to reflect state change
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("👥 List Users", callback_data="admin_list"), InlineKeyboardButton("➕ Add User", callback_data="admin_add")],
                [InlineKeyboardButton("➖ Remove User", callback_data="admin_remove"), InlineKeyboardButton("🚫 Blacklist Logs", callback_data="admin_blacklist")],
                [InlineKeyboardButton(f"📄 Doc Mode: {status_str}", callback_data="admin_toggle_doc"), InlineKeyboardButton("🧹 Clear Streams", callback_data="admin_clear_streams")],
                [InlineKeyboardButton("❌ Close Console", callback_data="admin_close")]
            ])
            try:
                await callback_query.message.edit_text(
                    f"🛠 **Admin System Console**\nChoose an administrative action below:",
                    reply_markup=keyboard
                )
            except Exception:
                pass # Avoid crashes if state editing is identical
            
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
            
            # Conditionally render the "Unban User" button only if there are actually banned users!
            keyboard_rows = []
            if blacklisted:
                keyboard_rows.append([InlineKeyboardButton("🔓 Unban User", callback_data="admin_unban")])
            keyboard_rows.append([InlineKeyboardButton("◀️ Back", callback_data="admin_main")])
            
            await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard_rows))
            await callback_query.answer()
            
        elif data == "admin_unban":
            await callback_query.message.delete()
            await client.send_message(
                chat_id=user_id,
                text="Please reply to this message with the numerical ID of the blocked user you want to unban.",
                reply_markup=ForceReply(placeholder="e.g. 123456789")
            )
            await callback_query.answer()
            
        elif data == "admin_main":
            doc_status = "🟢" if is_document_mode(user_id) else "🔴"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("👥 List Users", callback_data="admin_list"), InlineKeyboardButton("➕ Add User", callback_data="admin_add")],
                [InlineKeyboardButton("➖ Remove User", callback_data="admin_remove"), InlineKeyboardButton("🚫 Blacklist Logs", callback_data="admin_blacklist")],
                [InlineKeyboardButton(f"📄 Doc Mode: {doc_status}", callback_data="admin_toggle_doc"), InlineKeyboardButton("🧹 Clear Streams", callback_data="admin_clear_streams")],
                [InlineKeyboardButton("❌ Close Console", callback_data="admin_close")]
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
            await callback_query.answer()

    # Reply Message Handler: Handles ForceReply text inputs from Admin
    @app.on_message(filters.reply & filters.private)
    async def admin_input_handler(client: Client, message: Message):
        if message.from_user.id != config.SYSTEM_CREATOR_ID:
            return
        
        reply_text = message.reply_to_message.text
        input_text = message.text.strip()
        
        # Guard clause: Validate format using id_validator helper
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
            # Validate if user exists before attempting removal
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
            # Validate if user is blacklisted before attempting unban
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
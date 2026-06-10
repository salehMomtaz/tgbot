import os
import uuid
import time
import asyncio
import urllib.parse
import aiohttp
import uvicorn
from pyrogram import Client, filters
from pyrogram.types import (
    Message, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    CallbackQuery
)
import config
from utils.gate import (
    is_authorized, 
    load_database, 
    add_user, 
    remove_user, 
    is_blacklisted, 
    blacklist_user, 
    unblacklist_user
)

# In-memory caches for format selections and link mappings
DOWNLOAD_CACHE = {}
LAST_UPDATE_TIME = {}

app = Client(
    "media_bot",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN
)

async def log_event(text: str):
    """Log an event locally and pipe to private Telegram channel if configured."""
    print(f"[LOG] {text}")
    if config.LOG_CHANNEL_ID != 0:
        try:
            await app.send_message(
                chat_id=config.LOG_CHANNEL_ID,
                text=f"📝 **System Log Event:**\n\n{text}"
            )
        except Exception as e:
            print(f"Failed to log event to channel: {e}")

def is_link(text: str) -> bool:
    return text.startswith("http://") or text.startswith("https://")

def is_social_media_link(url: str) -> bool:
    url_lower = url.lower()
    social_domains = ["youtube.com", "youtu.be", "instagram.com", "tiktok.com"]
    return any(domain in url_lower for domain in social_domains)

async def download_direct_file(url: str, cache_id: str, progress_fn) -> str:
    os.makedirs("cache", exist_ok=True)
    parsed_url = urllib.parse.urlparse(url)
    file_name = os.path.basename(parsed_url.path) or f"download_{cache_id}"
    file_name = urllib.parse.unquote(file_name)
    out_path = f"cache/{cache_id}_{file_name}"
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=1800) as response:
            if response.status != 200:
                raise RuntimeError(f"Server returned error code {response.status}")
            
            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0
            
            with open(out_path, "wb") as f:
                async for chunk in response.content.iter_chunked(512 * 1024):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_fn:
                        await progress_fn(downloaded, total_size)
                        
    return out_path

async def progress_bar_handler(current, total, message: Message, status_title: str):
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

# 1. Gate: intercepts all messages, processes blacklist dynamic bans
@app.on_message(group=-1)
async def security_gate(client: Client, message: Message):
    if not message.from_user:
        message.stop_propagation()
        
    user_id = message.from_user.id
    
    # Immediately block previously banned users
    if is_blacklisted(user_id):
        message.stop_propagation()
        
    if not is_authorized(user_id):
        # Auto-blacklist strangers attempting to flood/access the bot
        blacklist_user(user_id)
        await log_event(f"⚠️ **Intruder Blocked:** User `{user_id}` has been added to the blacklist.")
        message.stop_propagation()

# 2. Main Text Handler
@app.on_message(filters.text & filters.private)
async def text_handler(client: Client, message: Message):
    text = message.text.strip()
    user_id = message.from_user.id
    
    # Admin Interface trigger (if creator types /start or any text that isn't a link/file)
    if user_id == config.SYSTEM_CREATOR_ID and not is_link(text):
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 List Users", callback_data="admin_list"), InlineKeyboardButton("➕ Add User", callback_data="admin_add")],
            [InlineKeyboardButton("➖ Remove User", callback_data="admin_remove"), InlineKeyboardButton("🚫 Blacklist Logs", callback_data="admin_blacklist")],
            [InlineKeyboardButton("🧹 Clear Streams", callback_data="admin_clear_streams"), InlineKeyboardButton("❌ Close Console", callback_data="admin_close")]
        ])
        await message.reply_text("🛠 **Admin System Console**\nChoose an administrative action below:", reply_markup=keyboard)
        return

    # Authorized standard user welcome response
    if user_id != config.SYSTEM_CREATOR_ID and not is_link(text):
        await message.reply_text(
            "👋 **Hello! Welcome to your Private Downloader Bot.**\n\n"
            "To get started:\n"
            "• Send me any YouTube, Instagram, or TikTok link to download it.\n"
            "• Send me any direct file URL to upload it directly to Telegram.\n"
            "• Forward me a Telegram file (video, document, music) to generate an instant direct stream link."
        )
        return

    # Process link extraction
    if is_link(text):
        if is_social_media_link(text):
            loading_msg = await message.reply_text("🔍 Analyzing link metadata, please wait...")
            try:
                from utils.downloader import extract_formats
                data = extract_formats(text)
                
                cache_id = str(uuid.uuid4())[:8]
                DOWNLOAD_CACHE[cache_id] = {
                    "url": text,
                    "title": data["title"],
                    "videos": data["videos"],
                    "audios": data["audios"],
                    "thumbnail_url": data["thumbnail"]
                }
                
                videos = data["videos"]
                audios = data["audios"]
                max_rows = max(len(videos), len(audios))
                
                keyboard_rows = []
                for i in range(max_rows):
                    row = []
                    if i < len(videos):
                        v = videos[i]
                        row.append(InlineKeyboardButton(
                            text=f"🎥 {v['quality']} ({v['size_str']})",
                            callback_data=f"dl:{cache_id}:v:{v['format_id']}"
                        ))
                    else:
                        row.append(InlineKeyboardButton(text="—", callback_data="none"))
                    
                    if i < len(audios):
                        a = audios[i]
                        row.append(InlineKeyboardButton(
                            text=f"🎵 {a['quality']} ({a['size_str']})",
                            callback_data=f"dl:{cache_id}:a:{a['format_id']}"
                        ))
                    else:
                        row.append(InlineKeyboardButton(text="—", callback_data="none"))
                    keyboard_rows.append(row)
                    
                keyboard_rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"dl:{cache_id}:cancel")])
                
                await log_event(f"ℹ️ **Link analyzed:** `{data['title']}` extracted for User `{user_id}`.")
                
                await message.reply_text(
                    f"📥 **Format Selection**\n\n📝 **Title:** {data['title']}\n"
                    f"⏱ **Duration:** {int(data['duration'] // 60)}m {int(data['duration'] % 60)}s\n\n"
                    f"Select an option to download and upload directly to Telegram:",
                    reply_markup=InlineKeyboardMarkup(keyboard_rows)
                )
            except Exception as e:
                await message.reply_text(f"❌ Extraction failed.\nError: `{str(e)}`")
                await log_event(f"❌ **Extraction Error:** Failed to parse link `{text}`. Details: `{str(e)}`")
            finally:
                await loading_msg.delete()
        else:
            # Generic direct URL downloader
            loading_msg = await message.reply_text("📥 Direct File URL detected. Starting download...")
            cache_id = str(uuid.uuid4())[:8]
            
            async def upload_progress(cur, tot):
                await progress_bar_handler(cur, tot, loading_msg, "Downloading direct file to server...")
                
            try:
                file_path = await download_direct_file(text, cache_id, upload_progress)
                await loading_msg.edit_text("📤 Uploading direct file to Telegram...")
                
                # Check 2GB threshold before attempt
                f_size = os.path.getsize(file_path)
                if f_size > (2000 * 1024 * 1024):
                    await loading_msg.edit_text("❌ Upload aborted: File exceeds Telegram's 2GB bot upload boundary limit.")
                    if os.path.exists(file_path):
                        os.remove(file_path)
                    return
                
                async def tg_progress(cur, tot):
                    await progress_bar_handler(cur, tot, loading_msg, "Uploading file to Telegram...")
                
                await client.send_document(
                    chat_id=message.chat.id,
                    document=file_path,
                    caption=f"📁 **Direct File Uploaded**\nSource: `{text}`",
                    progress=tg_progress
                )
                await log_event(f"✅ **Direct Upload:** File from URL `{text}` was successfully uploaded for User `{user_id}`.")
                if os.path.exists(file_path):
                    os.remove(file_path)
                await loading_msg.delete()
            except Exception as e:
                await loading_msg.edit_text(f"❌ Failed to process direct file URL.\nError: `{str(e)}`")
                await log_event(f"❌ **Direct Upload Error:** Failed to upload `{text}`. Details: `{str(e)}`")

# 3. Intercept incoming files for streaming
@app.on_message((filters.document | filters.video | filters.audio | filters.voice) & filters.private)
async def file_stream_interceptor(client: Client, message: Message):
    media_obj = None
    for attr in ["document", "video", "audio", "voice"]:
        if hasattr(message, attr) and getattr(message, attr) is not None:
            media_obj = getattr(message, attr)
            break
            
    if not media_obj:
        return
        
    file_name = getattr(media_obj, "file_name", None) or "file"
    file_size = media_obj.file_size
    mime_type = getattr(media_obj, "mime_type", "application/octet-stream")
    
    token = str(uuid.uuid4())[:12]
    
    from modules.stream_handler import STREAM_CACHE
    STREAM_CACHE[token] = {
        "chat_id": message.chat.id,
        "message_id": message.id,
        "file_name": file_name,
        "file_size": file_size,
        "mime_type": mime_type,
        "created_at": time.time()  # Timestamp to enforce 24-hour expiration check
    }
    
    safe_file_name = urllib.parse.quote(file_name)
    stream_url = f"{config.DOMAIN}/stream/{token}/{safe_file_name}"
    
    await log_event(f"🔗 **Stream Link Generated:** File `{file_name}` ({round(file_size / (1024*1024), 2)}MB) generated stream link.")
    
    await message.reply_text(
        f"🔗 **File Stream Link Generated!**\n\n"
        f"📁 **File:** `{file_name}`\n"
        f"📦 **Size:** {round(file_size / (1024*1024), 2)} MB\n\n"
        f"⬇️ **Direct Link (Expires in 24 hours):**\n`{stream_url}`\n\n"
        f"_Data is pulled on-the-fly directly from Telegram's servers._"
    )

# 4. Glass buttons Callback Handlers
@app.on_callback_query()
async def callback_dispatcher(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    user_id = callback_query.from_user.id
    
    if not is_authorized(user_id):
        await callback_query.answer("Unauthorized.", show_alert=True)
        return

    # Admin Handlers
    if data.startswith("admin_"):
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
            await log_event(f"🧹 **Admin action:** System creator cleared all active streaming sessions from memory.")
            
        elif data == "admin_list":
            db = load_database()
            users = db["authorized"]
            text = "📋 **Authorized Users List:**\n" + "\n".join([f"• `{uid}`" for uid in users]) if users else "No additional users authorized."
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_main")]])
            await callback_query.message.edit_text(text, reply_markup=keyboard)
            await callback_query.answer()
            
        elif data == "admin_blacklist":
            db = load_database()
            blacklisted = db["blacklisted"]
            text = "🚫 **Banned Intruders List:**\n" + "\n".join([f"• `{uid}`" for uid in blacklisted]) if blacklisted else "Blacklist registry is empty."
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔓 Unban User", callback_data="admin_unban")],
                [InlineKeyboardButton("◀️ Back", callback_data="admin_main")]
            ])
            await callback_query.message.edit_text(text, reply_markup=keyboard)
            await callback_query.answer()
            
        elif data == "admin_unban":
            await callback_query.message.delete()
            await client.send_message(
                chat_id=user_id,
                text="Please reply to this message with the numerical ID of the blocked user you want to unban.",
                reply_markup=pyrogram.types.ForceReply(placeholder="e.g. 123456789")
            )
            await callback_query.answer()
            
        elif data == "admin_main":
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("👥 List Users", callback_data="admin_list"), InlineKeyboardButton("➕ Add User", callback_data="admin_add")],
                [InlineKeyboardButton("➖ Remove User", callback_data="admin_remove"), InlineKeyboardButton("🚫 Blacklist Logs", callback_data="admin_blacklist")],
                [InlineKeyboardButton("🧹 Clear Streams", callback_data="admin_clear_streams"), InlineKeyboardButton("❌ Close Console", callback_data="admin_close")]
            ])
            await callback_query.message.edit_text("🛠 **Admin System Console**\nChoose an administrative action below:", reply_markup=keyboard)
            await callback_query.answer()
            
        elif data in ["admin_add", "admin_remove"]:
            action_text = "authorize" if data == "admin_add" else "remove"
            await callback_query.message.delete()
            await client.send_message(
                chat_id=user_id,
                text=f"Please reply to this message with the numerical ID of the user you want to {action_text}.",
                reply_markup=pyrogram.types.ForceReply(placeholder="e.g. 123456789")
            )
            await callback_query.answer()

    # Download Handler
    elif data.startswith("dl:"):
        _, cache_id, action, format_id = data.split(":")
        
        if action == "cancel":
            DOWNLOAD_CACHE.pop(cache_id, None)
            await callback_query.message.delete()
            await callback_query.answer("Cancelled.")
            return
            
        cache_data = DOWNLOAD_CACHE.get(cache_id)
        if not cache_data:
            await callback_query.answer("⚠️ Session expired or not found.", show_alert=True)
            return
            
        # Extract file size prediction to check limits beforehand
        target_list = cache_data["videos"] if action == 'v' else cache_data["audios"]
        target_fmt = next((f for f in target_list if f["format_id"] == format_id), None)
        if target_fmt and target_fmt["bytes"] > (2000 * 1024 * 1024):
            await callback_query.answer("❌ This file exceeds Telegram's 2GB bot upload limit. Please select another quality.", show_alert=True)
            return
            
        await callback_query.message.edit_text("⚡️ Downloading file from server to VPS...")
        await callback_query.answer("Starting download...")
        
        loop = asyncio.get_event_loop()
        try:
            from utils.downloader import download_media, probe_video_dimensions
            
            # Progress reporting logic
            async def progress_reporter(curr, tot):
                await progress_bar_handler(curr, tot, callback_query.message, "Downloading from server to VPS...")
                
            def thread_progress(curr, tot):
                # Call async progress bar thread-safely from the yt-dlp worker thread
                asyncio.run_coroutine_threadsafe(progress_reporter(curr, tot), loop)
            
            result = await loop.run_in_executor(
                None, download_media, cache_data["url"], format_id, action, cache_id, thread_progress
            )
            
            file_path = result['file_path']
            thumb_path = result['thumb_path']
            title = result['title']
            uploader = result['uploader']
            
            await callback_query.message.edit_text("📤 Uploading media to Telegram...")
            
            async def tg_progress(curr, tot):
                await progress_bar_handler(curr, tot, callback_query.message, "Uploading file to Telegram...")
            
            if action == 'a':  # Audio
                await client.send_audio(
                    chat_id=callback_query.message.chat.id,
                    audio=file_path,
                    title=title,
                    performer=uploader,
                    duration=int(result['duration']),
                    thumb=thumb_path,
                    caption=f"🎵 **{title}**\nUploaded via Downloader Bot",
                    progress=tg_progress
                )
            elif action == 'v':  # Video
                width, height, duration = probe_video_dimensions(file_path)
                await client.send_video(
                    chat_id=callback_query.message.chat.id,
                    video=file_path,
                    width=width,
                    height=height,
                    duration=duration,
                    thumb=thumb_path,
                    supports_streaming=True,
                    caption=f"🎥 **{title}**\nUploaded via Downloader Bot",
                    progress=tg_progress
                )
                
            await log_event(f"✅ **Successful Transfer:** `{title}` was successfully downloaded and sent to chat `{callback_query.message.chat.id}`.")
            await callback_query.message.delete()
            
            # Cleanup disk files
            if os.path.exists(file_path):
                os.remove(file_path)
            if thumb_path and os.path.exists(thumb_path):
                os.remove(thumb_path)
                
            base_path, _ = os.path.splitext(file_path)
            for ext in ['.jpg', '.jpeg', '.png', '.webp', '.mp4', '.mkv', '.mp3']:
                test_path = f"{base_path}{ext}"
                if os.path.exists(test_path):
                    os.remove(test_path)
                    
            DOWNLOAD_CACHE.pop(cache_id, None)
            
        except Exception as e:
            await callback_query.message.edit_text(f"❌ Download/Upload failure.\nError: `{str(e)}`")
            await log_event(f"❌ **Transfer Failure:** Extraction/Upload crashed on target `{cache_data['url']}`. Details: `{str(e)}`")

# 5. Handle ForceReply inputs for adding/removing/unbanning users
@app.on_message(filters.reply & filters.private)
async def admin_input_handler(client: Client, message: Message):
    if message.from_user.id != config.SYSTEM_CREATOR_ID:
        return
    
    reply_text = message.reply_to_message.text
    input_text = message.text.strip()
    
    if not input_text.isdigit():
        await message.reply_text("❌ Error: Invalid ID. Send numerical input only.")
        return
        
    target_id = int(input_text)
    
    if "user you want to authorize" in reply_text:
        if add_user(target_id):
            await message.reply_text(f"✅ User `{target_id}` has been authorized successfully.")
            await log_event(f"👥 **User Authorized:** Creator whitelisted User ID `{target_id}`.")
        else:
            await message.reply_text(f"ℹ️ User `{target_id}` was already authorized.")
    elif "user you want to remove" in reply_text:
        if remove_user(target_id):
            await message.reply_text(f"✅ User `{target_id}` has been removed.")
            await log_event(f"👥 **User Revoked:** Creator removed User ID `{target_id}` from whitelist.")
        else:
            await message.reply_text(f"❌ User `{target_id}` was not found in the list.")
    elif "user you want to unban" in reply_text:
        if unblacklist_user(target_id):
            await message.reply_text(f"✅ User `{target_id}` has been unbanned.")
            await log_event(f"🔓 **User Unbanned:** Creator unbanned and unblacklisted User ID `{target_id}`.")
        else:
            await message.reply_text(f"❌ User `{target_id}` was not found in the blacklist.")

if __name__ == "__main__":
    import pyrogram.types
    from utils.updater import auto_update_ytdlp
    
    print("Starting Telegram Bot...")
    
    loop = asyncio.get_event_loop()
    loop.create_task(auto_update_ytdlp())
    
    # Startup Web server and Telegram MTProto Client concurrently
    from modules.stream_handler import fastapi_app
    config_uvicorn = uvicorn.Config(
        fastapi_app,
        host="0.0.0.0",
        port=8080,
        log_level="info",
        loop="asyncio"
    )
    server = uvicorn.Server(config_uvicorn)
    
    # Bind the app reference dynamically to our streaming service
    import modules.stream_handler
    modules.stream_handler.tg_client = app
    
    loop.run_until_complete(app.start())
    print("Telegram Bot Online.")
    
    loop.run_until_complete(server.serve())

import os
import uuid
import asyncio
import urllib.parse
import aiohttp
import uvicorn
from pyrogram import Client, filters
from pyrogram.types import (
    Message, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton
)
import config
from utils.gate import is_authorized, load_authorized_users, add_user, remove_user

# In-memory caches for format selections and links
DOWNLOAD_CACHE = {}

app = Client(
    "media_bot",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN
)

def is_link(text: str) -> bool:
    return text.startswith("http://") or text.startswith("https://")

def is_social_media_link(url: str) -> bool:
    """Check if the target link belongs to supported media crawlers."""
    url_lower = url.lower()
    social_domains = ["youtube.com", "youtu.be", "instagram.com", "tiktok.com"]
    return any(domain in url_lower for domain in social_domains)

async def download_direct_file(url: str, cache_id: str) -> str:
    """Download a raw URL stream to local cache directory."""
    os.makedirs("cache", exist_ok=True)
    
    parsed_url = urllib.parse.urlparse(url)
    file_name = os.path.basename(parsed_url.path) or f"download_{cache_id}"
    file_name = urllib.parse.unquote(file_name)
    
    out_path = f"cache/{cache_id}_{file_name}"
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=1800) as response:
            if response.status != 200:
                raise RuntimeError(f"Server returned error code {response.status}")
            
            with open(out_path, "wb") as f:
                async for chunk in response.content.iter_chunked(1024 * 1024):  # 1MB chunks
                    f.write(chunk)
                    
    return out_path

# 1. Protection Gate: Intercept all incoming messages
@app.on_message(group=-1)
async def security_gate(client: Client, message: Message):
    if not message.from_user:
        message.stop_propagation()
    user_id = message.from_user.id
    if not is_authorized(user_id):
        message.stop_propagation()

# 2. Start Handler: Sends the console keyboard tray ONCE
@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id == config.SYSTEM_CREATOR_ID:
        creator_keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton("🛠 Console")]],
            resize_keyboard=True
        )
        await message.reply_text(
            "Welcome, Administrator. Your system control desk is attached below.",
            reply_markup=creator_keyboard
        )
    else:
        await message.reply_text(
            "Hello! You are authorized to use this downloader. Send me a link to get started."
        )

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
        
    file_name = getattr(media_obj, "file_name", None)
    if not file_name:
        if message.video:
            file_name = "video.mp4"
        elif message.audio:
            file_name = "audio.mp3"
        elif message.voice:
            file_name = "voice.ogg"
        else:
            file_name = "document"
            
    file_size = media_obj.file_size
    mime_type = getattr(media_obj, "mime_type", "application/octet-stream")
    
    # Generate direct stream identifier
    token = str(uuid.uuid4())[:12]
    
    from modules.stream_handler import STREAM_CACHE
    STREAM_CACHE[token] = {
        "chat_id": message.chat.id,
        "message_id": message.id,
        "file_name": file_name,
        "file_size": file_size,
        "mime_type": mime_type
    }
    
    safe_file_name = urllib.parse.quote(file_name)
    stream_url = f"{config.DOMAIN}/stream/{token}/{safe_file_name}"
    
    await message.reply_text(
        f"🔗 **File Stream Link Generated!**\n\n"
        f"📁 **File:** `{file_name}`\n"
        f"📦 **Size:** {round(file_size / (1024*1024), 2)} MB\n\n"
        f"⬇️ **Direct Link (No VPS storage space used):**\n`{stream_url}`\n\n"
        f"_Click to stream or download. Data is pulled on-the-fly directly from Telegram._"
    )

# 4. Text and URL Handler
@app.on_message(filters.text & filters.private)
async def text_handler(client: Client, message: Message):
    text = message.text.strip()
    
    if text == "🛠 Console" and message.from_user.id == config.SYSTEM_CREATOR_ID:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 List Users", callback_data="admin_list"), InlineKeyboardButton("➕ Add User", callback_data="admin_add")],
            [InlineKeyboardButton("➖ Remove User", callback_data="admin_remove"), InlineKeyboardButton("❌ Close Console", callback_data="admin_close")]
        ])
        await message.reply_text("🛠 **Admin System Console**\nChoose an administrative action below:", reply_markup=keyboard)
        return

    if is_link(text):
        if is_social_media_link(text):
            # Process YouTube, Instagram, or TikTok link format stats
            loading_msg = await message.reply_text("🔍 Analyzing link, please wait...")
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
                            text=f"🎥 {v['quality']} ({v['size_mb']}MB)",
                            callback_data=f"dl:{cache_id}:v:{v['format_id']}"
                        ))
                    else:
                        row.append(InlineKeyboardButton(text="—", callback_data="none"))
                    
                    if i < len(audios):
                        a = audios[i]
                        row.append(InlineKeyboardButton(
                            text=f"🎵 {a['quality']} ({a['size_mb']}MB)",
                            callback_data=f"dl:{cache_id}:a:{a['format_id']}"
                        ))
                    else:
                        row.append(InlineKeyboardButton(text="—", callback_data="none"))
                    keyboard_rows.append(row)
                    
                keyboard_rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"dl:{cache_id}:cancel")])
                
                await message.reply_text(
                    f"📥 **Format Selection**\n\n📝 **Title:** {data['title']}\n"
                    f"⏱ **Duration:** {int(data['duration'] // 60)}m {int(data['duration'] % 60)}s\n\n"
                    f"Select an option to download and upload directly to Telegram:",
                    reply_markup=InlineKeyboardMarkup(keyboard_rows)
                )
            except Exception as e:
                await message.reply_text(f"❌ Extraction failed.\nError: `{str(e)}`")
            finally:
                await loading_msg.delete()
        else:
            # Direct link upload (URL Uploader feature)
            loading_msg = await message.reply_text("📥 Direct File URL detected. Downloading to cache...")
            cache_id = str(uuid.uuid4())[:8]
            try:
                file_path = await download_direct_file(text, cache_id)
                await loading_msg.edit_text("📤 Uploading file to Telegram...")
                await client.send_document(
                    chat_id=message.chat.id,
                    document=file_path,
                    caption=f"📁 **Direct File Uploaded**\nSource: `{text}`"
                )
                if os.path.exists(file_path):
                    os.remove(file_path)
                await loading_msg.delete()
            except Exception as e:
                await loading_msg.edit_text(f"❌ Failed to process direct file URL.\nError: `{str(e)}`")

# 5. Callback Handlers
@app.on_callback_query()
async def callback_dispatcher(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    user_id = callback_query.from_user.id
    
    if not is_authorized(user_id):
        await callback_query.answer("Unauthorized.", show_alert=True)
        return

    if data.startswith("admin_"):
        if user_id != config.SYSTEM_CREATOR_ID:
            await callback_query.answer("Access Denied.", show_alert=True)
            return
            
        if data == "admin_close":
            await callback_query.message.delete()
            await callback_query.answer("Console closed.")
        elif data == "admin_list":
            users = load_authorized_users()
            text = "📋 **Authorized Users List:**\n" + "\n".join([f"• `{uid}`" for uid in users]) if users else "No additional users authorized."
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_main")]])
            await callback_query.message.edit_text(text, reply_markup=keyboard)
            await callback_query.answer()
        elif data == "admin_main":
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("👥 List Users", callback_data="admin_list"), InlineKeyboardButton("➕ Add User", callback_data="admin_add")],
                [InlineKeyboardButton("➖ Remove User", callback_data="admin_remove"), InlineKeyboardButton("❌ Close Console", callback_data="admin_close")]
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
            
        await callback_query.message.edit_text("⚡️ Downloading file from server to VPS...")
        await callback_query.answer("Starting download...")
        
        loop = asyncio.get_event_loop()
        try:
            from utils.downloader import download_media, probe_video_dimensions
            
            result = await loop.run_in_executor(
                None, download_media, cache_data["url"], format_id, action, cache_id
            )
            
            file_path = result['file_path']
            thumb_path = result['thumb_path']
            title = result['title']
            uploader = result['uploader']
            
            await callback_query.message.edit_text("📤 Uploading media to Telegram...")
            
            if action == 'a':  # Audio
                await client.send_audio(
                    chat_id=callback_query.message.chat.id,
                    audio=file_path,
                    title=title,
                    performer=uploader,
                    duration=int(result['duration']),
                    thumb=thumb_path,
                    caption=f"🎵 **{title}**\nUploaded via Downloader Bot"
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
                    supports_streaming=True,  # Crucial: allows video to play before it has fully buffered on client
                    caption=f"🎥 **{title}**\nUploaded via Downloader Bot"
                )
                
            await callback_query.message.delete()
            
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

# 6. ForceReply handler
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
        else:
            await message.reply_text(f"ℹ️ User `{target_id}` was already authorized.")
    elif "user you want to remove" in reply_text:
        if remove_user(target_id):
            await message.reply_text(f"✅ User `{target_id}` has been removed.")
        else:
            await message.reply_text(f"❌ User `{target_id}` was not found in the list.")

# 7. Asynchronous concurrent main thread
async def main():
    print("Initializing services...")
    
    # Bind the Pyrogram app to the FastAPI stream handler
    import modules.stream_handler
    modules.stream_handler.tg_client = app
    
    # Start Pyrogram
    await app.start()
    print("Telegram Bot Online.")
    
    # Configure and launch Uvicorn (FastAPI) Web Server
    from modules.stream_handler import fastapi_app
    config_uvicorn = uvicorn.Config(
        fastapi_app,
        host="0.0.0.0",
        port=8080,
        log_level="info",
        loop="asyncio"
    )
    server = uvicorn.Server(config_uvicorn)
    
    from utils.updater import auto_update_ytdlp
    
    # Run Uvicorn server and the yt-dlp 6-hour updater concurrently in the same loop
    await asyncio.gather(
        server.serve(),
        auto_update_ytdlp()
    )

if __name__ == "__main__":
    import sys
    try:
        # Custom loop startup to avoid uvicorn loop conflict
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("Stopping bot gracefully...")

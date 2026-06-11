import os
import uuid
import time
import asyncio
import urllib.parse
import aiohttp
import uvicorn
from pyrogram import Client, filters, utils
from pyrogram.types import (
    Message, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    CallbackQuery,
    ForceReply
)
import config
from utils.gate import (
    is_authorized, 
    load_database, 
    add_user, 
    remove_user, 
    is_blacklisted, 
    blacklist_user, 
    unblacklist_user,
    is_document_mode,
    toggle_document_mode
)
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
# Application Initializations
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

def is_link(text: str) -> bool:
    return text.startswith("http://") or text.startswith("https://")

def is_social_media_link(url: str) -> bool:
    url_lower = url.lower()
    social_domains = ["youtube.com", "youtu.be", "instagram.com", "tiktok.com", "twitter.com", "x.com"]
    return any(domain in url_lower for domain in social_domains)

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

async def download_direct_file(url: str, cache_id: str, progress_fn) -> str:
    """Download direct file URL stream to VPS cache."""
    os.makedirs("cache", exist_ok=True)
    parsed_url = urllib.parse.urlparse(url)
    file_name = os.path.basename(parsed_url.path) or f"download_{cache_id}"
    file_name = urllib.parse.unquote(file_name)
    out_path = f"cache/{cache_id}_{file_name}"
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=1800) as response:
            if response.status != 200:
                raise RuntimeError(f"Server returned error {response.status}")
            
            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0
            
            with open(out_path, "wb") as f:
                async for chunk in response.content.iter_chunked(512 * 1024):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_fn:
                        await progress_fn(downloaded, total_size)
                        
    return out_path

async def send_single_media(client_to_use, chat_id, file_path, action, title, uploader, duration, thumb_path, progress_fn, force_document=False):
    """Sends a single media file using the designated client (standard or premium)."""
    from utils.downloader import probe_video_dimensions
    
    if force_document:
        return await client_to_use.send_document(
            chat_id=chat_id,
            document=file_path,
            caption=f"📁 **Part:** `{os.path.basename(file_path)}`",
            progress=progress_fn
        )
        
    if action == 'a':
        return await client_to_use.send_audio(
            chat_id=chat_id,
            audio=file_path,
            title=title,
            performer=uploader,
            duration=int(duration),
            thumb=thumb_path,
            caption=f"🎵 **{title}**\nUploaded via Downloader Bot",
            progress=progress_fn
        )
    else:  # action == 'v'
        width, height, parsed_duration = probe_video_dimensions(file_path)
        final_duration = parsed_duration if parsed_duration > 0 else int(duration)
        return await client_to_use.send_video(
            chat_id=chat_id,
            video=file_path,
            width=width,
            height=height,
            duration=final_duration,
            thumb=thumb_path,
            supports_streaming=True,
            caption=f"🎥 **{title}**\nUploaded via Downloader Bot",
            progress=progress_fn
        )

async def process_split_and_upload(chat_id, file_path, action, title, uploader, duration, thumb_path, progress_msg):
    """
    On-Demand Sequential Uploader:
    Generates chunks one-by-one, uploads them, and immediately purges them from disk.
    Caps VPS disk overhead to exactly ONE chunk size.
    """
    from utils.downloader import split_file_generator
    
    file_size = os.path.getsize(file_path)
    use_premium = bool(premium_app and file_size > (2000 * 1024 * 1024))
    client_to_use = premium_app if use_premium else app
    
    # 1.95 GB for standard Bot, 3.9 GB for Premium Userbot
    max_chunk_size = (3900 * 1024 * 1024) if use_premium else (1950 * 1024 * 1024)
    force_document = is_document_mode(chat_id)
    
    is_split = file_size > max_chunk_size
    parts_list = []
    
    try:
        part_num = 1
        loop = asyncio.get_event_loop()
        generator = split_file_generator(file_path, max_chunk_size)
        
        while True:
            # Generate the next chunk sequentially inside executor to keep async loop free
            def get_next_part():
                try:
                    return next(generator)
                except StopIteration:
                    return None
            
            part_path = await loop.run_in_executor(None, get_next_part)
            if not part_path:
                break
                
            parts_list.append(part_path)
            
            async def upload_progress(cur, tot):
                part_label = f"part {part_num}" if is_split else "file"
                await progress_bar_handler(cur, tot, progress_msg, f"Uploading {part_label} to Telegram...")
                
            await progress_msg.edit_text(f"📤 Uploading part {part_num}...")
            
            # Split volumes are binary raw blocks: they MUST always be uploaded as documents!
            await send_single_media(
                client_to_use=client_to_use,
                chat_id=chat_id,
                file_path=part_path,
                action=action,
                title=title if not is_split else f"{title} (Part {part_num})",
                uploader=uploader,
                duration=duration,
                thumb_path=thumb_path if not is_split else None,
                progress_fn=upload_progress,
                force_document=force_document or is_split
            )
            
            # Purge part file immediately to recycle VPS space
            if part_path != file_path:
                if os.path.exists(part_path):
                    os.remove(part_path)
                    
            part_num += 1
            
        if os.path.exists(file_path):
            os.remove(file_path)
            
        await progress_msg.delete()
        
    except Exception as e:
        for p in parts_list:
            if p != file_path and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
        raise e

# =========================================================================
# Core Telegram Message Handlers
# =========================================================================

# 1. Gate: Pre-intercepts and blocks strangers & blacklisted users
@app.on_message(group=-1)
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

# 2. Main Private Message Router
@app.on_message(filters.text & filters.private)
async def text_handler(client: Client, message: Message):
    text = message.text.strip()
    user_id = message.from_user.id
    
    # Creator Admin Interface triggered directly inside chat
    if user_id == config.SYSTEM_CREATOR_ID and not is_link(text):
        doc_status = "🟢" if is_document_mode(user_id) else "🔴"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 List Users", callback_data="admin_list"), InlineKeyboardButton("➕ Add User", callback_data="admin_add")],
            [InlineKeyboardButton("➖ Remove User", callback_data="admin_remove"), InlineKeyboardButton("🚫 Blacklist Logs", callback_data="admin_blacklist")],
            [InlineKeyboardButton(f"📄 Doc Mode: {doc_status}", callback_data="admin_toggle_doc"), InlineKeyboardButton("🧹 Clear Streams", callback_data="admin_clear_streams")],
            [InlineKeyboardButton("❌ Close Console", callback_data="admin_close")]
        ])
        await message.reply_text(
            f"🛠 **Admin System Console**\nChoose an administrative action below:",
            reply_markup=keyboard
        )
        return

    # Authorized user greeting
    if user_id != config.SYSTEM_CREATOR_ID and not is_link(text):
        await message.reply_text(
            "👋 **Hello! Welcome to your Private Downloader Bot.**\n\n"
            "To get started:\n"
            "• Send me any YouTube, Instagram, TikTok, or X/Twitter link to download it.\n"
            "• Send me any direct file URL to upload it directly to Telegram.\n"
            "• Forward me a Telegram file (video, document, music) to generate an instant direct stream link."
        )
        return

    # Process link extraction inside queue
    if is_link(text):
        if is_social_media_link(text):
            status_msg = await message.reply_text("📥 Received. Analyzing link formats...")
            
            async def download_job():
                await status_msg.edit_text("🔍 Fetching format attributes...")
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
                    
                    await log_event(f"ℹ️ **Link analyzed:** `{data['title']}` for User `{user_id}`.")
                    await status_msg.delete()
                    await message.reply_text(
                        f"📥 **Format Selection**\n\n📝 **Title:** {data['title']}\n"
                        f"⏱ **Duration:** {int(data['duration'] // 60)}m {int(data['duration'] % 60)}s\n\n"
                        f"Select an option below:",
                        reply_markup=InlineKeyboardMarkup(keyboard_rows)
                    )
                except Exception as e:
                    await status_msg.edit_text(f"❌ Extraction failed.\nError: `{str(e)}`")
                    await log_event(f"❌ **Extraction Error:** Failed to parse `{text}`. Details: `{str(e)}`")

            await queue.add_task(user_id, status_msg, download_job)
        else:
            # Generic direct URL uploader inside queue
            status_msg = await message.reply_text("📥 Received URL. Queueing job...")
            
            async def direct_upload_job():
                await status_msg.edit_text("⚡ Starting direct URL download...")
                cache_id = str(uuid.uuid4())[:8]
                try:
                    async def dl_progress(cur, tot):
                        await progress_bar_handler(cur, tot, status_msg, "Downloading direct file to server...")
                        
                    file_path = await download_direct_file(text, cache_id, dl_progress)
                    await status_msg.edit_text("📤 Uploading direct file to Telegram...")
                    
                    # Split and upload sequentially (Toyota method)
                    await process_split_and_upload(
                        chat_id=message.chat.id,
                        file_path=file_path,
                        action='d',
                        title=os.path.basename(file_path),
                        uploader="Direct Link",
                        duration=0,
                        thumb_path=None,
                        progress_msg=status_msg
                    )
                    await log_event(f"✅ **Direct Upload:** Finished for User `{user_id}` from source `{text}`.")
                except Exception as e:
                    await status_msg.edit_text(f"❌ Failed to process direct file URL.\nError: `{str(e)}`")
                    await log_event(f"❌ **Direct Upload Error:** Failed on `{text}`. Details: `{str(e)}`")

            await queue.add_task(user_id, status_msg, direct_upload_job)

# 3. Intercept incoming Telegram files for streaming
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
        "created_at": time.time()
    }
    
    safe_file_name = urllib.parse.quote(file_name)
    stream_url = f"{config.DOMAIN}/stream/{token}/{safe_file_name}"
    
    await log_event(f"🔗 **Stream Link Generated:** `{file_name}` ({round(file_size / (1024*1024), 2)}MB) for User `{message.from_user.id}`.")
    
    await message.reply_text(
        f"🔗 **File Stream Link Generated!**\n\n"
        f"📁 **File:** `{file_name}`\n"
        f"📦 **Size:** {round(file_size / (1024*1024), 2)} MB\n\n"
        f"⬇️ **Direct Link (Expires in 24 hours):**\n`{stream_url}`\n\n"
        f"_Data is pulled on-the-fly directly from Telegram's servers._"
    )

# 4. Callback Query Dispatcher (Console administration and selections)
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
            await callback_query.message.edit_text(
                f"🛠 **Admin System Console**\nChoose an administrative action below:",
                reply_markup=keyboard
            )
            
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
            
        await callback_query.message.edit_text("⏳ Request enqueued in Active Job Queue...")
        await callback_query.answer("Transfer enqueued...")
        
        # Define the processing closure to pass to queue
        async def queued_transfer_job():
            await callback_query.message.edit_text("⚡️ Downloading file from server to VPS...")
            loop = asyncio.get_event_loop()
            try:
                from utils.downloader import download_media
                
                async def download_progress(curr, tot):
                    await progress_bar_handler(curr, tot, callback_query.message, "Downloading from server to VPS...")
                    
                def thread_progress(curr, tot):
                    asyncio.run_coroutine_threadsafe(download_progress(curr, tot), loop)
                
                # Execute blocking download thread-safely
                result = await loop.run_in_executor(
                    None, download_media, cache_data["url"], format_id, action, cache_id, thread_progress
                )
                
                file_path = result['file_path']
                thumb_path = result['thumb_path']
                title = result['title']
                uploader = result['uploader']
                
                # Handover file to our dynamic on-demand split uploader (sequential split/upload/delete)
                await process_split_and_upload(
                    chat_id=callback_query.message.chat.id,
                    file_path=file_path,
                    action=action,
                    title=title,
                    uploader=uploader,
                    duration=result['duration'],
                    thumb_path=thumb_path,
                    progress_msg=callback_query.message
                )
                
                # Cleanup residual files in cache directory
                if thumb_path and os.path.exists(thumb_path):
                    os.remove(thumb_path)
                base_path, _ = os.path.splitext(file_path)
                for ext in ['.jpg', '.jpeg', '.png', '.webp', '.mp4', '.mkv', '.mp3']:
                    test_path = f"{base_path}{ext}"
                    if os.path.exists(test_path):
                        os.remove(test_path)
                        
                DOWNLOAD_CACHE.pop(cache_id, None)
                await log_event(f"✅ **Job Successful:** `{title}` was successfully processed and sent to User `{user_id}`.")
                
            except Exception as e:
                await callback_query.message.edit_text(f"❌ Download/Upload failure.\nError: `{str(e)}`")
                await log_event(f"❌ **Job Failure:** Extraction/Upload crashed on `{cache_data['url']}`. Details: `{str(e)}`")

        # Hand over task to queue to execute sequentially
        await queue.add_task(user_id, callback_query.message, queued_transfer_job)

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
            await log_event(f"👥 **User Whitelisted:** Creator whitelisted User ID `{target_id}`.")
        else:
            await message.reply_text(f"ℹ️ User `{target_id}` was already authorized.")
    elif "user you want to remove" in reply_text:
        if remove_user(target_id):
            await message.reply_text(f"✅ User `{target_id}` has been removed.")
            await log_event(f"👥 **User Revoked:** Creator removed User ID `{target_id}`.")
        else:
            await message.reply_text(f"❌ User `{target_id}` was not found in the list.")
    elif "user you want to unban" in reply_text:
        if unblacklist_user(target_id):
            await message.reply_text(f"✅ User `{target_id}` has been unbanned.")
            await log_event(f"🔓 **User Unbanned:** Creator unbanned and unblacklisted User ID `{target_id}`.")
        else:
            await message.reply_text(f"❌ User `{target_id}` was not found in the blacklist.")

# =========================================================================
# Event Loop Bootstrap & Startup Configuration
# =========================================================================

async def main():
    print("Initializing services...")
    
    # Bind the app reference dynamically to our streaming service
    import modules.stream_handler
    modules.stream_handler.tg_client = app
    
    # Start Standard Bot Client
    await app.start()
    print("Telegram Bot Online.")
    
    # Start Premium Userbot Client if session is configured
    if premium_app:
        await premium_app.start()
        print("Premium Userbot Client connected.")
    
    # Configure and launch Uvicorn (FastAPI Web Server) on port 8080
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
    
    # Run FastAPI web server and the 6-hour yt-dlp nightly updater concurrently
    await asyncio.get_event_loop().create_task(server.serve())
    await asyncio.get_event_loop().create_task(auto_update_ytdlp())

if __name__ == "__main__":
    import sys
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("Stopping bot gracefully...")

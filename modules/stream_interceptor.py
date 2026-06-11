# modules/stream_interceptor.py
import uuid
import time
import urllib.parse
from pyrogram import Client, filters
from pyrogram.types import Message
import config

def register_stream_interceptor_handlers(app: Client):
    # Import log_event dynamically to prevent circular imports
    from main import log_event

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
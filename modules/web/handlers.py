# modules/web/handlers.py — /web webpage → markdown (ported from balebot)
import os
import uuid
from pyrogram import Client, filters
from pyrogram.types import Message
from utils.propagation import stop
from modules.web.api import fetch_markdown_text

def register_web_handlers(app: Client, premium_app: Client | None = None):
    @app.on_message(filters.command("web") & filters.private, group=0)
    async def web_handler(client: Client, message: Message):
        raw = (message.text or "")[4:].strip()
        if not raw:
            await message.reply_text("⚠️ **Usage:** `/web <url>`")
            stop(message)
            return
        # take first token as url
        url = raw.split()[0].strip()
        if not url.startswith("http"):
            url = "https://" + url
        status = await message.reply_text("🔍 Fetching webpage and converting to Markdown...")
        try:
            title, md = await fetch_markdown_text(url)
            if not md.strip():
                await status.edit_text("ℹ️ No readable markdown from this page.")
                stop(message)
                return
            sanitized = "".join(c if c.isalnum() or c in " ._-" else "_" for c in title)
            if len(md) > 3500:
                os.makedirs("cache", exist_ok=True)
                cid = str(uuid.uuid4())[:8]
                p = f"cache/{cid}_{sanitized}.txt"
                with open(p, "w", encoding="utf-8") as f:
                    f.write(f"🌐 Webpage Markdown\n🔗 Source: {url}\n{'='*40}\n\n{md}")
                from utils.uploader_handler import process_split_and_upload
                await process_split_and_upload(bot_client=app, premium_client=premium_app, chat_id=message.chat.id, file_path=p, action='d', title=f"{sanitized}.txt", uploader="Web", duration=0, thumb_path=None, progress_msg=status, reply_to_message_id=message.id)
                from main import log_event
                await log_event(f"📄 **Web Markdown:** `{title}` user {message.from_user.id}")
            else:
                await status.edit_text(f"🌐 **Webpage:** `{title}`\n\n```\n{md[:3900]}\n```")
                from main import log_event
                await log_event(f"📄 **Web Markdown:** displayed `{title}` user {message.from_user.id}")
        except Exception as e:
            await status.edit_text(f"❌ Failed to extract: `{e}`")
            from main import log_event
            await log_event(f"❌ **Web Error:** {url} {e}")
        stop(message)

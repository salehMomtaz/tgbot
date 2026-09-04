# modules/translate/handlers.py
from pyrogram import Client, filters
from pyrogram.types import Message
from utils.propagation import stop
from utils.gate import is_authorized
from modules.translate.api import google_translate_async


def register_translate_handlers(app: Client):
    @app.on_message(filters.command("tr") & filters.private, group=0)
    async def tr_handler(client: Client, message: Message):
        if not is_authorized(message.from_user.id):
            # Strangers stay invisible to extras: no reply (avoids oracle),
            # no fall-through (avoids the downloader grabbing "/tr ...").
            stop(message)
            return
        txt = (message.text or "").strip()
        args = txt[3:].strip() if len(txt) > 3 else ""
        if not args:
            await message.reply_text(
                "🈯 **Google Translate**\n\n` /tr src:dst text `\n\n*Examples:*\n• `/tr fa:en hello`\n• `/tr auto:en hello`",
            )
            stop(message)
            return
        parts = args.split(None, 1)
        lang_pair = parts[0].strip()
        if ":" not in lang_pair:
            await message.reply_text("⚠️ Language pair must be `src:dst` (e.g., `fa:en`).")
            stop(message)
            return
        src, dst = lang_pair.split(":", 1)
        src = src.strip().lower(); dst = dst.strip().lower()
        if len(parts) < 2:
            await message.reply_text("⚠️ Please write the text to translate after the codes.")
            stop(message)
            return
        target = parts[1].strip()
        try:
            trans = await google_translate_async(target, src, dst)
            await message.reply_text(f"🈯 **Translation ({src} → {dst})**\n\n```\n{trans}\n```")
        except Exception as e:
            await message.reply_text(f"❌ Translation failed: `{e}`")
        stop(message)

# modules/translate/handlers.py
from pyrogram import Client, filters
from pyrogram.types import Message
from modules.translate.api import google_translate_async


def register_translate_handlers(app: Client):
    @app.on_message(filters.command("tr") & filters.private, group=0)
    async def tr_handler(client: Client, message: Message):
        txt = (message.text or "").strip()
        args = txt[3:].strip() if len(txt) > 3 else ""
        if not args:
            await message.reply_text(
                "🈯 **Google Translate**\n\n` /tr src:dst text `\n\n*Examples:*\n• `/tr fa:en hello`\n• `/tr auto:en hello`",
            )
            try: message.stop_propagation()
            except: pass
            return
        parts = args.split(None, 1)
        lang_pair = parts[0].strip()
        if ":" not in lang_pair:
            await message.reply_text("⚠️ Language pair must be `src:dst` (e.g., `fa:en`).")
            try: message.stop_propagation()
            except: pass
            return
        src, dst = lang_pair.split(":", 1)
        src = src.strip().lower(); dst = dst.strip().lower()
        if len(parts) < 2:
            await message.reply_text("⚠️ Please write the text to translate after the codes.")
            try: message.stop_propagation()
            except: pass
            return
        target = parts[1].strip()
        try:
            trans = await google_translate_async(target, src, dst)
            await message.reply_text(f"🈯 **Translation ({src} → {dst})**\n\n```\n{trans}\n```")
        except Exception as e:
            await message.reply_text(f"❌ Translation failed: `{e}`")
        try: message.stop_propagation()
        except: pass

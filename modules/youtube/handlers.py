# modules/youtube/handlers.py — pyrogram port of balebot youtube module
import os
import uuid
import glob
import shutil
import asyncio
import urllib.parse

import yt_dlp
from pyrogram import Client, filters
from pyrogram.types import Message

import config
from utils.shared import queue
from utils.gate import is_authorized
from modules.youtube.scraper import clean_vtt_subtitles, search_ytdlp_flat


def register_youtube_handlers(app: Client, premium_app: Client | None = None):
    @app.on_message(filters.command("yt") & filters.private, group=0)
    async def yt_search(client: Client, message: Message):
        # parse: /yt [limit] query
        raw = (message.text or "")[3:].strip()
        if not raw:
            await message.reply_text("⚠️ **Usage:** `/yt <query>` or `/yt <limit> <query>`")
            try: message.stop_propagation()
            except: pass
            return
        parts = raw.split(None, 1)
        limit = 5; query = raw
        if parts[0].isdigit():
            n = int(parts[0])
            if 1 <= n <= 15:
                limit = n
                query = parts[1].strip() if len(parts) > 1 else ""
        if not query:
            await message.reply_text("⚠️ Please provide a search query.")
            try: message.stop_propagation()
            except: pass
            return
        status = await message.reply_text("🔍 Searching YouTube...")
        try:
            entries = await search_ytdlp_flat(query, limit)
            if not entries:
                await status.edit_text("ℹ️ No videos found.")
                try: message.stop_propagation()
                except: pass
                return
            lines = []
            for idx, e in enumerate(entries, 1):
                title = e.get('title', 'Unknown')
                vid = e.get('id')
                uploader = e.get('uploader', 'Unknown')
                dur = e.get('duration')
                dstr = f"{int(dur//60)}m {int(dur%60)}s" if dur else "??"
                lines.append(f"{idx}. **{title}**\n   👤 `{uploader}` | ⏱ `{dstr}`\n   🔗 https://youtu.be/{vid}")
            await status.edit_text("🎬 **YouTube Results:**\n\n" + "\n\n".join(lines))
        except Exception as e:
            await status.edit_text(f"❌ Search failed: {e}")
        try: message.stop_propagation()
        except: pass

    @app.on_message(filters.command("ytrecent") & filters.private, group=0)
    async def ytrecent_handler(client: Client, message: Message):
        raw = (message.text or "")[9:].strip()
        if not raw:
            await message.reply_text("⚠️ **Usage:** `/ytrecent <@channel_handle> [count]`")
            try: message.stop_propagation()
            except: pass
            return
        parts = raw.split()
        channel = parts[0].strip()
        limit = 5
        if len(parts) > 1 and parts[1].isdigit():
            limit = min(int(parts[1]), 15)
        status = await message.reply_text(f"🔍 Fetching recent uploads for `{channel}`...")
        try:
            clean_channel = channel if channel.startswith("@") else f"@{channel}"
            url = f"https://www.youtube.com/{clean_channel}/videos"
            loop = asyncio.get_event_loop()
            def extract():
                ydl_opts = {'quiet': True, 'extract_flat': True, 'skip_download': True, 'proxy': getattr(config, 'YTDLP_PROXY', None)}
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    return ydl.extract_info(url, download=False)
            info = await loop.run_in_executor(None, extract)
            entries = info.get('entries', [])[:limit]
            if not entries:
                await status.edit_text("ℹ️ No recent uploads found.")
                try: message.stop_propagation()
                except: pass
                return
            lines = [f"{i}. **{e.get('title','Unknown')}**\n   🔗 https://youtu.be/{e.get('id')}" for i,e in enumerate(entries,1)]
            await status.edit_text(f"🎬 **Recent: {clean_channel}**\n\n" + "\n\n".join(lines))
        except Exception as e:
            await status.edit_text(f"❌ Failed: {e}")
        try: message.stop_propagation()
        except: pass

    @app.on_message(filters.command("ytch") & filters.private, group=0)
    async def ytch_handler(client: Client, message: Message):
        raw = (message.text or "")[5:].strip()
        if not raw:
            await message.reply_text("⚠️ **Usage:** `/ytch <@channel_handle> <query>`")
            try: message.stop_propagation()
            except: pass
            return
        parts = raw.split(None, 1)
        channel = parts[0].strip()
        query = parts[1].strip() if len(parts) > 1 else ""
        if not query:
            await message.reply_text("⚠️ Please specify a search keyword.")
            try: message.stop_propagation()
            except: pass
            return
        status = await message.reply_text(f"🔍 Searching `{query}` inside `{channel}`...")
        try:
            clean_channel = channel if channel.startswith("@") else f"@{channel}"
            search_q = f"{clean_channel} {query}"
            entries = await search_ytdlp_flat(search_q, 5)
            if not entries:
                await status.edit_text("ℹ️ No matching videos found.")
                try: message.stop_propagation()
                except: pass
                return
            lines = [f"{i}. **{e.get('title','Unknown')}**\n   🔗 https://youtu.be/{e.get('id')}" for i,e in enumerate(entries,1)]
            await status.edit_text(f"🎬 **Results inside {clean_channel} matching `{query}`:**\n\n" + "\n\n".join(lines))
        except Exception as e:
            await status.edit_text(f"❌ Failed: {e}")
        try: message.stop_propagation()
        except: pass

    @app.on_message(filters.command("transcript") & filters.private, group=0)
    async def transcript_handler(client: Client, message: Message):
        url = (message.text or "")[11:].strip()
        if not url:
            await message.reply_text("⚠️ **Usage:** `/transcript <youtube_video_url>`")
            try: message.stop_propagation()
            except: pass
            return
        status = await message.reply_text("📥 Enqueueing transcript job...")
        user_id = message.from_user.id
        # check auth gate via is_authorized (security gate already did, but keep)
        if not is_authorized(user_id):
            await status.edit_text("🚫 Not authorized.")
            try: message.stop_propagation()
            except: pass
            return
        async def transcript_job():
            await status.edit_text("⚡ Requesting subtitle extraction...")
            cache_id = str(uuid.uuid4())[:8]
            task_dir = f"cache/{cache_id}"
            os.makedirs(task_dir, exist_ok=True)
            loop = asyncio.get_event_loop()
            def extract_subs():
                ydl_opts = {'quiet': True, 'skip_download': True, 'writeautomaticsub': True, 'writesubtitles': True, 'subtitleslangs': ['en', 'fa', 'auto'], 'outtmpl': f"{task_dir}/subtitle", 'proxy': getattr(config, 'YTDLP_PROXY', None)}
                from utils.downloader.cookies import get_cookies_for_url
                try:
                    ydl_opts['cookiefile'] = get_cookies_for_url(url)
                except: pass
                if getattr(config, 'YTDLP_USER_AGENT', ''):
                    ydl_opts['user_agent'] = config.YTDLP_USER_AGENT
                try:
                    from utils.downloader.cookies import _apply_pot_options as _apply
                    # use url_normalize's patch if available
                    from utils.downloader.url_normalize import _apply_pot_options as _pot
                    ydl_opts = _pot(ydl_opts, url)
                except Exception:
                    try:
                        from operators.downloader import _apply_pot_options as _old
                        ydl_opts = _old(ydl_opts, url)
                    except: pass
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    return ydl.extract_info(url, download=True)
            try:
                info = await loop.run_in_executor(None, extract_subs)
                title = info.get('title', 'Unknown')
                found = glob.glob(os.path.join(task_dir, "subtitle*.*.vtt"))
                if not found:
                    raise FileNotFoundError("No subtitles could be extracted.")
                vtt_path = found[0]
                await status.edit_text("🧹 Cleaning transcript...")
                clean_text = await loop.run_in_executor(None, clean_vtt_subtitles, vtt_path)
                sanitized = "".join(c if c.isalnum() or c in " ._-" else "_" for c in title)
                out_path = f"{task_dir}/{sanitized}_Transcript.txt"
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(f"📖 YouTube Transcript\n📝 Title: {title}\n🔗 URL: {url}\n{'='*40}\n\n{clean_text}")
                await status.edit_text("📤 Delivering transcript...")
                from utils.uploader_handler import process_split_and_upload
                await process_split_and_upload(bot_client=app, premium_client=premium_app, chat_id=user_id, file_path=out_path, action='d', title=f"{sanitized}_Transcript.txt", uploader="YouTube", duration=0, thumb_path=None, progress_msg=status, reply_to_message_id=message.id)
                from main import log_event
                await log_event(f"📖 **Transcript:** delivered for `{title}` user {user_id}")
            except Exception as e:
                await status.edit_text(f"❌ Transcript failed: `{e}`")
                from main import log_event
                await log_event(f"❌ **Transcript Error:** {url} {e}")
            finally:
                if os.path.exists(task_dir):
                    try: shutil.rmtree(task_dir)
                    except: pass
        await queue.add_task(user_id, status, transcript_job)
        try: message.stop_propagation()
        except: pass

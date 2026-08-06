# modules/downloader_handler.py
import os
import uuid
import shutil
import asyncio
import urllib.parse
import aiohttp
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
import config
from utils.shared import queue, DOWNLOAD_CACHE  # Fixed: Import from clean shared registry
from main import progress_bar_handler, log_event
from utils.gate import is_authorized, is_premium_user
from utils.downloader import (
    extract_formats,
    download_media,
    extract_playlist_meta,
    is_playlist_url,
    is_pure_playlist_url,
    PLAYLIST_TIERS,
)
from utils.uploader_handler import process_split_and_upload, send_reply_safe
from utils.rich_stream import RichStream


def is_link(text: str) -> bool:
    """Helper to detect if incoming text is a web link."""
    return text.startswith("http://") or text.startswith("https://")


def is_social_media_link(url: str) -> bool:
    """Check if the target link belongs to supported media crawlers."""
    url_lower = url.lower()
    social_domains = ["youtube.com", "youtu.be", "instagram.com", "tiktok.com", "twitter.com", "x.com"]
    return any(domain in url_lower for domain in social_domains)


# Metadata fetches (format extraction / playlist reading) run IMMEDIATELY and
# concurrently — they do NOT pass through the sequential DownloadQueue. Only the
# real download+upload jobs are queued. This lets a user fetch + pick formats for
# many links while earlier downloads are still running, instead of being blocked
# behind whichever download currently occupies the single worker slot.
# References are held so CPython can't GC a task mid-flight.
_bg_fetch_tasks: set = set()


def _spawn_fetch(coro) -> None:
    """Fire-and-forget a metadata-fetch coroutine on the event loop."""
    task = asyncio.create_task(coro)
    _bg_fetch_tasks.add(task)
    task.add_done_callback(_bg_fetch_tasks.discard)


# ===========================================================================
# Single-video format keyboard
# ===========================================================================
def build_format_keyboard(cache_id: str, videos: list, audios: list, premium_allowed: bool = True) -> InlineKeyboardMarkup:
    """Build the video/audio format-selection keyboard for a single media link.

    Video button sizes already include the merged best-audio track (see
    utils.downloader.extract_formats), so they match what actually gets uploaded.

    Formats over Telegram's 2 GB bot upload ceiling are only reachable through
    the Premium userbot (4 GB path). When the user is not Premium-whitelisted
    (*premium_allowed* False) those options are shown locked with a 🔒 and route
    to a "Premium required" answer instead of the download callback.
    """
    _TWO_GB = 2000 * 1024 * 1024

    def _btn(prefix: str, action: str, fmt: dict) -> InlineKeyboardButton:
        locked = not premium_allowed and fmt.get("bytes", 0) > _TWO_GB
        label = f"{prefix} {fmt['quality']} ({fmt['size_str']})"
        if locked:
            label = f"🔒 {label}"
        callback = f"dl:{cache_id}:lock" if locked else f"dl:{cache_id}:{action}:{fmt['format_id']}"
        return InlineKeyboardButton(text=label, callback_data=callback)

    max_rows = max(len(videos), len(audios))
    keyboard_rows = []
    for i in range(max_rows):
        row = []
        if i < len(videos):
            row.append(_btn("🎥", "v", videos[i]))
        else:
            row.append(InlineKeyboardButton(text="—", callback_data="none"))

        if i < len(audios):
            row.append(_btn("🎵", "a", audios[i]))
        else:
            row.append(InlineKeyboardButton(text="—", callback_data="none"))
        keyboard_rows.append(row)

    keyboard_rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"dl:{cache_id}:cancel")])
    return InlineKeyboardMarkup(keyboard_rows)


async def show_format_selection(message: Message, status: RichStream, url: str, custom_filename, user_id: int, origin_message_id: int | None = None):
    """Extract formats for a single media URL and post the selection keyboard.

    *origin_message_id* is the user's original link message. The keyboard (and
    later the uploaded file) quote-reply to it, even when we got here via a
    callback chain (playlist decision menu → explorer → a video) where the
    local *message* is one of the bot's own menus.

    *status* is a RichStream streaming the analysis progress; it is closed (and
    any fallback status message removed) before the keyboard is posted.
    """
    reply_to_id = origin_message_id or message.id
    await status.update("🔍 Fetching format attributes...")
    try:
        # extract_formats is a blocking yt-dlp call — run it off the event loop
        # so concurrent fetches (and any running download) keep making progress.
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, extract_formats, url)

        cache_id = str(uuid.uuid4())[:8]
        DOWNLOAD_CACHE[cache_id] = {
            "url": url,
            "title": data["title"],
            "videos": data["videos"],
            "audios": data["audios"],
            "thumbnail_url": data["thumbnail"],
            "custom_filename": custom_filename,
            "best_audio_format_id": data["best_audio_format_id"],
            "origin_message_id": reply_to_id,
        }

        premium_allowed = is_premium_user(user_id)
        keyboard = build_format_keyboard(cache_id, data["videos"], data["audios"], premium_allowed=premium_allowed)

        await log_event(f"ℹ️ **Link analyzed:** `{data['title']}` for User `{user_id}`.")
        await status.close()
        await send_reply_safe(
            message._client.send_message,
            reply_to_id,
            chat_id=message.chat.id,
            text=(
                f"📥 **Format Selection**\n\n📝 **Title:** {data['title']}\n"
                f"⏱ **Duration:** {int(data['duration'] // 60)}m {int(data['duration'] % 60)}s\n\n"
                f"Select an option below:"
                + ("" if premium_allowed else "\n\n🔒 = 4GB Premium upload (ask the admin to enable it)")
            ),
            reply_markup=keyboard,
        )
    except Exception as e:
        await status.close()
        await send_reply_safe(
            message._client.send_message,
            reply_to_id,
            chat_id=message.chat.id,
            text=f"❌ Extraction failed.\nError: `{str(e)}`",
        )
        await log_event(f"❌ **Extraction Error:** Failed to parse `{url}`. Details: `{str(e)}`")


# ===========================================================================
# Playlist keyboards: decision menu → tier selector / paginated explorer
# ===========================================================================
# Flow: any playlist link (pure /playlist?list=… or watch?v=…&list=…) lands on
# a DECISION keyboard first — "download the whole playlist" or "explore videos"
# and pick which ones. Only then does the user reach quality tiers or a single
# video's format keyboard. watch?v=&list= URLs also offer a "just this video"
# shortcut (ytdlnis-style).

def build_playlist_decision_keyboard(cache_id: str, include_single: bool) -> InlineKeyboardMarkup:
    """First menu shown for a playlist: whole-playlist vs explore (vs this video)."""
    rows = [
        [InlineKeyboardButton("⬇️ Download whole playlist", callback_data=f"pl:{cache_id}:whole")],
        [InlineKeyboardButton("🔎 Explore videos", callback_data=f"pl:{cache_id}:explore")],
    ]
    if include_single:
        rows.append([InlineKeyboardButton("▶️ Just this video", callback_data=f"pl:{cache_id}:single")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"pl:{cache_id}:cancel")])
    return InlineKeyboardMarkup(rows)


def build_playlist_tier_keyboard(cache_id: str) -> InlineKeyboardMarkup:
    """Quality-tier keyboard shown after 'Download whole playlist'.

    Three video tiers + three audio tiers (low/medium/high). Mapping to real
    qualities lives in utils.downloader.PLAYLIST_TIERS. Includes a back button to
    the decision menu.
    """
    rows = [
        [InlineKeyboardButton("🎥 High · 1080p", callback_data=f"pl:{cache_id}:vh"),
         InlineKeyboardButton("🎥 Medium · 720p", callback_data=f"pl:{cache_id}:vm"),
         InlineKeyboardButton("🎥 Low · 480p", callback_data=f"pl:{cache_id}:vl")],
        [InlineKeyboardButton("🎵 High · best", callback_data=f"pl:{cache_id}:ah"),
         InlineKeyboardButton("🎵 Medium · ≤160k", callback_data=f"pl:{cache_id}:am"),
         InlineKeyboardButton("🎵 Low · ≤70k", callback_data=f"pl:{cache_id}:al")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"pl:{cache_id}:menu"),
         InlineKeyboardButton("❌ Cancel", callback_data=f"pl:{cache_id}:cancel")],
    ]
    return InlineKeyboardMarkup(rows)


def _fmt_duration(seconds: int) -> str:
    if not seconds:
        return "?"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def build_playlist_explore_keyboard(cache_id: str, entries: list, page: int, page_size: int = 8) -> tuple[InlineKeyboardMarkup, int]:
    """Paginated per-video picker. Returns (keyboard, total_pages).

    Each video is a full-width button 'N. Title · m:ss'; tapping it drops into the
    normal single-video format flow for that entry. Navigation is page-based.
    """
    import math
    total = len(entries)
    total_pages = max(1, math.ceil(total / page_size))
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    chunk = entries[start:start + page_size]

    rows = []
    for offset, e in enumerate(chunk):
        abs_idx = start + offset
        title = (e.get("title") or "Untitled")
        if len(title) > 48:
            title = title[:47] + "…"
        dur = _fmt_duration(e.get("duration"))
        rows.append([InlineKeyboardButton(
            text=f"{abs_idx + 1}. {title} · {dur}",
            callback_data=f"plx:{cache_id}:{abs_idx}",
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"pln:{cache_id}:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="none"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"pln:{cache_id}:{page + 1}"))
    rows.append(nav)

    rows.append([
        InlineKeyboardButton("🔙 Back", callback_data=f"pl:{cache_id}:menu"),
        InlineKeyboardButton("⬇️ Whole playlist", callback_data=f"pl:{cache_id}:whole"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"pl:{cache_id}:cancel"),
    ])
    return InlineKeyboardMarkup(rows), total_pages


async def begin_playlist_flow(message: Message, url: str, custom_filename, user_id: int, pure: bool):
    """Read playlist metadata (flat) and post the DECISION keyboard.

    *pure* is True for ``/playlist?list=`` URLs (no single-video shortcut), False
    for ``watch?v=&list=`` URLs (a 'just this video' button is offered too).
    """
    status = RichStream(
        message.chat.id, message._client.send_message,
        reply_to_message_id=message.id,
    )
    await status.update(thinking="Reading playlist…")

    async def meta_job():
        try:
            loop = asyncio.get_event_loop()
            meta = await loop.run_in_executor(None, extract_playlist_meta, url)
        except Exception as e:
            await status.close()
            await send_reply_safe(
                message._client.send_message, message.id,
                chat_id=message.chat.id,
                text=f"❌ Could not read playlist.\nError: `{str(e)}`",
            )
            await log_event(f"❌ **Playlist read error:** `{url}`. Details: `{str(e)}`")
            return

        total = len(meta["entries"])
        cap = getattr(config, "PLAYLIST_MAX_VIDEOS", 50)
        cap_note = f"\n\n⚠️ Playlist has {total} videos; the first {cap} are listed/downloaded." if total > cap else ""

        cache_id = str(uuid.uuid4())[:8]
        DOWNLOAD_CACHE[cache_id] = {
            "type": "playlist",
            "url": url,
            "title": meta["title"],
            "entries": meta["entries"],
            "custom_filename": custom_filename,
            "origin_message_id": message.id,
        }

        keyboard = build_playlist_decision_keyboard(cache_id, include_single=not pure)
        await status.close()
        await send_reply_safe(
            message._client.send_message, message.id,
            chat_id=message.chat.id,
            text=(
                f"📋 **Playlist:** {meta['title']}\n📺 **Videos:** {total}{cap_note}\n\n"
                f"What do you want to do?"
            ),
            reply_markup=keyboard,
        )
        await log_event(f"📋 **Playlist ready:** `{meta['title']}` ({total} videos) for User `{user_id}`.")

    _spawn_fetch(meta_job())


def register_downloader_handlers(app: Client, premium_app: Client = None):

    # =========================================================================
    # Group 1: Link Downloader Handler (Only triggers if the text is a link)
    # =========================================================================
    @app.on_message(
        filters.text &
        filters.private &
        filters.create(lambda _, __, m: is_link(m.text.strip().split("|")[0].strip())),
        group=1
    )
    async def text_link_handler(client: Client, message: Message):
        text = message.text.strip()
        user_id = message.from_user.id

        parts = text.split("|", 1)
        url = parts[0].strip()
        custom_filename = parts[1].strip() if len(parts) > 1 else None

        if not is_authorized(user_id):
            return

        if is_social_media_link(url):
            # --- YouTube playlist branch (pre-fetch tier selection) ---
            # A pure /playlist URL goes straight to the tier keyboard; a
            # watch?v=&list= URL offers a "just this video" escape too.
            if is_playlist_url(url):
                await begin_playlist_flow(
                    message, url, custom_filename, user_id,
                    pure=is_pure_playlist_url(url)
                )
                return

            # --- Single-video flow ---
            status = RichStream(
                message.chat.id, message._client.send_message,
                reply_to_message_id=message.id,
            )
            await status.update(thinking="Analyzing link formats…")

            # Fetch formats immediately & concurrently — NOT queued. Only the
            # actual download (queued when the user taps a format button) is
            # serialized, so the user can pick formats for many links while
            # earlier downloads run.
            _spawn_fetch(show_format_selection(message, status, url, custom_filename, user_id))
        else:
            status_msg = await send_reply_safe(
                message._client.send_message, message.id,
                chat_id=message.chat.id, text="📥 Received URL. Queueing job...",
            )

            async def direct_upload_job():
                await status_msg.edit_text("⚡ Starting direct URL download...")
                cache_id = str(uuid.uuid4())[:8]
                task_dir = f"cache/{cache_id}"
                try:
                    async def dl_progress(cur, tot):
                        await progress_bar_handler(cur, tot, status_msg, "Downloading direct file to server...")

                    file_path = await download_direct_file(url, cache_id, dl_progress)

                    dir_name = os.path.dirname(file_path)
                    clean_name = custom_filename if custom_filename else os.path.basename(file_path)
                    clean_file_path = os.path.join(dir_name, clean_name)
                    if clean_file_path != file_path:
                        os.rename(file_path, clean_file_path)

                    await status_msg.edit_text("📤 Uploading direct file to Telegram...")

                    await process_split_and_upload(
                        bot_client=app,
                        premium_client=premium_app,
                        chat_id=message.chat.id,
                        file_path=clean_file_path,
                        action='d',
                        title=clean_name,
                        uploader="Direct Link",
                        duration=0,
                        thumb_path=None,
                        progress_msg=status_msg,
                        reply_to_message_id=message.id,
                        premium_allowed=is_premium_user(user_id),
                    )
                    await log_event(f"✅ **Direct Upload:** Finished for User `{user_id}` from source `{url}`.")
                except Exception as e:
                    await status_msg.edit_text(f"❌ Failed to process direct file URL.\nError: `{str(e)}`")
                    await log_event(f"❌ **Direct Upload Error:** Failed on `{url}`. Details: `{str(e)}`")
                finally:
                    if os.path.exists(task_dir):
                        try:
                            shutil.rmtree(task_dir)
                            print(f"[Cleanup] Cleared direct download directory: {task_dir}")
                        except Exception as ce:
                            print(f"[Cleanup] Error: {ce}")

            await queue.add_task(user_id, status_msg, direct_upload_job)

    # =========================================================================
    # Callback: single-video format selection (dl:...)
    # =========================================================================
    @app.on_callback_query(filters.regex(r"^dl:"))
    async def dl_callback_handler(client: Client, callback_query: CallbackQuery):
        data = callback_query.data
        user_id = callback_query.from_user.id

        parts = data.split(":")
        if len(parts) < 3:
            return

        cache_id = parts[1]
        action = parts[2]

        if action == "cancel":
            DOWNLOAD_CACHE.pop(cache_id, None)
            await callback_query.message.delete()
            await callback_query.answer("Cancelled.")
            return

        if action == "lock":
            await callback_query.answer(
                "🔒 This quality is over Telegram's 2GB bot upload limit. "
                "Ask the admin to enable 4GB Premium uploads for you.",
                show_alert=True,
            )
            return

        format_id = parts[3]
        cache_data = DOWNLOAD_CACHE.get(cache_id)
        if not cache_data:
            await callback_query.answer("⚠️ Session expired or not found.", show_alert=True)
            return

        target_list = cache_data["videos"] if action == 'v' else cache_data["audios"]
        target_fmt = next((f for f in target_list if f["format_id"] == format_id), None)

        if not premium_app or not is_premium_user(user_id):
            if target_fmt and target_fmt["bytes"] > (2000 * 1024 * 1024):
                await callback_query.answer("❌ This format exceeds Telegram's 2GB bot upload limit. Ask the admin to enable 4GB Premium uploads for you.", show_alert=True)
                return

        # Early disk check: fail fast with a clear message before enqueueing if
        # the VPS cannot hold the 2x-merge peak for this format's size.
        if target_fmt and target_fmt.get("bytes"):
            from utils.downloader import _ensure_disk_space, required_merge_headroom
            try:
                _ensure_disk_space(f"cache/{cache_id}", required_merge_headroom(target_fmt["bytes"]))
            except RuntimeError as disk_err:
                await callback_query.answer(f"❌ {disk_err}", show_alert=True)
                return

        await callback_query.message.edit_text("⏳ Request enqueued in Active Job Queue...")
        await callback_query.answer("Transfer enqueued...")

        task_dir = f"cache/{cache_id}"

        async def queued_transfer_job():
            await callback_query.message.edit_text("⚡️ Downloading file from server to VPS...")
            loop = asyncio.get_event_loop()
            try:
                from utils.downloader import download_media

                async def download_progress(curr, tot):
                    await progress_bar_handler(curr, tot, callback_query.message, "Downloading from server to VPS...")

                def thread_progress(curr, tot):
                    asyncio.run_coroutine_threadsafe(download_progress(curr, tot), loop)

                result = await loop.run_in_executor(
                    None, download_media, cache_data["url"], format_id, action, cache_id, thread_progress,
                    None, (target_fmt.get("height") if action == "v" and target_fmt else None),
                    cache_data.get("best_audio_format_id") if action == "v" else None,
                    bool(target_fmt.get("muxed")) if action == "v" and target_fmt else False,
                    (target_fmt.get("bytes") if target_fmt else None)
                )

                file_path = result['file_path']
                thumb_path = result['thumb_path']
                title = result['title']
                uploader = result['uploader']

                dir_name = os.path.dirname(file_path)
                ext = os.path.splitext(file_path)[1]

                custom_name = cache_data.get("custom_filename")
                if custom_name:
                    clean_name = custom_name if custom_name.endswith(ext) else f"{custom_name}{ext}"
                else:
                    clean_name = os.path.basename(file_path)

                clean_file_path = os.path.join(dir_name, clean_name)
                if clean_file_path != file_path:
                    os.rename(file_path, clean_file_path)

                await process_split_and_upload(
                    bot_client=app,
                    premium_client=premium_app,
                    chat_id=callback_query.message.chat.id,
                    file_path=clean_file_path,
                    action=action,
                    title=clean_name,
                    uploader=uploader,
                    duration=result['duration'],
                    thumb_path=thumb_path,
                    progress_msg=callback_query.message,
                    reply_to_message_id=cache_data.get("origin_message_id"),
                    premium_allowed=is_premium_user(user_id),
                )

                DOWNLOAD_CACHE.pop(cache_id, None)
                await log_event(f"✅ **Job Successful:** `{clean_name}` was successfully processed and sent.")

            except Exception as e:
                await callback_query.message.edit_text(f"❌ Download/Upload failure.\nError: `{str(e)}`")
                await log_event(f"❌ **Job Failure:** Extraction/Upload crashed on `{cache_data['url']}`. Details: `{str(e)}`")
            finally:
                if os.path.exists(task_dir):
                    try:
                        shutil.rmtree(task_dir)
                        print(f"[Cleanup] Cleaned active task directory: {task_dir}")
                    except Exception as ce:
                        print(f"[Cleanup] Error: {ce}")

        await queue.add_task(user_id, callback_query.message, queued_transfer_job)

    # =========================================================================
    # Callback: playlist tier selection (pl:...)
    # =========================================================================
    @app.on_callback_query(filters.regex(r"^pl:"))
    async def playlist_callback_handler(client: Client, callback_query: CallbackQuery):
        data = callback_query.data
        user_id = callback_query.from_user.id

        parts = data.split(":")
        if len(parts) < 3:
            return

        cache_id = parts[1]
        action = parts[2]
        cache_data = DOWNLOAD_CACHE.get(cache_id)

        if not cache_data or cache_data.get("type") != "playlist":
            await callback_query.answer("⚠️ Session expired or not found.", show_alert=True)
            return

        if action == "cancel":
            DOWNLOAD_CACHE.pop(cache_id, None)
            await callback_query.message.delete()
            await callback_query.answer("Cancelled.")
            return

        # Escape hatch for watch?v=&list= URLs: drop into single-video flow.
        if action == "single":
            await callback_query.answer()
            url = cache_data.get("url")
            custom_filename = cache_data.get("custom_filename")
            origin_id = cache_data.get("origin_message_id")
            DOWNLOAD_CACHE.pop(cache_id, None)
            await callback_query.message.delete()
            status = RichStream(
                callback_query.message.chat.id, callback_query._client.send_message,
                reply_to_message_id=origin_id,
            )
            await status.update("🔍 Fetching format attributes...")

            _spawn_fetch(show_format_selection(
                callback_query.message, status, url, custom_filename, user_id,
                origin_message_id=origin_id,
            ))
            return

        # Back to the top-level decision menu.
        if action == "menu":
            include_single = not is_pure_playlist_url(cache_data.get("url") or "")
            await callback_query.answer()
            await callback_query.message.edit_text(
                f"📋 **Playlist:** {cache_data['title']}\n"
                f"📺 **Videos:** {len(cache_data['entries'])}\n\n"
                f"What do you want to do?",
                reply_markup=build_playlist_decision_keyboard(cache_id, include_single),
            )
            return

        # "Download whole playlist" → quality-tier selector.
        if action == "whole":
            await callback_query.answer()
            await callback_query.message.edit_text(
                f"📋 **Playlist:** {cache_data['title']}\n\n"
                f"Pick a quality tier — it applies to every video:",
                reply_markup=build_playlist_tier_keyboard(cache_id),
            )
            return

        # "Explore videos" → first page of the per-video picker.
        if action == "explore":
            await callback_query.answer()
            keyboard, total_pages = build_playlist_explore_keyboard(cache_id, cache_data["entries"], 0)
            await callback_query.message.edit_text(
                f"🔎 **{cache_data['title']}**\n"
                f"Pick the videos to download (page 1/{total_pages}):",
                reply_markup=keyboard,
            )
            return

        if action not in ("vh", "vm", "vl", "ah", "am", "al"):
            return

        fmt_type = "v" if action[0] == "v" else "a"
        tier = {"h": "high", "m": "medium", "l": "low"}[action[1]]
        selector, _label = PLAYLIST_TIERS[(fmt_type, tier)]

        cap = getattr(config, "PLAYLIST_MAX_VIDEOS", 50)
        entries = cache_data["entries"][:cap]
        total = len(entries)
        playlist_title = cache_data["title"]

        await callback_query.message.edit_text("⏳ Playlist enqueued in Active Job Queue...")
        await callback_query.answer("Playlist download started...")

        progress_msg = callback_query.message
        chat_id = callback_query.message.chat.id
        origin_id = cache_data.get("origin_message_id")

        async def playlist_job():
            success = 0
            skipped = 0
            for idx, entry in enumerate(entries, 1):
                entry_url = entry["url"]
                entry_title = entry["title"]
                sub_cache_id = f"{cache_id}-{idx}"
                task_dir = f"cache/{sub_cache_id}"
                try:
                    await progress_msg.edit_text(
                        f"📋 **{playlist_title}**\n"
                        f"▶️ Video `{idx}/{total}`\n"
                        f"📝 `{entry_title}`\n\n"
                        f"⏳ Downloading..."
                    )
                    loop = asyncio.get_event_loop()

                    async def download_progress(curr, tot):
                        await progress_bar_handler(curr, tot, progress_msg, f"Downloading {idx}/{total}: {entry_title}")

                    def thread_progress(curr, tot):
                        asyncio.run_coroutine_threadsafe(download_progress(curr, tot), loop)

                    # download_media(url, format_id=None, format_type, cache_id, progress_fn, format_selector)
                    result = await loop.run_in_executor(
                        None, download_media, entry_url, None, fmt_type, sub_cache_id, thread_progress, selector
                    )

                    file_path = result["file_path"]
                    dir_name = os.path.dirname(file_path)
                    ext = os.path.splitext(file_path)[1]

                    custom_name = cache_data.get("custom_filename")
                    if custom_name:
                        clean_name = custom_name if custom_name.endswith(ext) else f"{custom_name} {idx}{ext}"
                    else:
                        clean_name = os.path.basename(file_path)
                    clean_file_path = os.path.join(dir_name, clean_name)
                    if clean_file_path != file_path:
                        os.rename(file_path, clean_file_path)

                    await progress_msg.edit_text(f"📤 Uploading video {idx}/{total}...")
                    await process_split_and_upload(
                        bot_client=app,
                        premium_client=premium_app,
                        chat_id=chat_id,
                        file_path=clean_file_path,
                        action=fmt_type,
                        title=result["title"],
                        uploader=result["uploader"],
                        duration=result["duration"],
                        thumb_path=result["thumb_path"],
                        progress_msg=progress_msg,
                        delete_progress_after=False,  # keep the rolling message across videos
                        reply_to_message_id=origin_id,
                        premium_allowed=is_premium_user(user_id),
                    )
                    success += 1
                    await log_event(f"✅ **Playlist item {idx}/{total}:** `{result['title']}` sent.")
                except Exception as e:
                    skipped += 1
                    try:
                        await send_reply_safe(
                            callback_query._client.send_message, origin_id,
                            chat_id=chat_id,
                            text=f"⚠️ Skipped video {idx}/{total} `{entry_title}`\nError: `{str(e)}`",
                        )
                    except Exception:
                        pass
                    await log_event(f"⚠️ **Playlist skip {idx}/{total}:** `{entry_title}`. Details: `{str(e)}`")
                finally:
                    if os.path.exists(task_dir):
                        try:
                            shutil.rmtree(task_dir)
                        except Exception as ce:
                            print(f"[Cleanup] Error: {ce}")

            DOWNLOAD_CACHE.pop(cache_id, None)
            summary = f"✅ **Playlist complete:** `{playlist_title}`\n📤 Sent `{success}/{total}` videos."
            if skipped:
                summary += f"\n⚠️ `{skipped}` video(s) skipped — see messages above."
            try:
                await progress_msg.edit_text(summary)
            except Exception:
                pass

        await queue.add_task(user_id, callback_query.message, playlist_job)

    # =========================================================================
    # Callback: playlist explorer pagination (pln:...)
    # =========================================================================
    @app.on_callback_query(filters.regex(r"^pln:"))
    async def playlist_explore_nav_handler(client: Client, callback_query: CallbackQuery):
        parts = callback_query.data.split(":")
        if len(parts) < 3:
            return
        cache_id = parts[1]
        try:
            page = int(parts[2])
        except ValueError:
            return
        cache_data = DOWNLOAD_CACHE.get(cache_id)
        if not cache_data or cache_data.get("type") != "playlist":
            await callback_query.answer("⚠️ Session expired or not found.", show_alert=True)
            return
        keyboard, total_pages = build_playlist_explore_keyboard(cache_id, cache_data["entries"], page)
        shown_page = min(max(page, 0), total_pages - 1) + 1
        await callback_query.answer()
        await callback_query.message.edit_text(
            f"🔎 **{cache_data['title']}**\n"
            f"Pick the videos to download (page {shown_page}/{total_pages}):",
            reply_markup=keyboard,
        )

    # =========================================================================
    # Callback: pick one video from the explorer → single-video format flow (plx:...)
    # =========================================================================
    @app.on_callback_query(filters.regex(r"^plx:"))
    async def playlist_explore_pick_handler(client: Client, callback_query: CallbackQuery):
        user_id = callback_query.from_user.id
        parts = callback_query.data.split(":")
        if len(parts) < 3:
            return
        cache_id = parts[1]
        try:
            idx = int(parts[2])
        except ValueError:
            return
        cache_data = DOWNLOAD_CACHE.get(cache_id)
        if not cache_data or cache_data.get("type") != "playlist":
            await callback_query.answer("⚠️ Session expired or not found.", show_alert=True)
            return
        entries = cache_data["entries"]
        if idx < 0 or idx >= len(entries):
            await callback_query.answer("⚠️ That video is no longer listed.", show_alert=True)
            return

        entry = entries[idx]
        origin_id = cache_data.get("origin_message_id")
        await callback_query.answer()
        status = RichStream(
            callback_query.message.chat.id, callback_query._client.send_message,
            reply_to_message_id=origin_id,
        )
        await status.update(f"🔍 Fetching formats for `{entry['title']}`...")

        _spawn_fetch(show_format_selection(
            callback_query.message, status, entry["url"],
            cache_data.get("custom_filename"), user_id,
            origin_message_id=origin_id,
        ))


async def download_direct_file(url: str, cache_id: str, progress_fn) -> str:
    """Download direct file URL stream to secure subfolder."""
    task_dir = f"cache/{cache_id}"
    os.makedirs(task_dir, exist_ok=True)

    parsed_url = urllib.parse.urlparse(url)
    file_name = os.path.basename(parsed_url.path) or f"download_{cache_id}"
    file_name = urllib.parse.unquote(file_name)
    out_path = f"{task_dir}/{file_name}"

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

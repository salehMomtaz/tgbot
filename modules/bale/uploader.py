# modules/bale/uploader.py — Bale-side direct upload (20 MB hard limit)
# Ported from balebot operators/uploader.py, stripped to essentials and pointed
# at the shared core (probe_video_dimensions, splitters) + Bale's 20 MB ceiling.
import os
import re
import asyncio
import aiohttp
import config
from utils.gate import is_document_mode

BALE_API_BASE = "https://tapi.bale.ai"

def sanitize_filename_for_bale(filename: str) -> str:
    base, ext = os.path.splitext(filename)
    clean_base = re.sub(r'[\\/:*?"<>|\[\]()\'\s]+', '_', base)
    if len(clean_base) > 40:
        clean_base = clean_base[:40].strip("_")
    clean_base = re.sub(r'_+', '_', clean_base).strip("_")
    if not clean_base:
        clean_base = "file"
    return f"{clean_base}{ext}"

def clean_caption_text(text: str, max_len: int = 150) -> str:
    """Bale markdown is fragile: always strip formatting, truncate."""
    if not text:
        return "Media File"
    cleaned = text.replace("*", "").replace("_", "").replace("`", "").replace("[", "").replace("]", "").replace("(", "").replace(")", "")
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].strip() + "..."
    return cleaned

async def upload_file_direct_to_bale(method: str, chat_id: int, file_path: str, caption: str = "", extra_params: dict = None, thumb_path: str = None, filename: str = None) -> dict:
    if not getattr(config, "BALE_TOKEN", ""):
        raise RuntimeError("BALE_TOKEN not configured")
    url = f"{BALE_API_BASE}/bot{config.BALE_TOKEN}/{method}"
    field = "document"
    if method == "sendVideo":
        field = "video"
    elif method == "sendAudio":
        field = "audio"
    display_name = filename if filename else os.path.basename(file_path)
    safe = sanitize_filename_for_bale(display_name)
    timeout = aiohttp.ClientTimeout(total=600, connect=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        thumb_file = None
        try:
            proxy = getattr(config, "AIOHTTP_PROXY", None)
            with open(file_path, "rb") as f:
                form = aiohttp.FormData()
                form.add_field("chat_id", str(chat_id))
                if caption:
                    form.add_field("caption", caption)
                if extra_params:
                    for k, v in extra_params.items():
                        if v is not None:
                            form.add_field(k, str(v))
                form.add_field(field, f, filename=safe)
                if thumb_path and os.path.isfile(thumb_path):
                    thumb_file = open(thumb_path, "rb")
                    form.add_field("thumbnail", thumb_file, filename="thumb.jpg")
                async with session.post(url, data=form, proxy=proxy) as resp:
                    j = await resp.json()
                    if not j.get("ok"):
                        raise RuntimeError(f"Bale API Error: {j.get('description', 'Unknown')}")
                    return j
        finally:
            if thumb_file:
                thumb_file.close()

async def send_single_media_bale(bot, chat_id: int, file_path: str, action: str, title: str, uploader: str, duration: int, thumb_path: str, force_document=False):
    safe_title = clean_caption_text(title)
    if force_document or action == 'd':
        return await upload_file_direct_to_bale("sendDocument", chat_id, file_path, caption=f"Part: {os.path.basename(file_path)}", thumb_path=thumb_path)
    if action == 'a':
        return await upload_file_direct_to_bale("sendAudio", chat_id, file_path, caption=f"{safe_title}", thumb_path=thumb_path,
            extra_params={"title": safe_title, "performer": clean_caption_text(uploader, 50), "duration": int(duration)})
    else:
        from utils.downloader import probe_video_dimensions
        w, h, pd = probe_video_dimensions(file_path)
        fd = pd if pd > 0 else int(duration)
        return await upload_file_direct_to_bale("sendVideo", chat_id, file_path, caption=f"{safe_title}", thumb_path=thumb_path,
            extra_params={"width": w, "height": h, "duration": fd, "supports_streaming": "true"})

async def process_split_and_upload_bale(bot, chat_id: int, file_path: str, action: str, title: str, uploader: str, duration: int, thumb_path: str, progress_msg):
    """Bale 20 MB sequential uploader: one chunk at a time, ffmpeg -c copy for media."""
    from utils.downloader import split_file_generator, split_video_by_size_generator
    from main import progress_bar_handler  # not used for Bale progress, kept for parity

    size = os.path.getsize(file_path)
    # Bale real limit is 20 MB; keep 19 MB target / 20 MB hard.
    target = getattr(config, "BALE_SPLIT_TARGET_MB", 19) * 1024 * 1024
    hard = getattr(config, "BALE_HARD_LIMIT_MB", 20) * 1024 * 1024

    # also respect admin-adjustable binary chunk if larger? no, Bale hard wins
    is_split = size > target
    force_doc = is_document_mode(chat_id) or action == 'd'

    # For Bale, force document if split and not video/audio? Keep same as Telegram logic
    # but hard ceiling is Bale's 20 MB regardless.
    is_media = action in ('v', 'a')
    try:
        part_num = 1
        loop = asyncio.get_event_loop()
        if is_media:
            gen = split_video_by_size_generator(file_path, target, hard)
        else:
            gen = split_file_generator(file_path, target, hard)
        while True:
            def nxt():
                try:
                    return next(gen)
                except StopIteration:
                    return None
            part = await loop.run_in_executor(None, nxt)
            if not part:
                break
            if progress_msg is not None:
                try:
                    await progress_msg.edit_text(f"📤 Uploading part {part_num} to Bale...")
                except Exception:
                    pass
            await send_single_media_bale(bot, chat_id, part, action, title if not is_split else f"{title} (Part {part_num})", uploader, duration, thumb_path if part_num == 1 else None, force_document=force_doc or is_split)
            if part != file_path and os.path.exists(part):
                try:
                    os.remove(part)
                except:
                    pass
            part_num += 1
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except:
                pass
        if progress_msg is not None:
            try:
                await progress_msg.delete()
            except:
                pass
    except Exception:
        raise

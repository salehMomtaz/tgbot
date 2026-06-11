# utils/uploader_handler.py
import os
import asyncio
from pyrogram import Client
from utils.gate import is_document_mode

async def send_single_media(bot_client: Client, premium_client: Client, chat_id: int, file_path: str, action: str, title: str, uploader: str, duration: int, thumb_path: str, progress_fn, force_document=False):
    """Sends a single media file using the designated client (standard bot or premium userbot)."""
    from utils.downloader import probe_video_dimensions
    
    # Select client based on file limits
    file_size = os.path.getsize(file_path)
    use_premium = bool(premium_client and file_size > (2000 * 1024 * 1024))
    client = premium_client if use_premium else bot_client
    
    if force_document:
        return await client.send_document(
            chat_id=chat_id,
            document=file_path,
            caption=f"📁 **Part:** `{os.path.basename(file_path)}`",
            progress=progress_fn
        )
        
    if action == 'a':
        return await client.send_audio(
            chat_id=chat_id,
            audio=file_path,
            title=title,
            performer=uploader,
            duration=int(duration),
            thumb=thumb_path,
            caption=f"🎵 **{title}**\nUploaded via Downloader Bot",
            progress=progress_fn
        )
    else:  # action == 'v' or default video
        width, height, parsed_duration = probe_video_dimensions(file_path)
        final_duration = parsed_duration if parsed_duration > 0 else int(duration)
        return await client.send_video(
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

async def process_split_and_upload(bot_client: Client, premium_client: Client, chat_id: int, file_path: str, action: str, title: str, uploader: str, duration: int, thumb_path: str, progress_msg):
    """
    On-Demand Sequential Uploader:
    Generates chunks one-by-one, uploads them, and immediately purges them from disk.
    Caps VPS disk overhead to exactly ONE chunk size.
    """
    from utils.downloader import split_file_generator
    from main import progress_bar_handler # Imported dynamically to prevent circular dependencies
    
    file_size = os.path.getsize(file_path)
    use_premium = bool(premium_client and file_size > (2000 * 1024 * 1024))
    
    # Define chunk boundaries: 1.95 GB for standard Bot, 3.9 GB for Premium Userbot
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
                bot_client=bot_client,
                premium_client=premium_client,
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
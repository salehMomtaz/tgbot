# utils/uploader_handler.py
import os
import asyncio
import config
from pyrogram import Client
from pyrogram.errors import MessageIdInvalid, RPCError
from utils.gate import is_document_mode, is_premium_user

# Telegram upload ceilings (bytes).
#   Bot API:  2 GB  (2 * 1024^3 = 2147483648)
#   Premium:  4 GB  (4 * 1024^3 = 4294967296)
# We aim a little below the real limit (target) and enforce a hard ceiling on
# the segment produced by the ffmpeg keyframe splitter, which can overshoot.
_PREMIUM_TARGET = 3900 * 1024 * 1024     # ~3.8 GB target per segment (Premium userbot)
_PREMIUM_HARD = 4000 * 1024 * 1024       # hard ceiling under Telegram's 4 GB
_BOT_TARGET = 1900 * 1024 * 1024         # ~1.86 GB target per segment (Bot API)
_BOT_HARD = 2000 * 1024 * 1024           # hard ceiling under Telegram's 2 GB


async def send_reply_safe(send_fn, reply_to_message_id: int | None = None, **kwargs):
    """Send via *send_fn*, quoting *reply_to_message_id* when given.

    The quoted message may have been deleted by the user in the meantime —
    Telegram then rejects the send with an RPC error (MESSAGE_ID_INVALID /
    "message to be replied not found"). On that specific failure we retry
    once WITHOUT the quote instead of failing the upload. Any other error
    class propagates unchanged.
    """
    if reply_to_message_id is not None:
        try:
            return await send_fn(reply_to_message_id=reply_to_message_id, **kwargs)
        except MessageIdInvalid:
            pass  # known bad reply target — fall through to unquoted send
        except RPCError as e:
            text = str(e).upper()
            if "MESSAGE_ID_INVALID" not in text and "REPLIED" not in text and "TO_REPLY" not in text:
                raise
    return await send_fn(**kwargs)


async def send_single_media(bot_client: Client, premium_client: Client, chat_id: int, file_path: str, action: str, title: str, uploader: str, duration: int, thumb_path: str, progress_fn, force_document=False, caption: str | None = None, reply_to_message_id: int | None = None, premium_allowed: bool | None = None):
    """Sends a single media file using the designated client, passing thumbs to document uploads too.

    When *reply_to_message_id* is set (the user's original link message), the
    media message quote-replies to it so chat context stays attached to the
    request that produced the file.

    *premium_allowed* decides whether the 4 GB Premium upload path may be used
    for this recipient. ``None`` (default) infers it from the recipient's own
    Premium whitelist status (*chat_id* in a private chat == the user id).

    Premium (>2 GB) uploads cannot be sent by the bot itself — bots are hard
    capped at 2 GB (only a Premium user account can upload 4 GB). They are
    therefore staged into the log channel by the Premium userbot, then the bot
    *copies* that message into the recipient chat (``copy_message`` sends by
    ``file_id``, which has no size limit since the file already lives on
    Telegram's CDN). This keeps the sender as the bot and lets the copy
    quote-reply to the user's link message.
    """
    from utils.downloader import probe_video_dimensions

    if premium_allowed is None:
        premium_allowed = is_premium_user(chat_id)

    file_size = os.path.getsize(file_path)
    use_premium = bool(premium_client and premium_allowed and file_size > (2000 * 1024 * 1024))

    if use_premium and config.LOG_CHANNEL_ID:
        try:
            return await _stage_and_relay(
                bot_client, premium_client, chat_id, file_path, action, title,
                uploader, duration, thumb_path, progress_fn,
                force_document, caption, reply_to_message_id,
            )
        except Exception as e:
            # Log channel staging is best-effort: fall back to a direct premium
            # send so the file still reaches the recipient on config drift.
            import logging
            logging.getLogger(__name__).warning(
                f"Log-channel staging failed, falling back to direct premium send: {e}"
            )

    client = premium_client if use_premium else bot_client

    if force_document:
        return await send_reply_safe(
            client.send_document,
            reply_to_message_id,
            chat_id=chat_id,
            document=file_path,
            caption=caption or f"📁 **Part:** `{os.path.basename(file_path)}`",
            thumb=thumb_path if (thumb_path and os.path.exists(thumb_path)) else None,  # Visual preview for doc mode!
            progress=progress_fn
        )

    if action == 'a':
        return await send_reply_safe(
            client.send_audio,
            reply_to_message_id,
            chat_id=chat_id,
            audio=file_path,
            title=title,
            performer=uploader,
            duration=int(duration),
            thumb=thumb_path,
            caption=caption or f"🎵 **{title}**\nUploaded via Downloader Bot",
            progress=progress_fn
        )
    else:  # action == 'v'
        if not (thumb_path and os.path.exists(thumb_path)):
            # No usable platform cover — extract a frame so every video upload
            # carries a thumbnail (Telegram auto-generates only <10MB videos;
            # larger files need the sender to provide one or they arrive blank).
            from utils.downloader import extract_video_frame_thumb
            thumb_path = extract_video_frame_thumb(file_path)
        width, height, parsed_duration = probe_video_dimensions(file_path)
        final_duration = parsed_duration if parsed_duration > 0 else int(duration)
        return await send_reply_safe(
            client.send_video,
            reply_to_message_id,
            chat_id=chat_id,
            video=file_path,
            width=width,
            height=height,
            duration=final_duration,
            thumb=thumb_path,
            supports_streaming=True,
            caption=caption or f"🎥 **{title}**\nUploaded via Downloader Bot",
            progress=progress_fn
        )


async def _stage_and_relay(bot_client: Client, premium_client: Client, chat_id: int, file_path: str, action: str, title: str, uploader: str, duration: int, thumb_path: str, progress_fn, force_document: bool, caption: str | None, reply_to_message_id: int | None):
    """Premium (>2 GB) upload: stage into the log channel, then bot copies to chat.

    The Premium userbot uploads the raw file to ``LOG_CHANNEL_ID`` (only a
    Premium account can push >2 GB), then the bot calls ``copy_message`` to
    relay it to *chat_id* — copying uses the existing ``file_id`` so there is no
    size limit, the sender shows as the bot, and the copy can quote-reply to the
    user's link message. The staged message stays in the log channel as a record.
    """
    from utils.downloader import probe_video_dimensions

    stage_caption = (
        f"📦 **Staged for delivery to `{chat_id}`**\n"
        f"{'📄' if force_document else ('🎵' if action == 'a' else '🎥')} **{title}**\n"
        f"`{os.path.basename(file_path)}`"
    )

    if force_document:
        staged = await send_reply_safe(
            premium_client.send_document,
            None,
            chat_id=config.LOG_CHANNEL_ID,
            document=file_path,
            caption=stage_caption,
            thumb=thumb_path if (thumb_path and os.path.exists(thumb_path)) else None,
            progress=progress_fn
        )
    elif action == 'a':
        staged = await send_reply_safe(
            premium_client.send_audio,
            None,
            chat_id=config.LOG_CHANNEL_ID,
            audio=file_path,
            title=title,
            performer=uploader,
            duration=int(duration),
            thumb=thumb_path,
            caption=stage_caption,
            progress=progress_fn
        )
    else:  # action == 'v'
        if not (thumb_path and os.path.exists(thumb_path)):
            from utils.downloader import extract_video_frame_thumb
            thumb_path = extract_video_frame_thumb(file_path)
        width, height, parsed_duration = probe_video_dimensions(file_path)
        final_duration = parsed_duration if parsed_duration > 0 else int(duration)
        staged = await send_reply_safe(
            premium_client.send_video,
            None,
            chat_id=config.LOG_CHANNEL_ID,
            video=file_path,
            width=width,
            height=height,
            duration=final_duration,
            thumb=thumb_path,
            supports_streaming=True,
            caption=stage_caption,
            progress=progress_fn
        )

    user_caption = caption or (
        f"📁 **{title}**" if force_document
        else f"🎵 **{title}**\nUploaded via Downloader Bot" if action == 'a'
        else f"🎥 **{title}**\nUploaded via Downloader Bot"
    )

    return await send_reply_safe(
        bot_client.copy_message,
        reply_to_message_id,
        chat_id=chat_id,
        from_chat_id=config.LOG_CHANNEL_ID,
        message_id=staged.id,
        caption=user_caption,
    )


async def process_split_and_upload(bot_client: Client, premium_client: Client, chat_id: int, file_path: str, action: str, title: str, uploader: str, duration: int, thumb_path: str, progress_msg, delete_progress_after: bool = True, caption: str | None = None, reply_to_message_id: int | None = None, premium_allowed: bool | None = None):
    """On-Demand Sequential Uploader.

    Splits the file only if it exceeds the active Telegram ceiling, then streams
    one part at a time and deletes it immediately after upload so at most ONE
    extra part ever lives on disk.

    When *delete_progress_after* is False the status message is left intact
    (used by the playlist loop, which reuses one rolling message across videos).

    Every uploaded part quote-replies to *reply_to_message_id* (the user's
    original link message) when provided; DM-relay callers pass None because
    there is no originating Telegram message to quote.
    """
    from utils.downloader import split_file_generator, split_video_by_size_generator
    from main import progress_bar_handler

    if premium_allowed is None:
        premium_allowed = is_premium_user(chat_id)

    file_size = os.path.getsize(file_path)
    use_premium = bool(premium_client and premium_allowed and file_size > (2000 * 1024 * 1024))

    if use_premium:
        target_bytes = _PREMIUM_TARGET
        hard_bytes = _PREMIUM_HARD
    else:
        target_bytes = _BOT_TARGET
        hard_bytes = _BOT_HARD

    is_split = file_size > target_bytes
    force_document = is_document_mode(chat_id)

    # Video/audio containers keep their keyframes with ffmpeg -c copy so every
    # part stays independently playable; documents are binary-chunked instead.
    is_media = action in ('v', 'a')

    if action == 'v' and not (thumb_path and os.path.exists(thumb_path)):
        # Resolve a real thumbnail ONCE (platform cover → frame extraction),
        # reused across split parts instead of re-extracting per part.
        from utils.downloader import extract_video_frame_thumb
        thumb_path = extract_video_frame_thumb(file_path)

    parts_list = []

    try:
        part_num = 1
        loop = asyncio.get_event_loop()

        if is_media:
            generator = split_video_by_size_generator(file_path, target_bytes, hard_bytes)
        else:
            generator = split_file_generator(file_path, target_bytes, hard_bytes)

        while True:
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

            if progress_msg is not None:
                await progress_msg.edit_text(f"📤 Uploading part {part_num}...")

            await send_single_media(
                bot_client=bot_client,
                premium_client=premium_client,
                chat_id=chat_id,
                file_path=part_path,
                action=action,
                title=title if not is_split else f"{title} (Part {part_num})",
                uploader=uploader,
                duration=duration,
                thumb_path=(thumb_path if part_num == 1 else None),
                progress_fn=upload_progress,
                force_document=force_document or is_split,
                caption=caption if part_num == 1 else None,
                reply_to_message_id=reply_to_message_id,
                premium_allowed=premium_allowed,
            )

            if part_path != file_path:
                if os.path.exists(part_path):
                    os.remove(part_path)

            part_num += 1

        if os.path.exists(file_path):
            os.remove(file_path)

        if delete_progress_after:
            if progress_msg is not None:
                await progress_msg.delete()

    except Exception as e:
        for p in parts_list:
            if p != file_path and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
        raise e

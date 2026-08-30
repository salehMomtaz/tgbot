"""
Shared constants, delivery helpers, and utility functions for direct-forward.

Mirrors the original modules/direct_forward.py delivery helpers section exactly.
"""

import asyncio
import logging
import os
import random
import re
import shutil

import config

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"']+")
# xma share targets can point at reels/posts (/reel/, /p/, /tv/) OR stories
# (/stories/<user>/<media_id>/). yt-dlp handles all of them (stories via the
# cookie jar); the important part is we never fail to SEE the link.
IG_POST_RE = re.compile(r"(instagram\.com/(?:reel|reels|p|tv|stories)/[^\s?]+)")

TELEGRAM_CAPTION_LIMIT = 1024
TELEGRAM_TEXT_LIMIT = 4096


def _poll_interval() -> float:
    """Effective delay between inbox sweeps: base ± jitter.

    Instagram flags machine-perfect fixed-cadence polling far more easily than
    a humanized, jittered several-minute rhythm (instagrapi best-practices:
    read-only monitoring should poll "several minutes rather than seconds").
    """
    base = max(60, config.DIRECT_FORWARD_POLL_SECONDS)
    jitter_pct = max(0, min(90, getattr(config, "DIRECT_FORWARD_POLL_JITTER_PCT", 40)))
    if jitter_pct:
        base *= random.uniform(1 - jitter_pct / 100, 1 + jitter_pct / 100)
    return base


def _tt_poll_interval() -> float:
    """Reconnect cadence for the TikTok IM WebSocket, honoring the TikTok
    dedicated knobs (TIKTOK_DIRECT_POLL_SECONDS / TIKTOK_DIRECT_POLL_JITTER_PCT).
    Kept separate from _poll_interval so Instagram/X and TikTok can be tuned
    independently; both stay humanized with the same ±jitter discipline."""
    base = max(60, getattr(config, "TIKTOK_DIRECT_POLL_SECONDS", config.DIRECT_FORWARD_POLL_SECONDS))
    jitter_pct = max(0, min(90, getattr(config, "TIKTOK_DIRECT_POLL_JITTER_PCT", 40)))
    if jitter_pct:
        base *= random.uniform(1 - jitter_pct / 100, 1 + jitter_pct / 100)
    return base


def _activity_stamp(thread) -> str:
    """Serialize a thread's last_activity_at for the state watermark map."""
    last_act = getattr(thread, "last_activity_at", None)
    try:
        return last_act.isoformat() if last_act else ""
    except Exception:
        return ""


class _NullStatusMessage:
    """Stand-in for a Pyrogram status message: the download queue wants
    something it can edit, background relays have nothing to edit."""
    async def edit_text(self, _text: str) -> None:
        return


def _chunk_text(text: str, limit: int) -> list[str]:
    """Split *text* into <= limit-char chunks on paragraph/word boundaries."""
    if not text:
        return []
    chunks = []
    rest = text
    while rest:
        if len(rest) <= limit:
            chunks.append(rest)
            break
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip()
    return chunks


def _compose_caption(header_lines: list[str], body: str) -> tuple[str, list[str]]:
    """Return (media_caption <= 1024, followup_texts). Telegram media captions
    cap at 1024 chars; the overflow is sent as normal text messages (<= 4096)."""
    header = "\n".join(line for line in header_lines if line)
    if not body:
        return header, []
    full = f"{header}\n\n{body}" if header else body
    if len(full) <= TELEGRAM_CAPTION_LIMIT:
        return full, []
    budget = TELEGRAM_CAPTION_LIMIT - len(header) - 4  # "\n\n" + "…"
    first_body = body[:max(0, budget)] + "…"
    remainder = body[max(0, budget):]
    followups = _chunk_text(remainder, TELEGRAM_TEXT_LIMIT)
    return f"{header}\n\n{first_body}", followups


async def _send_followups(bot_client, chat_id: int, followups: list[str]) -> None:
    for chunk in followups:
        try:
            await bot_client.send_message(chat_id=chat_id, text=chunk)
        except Exception as e:
            logger.warning(f"[DirectForward] follow-up message failed: {e}")


async def _download_and_deliver(bot_client, premium_client, chat_id: int, url: str,
                                header_lines: list[str], body: str,
                                preview_url: str | None = None) -> None:
    """Download *url* via the normal yt-dlp pipeline (cookie jars included)
    and deliver it with the info header + body (split over follow-ups).
    Runs on the shared single-worker queue behind interactive downloads.

    When yt-dlp hard-fails and a *preview_url* (from the DM's share payload)
    exists, the preview image is delivered as a fallback so the user still
    sees what the share was, with a note that the full media failed."""
    from utils.downloader import download_media, probe_video_dimensions
    from utils.uploader_handler import process_split_and_upload
    import hashlib

    cache_id = f"df_{hashlib.md5(url.encode()).hexdigest()[:10]}"
    caption, followups = _compose_caption(header_lines, body)
    try:
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None, download_media, url, None, "v", cache_id, None, None, None, None,
            )
        except Exception as dl_error:
            if not preview_url:
                raise
            logger.warning(f"[DirectForward] yt-dlp failed for {url}: {dl_error} — delivering preview image")
            data = await loop.run_in_executor(None, _fetch_bytes, preview_url, "https://www.instagram.com/")
            path = f"cache/{cache_id}/preview.jpg"
            os.makedirs(f"cache/{cache_id}", exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
            await bot_client.send_photo(chat_id=chat_id, photo=path,
                                        caption=f"⚠️ (full media failed to download)\n{caption}")
            await _send_followups(bot_client, chat_id, followups)
            return

        file_path = result["file_path"]
        width, height, _dur = probe_video_dimensions(file_path)
        is_photo = (width == 320 and height == 320) or file_path.lower().endswith(
            (".jpg", ".jpeg", ".png", ".webp"))

        if is_photo:
            await bot_client.send_photo(chat_id=chat_id, photo=file_path, caption=caption)
        else:
            await process_split_and_upload(
                bot_client=bot_client,
                premium_client=premium_client,
                chat_id=chat_id,
                file_path=file_path,
                action="v",
                title=result.get("title", "Media"),
                uploader=result.get("uploader", "Unknown"),
                duration=result.get("duration", 0),
                thumb_path=result.get("thumb_path"),
                progress_msg=None,
                delete_progress_after=True,
                caption=caption,
                premium_allowed=True,
            )
        await _send_followups(bot_client, chat_id, followups)
        logger.info(f"[DirectForward] ✅ relayed {url} -> {chat_id}")
    finally:
        task_dir = f"cache/{cache_id}"
        if os.path.exists(task_dir):
            try:
                shutil.rmtree(task_dir)
            except Exception:
                pass


def _enqueue_relay(queue, user_chat_id: int, corofn_factory) -> None:
    """Fire-and-forget enqueue of one relay job on the shared download queue."""
    async def _submit():
        await queue.add_task(user_id=user_chat_id, message=_NullStatusMessage(),
                             coroutine=corofn_factory)
    asyncio.create_task(_submit())


def _fetch_bytes(url: str, referer: str | None = None) -> bytes:
    """Small helper for downloading DM attachment bytes (runs in executor)."""
    import requests
    headers = {"User-Agent": getattr(config, "YTDLP_USER_AGENT", "") or
               "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
    if referer:
        headers["Referer"] = referer
    proxies = {"http": config.REQUESTS_PROXY, "https": config.REQUESTS_PROXY} if config.REQUESTS_PROXY else None
    resp = requests.get(url, headers=headers, proxies=proxies, timeout=60)
    resp.raise_for_status()
    return resp.content


def _video_upload_kwargs(file_path: str) -> dict:
    """Full send_video metadata for a raw CDN video (DM attachments, carousels).

    Raw CDN bytes have no sidecar cover file, so probe width/height/duration
    and generate a frame thumbnail — otherwise every such upload would arrive
    thumbless (Telegram only auto-generates thumbs for <10MB videos).
    Best-effort: never raises, so a corrupt file still uploads as before.
    """
    from utils.downloader import probe_video_dimensions, extract_video_frame_thumb
    width, height, duration = probe_video_dimensions(file_path)
    return {
        "width": width,
        "height": height,
        "duration": duration,
        "thumb": extract_video_frame_thumb(file_path),
        "supports_streaming": True,
    }


def _x_media_payload_ok(data: bytes, is_photo: bool) -> bool:
    """Validate a fetched CDN payload by MAGIC BYTES, never by size — X serves
    legitimately tiny images (a 133-byte solid PNG is a real photo). Reject only
    when the bytes are neither a real image nor a real mp4, or are an HTML
    interstitial (login wall / challenge page)."""
    head = data[:16]
    img_ok = (head.startswith(b"\x89PNG\r\n\x1a\n") or head.startswith(b"\xff\xd8\xff")
              or head.startswith(b"GIF87a") or head.startswith(b"GIF89a")
              or head.startswith(b"RIFF") or head.startswith(b"BM"))
    mp4_ok = len(data) >= 8 and data[4:8] == b"ftyp"
    lower = data[:256].lstrip().lower()
    if lower.startswith(b"<html") or lower.startswith(b"<!doctype"):
        return False
    return img_ok if is_photo else (mp4_ok or img_ok)


def _header_lines(platform: str, sender_label: str, author_label: str | None,
                  author_id: str | None, post_url: str | None) -> list[str]:
    lines = [f"📥 **{platform} DM** from {sender_label}"]
    if author_label:
        if author_id:
            lines.append(f"👤 **Post by @{author_label}** (id: `{author_id}`)")
        else:
            lines.append(f"👤 **Post by @{author_label}**")
    if post_url:
        lines.append(f"🔗 {post_url}")
    return lines
"""
Bot API 10.2 rich messages with inline media ("media on top, long text below").

Telegram media captions cap at **1024** characters, but a relayed post's text
(caption + hashtags + credits) routinely exceeds that. Rich Messages
(``sendRichMessage``, Bot API 10.1/10.2) solve it natively: media is placed
where its ``tg://video?id=...`` / ``tg://photo?id=...`` element appears in the
HTML, and the message can carry up to **32768** UTF-8 characters — so the
media stays first and the whole caption follows in the same bubble.

Only the Bot API exposes this method (pyrogram has no MTProto equivalent), so
the call goes over HTTPS with the bot token, exactly like
``utils.rich_stream`` and the log-channel handler. The blocking POST runs in an
executor from the async helpers.

Limits enforced here (the caller decides when rich is worth using):
  * videos: Bot API upload ceiling 2 GB (the premium 4 GB path stays MTProto);
  * photos: 10 MB;
  * rich text: 32768 characters (longer text is truncated with a marker).
"""
from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import os
import re
import time

import requests

import config

logger = logging.getLogger(__name__)

RICH_TEXT_LIMIT = 32000          # below the API's 32768 (leave room for markup)
RICH_VIDEO_MAX = 2 * 1024 ** 3   # Bot API hard cap
RICH_PHOTO_MAX = 10 * 1024 ** 2  # Bot API photo cap

_MEDIA_TYPES = {
    ".mp4": ("video", "video/mp4"), ".mov": ("video", "video/quicktime"),
    ".m4v": ("video", "video/mp4"), ".webm": ("video", "video/webm"),
    ".jpg": ("photo", "image/jpeg"), ".jpeg": ("photo", "image/jpeg"),
    ".png": ("photo", "image/png"), ".webp": ("photo", "image/webp"),
}


def _base_url() -> str:
    return f"https://api.telegram.org/bot{config.BOT_TOKEN}"


def _md_bold_to_html(text: str) -> str:
    """Escape HTML and turn the project's ``**bold**`` markers into <b>."""
    escaped = _html.escape(text)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)


def build_html(header_lines: list[str], body: str) -> str:
    """Post text first, relay header last, as one rich-HTML document."""
    parts: list[str] = []
    body = (body or "").strip()
    if body:
        if len(body) > RICH_TEXT_LIMIT:
            body = body[:RICH_TEXT_LIMIT] + "\n…"
        # Blank line = new paragraph, single newline = line break.
        for para in re.split(r"\n\s*\n", body):
            para = para.strip()
            if para:
                parts.append(f"<p>{_md_bold_to_html(para).replace(chr(10), '<br>')}</p>")
    head = "<br>".join(_md_bold_to_html(line) for line in (header_lines or []) if line)
    if head:
        parts.append(f"<p>{head}</p>")
    return "".join(parts)


def _reply_params(reply_to_message_id) -> dict | None:
    if not reply_to_message_id:
        return None
    return {"message_id": int(reply_to_message_id), "allow_sending_without_reply": True}


def _post_sync(method: str, data: dict, files: dict | None = None,
               timeout: int = 600) -> dict:
    proxies = (
        {"http": config.REQUESTS_PROXY, "https": config.REQUESTS_PROXY}
        if getattr(config, "REQUESTS_PROXY", None)
        else None
    )
    try:
        resp = requests.post(f"{_base_url()}/{method}", data=data, files=files,
                             timeout=timeout, proxies=proxies)
        try:
            payload = resp.json()
        except Exception:
            payload = {"ok": False, "description": resp.text[:300]}
        if not payload.get("ok"):
            logger.warning("[RichMedia] %s rejected: %s",
                           method, str(payload.get("description"))[:300])
        return payload
    except Exception as e:  # noqa: BLE001
        logger.warning("[RichMedia] %s failed: %s", method, e)
        return {"ok": False}


async def _post(method: str, data: dict, files: dict | None = None,
                timeout: int = 600) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _post_sync, method, data, files, timeout)


async def send_rich_text(chat_id: int, header_lines: list[str], body: str,
                         reply_to_message_id=None) -> bool:
    """Send text-only rich content (used when media can't go in a rich bubble)."""
    doc = build_html(header_lines, body)
    if not doc:
        return False
    data = {"chat_id": str(chat_id), "rich_message": json.dumps({"html": doc})}
    rp = _reply_params(reply_to_message_id)
    if rp:
        data["reply_parameters"] = json.dumps(rp)
    payload = await _post("sendRichMessage", data, timeout=60)
    return (payload.get("result") or {}).get("message_id") if payload.get("ok") else None


def _media_descriptor(path: str, thumb_path: str | None) -> tuple[dict, dict, str]:
    """Return (rich-media entry, multipart files, media kind)."""
    ext = os.path.splitext(path)[1].lower()
    kind, mime = _MEDIA_TYPES.get(ext, ("video", "video/mp4"))
    entry = {"type": kind, "media": "attach://f0"}
    files = {"f0": (os.path.basename(path), open(path, "rb"), mime)}
    if thumb_path and os.path.exists(thumb_path):
        entry["thumbnail"] = "attach://t0"
        files["t0"] = (os.path.basename(thumb_path), open(thumb_path, "rb"), "image/jpeg")
    return entry, files, kind


async def send_rich_media(chat_id: int, path: str, header_lines: list[str],
                          body: str, thumb_path: str | None = None,
                          width: int | None = None, height: int | None = None,
                          duration: int | None = None,
                          reply_to_message_id=None) -> bool:
    """Send one photo/video with the media at the top and the full text below.

    Returns False (caller falls back to a normal send) when the file is over
    the Bot API ceiling, missing, or Telegram rejects the rich message.
    """
    try:
        if not path or not os.path.exists(path):
            return False
        size = os.path.getsize(path)
        entry, files, kind = _media_descriptor(path, thumb_path)
        if kind == "photo":
            if size > RICH_PHOTO_MAX:
                return False
        elif size > RICH_VIDEO_MAX:
            return False
        for k, v in (("width", width), ("height", height), ("duration", duration)):
            if v:
                entry[k] = int(v)
        if kind == "video":
            entry["supports_streaming"] = True
        element = (f'<figure><video src="tg://video?id=m0"></video></figure>'
                   if kind == "video" else
                   f'<figure><img src="tg://photo?id=m0"/></figure>')
        doc = element + build_html(header_lines, body)
        rich = {"html": doc, "media": [{"id": "m0", "media": entry}]}
        data = {"chat_id": str(chat_id), "rich_message": json.dumps(rich)}
        rp = _reply_params(reply_to_message_id)
        if rp:
            data["reply_parameters"] = json.dumps(rp)
        payload = await _post("sendRichMessage", data, files)
        return (payload.get("result") or {}).get("message_id") if payload.get("ok") else None
    except Exception as e:  # noqa: BLE001
        logger.warning("[RichMedia] rich send failed: %s", e)
        return False
    finally:
        try:
            for fh in (locals().get("files") or {}).values():
                fh[1].close()
        except Exception:
            pass


async def send_rich_album(chat_id: int, items: list[tuple[str, bool]],
                          header_lines: list[str], body: str,
                          cap: int = 10, reply_to_message_id=None) -> bool:
    """Send 2..``cap`` photos/videos in one rich collage with the full text.

    ``items`` is ``[(path, is_video), ...]``. Returns False when the set can't
    be expressed as a rich message (caller keeps the normal album path).
    """
    items = items[:cap]
    if len(items) < 2:
        return False
    files: dict = {}
    media: list = []
    blocks: list[str] = []
    try:
        for i, (path, is_video) in enumerate(items):
            if not path or not os.path.exists(path):
                return False
            size = os.path.getsize(path)
            if size > (RICH_VIDEO_MAX if is_video else RICH_PHOTO_MAX):
                return False
            ext = os.path.splitext(path)[1].lower()
            kind = "video" if is_video else "photo"
            fkey = f"f{i}"
            entry = {"type": kind, "media": f"attach://{fkey}"}
            if is_video:
                entry["supports_streaming"] = True
            files[fkey] = (os.path.basename(path), open(path, "rb"),
                           _MEDIA_TYPES.get(ext, ("video", "video/mp4"))[1])
            media.append({"id": f"m{i}", "media": entry})
            blocks.append(f'<video src="tg://video?id=m{i}"></video>' if is_video
                          else f'<img src="tg://photo?id=m{i}"/>')
        doc = f"<tg-collage>{''.join(blocks)}</tg-collage>" + build_html(header_lines, body)
        rich = {"html": doc, "media": media}
        data = {"chat_id": str(chat_id), "rich_message": json.dumps(rich)}
        rp = _reply_params(reply_to_message_id)
        if rp:
            data["reply_parameters"] = json.dumps(rp)
        payload = await _post("sendRichMessage", data, files)
        return (payload.get("result") or {}).get("message_id") if payload.get("ok") else None
    except Exception as e:  # noqa: BLE001
        logger.warning("[RichMedia] rich album failed: %s", e)
        return False
    finally:
        for fh in files.values():
            try:
                fh[1].close()
            except Exception:
                pass

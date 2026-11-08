"""
Bot API 10.x rich TEXT messages for long relay captions.

Telegram media captions cap at **1024** characters, but a relayed post's text
(caption + hashtags + credits) routinely exceeds that. Rich Messages
(``sendRichMessage``) carry up to **32768** UTF-8 characters and support
paragraphs, headings, quotes and more.

Design decision (2026-11-08): the rich message is **text only** and is sent as
a **reply to the normal media message**. Inline rich media was abandoned for
two reasons:
  * a rich message's media does not appear in Telegram's **Media tab**, so the
    operator lost media history;
  * ``sendRichMessage`` media uploads have a much smaller size cap than normal
    media uploads — a 73 MB video was rejected with "Request Entity Too Large".

So the media is always sent through the normal pyrogram media path (Media-tab
visible), with the relay header as its caption, and the full post text follows
here. Only the Bot API exposes this method (pyrogram has no MTProto
equivalent), so the call goes over HTTPS with the bot token, exactly like
``utils.rich_stream`` and the log-channel handler. The blocking POST runs in an
executor from the async helpers.

Limits enforced here: up to ``RICH_TEXT_LIMIT`` characters (the API caps at
32768; the extra margin covers markup).
"""
from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import re

import requests

import config

logger = logging.getLogger(__name__)

RICH_TEXT_LIMIT = 32000          # below the API's 32768 (leave room for markup)


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


def _post_sync(method: str, data: dict, timeout: int = 60) -> dict:
    proxies = (
        {"http": config.REQUESTS_PROXY, "https": config.REQUESTS_PROXY}
        if getattr(config, "REQUESTS_PROXY", None)
        else None
    )
    try:
        resp = requests.post(f"{_base_url()}/{method}", data=data,
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


async def _post(method: str, data: dict, timeout: int = 60) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _post_sync, method, data, timeout)


async def send_rich_text(chat_id: int, header_lines: list[str], body: str,
                         reply_to_message_id=None) -> int | None:
    """Send text-only rich content; returns the message id (falsy on failure).

    Used for the overflow of a long caption, as a reply to the media message
    that carries only the relay header.
    """
    doc = build_html(header_lines, body)
    if not doc:
        return None
    data = {"chat_id": str(chat_id), "rich_message": json.dumps({"html": doc})}
    rp = _reply_params(reply_to_message_id)
    if rp:
        data["reply_parameters"] = json.dumps(rp)
    payload = await _post("sendRichMessage", data)
    return (payload.get("result") or {}).get("message_id") if payload.get("ok") else None

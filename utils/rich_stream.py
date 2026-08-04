# utils/rich_stream.py
"""
Bot API 10.1+ streaming-text status ("thinking") messages.

telegram-bot-api.md → "Recent changes" (Bot API 10.1, 2026-06-11) introduced
Rich Messages and streaming drafts: sendRichMessage / sendRichMessageDraft
(and sendMessageDraft for plain text). A *draft* is an ephemeral, animated
30-second preview; once the real output is ready the caller MUST send the
final message via the normal send path so it persists in the chat.

The bot talks to Telegram over MTProto via pyrogram, but the draft methods only
exist in the Bot API (HTTP) — pyrogram has no MTProto equivalent. Exactly like
utils.logger.TelegramChannelHandler already does for the log channel, we call
the hosted Bot API directly with the same bot token.

Rules:
  * Private chats only. sendMessageDraft / sendRichMessageDraft require a
    private chat; every user-facing flow in this bot is filters.private.
  * Same draft_id across updates animates the transition.
  * Streaming is best-effort. If the server rejects the first draft call we
    silently fall back to a real status message edited along the way, so the
    bot never depends on streaming to work.
  * Scope: short waiting/analysis phases (format extraction, playlist read).
    Progress bars and final media stay on real pyrogram messages because they
    are edited by message id, and a draft has no id to edit.
  * Ephemeral Messages (receiver_user_id / callback_query_id) are a
    group-chat feature; this bot is private-chat only, so they are not used.
"""
import asyncio
import itertools
import logging

import requests

import config

logger = logging.getLogger(__name__)

_draft_counter = itertools.count(1)


def _post_sync(base_url: str, method: str, payload: dict) -> bool:
    """POST a Bot API method; True only if the server accepted it (ok=true)."""
    try:
        proxies = (
            {"http": config.REQUESTS_PROXY, "https": config.REQUESTS_PROXY}
            if getattr(config, "REQUESTS_PROXY", None)
            else None
        )
        resp = requests.post(f"{base_url}/{method}", json=payload, timeout=10, proxies=proxies)
        try:
            data = resp.json()
        except Exception:
            data = {}
        return bool(data.get("ok", resp.status_code == 200))
    except Exception as e:
        logger.debug("RichStream %s POST failed: %s", method, e)
        return False


async def _post(base_url: str, method: str, payload: dict) -> bool:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _post_sync, base_url, method, payload)


class RichStream:
    """Streams an animated 'thinking' status while a waiting phase runs.

    The caller sends the real, persisted message itself once it has the result
    (format keyboard, playlist menu, error text, ...) — the draft then expires
    on its own and is never left in the chat.

    If streaming turns out to be unavailable we fall back to a real status
    message edited along the way, so behaviour is identical to the
    pre-streaming code path.
    """

    def __init__(self, chat_id: int, send_fn, reply_to_message_id=None, base_url: str | None = None):
        self.chat_id = chat_id
        self.send_fn = send_fn  # pyrogram send coroutine (e.g. message._client.send_message)
        self.reply_to_message_id = reply_to_message_id
        self.base_url = base_url or f"https://api.telegram.org/bot{config.BOT_TOKEN}"
        self.draft_id = next(_draft_counter)
        self._fallback_msg = None  # real status message, when streaming failed
        self._streaming = True  # optimistic; turned off on first rejection
        self._closed = False

    # -- public -----------------------------------------------------------
    async def update(self, markdown: str | None = None, *, thinking: str | None = None) -> None:
        """Stream a partial status update (same draft_id → animated).

        *markdown* is rich Markdown (GFM-compatible: **bold**, `code`, lists).
        *thinking* renders the <tg-thinking> placeholder instead.
        """
        if self._closed:
            return
        rich = {}
        if thinking is not None:
            rich["markdown"] = f"<tg-thinking>{thinking}</tg-thinking>"
        elif markdown is not None:
            rich["markdown"] = markdown

        if self._streaming and rich:
            payload = {
                "chat_id": self.chat_id,
                "draft_id": self.draft_id,
                "rich_message": rich,
            }
            if await _post(self.base_url, "sendRichMessageDraft", payload):
                return
            logger.warning("RichStream draft rejected (chat %s) — falling back to plain status", self.chat_id)
            self._streaming = False
        await self._fallback(markdown or thinking or "")

    async def close(self) -> None:
        """Stop streaming. Call before sending the real final message.

        Removes a fallback status message if one was created (mirrors the old
        ``status_msg.delete()``); an active draft just expires on its own.
        """
        if self._fallback_msg is not None:
            try:
                await self._fallback_msg.delete()
            except Exception:
                pass
        self._closed = True

    # -- internal ---------------------------------------------------------
    async def _fallback(self, text: str) -> None:
        from utils.uploader_handler import send_reply_safe
        try:
            if self._fallback_msg is None:
                self._fallback_msg = await send_reply_safe(
                    self.send_fn, self.reply_to_message_id,
                    chat_id=self.chat_id, text=text,
                )
            else:
                await self._fallback_msg.edit_text(text)
        except Exception as e:
            logger.debug("RichStream fallback status failed: %s", e)

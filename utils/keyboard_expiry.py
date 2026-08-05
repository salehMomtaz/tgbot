# utils/keyboard_expiry.py
"""Inline-keyboard auto-expiration for the bot.

The bot's conversations are full of inline keyboards (admin console, format
selectors, cookie menus, ...). If the user never taps them again they stay in
chat history forever as dead buttons. This module gives every inline keyboard a
TTL: once it goes unused for ``_TTL_SECONDS`` the buttons are stripped from the
message (the text stays, the markup is removed), so history does not accumulate
"keyboard leftovers".

Design:
* ``watch(chat_id, message_id)`` registers a message; called automatically by
  the send/edit monkeypatches in ``main.py`` whenever a message carries a
  ``reply_markup``.
* ``touch(message_id)`` resets the deadline; called from the group -2 callback
  interceptor so every button press on a live keyboard keeps it alive.
* ``expiry_loop`` runs in the background, strips expired keyboards, and sweeps
  stale one-shot state (premium session generation) that forgot to clean up.
"""
import asyncio
import time
import logging

logger = logging.getLogger(__name__)

# Registry: (chat_id, message_id) -> expires_at.
# Message ids are only unique per chat, so the key must carry both or two
# users' keyboards (both often land on the same small message id) would collide.
_REGISTRY = {}
_TTL_SECONDS = 20 * 60  # 20 minutes without a button press
_LOOP_INTERVAL = 30     # check cadence (seconds)
_MAX_BATCH = 50         # don't try to strip more than this per sweep tick


def watch(chat_id: int, message_id: int, ttl: int = _TTL_SECONDS):
    _REGISTRY[(chat_id, message_id)] = time.monotonic() + ttl


def touch(chat_id: int, message_id: int):
    key = (chat_id, message_id)
    if key in _REGISTRY:
        _REGISTRY[key] = time.monotonic() + _TTL_SECONDS


def unwatch(chat_id: int, message_id: int):
    _REGISTRY.pop((chat_id, message_id), None)


async def expiry_loop(client) -> None:
    """Background task: strip expired inline keyboards + sweep stale gen state."""
    while True:
        await asyncio.sleep(_LOOP_INTERVAL)
        now = time.monotonic()
        expired = [
            (cid, mid)
            for (cid, mid), exp in list(_REGISTRY.items())
            if exp <= now
        ]
        for cid, mid in expired[:_MAX_BATCH]:
            try:
                await client.edit_message_reply_markup(cid, mid, reply_markup=None)
                logger.info(f"[KeyboardExpiry] Stripped expired keyboard on message {mid}")
            except Exception:
                pass
            _REGISTRY.pop((cid, mid), None)

        # Sweep dangling premium session-generation temp clients (no-op if idle).
        try:
            from modules import admin as admin_mod
            await admin_mod.sweep_stale_generations(client)
        except Exception:
            pass

"""
Friend Media Archiver — archives your friends' Telegram profile pictures +
stories (and best-effort Instagram stories) into a place only you can see.

The connected *user* account (kurigram `premium_app`) does the reading; the bot
account NEVER messages your friends. All fetched media is delivered only to the
configured safe destination (your Saved Messages or a chat id you own).
"""

from .admin import (
    render_menu,
    fm_callback_dispatch,
    handle_friend_text,
    start_friend_media_task,
)

__all__ = [
    "render_menu",
    "fm_callback_dispatch",
    "handle_friend_text",
    "start_friend_media_task",
]

"""
Admin console integration for the Friend Media Archiver.

Exposes:
  * render_menu(client, callback_query)            — opens the 📸 menu
  * fm_callback_dispatch(client, callback_query)   — handles all ^fm_ callbacks
  * handle_friend_text(...)                         — free-form text-state input
  * start_friend_media_task(app, premium_app)      — optional auto-archive loop

The console lets the operator add friends by numeric id / @username / handle,
toggle what to archive (profile pics / stories / Instagram), and run an archive.
ALL archived media is delivered ONLY to the safe destination (see common.py) —
the friends themselves are NEVER messaged.
"""

import asyncio
import logging
import time
import config
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, Message,
)

from . import state as fm_state
from . import telegram as fm_tg
from . import instagram as fm_ig
from . import common as fm_common

# USER_STATES is the admin module's state dict (imported lazily below to avoid a
# circular import: modules.admin.__init__ imports this package at module load).

logger = logging.getLogger(__name__)


def _enabled():
    return bool(getattr(config, "FRIEND_MEDIA_ENABLED", False))


def _blurb():
    dest = fm_common.resolve_destination()
    dest_label = "Saved Messages (your account)" if dest == "me" else str(dest)
    ig = "🟢" if getattr(config, "FRIEND_MEDIA_IG_ENABLED", False) else "🔴"
    sched = int(getattr(config, "FRIEND_MEDIA_SCHEDULE_MINUTES", 0) or 0)
    sched_label = f"every {sched}m" if sched > 0 else "manual only"
    return (
        "📸 **Friend Media Archiver**\n\n"
        "Archives your friends' Telegram profile pictures + stories (and, "
        "best-effort, their current Instagram stories) into a place **only you** "
        "can see. It **never messages your friends** — the only thing that touches "
        "them is a silent `add_contact`.\n\n"
        f"Destination: `{dest_label}`\n"
        f"Instagram stories: {ig}\n"
        f"Auto-archive: **{sched_label}**\n"
        f"Max photos/run: `{config.FRIEND_MEDIA_MAX_PHOTOS}` · "
        f"max stories/run: `{config.FRIEND_MEDIA_MAX_STORIES}`"
    )


def _menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Friend", callback_data="fm_add"),
         InlineKeyboardButton("📋 List Friends", callback_data="fm_list")],
        [InlineKeyboardButton("🚀 Archive All", callback_data="fm_archive_all"),
         InlineKeyboardButton("⚙️ Settings", callback_data="fm_settings")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="fm_menu"),
         InlineKeyboardButton("◀️ Back to Console", callback_data="admin_main")],
    ])


async def render_menu(client, callback_query):
    if not _enabled():
        await callback_query.message.edit_text(
            "📸 Friend Media Archiver is **disabled** (FRIEND_MEDIA_ENABLED=false). "
            "Enable it in `.env` to use this feature.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Back to Console", callback_data="admin_main")
            ]])
        )
        return
    await callback_query.message.edit_text(_blurb(), reply_markup=_menu_keyboard())


def _settings_keyboard():
    dest = fm_common.resolve_destination()
    dest_label = "Saved Messages" if dest == "me" else str(dest)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📥 Destination: {dest_label}", callback_data="fm_dest")],
        [InlineKeyboardButton(
            f"{'🔴 Disable' if config.FRIEND_MEDIA_IG_ENABLED else '🟢 Enable'} IG stories",
            callback_data="fm_toggle_ig_global")],
        [InlineKeyboardButton("⏱ Set schedule (min)", callback_data="fm_sched")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="fm_settings"),
         InlineKeyboardButton("◀️ Back", callback_data="fm_menu")],
    ])


def _friend_keyboard(key, friend):
    pp = "✅" if friend.get("profile_photos") else "❌"
    st = "✅" if friend.get("stories") else "❌"
    ig_en = "✅" if friend.get("ig_enabled") else "❌"
    rows = [
        [InlineKeyboardButton("🚀 Archive now", callback_data=f"fm_arc:{key}")],
        [InlineKeyboardButton(f"📸 Profile pics: {pp}", callback_data=f"fm_tg_pp:{key}"),
         InlineKeyboardButton(f"📖 Stories: {st}", callback_data=f"fm_tg_st:{key}")],
        [InlineKeyboardButton(f"🟣 IG: {ig_en}", callback_data=f"fm_ig_toggle:{key}"),
         InlineKeyboardButton("✏️ Set IG @", callback_data=f"fm_ig_set:{key}")],
        [InlineKeyboardButton("🗑 Remove", callback_data=f"fm_del:{key}"),
         InlineKeyboardButton("◀️ Back", callback_data="fm_list")],
    ]
    return InlineKeyboardMarkup(rows)


def _label(friend):
    name = friend.get("first_name") or friend.get("username") or friend.get("handle") or "?"
    tg = friend.get("telegram_user_id")
    ig = friend.get("ig_username")
    bits = []
    if friend.get("profile_photos"):
        bits.append("📸pics")
    if friend.get("stories"):
        bits.append("📖stories")
    if friend.get("ig_enabled") and ig:
        bits.append(f"🟣ig@{ig}")
    tog = " ".join(bits) if bits else "∅ nothing"
    return f"• `{name}` (tg:{tg or '?'}) — {tog}"


async def _list_message():
    friends = await fm_state.list_friends()
    if not friends:
        return ("📋 **Friends**\n\nNo friends added yet. Tap **➕ Add Friend** and "
                "enter a numeric id, @username, or handle.", _menu_keyboard())
    lines = [_label(f) for _, f in friends]
    kb_rows = []
    for key, f in friends:
        kb_rows.append([InlineKeyboardButton(
            (f.get("first_name") or f.get("handle") or key)[:32],
            callback_data=f"fm_friend:{key}")])
    kb_rows.append([InlineKeyboardButton("➕ Add Friend", callback_data="fm_add"),
                    InlineKeyboardButton("◀️ Back", callback_data="fm_menu")])
    return ("📋 **Friends** (" + str(len(friends)) + ")\n\n" + "\n".join(lines),
            InlineKeyboardMarkup(kb_rows))


async def fm_callback_dispatch(client, callback_query):
    from modules.admin.state import USER_STATES
    data = callback_query.data
    user_id = callback_query.from_user.id

    if user_id != config.SYSTEM_CREATOR_ID:
        await callback_query.answer("Access Denied.", show_alert=True)
        return

    # ---- Menu navigation ----
    if data in ("fm_menu",):
        await render_menu(client, callback_query)
        await callback_query.answer()
        return
    if data == "fm_settings":
        await callback_query.message.edit_text(
            "⚙️ **Settings**\n\nDestination is where media lands (only you see it). "
            "IG stories require a valid igcookies jar.",
            reply_markup=_settings_keyboard())
        await callback_query.answer()
        return
    if data == "fm_list":
        txt, kb = await _list_message()
        await callback_query.message.edit_text(txt, reply_markup=kb)
        await callback_query.answer()
        return
    if data == "fm_add":
        USER_STATES[user_id] = "waiting_for_friend_add"
        await callback_query.message.edit_text(
            "➕ **Add Friend**\n\nSend the friend's **numeric Telegram id**, "
            "**@username**, or **username** (one per line to add several).\n\n"
            "They will be added to your connected account's contacts (silent — "
            "nothing is sent to them). Only YOU will receive their media.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Cancel", callback_data="fm_menu")]]))
        await callback_query.answer()
        return
    if data == "fm_dest":
        USER_STATES[user_id] = "waiting_for_friend_dest"
        await callback_query.message.edit_text(
            "📥 **Set destination**\n\nSend `saved` (your account's Saved Messages) "
            "or a numeric chat id you own (e.g. a private channel).",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Cancel", callback_data="fm_settings")]]))
        await callback_query.answer()
        return
    if data == "fm_sched":
        USER_STATES[user_id] = "waiting_for_friend_schedule"
        await callback_query.message.edit_text(
            "⏱ **Set auto-archive schedule**\n\nSend minutes between runs "
            "(0 = manual only, recommended for large lists).",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Cancel", callback_data="fm_settings")]]))
        await callback_query.answer()
        return
    if data == "fm_toggle_ig_global":
        config.FRIEND_MEDIA_IG_ENABLED = not config.FRIEND_MEDIA_IG_ENABLED
        await callback_query.message.edit_text(
            "⚙️ **Settings**", reply_markup=_settings_keyboard())
        await callback_query.answer()
        return

    # ---- Archive all ----
    if data == "fm_archive_all":
        await callback_query.answer()
        await _archive_all(client, callback_query.message)
        return

    # ---- Per-friend actions ----
    if data.startswith("fm_friend:"):
        key = data.split(":", 1)[1]
        friend = await fm_state.get_friend(key)
        if friend is None:
            await callback_query.answer("Not found.", show_alert=True)
            return
        await callback_query.message.edit_text(
            "👤 **Friend**\n\n" + _label(friend),
            reply_markup=_friend_keyboard(key, friend))
        await callback_query.answer()
        return

    if data.startswith("fm_arc:"):
        key = data.split(":", 1)[1]
        await callback_query.answer("Starting archive…")
        await _archive_one(client, callback_query.message, key)
        return

    if data.startswith("fm_tg_pp:"):
        key = data.split(":", 1)[1]
        f = await fm_state.get_friend(key)
        if f is None:
            await callback_query.answer("Not found.", show_alert=True)
            return
        await fm_state.update_friend(key, {"profile_photos": not f.get("profile_photos")})
        f = await fm_state.get_friend(key)
        await callback_query.message.edit_text(
            "👤 **Friend**\n\n" + _label(f), reply_markup=_friend_keyboard(key, f))
        await callback_query.answer()
        return

    if data.startswith("fm_tg_st:"):
        key = data.split(":", 1)[1]
        f = await fm_state.get_friend(key)
        if f is None:
            await callback_query.answer("Not found.", show_alert=True)
            return
        await fm_state.update_friend(key, {"stories": not f.get("stories")})
        f = await fm_state.get_friend(key)
        await callback_query.message.edit_text(
            "👤 **Friend**\n\n" + _label(f), reply_markup=_friend_keyboard(key, f))
        await callback_query.answer()
        return

    if data.startswith("fm_ig_toggle:"):
        key = data.split(":", 1)[1]
        f = await fm_state.get_friend(key)
        if f is None:
            await callback_query.answer("Not found.", show_alert=True)
            return
        await fm_state.update_friend(key, {"ig_enabled": not f.get("ig_enabled")})
        f = await fm_state.get_friend(key)
        await callback_query.message.edit_text(
            "👤 **Friend**\n\n" + _label(f), reply_markup=_friend_keyboard(key, f))
        await callback_query.answer()
        return

    if data.startswith("fm_ig_set:"):
        key = data.split(":", 1)[1]
        USER_STATES[user_id] = f"waiting_for_friend_ig:{key}"
        await callback_query.message.edit_text(
            "✏️ **Set Instagram @username** for this friend.\n\n"
            "Send the username WITHOUT the @ (e.g. `nature_lover`).",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Cancel", callback_data=f"fm_friend:{key}")]]))
        await callback_query.answer()
        return

    if data.startswith("fm_del:"):
        key = data.split(":", 1)[1]
        await callback_query.message.edit_text(
            "🗑 **Remove this friend?** Their archived media stays in your Saved "
            "Messages; only future archiving stops.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, remove", callback_data=f"fm_del_confirm:{key}")],
                [InlineKeyboardButton("❌ Cancel", callback_data=f"fm_friend:{key}")]]))
        await callback_query.answer()
        return

    if data.startswith("fm_del_confirm:"):
        key = data.split(":", 1)[1]
        await fm_state.remove_friend(key)
        txt, kb = await _list_message()
        await callback_query.message.edit_text(txt, reply_markup=kb)
        await callback_query.answer("Removed.")
        return

    await callback_query.answer()


async def _archive_all(client, status_msg):
    if fm_common.user_client() is None:
        await status_msg.reply_text(
            "⚠️ The connected user account (PREMIUM_STRING_SESSION) is not started. "
            "I can't read friends' profile-photo history without a user account.")
        return
    friends = await fm_state.list_friends()
    enabled = [ (k, f) for k, f in friends
                if f.get("profile_photos") or f.get("stories")
                or (f.get("ig_enabled") and f.get("ig_username")) ]
    if not enabled:
        await status_msg.reply_text("No friends have archiving enabled.")
        return
    await status_msg.reply_text(
        f"🚀 Archiving {len(enabled)} friend(s)… this may take a while. "
        "You'll receive the media in your Saved Messages.")
    asyncio.create_task(_run_archives(client, enabled, status_msg))


async def _run_archives(client, friends, status_msg):
    for key, friend in friends:
        try:
            parts = []
            if friend.get("telegram_user_id") and (friend.get("profile_photos") or friend.get("stories")):
                parts.append(await fm_tg.archive_friend_telegram(key, friend))
            if friend.get("ig_enabled") and friend.get("ig_username"):
                n = await fm_ig.archive_instagram_stories(key, friend, bot=client)
                parts.append(f"{n} IG stories")
            summary = "; ".join(p for p in parts if p) or "nothing"
            await fm_state.update_friend(key, {"last_run": int(time.time()),
                                               "last_count": summary})
            await client.send_message(
                config.SYSTEM_CREATOR_ID,
                f"✅ Archived `{friend.get('first_name') or friend.get('handle') or key}`: {summary}")
        except Exception as e:
            logger.exception(f"[FriendMedia] archive {key} failed: {e}")
            await client.send_message(
                config.SYSTEM_CREATOR_ID,
                f"❌ Archive failed for `{key}`: {e}")


async def _archive_one(client, status_msg, key):
    friend = await fm_state.get_friend(key)
    if friend is None:
        await status_msg.reply_text("Friend not found.")
        return
    if fm_common.user_client() is None and (friend.get("profile_photos") or friend.get("stories")):
        await status_msg.reply_text(
            "⚠️ Connected user account not started — can't archive Telegram media.")
        return
    await status_msg.reply_text(f"🚀 Archiving `{friend.get('first_name') or key}`…")
    asyncio.create_task(_run_archives(client, [(key, friend)], status_msg))


async def handle_friend_text(client, message, user_id, state, input_text, prompt_id, app, back_markup):
    from modules.admin.state import USER_STATES
    txt = input_text.strip()

    if state == "waiting_for_friend_add":
        lines = [l.strip() for l in txt.splitlines() if l.strip()]
        added = []
        for handle in lines:
            key, friend = await _resolve_and_add(handle)
            if key:
                added.append(f"`{friend.get('first_name') or handle}` (tg:{friend.get('telegram_user_id')})")
        USER_STATES.pop(user_id, None)
        if prompt_id:
            try:
                await client.delete_messages(chat_id=user_id, message_ids=prompt_id)
            except Exception:
                pass
        if added:
            await message.reply_text(
                "✅ Added:\n" + "\n".join(added) +
                "\n\nTheir media will be archived into your Saved Messages when you "
                "tap **Archive now** / **Archive All**.", reply_markup=back_markup)
        else:
            await message.reply_text(
                "❌ Could not resolve any of those. Check the id/username and try again "
                "from ➕ Add Friend.", reply_markup=back_markup)
        return

    if state.startswith("waiting_for_friend_ig:"):
        key = state.split(":", 1)[1]
        ig = txt.lstrip("@")
        await fm_state.update_friend(key, {"ig_username": ig,
                                           "ig_enabled": True})
        USER_STATES.pop(user_id, None)
        if prompt_id:
            try:
                await client.delete_messages(chat_id=user_id, message_ids=prompt_id)
            except Exception:
                pass
        await message.reply_text(
            f"✅ Instagram set to `@{ig}` for that friend (IG stories enabled).",
            reply_markup=back_markup)
        return

    if state == "waiting_for_friend_dest":
        v = txt.strip().lower()
        if v in ("saved", "me", ""):
            resolved = "saved"
        else:
            try:
                int(v)
                resolved = v
            except Exception:
                await message.reply_text(
                    "❌ Send `saved` or a numeric chat id you own.", reply_markup=back_markup)
                return
        config.FRIEND_MEDIA_DESTINATION = resolved
        USER_STATES.pop(user_id, None)
        if prompt_id:
            try:
                await client.delete_messages(chat_id=user_id, message_ids=prompt_id)
            except Exception:
                pass
        label = "Saved Messages" if resolved == "saved" else resolved
        await message.reply_text(f"✅ Destination set to **{label}**.", reply_markup=back_markup)
        return

    if state == "waiting_for_friend_schedule":
        try:
            mins = int(txt.strip())
            if mins < 0:
                raise ValueError
        except Exception:
            await message.reply_text("❌ Send a non-negative number of minutes (0 = manual).",
                                    reply_markup=back_markup)
            return
        config.FRIEND_MEDIA_SCHEDULE_MINUTES = mins
        USER_STATES.pop(user_id, None)
        if prompt_id:
            try:
                await client.delete_messages(chat_id=user_id, message_ids=prompt_id)
            except Exception:
                pass
        await message.reply_text(
            f"✅ Auto-archive schedule set to **{mins} min** (0 = manual only). "
            "Restart the bot for the loop to pick up a new schedule.",
            reply_markup=back_markup)
        return


async def _resolve_and_add(handle):
    """Resolve a handle to a Telegram user and persist the friend record.

    Returns (key, friend) or (None, None).
    """
    # If it's purely a numeric id we can store the key even before resolving,
    # but we still try to resolve to validate + add to contacts + capture name.
    user = await fm_tg.resolve_telegram_user(handle)
    if user is None:
        # A bare numeric id that the connected account can't resolve *yet* (e.g. a
        # contacts-only friend) is still stored so an archive attempt can try
        # get_chat_photos by id later — the account may know the peer at run time.
        if handle.isdigit():
            key = handle
            friend = {
                "platform": "telegram",
                "handle": handle,
                "telegram_user_id": int(handle),
                "username": "",
                "first_name": handle,
                "profile_photos": True,
                "stories": True,
                "ig_username": "",
                "ig_enabled": False,
            }
            await fm_state.add_or_update_friend(key, friend)
            return key, friend
        # Allow storing an unresolved @username (e.g. an IG-only friend).
        if handle.startswith("@") or _looks_like_username(handle):
            key = "tg:" + handle.lstrip("@")
            friend = {
                "platform": "telegram",
                "handle": handle,
                "telegram_user_id": None,
                "username": handle.lstrip("@"),
                "first_name": handle.lstrip("@"),
                "profile_photos": True,
                "stories": True,
                "ig_username": "",
                "ig_enabled": False,
            }
            await fm_state.add_or_update_friend(key, friend)
            return key, friend
        return None, None
    u_id = getattr(user, "id", None)
    uname = getattr(user, "username", None)
    fname = getattr(user, "first_name", None) or (uname or str(u_id))
    key = str(u_id) if u_id else ("tg:" + (uname or handle))
    friend = {
        "platform": "telegram",
        "handle": handle,
        "telegram_user_id": u_id,
        "username": uname or "",
        "first_name": fname,
        "profile_photos": True,
        "stories": True,
        "ig_username": "",
        "ig_enabled": False,
    }
    await fm_state.add_or_update_friend(key, friend)
    return key, friend


def _looks_like_username(h):
    return all(c.isalnum() or c in (".", "_") for c in h) and not h.isdigit()


def start_friend_media_task(app, premium_app):
    """Optional auto-archive loop. Returns an asyncio Task or None.

    Requires FRIEND_MEDIA_ENABLED and FRIEND_MEDIA_SCHEDULE_MINUTES > 0, plus a
    started connected user account (premium_app).
    """
    if not _enabled():
        return None
    mins = int(getattr(config, "FRIEND_MEDIA_SCHEDULE_MINUTES", 0) or 0)
    if mins <= 0:
        return None
    if premium_app is None:
        logging.warning("[FriendMedia] auto-archive enabled but no user account (premium_app).")
        return None

    async def _loop():
        while True:
            try:
                await asyncio.sleep(mins * 60)
                friends = await fm_state.list_friends()
                enabled = [ (k, f) for k, f in friends
                            if f.get("profile_photos") or f.get("stories")
                            or (f.get("ig_enabled") and f.get("ig_username")) ]
                if enabled:
                    await _run_archives(app, enabled, None)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logging.exception(f"[FriendMedia] auto-archive loop error: {e}")

    logging.info(f"[FriendMedia] auto-archive loop scheduled every {mins}m.")
    return asyncio.create_task(_loop())

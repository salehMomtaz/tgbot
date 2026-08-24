"""
Admin console integration for the Friend Media Archiver.

Exposes:
  * render_menu(client, callback_query)            — opens the 📸 menu
  * fm_callback_dispatch(client, callback_query)   — handles all ^fm_ callbacks
  * handle_friend_text(...)                         — free-form text-state input
  * start_friend_media_task(app, premium_app)      — auto-archive watcher

Console features: add friends (id/@username/phone/contacts browser), toggle what
to archive (profile pics / stories / IG stories+posts), explicit FULL backfill,
incremental checks (new media only), settings persisted to .env via dotenv
set_key (survive restarts, applied live), and a background loop that re-reads
its gates EVERY cycle so console toggles take effect without a restart.

ALL archived media is delivered ONLY to the safe destination (see common.py) —
the friends themselves are NEVER messaged.
"""

import os
import random
import asyncio
import logging
import time
import config
from dotenv import set_key as dotenv_set_key
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, Message,
)

from . import state as fm_state
from . import telegram as fm_tg
from . import instagram as fm_ig
from . import common as fm_common

logger = logging.getLogger(__name__)

# Serialize all archive jobs (manual, backfill, auto-loop): one friend at a
# time keeps the user account under flood limits and deliveries ordered.
_ARCHIVE_LOCK = asyncio.Lock()

_ENV_FILE = ".env"


def _persist_env(key, value):
    """Persist a knob to .env AND live-apply it on config (console-only ops
    must survive restarts — there is no ssh in this workflow)."""
    try:
        dotenv_set_key(_ENV_FILE, key, str(value))
    except Exception as e:
        logger.warning(f"[FriendMedia] could not persist {key} to .env: {e}")
    setattr(config, key, value)


def _enabled():
    return bool(getattr(config, "FRIEND_MEDIA_ENABLED", False))


def _dest_label(dest=None):
    d = dest or fm_common.resolve_destination()
    if d == "logchannel":
        return "Log channel → DM"
    if d == "me":
        return "Saved Messages"
    return str(d)


def _blurb():
    ig = "🟢" if getattr(config, "FRIEND_MEDIA_IG_ENABLED", False) else "🔴"
    sched = int(getattr(config, "FRIEND_MEDIA_SCHEDULE_MINUTES", 0) or 0)
    sched_label = f"every {sched}m" if sched > 0 else "manual only"
    return (
        "📸 **Friend Media Archiver**\n\n"
        "Archives your friends' Telegram profile pictures + stories (and, "
        "best-effort, their Instagram stories + NEW posts only — never older "
        "IG content) into a place **only you** see. It **never messages your "
        "friends** — the only thing that touches them is a silent "
        "`add_contact`.\n\n"
        f"Destination: `{_dest_label()}`\n"
        f"Instagram: {ig}\n"
        f"Auto-check: **{sched_label}**\n"
        f"Max photos/run: `{config.FRIEND_MEDIA_MAX_PHOTOS}` · "
        f"max stories/run: `{config.FRIEND_MEDIA_MAX_STORIES}`"
    )


def _menu_keyboard():
    feat = ("🔴 Disable feature" if _enabled() else "🟢 Enable feature")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Friend", callback_data="fm_add"),
         InlineKeyboardButton("📋 List Friends", callback_data="fm_list")],
        [InlineKeyboardButton("📇 Contacts", callback_data="fm_contacts"),
         InlineKeyboardButton("🚀 Check All (new)", callback_data="fm_archive_all")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="fm_settings"),
         InlineKeyboardButton(feat, callback_data="fm_toggle_enabled")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="fm_menu"),
         InlineKeyboardButton("◀️ Back to Console", callback_data="admin_main")],
    ])


async def render_menu(client, callback_query):
    await callback_query.message.edit_text(_blurb(), reply_markup=_menu_keyboard())


def _settings_keyboard():
    ig = getattr(config, "FRIEND_MEDIA_IG_ENABLED", False)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📥 Destination: {_dest_label()}", callback_data="fm_dest")],
        [InlineKeyboardButton(
            f"{'🔴 Disable' if ig else '🟢 Enable'} Instagram",
            callback_data="fm_toggle_ig_global")],
        [InlineKeyboardButton("⏱ Set check interval (min)", callback_data="fm_sched")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="fm_settings"),
         InlineKeyboardButton("◀️ Back", callback_data="fm_menu")],
    ])


def _friend_keyboard(key, friend):
    pp = "✅" if friend.get("profile_photos") else "❌"
    st = "✅" if friend.get("stories") else "❌"
    igs = "✅" if friend.get("ig_stories") else "❌"
    igp = "✅" if friend.get("ig_posts") else "❌"
    ig_en = "✅" if friend.get("ig_enabled") else "❌"
    rows = [
        [InlineKeyboardButton("🔍 Check now (new)", callback_data=f"fm_arc:{key}"),
         InlineKeyboardButton("⬇️ Full backfill", callback_data=f"fm_backfill:{key}")],
        [InlineKeyboardButton(f"📸 Pics: {pp}", callback_data=f"fm_tg_pp:{key}"),
         InlineKeyboardButton(f"📖 Stories: {st}", callback_data=f"fm_tg_st:{key}")],
        [InlineKeyboardButton(f"🟣 IG: {ig_en}", callback_data=f"fm_ig_toggle:{key}"),
         InlineKeyboardButton("✏️ Set IG @", callback_data=f"fm_ig_set:{key}")],
        [InlineKeyboardButton(f"📷 IG stories: {igs}", callback_data=f"fm_ig_s:{key}"),
         InlineKeyboardButton(f"🖼 IG posts: {igp}", callback_data=f"fm_ig_p:{key}")],
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
        n = len(friend.get("seen_photo_ids") or [])
        bits.append(f"📸pics({n})")
    if friend.get("stories"):
        bits.append("📖stories")
    if friend.get("ig_enabled") and ig:
        sub = []
        if friend.get("ig_stories"):
            sub.append("📷")
        if friend.get("ig_posts"):
            sub.append("🖼")
        bits.append(f"🟣@{ig}{''.join(sub)}")
    tog = " ".join(bits) if bits else "∅ nothing"
    mark = " ✓backfilled" if friend.get("backfilled") else ""
    return f"• `{name}` (tg:{tg or '?'}){mark} — {tog}"


def _contacts_keyboard(page, total_pages):
    rows = []
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"fm_contacts_page:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="fm_noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"fm_contacts_page:{page + 1}"))
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔎 Search contacts", callback_data="fm_contact_search"),
                 InlineKeyboardButton("📞 Add by phone", callback_data="fm_contact_phone")])
    rows.append([InlineKeyboardButton("➕ Add by id/@user", callback_data="fm_add"),
                 InlineKeyboardButton("◀️ Back", callback_data="fm_menu")])
    return InlineKeyboardMarkup(rows)


_CONTACTS_PAGE = 8


async def _render_contacts(callback_query, page=0):
    uc = fm_common.user_client()
    if uc is None:
        await callback_query.message.edit_text(
            "⚠️ Connected user account not started.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Back", callback_data="fm_menu")]]))
        return
    try:
        contacts = await uc.get_contacts()
    except Exception as e:
        logger.warning(f"[FriendMedia] get_contacts failed: {e}")
        contacts = []
    contacts = sorted(contacts or [], key=lambda u: (getattr(u, "first_name", "") or "").lower())
    total_pages = max(1, (len(contacts) + _CONTACTS_PAGE - 1) // _CONTACTS_PAGE)
    page = max(0, min(page, total_pages - 1))
    chunk = contacts[page * _CONTACTS_PAGE:(page + 1) * _CONTACTS_PAGE]
    rows = [[InlineKeyboardButton(
        (f"{getattr(c, 'first_name', '') or c.id} {('@' + c.username) if getattr(c, 'username', None) else ''}")[:40],
        callback_data=f"fm_contact_add:{c.id}")]
        for c in chunk]
    for r in rows:
        pass
    kb = InlineKeyboardMarkup(rows + _contacts_keyboard(page, total_pages).inline_keyboard)
    header = (f"📇 **Contacts** ({len(contacts)})\n\nTap a contact to ADD them as an "
              "archived friend (silent add_contact — nothing is sent to them).")
    await callback_query.message.edit_text(header, reply_markup=kb)


async def _add_friend_from_user(u):
    """Persist a resolved pyrogram User as a friend record."""
    u_id = getattr(u, "id", None)
    uname = getattr(u, "username", None)
    fname = getattr(u, "first_name", None) or (uname or str(u_id))
    key = str(u_id) if u_id else ("tg:" + (uname or fname))
    friend = {
        "platform": "telegram",
        "handle": str(u_id or uname),
        "telegram_user_id": u_id,
        "username": uname or "",
        "first_name": fname,
        "profile_photos": True,
        "stories": True,
        "ig_username": "",
        "ig_enabled": False,
        "ig_stories": True,
        "ig_posts": False,
    }
    merged = await fm_state.add_or_update_friend(key, friend)
    return key, merged


async def fm_callback_dispatch(client, callback_query):
    from modules.admin.state import USER_STATES
    data = callback_query.data
    user_id = callback_query.from_user.id

    if user_id != config.SYSTEM_CREATOR_ID:
        await callback_query.answer("Access Denied.", show_alert=True)
        return

    # ---- Menu navigation ----
    if data == "fm_noop":
        await callback_query.answer()
        return
    if data == "fm_menu":
        await render_menu(client, callback_query)
        await callback_query.answer()
        return
    if data == "fm_settings":
        await callback_query.message.edit_text(
            "⚙️ **Settings**\n\nDestination is where archived media lands (only you "
            "see it). Instagram requires a valid igcookies jar. Settings persist "
            "to `.env` automatically.",
            reply_markup=_settings_keyboard())
        await callback_query.answer()
        return
    if data == "fm_toggle_enabled":
        new = not _enabled()
        _persist_env("FRIEND_MEDIA_ENABLED", "true" if new else "false")
        # The background loop re-reads this flag each cycle — no restart needed.
        await render_menu(client, callback_query)
        await callback_query.answer("Enabled." if new else "Disabled.")
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
            "nothing is sent to them), then a one-time full profile-pic backfill "
            "starts automatically. Only YOU receive their media.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Cancel", callback_data="fm_menu")]]))
        await callback_query.answer()
        return
    if data == "fm_dest":
        USER_STATES[user_id] = "waiting_for_friend_dest"
        await callback_query.message.edit_text(
            "📥 **Set destination**\n\n"
            "• `logchannel` — post to your Log Channel, then DM you (default)\n"
            "• `saved` — your connected account's Saved Messages\n"
            "• `<chat_id>` — a numeric chat id you own",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Cancel", callback_data="fm_settings")]]))
        await callback_query.answer()
        return
    if data == "fm_sched":
        USER_STATES[user_id] = "waiting_for_friend_schedule"
        await callback_query.message.edit_text(
            "⏱ **Set auto-check interval**\n\nSend minutes between runs "
            "(0 = manual only). Applies live — no restart needed.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Cancel", callback_data="fm_settings")]]))
        await callback_query.answer()
        return
    if data == "fm_toggle_ig_global":
        _persist_env("FRIEND_MEDIA_IG_ENABLED",
                     "false" if getattr(config, "FRIEND_MEDIA_IG_ENABLED", False) else "true")
        await callback_query.message.edit_text(
            "⚙️ **Settings**", reply_markup=_settings_keyboard())
        await callback_query.answer()
        return

    # ---- Contacts tools ----
    if data == "fm_contacts":
        await callback_query.answer("Loading contacts…")
        await _render_contacts(callback_query, 0)
        return
    if data.startswith("fm_contacts_page:"):
        page = int(data.split(":", 1)[1])
        await callback_query.answer()
        await _render_contacts(callback_query, page)
        return
    if data == "fm_contact_search":
        USER_STATES[user_id] = "waiting_for_friend_search"
        await callback_query.message.edit_text(
            "🔎 **Search contacts**\n\nSend a name or username to search your "
            "connected account's contacts.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Cancel", callback_data="fm_contacts")]]))
        await callback_query.answer()
        return
    if data == "fm_contact_phone":
        USER_STATES[user_id] = "waiting_for_friend_phone"
        await callback_query.message.edit_text(
            "📞 **Add by phone number**\n\nSend an international phone number "
            "(e.g. `+15551234567`). It is imported into your connected account's "
            "contacts (silent) and added as a friend if resolvable.\n\n⚠️ Telegram "
            "limits contact imports; use sparingly.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Cancel", callback_data="fm_contacts")]]))
        await callback_query.answer()
        return
    if data.startswith("fm_contact_add:"):
        uid = int(data.split(":", 1)[1])
        uc = fm_common.user_client()
        u = None
        if uc is not None:
            try:
                contacts = await uc.get_contacts()
                u = next((c for c in contacts or [] if c.id == uid), None)
            except Exception:
                pass
        if u is None:
            await callback_query.answer("Contact not found.", show_alert=True)
            return
        await fm_common.ensure_contact(u)
        key, _ = await _add_friend_from_user(u)
        asyncio.create_task(_auto_backfill(client, key))
        await callback_query.message.edit_text(
            f"👤 **Friend**\n\n" + _label(await fm_state.get_friend(key)),
            reply_markup=_friend_keyboard(key, await fm_state.get_friend(key)))
        await callback_query.answer("Added + backfill started.")
        return

    # ---- Archive all ----
    if data == "fm_archive_all":
        await callback_query.answer("Starting…")
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

    if data.startswith("fm_arc:") or data.startswith("fm_backfill:"):
        full = data.startswith("fm_backfill:")
        key = data.split(":", 1)[1]
        await callback_query.answer("Full backfill starting…" if full else "Checking…")
        await _archive_one(client, callback_query.message, key, full=full)
        return

    async def _toggle(field):
        key = data.split(":", 1)[1]
        f = await fm_state.get_friend(key)
        if f is None:
            await callback_query.answer("Not found.", show_alert=True)
            return
        await fm_state.update_friend(key, {field: not f.get(field)})
        f = await fm_state.get_friend(key)
        await callback_query.message.edit_text(
            "👤 **Friend**\n\n" + _label(f), reply_markup=_friend_keyboard(key, f))
        await callback_query.answer()

    if data.startswith("fm_tg_pp:"):
        await _toggle("profile_photos")
        return
    if data.startswith("fm_tg_st:"):
        await _toggle("stories")
        return
    if data.startswith("fm_ig_toggle:"):
        await _toggle("ig_enabled")
        return
    if data.startswith("fm_ig_s:"):
        await _toggle("ig_stories")
        return
    if data.startswith("fm_ig_p:"):
        await _toggle("ig_posts")
        return

    if data.startswith("fm_ig_set:"):
        key = data.split(":", 1)[1]
        USER_STATES[user_id] = f"waiting_for_friend_ig:{key}"
        await callback_query.message.edit_text(
            "✏️ **Set Instagram @username** for this friend.\n\n"
            "Send the username WITHOUT the @ (e.g. `nature_lover`). Only content "
            "posted AFTER linking is delivered — no older IG history is fetched.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Cancel", callback_data=f"fm_friend:{key}")]]))
        await callback_query.answer()
        return

    if data.startswith("fm_del:"):
        key = data.split(":", 1)[1]
        await callback_query.message.edit_text(
            "🗑 **Remove this friend?** Already-archived media stays where it was "
            "delivered; only future archiving stops.",
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


async def _list_message():
    friends = await fm_state.list_friends()
    if not friends:
        return ("📋 **Friends**\n\nNo friends added yet. Tap **➕ Add Friend**, "
                "**📇 Contacts**, or **📞 Add by phone**.", _menu_keyboard())
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


async def _archive_one_friend(client, key, friend, status_msg=None, full=False):
    """Archive ONE friend under the global lock. Returns summary string."""
    parts = []
    async with _ARCHIVE_LOCK:
        try:
            if friend.get("telegram_user_id") and (friend.get("profile_photos") or friend.get("stories")):
                parts.append(await fm_tg.archive_friend_telegram(
                    key, friend, status_msg=status_msg, full=full))
            if (friend.get("ig_enabled") and friend.get("ig_username")
                    and getattr(config, "FRIEND_MEDIA_IG_ENABLED", False)):
                if friend.get("ig_stories"):
                    n = await fm_ig.archive_instagram_stories(key, friend, bot=client)
                    parts.append(f"{n} new IG stories")
                if friend.get("ig_posts"):
                    n = await fm_ig.archive_instagram_posts(key, friend, bot=client)
                    parts.append(f"{n} new IG posts")
            summary = "; ".join(p for p in parts if p) or "nothing new"
            await fm_state.update_friend(key, {"last_run": int(time.time()),
                                               "last_count": summary})
            return summary
        except Exception as e:
            logger.exception(f"[FriendMedia] archive {key} failed: {e}")
            return f"failed: {e}"


async def _run_archives(client, friends, status_msg, full=False):
    for key, friend in friends:
        label = friend.get("first_name") or friend.get("handle") or key
        try:
            summary = await _archive_one_friend(client, key, friend,
                                                status_msg=status_msg, full=full)
            await client.send_message(
                config.SYSTEM_CREATOR_ID,
                f"✅ `{label}` ({key}): {summary}")
        except Exception as e:
            logger.exception(f"[FriendMedia] archive {key} failed: {e}")
            await client.send_message(
                config.SYSTEM_CREATOR_ID,
                f"❌ Archive failed for `{key}`: {e}")
    if status_msg is not None:
        try:
            await status_msg.edit_text("🏁 Archive run finished.")
        except Exception:
            pass


async def _archive_all(client, status_msg):
    if fm_common.user_client() is None:
        await status_msg.reply_text(
            "⚠️ The connected user account (PREMIUM_STRING_SESSION) is not started. "
            "I can't read friends' profile-photo history without a user account.")
        return
    friends = await fm_state.list_friends()
    enabled = [(k, f) for k, f in friends
               if f.get("profile_photos") or f.get("stories")
               or (f.get("ig_enabled") and f.get("ig_username"))]
    if not enabled:
        await status_msg.reply_text("No friends have archiving enabled.")
        return
    msg = await status_msg.reply_text(
        f"🚀 Checking {len(enabled)} friend(s) for NEW media (delivering to "
        f"{_dest_label()})…")
    asyncio.create_task(_run_archives(client, enabled, msg))


async def _archive_one(client, status_msg, key, full=False):
    friend = await fm_state.get_friend(key)
    if friend is None:
        await status_msg.reply_text("Friend not found.")
        return
    if fm_common.user_client() is None and (friend.get("profile_photos") or friend.get("stories")):
        await status_msg.reply_text(
            "⚠️ Connected user account not started — can't archive Telegram media.")
        return
    msg = await status_msg.reply_text(
        f"⬇️ Full backfill of `{key}`…" if full else f"🔍 Checking `{key}` for new media…")
    asyncio.create_task(_run_archives(client, [(key, friend)], msg, full=full))


async def _auto_backfill(client, key):
    """One-time full backfill right after a friend is added."""
    friend = await fm_state.get_friend(key)
    if friend is None or fm_common.user_client() is None:
        return
    summary = await _archive_one_friend(client, key, friend, full=True)
    try:
        await client.send_message(
            config.SYSTEM_CREATOR_ID,
            f"⬇️ Backfill complete for `{key}`: {summary}")
    except Exception:
        pass


async def handle_friend_text(client, message, user_id, state, input_text, prompt_id, app, back_markup):
    from modules.admin.state import USER_STATES
    txt = input_text.strip()

    async def _clear_prompt():
        if prompt_id:
            try:
                await client.delete_messages(chat_id=user_id, message_ids=prompt_id)
            except Exception:
                pass

    if state == "waiting_for_friend_add":
        lines = [l.strip() for l in txt.splitlines() if l.strip()]
        added, failed = [], []
        keys = []
        for handle in lines:
            key, friend = await _resolve_and_add(handle)
            if key:
                added.append(f"`{friend.get('first_name') or handle}` (tg:{friend.get('telegram_user_id')})")
                keys.append(key)
            else:
                failed.append(handle)
        USER_STATES.pop(user_id, None)
        await _clear_prompt()
        if added:
            reply = "✅ Added:\n" + "\n".join(added)
            if failed:
                reply += "\n\n❌ Could not resolve: " + ", ".join(failed)
            reply += ("\n\n⬇️ One-time full profile-pic backfill is starting — you'll "
                      f"receive everything at {_dest_label()}. After that only NEW "
                      "media gets delivered.")
            await message.reply_text(reply, reply_markup=back_markup)
            for key in keys:
                asyncio.create_task(_auto_backfill(app, key))
        else:
            await message.reply_text(
                "❌ Could not resolve any of those. Check the id/username and try again "
                "from ➕ Add Friend.", reply_markup=back_markup)
        return

    if state == "waiting_for_friend_search":
        uc = fm_common.user_client()
        USER_STATES.pop(user_id, None)
        await _clear_prompt()
        results = []
        if uc is not None:
            try:
                results = await uc.search_contacts(txt, limit=6) or []
            except Exception as e:
                logger.info(f"[FriendMedia] search_contacts failed: {e}")
        if not results:
            await message.reply_text("❌ No contacts matched. Try 📇 Contacts instead.",
                                     reply_markup=back_markup)
            return
        rows = [[InlineKeyboardButton(
            f"{getattr(u, 'first_name', '') or u.id}"
            f"{(' @' + u.username) if getattr(u, 'username', None) else ''}",
            callback_data=f"fm_contact_add:{u.id}")] for u in results]
        rows.append([InlineKeyboardButton("◀️ Back", callback_data="fm_contacts")])
        await message.reply_text("🔎 Results — tap to add as friend:",
                                 reply_markup=InlineKeyboardMarkup(rows))
        return

    if state == "waiting_for_friend_phone":
        phone = txt.replace(" ", "").replace("-", "")
        if not phone.lstrip("+").isdigit():
            await message.reply_text("❌ Send digits only, e.g. `+15551234567`.",
                                     reply_markup=back_markup)
            return
        from pyrogram.types import InputPhoneContact
        uc = fm_common.user_client()
        USER_STATES.pop(user_id, None)
        await _clear_prompt()
        if uc is None:
            await message.reply_text("⚠️ Connected user account not started.",
                                     reply_markup=back_markup)
            return
        try:
            loop = __import__("asyncio").get_event_loop()
            first = "FM" + phone[-4:].lstrip("0") or "FM"
            contact = InputPhoneContact(phone_number=phone, first_name=first,
                                        last_name="", client_id=0)
            result = await uc.import_contacts([contact])
            users = getattr(result, "users", None) or []
        except Exception as e:
            logger.info(f"[FriendMedia] import_contacts failed: {e}")
            users = []
        if not users:
            await message.reply_text(
                "❌ Telegram could not resolve that number to an account (not "
                "registered, privacy-limited, or import limit hit).",
                reply_markup=back_markup)
            return
        u = users[0]
        await fm_common.ensure_contact(u)
        key, _ = await _add_friend_from_user(u)
        asyncio.create_task(_auto_backfill(app, key))
        await message.reply_text(
            f"✅ Added `{getattr(u, 'first_name', '') or u.id}` (tg:{u.id}) — "
            "full backfill starting.", reply_markup=back_markup)
        return

    if state.startswith("waiting_for_friend_ig:"):
        key = state.split(":", 1)[1]
        ig = txt.lstrip("@")
        await fm_state.update_friend(key, {"ig_username": ig,
                                           "ig_enabled": True,
                                           "ig_stories": True,
                                           "last_ig_media_pk": None,
                                           "seen_ig_story_pks": []})
        USER_STATES.pop(user_id, None)
        await _clear_prompt()
        await message.reply_text(
            f"✅ Instagram set to `@{ig}`. First run records a watermark — only "
            "content posted AFTER now will be delivered (no older IG history).",
            reply_markup=back_markup)
        return

    if state == "waiting_for_friend_dest":
        v = txt.strip().lower()
        if v == "logchannel":
            resolved = "logchannel"
        elif v in ("saved", "me", ""):
            resolved = "saved"
        else:
            try:
                int(v)
                resolved = v
            except Exception:
                await message.reply_text(
                    "❌ Send `logchannel`, `saved`, or a numeric chat id you own.",
                    reply_markup=back_markup)
                return
        _persist_env("FRIEND_MEDIA_DESTINATION", resolved)
        USER_STATES.pop(user_id, None)
        await _clear_prompt()
        await message.reply_text(f"✅ Destination set to **{_dest_label(resolved)}** "
                                 "(persisted to .env).", reply_markup=back_markup)
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
        _persist_env("FRIEND_MEDIA_SCHEDULE_MINUTES", mins)
        USER_STATES.pop(user_id, None)
        await _clear_prompt()
        await message.reply_text(
            f"✅ Auto-check interval set to **{mins} min** (0 = manual only). "
            "Applies live within a minute — no restart needed.",
            reply_markup=back_markup)
        return


async def _resolve_and_add(handle):
    """Resolve a handle to a Telegram user and persist the friend record.

    Returns (key, friend) or (None, None).
    """
    user = await fm_tg.resolve_telegram_user(handle)
    if user is not None:
        return await _add_friend_from_user(user)
    # A bare numeric id that the connected account can't resolve *yet* is still
    # stored — resolve_telegram_user falls back to the contacts scan at run time.
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
            "ig_stories": True,
            "ig_posts": False,
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
            "ig_stories": True,
            "ig_posts": False,
        }
        await fm_state.add_or_update_friend(key, friend)
        return key, friend
    return None, None


def _looks_like_username(h):
    return all(c.isalnum() or c in (".", "_") for c in h) and not h.isdigit()


def start_friend_media_task(app, premium_app):
    """Background auto-check loop. ALWAYS starts; it self-gates on the LIVE
    config every cycle (feature toggle + interval are read fresh), so console
    toggles apply without a restart. Interval sleeps carry ±10% jitter — fixed
    machine cadence is exactly what got accounts flagged elsewhere."""
    if premium_app is None:
        logging.info("[FriendMedia] no user account; watcher idle until configured.")

    async def _loop():
        while True:
            try:
                mins = int(getattr(config, "FRIEND_MEDIA_SCHEDULE_MINUTES", 0) or 0)
                run_now = (_enabled() and mins > 0
                           and fm_common.user_client() is not None)
                if run_now:
                    friends = await fm_state.list_friends()
                    enabled = [(k, f) for k, f in friends
                               if f.get("profile_photos") or f.get("stories")
                               or (f.get("ig_enabled") and f.get("ig_username"))]
                    if enabled:
                        await _run_archives(app, enabled, None)
                    await asyncio.sleep(int(max(mins * 60, 60) *
                                            random.uniform(0.9, 1.1)))
                else:
                    # Idle poll: re-check the gates shortly.
                    await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logging.exception(f"[FriendMedia] auto-archive loop error: {e}")
                await asyncio.sleep(60)

    logging.info("[FriendMedia] auto-check watcher started (self-gated on live config).")
    return asyncio.create_task(_loop())

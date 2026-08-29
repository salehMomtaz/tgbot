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
    sched = int(getattr(config, "FRIEND_MEDIA_SCHEDULE_MINUTES", 60) or 0)
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
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 List Friends", callback_data="fm_list_choose")],
        [InlineKeyboardButton("📇 Contacts", callback_data="fm_contacts"),
         InlineKeyboardButton("🚀 Check All", callback_data="fm_archive_all")],
        [InlineKeyboardButton("⏱ Set check interval (min)", callback_data="fm_sched")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="fm_menu"),
         InlineKeyboardButton("◀️ Back to Console", callback_data="admin_main")],
    ])


async def render_menu(client, callback_query):
    await callback_query.message.edit_text(_blurb(), reply_markup=_menu_keyboard())


def _settings_keyboard():
    """Settings submenu (kept for back-compat with the dead-handler path
    fm_settings below; not currently reachable from the main menu — the
    operator uses the top-level 'Set check interval' button on the main
    Friend Media menu and the .env file for everything else)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏱ Set check interval (min)", callback_data="fm_sched")],
        [InlineKeyboardButton("◀️ Back", callback_data="fm_menu")],
    ])


def _friend_platform(friend):
    """Return the friend's canonical platform: 'ig' when it is an Instagram
    friend, else 'tg'. A record is IG only when it carries an ig_username AND is
    not a Telegram entity (platform=="instagram" or no telegram_user_id)."""
    if (friend or {}).get("ig_username") and (friend.get("platform") == "instagram"
                                              or not friend.get("telegram_user_id")):
        return "ig"
    return "tg"


def _friend_keyboard(key, friend):
    """Platform-SPECIFIC per-friend menu: a Telegram friend menu never shows IG
    controls, and an Instagram friend menu never shows Telegram controls. The
    two are modelled separately by design (operator adds the same real person to
    each list independently)."""
    plat = _friend_platform(friend)
    if plat == "ig":
        igs = "✅" if friend.get("ig_stories") else "❌"
        igp = "✅" if friend.get("ig_posts") else "❌"
        rows = [
            [InlineKeyboardButton("🔍 Check now", callback_data=f"fm_arc:{key}")],
            [InlineKeyboardButton(f"📷 IG stories: {igs}", callback_data=f"fm_ig_s:{key}"),
             InlineKeyboardButton(f"🖼 IG posts: {igp}", callback_data=f"fm_ig_p:{key}")],
            [InlineKeyboardButton("🗂 Archive (zip)", callback_data=f"fm_ig_archive:{key}")],
            [InlineKeyboardButton("🗑 Remove", callback_data=f"fm_del:{key}"),
             InlineKeyboardButton("◀️ Back", callback_data="fm_list_choose")],
        ]
        return InlineKeyboardMarkup(rows)

    pp = "✅" if friend.get("profile_photos") else "❌"
    st = "✅" if friend.get("stories") else "❌"
    rows = [
        [InlineKeyboardButton("🔍 Check now", callback_data=f"fm_arc:{key}"),
         InlineKeyboardButton("⬇️ Full backfill", callback_data=f"fm_backfill:{key}")],
        [InlineKeyboardButton(f"📸 Pics: {pp}", callback_data=f"fm_tg_pp:{key}"),
         InlineKeyboardButton(f"📖 Stories: {st}", callback_data=f"fm_tg_st:{key}")],
        [InlineKeyboardButton("🗑 Remove", callback_data=f"fm_del:{key}"),
         InlineKeyboardButton("◀️ Back", callback_data="fm_list_choose")],
    ]
    return InlineKeyboardMarkup(rows)


def _label(friend):
    plat = _friend_platform(friend)
    if plat == "ig":
        ig = friend.get("ig_username")
        sub = []
        if friend.get("ig_stories"):
            sub.append("📷")
        if friend.get("ig_posts"):
            sub.append("🖼")
        bits = f"🟣@{ig}{''.join(sub)}"
        return f"• `@{ig}`{''.join(' '+s for s in sub)}"
    name = friend.get("first_name") or friend.get("username") or friend.get("handle") or "?"
    tg = friend.get("telegram_user_id")
    bits = []
    if friend.get("profile_photos"):
        n = len(friend.get("seen_photo_ids") or [])
        bits.append(f"📸pics({n})")
    if friend.get("stories"):
        bits.append("📖stories")
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
    # Use the real account name when we have one. The previous fallback chain
    # `(uname or str(u_id))` would persist "1234567890" as the first_name when
    # the Telegram account has no display name set — the operator saw this as
    # a placeholder. Prefer @username (a public identifier, not a number), and
    # only fall back to the user id as a last resort so the friend record
    # always has a display label.
    fname = (
        (getattr(u, "first_name", None) or "").strip()
        or (uname or "").strip()
        or (str(u_id) if u_id else "")
    )
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
    if data == "fm_list_choose":
        txt, kb = await _list_choose_message()
        await callback_query.message.edit_text(txt, reply_markup=kb)
        await callback_query.answer()
        return
    if data == "fm_list":
        # Backwards-compatible entry: show the platform chooser.
        txt, kb = await _list_choose_message()
        await callback_query.message.edit_text(txt, reply_markup=kb)
        await callback_query.answer()
        return
    if data.startswith("fm_list_view:"):
        plat = data.split(":", 1)[1]
        await _render_friend_list(callback_query, plat)
        await callback_query.answer()
        return
    if data.startswith("fm_list_sel:"):
        plat = data.split(":", 1)[1]
        await _render_friend_list(callback_query, plat, select_mode=True)
        await callback_query.answer()
        return
    if data.startswith("fm_sel_toggle:"):
        _, plat, key = data.split(":", 2)
        sel = _SELECT.setdefault(user_id, {"plat": plat, "selected": set()})
        if key in sel["selected"]:
            sel["selected"].discard(key)
        else:
            sel["selected"].add(key)
        await _render_friend_list(callback_query, plat, select_mode=True)
        await callback_query.answer()
        return
    if data.startswith("fm_sel_delete:"):
        plat = data.split(":", 1)[1]
        sel = _SELECT.get(user_id, {"plat": plat, "selected": set()})
        keys = list(sel.get("selected") or [])
        for key in keys:
            await fm_state.remove_friend(key)
        sel["selected"] = set()
        await _render_friend_list(callback_query, plat)
        await callback_query.answer(f"Deleted {len(keys)} friend(s).")
        return
    if data.startswith("fm_delall_ask:"):
        plat = data.split(":", 1)[1]
        friends = await fm_state.list_friends()
        items = _split_friends(friends, plat)
        title = "🟣 IG Friends" if plat == "ig" else "📋 TG Friends"
        await callback_query.message.edit_text(
            f"🗑 **Delete ALL {len(items)} {title}?**\n\nThis removes them from the "
            "archiver; already-delivered media stays. This cannot be undone.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, delete all",
                                      callback_data=f"fm_delall_confirm:{plat}")],
                [InlineKeyboardButton("❌ Cancel", callback_data=f"fm_list_view:{plat}")]]))
        await callback_query.answer()
        return
    if data.startswith("fm_delall_confirm:"):
        plat = data.split(":", 1)[1]
        friends = await fm_state.list_friends()
        items = _split_friends(friends, plat)
        for key, _f in items:
            await fm_state.remove_friend(key)
        _SELECT.pop(user_id, None)
        await _render_friend_list(callback_query, plat)
        await callback_query.answer(f"Deleted {len(items)} friend(s).")
        return
    if data == "fm_add":
        await callback_query.message.edit_text(
            "➕ **Add Friend**\n\nWhich platform?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Telegram friend", callback_data="fm_add_tg")],
                [InlineKeyboardButton("🟣 Instagram friend", callback_data="fm_add_ig")],
                [InlineKeyboardButton("◀️ Cancel", callback_data="fm_menu")]]))
        await callback_query.answer()
        return
    if data == "fm_add_tg":
        USER_STATES[user_id] = "waiting_for_friend_add"
        await callback_query.message.edit_text(
            "➕ **Add Telegram Friend**\n\nSend the friend's **@username** "
            "or **phone number** (e.g. `+15551234567` or `15551234567` — "
            "with or without the leading `+`; one per line to add several).\n\n"
            "Resolution (silent — no message is sent to the friend):\n"
            "  • `@username` → looked up by the connected account\n"
            "  • Phone number → `contacts.ImportContacts` returns the real id\n\n"
            "A one-time full profile-pic backfill starts automatically. Only YOU "
            "receive their media.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Cancel", callback_data="fm_menu")]]))
        await callback_query.answer()
        return
    if data == "fm_add_ig":
        USER_STATES[user_id] = "waiting_for_friend_add_ig"
        await callback_query.message.edit_text(
            "➕ **Add Instagram Friend**\n\nSend the Instagram **username** "
            "(e.g. `nature_lover` or `@nature_lover` — both work, the leading "
            "`@` is optional; one per line to add several).\n\n"
            "They are added as IG friends — NEW stories + posts (posted after "
            "linking) are delivered. The current profile picture is also "
            "delivered once on the first cycle. No older IG history is fetched, "
            "and nothing is ever sent to them.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Cancel", callback_data="fm_menu")]]))
        await callback_query.answer()
        return
    if data == "fm_sched":
        USER_STATES[user_id] = "waiting_for_friend_schedule"
        await callback_query.message.edit_text(
            "⏱ **Set auto-check interval**\n\nSend minutes between runs "
            "(0 = manual only). Applies live — no restart needed.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Cancel", callback_data="fm_menu")]]))
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
        await callback_query.answer("Full backfill starting..." if full else "Checking...")
        await _archive_one(client, callback_query.message, key, full=full)
        return

    if data.startswith("fm_ig_archive:"):
        key = data.split(":", 1)[1]
        friend = await fm_state.get_friend(key)
        if friend is None or not (friend.get("ig_username")):
            await callback_query.answer("IG @username not set for this friend.", show_alert=True)
            return
        await callback_query.answer("Starting IG archive (zip)…")
        asyncio.create_task(_run_ig_archive(client, callback_query.message, key, friend))
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
    if data.startswith("fm_ig_s:"):
        await _toggle("ig_stories")
        return
    if data.startswith("fm_ig_p:"):
        await _toggle("ig_posts")
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
        txt, kb = await _list_choose_message()
        await callback_query.message.edit_text(txt, reply_markup=kb)
        await callback_query.answer("Removed.")
        return

    await callback_query.answer()


def _platform_of(friend):
    """Which list a friend belongs to. TG and IG are modelled as SEPARATE
    friend records — a friend is either TG (telegram identity) or IG (instagram
    identity), never both. (Operator adds the same real person to each list
    independently when they exist on both.)"""
    if _friend_platform(friend) == "ig":
        return ("ig", None)
    return (None, "tg")


def _split_friends(friends, plat):
    """Return [(key, friend), ...] for the requested platform list ('tg'|'ig')."""
    out = []
    for key, f in friends:
        ig, tg = _platform_of(f)
        if plat == "ig" and ig:
            out.append((key, f))
        elif plat == "tg" and tg:
            out.append((key, f))
    return out


def _list_name(friend, key):
    if (friend or {}).get("ig_username"):
        return f"@{friend['ig_username']}"
    return (friend.get("first_name") or friend.get("handle") or friend.get("username") or key)[:32]


# Multi-select delete session state, keyed by creator id → {plat, selected:set()}.
_SELECT = {}


async def _render_friend_list(callback_query, plat, select_mode=False):
    """Render one platform list ('tg'|'ig') with optional multi-select."""
    friends = await fm_state.list_friends()
    items = _split_friends(friends, plat)
    user_id = callback_query.from_user.id
    sel = _SELECT.setdefault(user_id, {"plat": plat, "selected": set()})
    if sel.get("plat") != plat:
        sel["selected"] = set()
        sel["plat"] = plat

    title = "🟣 IG Friends" if plat == "ig" else "📋 TG Friends"
    add_cb = "fm_add_tg" if plat == "tg" else "fm_add_ig"
    add_label = "➕ Add TG Friend" if plat == "tg" else "➕ Add IG Friend"
    if not items:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(add_label, callback_data=add_cb),
             InlineKeyboardButton("◀️ Back", callback_data="fm_list_choose")]])
        header = (f"{title} (0)\n\nNo friends here yet.")
        if select_mode:
            await callback_query.answer("Selection mode", show_alert=False)
        await callback_query.message.edit_text(header, reply_markup=kb)
        return

    rows = []
    for key, f in items:
        name = _list_name(f, key)
        if select_mode:
            mark = "✅ " if key in sel["selected"] else ""
            cb = f"fm_sel_toggle:{plat}:{key}"
        else:
            # Non-select mode: tapping a friend OPENS its menu (not selection).
            mark = ""
            cb = f"fm_friend:{key}"
        rows.append([InlineKeyboardButton(f"{mark}{name}", callback_data=cb)])

    ctrl = []
    if select_mode:
        ctrl.append([InlineKeyboardButton(
            f"🗑 Delete selected ({len(sel['selected'])})",
            callback_data=f"fm_sel_delete:{plat}"),
            InlineKeyboardButton("✅ Done", callback_data=f"fm_list_view:{plat}")])
    else:
        ctrl.append([InlineKeyboardButton("☑️ Select", callback_data=f"fm_list_sel:{plat}"),
                     InlineKeyboardButton(f"🗑 Delete all ({len(items)})",
                                          callback_data=f"fm_delall_ask:{plat}")])
    ctrl.append([InlineKeyboardButton(add_label, callback_data=add_cb),
                 InlineKeyboardButton("◀️ Back", callback_data="fm_list_choose")])
    rows.extend(ctrl)
    header = (f"{title} ({len(items)})\n\n" +
              ("Tap friends to toggle selection, then confirm." if select_mode
               else "Tap a friend to open it; use Select / Delete all for bulk actions."))
    await callback_query.message.edit_text(header, reply_markup=InlineKeyboardMarkup(rows))


async def _list_choose_message():
    return ("📋 **Friends**\n\nChoose a platform to manage:", InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 TG Friends", callback_data="fm_list_view:tg")],
        [InlineKeyboardButton("🟣 IG Friends", callback_data="fm_list_view:ig")],
        [InlineKeyboardButton("◀️ Back", callback_data="fm_menu")],
    ]))


async def _archive_one_friend(client, key, friend, status_msg=None, full=False):
    """Archive ONE friend under the global lock. Returns ``(delivered, summary)``.

    ``delivered`` is the count of NEW media items actually delivered to the
    destination (the operator-visible signal — anything > 0 means "something
    new arrived, ping the admin in DM"). ``summary`` is the human-readable
    per-channel breakdown preserved for ``last_count`` and backfill messages.
    """
    parts = []
    delivered_total = 0
    async with _ARCHIVE_LOCK:
        try:
            # Telegram archiving is gated on ACTUAL Telegram identity: a friend
            # with a telegram_user_id, or a platform=="telegram" record that
            # archive_friend_telegram can self-heal by re-resolving. An IG-ONLY
            # friend (platform=="instagram", no id) must NOT be run through the
            # Telegram path — that would wrongly report "could not resolve ... to
            # a Telegram account".
            is_tg = (friend.get("platform") == "telegram"
                     or bool(friend.get("telegram_user_id")))
            if is_tg and (friend.get("profile_photos") or friend.get("stories")):
                tg_summary = await fm_tg.archive_friend_telegram(
                    key, friend, status_msg=status_msg, full=full)
                parts.append(tg_summary)
                delivered_total += _count_new_in_summary(tg_summary)
            if (friend.get("ig_enabled") and friend.get("ig_username")
                    and getattr(config, "FRIEND_MEDIA_IG_ENABLED", False)):
                # One-time profile picture on the first cycle (per
                # operator's "current stories + profile picture on add"
                # request). After the first successful delivery we set
                # ``ig_profile_pic_delivered`` so subsequent hourly
                # cycles only deliver new stories + posts.
                if not friend.get("ig_profile_pic_delivered"):
                    try:
                        ok = await fm_ig.archive_instagram_profile_pic(
                            key, friend, bot=client)
                        if ok:
                            parts.append("1 IG profile picture")
                            delivered_total += 1
                            await fm_state.update_friend(
                                key, {"ig_profile_pic_delivered": True})
                    except fm_ig.IGUnavailable as e:
                        parts.append(f"⚠️ IG profile picture skipped: {e}")
                if friend.get("ig_stories"):
                    try:
                        n = await fm_ig.archive_instagram_stories(key, friend, bot=client)
                        parts.append(f"{n} new IG stories")
                        delivered_total += int(n or 0)
                    except fm_ig.IGUnavailable as e:
                        parts.append(f"⚠️ IG stories skipped: {e}")
                if friend.get("ig_posts"):
                    try:
                        n = await fm_ig.archive_instagram_posts(key, friend, bot=client)
                        parts.append(f"{n} new IG posts")
                        delivered_total += int(n or 0)
                    except fm_ig.IGUnavailable as e:
                        parts.append(f"⚠️ IG posts skipped: {e}")
            summary = "; ".join(p for p in parts if p) or "nothing new"
            await fm_state.update_friend(key, {"last_run": int(time.time()),
                                               "last_count": summary})
            return delivered_total, summary
        except Exception as e:
            logger.exception(f"[FriendMedia] archive {key} failed: {e}")
            return 0, f"failed: {e}"


def _count_new_in_summary(text):
    """Pull the leading integer from "N new ..." style fragments of a summary.

    The Telegram path returns strings like
    ``"0 new profile pics (new; page scanned), 2 new stories"``; we sum the
    ``N new`` counts to know whether anything was actually delivered. Fragments
    that don't match the pattern (mode hints, warnings) contribute 0.
    """
    if not text:
        return 0
    import re
    total = 0
    for m in re.finditer(r"(\d+)\s+new\b", text):
        try:
            total += int(m.group(1))
        except (TypeError, ValueError):
            pass
    return total


async def _run_archives(client, friends, status_msg, full=False):
    # Aggregate the cycle's outcome so the final status message is honest about
    # what actually happened (no more "🏁 Archive run finished." that reads as
    # "we downloaded all of their stuff").  The per-friend detail still goes
    # to the DM (if new) or the log channel (if not) — only the summary line
    # lands in the in-chat status message.
    total_new = 0
    checked = 0
    failed_keys = []
    for key, friend in friends:
        label = friend.get("first_name") or friend.get("handle") or key
        try:
            delivered, summary = await _archive_one_friend(
                client, key, friend, status_msg=status_msg, full=full)
            checked += 1
            total_new += delivered
            if delivered > 0:
                # Something new arrived — ping the admin in DM with the media
                # already delivered to the safe destination by the archive_*
                # helpers (profile pics/stories/IG posts/stories).
                await client.send_message(
                    config.SYSTEM_CREATOR_ID,
                    f"✅ `{label}` ({key}): {summary}")
            else:
                # Quiet cycle: no new material. Mirror the per-friend summary
                # to the LOG_CHANNEL_ID (via the root logger / TelegramChannelHandler)
                # instead of spamming the admin's DM. The full record is still
                # in last_count for the console.
                logger.info(
                    f"[FriendMedia] `{label}` ({key}): {summary} (no new material)")
        except Exception as e:
            logger.exception(f"[FriendMedia] archive {key} failed: {e}")
            failed_keys.append(key)
            await client.send_message(
                config.SYSTEM_CREATOR_ID,
                f"❌ Archive failed for `{key}`: {e}")
    if status_msg is not None:
        try:
            if checked == 0 and failed_keys:
                await status_msg.edit_text(
                    f"❌ Archive run failed for {len(failed_keys)} friend(s).")
            elif total_new == 0:
                # The honest "no new content" answer the operator asked for:
                # we DID scan every friend, we just found nothing new. Listed
                # by name so the operator can see who was checked.
                names = ", ".join(
                    f"`{friend.get('first_name') or friend.get('handle') or k}`"
                    for k, friend in friends
                )
                if len(names) > 200:
                    names = f"{checked} friend(s)"
                await status_msg.edit_text(
                    f"🟰 Checked {checked} friend(s) — no new content. ({names})")
            else:
                await status_msg.edit_text(
                    f"✅ Checked {checked} friend(s) — **{total_new}** new item(s) "
                    f"delivered to {_dest_label()}.")
        except Exception:
            pass


async def _archive_all(client, status_msg):
    _uc = fm_common.user_client()
    if _uc is None or not getattr(_uc, "is_initialized", False):
        await status_msg.reply_text(
            "⚠️ The connected user account (PREMIUM_STRING_SESSION) is missing or "
            "still starting. I can't read friends' profile-photo history without "
            "it — try again in a minute.")
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
    delivered, summary = await _archive_one_friend(client, key, friend, full=True)
    try:
        await client.send_message(
            config.SYSTEM_CREATOR_ID,
            f"⬇️ Backfill complete for `{key}`: {summary}")
    except Exception:
        pass


async def _ig_add_archive(client, key):
    """One-time IG profile-pic + live-stories archive right after a friend is
    added on the IG side. Mirrors the TG-side ``_auto_backfill`` pattern.

    Per the operator's "current stories + profile picture on add" flow:
    ``archive_instagram_profile_pic`` (one-time, gated by the
    ``ig_profile_pic_delivered`` flag in the friend record) and
    ``archive_instagram_stories`` (the live 24h-window stories — anything
    older is already expired server-side, so the very first
    ``cl.user_stories(pk)`` call returns exactly what the operator
    wanted). The posts watermark is still set so older posts are NOT
    backfilled.

    If the IG session is stale (LoginRequired mid-flow), the inner
    archive will silently return "nothing new" because both
    profile-pic and stories are best-effort and raise IGUnavailable
    on failure. We surface that explicitly: a session-expired alert
    is sent to the operator's chat so the operator knows to re-upload
    a fresh igcookies.txt (not "the bot is broken" — the cookies
    were rotated by IG's anti-automation layer).
    """
    from modules.friend_media.instagram import IGUnavailable
    friend = await fm_state.get_friend(key)
    if friend is None:
        return
    delivered = 0
    summary = ""
    session_dead = False
    try:
        delivered, summary = await _archive_one_friend(
            client, key, friend, full=False)
    except IGUnavailable as e:
        # IG session went bad between the user-uploaded cookies and the
        # add-archive task. Surface this explicitly so the operator knows
        # to re-upload fresh cookies, instead of seeing "nothing new" and
        # wondering if the friend has no stories.
        session_dead = True
        logger.warning(f"[FriendMedia] IG add-archive for {key} session expired: {e}")
    except Exception as e:
        logger.warning(f"[FriendMedia] IG add-archive for {key} failed: {e}")
    if delivered > 0 or (summary and summary not in ("nothing new", "nothing enabled")):
        try:
            await client.send_message(
                config.SYSTEM_CREATOR_ID,
                f"📸 IG first archive for `{key}`: {summary}")
        except Exception:
            pass
    if session_dead:
        try:
            await client.send_message(
                config.SYSTEM_CREATOR_ID,
                f"⚠️ **IG session expired before the `{key}` first archive could run.**\n\n"
                f"`{e}`\n\n"
                f"The friend was added, but the profile pic + live stories weren't "
                f"delivered. Re-upload a fresh `igcookies.txt` via Admin → 🍪 Cookie Jars "
                f"(the mobile-app export has the device-binding cookies that survive "
                f"IG's anti-automation layer), and the next auto-check cycle will pick "
                f"up the pending archive for `{key}` automatically.",
            )
        except Exception:
            pass


async def _run_ig_archive(client, status_msg, key, friend):
    """Run a full IG archive (-> zip) for one friend, streaming progress to a
    status message and delivering the zip to the safe destination."""
    ig_user = (friend.get("ig_username") or "").lstrip("@")

    msg = None
    try:
        msg = await status_msg.reply_text(f"🗂 Starting full IG archive of @{ig_user}…")
    except Exception:
        pass

    status = msg or status_msg
    try:
        await status.edit_text(f"🗂 Archiving @{ig_user} … starting")

        async def _cb(step):
            try:
                await status.edit_text(f"🗂 Archiving @{ig_user} … {step}")
            except Exception:
                pass

        zip_path = await fm_ig.archive_instagram_full(key, friend, bot=client, status_cb=_cb)
        await status.edit_text(
            f"✅ IG archive of @{ig_user} delivered.\n"
            f"Profile pic + posts + reels + highlights are in the zip.")
    except Exception as e:
        logger.exception(f"[FriendMedia] IG archive for @{ig_user} failed: {e}")
        try:
            await status.edit_text(f"❌ IG archive of @{ig_user} failed: {e}")
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
                # The ladder tried id → username → phone (when phone-shaped)
                # and none returned a user. Telegram's API refused the lookup
                # (PEER_ID_INVALID, USER_ID_INVALID, no public username, or
                # the target has "find me by phone" set to Nobody). The number
                # could also just not be on Telegram.
                reply += ("\n\n❌ Could not resolve: " + ", ".join(failed)
                          + " — Telegram's API returned no user for those "
                            "(unknown id, no public username, or the number "
                            "isn't on Telegram / has 'find me by phone' disabled).")
            reply += ("\n\n⬇️ One-time full profile-pic backfill is starting — you'll "
                      f"receive everything at {_dest_label()}. After that only NEW "
                      "media gets delivered.")
            await message.reply_text(reply, reply_markup=back_markup)
            for key in keys:
                asyncio.create_task(_auto_backfill(app, key))
        else:
            reply = ("❌ Could not resolve any of those. Telegram's API returned "
                     "no user for any of them (unknown id, no public username, "
                     "or the number isn't on Telegram / has 'find me by phone' "
                     "disabled). Check the values and try again from ➕ Add Friend.")
            await message.reply_text(reply, reply_markup=back_markup)
        return

    if state == "waiting_for_friend_add_ig":
        lines = [l.strip().lstrip("@") for l in txt.splitlines() if l.strip()]
        USER_STATES.pop(user_id, None)
        await _clear_prompt()
        added, failed = [], []
        keys = []
        for ig in lines:
            key, friend = await _add_ig_friend(ig)
            if key:
                added.append(f"`@{ig}`")
                keys.append(key)
            else:
                failed.append(ig)
        if added:
            reply = "✅ Added IG friend(s):\n" + "\n".join(added)
            reply += ("\n\n📸 Profile picture + currently-live stories are being "
                      "delivered to your destination right now (anything in the "
                      "24h window). After that, only NEW stories/posts (posted "
                      "after now) get delivered — older IG history is never "
                      "backfilled.\n\n"
                      "Anything that exists on the friend's profile (profile pic, "
                      "current stories, current feed posts, reels, highlights, "
                      "past posts) can be downloaded on demand via the "
                      "🗂 Archive (zip) button on the friend's card. The auto-monitor "
                      "itself only delivers NEW material going forward — the on-demand "
                      "zip is the one-shot way to grab a full snapshot.\n\n"
                      "⏳ Running the first archive in the background. If the IG "
                      "session is stale, you'll see a session-expired message — "
                      "re-upload a fresh igcookies.txt via Admin → 🍪 Cookie Jars "
                      "and the next cycle will pick it up.")
            if failed:
                reply += "\n\n❌ Could not add: " + ", ".join(failed)
        else:
            reply = "❌ Could not add any IG friend. Check the username and try again."
        await message.reply_text(reply, reply_markup=back_markup)
        # Kick off the one-time profile-pic + live-stories archive for each
        # new IG friend. Same pattern as the TG-side _auto_backfill: this
        # is best-effort and silent on failure (the operator already saw
        # the IG cookies warning if the session is stale).
        for key in keys:
            asyncio.create_task(_ig_add_archive(app, key))
        return

    if state == "waiting_for_friend_search":
        uc = fm_common.user_client()
        USER_STATES.pop(user_id, None)
        await _clear_prompt()
        results = []
        if uc is not None:
            try:
                # search_contacts returns a FoundContacts object (`.users`), NOT a
                # plain list — iterating the object directly raised
                # "'FoundContacts' object is not iterable" and surfaced as a
                # generic "Something went wrong" to the operator.
                found = await uc.search_contacts(txt, limit=6)
                results = list(getattr(found, "users", None) or [])
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
        uc = fm_common.user_client()
        USER_STATES.pop(user_id, None)
        await _clear_prompt()
        if uc is None:
            await message.reply_text("⚠️ Connected user account not started.",
                                     reply_markup=back_markup)
            return
        u = await _resolve_phone_to_user(phone)
        if u is None:
            await message.reply_text(
                "❌ Telegram could not resolve that number to an account (not "
                "registered, has 'find me by phone' disabled, or import limit hit).",
                reply_markup=back_markup)
            return
        await fm_common.ensure_contact(u)
        key, _ = await _add_friend_from_user(u)
        asyncio.create_task(_auto_backfill(app, key))
        await message.reply_text(
            f"✅ Added `{getattr(u, 'first_name', '') or u.id}` (tg:{u.id}) — "
            "full backfill starting.", reply_markup=back_markup)
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


async def _resolve_phone_to_user(phone):
    """Resolve a phone-number string to a Telegram ``User`` via
    ``contacts.ResolvePhone`` (raw MTProto function).

    Returns the User, or None if Telegram could not resolve the number
    (unregistered, privacy-locked against phone lookup, or daily import limit
    exhausted). The Operator gets a single "could not resolve" message either
    way — the API can't distinguish the three failure modes.

    Normalisation: the raw function expects E.164 with a leading ``+``.
    Operators commonly paste a bare digit string (no ``+``), so we auto-add
    the ``+`` when missing. ``+15551234567`` and ``15551234567`` are now
    equivalent inputs.

    Why ``contacts.ResolvePhone`` and not ``contacts.ImportContacts``:
    ``ImportContacts`` creates a contact entry under whatever name we send
    as the placeholder. Telegram echoes that placeholder back as the
    User's ``first_name`` for the imported contact, and ``get_users`` on
    the resulting peer also returns the placeholder name (not the
    account's real display name). A follow-up ``get_users(id)`` second
    pass does NOT recover the real name — the address book entry
    shadows the account name. ``ResolvePhone`` is the only API that
    returns the canonical account info (real ``first_name`` /
    ``last_name`` / ``username``) without creating a contact entry.
    Once we have the real User, the caller adds the contact via
    ``ensure_contact`` under the real name.
    """
    from pyrogram import raw
    from pyrogram.types import User as UserType
    uc = fm_common.user_client()
    if uc is None:
        return None
    norm = phone.replace(" ", "").replace("-", "")
    if not norm.lstrip("+").isdigit():
        return None
    if not norm.startswith("+"):
        norm = "+" + norm
    try:
        r = await uc.invoke(raw.functions.contacts.ResolvePhone(phone=norm))
    except Exception as e:
        logger.info(f"[FriendMedia] contacts.ResolvePhone({norm}) failed: {e}")
        return None
    raw_users = list(getattr(r, "users", None) or [])
    if not raw_users:
        return None
    # Convert the first raw User to the high-level User the rest of the
    # code expects. User._parse is async but takes no I/O — it just
    # builds the high-level object from the raw type.
    try:
        parsed = await UserType._parse(uc, raw_users[0])
    except Exception as e:
        logger.info(
            f"[FriendMedia] User._parse after ResolvePhone failed: {e}"
        )
        return None
    if parsed is None:
        return None
    return parsed


def _looks_like_phone(handle):
    """Heuristic — input is a phone-number-shaped string.

    NOT used as a hard discriminator: the actual discriminator is whether
    ``import_contacts`` returns a user. This only decides whether the
    try-as-phone fallback is worth running on a digit-only input. We accept:
      * any string with a leading "+" and only digits after
      * 7-15 raw digits (E.164 ceiling), no leading 0 (country code required)
    Old short Telegram user ids (e.g. ``100``, ``1000``) are < 7 digits and so
    skip the phone fallback; long ids (>10 digits) are still attempted as ids
    first and only fall through to phone on PEER_ID_INVALID.
    """
    if not handle:
        return False
    s = handle.strip().replace(" ", "").replace("-", "")
    if not s:
        return False
    if s.startswith("+"):
        return s[1:].isdigit() and 1 <= len(s) - 1 <= 15
    if s.isdigit() and 7 <= len(s) <= 15 and not s.startswith("0"):
        return True
    return False


async def _resolve_and_add(handle):
    """Resolve a handle to a Telegram user and persist the friend record.

    Resolution ladder (in order, until one succeeds):
      1. ``get_users(int(h))`` if ``h`` looks like a numeric id — succeeds
         when the connected user account already knows the peer (it's a real
         user id, the user is in the account's contacts, or they share a
         dialog). PEER_ID_INVALID is the expected miss.
      2. ``get_users(h)`` for ``@username`` / bare username — succeeds when
         the user has a public username resolvable by the account.
      3. ``contacts.ImportContacts`` if the input also looks like a phone
         number (has a leading ``+`` or is 7-15 raw digits) — succeeds when
         the phone is registered to a Telegram user who hasn't disabled
         "find me by phone number". This is the only path that can resolve
         a phone number; the previous digit-count heuristic (>10 digits →
         phone) was unreliable (some countries have 7-digit numbers, some
         old Telegram accounts have very short ids).

    Returns ``(key, friend)`` on success, or ``(None, None)`` if no path
    matched (Telegram refused the lookup, the user doesn't exist, or the
    privacy settings blocked it). The caller surfaces an honest
    "could not resolve" error in that case.
    """
    user = await fm_tg.resolve_telegram_user(handle)
    if user is not None:
        # Even when id/username lookup succeeded, make sure the peer is in
        # the connected account's address book — ensures subsequent
        # get_chat_photos / get_chat_stories can resolve the peer on every
        # cycle without depending on the original lookup path. ensure_contact
        # is best-effort and silent.
        await fm_common.ensure_contact(user)
        return await _add_friend_from_user(user)
    # Last resort: try the input as a phone number. The check is heuristic;
    # the real discriminator is the API call — if Telegram refuses (empty
    # users list) we still fail honestly instead of persisting a dead record.
    if _looks_like_phone(handle):
        phone_user = await _resolve_phone_to_user(handle)
        if phone_user is not None:
            # Mirror the id/username path: add to the address book so
            # archive_telegram_profile_photos / archive_telegram_stories
            # (which call get_chat_photos / get_chat_stories on the
            # connected account) can resolve the peer on every cycle.
            await fm_common.ensure_contact(phone_user)
            return await _add_friend_from_user(phone_user)
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


async def _add_ig_friend(ig_username):
    """Add an Instagram-only friend keyed by ``ig:<username>``. First run primes
    the posts watermark (via the normal archive path) and delivers nothing older."""
    ig = (ig_username or "").lstrip("@").strip()
    if not ig:
        return None, None
    key = "ig:" + ig
    friend = {
        "platform": "instagram",
        "handle": ig,
        "telegram_user_id": None,
        "username": "",
        "first_name": ig,
        "profile_photos": False,
        "stories": False,
        "ig_username": ig,
        "ig_enabled": True,
        "ig_stories": True,
        "ig_posts": True,
    }
    await fm_state.add_or_update_friend(key, friend)
    return key, friend


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

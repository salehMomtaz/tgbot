"""Channel force-join verification for the free tier.

Provides:
- ``_greeting_text()`` / ``build_greeting_keyboard()`` — build a single,
  self-contained /start greeting: the normal intro guide PLUS, when channel
  force-join is required, an appendix (which channels to join) and an inline
  "✅ I joined — verify" button. The /start handler must send exactly ONE
  message (never a second generic greeting).
- ``register_join_handlers(app)`` — wires the ``chkjoin:`` callback so tapping
  "I joined" re-checks membership live and tells the user the verdict.

The free-access gate itself lives in ``utils/subscription/access.py``; this
module only renders the verification UI and performs an on-demand re-check.
"""

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from utils.subscription.store import get_settings, get_channels
from utils.subscription.access import check_all_channels, check_access


def _channel_rows():
    """Return (join_lines, url_rows) for the configured channels."""
    chans = get_channels()
    lines, url_rows = [], []
    for ch in chans:
        cuser = ch.get("username") or ""
        cid = ch.get("id", 0)
        if cuser:
            link = f"https://t.me/{cuser.lstrip('@')}"
            lines.append(f"• {cuser} — {link}")
            url_rows.append([InlineKeyboardButton(f"📢 Join {cuser}", url=link)])
        else:
            lines.append(f"• channel `{cid}`")
    return lines, url_rows


def _greeting_text(user_id: int = 0) -> str:
    """The normal intro greeting (always shown)."""
    return (
        "👋 **Hello! Welcome to your Private Downloader Bot.**\n\n"
        "**What I can do:**\n"
        "• Send me any **YouTube, Instagram, TikTok, or X/Twitter** link → I download & upload it.\n"
        "• Send any **direct file URL** (mp4, zip, pdf, …) → I upload it to Telegram.\n"
        "• Forward me a **Telegram file** (video, document, music) → I generate an instant stream link.\n\n"
        "**Need a hand?**\n"
        "• `/subscription` — plans, quota, subscribe (Stars / TON)\n"
        "• `/quota` — how many downloads left today\n"
        "• `/search <q>` · `/user <u>` · `/trend` — GitHub\n"
        "• `/yt <q>` `/ytrecent @ch` `/ytch @ch <q>` `/transcript <url>` — YouTube\n"
        "• `/tr src:dst text` — translate · `/web <url>` — page → Markdown\n"
        "• `/start` — this guide."
    )


def build_greeting_keyboard() -> InlineKeyboardMarkup | None:
    """Keyboard for the /start greeting: join buttons + an 'I joined — verify' button."""
    chans = get_channels()
    if not chans:
        return None
    _, url_rows = _channel_rows()
    rows = list(url_rows)
    # A live re-check button: verifies the user has joined the required channel(s).
    rows.append([InlineKeyboardButton("✅ I joined — verify", callback_data="chkjoin:run")])
    rows.append([InlineKeyboardButton("💳 Subscription / plans", callback_data="sub:menu")])
    return InlineKeyboardMarkup(rows)


async def _verify_joined(client, user_id: int):
    """Re-check membership. Returns (ok, detail)."""
    try:
        settings = get_settings()
        if not settings.get("enabled"):
            return True, "no_op"  # channel gate is off
        ok, reason = await check_access(client, user_id)
        if ok:
            return True, "ok"
        if reason == "need_channel":
            all_ok, missing = await check_all_channels(client, user_id)
            if all_ok:
                return True, "ok"
            names = ", ".join(ch.get("username") or str(ch.get("id")) for ch in missing) or "the channel(s)"
            return False, f"Still missing: **{names}**"
        if reason == "need_subscription":
            # free path isn't active or not entitled — tell them to subscribe
            return False, "no_free_path"
        return False, "blocked"
    except Exception as e:  # defensive — never leave the callback hanging
        return False, f"error:{e}"


def _is_member_status(status) -> bool:
    """True if chat-member status counts as 'is still in channel'."""
    s = (getattr(status, "value", None) or str(status)) if status else ""
    return s.lower() in ("member", "administrator", "creator", "owner", "restricted")


def register_join_handlers(app: Client):
    # Notify free-tier users when they leave a required channel: Telegram
    # emits a ChatMemberUpdated whenever someone's status in a channel
    # changes (join/leave/kick). The bot must be an admin in the channel
    # to receive these updates (otherwise they are silently dropped).
    # This is the user-requested "their leave of the channel also should
    # be known and notified that they left and no longer qualify for free
    # usage" feature.
    try:
        @app.on_chat_member_updated(group=0)
        async def _channel_leave_watcher(client: Client, update):
            # update: pyrogram.types.ChatMemberUpdated (new_chat_member, old_chat_member, chat, from_user)
            try:
                chat = getattr(update, "chat", None)
                if chat is None:
                    return
                chat_id = int(getattr(chat, "id", 0) or 0)
                # Only care about force-join channels
                chans = get_channels()
                if not chans:
                    return
                force_ids = {int(c.get("id", 0) or 0) for c in chans if c.get("id")}
                # For @username-only channels we resolve lazily, but for the
                # ChatMemberUpdated we already have chat_id from the event.
                # If none of our force-join IDs match this chat, ignore.
                # Also handle username-only: check if chat.username matches.
                chat_username = (getattr(chat, "username", "") or "").lstrip("@")
                matched = chat_id in force_ids
                if not matched:
                    # username-only fallback: compare username case-insensitively
                    for c in chans:
                        if (c.get("username", "").lstrip("@").lower() == chat_username.lower()
                                and chat_username):
                            matched = True
                            break
                if not matched:
                    return

                old = getattr(update, "old_chat_member", None)
                new = getattr(update, "new_chat_member", None)
                if old is None or new is None:
                    return
                old_status = getattr(old, "status", None)
                new_status = getattr(new, "status", None)
                # left / kicked / banned -> not a member anymore; member/admin/creator -> is member
                was_member = _is_member_status(old_status)
                is_member = _is_member_status(new_status)
                if was_member and not is_member:
                    # User left / was kicked from a required channel.
                    # Only notify users who were relying on the free path
                    # (subscribed users don't care; they have paid access).
                    from utils.subscription.store import is_subscription_active
                    # ChatMemberUpdated.user is the affected user; from_user is actor (may be same)
                    # For channel leaves the admin does the remove, but the subject is in new_chat_member.user
                    subject = getattr(new, "user", None)
                    if subject is None:
                        subject = getattr(update, "from_user", None)
                    uid = int(getattr(subject, "id", 0) or 0) if subject else 0
                    if not uid or uid == getattr(client.me, "id", 0):
                        return
                    active, _ = is_subscription_active(uid)
                    if active:
                        return  # paid users keep access even without channel
                    # Free-path user who just lost a required channel
                    ch_label = f"@{chat_username}" if chat_username else f"`{chat_id}`"
                    try:
                        await client.send_message(
                            uid,
                            f"⚠️ You left {ch_label} — you no longer qualify for **free** downloads.\n\n"
                            f"Re-join {ch_label} and tap **✅ I joined — verify** on /start, or use "
                            f"/subscription to unlock without joining.",
                        )
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception:
        # pyrogram < 2 or stripped dispatcher — chat_member updates not available; ignore
        pass

    @app.on_callback_query(filters.regex(r"^chkjoin:"), group=2)
    async def chkjoin_cb(client: Client, cb: CallbackQuery):
        user_id = cb.from_user.id
        ok, detail = await _verify_joined(client, user_id)
        if ok:
            try:
                await cb.message.edit_text(
                    "✅ **Membership verified!** You now have **free access**.\n\n"
                    "Send me a link to download it, or tap /start for the guide.",
                )
            except Exception:
                pass
            await cb.answer("✅ Membership verified — free access granted!", show_alert=True)
            return
        studio = "The channel(s) couldn't verify. Make sure the bot is an **admin** of the channel."
        if detail.startswith("no_free_path"):
            studio = "Free tier isn't enabled for you — subscribe instead."
        elif detail.startswith("blocked"):
            studio = "You don't have access."
        elif detail.startswith("error"):
            studio = "⚠️ Could not verify membership. Please try again in a few seconds."
        try:
            await cb.message.edit_text(
                f"❌ **Not verified yet.**\n\n{studio}\n\nJoin the channel then tap **✅ I joined** again.",
                reply_markup=build_greeting_keyboard(),
            )
        except Exception:
            pass
        await cb.answer("❌ Membership not detected yet.", show_alert=True)
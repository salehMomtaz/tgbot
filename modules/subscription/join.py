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


def register_join_handlers(app: Client):
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
"""
Direct-Forward menu rendering for the admin console.

Mirrors the original modules/admin.py direct-forward menu functions exactly.
"""

import logging
import config
from pyrogram.types import CallbackQuery
from .keyboards import get_direct_menu_keyboard
from modules import direct_forward

logger = logging.getLogger(__name__)


async def _render_direct_menu(callback_query: CallbackQuery):
    state = direct_forward._load_state()
    ig_enabled = "🟢" if config.IG_DIRECT_ENABLED else "⚪"
    x_enabled = "🟢" if config.X_DIRECT_ENABLED else "⚪"
    tt_enabled = "🟢" if getattr(config, "TIKTOK_DIRECT_ENABLED", False) else "⚪"
    chat_set = "✅" if getattr(config, "DIRECT_FORWARD_CHAT_ID", 0) else "⚠️ DIRECT_FORWARD_CHAT_ID=0 (relay off)"

    # X cookie health summary
    x_cookies = direct_forward._x_jar_cookies()
    if not x_cookies:
        x_cookie_status = "⚠️ no jar"
    elif "auth_token" not in x_cookies or "twid" not in x_cookies:
        x_cookie_status = "⚠️ missing cookies"
    else:
        uid = direct_forward._x_twid_user_id(x_cookies)
        x_cookie_status = f"✅ uid `{uid}`" if uid else "⚠️ bad twid"

    # X Chat PIN status (the E2EE passcode the bridge needs).
    if getattr(config, "XCHAT_PIN", ""):
        pin_status = "✅ set (hidden)"
    else:
        pin_status = "⚠️ not set — E2EE self-DM can't be read"

    # TikTok cookie health summary
    tt_cookies = direct_forward._tt_jar_cookies()
    if not tt_cookies:
        tt_cookie_status = "⚠️ no jar"
    elif not tt_cookies.get("sessionid"):
        tt_cookie_status = "⚠️ missing sessionid"
    else:
        tt_cookie_status = "✅ sessionid present"

    try:
        await callback_query.message.edit_text(
            "📨 **Direct-Forward (DM relay)**\n\n"
            "The bot relays media you DM to its own Instagram account, "
            "send to your OWN X self-DM (Message Yourself), or send to "
            "your OWN TikTok self-DM (Message Yourself).\n\n"
            f"• Relay chat: {chat_set}\n"
            f"• Poll interval: {config.DIRECT_FORWARD_POLL_SECONDS}s\n\n"
            f"**Instagram**\n"
            f"• {ig_enabled} Status: **{direct_forward.pairing_status('ig', state)}**\n\n"
            f"**X / Twitter**\n"
            f"• {x_enabled} Status: **{'enabled' if config.X_DIRECT_ENABLED else 'disabled'}**\n"
            f"• Cookies: {x_cookie_status}\n"
            f"• X Chat PIN: {pin_status}\n"
            f"• Method: self-DM — send tweet links/photos/videos to your own X "
            f"self-DM (Message Yourself).\n\n"
            f"**TikTok**\n"
            f"• {tt_enabled} Status: **{'enabled' if tt_enabled == '🟢' else 'disabled'}**\n"
            f"• Cookies: {tt_cookie_status}\n"
            f"• Method: self-DM — send videos to your own TikTok self-DM "
            f"(Message Yourself); the bot watches the IM WebSocket.\n\n"
            "Tap **🧪 Test X Cookies** / **🧪 Test TikTok** to validate the "
            "jars, **🔑 Set X Chat PIN** to enter the E2EE passcode, and the "
            "**🟢 Enable …** / **🔴 Disable …** buttons to toggle each relay.\n"
            "Instagram: tap **🔗 Pair Instagram**, then send "
            "the code to the bot account via Instagram DM.",
            reply_markup=get_direct_menu_keyboard()
        )
    except Exception:
        pass
    await callback_query.answer()
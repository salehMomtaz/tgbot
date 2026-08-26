"""
Keyboard builders for the admin console.

Mirrors the original modules/admin.py keyboard functions exactly.
"""

from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import config
from .pot_menu import _pot_running
from utils.gate import is_document_mode


def build_console_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Main admin console keyboard. PO Token badge reflects live provider health."""
    doc_status = "✅" if is_document_mode(user_id) else "❌"
    pot_status = "🟢" if _pot_running() else "🔴"
    premium_status = "🟢" if config.PREMIUM_STRING_SESSION else "⚪"
    try:
        from utils.subscription.store import get_settings as _gs
        s = _gs()
        sub_badge = "🟢 ON" if s.get("enabled") else "⚪ OFF"
    except Exception:
        sub_badge = "⚪"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 List Users", callback_data="admin_list"),
         InlineKeyboardButton("➕ Add User", callback_data="admin_add")],
        [InlineKeyboardButton("➖ Remove User", callback_data="admin_remove"),
         InlineKeyboardButton("🚫 Blacklist Logs", callback_data="admin_blacklist")],
        [InlineKeyboardButton(f"📄 Doc Mode: {doc_status}", callback_data="admin_toggle_doc"),
         InlineKeyboardButton("🍪 Cookie Jars", callback_data="admin_cookies_menu")],
        [InlineKeyboardButton(f"👑 Premium Uploads: {premium_status}", callback_data="admin_premium_menu"),
         InlineKeyboardButton(f"🔐 PO Token: {pot_status}", callback_data="admin_pot_menu")],
        [InlineKeyboardButton(f"💳 Subscriptions: {sub_badge}", callback_data="admin_sub_menu"),
         InlineKeyboardButton("📨 Direct-Forward", callback_data="admin_direct_menu")],
        [InlineKeyboardButton("📸 Friend Media", callback_data="admin_friend_media_menu")],
        [InlineKeyboardButton("💥 Abort Transfer", callback_data="admin_abort_queue"),
         InlineKeyboardButton("🔄 Restart Bot", callback_data="admin_restart")],
        [InlineKeyboardButton("❌ Close Console", callback_data="admin_close")]
    ])


def get_premium_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Premium", callback_data="admin_premium_add"),
         InlineKeyboardButton("➖ Remove Premium", callback_data="admin_premium_remove")],
        [InlineKeyboardButton("🔑 Generate Session", callback_data="admin_premium_gen"),
         InlineKeyboardButton("🧹 Cleanup Stale Gen", callback_data="admin_premium_gen_clean")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="admin_premium_menu"),
         InlineKeyboardButton("◀️ Back to Console", callback_data="admin_main")]
    ])


def get_cookie_action_keyboard(cookie_key: str) -> InlineKeyboardMarkup:
    """Per-jar action keyboard. Test/Backup/Restore are YouTube-specific."""
    rows = [[
        InlineKeyboardButton("📤 Download", callback_data=f"admin_cookie_action:{cookie_key}:download"),
        InlineKeyboardButton("✏️ Replace", callback_data=f"admin_cookie_action:{cookie_key}:replace"),
    ]]
    if cookie_key == "ytcookies":
        rows.append([
            InlineKeyboardButton("🧪 Test", callback_data=f"admin_cookie_action:{cookie_key}:test"),
            InlineKeyboardButton("💾 Save Backup", callback_data=f"admin_cookie_action:{cookie_key}:savebackup"),
        ])
        rows.append([
            InlineKeyboardButton("♻️ Restore Backup", callback_data=f"admin_cookie_action:{cookie_key}:restore")
        ])
    rows.append([InlineKeyboardButton("◀️ Back", callback_data="admin_cookies_menu")])
    return InlineKeyboardMarkup(rows)


def get_pot_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧪 Test Stack", callback_data="admin_pot_action:test"),
         InlineKeyboardButton("🔍 Run Diagnosis", callback_data="admin_pot_action:diagnose")],
        [InlineKeyboardButton("🚀 Start Provider", callback_data="admin_pot_action:start"),
         InlineKeyboardButton("🛑 Stop Provider", callback_data="admin_pot_action:stop")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="admin_pot_menu")],
        [InlineKeyboardButton("◀️ Back to Console", callback_data="admin_main")]
    ])


def get_direct_menu_keyboard() -> InlineKeyboardMarkup:
    ig_label = "🔴 Disable IG" if config.IG_DIRECT_ENABLED else "🟢 Enable IG"
    x_label = "🔴 Disable X" if config.X_DIRECT_ENABLED else "🟢 Enable X"
    tt_label = "🔴 Disable TikTok" if getattr(config, "TIKTOK_DIRECT_ENABLED", False) else "🟢 Enable TikTok"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(ig_label, callback_data="admin_direct_toggle_ig"),
         InlineKeyboardButton("🔗 Pair IG", callback_data="admin_direct_pair_ig")],
        [InlineKeyboardButton("💔 Unpair IG", callback_data="admin_direct_unpair_ig")],
        [InlineKeyboardButton(x_label, callback_data="admin_direct_toggle_x"),
         InlineKeyboardButton("🧪 Test X Cookies", callback_data="admin_direct_test_x")],
        [InlineKeyboardButton(tt_label, callback_data="admin_direct_toggle_tiktok"),
         InlineKeyboardButton("🧪 Test TikTok", callback_data="admin_direct_test_tiktok")],
        [InlineKeyboardButton("🔑 Set X Chat PIN", callback_data="admin_direct_set_x_pin")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="admin_direct_menu"),
         InlineKeyboardButton("◀️ Back to Console", callback_data="admin_main")]
    ])


# Reusable "Abort" button shown on every step of the session-generation flow so
# the admin can stop at any point and the temp client is never left dangling.
_gen_abort_markup = InlineKeyboardMarkup([[
    InlineKeyboardButton("❌ Abort Session Generation", callback_data="admin_premium_gen_abort")
]])


# Phone-call-app style dial pad used for Step 2/3 (login code entry).
# The code is entered via inline-button taps (callback data), NEVER as a chat
# message: Telegram's anti-sharing detection sees a login code typed into a
# chat, flags it as "previously shared by your account" and instantly
# invalidates it (PHONE_CODE_EXPIRED seconds after send_code). Buttons carry
# the digit in callback_data so the code never appears in message text.
# Simple numeric layout: 3x4 keypad (1-9, then backspace/0/OK) + Abort.
_gen_dial_pad_markup = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("1", callback_data="admin_premium_gen_digit:1"),
        InlineKeyboardButton("2", callback_data="admin_premium_gen_digit:2"),
        InlineKeyboardButton("3", callback_data="admin_premium_gen_digit:3"),
    ],
    [
        InlineKeyboardButton("4", callback_data="admin_premium_gen_digit:4"),
        InlineKeyboardButton("5", callback_data="admin_premium_gen_digit:5"),
        InlineKeyboardButton("6", callback_data="admin_premium_gen_digit:6"),
    ],
    [
        InlineKeyboardButton("7", callback_data="admin_premium_gen_digit:7"),
        InlineKeyboardButton("8", callback_data="admin_premium_gen_digit:8"),
        InlineKeyboardButton("9", callback_data="admin_premium_gen_digit:9"),
    ],
    [
        InlineKeyboardButton("⌫", callback_data="admin_premium_gen_bksp"),
        InlineKeyboardButton("0", callback_data="admin_premium_gen_digit:0"),
        InlineKeyboardButton("✓", callback_data="admin_premium_gen_enter"),
    ],
    [InlineKeyboardButton("❌ Abort Session Generation", callback_data="admin_premium_gen_abort")],
])


# Reusable "Back to Console" inline button
back_markup = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back to Console", callback_data="admin_main")]])
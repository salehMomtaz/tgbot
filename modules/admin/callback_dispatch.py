"""
Admin console callback query dispatcher — the core of the admin UI.

Mirrors the original modules/admin.py _admin_callback_dispatch exactly.
"""

import asyncio
import os
import shutil
import logging
import config
from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from dotenv import set_key
from main import log_event, schedule_self_restart
from utils.gate import (
    load_database,
    add_user,
    remove_user,
    unblacklist_user,
    is_blacklisted,
    is_authorized,
    is_premium_user,
    add_premium_user,
    remove_premium_user,
    toggle_document_mode,
)
from utils.id_validator import is_valid_telegram_id
from utils.shared import queue

from .state import USER_STATES, ACTIVE_PROMPTS, PREMIUM_GEN
from .keyboards import (
    build_console_keyboard,
    back_markup as _back_markup,
    get_direct_menu_keyboard,
    _gen_abort_markup,
    _gen_dial_pad_markup,
)
from .premium_gen import (
    _premium_gen_cleanup,
    _premium_gen_pad_text,
    _finish_premium_gen,
    discard_client_quiet,
)
from .cookies import COOKIE_MAP, _write_cookie_jar
from .cookie_test import _test_cookie_jar
from .pot_menu import _handle_pot_action
from modules import direct_forward
from .direct_menu import _render_direct_menu

logger = logging.getLogger(__name__)

# Re-export back_markup for other modules
back_markup = _back_markup

_SUB_LAST: dict[int, float] = {}
def _sub_rate_ok(uid: int) -> bool:
    import time as _t2
    now = _t2.monotonic()
    last = _SUB_LAST.get(uid, 0)
    lim = int(getattr(config, "SUB_RATE_LIMIT_SECONDS", 3) or 3)
    if now - last < lim:
        return False
    _SUB_LAST[uid] = now
    return True


async def _admin_callback_dispatch(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    user_id = callback_query.from_user.id

    if user_id != config.SYSTEM_CREATOR_ID:
        await callback_query.answer("Access Denied.", show_alert=True)
        return

    if data == "admin_close":
        USER_STATES.pop(user_id, None)
        await _purge_active_prompt(user_id, client)
        await callback_query.message.delete()
        await callback_query.answer("Console closed.")

    elif data == "admin_abort_queue":
        queue_len = len(queue._pending)
        queue._pending.clear()
        queue._active = False

        if os.path.exists("cache"):
            try:
                shutil.rmtree("cache")
                os.makedirs("cache", exist_ok=True)
            except Exception:
                pass

        await callback_query.answer("💥 System Reset: All queue jobs aborted and cache purged!", show_alert=True)
        await log_event(f"💥 **Admin Action:** Queue reset executed. {queue_len} pending jobs aborted.")

    elif data == "admin_toggle_doc":
        state = toggle_document_mode(user_id)
        status_str = "✅" if state else "❌"
        await callback_query.answer(f"📄 Document Mode toggled to {status_str}.", show_alert=True)
        await log_event(f"⚙️ **Admin Action:** Document Mode toggled to {status_str}.")

        try:
            await callback_query.message.edit_text(
                "🛠 **Admin System Console**\nChoose an administrative action below:",
                reply_markup=build_console_keyboard(user_id)
            )
        except Exception:
            pass

    elif data == "admin_list":
        db = load_database()
        users = db["authorized"]
        text = "📋 **Authorized Users List:**\n" + "\n".join([f"• `{uid}`" for uid in users]) if users else "No additional users authorized."
        await callback_query.message.edit_text(text, reply_markup=back_markup)
        await callback_query.answer()

    elif data == "admin_blacklist":
        db = load_database()
        blacklisted = db["blacklisted"]
        text = "🚫 **Banned Intruders List:**\n" + "\n".join([f"• `{uid}`" for uid in blacklisted]) if blacklisted else "Blacklist registry is empty."

        keyboard_rows = []
        if blacklisted:
            keyboard_rows.append([InlineKeyboardButton("🔓 Unban User", callback_data="admin_unban")])
        keyboard_rows.append([InlineKeyboardButton("◀️ Back", callback_data="admin_main")])

        await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard_rows))
        await callback_query.answer()

    elif data == "admin_unban":
        USER_STATES[user_id] = "waiting_for_unban"
        ACTIVE_PROMPTS[user_id] = callback_query.message.id
        await callback_query.message.edit_text(
            "🔓 **Unban User**\nPlease type the numerical ID of the blocked user you want to unban directly in your text box and press send:",
            reply_markup=back_markup
        )
        await callback_query.answer()

    elif data == "admin_main":
        USER_STATES.pop(user_id, None)  # Reset state on return
        ACTIVE_PROMPTS.pop(user_id, None)
        await _premium_gen_cleanup(user_id, client)

        await callback_query.message.edit_text(
            "🛠 **Admin System Console**\nChoose an administrative action below:",
            reply_markup=build_console_keyboard(user_id)
        )
        await callback_query.answer()

    elif data == "admin_add":
        USER_STATES[user_id] = "waiting_for_add_user"
        ACTIVE_PROMPTS[user_id] = callback_query.message.id
        await callback_query.message.edit_text(
            "➕ **Add Authorized User**\nPlease type the numerical ID of the user you want to authorize directly in your text box and press send:",
            reply_markup=back_markup
        )
        await callback_query.answer()

    elif data == "admin_remove":
        USER_STATES[user_id] = "waiting_for_remove_user"
        ACTIVE_PROMPTS[user_id] = callback_query.message.id
        await callback_query.message.edit_text(
            "➖ **Remove Authorized User**\nPlease type the numerical ID of the user you want to remove directly in your text box and press send:",
            reply_markup=back_markup
        )
        await callback_query.answer()

    # =========================================================================
    # Premium Uploads (4 GB) Sub-Menus Configuration
    # =========================================================================
    elif data == "admin_premium_menu":
        USER_STATES.pop(user_id, None)
        ACTIVE_PROMPTS.pop(user_id, None)
        db = load_database()
        premium_users = db["premium_users"]
        premium_lines = "\n".join([f"• `{uid}`" for uid in premium_users]) if premium_users else "No Premium-enabled users yet."

        # Sweep any stale in-chat generation so a dangling temp client is cleaned.
        import time as _t
        gen = PREMIUM_GEN.get(user_id)
        if gen and gen.get("client") and gen.get("expires_at", 0) < _t.monotonic():
            await _premium_gen_cleanup(user_id, client)

        if config.PREMIUM_STRING_SESSION:
            status_note = "🟢 Premium userbot session is configured — 4 GB uploads are available to whitelisted users."
        else:
            status_note = "⚪ No `PREMIUM_STRING_SESSION` set — 4 GB uploads are DISABLED. Tap **🔑 Generate Session** to create one right here."

        from .keyboards import get_premium_menu_keyboard
        await callback_query.message.edit_text(
            f"👑 **Premium Uploads (4 GB)**\n\n{status_note}\n\n"
            f"**Whitelisted users:**\n{premium_lines}",
            reply_markup=get_premium_menu_keyboard()
        )
        await callback_query.answer()

    elif data == "admin_premium_add":
        USER_STATES[user_id] = "waiting_for_add_premium"
        ACTIVE_PROMPTS[user_id] = callback_query.message.id
        await callback_query.message.edit_text(
            "👑 **Enable 4 GB Premium Uploads**\nPlease type the numerical ID of the user to whitelist for 4 GB uploads:",
            reply_markup=back_markup
        )
        await callback_query.answer()

    elif data == "admin_premium_remove":
        USER_STATES[user_id] = "waiting_for_remove_premium"
        ACTIVE_PROMPTS[user_id] = callback_query.message.id
        await callback_query.message.edit_text(
            "👑 **Disable 4 GB Premium Uploads**\nPlease type the numerical ID of the user to remove from the Premium whitelist:",
            reply_markup=back_markup
        )
        await callback_query.answer()

    elif data == "admin_premium_gen":
        # Start (or restart) the in-chat session-string generation flow.
        # Any previous stale generation is cleaned up first so nothing dangles.
        await _premium_gen_cleanup(user_id, client)
        USER_STATES[user_id] = "waiting_for_premium_phone"
        ACTIVE_PROMPTS[user_id] = callback_query.message.id
        await callback_query.message.edit_text(
            "🔑 **Generate a Premium String Session**\n\n"
            "The bot API is hard-capped at 2 GB per upload; only a Premium *user* "
            "account over MTProto can send 4 GB. This flow logs into that account "
            "and exports a session string.\n\n"
            "**Step 1/3** — Send the phone number of the Premium account in "
            "international format (country code + number), e.g. `+15551234567`.\n\n"
            "_(Tap **❌ Abort Session Generation** at any step to cancel; the temp "
            "login client is always cleaned up.)_",
            reply_markup=_gen_abort_markup
        )
        await callback_query.answer()

    elif data == "admin_premium_gen_abort":
        await _premium_gen_cleanup(user_id, client)
        await callback_query.message.edit_text(
            "🚫 Session generation aborted. Any temporary login client was disconnected.",
            reply_markup=back_markup
        )
        await callback_query.answer("Aborted.", show_alert=False)

    elif data.startswith("admin_premium_gen_digit:"):
        # Dial-pad digit tap (Step 2/3). The digit travels in callback_data,
        # never in a chat message, so Telegram's login-code anti-sharing
        # detection is not triggered.
        gen = PREMIUM_GEN.get(user_id)
        if not gen or not gen.get("client"):
            await callback_query.answer("Session generation expired. Start again from the 👑 Premium menu.", show_alert=True)
            return
        digit = data.rsplit(":", 1)[1]
        if len(gen.get("code_buffer", "")) >= 6:
            await callback_query.answer("Max 6 digits entered — tap ✓ or ⌫.", show_alert=False)
            return
        gen["code_buffer"] = gen.get("code_buffer", "") + digit
        await _premium_gen_pad_text(callback_query, gen)
        await callback_query.answer()

    elif data == "admin_premium_gen_bksp":
        gen = PREMIUM_GEN.get(user_id)
        if not gen or not gen.get("client"):
            await callback_query.answer("Session generation expired. Start again from the 👑 Premium menu.", show_alert=True)
            return
        gen["code_buffer"] = gen.get("code_buffer", "")[:-1]
        await _premium_gen_pad_text(callback_query, gen)
        await callback_query.answer()

    elif data == "admin_premium_gen_enter":
        # Submit the entered code (Step 2/3 -> 2FA or finish).
        from utils import premium_session
        gen = PREMIUM_GEN.get(user_id)
        if not gen or not gen.get("client"):
            await callback_query.answer("Session generation expired. Start again from the 👑 Premium menu.", show_alert=True)
            return
        code = gen.get("code_buffer", "")
        if len(code) < 4:
            await callback_query.answer("Enter the full code first, then tap ✓.", show_alert=True)
            return
        try:
            outcome = await premium_session.verify_code(
                gen["client"], gen["phone"], gen["phone_code_hash"], code
            )
        except Exception as e:
            gen["code_buffer"] = ""
            await callback_query.message.edit_text(
                f"❌ Invalid code: `{e}`.\n\n"
                "The code was rejected — check it on the dial pad again.\n\n"
                "_(If the code expired, tap **Abort** and start over.)_",
                reply_markup=_gen_dial_pad_markup
            )
            await callback_query.answer()
            return
        if outcome == "2fa":
            USER_STATES[user_id] = "waiting_for_premium_password"
            step_msg = await callback_query.message.edit_text(
                "🔑 **Step 3/3 — Two-factor password**\n\n"
                "This account has two-step verification enabled. Type your **2FA password** "
                "to finish logging in.\n\n"
                "_(Tap Abort at any time to cancel.)_",
                reply_markup=_gen_abort_markup
            )
            ACTIVE_PROMPTS[user_id] = step_msg.id
            await callback_query.answer()
            return
        # Success: replace the dial pad (which shows the entered code) with a
        # plain "logging in" note before exporting the session string.
        try:
            await callback_query.message.edit_text(
                "✅ Code accepted — logging in and exporting the session string…"
            )
        except Exception:
            pass
        await _finish_premium_gen(client, callback_query.message, user_id, client, back_markup)
        await callback_query.answer()
        return

    elif data == "admin_premium_gen_clean":
        # Menu button: sweep any stale generation, then re-render the menu.
        # No edit-text of different content, so repeated presses are safe.
        await _premium_gen_cleanup(user_id, client)
        db = load_database()
        premium_users = db["premium_users"]
        premium_lines = "\n".join([f"• `{uid}`" for uid in premium_users]) if premium_users else "No Premium-enabled users yet."
        if config.PREMIUM_STRING_SESSION:
            status_note = "🟢 Premium userbot session is configured — 4 GB uploads are available to whitelisted users."
        else:
            status_note = "⚪ No `PREMIUM_STRING_SESSION` set — 4 GB uploads are DISABLED. Tap **🔑 Generate Session** to create one right here."
        from .keyboards import get_premium_menu_keyboard
        await callback_query.message.edit_text(
            f"👑 **Premium Uploads (4 GB)**\n\n{status_note}\n\n"
            f"**Whitelisted users:**\n{premium_lines}",
            reply_markup=get_premium_menu_keyboard()
        )
        await callback_query.answer("Stale generation cleaned.", show_alert=False)

    elif data == "admin_premium_gen_save":
        gen = PREMIUM_GEN.get(user_id)
        result = (gen or {}).get("result")
        if not result:
            await callback_query.answer("No pending session string to save.", show_alert=True)
            return
        try:
            from utils.premium_session import save_session_string
            save_session_string(result)
        except Exception as e:
            await callback_query.answer(f"❌ Failed to save: {e}", show_alert=True)
            return
        PREMIUM_GEN.pop(user_id, None)
        await callback_query.message.edit_text(
            "✅ **Session string saved!**\n\n"
            "`PREMIUM_STRING_SESSION` has been written to `.env`.\n\n"
            "🔄 **Restarting the bot automatically** to activate it — back in "
            "a few seconds. No shell access needed.",
            reply_markup=back_markup
        )
        await log_event("👑 **Premium Session:** New PREMIUM_STRING_SESSION saved to .env by creator. Restarting automatically.")
        await callback_query.answer()
        schedule_self_restart(delay=3.0)

    # =========================================================================
    # Restart Bot (self-restart via systemd SIGTERM / execv fallback)
    # =========================================================================
    elif data == "admin_restart":
        USER_STATES.pop(user_id, None)
        ACTIVE_PROMPTS.pop(user_id, None)
        confirm_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, restart now", callback_data="admin_restart_confirm"),
             InlineKeyboardButton("↩️ Cancel", callback_data="admin_main")],
        ])
        await callback_query.message.edit_text(
            "🔄 **Restart the bot?**\n\nThe bot will shut down gracefully and "
            "come back in a few seconds. Any running download will be "
            "interrupted and the queue cleared.\n\nContinue?",
            reply_markup=confirm_keyboard
        )
        await callback_query.answer()

    elif data == "admin_restart_confirm":
        await callback_query.message.edit_text(
            "🔄 **Restarting the bot…**\n\nBack in a few seconds."
        )
        await log_event(f"🔄 **Admin Action:** Bot restart requested by creator (`{user_id}`).")
        await callback_query.answer()
        schedule_self_restart(delay=3.0)

    # =========================================================================
    # Cookies Sub-Menus Configuration
    # =========================================================================
    elif data == "admin_cookies_menu":
        USER_STATES.pop(user_id, None)
        ACTIVE_PROMPTS.pop(user_id, None)
        from .keyboards import get_cookie_action_keyboard
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("YouTube", callback_data="admin_cookie_select:ytcookies"), InlineKeyboardButton("Instagram", callback_data="admin_cookie_select:igcookies")],
            [InlineKeyboardButton("TikTok", callback_data="admin_cookie_select:ttcookies"), InlineKeyboardButton("X/Twitter", callback_data="admin_cookie_select:xcookies")],
            [InlineKeyboardButton("Global (cookies.txt)", callback_data="admin_cookie_select:cookies"), InlineKeyboardButton("➕ Per-Site Jar", callback_data="admin_cookie_add_site")],
            [InlineKeyboardButton("◀️ Return to Console", callback_data="admin_main")]
        ])
        await callback_query.message.edit_text(
            "🍪 **Cookie Jars Manager**\nSelect a cookie profile to view or edit, or add a per-site jar for any yt-dlp site:",
            reply_markup=keyboard
        )
        await callback_query.answer()

    elif data == "admin_cookie_add_site":
        USER_STATES[user_id] = "waiting_for_per_site_name"
        ACTIVE_PROMPTS[user_id] = callback_query.message.id
        await callback_query.message.edit_text(
            "➕ **Per-Site Cookie Jar**\n\n"
            "Type the site name (e.g. `reddit`, `tiktok`, `soundcloud`) — "
            "the jar will be stored as `cookies/ytdlp/<site>.txt`.\n\n"
            "Only letters, numbers, dashes and dots are allowed. "
            "Lower-case is fine; we'll lower-case it for you.\n\n"
            "Then send the **`.txt` cookie file** as a document.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Cancel", callback_data="admin_cookies_menu")]])
        )
        await callback_query.answer()

    elif data.startswith("admin_cookie_select:"):
        cookie_key = data.split(":")[1]
        file_path = COOKIE_MAP.get(cookie_key)
        status_line = ""
        if file_path:
            from utils import cookie_manager
            rec = cookie_manager.get_meta_record(file_path)
            has_lines = os.path.exists(file_path) and cookie_manager.has_real_cookie_lines(file_path)
            if not has_lines:
                status_line = "\n⚠️ Jar is empty — authenticated downloads will fail."
            else:
                import time as _time
                last_ok = rec.get("last_success")
                merges = rec.get("merge_count", 0)
                last_fail = rec.get("last_failure")
                last_up = rec.get("last_upload")
                if last_ok:
                    age_h = int((_time.time() - last_ok) / 3600)
                    status_line = f"\n✅ Last authenticated success: {age_h}h ago · rotation merges: {merges}"
                if last_up:
                    age_h = int((_time.time() - last_up) / 3600)
                    status_line += f"\n📤 Last uploaded: {age_h}h ago"
                if not last_ok and not last_up and last_fail:
                    status_line = f"\n❌ Last auth failure: {rec.get('failure_reason', 'unknown')[:120]}"
                if not status_line:
                    status_line = "\nℹ️ Jar present but never validated by a successful run yet."
        await callback_query.message.edit_text(
            f"🍪 **Cookie Profile: `{cookie_key}.txt`**{status_line}\nSelect an administration action:",
            reply_markup=get_cookie_action_keyboard(cookie_key)
        )
        await callback_query.answer()

    elif data.startswith("admin_cookie_action:"):
        _, cookie_key, action = data.split(":")
        file_path = COOKIE_MAP.get(cookie_key)

        if not file_path:
            await callback_query.answer("❌ Invalid cookie profile.", show_alert=True)
            return

        if action == "download":
            if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                await callback_query.answer("Delivering file...")
                await client.send_document(
                    chat_id=user_id,
                    document=file_path,
                    caption=f"🍪 Here is your active `{cookie_key}.txt` file."
                )
            else:
                await callback_query.answer("⚠️ File is empty or does not exist on VPS yet.", show_alert=True)

        elif action == "test":
            await callback_query.answer("Testing jar...")
            await _test_cookie_jar(client, user_id, cookie_key, file_path, back_markup=back_markup)

        elif action == "replace":
            USER_STATES[user_id] = f"waiting_for_replace_{cookie_key}"
            ACTIVE_PROMPTS[user_id] = callback_query.message.id
            await callback_query.message.edit_text(
                f"✏️ **Replace {cookie_key}.txt**\n"
                "Send your fresh cookies as a **`.txt` document file** (Netscape format).\n"
                "_Text-paste is not accepted — Telegram truncates it and corrupts the jar._",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Cancel & Return", callback_data=f"admin_cookie_select:{cookie_key}")]])
            )
            await callback_query.answer()

        elif action == "restore":
            await callback_query.answer("Restoring from backup...")
            backup_path = getattr(config, "YT_COOKIES_BACKUP", "ytcookies.backup")
            if not os.path.exists(backup_path) or os.path.getsize(backup_path) == 0:
                await callback_query.message.edit_text(
                    f"⚠️ No backup found for `{cookie_key}.txt`.",
                    reply_markup=get_cookie_action_keyboard(cookie_key)
                )
                return
            try:
                from utils.downloader import _purge_cookie_snapshots
                if os.path.exists(file_path):
                    os.chmod(file_path, 0o644)
                shutil.copy(backup_path, file_path)
                os.chmod(file_path, 0o444)
                _purge_cookie_snapshots(file_path)
                await callback_query.message.edit_text(
                    f"✅ Restored `{cookie_key}.txt` from `{backup_path}`.",
                    reply_markup=get_cookie_action_keyboard(cookie_key)
                )
                await log_event(f"🍪 **Admin Action:** `{cookie_key}.txt` restored from backup.")
            except Exception as e:
                await callback_query.message.edit_text(
                    f"❌ Failed to restore backup: {e}",
                    reply_markup=get_cookie_action_keyboard(cookie_key)
                )

        elif action == "savebackup":
            await callback_query.answer("Saving backup...")
            backup_path = getattr(config, "YT_COOKIES_BACKUP", "ytcookies.backup")
            if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
                await callback_query.message.edit_text(
                    f"⚠️ `{cookie_key}.txt` is empty. Nothing to back up.",
                    reply_markup=get_cookie_action_keyboard(cookie_key)
                )
                return
            try:
                from utils.downloader import _purge_cookie_snapshots
                if os.path.exists(backup_path):
                    os.chmod(backup_path, 0o644)
                # Source jar is read-only; shutil.copy only reads it, which is fine.
                shutil.copy(file_path, backup_path)
                os.chmod(backup_path, 0o444)
                _purge_cookie_snapshots(file_path)
                await callback_query.message.edit_text(
                    f"✅ Saved `{cookie_key}.txt` as protected backup `{backup_path}` (read-only).",
                    reply_markup=get_cookie_action_keyboard(cookie_key)
                )
                await log_event(f"🍪 **Admin Action:** `{cookie_key}.txt` saved as protected backup.")
            except Exception as e:
                await callback_query.message.edit_text(
                    f"❌ Failed to save backup: {e}",
                    reply_markup=get_cookie_action_keyboard(cookie_key)
                )

    # =========================================================================
    # PO Token Provider Sub-Menus
    # =========================================================================
    elif data == "admin_pot_menu":
        USER_STATES.pop(user_id, None)
        # NOTE: this calls _render_pot_menu DIRECTLY, not _handle_pot_action
        # with a "render" action — _handle_pot_action only handles
        # start/stop/diagnose/test, so a "render" action would silently fall
        # through and the PO menu would never open. (Regression from the
        # package-split refactor: pre-split admin.py called _render_pot_menu
        # directly here.) Calling the renderer directly is the original,
        # correct behaviour.
        from .pot_menu import _render_pot_menu
        await _render_pot_menu(callback_query)

    elif data.startswith("admin_pot_action:"):
        await _handle_pot_action(client, callback_query, data.split(":")[1])

    elif data == "admin_direct_menu":
        await _render_direct_menu(callback_query)

    elif data == "admin_direct_toggle_ig":
        new_state = not config.IG_DIRECT_ENABLED
        if new_state and not getattr(config, "DIRECT_FORWARD_CHAT_ID", 0):
            await callback_query.answer(
                "Set DIRECT_FORWARD_CHAT_ID in .env first — the relay "
                "needs a destination chat.", show_alert=True)
            return
        set_key(".env", "IG_DIRECT_ENABLED", str(new_state).lower())
        config.IG_DIRECT_ENABLED = new_state
        if new_state:
            await callback_query.message.edit_text(
                "✅ Instagram direct-forward **enabled**.\n\n"
                "🔄 **Restarting the bot** to activate — back in a few seconds.\n\n"
                "After restart, tap **🔗 Pair IG** to complete the pairing handshake.",
                reply_markup=get_direct_menu_keyboard())
            await log_event("📨 **Admin Action:** IG direct-forward enabled. Auto-restarting.")
            await callback_query.answer()
            schedule_self_restart(delay=3.0)
        else:
            await callback_query.message.edit_text(
                "✅ Instagram direct-forward **disabled**. "
                "The worker will stop on next restart.",
                reply_markup=get_direct_menu_keyboard())
            await log_event("📨 **Admin Action:** IG direct-forward disabled.")
            await callback_query.answer("IG relay disabled", show_alert=True)

    elif data == "admin_direct_pair_ig":
        code = direct_forward.request_pair_code("ig", requested_by=user_id)
        await callback_query.message.edit_text(
            "🔗 **Instagram pairing handshake**\n\n"
            f"Your one-time code: **`{code}`**\n\n"
            f"1. Open Instagram on your phone.\n"
            f"2. Send this code (just the 6 digits) as a **direct message** "
            f"to the bot's Instagram account.\n"
            f"3. Within ~2 poll intervals the bot will confirm here that it "
            f"found your chat.\n\n"
            "The code expires in 10 minutes. Only messages in YOUR chat "
            "with the bot account will ever be relayed.",
            reply_markup=get_direct_menu_keyboard()
        )
        await log_event(f"📨 **Admin Action:** Instagram pairing code issued (user {user_id}).")
        await callback_query.answer()

    elif data == "admin_direct_unpair_ig":
        removed = direct_forward.unpair_platform("ig")
        await callback_query.message.edit_text(
            "💔 Instagram pairing removed. " if removed else "ℹ️ No Instagram pairing existed. ",
            reply_markup=get_direct_menu_keyboard()
        )
        await log_event("📨 **Admin Action:** Instagram DM pairing removed." if removed else
                        "📨 **Admin Action:** Instagram unpair requested (was unpaired).")
        await callback_query.answer("Pairing forgotten" if removed else "Nothing to forget",
                                    show_alert=True)

    elif data == "admin_direct_toggle_x":
        new_state = not config.X_DIRECT_ENABLED
        if new_state and not getattr(config, "DIRECT_FORWARD_CHAT_ID", 0):
            await callback_query.answer(
                "Set DIRECT_FORWARD_CHAT_ID in .env first — the relay "
                "needs a destination chat.", show_alert=True)
            return
        set_key(".env", "X_DIRECT_ENABLED", str(new_state).lower())
        config.X_DIRECT_ENABLED = new_state
        if new_state:
            await callback_query.message.edit_text(
                "✅ X/Twitter direct-forward **enabled**.\n\n"
                "🔄 **Restarting the bot** to activate — back in a few seconds.",
                reply_markup=get_direct_menu_keyboard())
            await log_event("📨 **Admin Action:** X direct-forward enabled. Auto-restarting.")
            await callback_query.answer()
            schedule_self_restart(delay=3.0)
        else:
            await callback_query.message.edit_text(
                "✅ X/Twitter direct-forward **disabled**. "
                "The worker will stop on next restart.",
                reply_markup=get_direct_menu_keyboard())
            await log_event("📨 **Admin Action:** X direct-forward disabled.")
            await callback_query.answer("X relay disabled", show_alert=True)

    elif data == "admin_direct_test_x":
        from modules import direct_forward
        result = direct_forward.test_x_connection()
        await callback_query.message.edit_text(result,
            reply_markup=get_direct_menu_keyboard())
        await callback_query.answer()

    elif data == "admin_direct_toggle_tiktok":
        new_state = not getattr(config, "TIKTOK_DIRECT_ENABLED", False)
        if new_state and not getattr(config, "DIRECT_FORWARD_CHAT_ID", 0):
            await callback_query.answer(
                "Set DIRECT_FORWARD_CHAT_ID in .env first — the relay "
                "needs a destination chat.", show_alert=True)
            return
        set_key(".env", "TIKTOK_DIRECT_ENABLED", str(new_state).lower())
        config.TIKTOK_DIRECT_ENABLED = new_state
        if new_state:
            await callback_query.message.edit_text(
                "✅ TikTok direct-forward **enabled**.\n\n"
                "🔄 **Restarting the bot** to activate — back in a few seconds.\n\n"
                "Send videos to your own TikTok **self-DM** (Message Yourself) "
                "and the bot relays them here.",
                reply_markup=get_direct_menu_keyboard())
            await log_event("📨 **Admin Action:** TikTok direct-forward enabled. Auto-restarting.")
            await callback_query.answer()
            schedule_self_restart(delay=3.0)
        else:
            await callback_query.message.edit_text(
                "✅ TikTok direct-forward **disabled**. "
                "The worker will stop on next restart.",
                reply_markup=get_direct_menu_keyboard())
            await log_event("📨 **Admin Action:** TikTok direct-forward disabled.")
            await callback_query.answer("TikTok relay disabled", show_alert=True)

    elif data == "admin_direct_test_tiktok":
        from modules import direct_forward
        result = await direct_forward.test_tiktok_connection()
        await callback_query.message.edit_text(result,
            reply_markup=get_direct_menu_keyboard())
        await callback_query.answer()

    elif data == "admin_direct_set_x_pin":
        # In-chat X Chat PIN entry (free-form text, NOT a telegram ID, so it
        # is dispatched before the is_valid_telegram_id gate in
        # admin_state_message_handler, like the premium session states).
        if getattr(config, "XCHAT_PIN", ""):
            cur = "set (hidden)" if config.XCHAT_PIN else "empty"
        else:
            cur = "empty"
        USER_STATES[user_id] = "waiting_for_x_pin"
        step_msg = await callback_query.message.edit_text(
            "🔑 **Set the X Chat PIN**\n\n"
            "The XChat-encrypted self-DM needs your **4-digit passcode** "
            "(the one you set in X Chat, NOT a Telegram code).\n\n"
            f"Current: `{cur}`\n\n"
            "Send the **4-digit PIN** as a message now — it is written to "
            "`.env` and the bridge picks it up automatically. No SSH needed.\n\n"
            "_(Tap ◀️ Back to Console to cancel.)_",
            reply_markup=back_markup
        )
        ACTIVE_PROMPTS[user_id] = step_msg.id
        await callback_query.answer()

    # =========================================================================
    # Subscription admin (💳)
    # =========================================================================
    elif data == "admin_sub_menu":
        from utils.subscription.store import get_settings, list_subscriptions
        from utils.subscription.tiers import TIERS, TIER_ORDER
        s = get_settings()
        subs = list_subscriptions()
        import time as _t2
        now = int(_t2.time())
        active_count = sum(1 for v in subs.values() if int(v.get("until", 0)) > now)
        en = "🟢 ON" if s.get("enabled") else "🔴 OFF"
        free = "✅" if s.get("free_enabled") else "❌"
        ch = s.get("channel_username") or (str(s.get("channel_id")) if s.get("channel_id") else "—")
        tier_lines = " · ".join(f"{TIERS[t]['label']}:{TIERS[t]['price_stars']}⭐" for t in TIER_ORDER)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{'🔴 Disable' if s.get('enabled') else '🟢 Enable'} subscription mode", callback_data="admin_sub_toggle")],
            [InlineKeyboardButton(f"Free tier: {free}", callback_data="admin_sub_toggle_free")],
            [InlineKeyboardButton("📢 Set channel", callback_data="admin_sub_set_channel"), InlineKeyboardButton("🌐 WebApp", callback_data="admin_sub_webapp")],
            [InlineKeyboardButton("➕ Grant sub", callback_data="admin_sub_grant"), InlineKeyboardButton("➖ Revoke sub", callback_data="admin_sub_revoke")],
            [InlineKeyboardButton("📋 List subs", callback_data="admin_sub_list"), InlineKeyboardButton("🔄 Refresh", callback_data="admin_sub_menu")],
            [InlineKeyboardButton("◀️ Back to Console", callback_data="admin_main")],
        ])
        await callback_query.message.edit_text(
            f"💳 **Subscriptions**\n\n"
            f"Mode: **{en}**\nFree tier: **{free}** (5/day, force-join)\nChannel: `{ch}`\n"
            f"Active subs: **{active_count}**\nTiers: {tier_lines}\n\n"
            f"Free users go last in the download queue (priority 0 vs 1-3). "
            f"WebApp at `/admin/subscription` (same port 8080).",
            reply_markup=kb
        )
        await callback_query.answer()

    elif data == "admin_sub_toggle":
        if not _sub_rate_ok(user_id):
            await callback_query.answer("Too fast — wait a moment.", show_alert=False)
            return
        from utils.subscription.store import get_settings, set_settings
        s = get_settings()
        ns = set_settings(enabled=not s.get("enabled"))
        await log_event(f"💳 **Admin:** Subscription mode toggled to {ns.get('enabled')}")
        # re-render by delegating
        callback_query.data = "admin_sub_menu"
        await _admin_callback_dispatch(client, callback_query)
        return

    elif data == "admin_sub_toggle_free":
        if not _sub_rate_ok(user_id):
            await callback_query.answer("Too fast — wait a moment.", show_alert=False)
            return
        from utils.subscription.store import get_settings, set_settings
        s = get_settings()
        ns = set_settings(free_enabled=not s.get("free_enabled"))
        await log_event(f"💳 **Admin:** Free tier toggled to {ns.get('free_enabled')}")
        callback_query.data = "admin_sub_menu"
        await _admin_callback_dispatch(client, callback_query)
        return

    elif data == "admin_sub_set_channel":
        USER_STATES[user_id] = "waiting_for_sub_channel"
        ACTIVE_PROMPTS[user_id] = callback_query.message.id
        await callback_query.message.edit_text(
            "📢 **Set force-join channel**\n\n"
            "Send the channel **@username** (e.g. `@mychannel`) or numeric ID (e.g. `-100123...`).\n"
            "Send `0` or `clear` to remove the requirement (free tier without join).\n\n"
            "_Free users must be members to download when subscription mode is ON._",
            reply_markup=back_markup
        )
        await callback_query.answer()

    elif data == "admin_sub_webapp":
        host = getattr(config, "SSL_CERT_PATH", "") and "https" or "http"
        hint = "Open `http(s)://<your-vps>:8080/admin/subscription`"
        try:
            from modules.subscription.webapp import _admin_token
            tok = _admin_token()
            hint += f"\nAdmin-Token: `{tok}` (or use Telegram WebApp inside the bot)"
        except Exception:
            pass
        await callback_query.message.edit_text(
            f"🌐 **Subscription WebApp**\n\n{hint}\n\n"
            "Best opened as a Telegram WebApp (Admin → Subscription → 🌐 WebApp) — "
            "Telegram `initData` authenticates you automatically. Outside Telegram, paste the token in the page.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_sub_menu")]])
        )
        await callback_query.answer()

    elif data == "admin_sub_grant":
        USER_STATES[user_id] = "waiting_for_sub_grant"
        ACTIVE_PROMPTS[user_id] = callback_query.message.id
        await callback_query.message.edit_text(
            "➕ **Grant subscription**\n\n"
            "Send: `<user_id> <tier> [days]`\n"
            "Example: `123456789 plus 30`  or  `123456789 pro`\n"
            "Tiers: `basic` (100/d), `plus` (500/d), `pro` (2500/d). Default 30 days.",
            reply_markup=back_markup
        )
        await callback_query.answer()

    elif data == "admin_sub_revoke":
        USER_STATES[user_id] = "waiting_for_sub_revoke"
        ACTIVE_PROMPTS[user_id] = callback_query.message.id
        await callback_query.message.edit_text(
            "➖ **Revoke subscription**\n\nSend the **user ID** to revoke:",
            reply_markup=back_markup
        )
        await callback_query.answer()

    elif data == "admin_sub_list":
        from utils.subscription.store import list_subscriptions
        subs = list_subscriptions()
        if not subs:
            txt = "No subscriptions stored."
        else:
            import time as _t3
            now = int(_t3.time())
            lines = []
            for uid, sub in sorted(subs.items(), key=lambda kv: kv[1].get("until", 0), reverse=True)[:30]:
                until = sub.get("until", 0)
                state = "✅" if until > now else "⌛ expired"
                lines.append(f"`{uid}` — {sub.get('tier')} until {until} {state}")
            txt = "📋 **Subscriptions (up to 30):**\n" + "\n".join(lines)
        await callback_query.message.edit_text(txt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_sub_menu")]]))
        await callback_query.answer()


# Helper function for internal use
async def _purge_active_prompt(user_id: int, client: Client):
    """Helper to safely delete any active ForceReply prompt bubble from the chat stream."""
    prompt_id = ACTIVE_PROMPTS.pop(user_id, None)
    if prompt_id:
        try:
            await client.delete_messages(chat_id=user_id, message_ids=prompt_id)
        except Exception:
            pass
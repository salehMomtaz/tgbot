"""
Admin handler registration — thin orchestrator that wires all sub-modules.

Mirrors the original modules/admin.py register_admin_handlers exactly.
"""

import os
import asyncio
import logging
import config
from pyrogram import Client, filters, ContinuePropagation
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import set_key
from main import log_event
from utils.gate import (
    load_database,
    add_user,
    remove_user,
    blacklist_user,
    unblacklist_user,
    is_blacklisted,
    is_authorized,
    is_premium_user,
    add_premium_user,
    remove_premium_user,
    toggle_document_mode,
)
from utils.id_validator import is_valid_telegram_id

from .state import USER_STATES, ACTIVE_PROMPTS
from .keyboards import back_markup
from .premium_gen import (
    _premium_gen_cleanup,
    _handle_premium_gen_input,
)
from .cookies import COOKIE_MAP
from .callback_dispatch import _admin_callback_dispatch, _purge_active_prompt

logger = logging.getLogger(__name__)


def register_admin_handlers(app: Client):
    # =========================================================================
    # Group -1: Global Interceptor Gate (Strict Security Shield)
    # =========================================================================
    @app.on_message(filters.private, group=-1)
    async def security_gate(client: Client, message: Message):
        if not message.from_user:
            message.stop_propagation()

        user_id = message.from_user.id
        if is_blacklisted(user_id):
            message.stop_propagation()

        if not is_authorized(user_id):
            blacklist_user(user_id)
            await log_event(f"⚠️ **Intruder Blocked:** User `{user_id}` has been banned and blacklisted.")
            message.stop_propagation()

    # =========================================================================
    # Group 0: State Machine Handler (Processes text inputs ONLY if user is in an active state)
    # =========================================================================
    @app.on_message(
        filters.text &
        filters.private &
        filters.create(lambda _, __, m: m.from_user.id in USER_STATES),
        group=0
    )
    async def admin_state_message_handler(client: Client, message: Message):
        user_id = message.from_user.id
        state = USER_STATES.get(user_id)
        input_text = message.text.strip()

        # Failsafe escape: If you type /start or console triggers, clear active state and let Group 1 handle it
        if input_text.lower() in ["/start", "🛠 console", "hey", "console", "hi!"]:
            USER_STATES.pop(user_id, None)
            await _purge_active_prompt(user_id, client)
            # Clean up any in-flight premium session generation so the temp
            # login client is never left dangling.
            from .state import PREMIUM_GEN
            from utils.premium_session import discard_client
            gen = PREMIUM_GEN.pop(user_id, None)
            if gen and gen.get("client"):
                await discard_client(gen["client"])
            return  # Propagates downstream to Group 1

        # We do NOT delete your typed message here anymore (it remains in your history)
        prompt_id = ACTIVE_PROMPTS.pop(user_id, None)

        # 1. Cookie Replacement via pasted text is REJECTED.
        # Telegram silently truncates plain-text messages near 4096 chars; a real
        # Netscape jar (YouTube ≈ 17 KB) gets cut and the bot would persist a
        # broken jar that the site then rejects ("sign in to confirm you're not a
        # bot"). Cookies must arrive as a .txt document, handled by the document
        # handler below. We keep the state so the admin can send the file next.
        if state.startswith("waiting_for_replace_"):
            await message.reply_text(
                "❌ Cookies can't be pasted as text — Telegram truncates long "
                "messages and corrupts the jar.\n\n"
                "Please send your cookies as a **`.txt` document file** instead.",
                reply_markup=back_markup
            )
            message.stop_propagation()
            return

        # 1b. Per-site cookie jar: user is typing the site name. Validate it,
        # then flip the state to waiting_for_replace_per_site_<name> so the
        # document handler below stores the file at cookies/ytdlp/<name>.txt.
        if state == "waiting_for_per_site_name":
            import re
            site_name = input_text.strip().lower()
            if not re.fullmatch(r"[a-z0-9][a-z0-9.\-]*", site_name):
                await message.reply_text(
                    "❌ Invalid site name. Use only letters, numbers, dashes and dots "
                    "(e.g. `reddit`, `tiktok`, `my-site`).",
                    reply_markup=back_markup
                )
                message.stop_propagation()
                return
            USER_STATES[user_id] = f"waiting_for_replace_per_site_{site_name}"
            await message.reply_text(
                f"✅ Site set to **`{site_name}`**.\n\n"
                f"Now send the **`.txt` cookie file** for `cookies/ytdlp/{site_name}.txt` "
                "as a document.",
                reply_markup=back_markup
            )
            message.stop_propagation()
            return

        # 1c. Premium session-generation states (phone -> code -> 2FA password).
        # These take free-form text, NOT a telegram ID, so they must be handled
        # before the is_valid_telegram_id gate below. Each step's prompt carries
        # an Abort button; the temp client is always cleaned up (disconnect) on
        # completion, abort, /start escape, or TTL expiry.
        if state in ("waiting_for_premium_phone", "waiting_for_premium_code", "waiting_for_premium_password"):
            try:
                await _handle_premium_gen_input(client, message, user_id, state, input_text, prompt_id, app, back_markup)
            except Exception as e:
                logger.exception(f"[AdminGen] Error in {state}: {e}")
                await _premium_gen_cleanup(user_id, app)
                await message.reply_text(
                    "❌ Something went wrong in the session generation flow. The temp "
                    "login client was cleaned up — please start again from the 👑 Premium menu.",
                    reply_markup=back_markup
                )
            message.stop_propagation()
            return

        # 1d. X Chat PIN entry (free-form text). Validates the 4-digit passcode,
        # writes it to .env via dotenv.set_key (dotenv-style quoting — safe for
        # run.sh's parser and the bridge wrapper), refreshes config.XCHAT_PIN so
        # the menu status updates, and tells the operator the bridge will pick it
        # up on its own (tools/start_xchat_bridge.sh re-reads .env every ~5 s).
        if state == "waiting_for_x_pin":
            import re as _re
            pin = input_text.strip()
            if not _re.fullmatch(r"\d{4}", pin):
                await message.reply_text(
                    "❌ X Chat passcodes are **4 digits** (e.g. `0421`).\n\n"
                    "Send the correct PIN, or tap ◀️ Back to Console to cancel.",
                    reply_markup=back_markup
                )
                message.stop_propagation()
                return
            set_key(".env", "XCHAT_PIN", pin)
            config.XCHAT_PIN = pin
            USER_STATES.pop(user_id, None)
            if prompt_id:
                try:
                    await client.delete_messages(chat_id=user_id, message_ids=prompt_id)
                except Exception:
                    pass
            from .keyboards import get_direct_menu_keyboard
            await message.reply_text(
                f"✅ X Chat PIN saved to `.env` (written as `XCHAT_PIN='{pin}'`).\n\n"
                "🔄 The bridge supervisor re-reads `.env` every few seconds and "
                "restarts the sidecar automatically — **no SSH, no restart needed**.\n\n"
                "If X relay is enabled, encrypted self-DM messages will start "
                "arriving within a minute. Tap **🔄 Refresh** to re-check status.",
                reply_markup=get_direct_menu_keyboard()
            )
            await log_event("🔑 **Admin Action:** X Chat PIN updated in-chat.")
            message.stop_propagation()
            return

        # 1e. Subscription free-form states (must be handled before the telegram-ID gate)
        if state == "waiting_for_sub_channel":
            txt = input_text.strip()
            if txt.lower() in ("0", "clear", "remove", "none", "-"):
                from utils.subscription.store import set_settings as _set_sub
                _set_sub(channel_id=0, channel_username="")
                USER_STATES.pop(user_id, None)
                if prompt_id:
                    try:
                        await client.delete_messages(chat_id=user_id, message_ids=prompt_id)
                    except Exception:
                        pass
                await message.reply_text("✅ Force-join channel **removed** (free tier without channel).", reply_markup=back_markup)
                await log_event("💳 **Admin:** Force-join channel cleared.")
                message.stop_propagation()
                return
            # Accept @username or numeric ID (channels are negative)
            channel_id = 0
            channel_username = ""
            if txt.startswith("@"):
                channel_username = txt.strip()
                # try to resolve to numeric id for member checks; best-effort
                try:
                    chat = await client.get_chat(channel_username)
                    channel_id = int(getattr(chat, "id", 0) or 0)
                except Exception:
                    channel_id = 0
            else:
                # numeric id
                try:
                    channel_id = int(txt)
                    # if 0 or not plausible, treat as username without @
                    if -9999999999999 <= channel_id <= 9999999999999:
                        pass
                    else:
                        raise ValueError
                    if channel_username == "" and txt.lstrip("-").isdigit():
                        channel_username = ""
                except Exception:
                    # maybe bare username without @
                    channel_username = "@" + txt.lstrip("@")
                    try:
                        chat = await client.get_chat(channel_username)
                        channel_id = int(getattr(chat, "id", 0) or 0)
                    except Exception:
                        channel_id = 0
            from utils.subscription.store import set_settings as _set_sub2
            _set_sub2(channel_id=channel_id, channel_username=channel_username)
            USER_STATES.pop(user_id, None)
            if prompt_id:
                try:
                    await client.delete_messages(chat_id=user_id, message_ids=prompt_id)
                except Exception:
                    pass
            await message.reply_text(
                f"✅ Force-join channel set to `{channel_username or channel_id}`\n"
                f"(id: `{channel_id}` username: `{channel_username or '—'}`).",
                reply_markup=back_markup
            )
            await log_event(f"💳 **Admin:** Force-join channel set to {channel_username or channel_id} (id {channel_id}).")
            message.stop_propagation()
            return

        if state == "waiting_for_sub_grant":
            # Expected: <user_id> <tier> [days]
            parts = input_text.strip().split()
            if len(parts) < 2:
                await message.reply_text(
                    "❌ Format: `<user_id> <tier> [days]`\nExample: `123456789 plus 30`",
                    reply_markup=back_markup
                )
                message.stop_propagation()
                return
            uid_txt, tier_txt = parts[0], parts[1].lower()
            days_txt = parts[2] if len(parts) > 2 else "30"
            if not is_valid_telegram_id(uid_txt):
                await message.reply_text("❌ Invalid user ID (5-11 digits).", reply_markup=back_markup)
                message.stop_propagation()
                return
            from utils.subscription.tiers import TIERS
            if tier_txt not in TIERS or tier_txt == "free":
                await message.reply_text(f"❌ Invalid tier `{tier_txt}`. Use: basic / plus / pro.", reply_markup=back_markup)
                message.stop_propagation()
                return
            try:
                days = int(days_txt)
                if not 1 <= days <= 3650:
                    raise ValueError
            except Exception:
                await message.reply_text("❌ Days must be 1..3650.", reply_markup=back_markup)
                message.stop_propagation()
                return
            target_id = int(uid_txt)
            from utils.subscription.store import set_subscription as _grant
            entry = _grant(target_id, tier_txt, duration_days=days, granted_by=f"admin:{user_id}")
            USER_STATES.pop(user_id, None)
            if prompt_id:
                try:
                    await client.delete_messages(chat_id=user_id, message_ids=prompt_id)
                except Exception:
                    pass
            await message.reply_text(
                f"✅ Granted **{tier_txt}** to `{target_id}` for {days} days (until `{entry['until']}`).",
                reply_markup=back_markup
            )
            await log_event(f"💳 **Admin:** Granted {tier_txt} ({days}d) to {target_id} (until {entry['until']}).")
            # notify recipient best-effort
            try:
                from utils.subscription.tiers import TIERS as _T
                await client.send_message(target_id, f"✅ You received **{_T[tier_txt]['label']}** for {days} days. Use /subscription to see status.")
            except Exception:
                pass
            message.stop_propagation()
            return

        if state == "waiting_for_sub_revoke":
            if not is_valid_telegram_id(input_text):
                await message.reply_text("❌ Invalid user ID (5-11 digits).", reply_markup=back_markup)
                message.stop_propagation()
                return
            target_id = int(input_text)
            from utils.subscription.store import remove_subscription as _revoke
            ok = _revoke(target_id)
            USER_STATES.pop(user_id, None)
            if prompt_id:
                try:
                    await client.delete_messages(chat_id=user_id, message_ids=prompt_id)
                except Exception:
                    pass
            if ok:
                await message.reply_text(f"✅ Revoked subscription for `{target_id}`.", reply_markup=back_markup)
                await log_event(f"💳 **Admin:** Revoked subscription for {target_id}.")
            else:
                await message.reply_text(f"ℹ️ No active subscription for `{target_id}`.", reply_markup=back_markup)
            message.stop_propagation()
            return

        # 2. Handle User ID Input States (Add, Remove, Unban)
        if not is_valid_telegram_id(input_text):
            USER_STATES.pop(user_id, None)
            if prompt_id:
                try:
                    await client.delete_messages(chat_id=user_id, message_ids=prompt_id)
                except Exception:
                    pass
            await message.reply_text(
                "❌ Error: Invalid Telegram ID. Please input digits only (between 5 and 11 numbers).",
                reply_markup=back_markup
            )
            message.stop_propagation()
            return

        target_id = int(input_text)
        USER_STATES.pop(user_id, None)  # Reset state

        # Delete the bot's old prompt message
        if prompt_id:
            try:
                await client.delete_messages(chat_id=user_id, message_ids=prompt_id)
            except Exception:
                pass

        if state == "waiting_for_add_user":
            if add_user(target_id):
                await message.reply_text(
                    f"✅ User `{target_id}` has been authorized successfully.",
                    reply_markup=back_markup
                )
                await log_event(f"👥 **User Whitelisted:** Creator whitelisted User ID `{target_id}`.")
            else:
                await message.reply_text(
                    f"ℹ️ User `{target_id}` was already authorized.",
                    reply_markup=back_markup
                )

        elif state == "waiting_for_remove_user":
            db = load_database()
            if target_id not in db["authorized"]:
                await message.reply_text(
                    f"❌ Error: User ID `{target_id}` is not currently authorized.",
                    reply_markup=back_markup
                )
                message.stop_propagation()
                return

            if remove_user(target_id):
                await message.reply_text(
                    f"✅ User `{target_id}` has been removed.",
                    reply_markup=back_markup
                )
                await log_event(f"👥 **User Revoked:** Creator removed User ID `{target_id}`.")

        elif state == "waiting_for_unban":
            db = load_database()
            if target_id not in db["blacklisted"]:
                await message.reply_text(
                    f"❌ Error: User ID `{target_id}` is not found in the blacklist.",
                    reply_markup=back_markup
                )
                message.stop_propagation()
                return

            if unblacklist_user(target_id):
                await message.reply_text(
                    f"✅ User `{target_id}` has been unbanned.",
                    reply_markup=back_markup
                )
                await log_event(f"🔓 **User Unbanned:** Creator unbanned and unblacklisted User ID `{target_id}`.")

        elif state == "waiting_for_add_premium":
            if add_premium_user(target_id):
                await message.reply_text(
                    f"✅ User `{target_id}` is now enabled for **4 GB Premium uploads**.",
                    reply_markup=back_markup
                )
                await log_event(f"👑 **Premium Granted:** Creator enabled 4 GB uploads for User ID `{target_id}`.")
            else:
                await message.reply_text(
                    f"ℹ️ User `{target_id}` is already Premium-enabled.",
                    reply_markup=back_markup
                )

        elif state == "waiting_for_remove_premium":
            db = load_database()
            if target_id not in db["premium_users"]:
                await message.reply_text(
                    f"❌ Error: User ID `{target_id}` is not in the Premium whitelist.",
                    reply_markup=back_markup
                )
                message.stop_propagation()
                return

            if remove_premium_user(target_id):
                await message.reply_text(
                    f"✅ User `{target_id}` Premium upload access revoked.",
                    reply_markup=back_markup
                )
                await log_event(f"👑 **Premium Revoked:** Creator removed User ID `{target_id}` from Premium uploads.")

        message.stop_propagation()

    # =========================================================================
    # Group 0b: Cookie Jar Replacement via Document File (optional convenience)
    # =========================================================================
    @app.on_message(
        filters.document &
        filters.private &
        filters.create(lambda _, __, m: m.from_user.id == config.SYSTEM_CREATOR_ID) &
        filters.create(lambda _, __, m: USER_STATES.get(m.from_user.id, "").startswith("waiting_for_replace_")),
        group=0
    )
    async def admin_cookie_replace_document_handler(client: Client, message: Message):
        user_id = message.from_user.id
        state = USER_STATES.pop(user_id, None)
        cookie_key = state.split("waiting_for_replace_")[1]
        file_path = COOKIE_MAP.get(cookie_key)

        prompt_id = ACTIVE_PROMPTS.pop(user_id, None)
        if prompt_id:
            try:
                await client.delete_messages(chat_id=user_id, message_ids=prompt_id)
            except Exception:
                pass

        if not file_path:
            await message.reply_text("❌ Error: Invalid cookie profile selected.", reply_markup=back_markup)
            return

        doc = message.document
        file_name = (doc.file_name or "").lower()
        mime = (doc.mime_type or "").lower()
        if not (file_name.endswith(".txt") or mime.startswith("text/")):
            await message.reply_text(
                "❌ Invalid file type. Please send a `.txt` cookie jar file (or paste the text).",
                reply_markup=back_markup
            )
            return

        try:
            buffer = await client.download_media(message=message, in_memory=True)
            # pyrogram 2.0.106 returns the in-memory BytesIO with the cursor left
            # at EOF, so buffer.read() yields b"" and every uploaded jar looks
            # "empty" (only the synthetic header survives → "no valid Netscape
            # cookie lines"). getvalue() returns the full bytes regardless of the
            # cursor position, which is what we actually want here.
            content = buffer.getvalue().decode("utf-8", errors="replace")
        except Exception as e:
            await message.reply_text(f"❌ Failed to download file: {e}", reply_markup=back_markup)
            return

        try:
            from .cookies import _write_cookie_jar
            _write_cookie_jar(cookie_key, file_path, content)
            await message.reply_text(f"✅ `{cookie_key}.txt` successfully replaced from document!", reply_markup=back_markup)
            await log_event(f"🍪 **Admin Action:** Cookie profile `{cookie_key}.txt` was replaced via document.")
        except Exception as e:
            await message.reply_text(f"❌ Failed to write cookie file: {e}", reply_markup=back_markup)

    # Handler for per-site jar uploads (stored under cookies/ytdlp/<site>.txt)
    @app.on_message(
        filters.document &
        filters.private &
        filters.create(lambda _, __, m: m.from_user.id == config.SYSTEM_CREATOR_ID) &
        filters.create(lambda _, __, m: USER_STATES.get(m.from_user.id, "").startswith("waiting_for_replace_per_site_")),
        group=0
    )
    async def admin_per_site_cookie_document_handler(client: Client, message: Message):
        user_id = message.from_user.id
        state = USER_STATES.pop(user_id, None)
        site_name = state.split("waiting_for_replace_per_site_")[1]
        ytdlp_dir = getattr(config, "YTDLP_COOKIES_DIR", "cookies/ytdlp")
        os.makedirs(ytdlp_dir, exist_ok=True)
        file_path = os.path.join(ytdlp_dir, f"{site_name}.txt")

        prompt_id = ACTIVE_PROMPTS.pop(user_id, None)
        if prompt_id:
            try:
                await client.delete_messages(chat_id=user_id, message_ids=prompt_id)
            except Exception:
                pass

        doc = message.document
        file_name = (doc.file_name or "").lower()
        mime = (doc.mime_type or "").lower()
        if not (file_name.endswith(".txt") or mime.startswith("text/")):
            await message.reply_text(
                "❌ Invalid file type. Please send a `.txt` cookie jar file.",
                reply_markup=back_markup
            )
            return

        try:
            buffer = await client.download_media(message=message, in_memory=True)
            content = buffer.getvalue().decode("utf-8", errors="replace")
        except Exception as e:
            await message.reply_text(f"❌ Failed to download file: {e}", reply_markup=back_markup)
            return

        try:
            from .cookies import _write_cookie_jar
            _write_cookie_jar(f"ytdlp_{site_name}", file_path, content)
            await message.reply_text(
                f"✅ Per-site cookie jar saved to `cookies/ytdlp/{site_name}.txt`!",
                reply_markup=back_markup
            )
            await log_event(f"🍪 **Admin Action:** Per-site cookie jar `cookies/ytdlp/{site_name}.txt` uploaded.")
        except Exception as e:
            await message.reply_text(f"❌ Failed to write cookie file: {e}", reply_markup=back_markup)

    # =========================================================================
    # Group 1: Main Text Router (Handles plain text commands and greetings)
    # =========================================================================
    @app.on_message(filters.text & filters.private, group=1)
    async def admin_start_text_handler(client: Client, message: Message):
        text = message.text.strip()
        user_id = message.from_user.id

        from modules.downloader_handler import is_link
        if is_link(text):
            # Pass link down to downloader_handler
            raise ContinuePropagation()

        if user_id == config.SYSTEM_CREATOR_ID:
            from .keyboards import build_console_keyboard
            keyboard = build_console_keyboard(user_id)
            await message.reply_text(
                "🛠 **Admin System Console**\nChoose an administrative action below:",
                reply_markup=keyboard
            )
        else:
            await message.reply_text(
                "👋 **Hello! Welcome to your Private Downloader Bot.**\n\n"
                "To get started:\n"
                "• Send me any YouTube, Instagram, TikTok, or X/Twitter link to download it.\n"
                "• Send me any direct file URL to upload it directly to Telegram.\n"
                "• Forward me a Telegram file (video, document, music) to generate an instant direct stream link."
            )
        message.stop_propagation()

    # =========================================================================
    # Group 2: Callback Query Dispatcher (Console clicks)
    # =========================================================================
    @app.on_callback_query(filters.regex(r"^admin_"))
    async def admin_callback_handler(client: Client, callback_query: CallbackQuery):
        # Any exception in the dispatch below must still resolve the callback
        # spinner and be logged — an unanswered callback looks like "the bot is
        # dead" to the user. Admin console buttons re-render/answer at the end.
        try:
            await _admin_callback_dispatch(client, callback_query)
        except Exception as e:
            # Re-tapping an already-open admin menu re-edits the message with
            # identical content -> Telegram 400 MESSAGE_NOT_MODIFIED. That is a
            # benign no-op, not an error: resolve the callback spinner quietly
            # and skip the traceback + scare alert.
            if "MESSAGE_NOT_MODIFIED" in str(e) or "MessageNotModified" in type(e).__name__:
                try:
                    await callback_query.answer()
                except Exception:
                    pass
                return
            logger.exception(f"[AdminCallback] Error handling {callback_query.data!r}: {e}")
            try:
                await callback_query.answer("⚠️ An internal error occurred. See log channel.", show_alert=True)
            except Exception:
                pass
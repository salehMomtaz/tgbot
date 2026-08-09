# modules/admin.py
import os
import shutil
import asyncio
import logging
import yt_dlp
from pyrogram import Client, filters, ContinuePropagation
from pyrogram.types import (
    CallbackQuery,
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
import config
from utils.gate import (
    load_database,
    add_user,
    remove_user,
    unblacklist_user,
    is_document_mode,
    toggle_document_mode,
    is_blacklisted,
    blacklist_user,
    is_authorized,
    is_premium_user,
    add_premium_user,
    remove_premium_user,
)
from utils.id_validator import is_valid_telegram_id

logger = logging.getLogger(__name__)

# Map callback string shortcuts to physical filenames
COOKIE_MAP = {
    "ytcookies": config.YT_COOKIES,
    "igcookies": config.IG_COOKIES,
    "ttcookies": config.TT_COOKIES,
    "xcookies": config.X_COOKIES,
    "cookies": config.COOKIES_FILE,
}

# In-memory dictionary to track administrative states per user
USER_STATES = {}

# In-memory registry to track and delete active ForceReply prompts on cancel or success
ACTIVE_PROMPTS = {}

# ---------------------------------------------------------------------------
# In-chat Premium session-string generation state.
# PREMIUM_GEN[user_id] = {
#     "client": temp in-memory Client, "phone": str, "phone_code_hash": str,
#     "code_buffer": str (digits tapped on the dial pad, Step 2/3),
#     "result": str|None, "expires_at": float
# }
# ---------------------------------------------------------------------------
PREMIUM_GEN = {}
_PREMIUM_GEN_TTL = 15 * 60  # auto-abort a dangling generation after 15 min

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


async def sweep_stale_generations(client=None):
    """Disconnect any premium-session generation that exceeded its TTL.

    Background safety net (driven by utils.keyboard_expiry.expiry_loop): a temp
    login client must never dangle just because the admin walked away mid-flow.
    """
    import time as _t
    now = _t.monotonic()
    for user_id, gen in list(PREMIUM_GEN.items()):
        if gen.get("expires_at", 0) < now:
            if gen.get("client"):
                try:
                    from utils.premium_session import discard_client
                    await discard_client(gen["client"])
                except Exception:
                    pass
            PREMIUM_GEN.pop(user_id, None)
            USER_STATES.pop(user_id, None)
            prompt_id = ACTIVE_PROMPTS.pop(user_id, None)
            if prompt_id and client:
                try:
                    await client.delete_messages(chat_id=user_id, message_ids=prompt_id)
                except Exception:
                    pass


def _pot_running() -> bool:
    """True if the PO-token provider manager exists and reports healthy."""
    try:
        import utils.shared as shared
        manager = getattr(shared, "pot_manager_instance", None)
        return bool(manager and manager.is_running())
    except Exception:
        return False


def build_console_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Main admin console keyboard. PO Token badge reflects live provider health."""
    doc_status = "✅" if is_document_mode(user_id) else "❌"
    pot_status = "🟢" if _pot_running() else "🔴"
    premium_status = "🟢" if config.PREMIUM_STRING_SESSION else "⚪"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 List Users", callback_data="admin_list"),
         InlineKeyboardButton("➕ Add User", callback_data="admin_add")],
        [InlineKeyboardButton("➖ Remove User", callback_data="admin_remove"),
         InlineKeyboardButton("🚫 Blacklist Logs", callback_data="admin_blacklist")],
        [InlineKeyboardButton(f"📄 Doc Mode: {doc_status}", callback_data="admin_toggle_doc"),
         InlineKeyboardButton("🍪 Cookie Jars", callback_data="admin_cookies_menu")],
        [InlineKeyboardButton(f"👑 Premium Uploads: {premium_status}", callback_data="admin_premium_menu"),
         InlineKeyboardButton(f"🔐 PO Token: {pot_status}", callback_data="admin_pot_menu")],
        [InlineKeyboardButton("💥 Abort Transfer", callback_data="admin_abort_queue"),
         InlineKeyboardButton("📨 Direct-Forward", callback_data="admin_direct_menu")],
        [InlineKeyboardButton("🔄 Restart Bot", callback_data="admin_restart"),
         InlineKeyboardButton("❌ Close Console", callback_data="admin_close")]
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
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(ig_label, callback_data="admin_direct_toggle_ig"),
         InlineKeyboardButton("🔗 Pair IG", callback_data="admin_direct_pair_ig")],
        [InlineKeyboardButton("💔 Unpair IG", callback_data="admin_direct_unpair_ig")],
        [InlineKeyboardButton(x_label, callback_data="admin_direct_toggle_x"),
         InlineKeyboardButton("🧪 Test X Cookies", callback_data="admin_direct_test_x")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="admin_direct_menu"),
         InlineKeyboardButton("◀️ Back to Console", callback_data="admin_main")]
    ])


def register_admin_handlers(app: Client):

    from main import log_event
    from utils.shared import queue  # Fixed: Import from clean shared registry

    # Reusable "Back to Console" inline button
    back_markup = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back to Console", callback_data="admin_main")]])

    async def purge_active_prompt(user_id: int, client: Client):
        """Helper to safely delete any active ForceReply prompt bubble from the chat stream."""
        prompt_id = ACTIVE_PROMPTS.pop(user_id, None)
        if prompt_id:
            try:
                await client.delete_messages(chat_id=user_id, message_ids=prompt_id)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # In-chat Premium session-string generation flow.
    # State names: waiting_for_premium_phone / _code / _password
    # ------------------------------------------------------------------
    async def _premium_gen_cleanup(user_id: int):
        """Disconnect the temp login client and clear all gen flow state."""
        gen = PREMIUM_GEN.pop(user_id, None)
        if gen and gen.get("client"):
            await discard_client_quiet(gen["client"])
        USER_STATES.pop(user_id, None)
        await purge_active_prompt(user_id, app)

    async def discard_client_quiet(tmp_client):
        try:
            await tmp_client.disconnect()
        except Exception:
            pass

    async def _premium_gen_pad_text(callback_query, gen):
        """Re-render the Step 2/3 dial-pad message with the digits entered so far."""
        code = gen.get("code_buffer", "")
        shown = " ".join(list(code)) if code else "_ (empty) _"
        try:
            await callback_query.message.edit_text(
                "🔑 **Step 2/3 — Enter the login code**\n\n"
                f"Code sent to `{gen.get('phone', '')}`. Enter it with the **dial pad below** — "
                "tap the digits like a phone call app, then **✓** when done.\n\n"
                f"**Entered so far:** `{shown}`\n\n"
                "_(Do NOT type the code as a message: Telegram flags codes typed "
                "into a chat as 'shared' and instantly invalidates them.)_\n\n"
                "_(Tap Abort at any time to cancel.)_",
                reply_markup=_gen_dial_pad_markup
            )
        except Exception:
            pass

    async def _handle_premium_gen_input(client: Client, message: Message,
                                        user_id: int, state: str, text: str, prompt_id):
        """Process one text step of the in-chat premium session generation."""
        from utils import premium_session
        import time as _time

        # If a temp login is mid-flight it carries an expiry; a stale flow that
        # somehow survived (no callback, no /start) is auto-aborted here.
        gen = PREMIUM_GEN.get(user_id)
        if gen and gen.get("expires_at", 0) < _time.monotonic():
            await _premium_gen_cleanup(user_id)
            await message.reply_text(
                "⏱️ Session generation timed out — please start again from the 👑 Premium menu.",
                reply_markup=back_markup
            )
            return

        # The code step's "prompt" is the live dial-pad message (Step 2/3) which
        # must survive text that the user types instead of tapping digits — only
        # delete the prompt for the phone/password steps, and re-register the
        # dial pad so abort/cleanup still finds it.
        if prompt_id and state != "waiting_for_premium_code":
            try:
                await client.delete_messages(chat_id=user_id, message_ids=prompt_id)
            except Exception:
                pass
        elif prompt_id and state == "waiting_for_premium_code":
            ACTIVE_PROMPTS[user_id] = prompt_id

        if state == "waiting_for_premium_phone":
            phone = text.strip().replace(" ", "")
            if not (phone.startswith("+") and phone[1:].isdigit() and 8 <= len(phone[1:]) <= 15):
                await message.reply_text(
                    "❌ Please send a valid international phone number **with country code**, "
                    "e.g. `+15551234567`.\n\n"
                    "_(You can still tap Abort to cancel.)_",
                    reply_markup=_gen_abort_markup
                )
                return
            tmp = None
            try:
                tmp = await premium_session.create_login_client()
                await premium_session.request_code(tmp)
                phone_code_hash = await premium_session.send_login_code(tmp, phone)
            except Exception as e:
                # tmp may not be stored in PREMIUM_GEN yet — disconnect it directly.
                if tmp is not None:
                    try:
                        await premium_session.discard_client(tmp)
                    except Exception:
                        pass
                await _premium_gen_cleanup(user_id)
                await message.reply_text(
                    f"❌ Could not request a login code:\n`{e}`\n\n"
                    "Tap **🔑 Generate Session** in the 👑 Premium menu to retry.",
                    reply_markup=back_markup
                )
                return
            PREMIUM_GEN[user_id] = {
                "client": tmp,
                "phone": phone,
                "phone_code_hash": phone_code_hash,
                "code_buffer": "",
                "result": None,
                "expires_at": _time.monotonic() + _PREMIUM_GEN_TTL,
            }
            USER_STATES[user_id] = "waiting_for_premium_code"
            step_msg = await message.reply_text(
                "🔑 **Step 2/3 — Enter the login code**\n\n"
                f"Code sent to `{phone}`. Enter it with the **dial pad below** — "
                "tap the digits like a phone call app, then **✓** when done.\n\n"
                "_(Do NOT type the code as a message: Telegram flags codes typed "
                "into a chat as 'shared' and instantly invalidates them.)_\n\n"
                "_(Tap Abort at any time to cancel.)_",
                reply_markup=_gen_dial_pad_markup
            )
            ACTIVE_PROMPTS[user_id] = step_msg.id
            return

        if state == "waiting_for_premium_code":
            # The code MUST be entered via the dial pad buttons (callback data).
            # A code typed as a chat message is detected by Telegram's
            # anti-sharing logic and rejected server-side within seconds —
            # accept it here only to tell the user why it won't work.
            await message.reply_text(
                "❌ Don't type the code as a message — Telegram flags codes sent "
                "in a chat as 'previously shared by your account' and they stop "
                "working instantly.\n\n"
                "Please use the **dial pad** on the Step 2 message to enter the "
                "code, then tap **✓**.",
                reply_markup=_gen_abort_markup
            )
            return

        if state == "waiting_for_premium_password":
            gen = PREMIUM_GEN.get(user_id)
            if not gen:
                await message.reply_text("⚠️ Session generation expired. Start again from the 👑 Premium menu.", reply_markup=back_markup)
                return
            password = text.strip()
            try:
                await premium_session.verify_password(gen["client"], password)
            except Exception as e:
                await message.reply_text(
                    f"❌ Wrong 2FA password: `{e}`.\n\n"
                    "Send it again, or tap **Abort** to cancel.",
                    reply_markup=_gen_abort_markup
                )
                return
            await _finish_premium_gen(client, message, user_id)
            return

    async def _finish_premium_gen(client: Client, message: Message, user_id: int):
        """Export the session string, clean up the temp client, show the result."""
        from utils import premium_session
        gen = PREMIUM_GEN.get(user_id)
        if not gen or not gen.get("client"):
            await message.reply_text("⚠️ Session generation expired. Start again from the 👑 Premium menu.", reply_markup=back_markup)
            return
        tmp_client = gen["client"]
        try:
            session_string = await premium_session.export_session(tmp_client)
        except Exception as e:
            await _premium_gen_cleanup(user_id)
            await message.reply_text(
                f"❌ Could not export the session: `{e}`.\n\n"
                "Tap **🔑 Generate Session** in the 👑 Premium menu to retry.",
                reply_markup=back_markup
            )
            return
        # Disconnect the temp client BEFORE dropping the handle so nothing dangles.
        try:
            await premium_session.discard_client(tmp_client)
        except Exception:
            pass
        gen["result"] = session_string
        gen["client"] = None
        # Keep the result around for the Save/Discard callbacks, then auto-expire.
        gen["expires_at"] = _time_monotonic() + 5 * 60
        USER_STATES.pop(user_id, None)
        await message.reply_text(
            "🔑 **Session string generated!**\n\n"
            "Copy it and save it somewhere safe, or tap **💾 Save to .env** to persist it "
            "for the bot.\n\n"
            f"```\n{session_string}\n```",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💾 Save to .env", callback_data="admin_premium_gen_save")],
                [InlineKeyboardButton("❌ Discard", callback_data="admin_premium_gen_abort")],
                [InlineKeyboardButton("◀️ Back to Premium Menu", callback_data="admin_premium_menu")],
            ])
        )
        await log_event(f"👑 **Premium Session:** New session string generated by creator (`{user_id}`).")

    def _time_monotonic():
        import time as _t
        return _t.monotonic()

    def _has_real_cookie_line(content: str) -> bool:
        """True if *content* contains at least one valid Netscape cookie line.

        A real cookie line has 7 tab-separated fields (domain, flag, path,
        secure, expiration, name, value). This rejects header-only, empty,
        or Telegram-truncated jars so we never persist a broken file.
        """
        for raw in content.splitlines():
            line = raw.rstrip("\n").rstrip("\r")
            if not line or line.startswith("#"):
                continue
            if len(line.split("\t")) >= 7:
                return True
        return False

    def _write_cookie_jar(cookie_key: str, file_path: str, content: str) -> None:
        """Validate and atomically write a cookie jar, keeping primary jars read-only.

        Defense in depth so the live jar can never be corrupted:
          * reject header-only / truncated / malformed jars up front;
          * back up the existing jar to <file>.autobak before touching it;
          * write to a temp file, fsync, then os.replace (atomic) so a crash
            mid-write cannot leave a truncated jar behind;
          * re-lock primary jars (YouTube/Instagram/TikTok/X) to 0o444 — yt-dlp
            never writes these paths directly; rotation write-back happens only
            through cookie_manager's atomic merge, which re-applies the lock;
          * purge stale yt-dlp snapshots so the next download uses the fresh jar;
          * record the upload in cookies/meta.json so the freshness watchdog
            treats the jar as warm from now on.
        """
        from utils.downloader import _purge_cookie_snapshots
        from utils import cookie_manager

        normalized = content
        if not normalized.strip().startswith("# Netscape"):
            normalized = f"# Netscape HTTP Cookie File\n{content}"

        if not _has_real_cookie_line(normalized):
            raise ValueError(
                "no valid Netscape cookie lines found — the file looks empty, "
                "truncated, or is not a real cookie jar"
            )

        # Cheap insurance: snapshot the current jar before overwriting it.
        if os.path.exists(file_path):
            try:
                shutil.copy(file_path, f"{file_path}.autobak")
            except Exception:
                pass

        # os.replace is a directory-level rename, so it succeeds even when the
        # existing file is 0o444 (read-only). The old inode is unlinked and a
        # fresh one takes its place; we then re-lock that fresh inode.
        is_primary = file_path in (config.YT_COOKIES, config.IG_COOKIES,
                                   config.TT_COOKIES, config.X_COOKIES)
        tmp_path = f"{file_path}.tmp.{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(normalized)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, file_path)
        if is_primary:
            os.chmod(file_path, 0o444)
        _purge_cookie_snapshots(file_path)
        cookie_manager.touch_cookie_uploaded(file_path)

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
            await purge_active_prompt(user_id, client)
            # Clean up any in-flight premium session generation so the temp
            # login client is never left dangling.
            gen = PREMIUM_GEN.pop(user_id, None)
            if gen and gen.get("client"):
                from utils.premium_session import discard_client
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
                await _handle_premium_gen_input(client, message, user_id, state, input_text, prompt_id)
            except Exception as e:
                logger.exception(f"[AdminGen] Error in {state}: {e}")
                await _premium_gen_cleanup(user_id)
                await message.reply_text(
                    "❌ Something went wrong in the session generation flow. The temp "
                    "login client was cleaned up — please start again from the 👑 Premium menu.",
                    reply_markup=back_markup
                )
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

    async def _admin_callback_dispatch(client: Client, callback_query: CallbackQuery):
        data = callback_query.data
        user_id = callback_query.from_user.id

        if user_id != config.SYSTEM_CREATOR_ID:
            await callback_query.answer("Access Denied.", show_alert=True)
            return

        if data == "admin_close":
            USER_STATES.pop(user_id, None)
            await purge_active_prompt(user_id, client)
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
            await _premium_gen_cleanup(user_id)

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
                await _premium_gen_cleanup(user_id)

            if config.PREMIUM_STRING_SESSION:
                status_note = "🟢 Premium userbot session is configured — 4 GB uploads are available to whitelisted users."
            else:
                status_note = "⚪ No `PREMIUM_STRING_SESSION` set — 4 GB uploads are DISABLED. Tap **🔑 Generate Session** to create one right here."

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
            await _premium_gen_cleanup(user_id)
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
            await _premium_gen_cleanup(user_id)
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
            await _finish_premium_gen(client, callback_query.message, user_id)
            await callback_query.answer()
            return

        elif data == "admin_premium_gen_clean":
            # Menu button: sweep any stale generation, then re-render the menu.
            # No edit-text of different content, so repeated presses are safe.
            await _premium_gen_cleanup(user_id)
            db = load_database()
            premium_users = db["premium_users"]
            premium_lines = "\n".join([f"• `{uid}`" for uid in premium_users]) if premium_users else "No Premium-enabled users yet."
            if config.PREMIUM_STRING_SESSION:
                status_note = "🟢 Premium userbot session is configured — 4 GB uploads are available to whitelisted users."
            else:
                status_note = "⚪ No `PREMIUM_STRING_SESSION` set — 4 GB uploads are DISABLED. Tap **🔑 Generate Session** to create one right here."
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
            from main import schedule_self_restart
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
            from main import schedule_self_restart
            schedule_self_restart(delay=3.0)

        # =========================================================================
        # Cookies Sub-Menus Configuration
        # =========================================================================
        elif data == "admin_cookies_menu":
            USER_STATES.pop(user_id, None)
            ACTIVE_PROMPTS.pop(user_id, None)
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
                await _test_cookie_jar(client, user_id, cookie_key, file_path)

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
            await _render_pot_menu(callback_query)

        elif data.startswith("admin_pot_action:"):
            await _handle_pot_action(client, callback_query, data.split(":")[1])

        elif data == "admin_direct_menu":
            await _render_direct_menu(callback_query)

        elif data == "admin_direct_toggle_ig":
            from dotenv import set_key
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
                from main import schedule_self_restart
                schedule_self_restart(delay=3.0)
            else:
                await callback_query.message.edit_text(
                    "✅ Instagram direct-forward **disabled**. "
                    "The worker will stop on next restart.",
                    reply_markup=get_direct_menu_keyboard())
                await log_event("📨 **Admin Action:** IG direct-forward disabled.")
                await callback_query.answer("IG relay disabled", show_alert=True)

        elif data == "admin_direct_pair_ig":
            from modules import direct_forward
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
            from modules import direct_forward
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
            from dotenv import set_key
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
                from main import schedule_self_restart
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

    # ------------------------------------------------------------------
    # Direct-forward menu helper (closure over log_event)
    # ------------------------------------------------------------------
    async def _render_direct_menu(callback_query: CallbackQuery):
        from modules import direct_forward
        state = direct_forward._load_state()
        ig_enabled = "🟢" if config.IG_DIRECT_ENABLED else "⚪"
        x_enabled = "🟢" if config.X_DIRECT_ENABLED else "⚪"
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

        try:
            await callback_query.message.edit_text(
                "📨 **Direct-Forward (DM relay)**\n\n"
                "The bot relays media you DM to its own Instagram account, or "
                "send to your OWN X self-DM (Message Yourself).\n\n"
                f"• Relay chat: {chat_set}\n"
                f"• Poll interval: {config.DIRECT_FORWARD_POLL_SECONDS}s\n\n"
                f"**Instagram**\n"
                f"• {ig_enabled} Status: **{direct_forward.pairing_status('ig', state)}**\n\n"
                f"**X / Twitter**\n"
                f"• {x_enabled} Status: **{'enabled' if config.X_DIRECT_ENABLED else 'disabled'}**\n"
                f"• Cookies: {x_cookie_status}\n"
                f"• Method: self-DM — send tweet links/photos/videos to your own X "
                f"self-DM (Message Yourself).\n\n"
                "Tap **🧪 Test X Cookies** to validate the jar. "
                "Use **🟢 Enable X** / **🔴 Disable X** to toggle the relay.\n"
                "Instagram: tap **🔗 Pair Instagram**, then send "
                "the code to the bot account via Instagram DM.",
                reply_markup=get_direct_menu_keyboard()
            )
        except Exception:
            pass
        await callback_query.answer()

    # ------------------------------------------------------------------
    # PO Token helpers (closures over app/log_event)
    # ------------------------------------------------------------------
    async def _render_pot_menu(callback_query: CallbackQuery):
        import utils.shared as shared
        manager = getattr(shared, "pot_manager_instance", None)
        running = manager.is_running() if manager else False
        available = getattr(shared, "POT_AVAILABLE", False)
        enabled = shared.is_pot_enabled()
        try:
            await callback_query.message.edit_text(
                "🔐 **PO Token Provider**\n\n"
                "YouTube downloads require this provider (cookies + PO token, no fallback). "
                "It runs on the Deno runtime and starts automatically with the bot.\n\n"
                f"• Provider running: **{'YES ✅' if running else 'NO ❌'}**\n"
                f"• Provider available: **{'YES ✅' if available else 'NO ❌'}**\n"
                f"• PO token enabled: **{'YES ✅' if enabled else 'NO ❌'}**\n"
                f"• Endpoint: `127.0.0.1:{config.YTDLP_POT_PORT}`\n\n"
                "• **Test Stack** — live extraction with the full stack.\n"
                "• **Run Diagnosis** — compare no-auth / cookies-only / full-stack.\n"
                "• **Start/Stop Provider** — launch or shut down the Deno server.",
                reply_markup=get_pot_menu_keyboard()
            )
        except Exception:
            pass
        await callback_query.answer()

    async def _handle_pot_action(client: Client, callback_query: CallbackQuery, action: str):
        import utils.shared as shared

        if action == "start":
            await callback_query.answer("Starting provider...")
            manager = getattr(shared, "pot_manager_instance", None)
            if manager and manager.is_running():
                await callback_query.message.edit_text(
                    "🚀 Provider is already running.",
                    reply_markup=get_pot_menu_keyboard()
                )
                return
            try:
                from utils.pot_provider import PotProviderManager
                manager = manager or PotProviderManager()
                await manager.start()
                shared.pot_manager_instance = manager
                shared.POT_AVAILABLE = True
                text = (
                    "🚀 **PO Token Provider Started**\n\n"
                    f"Listening on `127.0.0.1:{config.YTDLP_POT_PORT}`.\n"
                    "Downloads will now use cookies + PO token."
                )
                await log_event("🔐 **Admin Action:** PO Token provider started from admin console.")
            except Exception as e:
                shared.POT_AVAILABLE = False
                text = (
                    f"❌ **Failed to start provider:**\n`{e}`\n\n"
                    "Make sure Deno ≥ 2.0 is installed and the provider is set up:\n"
                    "`./install.sh`\n"
                    "(installs Deno, clones bgutil-provider, builds the native canvas FFI)"
                )
            try:
                await callback_query.message.edit_text(text=text, reply_markup=get_pot_menu_keyboard())
            except Exception:
                pass
            return

        if action == "stop":
            await callback_query.answer("Stopping provider...")
            manager = getattr(shared, "pot_manager_instance", None)
            if manager:
                await manager.stop()
            shared.POT_AVAILABLE = False
            try:
                await callback_query.message.edit_text(
                    "🛑 **PO Token Provider Stopped.**\n"
                    "YouTube downloads will FAIL while it is stopped (no fallback). "
                    "Tap **Start Provider** to resume.",
                    reply_markup=get_pot_menu_keyboard()
                )
            except Exception:
                pass
            await log_event("🔐 **Admin Action:** PO Token provider stopped from admin console.")
            return

        if action == "diagnose":
            await callback_query.answer("Running diagnosis...")
            try:
                await callback_query.message.edit_text(
                    "🔍 **Running YouTube access diagnosis...**\nThis may take up to 30 seconds.",
                    reply_markup=get_pot_menu_keyboard()
                )
            except Exception:
                pass
            from utils.downloader import diagnose_youtube_access
            loop = asyncio.get_event_loop()
            try:
                report = await loop.run_in_executor(None, diagnose_youtube_access)
                text = (
                    "🔍 **YouTube Access Diagnosis**\n\n"
                    f"• No auth: `{report['no_auth_count']}` real formats\n"
                    f"• Cookies only: `{report['cookies_count']}` real formats\n"
                    f"• Cookies + PO token + mweb: `{report['full_count']}` real formats\n\n"
                    f"**Recommendation:** {report['recommendation']}"
                )
            except Exception as e:
                text = f"❌ **Diagnosis failed:**\n`{e}`"
            try:
                await callback_query.message.edit_text(text=text, reply_markup=get_pot_menu_keyboard())
            except Exception:
                pass
            return

        if action == "test":
            await callback_query.answer("Testing full stack...")
            try:
                await callback_query.message.edit_text(
                    "🧪 **Testing cookies + PO-token stack...**",
                    reply_markup=get_pot_menu_keyboard()
                )
            except Exception:
                pass
            await _test_cookie_jar(client, callback_query.from_user.id, "ytcookies", COOKIE_MAP["ytcookies"], force_pot=True)
            return

    # ------------------------------------------------------------------
    # Live cookie-jar test (yt-dlp extraction against a public video)
    # ------------------------------------------------------------------
    def _run_cookie_test_sync(cookie_key: str, file_path: str, force_pot: bool) -> dict:
        """Synchronous yt-dlp probe. Run inside an executor so it never blocks the loop."""
        from utils.downloader import get_cookies_for_url, _apply_pot_options
        from utils import cookie_manager
        import utils.shared as shared

        test_url = "https://www.youtube.com/watch?v=jSi2LDkyKmI"
        cookie_snapshot = get_cookies_for_url(test_url)
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "format": "all",
            "cookiefile": cookie_snapshot,
            "proxy": getattr(config, "YTDLP_PROXY", None),
        }
        user_agent = getattr(config, "YTDLP_USER_AGENT", "")
        if user_agent:
            ydl_opts["user_agent"] = user_agent

        original_pot = shared.is_pot_enabled()
        if force_pot:
            shared.set_pot_enabled(True)
        try:
            ydl_opts = _apply_pot_options(ydl_opts, test_url)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(test_url, download=False)
        except Exception as exc:
            cookie_manager.commit(cookie_snapshot, success=False, error_text=str(exc))
            return {"ok": False, "error": str(exc)}
        finally:
            shared.set_pot_enabled(original_pot)

        cookie_manager.commit(cookie_snapshot, success=bool(info))

        formats = info.get("formats", []) if info else []
        real_formats = [
            f for f in formats
            if f.get("format_note") != "storyboard" and f.get("ext") != "mhtml"
        ]
        samples = []
        seen = set()
        for f in real_formats:
            note = f.get("format_note") or "?"
            ext = f.get("ext") or "?"
            key = (note, ext)
            if key not in seen:
                seen.add(key)
                samples.append(f"• `{note}` ({ext})")
            if len(samples) >= 6:
                break
        return {
            "ok": True,
            "real_count": len(real_formats),
            "samples": samples,
            "storyboard_only": len(real_formats) == 0,
        }

    async def _test_cookie_jar(client: Client, user_id: int, cookie_key: str, file_path: str, force_pot: bool = False):
        """Run a lightweight yt-dlp extraction on a known public video and report format availability."""
        if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
            await client.send_message(
                chat_id=user_id,
                text=f"⚠️ `{cookie_key}.txt` is empty or missing. Nothing to test.",
                reply_markup=back_markup
            )
            return

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _run_cookie_test_sync, cookie_key, file_path, force_pot)

        if not result.get("ok"):
            await client.send_message(
                chat_id=user_id,
                text=(
                    f"❌ **Cookie Test Failed for `{cookie_key}.txt`**\n\n"
                    "yt-dlp could not extract anything using this jar.\n"
                    f"Error: `{result.get('error')}`\n\n"
                    "Please upload a fresh cookie jar from a browser where YouTube plays normally."
                ),
                reply_markup=back_markup
            )
            return

        if result.get("real_count", 0) > 0:
            summary = "\n".join(result["samples"])
            pot_label = " (with PO token)" if force_pot else ""
            await client.send_message(
                chat_id=user_id,
                text=(
                    f"✅ **Cookie Test Passed{pot_label} for `{cookie_key}.txt`**\n\n"
                    f"YouTube returned {result['real_count']} downloadable formats.\n"
                    f"Sample formats:\n{summary}\n\n"
                    "The jar is working — try your link again."
                ),
                reply_markup=back_markup
            )
            await log_event(f"🧪 **Admin Action:** Cookie jar `{cookie_key}.txt` passed live test ({result['real_count']} formats).")
        else:
            await client.send_message(
                chat_id=user_id,
                text=(
                    f"⚠️ **Cookie Test Warning for `{cookie_key}.txt`**\n\n"
                    "YouTube accepted the cookies, but only returned storyboard/preview formats.\n\n"
                    "This means the jar is **bot-flagged, expired, or from an account that cannot watch videos**.\n"
                    "Please upload a fresh `ytcookies.txt` from a browser where you can actually play YouTube videos."
                ),
                reply_markup=back_markup
            )
            await log_event(f"⚠️ **Admin Action:** Cookie jar `{cookie_key}.txt` failed live test (storyboard-only).")

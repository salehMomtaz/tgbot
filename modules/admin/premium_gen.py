"""
In-chat Premium session-string generation flow.

Mirrors the original modules/admin.py premium generation functions exactly.
"""

import asyncio
import logging
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from .state import PREMIUM_GEN, USER_STATES, ACTIVE_PROMPTS, _PREMIUM_GEN_TTL
from .keyboards import _gen_abort_markup, _gen_dial_pad_markup

logger = logging.getLogger(__name__)


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


async def _premium_gen_cleanup(user_id: int, app):
    """Disconnect the temp login client and clear all gen flow state."""
    from utils.premium_session import discard_client
    gen = PREMIUM_GEN.pop(user_id, None)
    if gen and gen.get("client"):
        await discard_client(gen["client"])
    USER_STATES.pop(user_id, None)
    await _purge_active_prompt(user_id, app)


async def discard_client_quiet(tmp_client):
    try:
        await tmp_client.disconnect()
    except Exception:
        pass


async def _purge_active_prompt(user_id: int, client):
    """Helper to safely delete any active ForceReply prompt bubble from the chat stream."""
    prompt_id = ACTIVE_PROMPTS.pop(user_id, None)
    if prompt_id:
        try:
            await client.delete_messages(chat_id=user_id, message_ids=prompt_id)
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


async def _handle_premium_gen_input(client, message, user_id: int, state: str, text: str, prompt_id, app, back_markup):
    """Process one text step of the in-chat premium session generation."""
    from utils import premium_session
    import time as _time

    # If a temp login is mid-flight it carries an expiry; a stale flow that
    # somehow survived (no callback, no /start) is auto-aborted here.
    gen = PREMIUM_GEN.get(user_id)
    if gen and gen.get("expires_at", 0) < _time.monotonic():
        await _premium_gen_cleanup(user_id, app)
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
            await _premium_gen_cleanup(user_id, app)
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
        await _finish_premium_gen(client, message, user_id, app, back_markup)
        return


async def _finish_premium_gen(client, message, user_id: int, app, back_markup):
    """Export the session string, clean up the temp client, show the result."""
    from utils import premium_session
    import time as _time
    gen = PREMIUM_GEN.get(user_id)
    if not gen or not gen.get("client"):
        await message.reply_text("⚠️ Session generation expired. Start again from the 👑 Premium menu.", reply_markup=back_markup)
        return
    tmp_client = gen["client"]
    try:
        session_string = await premium_session.export_session(tmp_client)
    except Exception as e:
        await _premium_gen_cleanup(user_id, app)
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
    gen["expires_at"] = _time.monotonic() + 5 * 60
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
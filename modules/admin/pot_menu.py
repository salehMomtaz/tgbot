"""
PO Token Provider menu and actions for the admin console.

Mirrors the original modules/admin.py PO token functions exactly.
"""

import asyncio
import logging
import config
from pyrogram.types import CallbackQuery
from utils.pot_provider import PotProviderManager
import utils.shared as shared

logger = logging.getLogger(__name__)


def _pot_running() -> bool:
    """True if the PO-token provider manager exists and reports healthy."""
    try:
        manager = getattr(shared, "pot_manager_instance", None)
        return bool(manager and manager.is_running())
    except Exception:
        return False


async def _render_pot_menu(callback_query: CallbackQuery):
    from .keyboards import get_pot_menu_keyboard
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


async def _handle_pot_action(client, callback_query: CallbackQuery, action: str):
    from .keyboards import get_pot_menu_keyboard
    from main import log_event

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
        from .cookie_test import _test_cookie_jar
        from .cookies import COOKIE_MAP
        await _test_cookie_jar(client, callback_query.from_user.id, "ytcookies", COOKIE_MAP["ytcookies"], force_pot=True)
        return
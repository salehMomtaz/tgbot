# modules/bale/admin.py — LIMITED Bale admin console (no secrets)
# Government-owned messenger => no cookies / POT / direct-forward / premium / subscriptions.
# Only: users, blacklist, doc mode, size limits, abort, close.
import logging
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from aiogram import Router
import config
from utils.gate import load_database, add_user, remove_user, unblacklist_user, toggle_document_mode, is_document_mode
import utils.shared as shared

router = Router()  # not used directly; handlers registered via dispatcher in bale_runner
logger = logging.getLogger(__name__)

back_markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Back to Console", callback_data="bale_admin_main")]])

def get_bale_console_keyboard(user_id: int) -> InlineKeyboardMarkup:
    doc = "✅" if is_document_mode(user_id) else "❌"
    limit = f"{getattr(config,'BALE_SPLIT_TARGET_MB',19)}/{getattr(config,'BALE_HARD_LIMIT_MB',20)}MB"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 List Users", callback_data="bale_admin_list"), InlineKeyboardButton(text="➕ Add User", callback_data="bale_admin_add")],
        [InlineKeyboardButton(text="➖ Remove User", callback_data="bale_admin_remove"), InlineKeyboardButton(text="🚫 Blacklist", callback_data="bale_admin_blacklist")],
        [InlineKeyboardButton(text=f"📄 Doc Mode: {doc}", callback_data="bale_admin_toggle_doc")],
        [InlineKeyboardButton(text=f"⚙️ Size Limits: {limit}", callback_data="bale_admin_setlimit")],
        [InlineKeyboardButton(text="💥 Abort Transfer", callback_data="bale_admin_abort")],
        [InlineKeyboardButton(text="❌ Close", callback_data="bale_admin_close")],
    ])

# State store mirroring Telegram's but namespaced for Bale (per integration doc)
BALE_USER_STATES = {}
BALE_ACTIVE_PROMPTS = {}

async def purge_prompt(user_id: int, bot):
    pid = BALE_ACTIVE_PROMPTS.pop(user_id, None)
    if pid:
        try:
            await bot.delete_message(chat_id=user_id, message_id=pid)
        except:
            pass

def _is_bale_admin(uid: int) -> bool:
    # Bale creator is separate from Telegram creator
    return uid == getattr(config, "BALE_SYSTEM_CREATOR_ID", 0) and uid != 0

# Handlers will be registered in bale_runner via dispatcher

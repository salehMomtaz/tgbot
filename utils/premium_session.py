# utils/premium_session.py
"""In-chat generation of a Premium string session (pyrogram).

Telegram Bots are hard-capped at 2 GB per upload (tdlib/telegram-bot-api#583);
the only way to reach 4 GB is a Premium *user* account over MTProto using a
``PREMIUM_STRING_SESSION``. This module performs the interactive login steps
(phone -> code -> optional 2FA password) that used to be done by a standalone
terminal script, so the admin can do it entirely from the Admin Console.

Login flow (all on a temporary in-memory client):
    client = Client(":memory:", api_id=config.API_ID, api_hash=config.API_HASH)
    await client.connect()
    sent = await client.send_code(phone)            -> phone_code_hash
    await client.sign_in(phone, sent.phone_code_hash, code)
        (raises SessionPasswordNeeded if 2FA is enabled)
    await client.check_password(password)           -> only when 2FA
    session = await client.export_session_string()
    await client.disconnect()

Only the exported string is persisted (wrapped/updates ``PREMIUM_STRING_SESSION``
in .env). The temp client never writes a session file (":memory:").
"""
import logging

import config

logger = logging.getLogger(__name__)


async def create_login_client() -> "object":
    """Return a fresh in-memory pyrogram Client bound to config API creds."""
    from pyrogram import Client
    return Client(":memory:", api_id=config.API_ID, api_hash=config.API_HASH)


async def request_code(client) -> tuple:
    """Connect the temp client. Returns the client itself (caller stores it)."""
    await client.connect()
    return client


async def send_login_code(client, phone: str) -> str:
    """Request a login code for *phone*. Returns the phone_code_hash."""
    sent = await client.send_code(phone)
    return sent.phone_code_hash


async def verify_code(client, phone: str, phone_code_hash: str, code: str):
    """Try to sign in with the SMS/app code.

    Returns "ok" on success, "2fa" when a 2FA password is required next.
    Raises pyrogram's PhoneCodeInvalid/PhoneCodeExpired on a bad code.
    """
    from pyrogram.errors import SessionPasswordNeeded
    try:
        await client.sign_in(phone, phone_code_hash, code)
        return "ok"
    except SessionPasswordNeeded:
        return "2fa"


async def verify_password(client, password: str) -> None:
    """Complete login with the 2FA password. Raises on wrong password."""
    await client.check_password(password)


async def export_session(client) -> str:
    """Export the authorized session string."""
    return await client.export_session_string()


async def discard_client(client) -> None:
    """Disconnect the temp client (idempotent, never raises)."""
    try:
        await client.disconnect()
    except Exception:
        pass


def save_session_string(session_string: str) -> None:
    """Persist the new session into .env and refresh config in memory.

    Uses python-dotenv's set_key so the value is written dotenv-style
    (quoted when needed) — exactly what run.sh's parser and config.py expect.
    """
    from dotenv import set_key
    set_key(".env", "PREMIUM_STRING_SESSION", session_string)
    config.PREMIUM_STRING_SESSION = session_string
    logger.info("[PremiumSession] New PREMIUM_STRING_SESSION persisted to .env.")

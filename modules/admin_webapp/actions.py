"""Admin WebApp — server-side action implementations (transport-free core).

Every function here returns a dict ``{"ok": bool, "message": str, ...}`` and
is called by the FastAPI endpoints in :mod:`modules.admin_webapp.api`. This
module deliberately does NOT depend on pyrogram's ``Client`` for most actions —
it reuses the exact same storage/utility code the in-chat admin console uses
(``utils.gate``, ``utils.subscription.store``, ``modules.admin.cookies``, …) so
the WebApp and the inline-keyboard console can never drift apart.

A few actions need the running pyrogram ``Client`` (resolving a channel
@username → id, notifying a granted user). They read it from
``modules.stream_handler.tg_client`` (set at startup); when it is unavailable
they degrade gracefully (username-only channel entry, skip notification).
"""
from __future__ import annotations

import asyncio
import os
import shutil
import time as _time
import logging

import config
from dotenv import set_key

from utils.gate import (
    load_database,
    add_user,
    remove_user,
    unblacklist_user,
    is_authorized,
    is_blacklisted,
    is_premium_user,
    add_premium_user,
    remove_premium_user,
    toggle_document_mode,
)
from utils.id_validator import is_valid_telegram_id
from utils.shared import queue
from main import log_event, schedule_self_restart

logger = logging.getLogger(__name__)


def _tg_client():
    """Return the running pyrogram Client or None (degraded mode)."""
    try:
        from modules.stream_handler import tg_client
        return tg_client
    except Exception:
        return None


# --------------------------------------------------------------------------
# Overview / state snapshot
# --------------------------------------------------------------------------
async def overview() -> dict:
    db = load_database()
    try:
        from utils.subscription.store import get_settings, list_subscriptions
        sub_s = get_settings()
        active_subs = sum(
            1 for v in list_subscriptions().values()
            if int(v.get("until", 0)) > int(_time.time())
        )
    except Exception:
        sub_s, active_subs = {}, 0
    return {
        "ok": True,
        "bot": {
            "creator": config.SYSTEM_CREATOR_ID,
            "premium_session": bool(config.PREMIUM_STRING_SESSION),
            "domain": getattr(config, "DOMAIN", ""),
            "log_channel": getattr(config, "LOG_CHANNEL_ID", 0),
        },
        "database": {
            "authorized_count": len(db.get("authorized", [])),
            "blacklisted_count": len(db.get("blacklisted", [])),
            "premium_count": len(db.get("premium_users", [])),
        },
        "queue": {
            "pending": len(queue._pending),
            "active": bool(queue._active),
            "max_depth": getattr(config, "MAX_QUEUE_DEPTH", 20),
        },
        "subscription": {
            "enabled": bool(sub_s.get("enabled")),
            "free_enabled": bool(sub_s.get("free_enabled")),
            "channels": sub_s.get("channels", []) or [],
            "active_subs": active_subs,
        },
        "pot": pot_state(),
        "cookies": cookie_jars_state(),
        "direct": direct_state(),
    }


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------
def list_users() -> dict:
    db = load_database()
    return {
        "ok": True,
        "authorized": list(db.get("authorized", [])),
        "blacklisted": list(db.get("blacklisted", [])),
        "premium": list(db.get("premium_users", [])),
    }


def add_user_action(uid: int) -> dict:
    if not (10000 <= uid <= 99999999999):
        return {"ok": False, "message": f"Invalid Telegram ID `{uid}` (5-11 digits)."}
    if add_user(uid):
        asyncio.create_task(log_event(f"👥 **Admin WebApp:** Whitelisted User ID `{uid}`."))
        return {"ok": True, "message": f"✅ User `{uid}` authorized."}
    return {"ok": False, "message": f"ℹ️ User `{uid}` was already authorized."}


def remove_user_action(uid: int) -> dict:
    if not is_authorized(uid):
        return {"ok": False, "message": f"❌ User ID `{uid}` is not currently authorized."}
    if remove_user(uid):
        asyncio.create_task(log_event(f"👥 **Admin WebApp:** Revoked User ID `{uid}`."))
        return {"ok": True, "message": f"✅ User `{uid}` removed."}
    return {"ok": False, "message": f"❌ Could not remove `{uid}`."}


def unban_user_action(uid: int) -> dict:
    if not is_blacklisted(uid):
        return {"ok": False, "message": f"❌ User ID `{uid}` is not in the blacklist."}
    if unblacklist_user(uid):
        asyncio.create_task(log_event(f"🔓 **Admin WebApp:** Unbanned User ID `{uid}`."))
        return {"ok": True, "message": f"✅ User `{uid}` unbanned."}
    return {"ok": False, "message": f"❌ Could not unban `{uid}`."}


def toggle_doc_action(uid: int) -> dict:
    state = toggle_document_mode(uid)
    label = "ON ✅" if state else "OFF ❌"
    asyncio.create_task(log_event(f"⚙️ **Admin WebApp:** Document Mode toggled to {label}."))
    return {"ok": True, "message": f"📄 Document Mode is now {label}.", "state": state}


# --------------------------------------------------------------------------
# Cookie jars (primary + per-site + global)
# --------------------------------------------------------------------------
def _jar_status(path: str) -> dict:
    from utils import cookie_manager
    rec = cookie_manager.get_meta_record(path)
    has_lines = os.path.exists(path) and cookie_manager.has_real_cookie_lines(path)
    size = os.path.getsize(path) if os.path.exists(path) else 0
    return {
        "exists": os.path.exists(path),
        "size": size,
        "valid": bool(has_lines),
        "last_success": rec.get("last_success"),
        "last_upload": rec.get("last_upload"),
        "last_failure": rec.get("last_failure"),
        "failure_reason": (rec.get("failure_reason") or "")[:200],
        "merge_count": rec.get("merge_count", 0),
    }


def cookie_jars_state() -> dict:
    from modules.admin.cookies import COOKIE_MAP
    return {
        "ok": True,
        "jars": {k: {"path": v, **(_jar_status(v))} for k, v in COOKIE_MAP.items()},
    }


def per_site_jars_state() -> dict:
    ytdlp_dir = getattr(config, "YTDLP_COOKIES_DIR", "cookies/ytdlp")
    jars = {}
    if os.path.isdir(ytdlp_dir):
        for name in sorted(os.listdir(ytdlp_dir)):
            if name.endswith(".txt"):
                site = name[:-4]
                jars[site] = _jar_status(os.path.join(ytdlp_dir, name))
    return {"ok": True, "dir": ytdlp_dir, "jars": jars}


def cookie_download(key: str) -> tuple[bytes | None, str]:
    """Return (content_bytes_or_None, filename)."""
    from modules.admin.cookies import COOKIE_MAP
    path = COOKIE_MAP.get(key)
    if not path or not os.path.exists(path):
        return None, ""
    with open(path, "rb") as f:
        return f.read(), f"{key}.txt"


def cookie_replace(key: str, content: str) -> dict:
    from modules.admin.cookies import COOKIE_MAP, _write_cookie_jar
    path = COOKIE_MAP.get(key)
    if not path:
        return {"ok": False, "message": "❌ Invalid cookie profile."}
    try:
        _write_cookie_jar(key, path, content)
        asyncio.create_task(log_event(f"🍪 **Admin WebApp:** Cookie profile `{key}.txt` replaced."))
        return {"ok": True, "message": f"✅ `{key}.txt` replaced successfully."}
    except Exception as e:
        return {"ok": False, "message": f"❌ Failed to write cookie file: {e}"}


def cookie_backup(key: str) -> dict:
    from modules.admin.cookies import COOKIE_MAP
    path = COOKIE_MAP.get(key)
    if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
        return {"ok": False, "message": f"⚠️ `{key}.txt` is empty or missing. Nothing to back up."}
    from utils.downloader.cookies import _purge_cookie_snapshots
    backup_path = getattr(config, "YT_COOKIES_BACKUP", "ytcookies.backup")
    try:
        if os.path.exists(backup_path):
            os.chmod(backup_path, 0o644)
        shutil.copy(path, backup_path)
        os.chmod(backup_path, 0o444)
        _purge_cookie_snapshots(path)
        asyncio.create_task(log_event(f"🍪 **Admin WebApp:** `{key}.txt` saved as protected backup."))
        return {"ok": True, "message": f"✅ Saved `{key}.txt` as protected backup (read-only)."}
    except Exception as e:
        return {"ok": False, "message": f"❌ Failed to save backup: {e}"}


def cookie_restore(key: str) -> dict:
    from modules.admin.cookies import COOKIE_MAP
    path = COOKIE_MAP.get(key)
    if not path:
        return {"ok": False, "message": "❌ Invalid cookie profile."}
    from utils.downloader.cookies import _purge_cookie_snapshots
    backup_path = getattr(config, "YT_COOKIES_BACKUP", "ytcookies.backup")
    if not os.path.exists(backup_path) or os.path.getsize(backup_path) == 0:
        return {"ok": False, "message": f"⚠️ No backup found for `{key}.txt`."}
    try:
        if os.path.exists(path):
            os.chmod(path, 0o644)
        shutil.copy(backup_path, path)
        os.chmod(path, 0o444)
        _purge_cookie_snapshots(path)
        asyncio.create_task(log_event(f"🍪 **Admin WebApp:** `{key}.txt` restored from backup."))
        return {"ok": True, "message": f"✅ Restored `{key}.txt` from backup."}
    except Exception as e:
        return {"ok": False, "message": f"❌ Failed to restore backup: {e}"}


async def cookie_test(key: str, force_pot: bool = False) -> dict:
    from modules.admin.cookies import COOKIE_MAP
    from modules.admin.cookie_test import _run_cookie_test_sync
    path = COOKIE_MAP.get(key)
    if not path:
        return {"ok": False, "message": "❌ Invalid cookie profile."}
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return {"ok": False, "message": f"⚠️ `{key}.txt` is empty or missing. Nothing to test."}
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _run_cookie_test_sync, key, path, force_pot)
    if not result.get("ok"):
        return {"ok": False, "message": f"❌ yt-dlp failed:\n{result.get('error')}"}
    if result.get("real_count", 0) > 0:
        samples = "\n".join(result.get("samples", []))
        label = " (with PO token)" if force_pot else ""
        return {
            "ok": True,
            "message": f"✅ Test passed{label}: {result['real_count']} formats.\n{samples}",
            "real_count": result["real_count"],
        }
    return {
        "ok": False,
        "message": "⚠️ YouTube accepted the cookies but returned only storyboard/preview formats — the jar is bot-flagged/expired.",
    }


def per_site_jar_replace(site: str, content: str) -> dict:
    import re
    if not re.fullmatch(r"[a-z0-9][a-z0-9.\-]*", site):
        return {"ok": False, "message": "❌ Invalid site name. Use letters, numbers, dashes and dots."}
    from modules.admin.cookies import _write_cookie_jar
    ytdlp_dir = getattr(config, "YTDLP_COOKIES_DIR", "cookies/ytdlp")
    os.makedirs(ytdlp_dir, exist_ok=True)
    file_path = os.path.join(ytdlp_dir, f"{site}.txt")
    try:
        _write_cookie_jar(f"ytdlp_{site}", file_path, content)
        asyncio.create_task(log_event(f"🍪 **Admin WebApp:** Per-site cookie jar `cookies/ytdlp/{site}.txt` uploaded."))
        return {"ok": True, "message": f"✅ Per-site jar saved to `cookies/ytdlp/{site}.txt`."}
    except Exception as e:
        return {"ok": False, "message": f"❌ Failed to write cookie file: {e}"}


def per_site_jar_delete(site: str) -> dict:
    ytdlp_dir = getattr(config, "YTDLP_COOKIES_DIR", "cookies/ytdlp")
    path = os.path.join(ytdlp_dir, f"{site}.txt")
    if not os.path.exists(path):
        return {"ok": False, "message": f"❌ No jar at `cookies/ytdlp/{site}.txt`."}
    try:
        os.remove(path)
        asyncio.create_task(log_event(f"🍪 **Admin WebApp:** Per-site cookie jar `{site}.txt` deleted."))
        return {"ok": True, "message": f"✅ Deleted `cookies/ytdlp/{site}.txt`."}
    except Exception as e:
        return {"ok": False, "message": f"❌ Failed to delete: {e}"}


# --------------------------------------------------------------------------
# Premium uploads (4 GB)
# --------------------------------------------------------------------------
def premium_state() -> dict:
    db = load_database()
    return {
        "ok": True,
        "session_set": bool(config.PREMIUM_STRING_SESSION),
        "users": list(db.get("premium_users", [])),
    }


def premium_add(uid: int) -> dict:
    if not (10000 <= uid <= 99999999999):
        return {"ok": False, "message": f"Invalid Telegram ID `{uid}` (5-11 digits)."}
    if add_premium_user(uid):
        asyncio.create_task(log_event(f"👑 **Admin WebApp:** Enabled 4 GB uploads for User ID `{uid}`."))
        return {"ok": True, "message": f"✅ User `{uid}` is now enabled for 4 GB Premium uploads."}
    return {"ok": False, "message": f"ℹ️ User `{uid}` is already Premium-enabled."}


def premium_remove(uid: int) -> dict:
    if not is_premium_user(uid):
        return {"ok": False, "message": f"❌ User ID `{uid}` is not in the Premium whitelist."}
    if remove_premium_user(uid):
        asyncio.create_task(log_event(f"👑 **Admin WebApp:** Revoked 4 GB uploads for User ID `{uid}`."))
        return {"ok": True, "message": f"✅ User `{uid}` Premium access revoked."}
    return {"ok": False, "message": f"❌ Could not remove `{uid}`."}


# --- WebApp session-string generation (own state, never mixes with the
# --- in-chat flow's PREMIUM_GEN so the two can't clobber each other).
WEB_PREMIUM_GEN: dict[str, dict] = {}
_WEB_GEN_TTL = 15 * 60


def _web_gen_sweep(uid: int):
    now = _time.monotonic()
    gen = WEB_PREMIUM_GEN.get(str(uid))
    if gen and gen.get("expires_at", 0) < now:
        asyncio.create_task(_web_gen_discard(gen))
        WEB_PREMIUM_GEN.pop(str(uid), None)
        return None
    return gen


async def _web_gen_discard(gen: dict):
    client = gen.get("client")
    if client:
        try:
            from utils.premium_session import discard_client
            await discard_client(client)
        except Exception:
            pass
    gen["client"] = None


async def premium_gen_start(uid: int, phone: str) -> dict:
    _web_gen_sweep(uid)
    phone = (phone or "").strip().replace(" ", "")
    if not (phone.startswith("+") and phone[1:].isdigit() and 8 <= len(phone[1:]) <= 15):
        return {"ok": False, "message": "❌ Use international format, e.g. `+15551234567`."}
    from utils import premium_session
    tmp = None
    try:
        tmp = await premium_session.create_login_client()
        await premium_session.request_code(tmp)
        phone_code_hash = await premium_session.send_login_code(tmp, phone)
    except Exception as e:
        if tmp is not None:
            try:
                await premium_session.discard_client(tmp)
            except Exception:
                pass
        return {"ok": False, "message": f"❌ Could not request a login code:\n{e}"}
    WEB_PREMIUM_GEN[str(uid)] = {
        "client": tmp,
        "phone": phone,
        "phone_code_hash": phone_code_hash,
        "result": None,
        "expires_at": _time.monotonic() + _WEB_GEN_TTL,
    }
    return {"ok": True, "message": f"✅ Code sent to `{phone}`. Enter the login code."}


async def premium_gen_verify(uid: int, code: str) -> dict:
    gen = _web_gen_sweep(uid)
    if not gen or not gen.get("client"):
        return {"ok": False, "message": "⚠️ Session generation expired. Start again."}
    from utils import premium_session
    code = (code or "").strip()
    try:
        outcome = await premium_session.verify_code(gen["client"], gen["phone"], gen["phone_code_hash"], code)
    except Exception as e:
        return {"ok": False, "message": f"❌ Invalid code: {e}"}
    if outcome == "2fa":
        return {"ok": True, "step": "2fa", "message": "Two-step verification is enabled. Enter your 2FA password."}
    await _web_gen_finish(uid, gen)
    return {"ok": True, "step": "done", "message": "✅ Login successful — session string exported."}


async def premium_gen_password(uid: int, password: str) -> dict:
    gen = _web_gen_sweep(uid)
    if not gen or not gen.get("client"):
        return {"ok": False, "message": "⚠️ Session generation expired. Start again."}
    from utils import premium_session
    try:
        await premium_session.verify_password(gen["client"], password)
    except Exception as e:
        return {"ok": False, "message": f"❌ Wrong 2FA password: {e}"}
    await _web_gen_finish(uid, gen)
    return {"ok": True, "step": "done", "message": "✅ Login successful — session string exported."}


async def _web_gen_finish(uid: int, gen: dict):
    from utils import premium_session
    tmp_client = gen["client"]
    try:
        session_string = await premium_session.export_session(tmp_client)
    except Exception:
        session_string = None
    try:
        await premium_session.discard_client(tmp_client)
    except Exception:
        pass
    gen["client"] = None
    if session_string:
        gen["result"] = session_string
        gen["expires_at"] = _time.monotonic() + 5 * 60


def premium_gen_state(uid: int) -> dict:
    gen = _web_gen_sweep(uid)
    if not gen:
        return {"ok": True, "active": False, "result": None}
    return {"ok": True, "active": bool(gen.get("client")), "result": gen.get("result"), "phone": gen.get("phone")}


async def premium_gen_save(uid: int) -> dict:
    gen = WEB_PREMIUM_GEN.get(str(uid))
    result = (gen or {}).get("result")
    if not result:
        return {"ok": False, "message": "No pending session string to save."}
    try:
        from utils.premium_session import save_session_string
        save_session_string(result)
    except Exception as e:
        return {"ok": False, "message": f"❌ Failed to save: {e}"}
    WEB_PREMIUM_GEN.pop(str(uid), None)
    asyncio.create_task(log_event("👑 **Admin WebApp:** New PREMIUM_STRING_SESSION saved to .env by creator. Restarting automatically."))
    schedule_self_restart(delay=3.0)
    return {"ok": True, "message": "✅ Saved to .env — restarting the bot automatically."}


async def premium_gen_abort(uid: int) -> dict:
    gen = WEB_PREMIUM_GEN.pop(str(uid), None)
    if gen:
        await _web_gen_discard(gen)
    return {"ok": True, "message": "🚫 Generation aborted — temp login client disconnected."}


# --------------------------------------------------------------------------
# PO Token Provider
# --------------------------------------------------------------------------
def pot_state() -> dict:
    import utils.shared as shared
    from modules.admin.pot_menu import _pot_running as _pot_running_status
    return {
        "ok": True,
        "running": bool(_pot_running_status()),
        "available": bool(shared.POT_AVAILABLE),
        "enabled": shared.is_pot_enabled(),
        "port": getattr(config, "YTDLP_POT_PORT", 4416),
    }


async def pot_start() -> dict:
    import utils.shared as shared
    from utils.pot_provider import PotProviderManager
    manager = getattr(shared, "pot_manager_instance", None)
    if manager and manager.is_running():
        return {"ok": True, "message": "🚀 Provider is already running."}
    try:
        manager = manager or PotProviderManager()
        await manager.start()
        shared.pot_manager_instance = manager
        shared.POT_AVAILABLE = True
        asyncio.create_task(log_event("🔐 **Admin WebApp:** PO Token provider started."))
        return {"ok": True, "message": f"🚀 Provider started on `127.0.0.1:{config.YTDLP_POT_PORT}`."}
    except Exception as e:
        shared.POT_AVAILABLE = False
        return {"ok": False, "message": f"❌ Failed to start provider:\n{e}"}


async def pot_stop() -> dict:
    import utils.shared as shared
    manager = getattr(shared, "pot_manager_instance", None)
    if manager:
        await manager.stop()
    shared.POT_AVAILABLE = False
    asyncio.create_task(log_event("🔐 **Admin WebApp:** PO Token provider stopped."))
    return {"ok": True, "message": "🛑 Provider stopped. YouTube downloads will FAIL while stopped (no fallback)."}


async def pot_diagnose() -> dict:
    from utils.downloader.cookies import diagnose_youtube_access
    loop = asyncio.get_event_loop()
    try:
        report = await loop.run_in_executor(None, diagnose_youtube_access)
        return {
            "ok": True,
            "message": (
                f"• No auth: `{report['no_auth_count']}` real formats\n"
                f"• Cookies only: `{report['cookies_count']}` real formats\n"
                f"• Cookies + PO token + mweb: `{report['full_count']}` real formats\n\n"
                f"**Recommendation:** {report['recommendation']}"
            ),
        }
    except Exception as e:
        return {"ok": False, "message": f"❌ Diagnosis failed:\n{e}"}


# --------------------------------------------------------------------------
# Subscriptions
# --------------------------------------------------------------------------
def sub_state() -> dict:
    from utils.subscription.store import get_settings, get_channels, list_subscriptions
    from utils.subscription.tiers import TIERS, TIER_ORDER
    s = get_settings()
    subs = list_subscriptions()
    now = int(_time.time())
    active = {k: v for k, v in subs.items() if int(v.get("until", 0)) > now}
    return {
        "ok": True,
        "enabled": bool(s.get("enabled")),
        "free_enabled": bool(s.get("free_enabled")),
        "channels": get_channels(),
        "active_count": len(active),
        "total_count": len(subs),
        "tiers": {k: TIERS[k] for k in TIER_ORDER},
    }


def sub_toggle() -> dict:
    from utils.subscription.store import get_settings, set_settings
    s = get_settings()
    ns = set_settings(enabled=not s.get("enabled"))
    asyncio.create_task(log_event(f"💳 **Admin WebApp:** Subscription mode toggled to {ns.get('enabled')}"))
    return {"ok": True, "message": f"Subscription mode is now {'ON' if ns.get('enabled') else 'OFF'}.", "enabled": ns.get("enabled")}


def sub_toggle_free() -> dict:
    from utils.subscription.store import get_settings, set_settings
    s = get_settings()
    ns = set_settings(free_enabled=not s.get("free_enabled"))
    asyncio.create_task(log_event(f"💳 **Admin WebApp:** Free tier toggled to {ns.get('free_enabled')}"))
    return {"ok": True, "message": f"Free tier is now {'ON' if ns.get('free_enabled') else 'OFF'}.", "free_enabled": ns.get("free_enabled")}


async def _parse_channel_input(text: str) -> tuple[int, str]:
    """Resolve @username → (chat_id, @username); numeric ids pass through."""
    txt = (text or "").strip()
    cid, cuser = 0, ""
    client = _tg_client()
    if txt.startswith("@"):
        cuser = txt
        if client:
            try:
                chat = await client.get_chat(cuser)
                cid = int(getattr(chat, "id", 0) or 0)
            except Exception:
                cid = 0
    else:
        try:
            cid = int(txt)
        except Exception:
            cuser = "@" + txt.lstrip("@")
            if client:
                try:
                    chat = await client.get_chat(cuser)
                    cid = int(getattr(chat, "id", 0) or 0)
                except Exception:
                    cid = 0
    return cid, cuser


async def sub_channel_add(text: str) -> dict:
    from utils.subscription.store import add_channel
    cid, cuser = await _parse_channel_input(text)
    if not cid and not cuser:
        return {"ok": False, "message": "❌ Could not parse channel. Send @username or numeric ID."}
    chans = add_channel(channel_id=cid, channel_username=cuser)
    names = ", ".join(c.get("username") or str(c.get("id")) for c in chans) or "—"
    asyncio.create_task(log_event(f"💳 **Admin WebApp:** Force-join channel added {cuser or cid} (id {cid}). Now {len(chans)} channels."))
    return {"ok": True, "message": f"✅ Channel added. Now tracking: {names}", "channels": chans}


async def sub_channel_remove(text: str) -> dict:
    from utils.subscription.store import remove_channel
    cid, cuser = await _parse_channel_input(text)
    chans = remove_channel(channel_id=cid, channel_username=cuser)
    names = ", ".join(c.get("username") or str(c.get("id")) for c in chans) or "— (none)"
    asyncio.create_task(log_event(f"💳 **Admin WebApp:** Force-join channel removed {cuser or cid}. Remaining {len(chans)}"))
    return {"ok": True, "message": f"✅ Channel removed. Remaining: {names}", "channels": chans}


def sub_channels_clear() -> dict:
    from utils.subscription.store import set_settings
    set_settings(channels=[], channel_id=0, channel_username="")
    asyncio.create_task(log_event("💳 **Admin WebApp:** All force-join channels cleared."))
    return {"ok": True, "message": "✅ All force-join channels removed (free tier without channel).", "channels": []}


def sub_grant(user_id: int, tier: str, days: int) -> dict:
    from utils.subscription.tiers import TIERS
    from utils.subscription.store import set_subscription
    if not (10000 <= user_id <= 99999999999):
        return {"ok": False, "message": "❌ Invalid user ID (5-11 digits)."}
    if tier not in TIERS or tier == "free":
        return {"ok": False, "message": f"❌ Invalid tier `{tier}`. Use: basic / plus / pro."}
    if not (1 <= days <= 3650):
        return {"ok": False, "message": "❌ Days must be 1..3650."}
    entry = set_subscription(user_id, tier, duration_days=days, granted_by=f"admin:webapp")
    asyncio.create_task(log_event(f"💳 **Admin WebApp:** Granted {tier} ({days}d) to {user_id} (until {entry['until']})."))
    client = _tg_client()
    if client:
        asyncio.create_task(_notify_grant(client, user_id, tier, days))
    return {"ok": True, "message": f"✅ Granted **{tier}** to `{user_id}` for {days} days."}


async def _notify_grant(client, user_id: int, tier: str, days: int):
    try:
        from utils.subscription.tiers import TIERS as _T
        await client.send_message(user_id, f"✅ You received **{_T[tier]['label']}** for {days} days. Use /subscription to see status.")
    except Exception:
        pass


def sub_revoke(user_id: int) -> dict:
    from utils.subscription.store import remove_subscription
    if remove_subscription(user_id):
        asyncio.create_task(log_event(f"💳 **Admin WebApp:** Revoked subscription for {user_id}."))
        return {"ok": True, "message": f"✅ Revoked subscription for `{user_id}`."}
    return {"ok": False, "message": f"ℹ️ No active subscription for `{user_id}`."}


def sub_list() -> dict:
    from utils.subscription.store import list_subscriptions
    subs = list_subscriptions()
    now = int(_time.time())
    rows = []
    for uid, sub in sorted(subs.items(), key=lambda kv: kv[1].get("until", 0), reverse=True)[:50]:
        rows.append({
            "user_id": int(uid),
            "tier": sub.get("tier"),
            "until": sub.get("until", 0),
            "granted_by": sub.get("granted_by"),
            "active": int(sub.get("until", 0)) > now,
        })
    return {"ok": True, "subscriptions": rows}


# --------------------------------------------------------------------------
# Direct-forward
# --------------------------------------------------------------------------
def direct_state() -> dict:
    from modules import direct_forward
    state = direct_forward._load_state()
    ig_cookies = None
    tt_cookies = None
    try:
        ig_cookies = direct_forward._ig_sessionid_from_jar()
    except Exception:
        pass
    try:
        tt_cookies = direct_forward._tt_jar_cookies()
    except Exception:
        pass
    x_cookies = direct_forward._x_jar_cookies()
    x_uid = direct_forward._x_twid_user_id(x_cookies) if x_cookies else None
    tt_ok = bool(tt_cookies and tt_cookies.get("sessionid"))
    return {
        "ok": True,
        "relay_chat": getattr(config, "DIRECT_FORWARD_CHAT_ID", 0),
        "poll_seconds": getattr(config, "DIRECT_FORWARD_POLL_SECONDS", 300),
        "ig": {
            "enabled": bool(config.IG_DIRECT_ENABLED),
            "status": direct_forward.pairing_status("ig", state),
            "cookies_ok": bool(ig_cookies),
        },
        "x": {
            "enabled": bool(config.X_DIRECT_ENABLED),
            "cookies": "ok" if x_cookies and x_uid else ("missing" if not x_cookies else "bad"),
            "uid": x_uid,
            "pin_set": bool(getattr(config, "XCHAT_PIN", "")),
        },
        "tt": {
            "enabled": bool(getattr(config, "TIKTOK_DIRECT_ENABLED", False)),
            "cookies": "ok" if tt_ok else "missing",
        },
    }


def direct_toggle(platform: str) -> dict:
    flags = {
        "ig": ("IG_DIRECT_ENABLED", "Instagram"),
        "x": ("X_DIRECT_ENABLED", "X/Twitter"),
        "tiktok": ("TIKTOK_DIRECT_ENABLED", "TikTok"),
    }
    if platform not in flags:
        return {"ok": False, "message": "❌ Unknown platform."}
    env_key, label = flags[platform]
    new_state = not getattr(config, env_key)
    if new_state and not getattr(config, "DIRECT_FORWARD_CHAT_ID", 0):
        return {"ok": False, "message": "Set DIRECT_FORWARD_CHAT_ID in .env first — the relay needs a destination chat."}
    set_key(".env", env_key, str(new_state).lower())
    setattr(config, env_key, new_state)
    if new_state:
        asyncio.create_task(log_event(f"📨 **Admin WebApp:** {label} direct-forward enabled. Auto-restarting."))
        schedule_self_restart(delay=3.0)
        return {"ok": True, "message": f"✅ {label} direct-forward **enabled** — restarting the bot to activate."}
    asyncio.create_task(log_event(f"📨 **Admin WebApp:** {label} direct-forward disabled."))
    return {"ok": True, "message": f"✅ {label} direct-forward **disabled** — worker stops on next restart."}


def direct_pair_ig(uid: int) -> dict:
    from modules import direct_forward
    code = direct_forward.request_pair_code("ig", requested_by=uid)
    return {"ok": True, "message": f"🔗 Your one-time code: **`{code}`**\n\nSend it as a DM to the bot's Instagram account. Expires in 10 min.", "code": code}


def direct_unpair_ig() -> dict:
    from modules import direct_forward
    removed = direct_forward.unpair_platform("ig")
    asyncio.create_task(log_event("📨 **Admin WebApp:** Instagram DM pairing removed." if removed else "📨 **Admin WebApp:** Instagram unpair requested (was unpaired)."))
    return {"ok": True, "message": "💔 Instagram pairing removed." if removed else "ℹ️ No Instagram pairing existed."}


async def direct_test(platform: str) -> dict:
    from modules import direct_forward
    if platform == "x":
        result = await asyncio.get_event_loop().run_in_executor(None, direct_forward.test_x_connection)
    elif platform == "tiktok":
        result = await direct_forward.test_tiktok_connection()
    else:
        return {"ok": False, "message": "❌ Unknown platform."}
    ok = result.strip().startswith("✅")
    return {"ok": ok, "message": result}


def direct_set_x_pin(pin: str) -> dict:
    import re as _re
    pin = (pin or "").strip()
    if not _re.fullmatch(r"\d{4}", pin):
        return {"ok": False, "message": "❌ X Chat passcodes are **4 digits** (e.g. `0421`)."}
    set_key(".env", "XCHAT_PIN", pin)
    config.XCHAT_PIN = pin
    asyncio.create_task(log_event("🔑 **Admin WebApp:** X Chat PIN updated."))
    return {"ok": True, "message": "✅ X Chat PIN saved to `.env` — the bridge picks it up automatically."}


# --------------------------------------------------------------------------
# System
# --------------------------------------------------------------------------
def abort_queue() -> dict:
    queue_len = len(queue._pending)
    queue._pending.clear()
    queue._active = False
    if os.path.exists("cache"):
        try:
            shutil.rmtree("cache")
            os.makedirs("cache", exist_ok=True)
        except Exception:
            pass
    asyncio.create_task(log_event(f"💥 **Admin WebApp:** Queue reset executed. {queue_len} pending jobs aborted."))
    return {"ok": True, "message": f"💥 System Reset: {queue_len} queued jobs aborted and cache purged."}


def restart_bot() -> dict:
    asyncio.create_task(log_event("🔄 **Admin WebApp:** Bot restart requested by creator."))
    schedule_self_restart(delay=3.0)
    return {"ok": True, "message": "🔄 Restarting the bot… back in a few seconds."}
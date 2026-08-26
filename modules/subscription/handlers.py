"""User-facing subscription handlers: /subscription, callbacks, quota gate."""
from __future__ import annotations

import config
from pyrogram import Client, filters, ContinuePropagation
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from utils.propagation import stop

from utils.subscription.tiers import TIERS, TIER_ORDER
from utils.subscription.store import get_settings, is_subscription_active, get_subscription
from utils.subscription.quota import remaining_quota, check_quota
from utils.subscription.access import check_access
from utils.subscription.payments_stars import create_stars_invoice
from utils.subscription.payments_ton import TON_ADDRESS


def _sub_status_text(user_id: int) -> str:
    active, sub = is_subscription_active(user_id)
    if not active or not sub:
        s = get_settings()
        if not s.get("enabled"):
            return "🆓 Subscription mode is **OFF** — all authorized users have unlimited access."
        rem = remaining_quota(user_id)
        lim = TIERS["free"]["daily_limit"] if s.get("free_enabled") else 0
        if s.get("free_enabled"):
            return f"📦 **Free tier:** {rem}/{lim} downloads left today."
        return "🔒 No active subscription. Choose a tier below to unlock downloads."
    tier = sub.get("tier", "?")
    t = TIERS.get(tier, {})
    until = sub.get("until", 0)
    import time, datetime
    try:
        dt = datetime.datetime.fromtimestamp(until).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        dt = str(until)
    rem = remaining_quota(user_id)
    lim = t.get("daily_limit", "?")
    return f"✅ **{t.get('label', tier)}** — expires {dt}\n📦 {rem}/{lim} left today."


def _tiers_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for tid in TIER_ORDER:
        t = TIERS[tid]
        rows.append([InlineKeyboardButton(
            f"⭐ {t['label']} — {t['daily_limit']}/day — {t['price_stars']} Stars",
            callback_data=f"sub:buy:{tid}:stars"
        )])
        # TON row if configured
        if TON_ADDRESS and t.get("price_ton"):
            rows.append([InlineKeyboardButton(
                f"💎 {t['label']} via TON — {t['price_ton']} TON",
                callback_data=f"sub:buy:{tid}:ton"
            )])
    rows.append([InlineKeyboardButton("🔄 Verify TON payment", callback_data="sub:verify_ton")])
    rows.append([InlineKeyboardButton("📊 My quota", callback_data="sub:quota")])
    return InlineKeyboardMarkup(rows)


def _ton_pay_keyboard(tier: str) -> InlineKeyboardMarkup:
    t = TIERS.get(tier, {})
    addr = TON_ADDRESS or "— not configured —"
    # Show address; user must send with memo = user_id
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 I sent it — Verify", callback_data=f"sub:verify:{tier}")],
        [InlineKeyboardButton("◀️ Back", callback_data="sub:menu")],
    ])


_RATE_CB: dict[int, float] = {}

def _rate_limited(uid: int) -> bool:
    import time as _t
    now = _t.monotonic()
    last = _RATE_CB.get(uid, 0)
    limit = int(getattr(config, "SUB_RATE_LIMIT_SECONDS", 3) or 3)
    if now - last < limit:
        return True
    _RATE_CB[uid] = now
    return False

def register_subscription_handlers(app: Client):
    @app.on_message(filters.command("subscription") & filters.private, group=0)
    async def sub_cmd(client: Client, message: Message):
        user_id = message.from_user.id
        text = _sub_status_text(user_id)
        await message.reply_text(
            f"💳 **Subscription**\n\n{text}\n\nChoose a tier:",
            reply_markup=_tiers_keyboard()
        )
        stop(message)
    @app.on_message(filters.command("quota") & filters.private, group=0)
    async def quota_cmd(client: Client, message: Message):
        user_id = message.from_user.id
        active, sub = is_subscription_active(user_id)
        rem = remaining_quota(user_id)
        allowed, rem2, lim = check_quota(user_id)
        tier = sub.get("tier") if sub else "free"
        await message.reply_text(f"📊 **Quota:** {rem}/{lim} left today (tier: {tier}). {'✅ Can download' if allowed else '❌ Limit reached — resets at 00:00 UTC'}")
        stop(message)
    @app.on_callback_query(filters.regex(r"^sub:"))
    async def sub_callback(client: Client, cb: CallbackQuery):
        data = cb.data
        user_id = cb.from_user.id
        if _rate_limited(user_id):
            await cb.answer("Too fast — wait a moment.", show_alert=False)
            return
        parts = data.split(":")
        # sub:buy:<tier>:<method>
        if len(parts) >= 3 and parts[1] == "buy":
            tier = parts[2]
            method = parts[3] if len(parts) > 3 else "stars"
            if tier not in TIERS or tier == "free":
                await cb.answer("Invalid tier", show_alert=True)
                return
            if method == "stars":
                await cb.answer("Creating Stars invoice…")
                ok = await create_stars_invoice(client, cb.message.chat.id, tier)
                if not ok:
                    await cb.answer("Failed to create invoice", show_alert=True)
                return
            if method == "ton":
                if not TON_ADDRESS:
                    await cb.answer("TON payments not configured", show_alert=True)
                    return
                t = TIERS[tier]
                await cb.message.edit_text(
                    f"💎 **Pay {t['price_ton']} TON for {t['label']}**\n\n"
                    f"Send **{t['price_ton']} TON** to:\n`{TON_ADDRESS}`\n\n"
                    f"⚠️ **Memo / Comment must be:** `{user_id}`\n"
                    f"(your Telegram user ID — so we can credit you)\n\n"
                    f"After sending, tap **Verify** below. It may take 30s to confirm.",
                    reply_markup=_ton_pay_keyboard(tier)
                )
                await cb.answer()
                return
        if data == "sub:verify_ton" or data.startswith("sub:verify:"):
            # generic verify: try all tiers for this user (last attempted)
            # if specific tier provided, verify that tier
            tier = parts[2] if len(parts) > 2 and parts[2] in TIERS else None
            # if no specific tier, try to find any pending by scanning recent unverified?
            # For simplicity, try each tier in order (TON amount match will pick correct)
            from utils.subscription.payments_ton import verify_ton_payment
            import asyncio
            loop = asyncio.get_event_loop()
            if tier:
                found, msg = await loop.run_in_executor(None, verify_ton_payment, user_id, tier)
                await cb.answer(msg, show_alert=True)
                if found:
                    try:
                        await cb.message.edit_text(f"✅ {msg}\n\n{_sub_status_text(user_id)}", reply_markup=_tiers_keyboard())
                    except Exception:
                        pass
            else:
                # try all tiers
                for tid in reversed(TIER_ORDER):  # highest first
                    found, msg = await loop.run_in_executor(None, verify_ton_payment, user_id, tid)
                    if found:
                        await cb.answer(msg, show_alert=True)
                        try:
                            await cb.message.edit_text(f"✅ {msg}\n\n{_sub_status_text(user_id)}", reply_markup=_tiers_keyboard())
                        except Exception:
                            pass
                        return
                await cb.answer("No matching TON transaction found yet. Wait 30s and try again.", show_alert=True)
            return
        if data == "sub:menu":
            await cb.message.edit_text(f"💳 **Subscription**\n\n{_sub_status_text(user_id)}\n\nChoose a tier:", reply_markup=_tiers_keyboard())
            await cb.answer()
            return
        if data == "sub:quota":
            rem = remaining_quota(user_id)
            allowed, _, lim = check_quota(user_id)
            await cb.answer(f"Quota: {rem}/{lim} left today. {'Can download' if allowed else 'Limit reached'}", show_alert=True)
            return


def register_subscription_payments(app: Client):
    """Wire Stars pre_checkout (raw) + successful_payment handlers."""
    from pyrogram import filters

    # Stars: answer pre_checkout_query via raw update — pyrogram 2.0.106 has no
    # high-level filter. CRITICAL: this is a RawUpdateHandler, and pyrogram's
    # dispatcher treats a RawUpdateHandler as matching EVERY update; if the
    # callback returns normally the dispatcher `break`s the group and any
    # handler registered AFTER it in the same group is starved before it ever
    # sees the update — which is why the ported extras (translate/web/github/
    # youtube) registered later in group 0 silently ignored /tr, /search etc.
    # So the handler MUST raise pyrogram.ContinuePropagation so the dispatcher
    # keeps iterating the rest of the group. It is raised OUTSIDE the try so the
    # `except Exception` below never swallows it (ContinuePropagation subclasses
    # Exception).
    from pyrogram.raw.types import UpdateBotPrecheckoutQuery

    @app.on_raw_update(group=0)
    async def _raw_precheckout(client, update, users, chats):
        try:
            if isinstance(update, UpdateBotPrecheckoutQuery):
                # synthesize minimal query object for handler
                class _Q:
                    pass
                q = _Q()
                q.id = getattr(update, "query_id", "")
                q.invoice_payload = getattr(update, "payload", b"")
                if isinstance(q.invoice_payload, bytes):
                    try:
                        q.invoice_payload = q.invoice_payload.decode()
                    except Exception:
                        q.invoice_payload = ""
                uid = getattr(update, "user_id", 0)
                # pyrogram users dict maps peer id -> User
                q.from_user = users.get(uid) if isinstance(users, dict) else None
                if q.from_user is None:
                    # fallback: mock with id only
                    class _U:
                        id = uid
                    q.from_user = _U()
                async def _answer(ok=True, error_message=None):
                    from utils.subscription.payments_stars import _bot_api
                    import asyncio
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, lambda: _bot_api("answerPreCheckoutQuery", {"pre_checkout_query_id": q.id, "ok": ok, **({"error_message": error_message} if error_message else {})}))
                q.answer = _answer
                from utils.subscription.payments_stars import handle_pre_checkout
                await handle_pre_checkout(client, q)
        except Exception:
            raise
        # Always let the dispatcher continue to the next handler in this group.
        raise ContinuePropagation

    # successful_payment — message filter via custom create
    def _is_success(m):
        return bool(getattr(m, "successful_payment", None))
    try:
        pay_filter = filters.create(lambda _, __, m: bool(getattr(m, "successful_payment", None)))
    except Exception:
        pay_filter = filters.create(_is_success)

    @app.on_message(pay_filter & filters.private, group=0)
    async def _on_success(client, message):
        from utils.subscription.payments_stars import handle_successful_payment
        await handle_successful_payment(client, message)


async def gate_and_quota_check(client, message: Message) -> bool:
    """
    Gate for downloader_handler: returns True if request may proceed, else sends message and returns False.
    Handles: blacklist, subscription, channel force-join, quota.
    """
    user_id = message.from_user.id
    settings = get_settings()
    if not settings.get("enabled"):
        # legacy: rely on is_authorized check already done by security gate
        return True
    # subscription mode: detailed gate
    ok, reason = await check_access(client, user_id)
    if not ok:
        if reason == "blocked_blacklisted":
            await message.reply_text("🚫 You are blacklisted.")
            return False
        if reason == "need_channel":
            from utils.subscription.store import get_channels
            from utils.subscription.access import check_all_channels
            chans = get_channels()
            _, missing = await check_all_channels(client, user_id)
            if not missing:
                missing = chans
            lines = []
            kb_rows = []
            for ch in missing:
                cuser = ch.get("username") or ""
                cid = ch.get("id", 0)
                if cuser:
                    link = f"https://t.me/{cuser.lstrip('@')}"
                    lines.append(f"• {cuser} — {link}")
                    kb_rows.append([InlineKeyboardButton(f"📢 Join {cuser}", url=link)])
                else:
                    lines.append(f"• channel `{cid}`")
            kb = InlineKeyboardMarkup(kb_rows) if kb_rows else None
            await message.reply_text(
                f"🔒 Free access requires joining our channel(s) first:\n" + "\n".join(lines) + "\n\nJoin all, then send your link again. Or use /subscription to unlock without joining.",
                reply_markup=kb
            )
            return False
        # need_subscription
        await message.reply_text(
            f"🔒 **Subscription required.**\n\n{_sub_status_text(user_id)}\n\nUse /subscription to choose a tier.",
            reply_markup=_tiers_keyboard()
        )
        return False
    # quota
    allowed, rem, lim = check_quota(user_id)
    if not allowed:
        await message.reply_text(f"⏳ Daily limit reached ({lim}/day). Try again after 00:00 UTC.\nUse /subscription to upgrade.")
        return False
    return True

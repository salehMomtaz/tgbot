"""Stars (XTR) payments via Bot API sendInvoice.

Why raw HTTP: pyrogram 2.0.106 has no high-level sendInvoice yet, but we can
call Bot API directly with BOT_TOKEN. Uses XTR currency, provider_token=''.

Flow:
 - create_stars_invoice(user_id, tier) → sends invoice via Bot API
 - handle_pre_checkout_query → answer with ok=True after validating payload
 - handle_successful_payment → activate subscription
"""
from __future__ import annotations

import json
import logging
import time
import hmac
import hashlib
import urllib.request
import urllib.parse

import config
from .tiers import TIERS
from .store import set_subscription

log = logging.getLogger(__name__)

# HMAC key for payload signing — use BOT_TOKEN as secret (never expose payload)
def _sign_payload(user_id: int, tier: str) -> str:
    msg = f"{user_id}:{tier}:{int(time.time()) // 3600}"
    key = (config.BOT_TOKEN or "secret").encode()
    return hmac.new(key, msg.encode(), hashlib.sha256).hexdigest()[:16]

def _make_payload(user_id: int, tier: str) -> str:
    # payload format: sub:<user_id>:<tier>:<sig>
    sig = _sign_payload(user_id, tier)
    return f"sub:{user_id}:{tier}:{sig}"

def parse_payload(payload: str) -> tuple[int, str] | None:
    try:
        parts = payload.split(":")
        if len(parts) != 4 or parts[0] != "sub":
            return None
        uid = int(parts[1])
        tier = parts[2]
        if tier not in TIERS or tier == "free":
            return None
        # verify sig (allow current hour and previous hour for clock skew)
        key = (config.BOT_TOKEN or "secret").encode()
        for h in [0, 1]:
            msg = f"{uid}:{tier}:{(int(time.time()) // 3600) - h}"
            expected = hmac.new(key, msg.encode(), hashlib.sha256).hexdigest()[:16]
            if hmac.compare_digest(expected, parts[3]):
                return uid, tier
        return None
    except Exception:
        return None


def _bot_api(method: str, params: dict) -> dict:
    token = config.BOT_TOKEN
    if not token:
        raise RuntimeError("BOT_TOKEN not set")
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(params).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            return json.loads(body)
    except Exception as e:
        log.warning("Bot API %s failed: %s", method, e)
        return {"ok": False, "description": str(e)}


async def create_stars_invoice(client, chat_id: int, tier: str) -> bool:
    """Send Stars invoice via Bot API. Returns True if sent."""
    t = TIERS.get(tier)
    if not t or tier == "free":
        return False
    payload = _make_payload(chat_id, tier)
    title = f"{t['label']} — {t['daily_limit']} downloads/day (30 days)"
    desc = f"Unlock {t['label']} tier: {t['daily_limit']} downloads per day for 30 days. Priority support."
    prices = [{"label": t["label"], "amount": t["price_stars"]}]
    # Bot API sendInvoice with XTR
    params = {
        "chat_id": chat_id,
        "title": title,
        "description": desc,
        "payload": payload,
        "provider_token": "",
        "currency": "XTR",
        "prices": prices,
    }
    # Must run in thread to not block event loop (urllib is sync)
    import asyncio
    loop = asyncio.get_event_loop()
    res = await loop.run_in_executor(None, lambda: _bot_api("sendInvoice", params))
    if not res.get("ok"):
        log.warning("sendInvoice failed: %s", res)
        # fallback: try via pyrogram raw if available
        try:
            await client.send_message(chat_id, f"❌ Could not create Stars invoice: {res.get('description')}")
        except Exception:
            pass
        return False
    return True


async def handle_pre_checkout(client, query) -> None:
    """Answer pre_checkout_query. Must answer within 10s."""
    payload = getattr(query, "invoice_payload", "") or ""
    parsed = parse_payload(payload)
    if not parsed:
        try:
            await query.answer(ok=False, error_message="Invalid payment payload.")
        except Exception:
            # fallback via raw API
            _bot_api("answerPreCheckoutQuery", {"pre_checkout_query_id": query.id, "ok": False, "error_message": "Invalid payload"})
        return
    # verify user matches payload
    uid, tier = parsed
    if query.from_user and query.from_user.id != uid:
        try:
            await query.answer(ok=False, error_message="User mismatch.")
        except Exception:
            _bot_api("answerPreCheckoutQuery", {"pre_checkout_query_id": query.id, "ok": False, "error_message": "User mismatch"})
        return
    try:
        await query.answer(ok=True)
    except Exception:
        _bot_api("answerPreCheckoutQuery", {"pre_checkout_query_id": query.id, "ok": True})


async def handle_successful_payment(client, message) -> None:
    pay = getattr(message, "successful_payment", None)
    if not pay:
        return
    payload = getattr(pay, "invoice_payload", "") or ""
    parsed = parse_payload(payload)
    if not parsed:
        log.warning("Successful payment with invalid payload: %s", payload)
        return
    uid, tier = parsed
    # Use message.from_user.id as ground truth; payload already verified user match at checkout
    actual_uid = message.from_user.id if message.from_user else uid
    t = TIERS.get(tier)
    if not t:
        return
    amount = getattr(pay, "total_amount", t["price_stars"])
    # For Stars, total_amount is in Stars units
    if amount < t["price_stars"]:
        log.warning("Underpaid Stars: %s < %s", amount, t["price_stars"])
        return
    charge_id = getattr(pay, "provider_payment_charge_id", "") or getattr(pay, "telegram_payment_charge_id", "")
    set_subscription(actual_uid, tier, duration_days=t["duration_days"], granted_by="stars", price_stars=amount, tx=charge_id)
    try:
        await message.reply_text(f"✅ Payment received! **{t['label']}** activated for 30 days.\nDaily limit: **{t['daily_limit']}** downloads.")
    except Exception:
        pass
    try:
        from main import log_event
        await log_event(f"💰 **Stars Payment:** User `{actual_uid}` bought **{tier}** ({amount} XTR).")
    except Exception:
        pass

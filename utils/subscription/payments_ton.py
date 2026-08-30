"""TON / Gram payments via toncenter API.

User sends TON to configured address with memo = user_id.
We poll toncenter getTransactions and match incoming transfers.

No private key needed — only verification of inbound transfers.

Config:
  SUB_TON_ADDRESS - wallet to receive TON (bounceable)
  SUB_TON_API_KEY - toncenter API key (optional but recommended)
  SUB_TON_TONAPI_KEY - alternative if using tonapi.io

Tiers map to TON amounts in tiers.py (price_ton).

Security:
 - memo must be exact user_id (digits)
 - tx is deduped via used_tx set in database.json (sub_used_tx)
 - amount must be >= tier price (with 1% tolerance for fees? no, require full)
"""
from __future__ import annotations

import logging
import json
import urllib.request
import urllib.parse

import config
from .tiers import TIERS
from .store import set_subscription

log = logging.getLogger(__name__)

def _get_ton_config():
    addr = getattr(config, "SUB_TON_ADDRESS", "") or __import__("os").getenv("SUB_TON_ADDRESS", "")
    key = getattr(config, "SUB_TON_API_KEY", "") or __import__("os").getenv("SUB_TON_API_KEY", "")
    return addr, key

TON_ADDRESS, TON_API_KEY = _get_ton_config()

def _ton_api_get_transactions(limit: int = 20) -> list:
    if not TON_ADDRESS:
        return []
    # toncenter.com API
    base = "https://toncenter.com/api/v2/json/getTransactions"
    params = {
        "address": TON_ADDRESS,
        "limit": str(limit),
        "archival": "true",
    }
    url = base + "?" + urllib.parse.urlencode(params)
    headers = {}
    if TON_API_KEY:
        headers["X-API-Key"] = TON_API_KEY
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            if data.get("ok"):
                return data.get("result", [])
            log.warning("toncenter error: %s", data)
            return []
    except Exception as e:
        log.warning("TON API failed: %s", e)
        return []


def verify_ton_payment(user_id: int, tier: str) -> tuple[bool, str]:
    """
    Scan recent TON tx and activate if found.
    Returns (found, msg). Synchronous — caller should run in executor.
    """
    t = TIERS.get(tier)
    if not t or tier == "free":
        return False, "Invalid tier"
    if not TON_ADDRESS:
        return False, "TON payments not configured (SUB_TON_ADDRESS missing)"
    needed = float(t["price_ton"])
    if needed <= 0:
        return False, "TON price not set for this tier"

    from utils.gate import load_database, save_database
    db = load_database()
    used = set(db.get("sub_used_tx", []))

    txs = _ton_api_get_transactions(limit=50)
    for tx in txs:
        tx_id = tx.get("transaction_id", {}).get("hash") or tx.get("transaction_id", {}).get("lt") or str(tx.get("transaction_id"))
        if tx_id in used:
            continue
        in_msg = tx.get("in_msg", {})
        if not in_msg:
            continue
        # value in nanotons
        val = in_msg.get("value")
        try:
            ton_val = int(val) / 1e9 if val else 0
        except Exception:
            ton_val = 0
        if ton_val < needed - 0.001:  # tiny tolerance
            continue
        # check memo/comment
        msg_data = in_msg.get("msg_data", {})
        text = ""
        if isinstance(msg_data, dict):
            text = msg_data.get("text", "") or ""
            # body may be base64
            if not text and "body" in msg_data:
                try:
                    import base64
                    text = base64.b64decode(msg_data["body"]).decode(errors="ignore")
                except Exception:
                    pass
        # Fallback: raw message field
        if not text:
            text = in_msg.get("message", "") or in_msg.get("comment", "") or ""
        text = str(text).strip()
        # memo should be user_id digits
        if text == str(user_id):
            # mark used
            db.setdefault("sub_used_tx", []).append(tx_id)
            save_database(db)
            set_subscription(user_id, tier, duration_days=t["duration_days"], granted_by="ton", price_stars=0, tx=tx_id)
            return True, f"✅ TON payment verified ({ton_val} TON) — {tier} activated!"
    return False, "No matching TON transaction found yet. Make sure you sent exactly with memo = your user ID, then tap Verify again in 30s."


async def handle_ton_verify(client, message, tier: str) -> None:
    user_id = message.from_user.id
    import asyncio
    loop = asyncio.get_event_loop()
    found, msg = await loop.run_in_executor(None, verify_ton_payment, user_id, tier)
    try:
        await message.reply_text(msg)
    except Exception:
        pass
    if found:
        try:
            from main import log_event
            await log_event(f"💰 **TON Payment:** User `{user_id}` bought **{tier}** via TON.")
        except Exception:
            pass

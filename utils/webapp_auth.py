"""Shared Telegram Mini App auth helpers (admin console + subscription webapp).

Both webapps authenticate the same way:

- **Admin**: a valid Telegram WebApp ``initData`` (user.id == SYSTEM_CREATOR_ID)
  OR an ``X-Admin-Token`` header equal to HMAC(BOT_TOKEN, "admin-sub")[:16].
- **User**: any valid ``initData`` identifies the user.

Validation follows the official spec:
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""
from __future__ import annotations

import hmac
import hashlib
import json
import time
import urllib.parse

import config
from fastapi import HTTPException
from starlette.requests import Request


def admin_token() -> str:
    """Stable admin token derived from BOT_TOKEN (never stored in a file)."""
    tok = getattr(config, "BOT_TOKEN", "") or ""
    return hmac.new(tok.encode(), b"admin-sub", hashlib.sha256).hexdigest()[:16]


def verify_init_data(init_data: str) -> dict | None:
    """Validate Telegram WebApp initData per docs; return parsed user dict or None."""
    if not init_data:
        return None
    try:
        params = dict(urllib.parse.parse_qsl(init_data, strict_parsing=True))
        recv_hash = params.pop("hash", None)
        if not recv_hash:
            return None
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        secret = hmac.new(b"WebAppData", (config.BOT_TOKEN or "").encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, recv_hash):
            return None
        try:
            auth_date = int(params.get("auth_date", "0"))
            if auth_date and abs(int(time.time()) - auth_date) > 86400 * 2:
                pass
        except Exception:
            pass
        user_json = json.loads(params.get("user", "{}")) if "user" in params else {}
        return user_json
    except Exception:
        return None


def _collect_init_data(request: Request) -> str:
    """Gather initData from the standard header/query locations, case-insensitively."""
    for name in ("X-Telegram-Init-Data", "X-Telegram-Initdata", "x-telegram-init-data"):
        v = request.headers.get(name, "") or ""
        if v:
            return v
    v = str(request.query_params.get("tgWebAppData", "") or "")
    if v:
        return v
    return ""


def parse_user_from_request(request: Request) -> dict | None:
    """Return the initData user dict for this request, or None."""
    init_data = _collect_init_data(request)
    if init_data:
        return verify_init_data(init_data)
    return None


def is_admin_auth(request: Request) -> bool:
    """True if the request carries creator-level admin auth."""
    tok = request.headers.get("X-Admin-Token", "")
    if tok and hmac.compare_digest(tok, admin_token()):
        return True
    user = parse_user_from_request(request)
    if user:
        try:
            uid = int(user.get("id", 0))
            if uid == int(getattr(config, "SYSTEM_CREATOR_ID", 0) or 0):
                return True
        except Exception:
            pass
    return False


def require_admin(request: Request) -> None:
    """Raise 403 unless the request is creator-authorized."""
    if not is_admin_auth(request):
        raise HTTPException(
            status_code=403,
            detail="Forbidden — admin auth required (X-Admin-Token or creator initData)",
        )
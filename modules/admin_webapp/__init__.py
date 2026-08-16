"""Admin WebApp — full admin console as a Telegram Mini App.

Mounted on the existing FastAPI server alongside the subscription webapp:

    GET  /admin       -> full admin console SPA (creator auth via initData/token)
    /admin/api/*      -> JSON endpoints (see api.py)

Every feature of the in-chat admin console (modules/admin/*) is available here:
users, cookie jars, PO token provider, premium uploads + session generation,
subscriptions + force-join channels, direct-forward relays, and the system
actions (abort queue / restart).
"""
from __future__ import annotations

from fastapi.responses import HTMLResponse, JSONResponse
from fastapi import HTTPException
from starlette.requests import Request

from . import actions
from .api import router as admin_api_router
from .ui import HTML


def mount(fastapi_app):
    fastapi_app.include_router(admin_api_router)

    @fastapi_app.get("/admin", response_class=HTMLResponse)
    async def _admin_page():
        return HTML

    @fastapi_app.get("/admin/api/health")
    async def _health(request: Request):
        # Public liveness probe (no auth) so the bot's uptime checks work.
        return JSONResponse({"ok": True})
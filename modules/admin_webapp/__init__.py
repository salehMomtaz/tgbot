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

# Telegram's Mini App WebView caches aggressively; never let a stale console
# (or a mid-edit, half-broken one) linger. No-store on both the SPA and every
# API response so updates always reach the operator on next open.
_CACHE_HEADERS = {"Cache-Control": "no-store, no-cache, must-revalidate"}


def mount(fastapi_app):
    fastapi_app.include_router(admin_api_router)

    @fastapi_app.get("/admin", response_class=HTMLResponse)
    async def _admin_page():
        return HTMLResponse(HTML, headers=_CACHE_HEADERS)

    @fastapi_app.middleware("http")
    async def _no_store_admin_api(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/admin/api/"):
            response.headers["Cache-Control"] = _CACHE_HEADERS["Cache-Control"]
        return response

    @fastapi_app.get("/admin/api/health")
    async def _health(request: Request):
        # Public liveness probe (no auth) so the bot's uptime checks work.
        return JSONResponse({"ok": True})
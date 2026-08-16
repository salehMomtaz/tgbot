"""Admin WebApp — FastAPI endpoints (thin auth + call-through layer).

Every route requires creator-level auth (valid Telegram initData OR
``X-Admin-Token``). Long-running probes (cookie test / PO diagnosis / direct
tests) run inside ``actions`` via executors so the bot's event loop is never
blocked — exactly like the in-chat console does.
"""
from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse, Response

from utils.webapp_auth import require_admin
from . import actions


router = APIRouter(prefix="/admin/api", tags=["admin-console"])


def _h(request: Request) -> None:
    require_admin(request)


# --- state -----------------------------------------------------------------
@router.get("/state")
async def api_state(request: Request):
    _h(request)
    return JSONResponse(await actions.overview())


# --- users -----------------------------------------------------------------
@router.get("/users")
async def api_users(request: Request):
    _h(request)
    return JSONResponse(actions.list_users())


@router.post("/users/add")
async def api_user_add(request: Request):
    _h(request)
    body = await request.json()
    uid = int(body.get("user_id", 0) or 0)
    return JSONResponse(actions.add_user_action(uid))


@router.post("/users/remove")
async def api_user_remove(request: Request):
    _h(request)
    body = await request.json()
    uid = int(body.get("user_id", 0) or 0)
    return JSONResponse(actions.remove_user_action(uid))


@router.post("/users/unban")
async def api_user_unban(request: Request):
    _h(request)
    body = await request.json()
    uid = int(body.get("user_id", 0) or 0)
    return JSONResponse(actions.unban_user_action(uid))


@router.post("/doc-mode")
async def api_doc_mode(request: Request):
    _h(request)
    user = _creator_uid(request)
    return JSONResponse(actions.toggle_doc_action(user))


def _creator_uid(request: Request) -> int:
    from utils.webapp_auth import parse_user_from_request
    user = parse_user_from_request(request)
    if user:
        try:
            return int(user.get("id", 0) or 0)
        except Exception:
            pass
    from utils.webapp_auth import admin_token
    import config
    return int(getattr(config, "SYSTEM_CREATOR_ID", 0) or 0)


# --- cookies ---------------------------------------------------------------
@router.get("/cookies")
async def api_cookies(request: Request):
    _h(request)
    return JSONResponse(actions.cookie_jars_state())


@router.get("/cookies/per-site")
async def api_cookies_per_site(request: Request):
    _h(request)
    return JSONResponse(actions.per_site_jars_state())


@router.get("/cookies/{key}/download")
async def api_cookie_download(request: Request, key: str):
    _h(request)
    content, filename = actions.cookie_download(key)
    if content is None:
        raise HTTPException(status_code=404, detail="Cookie jar is empty or does not exist.")
    return Response(
        content=content,
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/cookies/{key}/upload")
async def api_cookie_upload(request: Request, key: str, file: UploadFile = File(...)):
    _h(request)
    data = await file.read()
    try:
        content = data.decode("utf-8", errors="replace")
    except Exception:
        content = ""
    return JSONResponse(actions.cookie_replace(key, content))


@router.post("/cookies/{key}/backup")
async def api_cookie_backup(request: Request, key: str):
    _h(request)
    return JSONResponse(actions.cookie_backup(key))


@router.post("/cookies/{key}/restore")
async def api_cookie_restore(request: Request, key: str):
    _h(request)
    return JSONResponse(actions.cookie_restore(key))


@router.post("/cookies/{key}/test")
async def api_cookie_test(request: Request, key: str):
    _h(request)
    force_pot = key == "ytcookies"
    return JSONResponse(await actions.cookie_test(key, force_pot=force_pot))


@router.post("/cookies/per-site/upload")
async def api_cookie_per_site_upload(request: Request, site: str = Form(...), file: UploadFile = File(...)):
    _h(request)
    data = await file.read()
    try:
        content = data.decode("utf-8", errors="replace")
    except Exception:
        content = ""
    return JSONResponse(actions.per_site_jar_replace(site.lower(), content))


@router.post("/cookies/per-site/{site}/delete")
async def api_cookie_per_site_delete(request: Request, site: str):
    _h(request)
    return JSONResponse(actions.per_site_jar_delete(site.lower()))


# --- premium uploads -------------------------------------------------------
@router.get("/premium")
async def api_premium(request: Request):
    _h(request)
    return JSONResponse(actions.premium_state())


@router.post("/premium/add")
async def api_premium_add(request: Request):
    _h(request)
    body = await request.json()
    return JSONResponse(actions.premium_add(int(body.get("user_id", 0) or 0)))


@router.post("/premium/remove")
async def api_premium_remove(request: Request):
    _h(request)
    body = await request.json()
    return JSONResponse(actions.premium_remove(int(body.get("user_id", 0) or 0)))


@router.get("/premium/gen")
async def api_premium_gen_state(request: Request):
    _h(request)
    uid = _creator_uid(request)
    return JSONResponse(actions.premium_gen_state(uid))


@router.post("/premium/gen/start")
async def api_premium_gen_start(request: Request):
    _h(request)
    uid = _creator_uid(request)
    body = await request.json()
    return JSONResponse(await actions.premium_gen_start(uid, str(body.get("phone", "") or "")))


@router.post("/premium/gen/verify")
async def api_premium_gen_verify(request: Request):
    _h(request)
    uid = _creator_uid(request)
    body = await request.json()
    return JSONResponse(await actions.premium_gen_verify(uid, str(body.get("code", "") or "")))


@router.post("/premium/gen/password")
async def api_premium_gen_password(request: Request):
    _h(request)
    uid = _creator_uid(request)
    body = await request.json()
    return JSONResponse(await actions.premium_gen_password(uid, str(body.get("password", "") or "")))


@router.post("/premium/gen/save")
async def api_premium_gen_save(request: Request):
    _h(request)
    uid = _creator_uid(request)
    return JSONResponse(await actions.premium_gen_save(uid))


@router.post("/premium/gen/abort")
async def api_premium_gen_abort(request: Request):
    _h(request)
    uid = _creator_uid(request)
    return JSONResponse(await actions.premium_gen_abort(uid))


# --- PO token provider -----------------------------------------------------
@router.get("/pot")
async def api_pot(request: Request):
    _h(request)
    return JSONResponse(actions.pot_state())


@router.post("/pot/start")
async def api_pot_start(request: Request):
    _h(request)
    return JSONResponse(await actions.pot_start())


@router.post("/pot/stop")
async def api_pot_stop(request: Request):
    _h(request)
    return JSONResponse(await actions.pot_stop())


@router.post("/pot/diagnose")
async def api_pot_diagnose(request: Request):
    _h(request)
    return JSONResponse(await actions.pot_diagnose())


@router.post("/pot/test")
async def api_pot_test(request: Request):
    _h(request)
    return JSONResponse(await actions.cookie_test("ytcookies", force_pot=True))


# --- subscriptions ---------------------------------------------------------
@router.get("/sub")
async def api_sub(request: Request):
    _h(request)
    return JSONResponse(actions.sub_state())


@router.post("/sub/toggle")
async def api_sub_toggle(request: Request):
    _h(request)
    return JSONResponse(actions.sub_toggle())


@router.post("/sub/toggle-free")
async def api_sub_toggle_free(request: Request):
    _h(request)
    return JSONResponse(actions.sub_toggle_free())


@router.post("/sub/channels/add")
async def api_sub_channel_add(request: Request):
    _h(request)
    body = await request.json()
    return JSONResponse(await actions.sub_channel_add(str(body.get("input", "") or "")))


@router.post("/sub/channels/remove")
async def api_sub_channel_remove(request: Request):
    _h(request)
    body = await request.json()
    return JSONResponse(await actions.sub_channel_remove(str(body.get("input", "") or "")))


@router.post("/sub/channels/clear")
async def api_sub_channels_clear(request: Request):
    _h(request)
    return JSONResponse(actions.sub_channels_clear())


@router.post("/sub/grant")
async def api_sub_grant(request: Request):
    _h(request)
    body = await request.json()
    return JSONResponse(actions.sub_grant(
        int(body.get("user_id", 0) or 0),
        str(body.get("tier", "") or ""),
        int(body.get("days", 30) or 30),
    ))


@router.post("/sub/revoke")
async def api_sub_revoke(request: Request):
    _h(request)
    body = await request.json()
    return JSONResponse(actions.sub_revoke(int(body.get("user_id", 0) or 0)))


@router.get("/sub/list")
async def api_sub_list(request: Request):
    _h(request)
    return JSONResponse(actions.sub_list())


# --- direct forward --------------------------------------------------------
@router.get("/direct")
async def api_direct(request: Request):
    _h(request)
    return JSONResponse(actions.direct_state())


@router.post("/direct/toggle")
async def api_direct_toggle(request: Request):
    _h(request)
    body = await request.json()
    return JSONResponse(actions.direct_toggle(str(body.get("platform", "") or "")))


@router.post("/direct/pair-ig")
async def api_direct_pair_ig(request: Request):
    _h(request)
    uid = _creator_uid(request)
    return JSONResponse(actions.direct_pair_ig(uid))


@router.post("/direct/unpair-ig")
async def api_direct_unpair_ig(request: Request):
    _h(request)
    return JSONResponse(actions.direct_unpair_ig())


@router.post("/direct/test")
async def api_direct_test(request: Request):
    _h(request)
    body = await request.json()
    return JSONResponse(await actions.direct_test(str(body.get("platform", "") or "")))


@router.post("/direct/set-x-pin")
async def api_direct_set_x_pin(request: Request):
    _h(request)
    body = await request.json()
    return JSONResponse(actions.direct_set_x_pin(str(body.get("pin", "") or "")))


# --- system ----------------------------------------------------------------
@router.post("/queue/abort")
async def api_queue_abort(request: Request):
    _h(request)
    return JSONResponse(actions.abort_queue())


@router.post("/restart")
async def api_restart(request: Request):
    _h(request)
    return JSONResponse(actions.restart_bot())
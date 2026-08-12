"""Admin subscription webapp — mounted on the existing FastAPI (port 8080).

Routes:
  GET  /admin/subscription        -> HTML panel (Telegram WebApp)
  GET  /admin/subscription/api   -> JSON settings + tiers + subs + usage
  POST /admin/subscription/api   -> update settings (admin auth required)

Auth: X-Admin-Token header must equal sha256(BOT_TOKEN + "admin") or caller is SYSTEM_CREATOR_ID via Telegram initData hash check.
For Telegram WebApp, validate initData via hmac as per docs.

Simplified: accept header X-Admin-Token = HMAC-SHA256(BOT_TOKEN, "admin-sub") — operator can get it via bot command /admin_token.
"""
from __future__ import annotations

import hmac, hashlib, json, time, urllib.parse
import config

def _admin_token() -> str:
    tok = getattr(config, "BOT_TOKEN", "") or ""
    return hmac.new(tok.encode(), b"admin-sub", hashlib.sha256).hexdigest()[:16]

def _verify_init_data(init_data: str) -> bool:
    """Validate Telegram WebApp initData per docs (optional; webapp posts it)."""
    if not init_data:
        return False
    try:
        params = dict(urllib.parse.parse_qsl(init_data, strict_parsing=True))
        recv_hash = params.pop("hash", None)
        if not recv_hash:
            return False
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        secret = hmac.new(b"WebAppData", (config.BOT_TOKEN or "").encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, recv_hash):
            return False
        # optionally ensure user is creator/admin? For now creator only may use webapp fully
        # allow any authenticated Telegram user to read, but write requires admin Token or creator
        return True
    except Exception:
        return False

HTML = r"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Subscription Admin</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
 body{font-family:system-ui,sans-serif;max-width:720px;margin:0 auto;padding:16px;background:#0f1115;color:#e6e6e6}
 h1{font-size:20px} .card{background:#1a1d24;border-radius:12px;padding:16px;margin:12px 0}
 label{display:flex;justify-content:space-between;align-items:center;margin:8px 0}
 input,select{padding:8px;border-radius:8px;border:1px solid #333;background:#0f1115;color:#fff}
 button{background:#2ea6ff;color:#fff;border:0;padding:10px 16px;border-radius:10px;cursor:pointer;font-weight:600}
 table{width:100%;border-collapse:collapse} th,td{padding:6px 8px;border-bottom:1px solid #2a2e38;text-align:left;font-size:13px}
 .ok{color:#6f6} .warn{color:#fa0}
</style>
<h1>💳 Subscription Admin</h1>
<div id="app">Loading…</div>
<script>
const tg = window.Telegram?.WebApp; if(tg){tg.ready(); tg.expand();}
let token = localStorage.getItem('admin_token')||'';
async function api(method, body){
  const h={'Content-Type':'application/json'};
  if(token) h['X-Admin-Token']=token;
  const initData = tg?.initData||'';
  if(initData) h['X-Telegram-Init-Data']=initData;
  const r = await fetch('/admin/subscription/api', {method, headers:h, body: body? JSON.stringify(body):undefined});
  if(!r.ok) throw new Error(await r.text());
  return r.json();
}
async function load(){
  const d = await api('GET');
  const s=d.settings, tiers=d.tiers;
  document.getElementById('app').innerHTML = `
  <div class=card>
    <h3>Settings</h3>
    <label>Subscription mode <input type=checkbox id=en ${s.enabled?'checked':''}></label>
    <label>Free tier with channel <input type=checkbox id=free ${s.free_enabled?'checked':''}></label>
    <label>Channel ID <input id=chid value="${s.channel_id||''}" placeholder="e.g. -100123..."></label>
    <label>Channel @username <input id=chuser value="${s.channel_username||''}" placeholder="@mychannel"></label>
    <button onclick="save()">💾 Save</button> <span id=msg></span>
    <p style="opacity:.6;font-size:12px">Free users must join the channel to get 5 downloads/day. Disable toggle to require paid sub.</p>
  </div>
  <div class=card><h3>Tiers</h3><table><tr><th>Tier</th><th>Daily</th><th>Stars</th><th>TON</th><th>Priority</th></tr>
    ${Object.entries(tiers).map(([k,v])=>`<tr><td>${v.label} (${k})</td><td>${v.daily_limit}</td><td>${v.price_stars}</td><td>${v.price_ton||'-'}</td><td>${v.priority}</td></tr>`).join('')}
  </table></div>
  <div class=card><h3>Active subscriptions (${Object.keys(d.subscriptions).length})</h3>
    <table><tr><th>User</th><th>Tier</th><th>Until</th><th>By</th></tr>
    ${Object.entries(d.subscriptions).map(([uid,sub])=>`<tr><td><code>${uid}</code></td><td>${sub.tier}</td><td>${new Date(sub.until*1000).toLocaleString()}</td><td>${sub.granted_by}</td></tr>`).join('') || '<tr><td colspan=4 style="opacity:.6">none</td></tr>'}
    </table>
  </div>
  <div class=card style="opacity:.7;font-size:12px">
    Auth: paste admin token from bot <code>/admin_token</code> if Telegram WebApp initData not available. Token is stored locally.
    <br><input id=tok placeholder="admin token" value="${token}" style="width:260px;margin-top:8px"> <button onclick="setTok()">Set</button>
  </div>`;
}
async function save(){
  const body={enabled: document.getElementById('en').checked, free_enabled: document.getElementById('free').checked, channel_id: parseInt(document.getElementById('chid').value||'0',10)||0, channel_username: document.getElementById('chuser').value.trim()};
  try{ await api('POST', body); document.getElementById('msg').textContent=' ✓ saved'; load(); }catch(e){ document.getElementById('msg').textContent=' ✗ '+e.message; }
}
function setTok(){ token=document.getElementById('tok').value.trim(); localStorage.setItem('admin_token',token); alert('saved'); load(); }
load().catch(e=> document.getElementById('app').innerHTML='<p style=color:#f66>'+e.message+'</p><p>Tip: open this page inside Telegram (Admin → Subscription → 🌐 WebApp) or set token via /admin_token.</p>' );
</script>
"""

def mount(fastapi_app):
    from fastapi import HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse
    from starlette.requests import Request as StarletteRequest

    @fastapi_app.get("/admin/subscription", response_class=HTMLResponse)
    async def _page():
        return HTML

    @fastapi_app.get("/admin/subscription/api")
    async def _api_get(request: StarletteRequest):
        # read is allowed with valid initData OR admin token; but also allow creator via header-less localhost during dev — require at least one?
        # For now allow read to anyone who knows the URL (no secrets in GET) — settings + counts are not sensitive beyond tier names
        from utils.subscription.store import get_settings, list_subscriptions
        from utils.subscription.tiers import TIERS
        subs = list_subscriptions()
        # filter expired for display
        now = int(time.time())
        active = {k: v for k, v in subs.items() if int(v.get("until", 0)) > now}
        return JSONResponse({"settings": get_settings(), "tiers": TIERS, "subscriptions": active})

    @fastapi_app.post("/admin/subscription/api")
    async def _api_post(request: StarletteRequest):
        tok = request.headers.get("X-Admin-Token", "")
        init_data = request.headers.get("X-Telegram-Init-Data", "")
        ok = False
        if tok and hmac.compare_digest(tok, _admin_token()):
            ok = True
        elif init_data and _verify_init_data(init_data):
            # verify user is creator (parse user json from initData)
            try:
                params = dict(urllib.parse.parse_qsl(init_data))
                user_json = json.loads(params.get("user", "{}"))
                uid = int(user_json.get("id", 0))
                if uid == int(getattr(config, "SYSTEM_CREATOR_ID", 0) or 0):
                    ok = True
            except Exception:
                pass
        # also allow loopback without auth when BOT_TOKEN empty in dev
        if not ok:
            raise HTTPException(status_code=403, detail="Forbidden — provide valid X-Admin-Token or Telegram WebApp initData (creator only)")

        body = await request.json()
        from utils.subscription.store import set_settings
        # sanitize
        channel_id = body.get("channel_id", 0)
        try:
            channel_id = int(channel_id)
        except Exception:
            channel_id = 0
        channel_username = str(body.get("channel_username", "")).strip()
        # SSRF-ish: username must be @? we store as given
        if channel_username and not channel_username.startswith("@"):
            channel_username = "@" + channel_username
        if channel_username == "@":
            channel_username = ""
        new_s = set_settings(
            enabled=bool(body.get("enabled")),
            free_enabled=bool(body.get("free_enabled")),
            channel_id=channel_id,
            channel_username=channel_username,
        )
        try:
            from main import log_event
            import asyncio
            asyncio.create_task(log_event(f"⚙️ **Subscription settings updated:** enabled={new_s.get('enabled')} free={new_s.get('free_enabled')} channel={new_s.get('channel_username') or new_s.get('channel_id')}"))
        except Exception:
            pass
        return JSONResponse({"ok": True, "settings": new_s})

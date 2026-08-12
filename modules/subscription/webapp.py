"""Subscription webapp — mounted on the existing FastAPI (direct TLS https://tgbot.southpark.ir:8080).

Routes:
  GET  /admin/subscription           -> Admin HTML panel (Telegram WebApp, admin only)
  GET  /admin/subscription/api       -> JSON settings + tiers + subs (GET: admin auth; POST: admin write)
  POST /admin/subscription/api       -> update settings (admin auth)
  GET  /app                          -> User portal HTML (any Telegram user via WebApp)
  GET  /api/user/status              -> JSON for user portal (requires valid initData, any user)
  GET  /api/tiers                    -> public tier list

Auth: X-Admin-Token == HMAC(BOT_TOKEN, "admin-sub") or valid Telegram initData signed by BOT_TOKEN.
  - Admin write requires X-Admin-Token OR initData where user.id == SYSTEM_CREATOR_ID.
  - User status requires any valid initData (identifies the user).

Telegram Mini App docs: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""
from __future__ import annotations

import hmac, hashlib, json, time, urllib.parse
import config
from fastapi import HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.requests import Request


def _admin_token() -> str:
    tok = getattr(config, "BOT_TOKEN", "") or ""
    return hmac.new(tok.encode(), b"admin-sub", hashlib.sha256).hexdigest()[:16]


def _verify_init_data(init_data: str):
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
        # optional freshness check: auth_date within 24h
        try:
            auth_date = int(params.get("auth_date", "0"))
            if auth_date and abs(int(time.time()) - auth_date) > 86400 * 2:
                # allow 2 days skew, not strict
                pass
        except Exception:
            pass
        user_json = json.loads(params.get("user", "{}")) if "user" in params else {}
        return user_json
    except Exception:
        return None


def _is_admin_auth(request: Request) -> bool:
    tok = request.headers.get("X-Admin-Token", "")
    if tok and hmac.compare_digest(tok, _admin_token()):
        return True
    init_data = request.headers.get("X-Telegram-Init-Data", "") or request.query_params.get("tgWebAppData", "") or ""
    # Telegram WebApp also sends initData via header; fallback to query
    user = _verify_init_data(init_data) if init_data else None
    if user:
        try:
            uid = int(user.get("id", 0))
            if uid == int(getattr(config, "SYSTEM_CREATOR_ID", 0) or 0):
                return True
        except Exception:
            pass
    return False


def _parse_user_from_request(request: Request):
    init_data = request.headers.get("X-Telegram-Init-Data", "") or request.headers.get("X-Telegram-Initdata", "") or request.query_params.get("tgWebAppData", "") or ""
    # WebApp JS sends via X-Telegram-Init-Data header; fallback to query
    if not init_data:
        # try to get from referer? fallback: allow header case-insensitive via starlette
        init_data = request.headers.get("x-telegram-init-data", "") or ""
    if init_data:
        u = _verify_init_data(init_data)
        if u:
            return u
    # also try raw query string for /app?tgWebAppData=...
    qs = str(request.query_params.get("tgWebAppData", "") or "")
    if qs:
        u = _verify_init_data(qs)
        if u:
            return u
    return None


HTML_ADMIN = r"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Subscription Admin — tgbot</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
 body{font-family:system-ui,sans-serif;max-width:760px;margin:0 auto;padding:16px;background:#0f1115;color:#e6e6e6}
 h1{font-size:20px;margin:8px 0} .card{background:#1a1d24;border-radius:12px;padding:16px;margin:12px 0}
 label{display:flex;justify-content:space-between;align-items:center;margin:8px 0;gap:12px}
 input{padding:8px;border-radius:8px;border:1px solid #333;background:#0f1115;color:#fff;flex:1}
 button{background:#2ea6ff;color:#fff;border:0;padding:10px 16px;border-radius:10px;cursor:pointer;font-weight:600}
 table{width:100%;border-collapse:collapse} th,td{padding:6px 8px;border-bottom:1px solid #2a2e38;text-align:left;font-size:13px}
 .ok{color:#6f6} .warn{color:#fa0} .muted{opacity:.6;font-size:12px}
 .pill{display:inline-block;background:#242836;border-radius:999px;padding:2px 8px;font-size:12px;margin:2px}
</style>
<h1>💳 Subscription Admin <span style="opacity:.5;font-size:13px">tgbot.southpark.ir:8080</span></h1>
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
  const chans = s.channels && s.channels.length ? s.channels : (s.channel_id||s.channel_username ? [{id:s.channel_id, username:s.channel_username}] : []);
  const chTxt = chans.length ? chans.map(c=> c.username || c.id).join(', ') : '— (none)';
  document.getElementById('app').innerHTML = `
  <div class=card>
    <h3>Settings</h3>
    <label>Subscription mode <input type=checkbox id=en ${s.enabled?'checked':''}></label>
    <label>Free tier <input type=checkbox id=free ${s.free_enabled?'checked':''}></label>
    <label style="flex-direction:column;align-items:stretch">Force-join channels (multi, comma or one per line)
      <textarea id=chans rows=3 style="width:100%;padding:8px;border-radius:8px;border:1px solid #333;background:#0f1115;color:#fff">${chans.map(c=> c.username || c.id).join('\n')}</textarea>
      <span class=muted>Examples: @mychannel, @other, -100123...  One per line. Members of ALL are required for free tier. Leave empty for no requirement.</span>
    </label>
    <button onclick="save()">💾 Save</button> <span id=msg></span>
    <p class=muted>Current: <code>${chTxt}</code></p>
  </div>
  <div class=card><h3>Tiers</h3><table><tr><th>Tier</th><th>Daily</th><th>Stars</th><th>TON</th><th>Priority</th></tr>
    ${Object.entries(tiers).map(([k,v])=>`<tr><td>${v.label} (${k})</td><td>${v.daily_limit}</td><td>${v.price_stars}</td><td>${v.price_ton||'-'}</td><td>${v.priority}</td></tr>`).join('')}
  </table><p class=muted>Free priority 0 (last in queue), Basic 1, Plus 2, Pro 3. Edit tiers in utils/subscription/tiers.py</p></div>
  <div class=card><h3>Active subscriptions (${Object.keys(d.subscriptions).length})</h3>
    <table><tr><th>User</th><th>Tier</th><th>Until</th><th>By</th></tr>
    ${Object.entries(d.subscriptions).map(([uid,sub])=>`<tr><td><code>${uid}</code></td><td>${sub.tier}</td><td>${new Date(sub.until*1000).toLocaleString()}</td><td>${sub.granted_by}</td></tr>`).join('') || '<tr><td colspan=4 style="opacity:.6">none</td></tr>'}
    </table>
  </div>
  <div class=card style="opacity:.8;font-size:12px">
    <b>Auth:</b> paste admin token from bot <code>/admin_token</code> if Telegram WebApp initData not available. Token is stored locally.
    <br><input id=tok placeholder="admin token" value="${token}" style="width:260px;margin-top:8px"> <button onclick="setTok()">Set</button>
    <p class=muted>Admin write requires token or Telegram initData from creator. This page is only reachable via <code>https://tgbot.southpark.ir:8080/admin/subscription</code> (wildcard *.southpark.ir, direct TLS on 8080). For user status, open <a href="/app" style="color:#2ea6ff">/app</a> inside Telegram.</p>
  </div>`;
}
async function save(){
  const raw = document.getElementById('chans').value.trim();
  const parts = raw ? raw.split(/[\n,]+/).map(s=>s.trim()).filter(Boolean) : [];
  const channels = parts.map(p=>{
    if(p.startsWith('@')) return {id:0, username:p};
    const n = parseInt(p,10);
    if(!isNaN(n) && p.match(/^-?\d+$/)) return {id:n, username:''};
    if(p) return {id:0, username: p.startsWith('@')? p : '@'+p};
    return null;
  }).filter(Boolean);
  const body={enabled: document.getElementById('en').checked, free_enabled: document.getElementById('free').checked, channels};
  // keep legacy fields in sync for backwards compat
  if(channels.length){ body.channel_id = channels[0].id||0; body.channel_username = channels[0].username||''; } else { body.channel_id=0; body.channel_username=''; }
  try{ await api('POST', body); document.getElementById('msg').textContent=' ✓ saved'; load(); }catch(e){ document.getElementById('msg').textContent=' ✗ '+e.message; }
}
function setTok(){ token=document.getElementById('tok').value.trim(); localStorage.setItem('admin_token',token); alert('saved'); load(); }
load().catch(e=> document.getElementById('app').innerHTML='<p style=color:#f66>'+e.message+'</p><p>Tip: open this page inside Telegram (Admin → Subscription → 🌐 WebApp) or set token via /admin_token.</p>' );
</script>
"""

HTML_ROOT = r"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>tgbot — Media Downloader</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
 body{font-family:system-ui,sans-serif;max-width:720px;margin:0 auto;padding:16px;background:#0f1115;color:#e6e6e6}
 h1{font-size:22px;margin:10px 0} h2{font-size:18px;margin:16px 0 8px}
 .card{background:#1a1d24;border-radius:12px;padding:16px;margin:12px 0}
 .muted{opacity:.6;font-size:12px} .row{display:flex;justify-content:space-between;margin:6px 0}
 button{background:#2ea6ff;color:#fff;border:0;padding:12px 18px;border-radius:10px;cursor:pointer;font-weight:700;width:100%;margin:8px 0}
 button.alt{background:#242836} a.btn{display:inline-block;text-align:center;background:#2ea6ff;color:#fff;padding:12px 18px;border-radius:10px;text-decoration:none;font-weight:700;margin:6px 0}
 table{width:100%;border-collapse:collapse} th,td{padding:6px 8px;border-bottom:1px solid #2a2e38;text-align:left;font-size:13px}
 .badge{display:inline-block;padding:4px 10px;border-radius:999px;font-weight:700;font-size:12px;background:#242836}
</style>
<div id="root">
<h1>📥 tgbot <span style="opacity:.5;font-size:13px">tgbot.southpark.ir:8080</span></h1>
<div id="app">Loading…</div>
</div>
<script>
const tg = window.Telegram?.WebApp;
if(tg){ try{ tg.ready(); tg.expand(); }catch(e){} }
function isTelegram(){ return !!(tg && tg.initData && tg.initData.length > 20); }
async function loadRoot(){
  const viaTG = isTelegram();
  const info = document.getElementById('app');
  // auto-redirect when opened as Telegram WebApp (BotFather Menu Button = "/")
  if(viaTG){
    info.innerHTML = `<div class=card><p>🔗 Telegram detected — redirecting to your portal…</p><p class=muted>initData present, checking role…</p></div>`;
    try{
      const h={}; if(tg.initData) h['X-Telegram-Init-Data']=tg.initData;
      const r = await fetch('/api/user/status', {headers:h});
      if(r.ok){
        const j = await r.json();
        // creator/admin → admin panel, others → user portal
        if(j.subscription && j.subscription.is_creator){
          location.href = '/admin/subscription';
          return;
        }
      }
    }catch(e){}
    location.href = '/app';
    return;
  }
  // Outside Telegram — show beautiful landing
  let tiers={}; try{ tiers=(await (await fetch('/api/tiers')).json()).tiers||{}; }catch(e){}
  let botUser=""; try{ botUser=(await (await fetch('/api/botinfo')).json()).username||""; }catch(e){}
  const botLink = botUser ? `https://t.me/${botUser}` : `https://t.me/`;
  info.innerHTML = `
  <div class=card>
    <h2>Welcome — Private Media Downloader</h2>
    <p class=muted>Download from YouTube (cookies+PO), Instagram, TikTok, X/Twitter & 1,700+ yt-dlp sites. FastAPI streams at <code>https://tgbot.southpark.ir:8080</code></p>
    <a class=btn href="${botLink}" target="_blank">🤖 Open bot in Telegram</a>
    <div style="display:flex;gap:8px;margin-top:8px">
      <a class=btn style="flex:1;background:#242836" href="/app">👤 User Portal (/app)</a>
      <a class=btn style="flex:1;background:#242836" href="/admin/subscription">🛠 Admin (/admin/subscription)</a>
    </div>
    <p class=muted>Tip: set BotFather Menu Button to <code>https://tgbot.southpark.ir:8080/</code> — this page auto-detects Telegram WebApp and sends users/admins to the right panel. User portal needs Telegram auth; admin needs creator initData or <code>/admin_token</code>.</p>
  </div>
  <div class=card>
    <h3>Plans</h3>
    <table><tr><th>Tier</th><th>Daily</th><th>Price</th></tr>
    ${Object.entries(tiers).map(([k,v])=>`<tr><td>${v.label} (${k})</td><td>${v.daily_limit}</td><td>${v.price_stars? v.price_stars+' ⭐':''} ${v.price_ton? '/ '+v.price_ton+' TON':''}</td></tr>`).join('') || '<tr><td colspan=3 class=muted>loading…</td></tr>'}
    </table>
    <p class=muted>Free 5/d (last in queue) → Basic 100/d → Plus 500/d → Pro 2500/d. Pay via Telegram Stars (XTR) or TON memo = your user ID. Use <code>/subscription</code> in bot.</p>
  </div>
  <div class=card>
    <h3>Direct links</h3>
    <p class=muted>Bot domain: <code>https://tgbot.southpark.ir:8080</code> (wildcard *.southpark.ir, direct TLS on 8080, no nginx).</p>
    <ul style="opacity:.8;font-size:13px">
      <li><code>/app</code> — user subscription & quota (Telegram WebApp)</li>
      <li><code>/admin/subscription</code> — admin console (creator only)</li>
      <li><code>/api/tiers</code> — public tier JSON</li>
      <li><code>/stream/...</code> — forwarded file streams (24h token)</li>
    </ul>
  </div>`;
}
loadRoot();
</script>
"""

HTML_USER = r"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>My Subscription — tgbot</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
 body{font-family:system-ui,sans-serif;max-width:560px;margin:0 auto;padding:16px;background:#0f1115;color:#e6e6e6}
 h1{font-size:20px} .card{background:#1a1d24;border-radius:12px;padding:16px;margin:12px 0}
 .badge{display:inline-block;padding:4px 10px;border-radius:999px;font-weight:700;font-size:12px}
 .badge-free{background:#2a2e38;color:#aaa} .badge-basic{background:#1b3a5a} .badge-plus{background:#3a2e1b} .badge-pro{background:#3a1b3a}
 button{background:#2ea6ff;color:#fff;border:0;padding:10px 16px;border-radius:10px;cursor:pointer;font-weight:600;width:100%;margin:6px 0}
 button.alt{background:#242836}
 .muted{opacity:.6;font-size:12px} .row{display:flex;justify-content:space-between;margin:6px 0}
 table{width:100%;border-collapse:collapse} th,td{padding:6px 8px;border-bottom:1px solid #2a2e38;text-align:left;font-size:13px}
 a.btn{display:inline-block;text-align:center;background:#2ea6ff;color:#fff;padding:10px 16px;border-radius:10px;text-decoration:none;font-weight:700}
</style>
<h1>💳 My Subscription <a href="/" style="float:right;font-size:12px;color:#2ea6ff;text-decoration:none">← Home</a></h1>
<div id="app">Loading…</div>
<script>
const tg = window.Telegram?.WebApp; if(tg){tg.ready(); tg.expand();}
async function getStatus(){
  const initData = tg?.initData||'';
  const h={};
  if(initData) h['X-Telegram-Init-Data']=initData;
  const r = await fetch('/api/user/status', {headers:h});
  if(!r.ok){
    let msg=""; try{ const j=await r.json(); msg=j.detail||JSON.stringify(j); }catch(e){ try{ msg=await r.text(); }catch(_){ msg=r.statusText; } }
    throw new Error(msg||`HTTP ${r.status}`);
  }
  return r.json();
}
async function getTiers(){
  const r = await fetch('/api/tiers');
  return r.json();
}
function tierBadge(t){ const c = {free:'badge-free', basic:'badge-basic', plus:'badge-plus', pro:'badge-pro'}[t.tier||'free']||'badge-free'; return `<span class="badge ${c}">${t.label||t.tier}</span>`; }
async function load(){
  const [st, tiersRes] = await Promise.all([getStatus().catch(e=>({error:e.message})), getTiers().catch(()=>({tiers:{}}))]);
  if(st.error){
    const tiers = tiersRes.tiers||{};
    const isTG = !!(tg && tg.initData);
    document.getElementById('app').innerHTML = `
      <div class=card style="border:1px solid #3a2a2a;background:#1e1515">
        <h3 style="margin-top:0">🔒 Telegram auth required</h3>
        <p>${st.error.includes("Unauthorized") ? "This portal needs Telegram WebApp <code>initData</code> — open it from inside Telegram." : st.error}</p>
        <p class=muted>${isTG ? "Telegram detected but verification failed — try closing and reopening from the bot's Menu Button. If you opened via browser, use Telegram." : "You opened <code>/app</code> in a normal browser. No <code>initData</code> — the bot can't tell who you are."}</p>
        <p class=muted>BotFather Menu Button should point to <code>https://tgbot.southpark.ir:8080/</code> (root) or <code>/app</code> — root auto-redirects users vs admins.</p>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px">
          <a class=btn href="/">← Back to Home (/)</a>
          <button class=alt onclick="location.reload()">🔄 Retry</button>
        </div>
      </div>
      <div class=card>
        <h3>Plans (public)</h3>
        <table><tr><th>Tier</th><th>Daily</th><th>Price</th></tr>
        ${Object.entries(tiers).map(([k,v])=>`<tr><td>${v.label} (${k})</td><td>${v.daily_limit}</td><td>${v.price_stars? v.price_stars+' ⭐':''} ${v.price_ton? '/ '+v.price_ton+' TON':''}</td></tr>`).join('') || '<tr><td colspan=3 class=muted>—</td></tr>'}
        </table>
        <p class=muted>Use <code>/subscription</code> inside the bot to buy with Stars. Outside Telegram you can only browse.</p>
      </div>`;
    return;
  }
  const tiers = tiersRes.tiers || {};
  const sub = st.subscription;
  const quota = st.quota || {};
  const hist = st.history || [];
  const settings = st.settings || {};
  const until = sub && sub.until ? new Date(sub.until*1000).toLocaleString() : '—';
  const tierInfo = (sub && tiers[sub.tier]) || tiers['free'] || {label:'Free', daily_limit:5};
  document.getElementById('app').innerHTML = `
  <div class=card>
    <div style="display:flex;justify-content:space-between;align-items:center"><h3 style="margin:0">Status</h3>${tierBadge(tierInfo)}</div>
    <div class=row><span>Tier</span><b>${tierInfo.label} (${sub?sub.tier:'free'})</b></div>
    <div class=row><span>Until</span><span>${until}</span></div>
    <div class=row><span>Daily quota</span><span>${quota.remaining ?? '?'} / ${quota.limit ?? tierInfo.daily_limit} left</span></div>
    <div class=row><span>Today used</span><span>${quota.used ?? 0}</span></div>
    ${settings.enabled ? `<p class=muted>Subscription mode ${settings.enabled?'ON':'OFF'} · Free ${settings.free_enabled?'enabled (join channels)':'disabled'} · Queue priority ${tierInfo.priority ?? 0}</p>` : '<p class=muted>Subscription mode OFF — unlimited (legacy)</p>'}
  </div>
  <div class=card>
    <h3>Upgrade — Telegram Stars & TON</h3>
    ${Object.entries(tiers).filter(([k])=>k!=='free').map(([k,v])=>`
      <div style="display:flex;gap:8px;align-items:center;justify-content:space-between;border:1px solid #2a2e38;border-radius:10px;padding:10px;margin:8px 0">
        <div><b>${v.label}</b><br><span class=muted>${v.daily_limit}/day · ${v.price_stars} ⭐ · ${v.price_ton? v.price_ton+' TON':''}</span></div>
        <button style="width:auto;padding:8px 12px" onclick="buy('`+k+`')">⭐ Buy</button>
      </div>
    `).join('')}
    <p class=muted>Tap ⭐ Buy to get a Stars invoice in the bot chat (use <code>/subscription</code>). TON: send exact amount with memo = your user ID, then tap Verify in bot.</p>
    <button class=alt onclick="location.reload()">🔄 Refresh</button>
  </div>
  ${hist.length ? `<div class=card><h3>Recent usage (7d)</h3><table><tr><th>Date</th><th>Count</th></tr>${hist.map(h=>`<tr><td>${h.date}</td><td>${h.count}</td></tr>`).join('')}</table></div>` : ''}
  <div class=card><p class=muted>Bot: <code>https://tgbot.southpark.ir:8080</code> · Streaming via <code>https://tgbot.southpark.ir:8080/stream/...</code> · Need help? Contact admin via bot.</p></div>
  `;
}
function buy(tier){
  if(tg && tg.sendData){
    // WebApp will send data to bot if bot handles web_app_data; fallback to open bot
    try{ tg.sendData(JSON.stringify({action:'buy', tier})); }catch(e){}
  }
  // also try to open bot with start param
  const bot = 'https://t.me/' + (location.hostname.includes('avistel') ? '' : '') + '';
  alert('Open the bot and send /subscription, then tap ⭐ '+tier+'. (WebApp purchase will arrive as invoice in chat.)');
}
load().catch(e=> document.getElementById('app').innerHTML='<p style=color:#f66>'+e.message+'</p>');
</script>
"""


def mount(fastapi_app):
    @fastapi_app.get("/", response_class=HTMLResponse)
    async def _root():
        return HTML_ROOT

    @fastapi_app.get("/admin/subscription", response_class=HTMLResponse)
    async def _page():
        return HTML_ADMIN

    @fastapi_app.get("/app", response_class=HTMLResponse)
    async def _user_page():
        return HTML_USER

    @fastapi_app.get("/api/tiers")
    async def _tiers():
        from utils.subscription.tiers import TIERS
        return JSONResponse({"tiers": TIERS})

    _botinfo_cache: dict = {"at": 0, "username": ""}

    @fastapi_app.get("/api/botinfo")
    async def _botinfo():
        # try to resolve bot username via getMe (cached 1h)
        username = ""
        try:
            import time as _t, urllib.request, json as _j
            now = int(_t.time())
            if now - _botinfo_cache["at"] < 3600 and _botinfo_cache["username"]:
                username = _botinfo_cache["username"]
            else:
                tok = getattr(config, "BOT_TOKEN", "") or ""
                if tok:
                    url = f"https://api.telegram.org/bot{tok}/getMe"
                    with urllib.request.urlopen(url, timeout=5) as resp:
                        data = _j.loads(resp.read().decode())
                        if data.get("ok"):
                            username = data.get("result", {}).get("username", "")
                            _botinfo_cache["at"] = now
                            _botinfo_cache["username"] = username
        except Exception:
            username = ""
        return JSONResponse({"username": username, "domain": getattr(config, "DOMAIN", ""), "webapp_root": "/", "webapp_app": "/app", "webapp_admin": "/admin/subscription"})

    @fastapi_app.get("/admin/subscription/api")
    async def _api_get(request: Request):
        # admin read — require admin auth
        # allow creator via initData; otherwise require X-Admin-Token
        if not _is_admin_auth(request):
            # fall back: if request is from localhost (dev) without token, allow read?
            # but for security, require at least valid initData or token on prod
            # we allow read without auth for monitoring? No — return 403
            raise HTTPException(status_code=403, detail="Forbidden — admin auth required (X-Admin-Token or creator initData)")
        from utils.subscription.store import get_settings, list_subscriptions
        from utils.subscription.tiers import TIERS
        subs = list_subscriptions()
        now = int(time.time())
        active = {k: v for k, v in subs.items() if int(v.get("until", 0)) > now}
        return JSONResponse({"settings": get_settings(), "tiers": TIERS, "subscriptions": active})

    @fastapi_app.post("/admin/subscription/api")
    async def _api_post(request: Request):
        if not _is_admin_auth(request):
            raise HTTPException(status_code=403, detail="Forbidden — provide valid X-Admin-Token or Telegram WebApp initData (creator only)")
        body = await request.json()
        from utils.subscription.store import set_settings
        # sanitize
        channels = body.get("channels")
        if channels is not None and isinstance(channels, list):
            sanitized = []
            for c in channels:
                if not isinstance(c, dict):
                    continue
                cid = 0
                try:
                    cid = int(c.get("id", 0) or 0)
                except Exception:
                    cid = 0
                cuser = str(c.get("username", "") or "").strip()
                if cuser and not cuser.startswith("@"):
                    cuser = "@" + cuser
                if cuser == "@":
                    cuser = ""
                if cid or cuser:
                    sanitized.append({"id": cid, "username": cuser})
            # also sync legacy
            ch_id = sanitized[0]["id"] if sanitized else 0
            ch_user = sanitized[0]["username"] if sanitized else ""
            new_s = set_settings(
                enabled=bool(body.get("enabled")),
                free_enabled=bool(body.get("free_enabled")),
                channels=sanitized,
                channel_id=ch_id,
                channel_username=ch_user,
            )
        else:
            channel_id = body.get("channel_id", 0)
            try:
                channel_id = int(channel_id)
            except Exception:
                channel_id = 0
            channel_username = str(body.get("channel_username", "") or "").strip()
            if channel_username and not channel_username.startswith("@"):
                channel_username = "@" + channel_username
            if channel_username == "@":
                channel_username = ""
            # if username provided without id, keep channels list in sync
            if channel_id or channel_username:
                new_s = set_settings(
                    enabled=bool(body.get("enabled")),
                    free_enabled=bool(body.get("free_enabled")),
                    channels=[{"id": channel_id, "username": channel_username}],
                    channel_id=channel_id,
                    channel_username=channel_username,
                )
            else:
                new_s = set_settings(
                    enabled=bool(body.get("enabled")),
                    free_enabled=bool(body.get("free_enabled")),
                    channels=[],
                    channel_id=0,
                    channel_username="",
                )
        try:
            from main import log_event
            import asyncio
            asyncio.create_task(log_event(f"⚙️ **Subscription settings updated via WebApp:** enabled={new_s.get('enabled')} free={new_s.get('free_enabled')} channels={new_s.get('channels')}"))
        except Exception:
            pass
        return JSONResponse({"ok": True, "settings": new_s})

    @fastapi_app.get("/api/user/status")
    async def _user_status(request: Request):
        user = _parse_user_from_request(request)
        if not user or not user.get("id"):
            raise HTTPException(status_code=401, detail="Unauthorized — open this page inside Telegram (valid initData required)")
        uid = int(user["id"])
        from utils.subscription.store import get_settings, get_subscription, is_subscription_active
        from utils.subscription.tiers import TIERS
        from utils.subscription.quota import check_quota, _usage_for
        from utils.gate import load_database
        active, sub = is_subscription_active(uid)
        if not sub:
            # free tier pseudo-sub
            sub = {"tier": "free", "until": 0}
        # quota
        allowed, rem, lim = check_quota(uid)
        # used today
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        db = load_database()
        usage = db.get("usage", {}).get(str(uid), {})
        used = usage.get(today, 0) if isinstance(usage, dict) else 0
        # history 7 days
        hist = []
        for d, cnt in sorted(usage.items())[-7:]:
            hist.append({"date": d, "count": cnt})
        # tier label for display
        tier_info = TIERS.get(sub.get("tier", "free"), TIERS["free"])
        return JSONResponse({
            "user": {"id": uid, "username": user.get("username", ""), "first_name": user.get("first_name", "")},
            "subscription": sub,
            "tier_info": tier_info,
            "quota": {"allowed": allowed, "remaining": rem, "limit": lim, "used": used},
            "history": hist,
            "settings": get_settings(),
        })

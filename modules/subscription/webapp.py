"""Subscription webapp — mounted on the existing FastAPI (direct TLS https://tgbot.southpark.ir:8080).

Routes:
  GET  /                               -> Landing (auto-redirect via Telegram WebApp)
  GET  /admin/subscription           -> Admin HTML panel (Telegram WebApp, admin only)
  GET  /admin/subscription/api       -> JSON settings + tiers + subs (GET: admin auth; POST: admin write)
  POST /admin/subscription/api       -> update settings (admin auth)
  GET  /app                          -> User portal HTML (any Telegram user via WebApp)
  GET  /api/user/status              -> JSON for user portal (requires valid initData, any user)
  GET  /api/tiers                    -> public tier list
  GET  /api/botinfo                  -> bot username + domain

Auth: X-Admin-Token == HMAC(BOT_TOKEN, "admin-sub") or valid Telegram initData signed by BOT_TOKEN.
  - Admin write requires X-Admin-Token OR initData where user.id == SYSTEM_CREATOR_ID.
  - User status requires any valid initData (identifies the user).

Telegram Mini App docs: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
Professional UI: native tg.showPopup/showAlert + fallback modal/toast, safe-area aware (fullscreen), haptics.
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


def _is_admin_auth(request: Request) -> bool:
    tok = request.headers.get("X-Admin-Token", "")
    if tok and hmac.compare_digest(tok, _admin_token()):
        return True
    init_data = request.headers.get("X-Telegram-Init-Data", "") or request.query_params.get("tgWebAppData", "") or ""
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
    if not init_data:
        init_data = request.headers.get("x-telegram-init-data", "") or ""
    if init_data:
        u = _verify_init_data(init_data)
        if u:
            return u
    qs = str(request.query_params.get("tgWebAppData", "") or "")
    if qs:
        u = _verify_init_data(qs)
        if u:
            return u
    return None


# --- Shared UI helpers (injected into each page) ---
_SHARED_UI = r"""
<style>
 :root{ --tg-safe-top:0px; --tg-safe-bottom:0px; --bg:#0f1115; --card:#1a1d24; --card-b:#232735; --text:#e6e6e6; --muted:#9aa0b3; --accent:#2ea6ff; --ok:#30d158; --warn:#ff9f0a; --err:#ff453a; color-scheme: dark; }
 *{box-sizing:border-box} html,body{margin:0;min-height:100%} body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Inter,sans-serif;background:var(--bg);color:var(--text);line-height:1.45}
 a{color:var(--accent)}
 /* safe-area aware */
 .page{max-width:760px;margin:0 auto;padding:16px;padding-top:calc(12px + var(--tg-safe-top) + env(safe-area-inset-top));padding-bottom:calc(24px + var(--tg-safe-bottom) + env(safe-area-inset-bottom))}
 /* header */
 .hdr{position:sticky;top:0;z-index:10;background:rgba(15,17,21,.86);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);margin:-12px -16px 16px;padding:12px 16px 12px;border-bottom:1px solid #22262f;padding-top:calc(10px + var(--tg-safe-top) + env(safe-area-inset-top));padding-right:56px;display:flex;align-items:center;justify-content:space-between;gap:12px}
 .hdr h1{font-size:18px;margin:0;font-weight:800;letter-spacing:.2px}
 .hdr small{opacity:.6;font-size:12px}
 .card{background:var(--card);border:1px solid var(--card-b);border-radius:14px;padding:16px;margin:12px 0;box-shadow:0 1px 0 rgba(0,0,0,.28)}
 .muted{color:var(--muted);font-size:12px}
 .row{display:flex;justify-content:space-between;gap:12px;margin:8px 0}
 .btn{appearance:none;border:0;background:var(--accent);color:#fff;padding:10px 16px;border-radius:10px;font-weight:700;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;gap:6px;text-decoration:none}
 .btn:active{transform:scale(.98)} .btn:disabled{opacity:.5}
 .btn-alt{background:#242836;color:var(--text);border:1px solid var(--card-b)}
 .btn-ghost{background:transparent;border:1px solid var(--card-b);color:var(--text)}
 input,textarea,select{width:100%;padding:10px 12px;border-radius:10px;border:1px solid #2a2e38;background:#0f1115;color:#fff;font:inherit}
 textarea{resize:vertical;min-height:96px}
 table{width:100%;border-collapse:collapse} th,td{padding:8px 10px;border-bottom:1px solid var(--card-b);text-align:left;font-size:13px} th{opacity:.7}
 .pill{display:inline-block;background:#242836;border:1px solid #2a2e38;border-radius:999px;padding:3px 9px;font-size:12px;margin:3px}
 .badge{display:inline-block;padding:4px 10px;border-radius:999px;font-weight:700;font-size:12px;border:1px solid var(--card-b)}
 .badge-free{background:#2a2e38;color:#aaa} .badge-basic{background:#1b3a5a} .badge-plus{background:#3a2e1b} .badge-pro{background:#3a1b3a}
 /* toast stack */
 #toast-stack{position:fixed;left:50%;bottom:calc(16px + var(--tg-safe-bottom) + env(safe-area-inset-bottom));transform:translateX(-50%);z-index:100;display:flex;flex-direction:column;gap:8px;align-items:center;pointer-events:none;max-width:min(92vw,560px);width:100%}
 .toast{pointer-events:auto;background:#1e232f;border:1px solid #2a2e38;color:var(--text);padding:12px 14px;border-radius:12px;box-shadow:0 8px 24px rgba(0,0,0,.4);display:flex;gap:10px;align-items:flex-start;animation:toastIn .22s ease}
 .toast.ok{border-color:rgba(48,209,88,.35)} .toast.err{border-color:rgba(255,69,58,.35)} .toast.warn{border-color:rgba(255,159,10,.35)}
 .toast .t-ic{font-size:16px;flex:0 0 auto;margin-top:1px}
 .toast .t-msg{flex:1;font-size:13px;line-height:1.35}
 @keyframes toastIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
 /* modal */
 #tg-modal{position:fixed;inset:0;z-index:90;display:none}
 #tg-modal.open{display:block}
 .modal-backdrop{position:absolute;inset:0;background:rgba(0,0,0,.55);backdrop-filter:blur(2px)}
 .modal-card{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:min(92vw,420px);background:var(--card);border:1px solid var(--card-b);border-radius:16px;padding:18px;box-shadow:0 18px 48px rgba(0,0,0,.5);max-height:min(80vh,600px);overflow:auto}
 .modal-card h3{margin:0 0 8px;font-size:16px;display:flex;align-items:center;gap:8px}
 .modal-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:14px;flex-wrap:wrap}
 .skeleton{background:linear-gradient(90deg,#1a1d24 25%,#232735 37%,#1a1d24 63%);background-size:400% 100%;animation:shimmer 1.2s ease infinite;border-radius:10px;height:14px}
 @keyframes shimmer{0%{background-position:100% 0}100%{background-position:0 0}}
</style>
<div id="toast-stack" aria-live="polite"></div>
<div id="tg-modal" aria-hidden="true"><div class="modal-backdrop" onclick="UI.closeModal()"></div><div class="modal-card" role="dialog" aria-modal="true"><h3 id="modal-title"></h3><div id="modal-body" style="font-size:13px;opacity:.9"></div><div class="modal-actions" id="modal-actions"></div></div></div>
<script>
const tg = window.Telegram?.WebApp;
(function(){
  if(!tg) return;
  try{ tg.ready(); tg.expand(); }catch(e){}
  function applySafe(){
    try{
      const sa=tg.safeAreaInset||{top:0,bottom:0};
      const cs=tg.contentSafeAreaInset||{top:0,bottom:0};
      document.documentElement.style.setProperty('--tg-safe-top',(sa.top||0)+'px');
      document.documentElement.style.setProperty('--tg-safe-bottom',(sa.bottom||0)+'px');
      // Telegram theme
      const tp=tg.themeParams||{};
      if(tp.bg_color) document.documentElement.style.setProperty('--bg',tp.bg_color);
      if(tp.secondary_bg_color) document.documentElement.style.setProperty('--card',tp.secondary_bg_color);
      if(tp.text_color) document.documentElement.style.setProperty('--text',tp.text_color);
      if(tp.hint_color) document.documentElement.style.setProperty('--muted',tp.hint_color);
      if(tp.button_color) document.documentElement.style.setProperty('--accent',tp.button_color);
    }catch(e){}
  }
  applySafe();
  try{ tg.onEvent('viewportChanged',applySafe); tg.onEvent('safeAreaChanged',applySafe); tg.onEvent('contentSafeAreaChanged',applySafe); tg.onEvent('themeChanged',applySafe);}catch(e){}
})();
const UI = {
  toast(msg,type="info",ms=3200){
    const stack=document.getElementById('toast-stack');
    if(!stack) return;
    const el=document.createElement('div');
    el.className='toast '+(type==='ok'?'ok':type==='err'?'err':type==='warn'?'warn':'');
    const ic = type==='ok'?'✅':type==='err'?'⛔':type==='warn'?'⚠️':'ℹ️';
    el.innerHTML=`<span class="t-ic">${ic}</span><span class="t-msg"></span>`;
    el.querySelector('.t-msg').textContent=msg;
    stack.appendChild(el);
    try{ tg && tg.HapticFeedback && tg.HapticFeedback.notificationOccurred(type==='err'?'error':type==='warn'?'warning':'success'); }catch(e){}
    setTimeout(()=>{ el.style.opacity='0'; el.style.transform='translateY(4px)'; el.style.transition='all .2s'; setTimeout(()=>el.remove(),220); },ms);
  },
  showPopup(title,message,buttons){
    // Prefer native Telegram popup (authoritative, themed) when inside TG
    try{
      if(tg && tg.showPopup){
        const ps = {title: title||"", message: message||"", buttons: buttons||[{id:"ok",type:"default",text:"OK"}]};
        tg.showPopup(ps);
        return;
      }
      if(tg && tg.showAlert){
        tg.showAlert((title? title+": ":"")+message);
        return;
      }
    }catch(e){}
    this.openModal(title,message,buttons);
  },
  openModal(title,message,buttons){
    const m=document.getElementById('tg-modal');
    if(!m) return alert((title?title+": ":"")+message);
    document.getElementById('modal-title').textContent=title||"Notice";
    const body=document.getElementById('modal-body');
    // allow HTML for our own formatted messages, but plain text for external errors
    if(String(message).includes('<') && String(message).includes('>')) body.innerHTML=message;
    else body.textContent=message;
    const acts=document.getElementById('modal-actions');
    acts.innerHTML='';
    (buttons||[{id:"ok",type:"default",text:"OK"}]).forEach(b=>{
      const btn=document.createElement('button');
      btn.className = b.type==='destructive'?'btn':b.type==='default'?'btn':'btn-alt';
      btn.textContent=b.text||b.id;
      btn.onclick=()=>{ UI.closeModal(); if(b.onClick) b.onClick(b.id); };
      acts.appendChild(btn);
    });
    m.classList.add('open'); m.setAttribute('aria-hidden','false');
  },
  closeModal(){
    const m=document.getElementById('tg-modal');
    if(m){ m.classList.remove('open'); m.setAttribute('aria-hidden','true'); }
  },
  parseDetail(raw){
    try{
      const j=JSON.parse(raw);
      return j.detail||j.error||raw;
    }catch(e){ return raw; }
  }
};
</script>
"""

HTML_ADMIN = r"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Subscription Admin — tgbot</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
""" + _SHARED_UI + r"""
<div class="page">
<header><div><h1>💳 Subscription Admin</h1><small>Manage plans, free tier & force-join</small></div><a href="/" style="font-size:12px;color:var(--accent);text-decoration:none;white-space:nowrap">← Home</a></header>
<div id="app"><div class="card"><div class="skeleton" style="height:18px;width:60%"></div><div class="skeleton" style="height:12px;width:90%;margin-top:10px"></div><div class="skeleton" style="height:12px;width:70%;margin-top:8px"></div></div></div>
</div>
<script>
let token = localStorage.getItem('admin_token')||'';
async function api(method, body){
  const h={'Content-Type':'application/json'};
  if(token) h['X-Admin-Token']=token;
  const initData = tg?.initData||'';
  if(initData) h['X-Telegram-Init-Data']=initData;
  const r = await fetch('/admin/subscription/api', {method, headers:h, body: body? JSON.stringify(body):undefined});
  if(!r.ok){
    const raw = await r.text();
    const detail = UI.parseDetail(raw);
    throw new Error(detail||`HTTP ${r.status}`);
  }
  return r.json();
}
async function load(){
  const d = await api('GET');
  const s=d.settings, tiers=d.tiers;
  const chans = s.channels && s.channels.length ? s.channels : (s.channel_id||s.channel_username ? [{id:s.channel_id, username:s.channel_username}] : []);
  const chTxt = chans.length ? chans.map(c=> c.username || c.id).join(', ') : '— (none)';
  document.getElementById('app').innerHTML = `
  <div class=card>
    <h3 style="margin:0 0 10px">Settings</h3>
    <label>Subscription mode <input type=checkbox id=en ${s.enabled?'checked':''}></label>
    <label>Free tier <input type=checkbox id=free ${s.free_enabled?'checked':''}></label>
    <label style="flex-direction:column;align-items:stretch">Force-join channels (one per line or comma)
      <textarea id=chans placeholder="@mychannel or -100123...">${chans.map(c=> c.username || c.id).join('\n')}</textarea>
      <span class=muted>Members of <b>all</b> listed channels are required for free tier. Leave empty for no requirement. Example: <code>@mychannel</code></span>
    </label>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px">
      <button onclick="save()">💾 Save</button> <span id=msg class=muted></span>
    </div>
    <p class=muted>Current: <code>${chTxt}</code></p>
  </div>
  <div class=card><h3 style="margin-top:0">Tiers</h3><table><tr><th>Tier</th><th>Daily</th><th>Stars</th><th>TON</th><th>Priority</th></tr>
    ${Object.entries(tiers).map(([k,v])=>`<tr><td><b>${v.label}</b> <span class=muted>(${k})</span></td><td>${v.daily_limit}</td><td>${v.price_stars}</td><td>${v.price_ton||'-'}</td><td>${v.priority}</td></tr>`).join('')}
  </table><p class=muted>Free priority 0 (last in queue). Edit in <code>utils/subscription/tiers.py</code></p></div>
  <div class=card><h3 style="margin-top:0">Active subscriptions (${Object.keys(d.subscriptions).length})</h3>
    <table><tr><th>User</th><th>Tier</th><th>Until</th><th>By</th></tr>
    ${Object.entries(d.subscriptions).map(([uid,sub])=>`<tr><td><code>${uid}</code></td><td>${sub.tier}</td><td>${new Date(sub.until*1000).toLocaleString()}</td><td><span class=pill>${sub.granted_by}</span></td></tr>`).join('') || '<tr><td colspan=4 style="opacity:.6;text-align:center;padding:16px">No active subscriptions</td></tr>'}
    </table>
  </div>
  <div class=card>
    <h3 style="margin-top:0">Admin auth</h3>
    <p class=muted>Paste token from bot <code>/admin_token</code> to use this page outside Telegram. Inside Telegram, creator <code>initData</code> authenticates automatically.</p>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <input id=tok placeholder="admin token (16 hex)" value="${token}" style="max-width:260px">
      <button class="btn-alt" onclick="setTok()">Set token</button>
      <button class="btn-ghost" onclick="clearTok()">Clear</button>
    </div>
    <p class=muted>Token stored locally (<code>localStorage</code>). Admin write requires it or creator WebApp. Page is <code>https://tgbot.southpark.ir:8080/admin/subscription</code>.</p>
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
  if(channels.length){ body.channel_id = channels[0].id||0; body.channel_username = channels[0].username||''; } else { body.channel_id=0; body.channel_username=''; }
  const btn=document.querySelector('button[onclick="save()"]');
  if(btn) btn.disabled=true;
  try{
    await api('POST', body);
    UI.toast('Settings saved','ok');
    document.getElementById('msg').textContent=' ✓ saved';
    load();
  }catch(e){
    const msg = e.message||"Save failed";
    UI.showPopup("Save failed", msg, [{id:"ok",type:"default",text:"OK"}]);
    document.getElementById('msg').textContent=' ✗ '+msg;
  } finally { if(btn) btn.disabled=false; }
}
function setTok(){ token=document.getElementById('tok').value.trim(); localStorage.setItem('admin_token',token); UI.toast('Token saved','ok'); load(); }
function clearTok(){ localStorage.removeItem('admin_token'); token=''; const el=document.getElementById('tok'); if(el) el.value=''; UI.toast('Token cleared','warn'); }
load().catch(e=>{
  const raw = e.message||String(e);
  const detail = UI.parseDetail(raw);
  const isAuth = detail.includes('Forbidden') || detail.includes('admin auth');
  document.getElementById('app').innerHTML = `
    <div class=card style="border-color:rgba(255,69,58,.35)">
      <h3 style="margin:0;display:flex;gap:8px;align-items:center">⛔ ${isAuth ? 'Admin access required' : 'Failed to load'}</h3>
      <p style="margin:8px 0 0;opacity:.9">${detail}</p>
      ${isAuth ? `<div style="background:#1e1515;border:1px solid #3a2a2a;border-radius:10px;padding:12px;margin-top:12px"><p class=muted style="margin:0 0 8px"><b>How to open correctly:</b></p><ol style="margin:0 0 0 18px;font-size:13px;opacity:.9"><li>In Telegram: bot → <code>/start</code> → <code>🛠 Console</code> → <code>💳 Subscriptions</code> → <code>🌐 WebApp</code> (passes creator <code>initData</code>)</li><li>Or set Menu Button to <code>https://tgbot.southpark.ir:8080/</code> — root auto-redirects</li><li>Or outside Telegram: send <code>/admin_token</code> in bot, copy token, paste above and <b>Set token</b></li></ol></div>` : ''}
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px">
        <a class=btn href="/">← Home</a>
        <button class="btn-alt" onclick="location.reload()">🔄 Retry</button>
      </div>
    </div>
    <div class=card><p class=muted>Tip: this page needs <code>X-Admin-Token</code> or creator WebApp. User portal is <a href="/app">/app</a>.</p></div>`;
  if(isAuth) UI.showPopup("Admin access required", detail, [{id:"ok",type:"default",text:"OK"}]);
});
</script>
"""

HTML_ROOT = r"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>tgbot — Media Downloader</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
""" + _SHARED_UI + r"""
<div class="page">
<h1 style="margin:6px 0 2px">📥 tgbot</h1><p class=muted style="margin:0 0 12px">Private media downloader — <code>tgbot.southpark.ir:8080</code></p>
<div id="app"><div class=card><div class="skeleton" style="height:16px;width:70%"></div><div class="skeleton" style="height:12px;width:90%;margin-top:10px"></div></div></div>
</div>
<script>
function isTelegram(){ const tg=window.Telegram?.WebApp; return !!(tg && tg.initData && tg.initData.length>20); }
async function loadRoot(){
  const tg=window.Telegram?.WebApp;
  const info=document.getElementById('app');
  if(isTelegram()){
    info.innerHTML=`<div class=card><p>🔗 Telegram detected — redirecting…</p><p class=muted>Checking role…</p></div>`;
    try{
      const h={}; if(tg.initData) h['X-Telegram-Init-Data']=tg.initData;
      const r=await fetch('/api/user/status', {headers:h});
      if(r.ok){
        const j=await r.json();
        if(j.subscription && j.subscription.is_creator){ location.href='/admin/subscription'; return; }
      }
    }catch(e){}
    location.href='/app'; return;
  }
  let tiers={}; try{ tiers=(await (await fetch('/api/tiers')).json()).tiers||{}; }catch(e){}
  let botUser=""; try{ botUser=(await (await fetch('/api/botinfo')).json()).username||""; }catch(e){}
  const botLink=botUser?`https://t.me/${botUser}`:`https://t.me/`;
  info.innerHTML=`
  <div class=card>
    <h2 style="margin:0 0 8px">Welcome — Private Media Downloader</h2>
    <p class=muted>Download from YouTube (cookies+PO), Instagram, TikTok, X/Twitter & 1,700+ yt-dlp sites. Streams at <code>https://tgbot.southpark.ir:8080</code></p>
    <a class=btn href="${botLink}" target="_blank">🤖 Open bot in Telegram</a>
    <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
      <a class=btn style="flex:1;background:#242836" href="/app">👤 User Portal</a>
      <a class=btn style="flex:1;background:#242836" href="/admin/subscription">🛠 Admin</a>
    </div>
    <p class=muted>Tip: set BotFather Menu Button to <code>https://tgbot.southpark.ir:8080/</code> — this page auto-detects Telegram and sends users/admins correctly.</p>
  </div>
  <div class=card><h3 style="margin:0 0 8px">Plans</h3><table><tr><th>Tier</th><th>Daily</th><th>Price</th></tr>${Object.entries(tiers).map(([k,v])=>`<tr><td><b>${v.label}</b> <span class=muted>(${k})</span></td><td>${v.daily_limit}</td><td>${v.price_stars? v.price_stars+' ⭐':''} ${v.price_ton? '/ '+v.price_ton+' TON':''}</td></tr>`).join('')||'<tr><td colspan=3 class=muted>loading…</td></tr>'}</table><p class=muted>Free 5/d (last) → Basic 100/d → Plus 500/d → Pro 2500/d. Pay via Stars (XTR) or TON memo = user ID. Use <code>/subscription</code> in bot.</p></div>
  <div class=card><h3 style="margin:0 0 8px">Links</h3><ul style="margin:0 0 0 18px;font-size:13px;opacity:.9"><li><code>/app</code> — user portal (needs Telegram)</li><li><code>/admin/subscription</code> — admin (creator only)</li><li><code>/api/tiers</code> — public JSON</li><li><code>/stream/...</code> — file streams (24h)</li></ul></div>`;
}
loadRoot();
</script>
"""

HTML_USER = r"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>My Subscription — tgbot</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
""" + _SHARED_UI + r"""
<div class="page">
<header><div><h1>💳 My Subscription</h1><small>Quota, history & upgrades</small></div><a href="/" style="font-size:12px;color:var(--accent);text-decoration:none;white-space:nowrap">← Home</a></header>
<div id="app"><div class=card><div class="skeleton" style="height:16px;width:60%"></div><div class="skeleton" style="height:12px;width:85%;margin-top:10px"></div></div></div>
</div>
<script>
async function getStatus(){
  const tg=window.Telegram?.WebApp;
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
async function getTiers(){ const r=await fetch('/api/tiers'); return r.json(); }
function tierBadge(t){ const c={free:'badge-free',basic:'badge-basic',plus:'badge-plus',pro:'badge-pro'}[t.tier||'free']||'badge-free'; return `<span class="badge ${c}">${t.label||t.tier}</span>`; }
async function load(){
  const [st, tiersRes] = await Promise.all([getStatus().catch(e=>({error:e.message})), getTiers().catch(()=>({tiers:{}}))]);
  if(st.error){
    const tiers=tiersRes.tiers||{};
    const tg=window.Telegram?.WebApp;
    const isTG=!!(tg && tg.initData);
    const detail=st.error;
    const isAuth=detail.includes('Unauthorized');
    document.getElementById('app').innerHTML=`
      <div class=card style="border-color:rgba(255,69,58,.35)">
        <h3 style="margin:0;display:flex;gap:8px;align-items:center">🔒 ${isAuth?'Telegram auth required':'Failed to load'}</h3>
        <p style="margin:8px 0 0">${isAuth ? "This portal needs Telegram WebApp <code>initData</code> — open it from inside Telegram." : detail}</p>
        <p class=muted>${isTG ? "Telegram detected but verification failed — close and reopen from Menu Button." : "You opened <code>/app</code> in a browser. No <code>initData</code> — bot can't identify you."}</p>
        <p class=muted>BotFather Menu Button → <code>https://tgbot.southpark.ir:8080/</code> auto-redirects.</p>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px"><a class=btn href="/">← Home</a><button class="btn-alt" onclick="location.reload()">🔄 Retry</button></div>
      </div>
      <div class=card><h3 style="margin:0 0 8px">Plans (public)</h3><table><tr><th>Tier</th><th>Daily</th><th>Price</th></tr>${Object.entries(tiers).map(([k,v])=>`<tr><td><b>${v.label}</b> <span class=muted>(${k})</span></td><td>${v.daily_limit}</td><td>${v.price_stars? v.price_stars+' ⭐':''} ${v.price_ton? '/ '+v.price_ton+' TON':''}</td></tr>`).join('')||'<tr><td colspan=3 class=muted>—</td></tr>'}</table><p class=muted>Use <code>/subscription</code> in bot to buy.</p></div>`;
    if(isAuth) UI.showPopup("Telegram auth required", "Open this page inside Telegram via the bot's Menu Button (initData).", [{id:"ok",type:"default",text:"OK"}]);
    return;
  }
  const tiers=tiersRes.tiers||{}; const sub=st.subscription; const quota=st.quota||{}; const hist=st.history||[]; const settings=st.settings||{};
  const until=sub && sub.until ? new Date(sub.until*1000).toLocaleString() : '—';
  const tierInfo=(sub && tiers[sub.tier])||tiers['free']||{label:'Free',daily_limit:5};
  document.getElementById('app').innerHTML=`
  <div class=card>
    <div style="display:flex;justify-content:space-between;align-items:center"><h3 style="margin:0">Status</h3>${tierBadge(tierInfo)}</div>
    <div class=row><span>Tier</span><b>${tierInfo.label} (${sub?sub.tier:'free'})</b></div>
    <div class=row><span>Until</span><span>${until}</span></div>
    <div class=row><span>Daily quota</span><span>${quota.remaining ?? '?'} / ${quota.limit ?? tierInfo.daily_limit} left</span></div>
    <div class=row><span>Today used</span><span>${quota.used ?? 0}</span></div>
    ${settings.enabled ? `<p class=muted>Mode ${settings.enabled?'ON':'OFF'} · Free ${settings.free_enabled?'on':'off'} · Priority ${tierInfo.priority ?? 0}</p>` : '<p class=muted>Subscription OFF — unlimited (legacy)</p>'}
  </div>
  <div class=card>
    <h3 style="margin:0 0 8px">Upgrade — Stars & TON</h3>
    ${Object.entries(tiers).filter(([k])=>k!=='free').map(([k,v])=>`<div style="display:flex;gap:8px;align-items:center;justify-content:space-between;border:1px solid var(--card-b);border-radius:10px;padding:10px;margin:8px 0"><div><b>${v.label}</b><br><span class=muted>${v.daily_limit}/day · ${v.price_stars} ⭐ ${v.price_ton? '/ '+v.price_ton+' TON':''}</span></div><button style="width:auto;padding:8px 12px" onclick="buy('${k}')">⭐ Buy</button></div>`).join('')}
    <p class=muted>Tap ⭐ to get Stars invoice in bot chat (via <code>/subscription</code>). TON: send exact amount memo = user ID, then Verify.</p>
    <button class="btn-alt" onclick="location.reload()">🔄 Refresh</button>
  </div>
  ${hist.length ? `<div class=card><h3 style="margin:0 0 8px">Recent usage (7d)</h3><table><tr><th>Date</th><th>Count</th></tr>${hist.map(h=>`<tr><td>${h.date}</td><td>${h.count}</td></tr>`).join('')}</table></div>` : ''}
  <div class=card><p class=muted>Domain <code>https://tgbot.southpark.ir:8080</code> · <code>/stream/...</code> 24h token · Help via bot.</p></div>`;
}
function buy(tier){
  const tg=window.Telegram?.WebApp;
  if(tg && tg.sendData){ try{ tg.sendData(JSON.stringify({action:'buy', tier})); }catch(e){} }
  UI.toast('Open bot → /subscription → tap ⭐ '+tier,'info',2600);
}
load().catch(e=>{
  UI.showPopup("Load failed", e.message||String(e), [{id:"ok",type:"default",text:"OK"}]);
  document.getElementById('app').innerHTML=`<div class=card style="border-color:rgba(255,69,58,.35)"><h3 style="margin:0">⛔ Load failed</h3><p>${e.message}</p><div style="display:flex;gap:8px"><a class=btn href="/">← Home</a><button class="btn-alt" onclick="location.reload()">Retry</button></div></div>`;
});
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
        if not _is_admin_auth(request):
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
            ch_id = sanitized[0]["id"] if sanitized else 0
            ch_user = sanitized[0]["username"] if sanitized else ""
            new_s = set_settings(enabled=bool(body.get("enabled")), free_enabled=bool(body.get("free_enabled")), channels=sanitized, channel_id=ch_id, channel_username=ch_user)
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
            if channel_id or channel_username:
                new_s = set_settings(enabled=bool(body.get("enabled")), free_enabled=bool(body.get("free_enabled")), channels=[{"id": channel_id, "username": channel_username}], channel_id=channel_id, channel_username=channel_username)
            else:
                new_s = set_settings(enabled=bool(body.get("enabled")), free_enabled=bool(body.get("free_enabled")), channels=[], channel_id=0, channel_username="")
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
        from utils.subscription.store import get_settings, is_subscription_active
        from utils.subscription.tiers import TIERS
        from utils.subscription.quota import check_quota
        from utils.gate import load_database
        active, sub = is_subscription_active(uid)
        if not sub:
            sub = {"tier": "free", "until": 0}
        allowed, rem, lim = check_quota(uid)
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        db = load_database()
        usage = db.get("usage", {}).get(str(uid), {})
        used = usage.get(today, 0) if isinstance(usage, dict) else 0
        hist = []
        for d, cnt in sorted(usage.items())[-7:]:
            hist.append({"date": d, "count": cnt})
        tier_info = TIERS.get(sub.get("tier", "free"), TIERS["free"])
        return JSONResponse({"user": {"id": uid, "username": user.get("username", ""), "first_name": user.get("first_name", "")}, "subscription": sub, "tier_info": tier_info, "quota": {"allowed": allowed, "remaining": rem, "limit": lim, "used": used}, "history": hist, "settings": get_settings()})

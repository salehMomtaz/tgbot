"""Admin WebApp — single-page admin console UI (HTML/CSS/JS).

Mirrors every feature of the in-chat admin console (modules/admin/*) as a
Telegram Mini App: Users, Cookie Jars, Premium uploads + session generation,
PO Token provider, Subscriptions + force-join channels, Direct-Forward relays
and the System actions (abort queue / restart). All API calls go to
``/admin/api/*`` and require creator auth (initData or X-Admin-Token).
"""
from __future__ import annotations

HTML = r"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Admin Console — tgbot</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
 :root{ --tg-safe-top:0px; --tg-safe-bottom:0px; --bg:#0f1115; --card:#1a1d24; --card-b:#232735; --text:#e6e6e6; --muted:#9aa0b3; --accent:#2ea6ff; --ok:#30d158; --warn:#ff9f0a; --err:#ff453a; color-scheme: dark; }
 *{box-sizing:border-box} html,body{margin:0;min-height:100%} body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Inter,sans-serif;background:var(--bg);color:var(--text);line-height:1.45}
 a{color:var(--accent)}
 .page{max-width:840px;margin:0 auto;padding:16px;padding-top:calc(12px + var(--tg-safe-top) + env(safe-area-inset-top));padding-bottom:calc(24px + var(--tg-safe-bottom) + env(safe-area-inset-bottom))}
 .hdr{position:sticky;top:0;z-index:10;background:rgba(15,17,21,.88);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);margin:-12px -16px 12px;padding:12px 16px;border-bottom:1px solid #22262f;padding-top:calc(10px + var(--tg-safe-top) + env(safe-area-inset-top));display:flex;align-items:center;justify-content:space-between;gap:12px}
 .hdr h1{font-size:18px;margin:0;font-weight:800}
 .hdr small{opacity:.6;font-size:12px}
 .card{background:var(--card);border:1px solid var(--card-b);border-radius:14px;padding:16px;margin:12px 0;box-shadow:0 1px 0 rgba(0,0,0,.28)}
 .muted{color:var(--muted);font-size:12px}
 .row{display:flex;justify-content:space-between;gap:12px;margin:8px 0;flex-wrap:wrap}
 .btn{appearance:none;border:0;background:var(--accent);color:#fff;padding:10px 16px;border-radius:10px;font-weight:700;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;gap:6px;text-decoration:none;font-size:13px}
 .btn:active{transform:scale(.98)} .btn:disabled{opacity:.5}
 .btn-alt{background:#242836;color:var(--text);border:1px solid var(--card-b)}
 .btn-ghost{background:transparent;border:1px solid var(--card-b);color:var(--text)}
 .btn-danger{background:#3a1616;color:#ff6b5e;border:1px solid #5a2323}
 .btn-sm{padding:6px 10px;font-size:12px;border-radius:8px}
 input,textarea,select{width:100%;padding:10px 12px;border-radius:10px;border:1px solid #2a2e38;background:#0f1115;color:#fff;font:inherit}
 textarea{resize:vertical;min-height:72px}
 table{width:100%;border-collapse:collapse} th,td{padding:7px 10px;border-bottom:1px solid var(--card-b);text-align:left;font-size:13px} th{opacity:.7}
 .pill{display:inline-block;background:#242836;border:1px solid #2a2e38;border-radius:999px;padding:3px 9px;font-size:12px;margin:3px}
 .grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px}
 .stat{border:1px solid var(--card-b);border-radius:12px;padding:12px 14px;background:#15181f}
 .stat b{font-size:20px;display:block;margin-top:2px}
 .stat small{opacity:.6;font-size:11px}
 .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:middle}
 .ok{color:var(--ok)} .warn{color:var(--warn)} .err{color:var(--err)} .acc{color:var(--accent)}
 .tabs{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 14px}
 .tab{appearance:none;border:1px solid var(--card-b);background:transparent;color:var(--muted);padding:8px 14px;border-radius:999px;font-size:13px;font-weight:600;cursor:pointer}
 .tab.on{background:var(--accent);color:#fff;border-color:var(--accent)}
 .inrow{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
 .inrow input{width:auto;flex:1;min-width:120px}
 .inrow .btn{width:auto}
 .divider{border:0;border-top:1px solid var(--card-b);margin:14px 0}
 code{background:#232735;padding:2px 6px;border-radius:6px;font-size:12px}
 .skeleton{background:linear-gradient(90deg,#1a1d24 25%,#232735 37%,#1a1d24 63%);background-size:400% 100%;animation:shimmer 1.2s ease infinite;border-radius:10px;height:14px}
 @keyframes shimmer{0%{background-position:100% 0}100%{background-position:0 0}}
 #toast-stack{position:fixed;left:50%;bottom:calc(16px + var(--tg-safe-bottom) + env(safe-area-inset-bottom));transform:translateX(-50%);z-index:100;display:flex;flex-direction:column;gap:8px;align-items:center;pointer-events:none;max-width:min(92vw,560px);width:100%}
 .toast{pointer-events:auto;background:#1e232f;border:1px solid #2a2e38;color:var(--text);padding:12px 14px;border-radius:12px;box-shadow:0 8px 24px rgba(0,0,0,.4);display:flex;gap:10px;align-items:flex-start;animation:toastIn .22s ease;white-space:pre-wrap;font-size:13px}
 .toast.ok{border-color:rgba(48,209,88,.35)} .toast.err{border-color:rgba(255,69,58,.35)} .toast.warn{border-color:rgba(255,159,10,.35)}
 .toast .t-ic{font-size:16px;flex:0 0 auto;margin-top:1px}
 .toast .t-msg{flex:1;line-height:1.35}
 @keyframes toastIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
 #tg-modal{position:fixed;inset:0;z-index:90;display:none}
 #tg-modal.open{display:block}
 .modal-backdrop{position:absolute;inset:0;background:rgba(0,0,0,.55);backdrop-filter:blur(2px)}
 .modal-card{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:min(94vw,440px);background:var(--card);border:1px solid var(--card-b);border-radius:16px;padding:18px;box-shadow:0 18px 48px rgba(0,0,0,.5);max-height:min(84vh,640px);overflow:auto}
 .modal-card h3{margin:0 0 10px;font-size:16px;display:flex;align-items:center;gap:8px}
 .modal-card pre{white-space:pre-wrap;word-break:break-all;font-size:11px;background:#0f1115;border:1px solid var(--card-b);border-radius:8px;padding:10px;max-height:180px;overflow:auto}
 .modal-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:14px;flex-wrap:wrap}
 .mstep{font-size:13px;opacity:.9;margin:0 0 12px}
</style>
<div id="toast-stack" aria-live="polite"></div>
<div id="tg-modal" aria-hidden="true"><div class="modal-backdrop" onclick="ADM.closeModal()"></div><div class="modal-card" role="dialog" aria-modal="true"><h3 id="modal-title"></h3><div id="modal-body" style="font-size:13px;opacity:.9"></div><div class="modal-actions" id="modal-actions"></div></div></div>
<script>
const tg = window.Telegram?.WebApp;
(function(){
  if(!tg) return;
  try{ tg.ready(); tg.expand(); }catch(e){}
  function applySafe(){
    try{
      const sa=tg.safeAreaInset||{top:0,bottom:0};
      document.documentElement.style.setProperty('--tg-safe-top',(sa.top||0)+'px');
      document.documentElement.style.setProperty('--tg-safe-bottom',(sa.bottom||0)+'px');
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
let ADM_TOKEN = localStorage.getItem('admin_token')||'';
const ADM = {
  toast(msg,type='info',ms=3800){
    const stack=document.getElementById('toast-stack'); if(!stack) return;
    const el=document.createElement('div');
    el.className='toast '+(type==='ok'?'ok':type==='err'?'err':type==='warn'?'warn':'');
    const ic = type==='ok'?'✅':type==='err'?'⛔':type==='warn'?'⚠️':'ℹ️';
    el.innerHTML=`<span class="t-ic">${ic}</span><span class="t-msg"></span>`;
    el.querySelector('.t-msg').textContent=msg;
    stack.appendChild(el);
    try{ tg && tg.HapticFeedback && tg.HapticFeedback.notificationOccurred(type==='err'?'error':type==='warn'?'warning':'success'); }catch(e){}
    setTimeout(()=>{ el.style.opacity='0'; el.style.transform='translateY(4px)'; el.style.transition='all .2s'; setTimeout(()=>el.remove(),220); },ms);
  },
  modal(title, bodyHtml, buttons){
    const m=document.getElementById('tg-modal'); if(!m) return;
    document.getElementById('modal-title').textContent=title||'Notice';
    document.getElementById('modal-body').innerHTML = bodyHtml || '';
    const acts=document.getElementById('modal-actions'); acts.innerHTML='';
    (buttons||[{id:'ok',type:'default',text:'OK'}]).forEach(b=>{
      const btn=document.createElement('button');
      btn.className = b.type==='danger'?'btn btn-danger':(b.type==='default'?'btn':'btn-alt');
      btn.textContent=b.text||b.id;
      btn.onclick=()=>{ ADM.closeModal(); if(b.onClick) b.onClick(b.id); };
      acts.appendChild(btn);
    });
    m.classList.add('open'); m.setAttribute('aria-hidden','false');
  },
  closeModal(){
    const m=document.getElementById('tg-modal');
    if(m){ m.classList.remove('open'); m.setAttribute('aria-hidden','true'); }
  },
  confirm(title, bodyHtml, okLabel, onOk, danger){
    ADM.modal(title, bodyHtml, [
      {id:'cancel',type:'alt',text:'Cancel'},
      {id:'ok',type:danger?'danger':'default',text:okLabel||'OK',onClick:()=>onOk&&onOk()}
    ]);
  },
  async api(path, method='GET', body=null, isForm=false){
    const h={};
    if(tg && tg.initData) h['X-Telegram-Init-Data']=tg.initData;
    if(ADM_TOKEN) h['X-Admin-Token']=ADM_TOKEN;
    const opts={method, headers:h};
    if(body && isForm){ opts.body=body; }
    else if(body){ h['Content-Type']='application/json'; opts.body=JSON.stringify(body); }
    const r = await fetch('/admin/api'+path, opts);
    if(!r.ok){
      let detail=`HTTP ${r.status}`;
      try{ const j=await r.json(); detail = j.detail || j.message || JSON.stringify(j); }catch(_){}
      const err=new Error(detail); err.status=r.status; throw err;
    }
    return r.json();
  },
  fmtSize(b){
    if(!b && b!==0) return '—';
    if(b<1024) return b+' B';
    if(b<1048576) return (b/1024).toFixed(0)+' KB';
    return (b/1048576).toFixed(1)+' MB';
  },
  ago(ts){
    if(!ts) return '—';
    const d=Math.max(0, Math.floor(Date.now()/1000 - ts));
    if(d<60) return d+'s ago';
    if(d<3600) return Math.floor(d/60)+'m ago';
    if(d<86400) return Math.floor(d/3600)+'h ago';
    return Math.floor(d/86400)+'d ago';
  },
  esc(s){
    return String(s==null?'':s).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  }
};
const TABS = ['Overview','Users','Cookies','PO Token','Premium','Subscriptions','Direct','System'];
let CUR = 'Overview';
const APP = document.getElementById('app') || (document.body.innerHTML += '<div id="app"></div>', document.getElementById('app'));

function navHtml(){
  return `<div class="tabs">` + TABS.map(t=>`<button class="tab ${CUR===t?'on':''}" onclick="ADM.goto('${t}')">${t}</button>`).join('') + `</div><div id="view"></div>`;
}
function setBody(){
  const app = document.getElementById('app');
  if(!app) return;
  app.innerHTML = `<div class="page"><header class="hdr"><div><h1>🛠 Admin Console</h1><small>tgbot · full control in a Mini App</small></div><a href="/" style="font-size:12px;color:var(--accent);text-decoration:none;white-space:nowrap">← Home</a></header>${navHtml()}</div>`;
}
function showAuth(err){
  const app = document.getElementById('app'); if(!app) return;
  app.innerHTML = `
  <div class="page">
    <header class="hdr"><div><h1>🛠 Admin Console</h1><small>creator access required</small></div><a href="/" style="font-size:12px;color:var(--accent);text-decoration:none;white-space:nowrap">← Home</a></header>
    <div class="card" style="border-color:rgba(255,69,58,.35)">
      <h3 style="margin:0;display:flex;gap:8px;align-items:center">⛔ Admin access required</h3>
      <p style="margin:8px 0 0;opacity:.9">${ADM.esc(err||'Forbidden')}</p>
      <div style="background:#1e1515;border:1px solid #3a2a2a;border-radius:10px;padding:12px;margin-top:12px">
        <p class="muted" style="margin:0 0 8px"><b>How to open correctly:</b></p>
        <ol style="margin:0 0 0 18px;font-size:13px;opacity:.9">
          <li>In Telegram: bot → <code>/start</code> → <code>🛠 Console</code> → <code>🌐 WebApp Console</code></li>
          <li>Or set BotFather Menu Button to <code>https://tgbot.southpark.ir:8080/</code> — root auto-redirects creators to <code>/admin</code></li>
          <li>Or outside Telegram: send <code>/admin_token</code> in bot, copy the token and paste it below.</li>
        </ol>
      </div>
      <div class="inrow" style="margin-top:12px">
        <input id="tok-in" placeholder="admin token (16 hex)" value="${ADM.esc(ADM_TOKEN)}">
        <button class="btn-alt" onclick="ADM.setToken()">Set token</button>
        <button class="btn-ghost" onclick="ADM.clearToken()">Clear</button>
      </div>
      <div class="inrow" style="margin-top:10px">
        <a class="btn" href="/">← Home</a>
        <button class="btn-alt" onclick="location.reload()">🔄 Retry</button>
      </div>
    </div>
  </div>`;
}
function setToken(){
  const el=document.getElementById('tok-in')||document.getElementById('sys-tok');
  if(!el) return;
  ADM_TOKEN=el.value.trim();
  localStorage.setItem('admin_token', ADM_TOKEN);
  ADM.toast('Token saved','ok');
  ADM.goto(CUR, true);
}
function clearToken(){
  localStorage.removeItem('admin_token'); ADM_TOKEN='';
  const el=document.getElementById('tok-in')||document.getElementById('sys-tok');
  if(el) el.value='';
  ADM.toast('Token cleared','warn');
}
ADM.setToken=setToken; ADM.clearToken=clearToken;

// ---------- view plumbing ----------
ADM.goto = function(tab, force){
  if(!force && tab===CUR) return;
  CUR = tab;
  setBody();
  const loaders = {Overview:loadOverview, Users:loadUsers, Cookies:loadCookies, 'PO Token':loadPot, Premium:loadPremium, Subscriptions:loadSub, Direct:loadDirect, System:loadSystem};
  (loaders[tab]||loadOverview)().catch(e=>{
    const v=document.getElementById('view');
    if(e.status===403){ showAuth(e.message); return; }
    if(v) v.innerHTML=`<div class="card" style="border-color:rgba(255,69,58,.35)"><h3 style="margin:0">⛔ ${ADM.esc(e.message)}</h3><div class="inrow" style="margin-top:10px"><button class="btn-alt" onclick="ADM.goto('${tab}',true)">Retry</button></div></div>`;
    ADM.toast(e.message,'err');
  });
};

// ---------- Overview ----------
async function loadOverview(){
  const v=document.getElementById('view');
  v.innerHTML=`<div class="card"><div class="skeleton" style="height:16px;width:50%"></div><div class="skeleton" style="height:12px;width:80%;margin-top:10px"></div></div>`;
  const s=await ADM.api('/state');
  const d=s.database, q=s.queue, sub=s.subscription, p=s.pot, dir=s.direct;
  const subDot = sub.enabled ? '<span class="dot ok"></span>ON' : '<span class="dot warn"></span>OFF';
  const potDot = p.running ? '<span class="dot ok"></span>Running' : '<span class="dot err"></span>Stopped';
  const premDot = s.bot.premium_session ? '<span class="dot ok"></span>Configured' : '<span class="dot warn"></span>Not set';
  v.innerHTML = `
  <div class="grid2">
    <div class="stat"><small>Authorized users</small><b>${d.authorized_count}</b></div>
    <div class="stat"><small>Blacklisted</small><b>${d.blacklisted_count}</b></div>
    <div class="stat"><small>Premium (4 GB)</small><b>${d.premium_count}</b></div>
    <div class="stat"><small>Queue</small><b>${q.pending} pending · ${q.active?'active':'idle'}</b></div>
    <div class="stat"><small>Subscriptions mode</small><b style="font-size:16px">${subDot}</b></div>
    <div class="stat"><small>Active subs</small><b>${sub.active_subs}</b></div>
    <div class="stat"><small>PO Token provider</small><b style="font-size:15px">${potDot}</b></div>
    <div class="stat"><small>Premium session</small><b style="font-size:15px">${premDot}</b></div>
  </div>
  <div class="card"><h3 style="margin-top:0">Subscription</h3>
    <div class="row"><span>Mode</span><b>${sub.enabled?'ON 🟢':'OFF ⚪'}</b></div>
    <div class="row"><span>Free tier</span><b>${sub.free_enabled?'ON ✅':'OFF ❌'}</b></div>
    <div class="row"><span>Force-join channels</span><span>${sub.channels && sub.channels.length ? sub.channels.map(c=>ADM.esc(c.username||c.id)).join(', ') : '— (none)'}</span></div>
  </div>
  <div class="card"><h3 style="margin-top:0">Cookie jars</h3>
    <table><tr><th>Jar</th><th>Status</th><th>Size</th><th>Last ok</th></tr>
    ${Object.entries(s.cookies.jars).map(([k,j])=>`<tr><td><code>${k}.txt</code></td><td>${j.valid?'<span class="ok">valid</span>':j.exists?'<span class="warn">empty/invalid</span>':'<span class="err">missing</span>'}</td><td>${ADM.fmtSize(j.size)}</td><td>${ADM.ago(j.last_success)}</td></tr>`).join('')}
    </table>
  </div>
  <div class="card"><h3 style="margin-top:0">Direct-Forward relays</h3>
    <div class="row"><span>Instagram</span><b>${dir.ig.enabled?'🟢':'⚪'} · ${ADM.esc(dir.ig.status)}</b></div>
    <div class="row"><span>X / Twitter</span><b>${dir.x.enabled?'🟢':'⚪'} · cookies ${dir.x.cookies}${dir.x.uid?' (uid '+dir.x.uid+')':''}</b></div>
    <div class="row"><span>TikTok</span><b>${dir.tt.enabled?'🟢':'⚪'} · cookies ${dir.tt.cookies}</b></div>
    <div class="row"><span>Relay chat</span><b>${dir.relay_chat||'<span class="warn">not set</span>'}</b></div>
  </div>
  <p class="muted">Domain: ${ADM.esc(s.bot.domain)} · creator: <code>${s.bot.creator}</code></p>`;
}

// ---------- Users ----------
async function loadUsers(){
  const v=document.getElementById('view');
  v.innerHTML=`<div class="card"><div class="skeleton" style="height:14px;width:40%"></div></div>`;
  const d=await ADM.api('/users');
  const authRows = d.authorized.length ? d.authorized.map(uid=>`<tr><td><code>${uid}</code></td><td><button class="btn btn-sm btn-danger" onclick="ADM.userRemove(${uid})">Remove</button></td></tr>`).join('') : '<tr><td colspan=2 class="muted">No additional authorized users</td></tr>';
  const blackRows = d.blacklisted.length ? d.blacklisted.map(uid=>`<tr><td><code>${uid}</code></td><td><button class="btn btn-sm btn-alt" onclick="ADM.userUnban(${uid})">Unban</button></td></tr>`).join('') : '<tr><td colspan=2 class="muted">Blacklist registry is empty</td></tr>';
  v.innerHTML = `
  <div class="card">
    <h3 style="margin-top:0">➕ Add authorized user</h3>
    <div class="inrow"><input id="add-uid" placeholder="Telegram numeric ID (5-11 digits)" inputmode="numeric"><button class="btn" onclick="ADM.userAdd()">Add</button></div>
  </div>
  <div class="card">
    <h3 style="margin-top:0">Authorized users</h3>
    <table><tr><th>User ID</th><th></th></tr>${authRows}</table>
  </div>
  <div class="card">
    <h3 style="margin-top:0">🚫 Blacklisted</h3>
    <table><tr><th>User ID</th><th></th></tr>${blackRows}</table>
  </div>
  <div class="card">
    <h3 style="margin-top:0">📄 Document mode (creator)</h3>
    <p class="muted">Toggles the creator's own Document-mode preference (uploads arrive as documents instead of videos).</p>
    <button class="btn-alt" onclick="ADM.toggleDoc()">Toggle Document Mode</button>
  </div>`;
}
async function userAdd(){
  const el=document.getElementById('add-uid'); const uid=el.value.trim();
  if(!uid){ ADM.toast('Enter a user ID','warn'); return; }
  const r=await ADM.api('/users/add','POST',{user_id:uid});
  ADM.toast(r.message, r.ok?'ok':'err'); loadUsers();
}
async function userRemove(uid){
  ADM.confirm('Remove user', `Revoke access for <code>${uid}</code>?`, 'Remove', async()=>{
    const r=await ADM.api('/users/remove','POST',{user_id:uid});
    ADM.toast(r.message, r.ok?'ok':'err'); loadUsers();
  }, true);
}
async function userUnban(uid){
  ADM.confirm('Unban user', `Unban <code>${uid}</code>?`, 'Unban', async()=>{
    const r=await ADM.api('/users/unban','POST',{user_id:uid});
    ADM.toast(r.message, r.ok?'ok':'err'); loadUsers();
  });
}
async function toggleDoc(){
  const r=await ADM.api('/doc-mode','POST',{});
  ADM.toast(r.message, r.ok?'ok':'err');
}
// ---------- Cookies ----------
async function loadCookies(){
  const v=document.getElementById('view');
  v.innerHTML=`<div class="card"><div class="skeleton" style="height:14px;width:40%"></div></div>`;
  const [c, ps] = await Promise.all([ADM.api('/cookies'), ADM.api('/cookies/per-site')]);
  const jarCards = Object.entries(c.jars).map(([k,j])=>{
    const status = j.valid ? '<span class="ok">● valid</span>' : j.exists ? '<span class="warn">● empty/invalid</span>' : '<span class="err">● missing</span>';
    return `<div class="card"><h3 style="margin-top:0">🍪 <code>${k}.txt</code> <span style="float:right;font-size:12px">${status}</span></h3>
      <div class="row"><span>Size</span><span>${ADM.fmtSize(j.size)}</span></div>
      <div class="row"><span>Last successful auth</span><span>${ADM.ago(j.last_success)}</span></div>
      <div class="row"><span>Last upload</span><span>${ADM.ago(j.last_upload)}</span></div>
      ${j.last_failure?`<div class="row"><span>Last failure</span><span class="err" style="font-size:12px">${ADM.esc((j.failure_reason||'unknown').slice(0,120))}</span></div>`:''}
      <div class="inrow" style="margin-top:10px">
        <button class="btn btn-sm" onclick="ADM.cookieDownload('${k}')">📤 Download</button>
        <label class="btn btn-sm btn-alt" style="cursor:pointer">✏️ Replace<input type="file" accept=".txt,text/plain" style="display:none" onchange="ADM.cookieReplace('${k}',this)"></label>
        <button class="btn btn-sm btn-alt" onclick="ADM.cookieTest('${k}')">🧪 Test</button>
        ${k==='ytcookies'?`<button class="btn btn-sm btn-alt" onclick="ADM.cookieBackup('${k}')">💾 Backup</button><button class="btn btn-sm btn-alt" onclick="ADM.cookieRestore('${k}')">♻️ Restore</button>`:''}
      </div>
    </div>`;
  }).join('');
  const perRows = Object.entries(ps.jars).map(([site,j])=>`<tr><td><code>${site}.txt</code></td><td>${j.valid?'<span class="ok">valid</span>':'<span class="warn">invalid</span>'}</td><td>${ADM.fmtSize(j.size)}</td><td><button class="btn btn-sm btn-ghost" onclick="ADM.perSiteDelete('${site}')">🗑</button></td></tr>`).join('') || '<tr><td colspan=4 class="muted">No per-site jars yet</td></tr>';
  v.innerHTML = `
  <div class="card"><h3 style="margin-top:0">Primary cookie jars</h3>
    <p class="muted">Jars are read-only at rest (0o444); yt-dlp uses snapshots and write-back merges keep them warm. Download/upload here are the admin paths.</p>
    ${jarCards}
  </div>
  <div class="card"><h3 style="margin-top:0">Per-site jars <span class="muted">(cookies/ytdlp/)</span></h3>
    <table><tr><th>Site</th><th>Status</th><th>Size</th><th></th></tr>${perRows}</table>
    <div class="inrow" style="margin-top:12px">
      <input id="psite-name" placeholder="site name, e.g. reddit" style="max-width:180px">
      <label class="btn btn-sm btn-alt" style="cursor:pointer">➕ Upload per-site jar<input type="file" accept=".txt,text/plain" style="display:none" onchange="ADM.perSiteUpload(this)"></label>
    </div>
  </div>`;
}
async function cookieDownload(key){
  try{
    const h={}; if(tg&&tg.initData) h['X-Telegram-Init-Data']=tg.initData; if(ADM_TOKEN) h['X-Admin-Token']=ADM_TOKEN;
    const r=await fetch(`/admin/api/cookies/${key}/download`,{headers:h});
    if(!r.ok){ let d=`HTTP ${r.status}`; try{const j=await r.json(); d=j.detail||d;}catch(_){} throw new Error(d); }
    const blob=await r.blob();
    const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download=`${key}.txt`; a.click();
    setTimeout(()=>URL.revokeObjectURL(a.href),4000);
  }catch(e){ ADM.toast('Download failed: '+e.message,'err'); }
}
async function cookieReplace(key, input){
  const f=input.files&&input.files[0]; if(!f) return;
  ADM.toast('Uploading jar…','info');
  const fd=new FormData(); fd.append('file', f);
  const r=await ADM.api(`/cookies/${key}/upload`,'POST',fd,true);
  ADM.toast(r.message, r.ok?'ok':'err'); input.value=''; loadCookies();
}
async function cookieTest(key){
  ADM.toast('Running live yt-dlp probe…','info',6000);
  const r=await ADM.api(`/cookies/${key}/test`,'POST',{});
  ADM.showPopup('Cookie test', r.message, r.ok);
}
async function cookieBackup(key){ const r=await ADM.api(`/cookies/${key}/backup`,'POST',{}); ADM.toast(r.message, r.ok?'ok':'err'); loadCookies(); }
async function cookieRestore(key){
  ADM.confirm('Restore backup', `Restore <code>${key}.txt</code> from <code>ytcookies.backup</code>? The current jar will be overwritten.`, 'Restore', async()=>{
    const r=await ADM.api(`/cookies/${key}/restore`,'POST',{});
    ADM.toast(r.message, r.ok?'ok':'err'); loadCookies();
  }, true);
}
async function perSiteUpload(input){
  const site=(document.getElementById('psite-name')||{}).value||''; const f=input.files&&input.files[0];
  if(!site||!f){ ADM.toast('Enter a site name and pick a file','warn'); return; }
  ADM.toast('Uploading per-site jar…','info');
  const fd=new FormData(); fd.append('site', site.trim().toLowerCase()); fd.append('file', f);
  const r=await ADM.api('/cookies/per-site/upload','POST',fd,true);
  ADM.toast(r.message, r.ok?'ok':'err'); input.value=''; loadCookies();
}
async function perSiteDelete(site){
  ADM.confirm('Delete per-site jar', `Delete <code>cookies/ytdlp/${site}.txt</code>?`, 'Delete', async()=>{
    const r=await ADM.api(`/cookies/per-site/${site}/delete`,'POST',{});
    ADM.toast(r.message, r.ok?'ok':'err'); loadCookies();
  }, true);
}

// ---------- PO Token ----------
async function loadPot(){
  const v=document.getElementById('view');
  v.innerHTML=`<div class="card"><div class="skeleton" style="height:14px;width:40%"></div></div>`;
  const p=await ADM.api('/pot');
  v.innerHTML = `
  <div class="card">
    <h3 style="margin-top:0">🔐 PO Token Provider</h3>
    <p class="muted">YouTube downloads require cookies + a PO token (no fallback). The provider runs on Deno and starts automatically with the bot.</p>
    <div class="row"><span>Provider running</span><b>${p.running?'<span class="ok">YES ✅</span>':'<span class="err">NO ❌</span>'}</b></div>
    <div class="row"><span>Provider available</span><b>${p.available?'YES ✅':'NO ❌'}</b></div>
    <div class="row"><span>PO token enabled</span><b>${p.enabled?'YES ✅':'NO ❌'}</b></div>
    <div class="row"><span>Endpoint</span><b><code>127.0.0.1:${p.port}</code></b></div>
    <div class="inrow" style="margin-top:12px">
      <button class="btn btn-sm" onclick="ADM.potStart()">🚀 Start</button>
      <button class="btn btn-sm btn-danger" onclick="ADM.potStop()">🛑 Stop</button>
      <button class="btn btn-sm btn-alt" onclick="ADM.potTest()">🧪 Test stack</button>
      <button class="btn btn-sm btn-alt" onclick="ADM.potDiagnose()">🔍 Diagnose</button>
    </div>
    <div id="pot-out"></div>
  </div>`;
}
async function potStart(){ const r=await ADM.api('/pot/start','POST',{}); ADM.showPopup('Start provider', r.message, r.ok); loadPot(); }
async function potStop(){
  ADM.confirm('Stop provider', 'YouTube downloads will FAIL while stopped (no fallback). Stop anyway?', 'Stop', async()=>{
    const r=await ADM.api('/pot/stop','POST',{}); ADM.showPopup('Stop provider', r.message, r.ok); loadPot();
  }, true);
}
async function potTest(){
  const out=document.getElementById('pot-out'); out.innerHTML='<p class="muted">Testing full stack (yt-dlp + cookies + PO)… may take ~30s</p>';
  const r=await ADM.api('/pot/test','POST',{});
  out.innerHTML=`<div style="background:#15181f;border:1px solid var(--card-b);border-radius:10px;padding:12px;margin-top:10px;white-space:pre-wrap;font-size:12px">${ADM.esc(r.message)}</div>`;
  ADM.toast(r.ok?'Test passed':'Test failed', r.ok?'ok':'err');
}
async function potDiagnose(){
  const out=document.getElementById('pot-out'); out.innerHTML='<p class="muted">Running diagnosis (no-auth vs cookies vs full stack)… up to 30s</p>';
  const r=await ADM.api('/pot/diagnose','POST',{});
  out.innerHTML=`<div style="background:#15181f;border:1px solid var(--card-b);border-radius:10px;padding:12px;margin-top:10px;white-space:pre-wrap;font-size:12px">${ADM.esc(r.message)}</div>`;
  ADM.toast(r.ok?'Diagnosis complete':'Diagnosis failed', r.ok?'ok':'err');
}

// ---------- Premium ----------
let GEN = null;
async function loadPremium(){
  const v=document.getElementById('view');
  v.innerHTML=`<div class="card"><div class="skeleton" style="height:14px;width:40%"></div></div>`;
  const p=await ADM.api('/premium');
  const rows = p.users.length ? p.users.map(uid=>`<tr><td><code>${uid}</code></td><td><button class="btn btn-sm btn-danger" onclick="ADM.premiumRemove(${uid})">Revoke</button></td></tr>`).join('') : '<tr><td colspan=2 class="muted">No Premium-enabled users yet</td></tr>';
  v.innerHTML = `
  <div class="card">
    <h3 style="margin-top:0">👑 Premium uploads (4 GB)</h3>
    <p class="muted">${p.session_set ? '<span class="ok">🟢 Premium userbot session is configured — 4 GB uploads available to whitelisted users.</span>' : '<span class="warn">⚪ No PREMIUM_STRING_SESSION set — 4 GB uploads are DISABLED. Generate one below.</span>'}</p>
    <div class="inrow">
      <button class="btn btn-sm" onclick="ADM.genOpen()">🔑 Generate Session</button>
      ${p.session_set?'<button class="btn btn-sm btn-ghost" onclick="ADM.genDiscardSaved()">❌ Forgot saved session</button>':''}
    </div>
    <p class="muted" style="margin-top:10px">Telegram bots are hard-capped at 2 GB; only a Premium <i>user</i> account over MTProto can send 4 GB.</p>
  </div>
  <div class="card">
    <h3 style="margin-top:0">Whitelisted users</h3>
    <div class="inrow" style="margin-bottom:10px"><input id="prem-uid" placeholder="Telegram numeric ID" inputmode="numeric" style="max-width:200px"><button class="btn btn-sm" onclick="ADM.premiumAdd()">➕ Add Premium</button></div>
    <table><tr><th>User ID</th><th></th></tr>${rows}</table>
  </div>`;
}
async function premiumAdd(){
  const el=document.getElementById('prem-uid'); const uid=el.value.trim();
  if(!uid){ ADM.toast('Enter a user ID','warn'); return; }
  const r=await ADM.api('/premium/add','POST',{user_id:uid});
  ADM.toast(r.message, r.ok?'ok':'err'); loadPremium();
}
async function premiumRemove(uid){
  ADM.confirm('Revoke Premium', `Revoke 4 GB uploads for <code>${uid}</code>?`, 'Revoke', async()=>{
    const r=await ADM.api('/premium/remove','POST',{user_id:uid});
    ADM.toast(r.message, r.ok?'ok':'err'); loadPremium();
  }, true);
}
async function genDiscardSaved(){
  ADM.confirm('Discard saved session', 'The session string was saved to .env. Discard just the local copy? (Set <code>PREMIUM_STRING_SESSION</code> empty manually to truly disable.)', 'OK', ()=>{});
}
function genOpen(){
  GEN = null;
  ADM.modal('🔑 Generate Premium Session',
    `<p class="mstep">Step 1/3 — enter the Premium account phone in international format. The login code goes to that account.</p>
     <input id="gen-phone" placeholder="+15551234567" inputmode="tel">
     <p class="muted">The temp login client is in-memory only — nothing is written to disk until you tap <b>Save</b>.</p>`,
    [{id:'cancel',type:'alt',text:'Cancel'},{id:'ok',type:'default',text:'Send code',onClick:()=>genSendCode()}]
  );
}
async function genSendCode(){
  const phone=(document.getElementById('gen-phone')||{}).value||'';
  if(!phone){ ADM.toast('Enter a phone number','warn'); return; }
  ADM.closeModal(); ADM.toast('Requesting code…','info');
  const r=await ADM.api('/premium/gen/start','POST',{phone});
  if(!r.ok){ ADM.showPopup('Failed', r.message, false); return; }
  GEN='code';
  ADM.modal('🔑 Enter login code',
    `<p class="mstep">Code sent to <code>${ADM.esc(phone)}</code>. Enter it below (typing here is safe — this is not a chat message).</p>
     <input id="gen-code" placeholder="12345" inputmode="numeric">`,
    [{id:'cancel',type:'alt',text:'Cancel',onClick:()=>genAbort()},{id:'ok',type:'default',text:'Verify',onClick:()=>genVerify()}]
  );
}
async function genVerify(){
  const code=(document.getElementById('gen-code')||{}).value||'';
  if(!code){ ADM.toast('Enter the code','warn'); return; }
  ADM.closeModal(); ADM.toast('Verifying…','info');
  const r=await ADM.api('/premium/gen/verify','POST',{code});
  if(!r.ok){ ADM.showPopup('Verification failed', r.message, false); GEN='code'; return; }
  if(r.step==='2fa'){
    GEN='2fa';
    ADM.modal('🔑 Two-factor password',
      `<p class="mstep">This account has 2FA enabled. Enter the password to finish logging in.</p>
       <input id="gen-pwd" type="password" placeholder="2FA password">`,
      [{id:'cancel',type:'alt',text:'Cancel',onClick:()=>genAbort()},{id:'ok',type:'default',text:'Finish',onClick:()=>genPassword()}]
    );
    return;
  }
  genResult();
}
async function genPassword(){
  const pwd=(document.getElementById('gen-pwd')||{}).value||'';
  if(!pwd){ ADM.toast('Enter the 2FA password','warn'); return; }
  ADM.closeModal(); ADM.toast('Logging in…','info');
  const r=await ADM.api('/premium/gen/password','POST',{password:pwd});
  if(!r.ok){ ADM.showPopup('Failed', r.message, false); GEN='2fa'; return; }
  genResult();
}
async function genResult(){
  GEN='result';
  const r=await ADM.api('/premium/gen');
  if(!r.result){ ADM.showPopup('No session', 'The session string was not produced. Start again.', false); GEN=null; return; }
  ADM.modal('🔑 Session string generated',
    `<p class="mstep">Copy it somewhere safe, or tap <b>Save to .env</b> to persist it for the bot (auto-restart follows).</p>
     <pre>${ADM.esc(r.result)}</pre>`,
    [{id:'cancel',type:'alt',text:'Discard',onClick:()=>genAbort()},{id:'save',type:'default',text:'💾 Save to .env',onClick:()=>genSave()}]
  );
}
async function genSave(){
  ADM.closeModal();
  const r=await ADM.api('/premium/gen/save','POST',{});
  ADM.toast(r.message, r.ok?'ok':'err');
  if(r.ok) setTimeout(()=>{ ADM.toast('🔄 Restarting the bot — page will reload shortly','warn'); setTimeout(()=>location.reload(),2500); },800);
}
async function genAbort(){
  ADM.closeModal(); ADM.api('/premium/gen/abort','POST',{}).then(r=>ADM.toast(r.message,'info')).catch(()=>{});
}
ADM.showPopup = function(title, message, ok){
  ADM.modal(title, `<div style="white-space:pre-wrap">${ADM.esc(message)}</div>`, [{id:'ok',type:ok?'default':'danger',text:'OK'}]);
};

// ---------- Subscriptions ----------
async function loadSub(){
  const v=document.getElementById('view');
  v.innerHTML=`<div class="card"><div class="skeleton" style="height:14px;width:40%"></div></div>`;
  const s=await ADM.api('/sub');
  const chRows = s.channels.length ? s.channels.map((c,i)=>`<tr><td><code>${ADM.esc(c.username||c.id)}</code></td><td>${c.id?c.id:'—'}</td><td><button class="btn btn-sm btn-danger" onclick="ADM.subChannelRemove('${ADM.esc(c.username||c.id).replace(/'/g,"")}')">Remove</button></td></tr>`).join('') : '<tr><td colspan=3 class="muted">No force-join channels</td></tr>';
  const tierRows = Object.entries(s.tiers).map(([k,t])=>`<tr><td><b>${ADM.esc(t.label)}</b> <span class="muted">(${k})</span></td><td>${t.daily_limit}/day</td><td>${t.price_stars} ⭐</td><td>${t.price_ton||'—'} TON</td><td>${t.priority}</td></tr>`).join('');
  v.innerHTML = `
  <div class="card"><h3 style="margin-top:0">Mode</h3>
    <div class="inrow">
      <button class="btn ${s.enabled?'btn-danger':'btn'}" onclick="ADM.subToggle()">${s.enabled?'🔴 Disable subscription mode':'🟢 Enable subscription mode'}</button>
      <button class="btn-alt" onclick="ADM.subToggleFree()">Free tier: ${s.free_enabled?'✅':'❌'}</button>
      <a class="btn btn-ghost btn-sm" href="/app">🌐 User portal</a>
    </div>
    <p class="muted">Mode <b>${s.enabled?'ON':'OFF'}</b> · Free tier <b>${s.free_enabled?'ON':'OFF'}</b> · Active subs <b>${s.active_count}</b> · Free users go last in queue (priority 0).</p>
  </div>
  <div class="card"><h3 style="margin-top:0">📢 Force-join channels</h3>
    <table><tr><th>Channel</th><th>ID</th><th></th></tr>${chRows}</table>
    <div class="inrow" style="margin-top:10px">
      <input id="sub-ch-in" placeholder="@username or -100123…" style="max-width:220px">
      <button class="btn btn-sm" onclick="ADM.subChannelAdd()">➕ Add</button>
      <button class="btn btn-sm btn-danger" onclick="ADM.subChannelsClear()">Clear all</button>
    </div>
    <p class="muted">Free users must be members of ALL listed channels to download when mode is ON.</p>
  </div>
  <div class="card"><h3 style="margin-top:0">🎫 Grant / revoke</h3>
    <div class="inrow">
      <input id="grant-in" placeholder="user_id tier days  (e.g. 123456789 plus 30)" style="flex:2;min-width:240px">
      <button class="btn btn-sm" onclick="ADM.subGrant()">Grant</button>
    </div>
    <div class="inrow" style="margin-top:8px">
      <input id="revoke-in" placeholder="user_id to revoke" inputmode="numeric" style="max-width:200px">
      <button class="btn btn-sm btn-danger" onclick="ADM.subRevoke()">Revoke</button>
    </div>
  </div>
  <div class="card"><h3 style="margin-top:0">Tiers</h3><table><tr><th>Tier</th><th>Daily</th><th>Stars</th><th>TON</th><th>Priority</th></tr>${tierRows}</table></div>
  <div class="card"><h3 style="margin-top:0">📋 Active subscriptions</h3>
    <div id="sub-list"><button class="btn btn-sm btn-alt" onclick="ADM.subList()">Load list</button></div>
  </div>`;
}
async function subToggle(){ const r=await ADM.api('/sub/toggle','POST',{}); ADM.toast(r.message, r.ok?'ok':'err'); loadSub(); }
async function subToggleFree(){ const r=await ADM.api('/sub/toggle-free','POST',{}); ADM.toast(r.message, r.ok?'ok':'err'); loadSub(); }
async function subChannelAdd(){
  const el=document.getElementById('sub-ch-in'); const txt=el.value.trim();
  if(!txt){ ADM.toast('Enter a channel','warn'); return; }
  const r=await ADM.api('/sub/channels/add','POST',{input:txt});
  ADM.toast(r.message, r.ok?'ok':'err'); loadSub();
}
async function subChannelRemove(idtxt){
  ADM.confirm('Remove channel', `Remove <code>${ADM.esc(idtxt)}</code> from force-join?`, 'Remove', async()=>{
    const r=await ADM.api('/sub/channels/remove','POST',{input:idtxt});
    ADM.toast(r.message, r.ok?'ok':'err'); loadSub();
  }, true);
}
async function subChannelsClear(){
  ADM.confirm('Clear channels', 'Remove ALL force-join channels (free tier without channel)?', 'Clear', async()=>{
    const r=await ADM.api('/sub/channels/clear','POST',{}); ADM.toast(r.message, r.ok?'ok':'err'); loadSub();
  }, true);
}
async function subGrant(){
  const el=document.getElementById('grant-in'); const parts=el.value.trim().split(/\s+/);
  if(parts.length<2){ ADM.toast('Format: user_id tier [days]','warn'); return; }
  const r=await ADM.api('/sub/grant','POST',{user_id:parts[0], tier:parts[1].toLowerCase(), days:parts[2]||30});
  ADM.toast(r.message, r.ok?'ok':'err'); loadSub();
}
async function subRevoke(){
  const el=document.getElementById('revoke-in'); const uid=el.value.trim();
  if(!uid){ ADM.toast('Enter a user ID','warn'); return; }
  ADM.confirm('Revoke subscription', `Revoke subscription for <code>${ADM.esc(uid)}</code>?`, 'Revoke', async()=>{
    const r=await ADM.api('/sub/revoke','POST',{user_id:uid}); ADM.toast(r.message, r.ok?'ok':'err'); loadSub();
  }, true);
}
async function subList(){
  const out=document.getElementById('sub-list'); out.innerHTML='<span class="muted">Loading…</span>';
  const r=await ADM.api('/sub/list');
  if(!r.subscriptions || !r.subscriptions.length){ out.innerHTML='<span class="muted">No subscriptions stored.</span>'; return; }
  out.innerHTML=`<table><tr><th>User</th><th>Tier</th><th>Until</th><th>By</th></tr>` + r.subscriptions.map(s=>`<tr><td><code>${s.user_id}</code></td><td>${s.tier}</td><td>${new Date(s.until*1000).toLocaleString()} ${s.active?'':'<span class="warn">(expired)</span>'}</td><td><span class="pill">${ADM.esc(s.granted_by||'')}</span></td></tr>`).join('') + `</table>`;
}

// ---------- Direct-Forward ----------
async function loadDirect(){
  const v=document.getElementById('view');
  v.innerHTML=`<div class="card"><div class="skeleton" style="height:14px;width:40%"></div></div>`;
  const d=await ADM.api('/direct');
  v.innerHTML = `
  <div class="card"><h3 style="margin-top:0">📨 Direct-Forward (DM relay)</h3>
    <p class="muted">Relay chat: <code>${d.relay_chat||'not set'}</code> · Poll: ${d.poll_seconds}s (± jitter) · <span class="warn">Enabling a platform auto-restarts the bot.</span></p>
  </div>
  <div class="grid2">
    <div class="card"><h3 style="margin-top:0">Instagram</h3>
      <div class="row"><span>Status</span><b>${d.ig.enabled?'<span class="ok">enabled</span>':'<span class="muted">disabled</span>'}</b></div>
      <div class="row"><span>Pairing</span><b>${ADM.esc(d.ig.status)}</b></div>
      <div class="row"><span>Cookies</span><b>${d.ig.cookies_ok?'<span class="ok">ok</span>':'<span class="warn">missing</span>'}</b></div>
      <div class="inrow" style="margin-top:10px">
        <button class="btn btn-sm ${d.ig.enabled?'btn-danger':'btn'}" onclick="ADM.directToggle('ig')">${d.ig.enabled?'🔴 Disable':'🟢 Enable'}</button>
        <button class="btn btn-sm btn-alt" onclick="ADM.directPairIg()">🔗 Pair</button>
        <button class="btn btn-sm btn-ghost" onclick="ADM.directUnpairIg()">💔 Unpair</button>
      </div>
    </div>
    <div class="card"><h3 style="margin-top:0">X / Twitter</h3>
      <div class="row"><span>Status</span><b>${d.x.enabled?'<span class="ok">enabled</span>':'<span class="muted">disabled</span>'}</b></div>
      <div class="row"><span>Cookies</span><b>${d.x.cookies==='ok'?'<span class="ok">ok</span>':d.x.cookies==='missing'?'<span class="warn">missing</span>':'<span class="err">bad</span>'}</b></div>
      ${d.x.uid?`<div class="row"><span>User ID</span><b><code>${d.x.uid}</code></b></div>`:''}
      <div class="row"><span>X Chat PIN</span><b>${d.x.pin_set?'<span class="ok">set (hidden)</span>':'<span class="warn">not set</span>'}</b></div>
      <div class="inrow" style="margin-top:10px">
        <button class="btn btn-sm ${d.x.enabled?'btn-danger':'btn'}" onclick="ADM.directToggle('x')">${d.x.enabled?'🔴 Disable':'🟢 Enable'}</button>
        <button class="btn btn-sm btn-alt" onclick="ADM.directTest('x')">🧪 Test</button>
        <button class="btn btn-sm btn-ghost" onclick="ADM.directSetPin()">🔑 Set PIN</button>
      </div>
    </div>
    <div class="card"><h3 style="margin-top:0">TikTok</h3>
      <div class="row"><span>Status</span><b>${d.tt.enabled?'<span class="ok">enabled</span>':'<span class="muted">disabled</span>'}</b></div>
      <div class="row"><span>Cookies</span><b>${d.tt.cookies==='ok'?'<span class="ok">ok</span>':'<span class="warn">missing</span>'}</b></div>
      <div class="inrow" style="margin-top:10px">
        <button class="btn btn-sm ${d.tt.enabled?'btn-danger':'btn'}" onclick="ADM.directToggle('tiktok')">${d.tt.enabled?'🔴 Disable':'🟢 Enable'}</button>
        <button class="btn btn-sm btn-alt" onclick="ADM.directTest('tiktok')">🧪 Test</button>
      </div>
    </div>
  </div>`;
}
async function directToggle(platform){
  const willEnable = document.querySelector(`button[onclick="ADM.directToggle('${platform}')"]`);
  const enabling = !(willEnable && willEnable.textContent.trim().startsWith('🔴'));
  ADM.confirm((enabling?'Enable':'Disable')+' relay', (enabling?'This enables the relay and <b>auto-restarts the bot</b>.':'This disables the relay; the worker stops on next restart.')+' Continue?', enabling?'Enable':'Disable', async()=>{
    const r=await ADM.api('/direct/toggle','POST',{platform});
    ADM.toast(r.message, r.ok?'ok':'err');
    if(r.ok && enabling) setTimeout(()=>{ ADM.toast('🔄 Restarting the bot — page will reload shortly','warn'); setTimeout(()=>location.reload(),2500); },800);
    else loadDirect();
  }, enabling);
}
async function directPairIg(){
  const r=await ADM.api('/direct/pair-ig','POST',{});
  ADM.showPopup('🔗 IG pairing code', r.message, true);
}
async function directUnpairIg(){
  ADM.confirm('Unpair Instagram', 'Forget the IG pairing?', 'Unpair', async()=>{
    const r=await ADM.api('/direct/unpair-ig','POST',{}); ADM.toast(r.message, r.ok?'ok':'err'); loadDirect();
  }, true);
}
async function directTest(platform){
  ADM.toast(`Testing ${platform}…`,'info');
  const r=await ADM.api('/direct/test','POST',{platform});
  ADM.showPopup(platform+' test', r.message, r.ok);
}
function directSetPin(){
  ADM.modal('🔑 Set X Chat PIN', `<p class="mstep">The XChat-encrypted self-DM needs the 4-digit passcode set in X Chat. Written to .env; the bridge picks it up automatically.</p>
    <input id="xpin" placeholder="0421" inputmode="numeric" maxlength="4">`,
    [{id:'cancel',type:'alt',text:'Cancel'},{id:'ok',type:'default',text:'Save',onClick:async()=>{
      const pin=(document.getElementById('xpin')||{}).value||'';
      const r=await ADM.api('/direct/set-x-pin','POST',{pin});
      ADM.closeModal(); ADM.toast(r.message, r.ok?'ok':'err'); loadDirect();
    }}]
  );
}

// ---------- System ----------
async function loadSystem(){
  const v=document.getElementById('view');
  v.innerHTML=`<div class="card"><div class="skeleton" style="height:14px;width:40%"></div></div>`;
  const s=await ADM.api('/state');
  v.innerHTML = `
  <div class="card">
    <h3 style="margin-top:0">💥 Abort transfer / reset</h3>
    <p class="muted">Clears the download queue (<b>${s.queue.pending} pending</b>, ${s.queue.active?'1 active':'idle'}) and purges the <code>cache/</code> directory. Any running download is interrupted.</p>
    <button class="btn btn-danger" onclick="ADM.abortQueue()">💥 Abort Transfer & Purge Cache</button>
  </div>
  <div class="card">
    <h3 style="margin-top:0">🔄 Restart bot</h3>
    <p class="muted">Graceful restart via systemd (SIGTERM → clean teardown → Restart=always). Queue is cleared.</p>
    <button class="btn" onclick="ADM.restartBot()">🔄 Restart Bot</button>
  </div>
  <div class="card">
    <h3 style="margin-top:0">🔐 Access</h3>
    <p class="muted">Admin token: <code>${ADM_TOKEN||'(none — using Telegram initData)'}</code></p>
    <div class="inrow">
      <input id="sys-tok" placeholder="admin token (16 hex)" value="${ADM.esc(ADM_TOKEN)}" style="max-width:260px">
      <button class="btn-alt btn-sm" onclick="ADM.setToken()">Set</button>
      <button class="btn-ghost btn-sm" onclick="ADM.clearToken()">Clear</button>
    </div>
  </div>`;
}
async function abortQueue(){
  ADM.confirm('Abort all transfers', 'Clear the download queue and purge <code>cache/</code>? Running downloads are interrupted.', 'Abort', async()=>{
    const r=await ADM.api('/queue/abort','POST',{}); ADM.showPopup('Reset', r.message, r.ok);
  }, true);
}
async function restartBot(){
  ADM.confirm('Restart the bot', 'The bot will shut down gracefully and come back in a few seconds. Any running download is interrupted. Continue?', 'Restart', async()=>{
    const r=await ADM.api('/restart','POST',{});
    ADM.toast(r.message, r.ok?'ok':'err');
    if(r.ok) setTimeout(()=>{ ADM.toast('🔄 Restarting — page will reload shortly','warn'); setTimeout(()=>location.reload(),3000); },1000);
  }, true);
}

// ---------- boot ----------
async function boot(){
  const app=document.getElementById('app');
  if(!app){
    const div=document.createElement('div'); div.id='app'; document.body.appendChild(div);
  }
  try{
    await ADM.api('/state');
    setBody(); loadOverview().catch(e=>{ if(e.status===403) showAuth(e.message); });
  }catch(e){
    if(e.status===403) showAuth(e.message);
    else { setBody(); loadOverview().catch(()=>showAuth(e.message)); }
  }
}
boot();
</script>
"""

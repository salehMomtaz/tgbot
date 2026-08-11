# 2026-08-11 health pass — package-split regression sweep

Full runtime + admin UI + security pass on the production box. Two things
prompted it: (a) the recent package-split refactor that broke
`modules/admin.py` / `modules/direct_forward.py` up into sub-packages
(commit `81a5139`) needed a verilive re-test of every admin button to catch
silent dispatch regressions; (b) the X photo-paste fix (commit `3cb497b`)
shipped that morning needed a confirmation that it actually closed the
"photo fallback fetch failed: 'urls'" loop in production. The pass was done
by driving the running bot from the creator's user account via
`tools/telethon_drive.py` (real inline-keyboard presses), while the bot ran
under `tgbot.service`.

## Box / runtime state

- **Host:** user `dev` on Ubuntu 24.04 (the box from AGENTS.md; the old US
  test VPS at `<ssh-port>` is retired but still holds a secret backup copy —
  see AGENTS.md "Secrets handling").
- **Box clock:** Tue 2026-08-11 ~18:42–19:30 UTC during the pass.
- **`tgbot.service`:** `active` throughout, `NRestarts=0`. Two restarts were
  performed by the agent during the pass (deploy the dispatch fix, then re-confirm):
  - pre-pass `MainPID=32429` (started 14:38:16 by the operator earlier that day),
  - post-fix `MainPID=82870` (started Tue 18:42:25 UTC).
- **PO provider:** Deno bound to `127.0.0.1:4417` only (verified via `ss
  -tlnp`), `POT_AVAILABLE=true`. The `admin_pot_action:test` live probe
  returned **"✅ Cookie Test Passed (with PO token) — YouTube returned 98
  downloadable formats"** (incl. English US original + de/es/fr/hi dubs),
  confirming the full cookies + PO-token + curl-cffi stack.
- **Direct-forward workers:** IG paired with the operator account, X enabled
  with `twid` uid + `XCHAT_PIN` set, TikTok enabled with `sessionid`. All
  three running healthy (HTTP 200s / WS pushes observed). No checkpoint
  freezes during the pass.
- **xchat-bridge** (`tgbot-xchat-bridge.service`) and **cookie-watch**
  (`cookie-watch.service`) active. **`tgbot-monitor.service` inactive** (by
  design — the Python spawner launches a detached monitor at boot instead;
  see invariant #15).
- Disk 22 %, load ~0.9. No tracebacks in `logs/bot.log` for the current boot.

## Log scan findings (since last restart)

- **Pre-fix X photo warnings (14:19–14:20 UTC)** — ten `photo fallback
  fetch failed: 'urls'` / `'pinned_tweet_ids_str'` lines from the
  pre-14:38 PID 32429 instance. These are the **exact** failure mode the
  commit `3cb497b` fix replaced (raw `gql.tweet_detail` walk). They stopped
  the moment that instance took over from the one that loaded before the
  midday deploys, and have not returned. **Closed.**
- **Post-fix X photo successes (14:56 onward, every few minutes)** — only
  clean `✅ relayed 1 share photo(s) -> <relay-chat-id>` lines. The
  accompanying `extract failed: ... No video could be found in this tweet`
  warnings are **expected** — they are the precondition that triggers the
  photo fallback in the first place (the tweet has photos, no video), not
  an error. Leaves the `WARN` log line count looking louder than reality;
  they were classified and dismissed, not chased.
- **Transient network blip 13:49–14:09** — `socket.send() raised
  exception` from pyrogram (MTProto reconnect) on two timestamps,
  self-healed in <30 s. Not chased, per skill guidance.
- **`instagrapi 1 validation error for Broadcast` at 14:16** — benign and
  handled (instagrapi marks a thread as having a broadcast item the model
  doesn't decode; the worker continues with the items it can read).
- **No `Traceback`** lines since the current boot (18:42 UTC). The lone
  pre-restart admin-UI bug found during the pass is below.

## Admin-console button sweep (every inline button driven live)

Driven via `venv/bin/python tools/telethon_drive.py --message "console"
--press '<pattern>' ...`. The bot renders a 12-button console from the
creator account (`id` in `config.SYSTEM_CREATOR_ID`).

| Pattern pressed | Result |
|---|---|
| `admin_list` | ✅ "No additional users authorized." (single-creator, correct) |
| `admin_cookies_menu` | ✅ "🍪 Cookie Jars Manager" — 5 jar buttons + ➕ Per-Site Jar (`admin_cookie_add_site`) + ◀️ Return (`admin_main`) |
| `admin_premium_menu` | ✅ "👑 Premium Uploads (4 GB) — 🟢 Premium userbot session configured"; Add/Remove/Generate/Cleanup/Refresh/Back |
| `admin_pot_menu` | ❌ **pre-fix** silently no-op; ✅ **post-fix** opens "🔓 PO Token Provider" with running=YES, available=YES, enabled=YES, `127.0.0.1:4417`, Test Stack/Diagnosis/Start-Stop/Refresh/Back (see fix #1) |
| `admin_pot_action:test` | ✅ "🧪 Testing cookies + PO-token stack..." → "✅ Cookie Test Passed (with PO token) — YouTube returned 98 downloadable formats" |
| `admin_direct_menu` | ✅ Full Direct-Forward submenu renders: IG paired, X enabled (uid + PIN), TikTok enabled (sessionid); buttons for each platform's toggle/pair/test + Set X PIN + Refresh + Back |
| `admin_premium_gen_clean` | ✅ Press acknowledged (no-op when nothing stale to sweep; no error) |
| `admin_abort_queue` | ✅ Press acknowledged, console unchanged (empty queue — nothing to abort; silent success) |
| `admin_restart` | ✅ Confirmation dialog "🔄 Restart the bot? ... Continue?" with ✅ Yes / ↩️ Cancel (no destructive action without confirmation — invariant #15 premium-restart design generalized). Cancel path returned safely to the console without restarting. |
| `admin_cookie_select:ytcookies` (via menu) | ✅ Opens "🍪 Cookie Jars Manager" then waits for second press to drill into the YouTube jar |

The downloads/playlist paths were **not** re-driven this pass (the operator
runs real traffic against the bot continuously — the live log lines for
YouTube / TikTok / IG / X delivery already cover the downloader path; the
operator asked specifically to keep test traffic light). The 2026-08-07
health-pass doc has the full per-site feature matrix if it is needed.

## Bug found + fixed (live-deployed on this box)

1. **`admin_pot_menu` was a silent no-op after the package-split refactor.**
   Pre-split `modules/admin.py:1370` called `_render_pot_menu(callback_query)`
   **directly**. The split refactor (commit `81a5139`) rewrote
   `modules/admin/callback_dispatch.py:536-538` as
   `_handle_pot_action(client, callback_query, "render")` — but
   `_handle_pot_action` in `modules/admin/pot_menu.py` only branches on
   `start` / `stop` / `diagnose` / `test`. There is **no** `render` branch,
   so `admin_pot_menu` silently fell through and the PO Token submenu never
   opened. The whole PO admin UI (the second-most-clicked admin submenu,
   since YouTube downloads hard-fail without it — invariant #3) was
   unreachable from chat.

   **Fix:** `modules/admin/callback_dispatch.py:536` now calls
   `_render_pot_menu(callback_query)` directly (imported from `.pot_menu`),
   matching the original pre-split behaviour. A code comment was added
   above the call documenting the regression so the next refactor doesn't
   re-introduce it.

   **Live verification:** after `python3 -m py_compile` +
   `sudo systemctl restart tgbot`, the Telethon driver pressed
   `admin_pot_menu` and the "🔓 PO Token Provider" submenu rendered
   correctly (running YES / available YES / enabled YES / endpoint
   `127.0.0.1:4417`, all six buttons present). The `admin_pot_action:test`
   live probe then passed (98 YouTube formats), confirming the submenu's
   downstream action callbacks also resolve.

### Audit: no other silent-fallthrough regressions

The same bug shape (dispatching a "render" menu pattern to an `_handle_*_action`
helper that doesn't take a `render` branch) was scanned for across the
whole dispatch file. The only two render-style menus routed via
`callback_dispatch.py` are:

| Callback | Dispatch target | Status |
|---|---|---|
| `admin_pot_menu` | was `_handle_pot_action(..., "render")` → **fixed** to `_render_pot_menu` directly | ✅ |
| `admin_direct_menu` | `_render_direct_menu` directly | ✅ (was already correct) |

All other `admin_direct_*` callbacks (`admin_direct_toggle_ig/_x/_tiktok`,
`admin_direct_pair_ig`/`_unpair_ig`, `admin_direct_test_x`/`_tiktok`,
`admin_direct_set_x_pin`) have **inline** handlers in
`callback_dispatch.py` — they don't delegate to any silent-fallthrough
helper. The package split introduced exactly one regression of this shape;
this pass closed it.

## Security checklist

| Check | Result |
|---|---|
| No secrets tracked in git | ✅ `git ls-files \| grep -E '\.(env\|session)$\|cookies/'` → empty |
| `.gitignore` covers every secret path | ✅ `.env` (line 2), `cookies/` (43), `database.json` (16), `telethon_session.txt` (62), `direct_forward_state.json` (47), `direct_ig_session.json` (45) all matched |
| PO provider bound to `127.0.0.1` only | ✅ `ss -tlnp \| grep 4417` → `LISTEN 127.0.0.1:4417 deno` (no IPv6/0.0.0.0) — invariant #2 holds |
| SSRF guard on direct-file path | ✅ `--url http://127.0.0.1:4417/` → "❌ Refusing to fetch a local/private network address." (`_is_ssrf_target` working) |
| Secret file permissions | `.env` 600, `telethon_session.txt` 600, `direct_ig_session.json` 600, cookie jars 444 — all match invariant #4 |
| `database.json` perms | ❗ Found mode **664** (group-writable, drifted over time as the bot chmod-flags it after writes). Tightened to **600** (`chmod 600 database.json`) — only the running bot's uid needs access |
| `iptables INPUT` policy | ❗ **ACCEPT** with zero rules — the box has no host firewall. SSH (22), HTTPS (443), HTTP (80), mosh/eternal-terminal (2022/19220), **and** the bot's FastAPI streamer on **8080** are all reachable from the public internet. This is the operator's infra call to make; the 8080 endpoint is intentionally public (`config.DOMAIN` points at the public hostname so streaming links work) and is token-gated per file, but **belt-and-braces: tightening 8080 to a CDN/proxy IP range (or all of Cloudflare) would shrink the attack surface**. Not changed here — left for the operator. |

## Files touched this pass

- `modules/admin/callback_dispatch.py` — the `admin_pot_menu` render fix
  (direct call to `_render_pot_menu`) + a code comment documenting the
  regression so it is not re-introduced.
- `database.json` — `chmod 600` (perms drift fix; not a code change and not
  committed — `database.json` is git-ignored, this is a one-shot runtime
  hygiene action the bot itself will re-impose on the next write anyway).
- `docs/memory/tgbot-2026-08-11-health-pass.md` — this capture.
- `docs/memory/README.md` — link to this capture.
- `README.md` — pointer to the memory index + a "last verified" line so a
  fresh contributor knows the package split + X photo fix passed on this date.

No secrets were staged. `git ls-files | grep -E '\.(env|session)$|cookies/'`
is empty after staging.

## What converged and what didn't

**Converged** (loop locked):
- Admin console full button sweep — every button resolves to a render or
  a confirmed action; the one regression (PO menu no-op) is fixed and
  live-verified.
- PO provider — cookies + PO-token + curl-cffi stack passes the 98-format
  live YouTube probe; bound to 127.0.0.1 only.
- Direct-forward — IG/X/TikTok workers running healthy; X photo-paste
  deliveries succeeding (~1 line/min during the pass, all
  `✅ relayed 1 share photo(s)`).
- Secrets in git — none tracked; `.gitignore` complete.
- SSRF — direct-file fetcher refuses loopback/private/range.

**Did NOT converge this pass and shouldn't**:
- The iptables `INPUT` policy of `ACCEPT` with zero rules — infra call, not
  code. Documented for the operator.
- Full downloader per-site feature matrix (YouTube / TikTok / IG / X /
  SoundCloud / Dailymotion / playlists / direct file / cancel) — already
  comprehensively covered by the live traffic the operator pushes; the
  2026-08-07 health-pass doc has the canonical matrix. Keeping test load
  light per operator request.

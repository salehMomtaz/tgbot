# Health passes — consolidated timeline

All 5 point-in-time health passes merged chronologically. Each section is the original file verbatim under a dated header. See `docs/memory/README.md` for index.

## Sources consolidated

- `docs/memory/tgbot-2026-08-07-health-pass.md`
- `docs/memory/tgbot-2026-08-08-x-selfdm-health-pass.md`
- `docs/memory/tgbot-2026-08-11-health-pass.md`
- `docs/memory/tgbot-2026-08-12-health-pass.md`
- `docs/memory/tgbot-2026-08-25-health-pass.md`

---

---

## 1. Source: `docs/memory/tgbot-2026-08-07-health-pass.md`

# 2026-08-07 full health pass (production box)

Full runtime + feature + security pass on this box (`38.45.80.233`, host
`ubuntu`, user `dev`). The old US test VPS (`66.23.198.52`) is **expired**;
this machine is now the one and only tgbot deployment. Everything below was
tested live by driving the bot from the creator's user account via
`tools/telethon_drive.py` (real inline-keyboard presses), while the bot ran
under `tgbot.service`.

## Box / runtime state

- `date`: box clock is **Fri 2026-08-07 06:xx UTC / 09:xx +0330**. (The
  operator believes it is Aug 8 — the box clock is one day behind. Not a bug,
  just a discrepancy to be aware of when correlating log timestamps.)
- `tgbot.service`: active, `NRestarts=0`, restarted several times during this
  pass to deploy fixes. Disk 14G/96G used (~83 GB free). Load ~1.0.
- Boot sequence is clean: both pyrogram clients on DC4, POT provider healthy
  on `127.0.0.1:4417`, DirectForward resumed the persisted IG session + warmup
  (account_info + direct_threads), sysmon launched, logger linked.

## Log scan findings

- **Network blip 02:48–02:50 UTC**: MTProto reconnect storm
  (`socket.send() raised exception`, `msg_id ... over 300 seconds in the
  past`) resolving cleanly at 02:50:07. Transient, self-healed, no data loss.
- **`upload.SaveBigFilePart` 2 s backoffs**: normal Telegram upload throttle on
  large files. Not an error.
- **One real pre-existing bug**: `admin.py:962` re-editing the 👑 Premium menu
  with identical content raised `400 MESSAGE_NOT_MODIFIED` → full traceback +
  "internal error" alert. **Fixed + live-verified** (see below).
- No ERROR lines since the current boot; no cookie freshness/stale warnings;
  no IG checkpoint freezes.

## Feature tests (all via inline-keyboard presses, all small files ≤50 MB)

| Site | URL used | Result |
|---|---|---|
| YouTube video | `jNQXAC9IVRw` (me at the zoo) | ✅ 240p merged mp4 |
| YouTube audio | same | ✅ `Me at the zoo.m4a` |
| YouTube playlist | `list=PLb911ot23pTQ` (2026 Total Solar Eclipse) | ✅ whole-playlist tier `vl` → 3/4 sent, 4th skipped as private (`Video unavailable. This video is private`) — skip-not-fatal confirmed |
| TikTok | `@scout2015/video/6718335390845095173` | ✅ 1280p mp4 |
| Instagram | `reel/DTvl0HADI-_` (zcairns) | ✅ 1278p DASH reel, exact CDN-probed size |
| Twitter/X | `x.com/i/status/2084701053883232631` | ✅ 1242p mp4 |
| SoundCloud | `soundcloud.com/markronson/uptown-funk` | ✅ 128k m4a (after routing fix) |
| Dailymotion | `dailymotion.com/video/x8e9ry1` | ✅ 288p mp4 (after size fix) |
| Direct file | learningcontainer sample-mp4 | ✅ direct upload 10.5 MB |
| Cancel | YouTube format keyboard → ❌ Cancel | ✅ session cleared, no download |
| Admin console | cookies → back → premium → back → PO → back → close | ✅ 7/7 navigation presses |

## Bugs found + fixed (all live-deployed on this box)

1. **Non-core yt-dlp sites misrouted to direct-file path.** `is_social_media_link`
   whitelisted only the six core domains, so SoundCloud/Dailymotion/Vimeo/etc.
   fell into `download_direct_file`, which did a plain HTTP GET of the **HTML
   page** and uploaded it (SoundCloud → 78 KB "video/mp4"). Fix: widened the
   allowlist to the common yt-dlp media domains. Direct `.mp4`/`.jpg` links
   still go the direct path (`is_link` gate unchanged). This matches the
   per-site jar design (`cookies/ytdlp/<site>.txt`) — a new site just needs its
   domain added here.
2. **Dailymotion (and any HLS) size label lied.** yt-dlp reports a *single
   segment's* byte count as `filesize` for Dailymotion HLS (440 B / 8 061 B),
   so buttons showed `0K`/`8K` while the real file was 220 MB / 17 MB. Fix:
   new `_sane_filesize()` in `utils/downloader.py` nulls a `filesize` that
   implies <1% of the declared tbr (physically impossible), letting
   `estimate_format_size` fall through to its tbr×duration chain. The
   estimator itself is untouched (invariant #11). Also excluded HLS
   (`_is_hls_format`) from CDN probing — a probe of an `.m3u8` manifest would
   measure the manifest, not the file. Verified: button now `~225M` / `~17M`;
   TikTok/YouTube sizes unchanged (real `filesize` unaffected).
3. **`MESSAGE_NOT_MODIFIED` on re-tapping an open admin menu.** Now swallowed
   in the `admin_callback_handler` wrapper (answer the callback quietly,
   skip the traceback + scare alert). Live-verified: tapping premium-menu
   🔄 Refresh twice produced zero tracebacks.
4. **SSRF in the direct-file path (hardening).** `download_direct_file` would
   fetch any http(s) URL, including `http://127.0.0.1:4417/` (the PO provider)
   and RFC1918/link-local ranges. New `_is_ssrf_target()` resolves the host and
   refuses loopback/private/link-local/multicast/reserved/unspecified
   addresses. Live-verified: `http://127.0.0.1:4417/` → "Refusing to fetch a
   local/private network address."
5. **Secret file perms tightened.** `.env`, `telethon_session.txt`,
   `direct_ig_session.json` were `664` (group/world-readable). Now `600`.
   Cookie jars stay locked at `444` (invariant #4).

## Security & hardening checks

- **Auth gate (group −1)**: strict — no `from_user` → stopped; blacklisted →
  stopped; non-authorized → auto-blacklist + log-channel alert + stopped.
  `database.json`: only the creator is authorized; no intruders have ever hit
  the bot (private chat).
- **Git secret hygiene**: `git ls-files` shows no `.env`/sessions/jars;
  `git check-ignore` confirms `.env`, `database.json`, `telethon_session.txt`,
  all cookie jars, IG/X sessions, forward state are ignored.
- **PO provider binding**: `ss -tlnp` shows `127.0.0.1:4417` only
  (invariant #2). Not internet-reachable.
- **Scheme gate**: only `http://`/`https://` are treated as links;
  `file:///etc/passwd` is treated as plain text (no local file read).
- **No secret leakage**: no reply/log path embeds `BOT_TOKEN`/`API_HASH`.

## Test-tool fixes (`tools/telethon_drive.py`)

- `--press` chain used `len(None)` when `--press` was not given → TypeError
  before auto-pick. Guarded.
- `_parse_size_mb` didn't handle `K` sizes (tiny/sub-MB buttons) or the `~`
  estimate prefix. Regex now handles `K/M/G` + optional `~`.
- Press index now increments **before** `await b.click()` — the bot's menu
  edit is often dispatched while the click is pending, so the chain would
  otherwise consume/advance incorrectly. Fixed the admin-console navigation
  chain and the YouTube `--pick v` stall.

## Notes for the next pass

- Vimeo now requires login (`yt-dlp` refuses without cookies); not testable
  without a Vimeo session — Dailymotion covers the "other site" slot.
- The box clock is one day behind the operator's expectation (Aug 7 vs Aug 8).
  If it matters, fix with NTP/timedatectl — out of scope here.

---

## 2. Source: `docs/memory/tgbot-2026-08-08-x-selfdm-health-pass.md`

# 2026-08-08 X self-DM direct-forward health pass

Live end-to-end pass of the rewritten X direct-forward worker
(`modules/direct_forward.py`, self-DM method) against the production bot
(chat `7429671248`, `tgbot.service`, box clock Sat 2026-08-08). The X session
was already live in `cookies/twitter/xcookies.txt` (`auth_token` + `twid`,
expiry 2027); this pass enabled the worker and drove every delivery path with
the operator's real self-DM data plus one fresh media upload.

## Setup done

- `.env`: `X_DIRECT_ENABLED=true` (was absent → worker disabled). The bot was
  restarted via its own SIGTERM path (running as `dev`, `Restart=always`), no
  sudo needed.
- First boot: `[DirectForward/X] first run — priming cursor, backlog is
  skipped.` → `x.last_id` set to the newest self-DM message id.

## What was driven (all via twikit, read-only + one upload)

| Input (self-DM) | Path exercised | Result |
|---|---|---|
| Tweet share card (photo-only, @cuteukeboy) — re-driven by resetting `x.last_id` | `_x_deep_find_tweet` → native `_x_deliver_share_photos` | ✅ 1 photo relayed, header `📥 X DM … 👤 Post by @cuteukeboy` |
| Tweet URL text (video, @oshtru, oshtru/status/1577855540407197696) | `_x_deliver_tweet` → `extract_formats` → `download_media` top 900p → split/upload | ✅ 1.71 MB mp4 relayed, full header + post link |
| Fresh DM photo (re-uploaded real 0.10 MB JPEG via `dm_new` + `upload_media`) | `_x_deep_find_media_url` → `_x_deliver_dm_attachment` (authenticated ton.twitter.com fetch) | ✅ 0.10 MB photo relayed |
| Plain-text self-test messages | route 3 URL scan | ✅ skipped (`no relayable media`) |

## Bugs found + fixed (each live-verified)

1. **`_x_deep_find_tweet` missed legacy share cards.** The real share card is
   `attachment.tweet.status.{id_str, text}` (key is `text`, not `full_text`)
   with the canonical link in `attachment.tweet.expanded_url`. The parser only
   matched GraphQL-style (`rest_id`+`legacy.full_text`) and `id_str`+`full_text`,
   so real tweet shares fell through to the bare-DM-photo path (no author/post
   header). Fix: also match `id_str`+`text`, and fall back to a card-level
   `expanded_url` when no status object is embedded. Verified against the
   operator's actual card before deploying.

2. **httpx `CookieConflict: Multiple cookies exist with name=__cf_bm` killed
   the worker.** `_x_fetch_auth_bytes` used the *shared* twikit `client.http`
   with `follow_redirects=True`. ton.twitter.com is Cloudflare-fronted; its
   `Set-Cookie: __cf_bm` piled a duplicate name into the session jar, and the
   next `dm_conversation` poll died (`dict(self.http.cookies)` itself raises).
   Symptom: everything works for one poll, then the worker goes permanently
   silent (`_x_fetch_self_messages` swallows the exception → returns `[]`).
   Fix: `_x_fetch_auth_bytes` now uses a **throwaway `httpx.AsyncClient`**
   (same base headers + a copy of the session jar, closed afterwards) so
   response cookies never touch the session. Verified by 3+ clean polls after
   ton fetches.

3. **`<500B` payload guard rejected legitimately tiny images.** The operator's
   earlier "photo test" self-DMs were genuinely tiny flat PNGs (133 B / 99 B).
   The guard `len(data) < 500 → invalid` dropped them. Fix: `_x_media_payload_ok`
   validates by **magic bytes** (PNG/JPEG/GIF/WebP/BMP, mp4 `ftyp`), rejecting
   only HTML interstitials — never by size.

## Residual notes

- Old DM-photo URLs in re-driven messages are still fetchable and return the
  real bytes (not expired); the earlier "invalid payload" was purely the
  size-guard false positive, now fixed.
- `_x_fallback_photos` (`get_tweet_by_id`) can raise `KeyError` on some old
  tweet JSON (twikit model quirk) but degrades to `[]` — delivery then fails
  loudly instead of dropping; acceptable.
- yt-dlp's X extractor works with the shared xcookies jar (900p video
  extracted cleanly); two candidate test URLs turned out to embed YouTube
  links (bot's YT sign-in guard fired) — not an X bug.

## Convergence

- `python3 -m py_compile` on all tracked `.py`, `bash -n` on the three scripts:
  pass. No secret tracked (`git ls-files | grep -E '\.(env|session)$|cookies/'`
  empty). Worker stable over multiple poll cycles, zero `ERROR`/`Traceback`
  from the X worker since the fixes.
- Left enabled: `X_DIRECT_ENABLED=true` (intended final state).

---

## 3. Source: `docs/memory/tgbot-2026-08-11-health-pass.md`

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

---

## 4. Source: `docs/memory/tgbot-2026-08-12-health-pass.md`

# 2026-08-12 health pass — full-feature + security sweep

Full runtime + feature matrix + admin UI + security pass on the production
box. Prompted by a fresh context handoff — the operator asked for a
comprehensive health check to verify all systems are operational after the
recent series of fixes (module split, TikTok embed rewrite, X photo fallback,
admin PO menu fix). The pass was done by driving the running bot from the
creator's user account via `tools/telethon_drive.py` (real inline-keyboard
presses), while the bot ran under `tgbot.service`.

## Box / runtime state

- **Host:** user `dev` on Ubuntu 24.04 (the box from AGENTS.md).
- **Box clock:** Wed 2026-08-12 ~02:51–03:02 UTC during the pass.
- **`tgbot.service`:** `active` throughout, `NRestarts=0`, `MainPID=644`
  (started 02:51 UTC).
- **PO provider:** Deno bound to `127.0.0.1:4417` only (verified via `ss
  -tlnp`), `POT_AVAILABLE=true`, healthy.
- **Direct-forward workers:** IG paired with @saleh.momtaz, X enabled with
  `twid` uid 1743868576920928256 + `XCHAT_PIN` set, TikTok enabled with
  `sessionid`. All three running healthy. XChat bridge
  (`tgbot-xchat-bridge.service`) active.
- **Disk** 22%, uptime 11 min. No tracebacks in `logs/bot.log` for the
  current boot.

## Log scan findings (since restart at 02:51 UTC)

- **No Traceback lines** since the current boot.
- **`socket.send() raised exception`** at 02:57:18 — transient pyrogram
  MTProto reconnect, self-healed in <6 s. Not chased, per skill guidance.
- **`upload.SaveBigFilePart wait`** at 02:58:44 — normal Telegram upload
  throttle on the SoundCloud test file. Not an error.
- **Twitter `No video could be found in this tweet`** at 03:02:09 — expected
  warning when the direct-forward worker encounters a photo-only tweet (by
  design; triggers the photo fallback path).
- All `✅ Job Successful` entries correspond to our test runs. Zero `Job
  Failed` entries.

## Feature matrix (all green)

Every feature was driven live via Telethon inline-keyboard presses:

| # | Feature | Test URL / action | Result |
|---|---|---|---|
| 1 | YouTube video pick | `jNQXAC9IVRw` → 240p 617K | ✅ Format keyboard → picked → downloaded → uploaded with thumbnail → delivered |
| 2 | YouTube audio pick | `jNQXAC9IVRw` → 129k 302K m4a | ✅ Audio delivered |
| 3 | Admin console | `console` → navigate all menus | ✅ Cookies/Premium/Direct-Forward/PO Token all render with correct status |
| 4 | SoundCloud audio | `markronson/uptown-funk` → 128k | ✅ m4a delivered |
| 5 | Cancel button | Format keyboard → `dl:*:cancel` | ✅ Cancelled cleanly |
| 6 | YouTube playlist | `PLb911ot23pTQ` → whole + 480p tier | ✅ 4/4 videos delivered |
| 7 | Dailymotion | `x94gmbz` | ⚠️ "Not found" — dead video (yt-dlp error, not bot bug) |
| 8 | Direct file (1) | `proof.ovh/files/1Mb.dat` | ⚠️ 404 — server down (not bot bug) |
| 9 | Direct file (2) | `learningcontainer.com` sample | ✅ Delivered |
| 10 | SSRF guard | 7 test cases (127.0.0.1, 10.x, 192.168.x, localhost blocked; google.com, youtube.com allowed) | ✅ All pass |

The two ⚠️ results are external server failures (dead video / 404), not bot
bugs. The bot correctly reported the errors to the user.

## Admin-console button sweep

Driven via `venv/bin/python tools/telethon_drive.py --message "console"
--press '<pattern>' ...`:

| Pattern pressed | Result |
|---|---|
| `admin_cookies_menu` | ✅ "🍪 Cookie Jars Manager" — YT/IG/TT/X/Global jars + Per-Site Jar + Return |
| `admin_premium_menu` | ✅ "👑 Premium Uploads (4 GB)" — session configured, no whitelisted users |
| `admin_direct_menu` | ✅ Full Direct-Forward submenu — IG paired, X enabled (uid + PIN), TikTok enabled |
| `admin_pot_menu` | ✅ "🔓 PO Token Provider" — running/available/enabled, 127.0.0.1:4417, all buttons present |
| `admin_main` | ✅ Return to main console |
| `admin_close` | ✅ Console closed |

All six admin menus render correctly with accurate status indicators. The
PO Token menu fix from the previous pass (commit `9cd22b5`) is confirmed
working.

## Security checklist

| Check | Result |
|---|---|
| No secrets tracked in git | ✅ `git ls-files | grep -E '\.(env|session)$|cookies/'` → empty |
| `.gitignore` covers every secret path | ✅ `.env`, `cookies/`, `database.json`, `telethon_session.txt`, `direct_forward_state.json`, `direct_ig_session.json` all matched |
| PO provider bound to `127.0.0.1` only | ✅ `ss -tlnp | grep 4417` → `LISTEN 127.0.0.1:4417 deno` (no IPv6/0.0.0.0) |
| SSRF guard on direct-file path | ✅ All 7 test cases pass — blocks loopback/private/link-local, allows public hosts |
| Secret file permissions | `.env` 600 ✅, `telethon_session.txt` 600 ✅, IG cookies 444 ✅ |
| No token/key leakage in logs | ✅ No `BOT_TOKEN`/`API_HASH` values found in log output |

## Direct-forward workers (startup log verification)

- **Instagram:** session resumed from `direct_ig_session.json`, warmup
  complete, polling @saleh.momtaz every ~300 s with jitter ✅
- **X/Twitter:** self-DM polling conversation
  `1743868576920928256-1743868576920928256` ✅
- **TikTok:** IM WebSocket connected to `im-ws-sg.tiktok.com`, cmd-1001
  sent ✅
- **XChat bridge:** `tgbot-xchat-bridge.service` active, wrapper supervisor
  running ✅

## What converged and what didn't

**Converged** (loop locked):
- Full feature matrix: YouTube (video + audio + playlist), SoundCloud,
  direct file, cancel — all delivered successfully.
- Admin console: every menu renders with correct status.
- PO provider: healthy, cookies + PO-token + curl-cffi stack operational.
- Direct-forward: IG/X/TikTok workers all running, XChat bridge active.
- SSRF guard: all test cases pass.
- Security: no secrets in git, correct file permissions, provider bound to
  localhost.

**Did NOT converge this pass and shouldn't**:
- Dailymotion dead video (`x94gmbz`) — external server issue, not a bot
  bug.
- `proof.ovh` 404 — server down, not a bot bug.
- Full TikTok/Instagram/X download tests — not driven this pass (live
  traffic from the operator covers these continuously; keeping test load
  light per operator request). The 2026-08-07 health-pass doc has the
  canonical per-site matrix.

## Files touched this pass

- `docs/memory/tgbot-2026-08-12-health-pass.md` — this capture.

No code changes were made. No secrets were staged.

## Historical traceback audit (pre-02:51 boots) — 2026-08-12 follow-up

A second pass on the same day re-scanned `logs/bot.log*` end-to-end: **46
`Traceback` entries total, ALL from boots before the 02:51 restart** (i.e. the
pre-refactor / refactoring-era runs). The current boot (and this pass) produced
**zero** tracebacks. Categorised by root cause — every class is benign or
already fixed by a later commit:

| Count | Signature | Disposition |
|---|---|---|
| 18 | `RuntimeError: … no video formats (photo-only tweet handled natively)` | Pre-fix X photo path; commit `3cb497b` rewrote it into the clean `_x_fallback_photos` flow — no longer raised. |
| 21 | `yt_dlp … [TikTok] … Unexpected response from webpage request` | Pre-`/embed/<id>` rewrite (commits `e48b060`/`c3b156c`/`7c20c30`). Current run shows no TikTok extraction errors. |
| 3 | `KeyError: 8877223559` | Historical; not reproducible in current boot. |
| 1 | `Dailymotion … Access forbidden` | Site-side geo block, not a bot bug. |
| 1 | `Direct Upload Error … 404` | Dead test link (operator-sent), not a bot bug. |
| 2 | `DM attachment invalid payload (133B/99B)` | Intended magic-byte guard (`_x_media_payload_ok`) rejecting HTML interstitials — by design (invariant #13b). |
| 1 | `YouTube … sign-in required / no cookies` | Operator had not yet uploaded `ytcookies.txt`; fixed by jar upload. |

**Conclusion:** the refactor (module package split) introduced no regressions;
the 46 tracebacks are all stale, pre-fix artifacts. The live bot is clean.

## Doc-path coherency fixes (this follow-up pass)

The package split (commit `81a5139`) left stale `module.py::func` references in
the prose of the contributor/design docs. Fixed to point at the new package
layout (verified against actual `def` locations):

- `AGENTS.md` — invariants prose: `utils/downloader.py::_apply_pot_options` →
  `url_normalize.py`; `::is_playlist_url` → `playlists.py`; `get_cookies_for_url`
  / `_site_cookie_context` → `cookies.py`; `modules/direct_forward.py` →
  `modules/direct_forward/`; `::_tiktok_worker` → `tiktok.py`;
  `::_find_thumbnail_file` → `thumbnails.py`; best-audio sort → `formats.py`;
  `modules/admin.py` → `modules/admin/`.
- `blueprint.md` — `utils/downloader.py::_apply_pot_options` → `url_normalize.py`;
  `::get_cookies_for_url` → `cookies.py`; `::extract_formats` → `formats.py`;
  Phase 16 `modules/direct_forward.py` → `modules/direct_forward/`; added
  **Phase 18** (TikTok self-DM + package refactor).
- `README.md` — Direct-Forward section now lists **Instagram / X / TikTok**
  self-DM (TikTok IM-WebSocket relay was missing from the user-facing docs).

`install.sh` / `run.sh` already match the package layout (no stale paths;
`chmod +x` + xchat-bridge `enable` present). Deployment script is current.

---

## 5. Source: `docs/memory/tgbot-2026-08-25-health-pass.md`

# 2026-08-25 — Routine health pass (no code changes)

## Scope

No task was assigned this session; this was a scheduled health/maintenance pass.
The working tree was clean and in sync with `origin/main` at `4851074`, so the
pass was verification-only. **No source, config, or deployment changes were made
on purpose** — nothing needed fixing.

## Verification results

- `systemctl`: `tgbot` active (0 restarts), `tgbot-xchat-bridge` active,
  `cookie-watch` active, `tgbot-monitor` inactive **by design** (the bot spawns
  a detached monitor at startup; the systemd unit is opt-in).
- `python3 -m py_compile $(git ls-files '*.py')` — clean.
- `bash -n install.sh run.sh uninstall.sh tools/start_xchat_bridge.sh` — clean.
- `cd cmd/tgbot-monitor && go test ./...` — ok.
- `logs/bot.log`: X self-DM polls returning 200 on cadence, cookie refresher
  skipping all four jars (<20h freshness), yt-dlp updater up to date
  (2026.08.20.234504). No tracebacks.
- `cache/xchat_inbox.jsonl`: 451 lines, zero duplicate lines.

## Operational finding (needs a HUMAN, not code)

The Instagram direct-forward worker has been cycling through checkpoint
challenges since 2026-08-24 ~19:10 (`Manual verification required via Instagram
native challenge flow`, then once `Please wait a few minutes before you try
again`). Each wake-up retries login, hits the wall again, and freezes another
~4 h. This matches the documented design (freeze 3–5 h, no retry storms); the
only fix is a human passing the checkpoint in the official Instagram app on the
bot account, then restarting the bot for a clean resume. The agent deliberately
did NOT touch the IG session/jar/proxy — retry gymnastics against an active
checkpoint make the account's standing worse.

## Gotcha learned: never execute `tools/start_xchat_bridge.sh` by hand

While syntax-checking scripts, the supervisor wrapper was accidentally
*executed* instead of only `bash -n`-checked. Because it is a resident loop that
(re)spawns the Deno sidecar whenever its gates hold, this briefly created a
SECOND `deno run -A xchat_bridge.mjs` alongside the one owned by
`tgbot-xchat-bridge.service`. Cleanup: killed the orphaned PID; verified the
legit bridge (child of the systemd wrapper) stayed up and the inbox file gained
no duplicates.

Rule for future agents: `tools/start_xchat_bridge.sh` is owned by systemd.
Interact with the bridge only via `systemctl status/restart tgbot-xchat-bridge`;
at most use `bash -n` for syntax checks — never run the wrapper directly while
the unit exists, or you fork a duplicate sidecar.

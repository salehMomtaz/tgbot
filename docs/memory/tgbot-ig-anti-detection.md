# Instagram anti-detection posture (the "automated behavior" flag)

**Date:** 2026-08-03 · **Area:** `modules/direct_forward.py`, `config.py`,
`docs/DIRECT_FORWARD_SETUP.md`

## What happened

The bot account's Instagram session was checkpointed with *"We suspect
automated behavior on your account"*. The DM-relay poller (`instagrapi`
private API from a datacenter VPS) was the loudest signal: a **fixed 120 s
cadence**, an inbox sweep of **20 thread items every cycle regardless of
activity**, and machine-paced requests.

## What the research says actually matters (instagrapi docs + community)

Ranked by evidence: (1) fresh device fingerprint per run → persist ONE session
file and never re-login; (2) datacenter ASNs are inherently suspicious → one
stable residential proxy if needed, never rotating; (3) login churn (`login()`
every boot) → resume `direct_ig_session.json`; (4) impossible travel (owner +
VPS in different countries) → keep the session/IP identity stable; (5) sudden
activity spikes → jitter + minimal call volume; (6) web-scraping endpoints →
stay on private API. Full source list lives in this repo's session notes and
`docs/DIRECT_FORWARD_SETUP.md` ("Avoiding checkpoints").

## Hardening implemented

| Lever | Implementation |
|---|---|
| Cadence | `DIRECT_FORWARD_POLL_SECONDS` default 120→**300**, each interval randomized **±40%** (`_poll_interval()`); never below 60 s |
| Request pacing | `cl.delay_range = [2, 4]` (was [1, 3]) — every private-API call |
| Idle volume | per-thread **`last_activity_at` watermarks** in state (`thread_activity` map): unchanged thread ⇒ **0** item fetches (was ~20/cycle) |
| Identity | unchanged: persisted session/device first, sessionid bootstrap 2nd, password last; settings re-dumped per cycle |
| IP | `DIRECT_FORWARD_PROXY` — ONE stable proxy for the account's whole life; applied to instagrapi (`set_proxy`) and twikit (guarded) |
| Checkpoint | freeze **3–5 h randomized**, log tells the human to pass it in the official app; no retry storms |
| Login resilience | **worker never dies on a login failure** — retries each poll with a *fresh* client (half-failed instagrapi login poisons state), so a mid-run `igcookies.txt` re-upload is picked up without a bot restart; challenge errors still freeze 3–5 h |

## Operational notes

- Deleting `direct_ig_session.json` is the #1 self-inflicted checkpoint
  trigger — it forces a brand-new device fingerprint.
- The igcookies jar (yt-dlp) and the DM session are the same account: heavy
  yt-dlp cookie usage ALSO feeds the account's risk score; the no-auth-first
  Instagram ladder exists partly for this reason.
- Playwright+stealth browser automation is **not** the right layer here — we
  don't scrape Instagram web; we speak the private mobile API. (Browser stealth
  would matter only for scraping instagram.com pages, which yt-dlp handles
  with its own challenge solvers.)
- X (twikit) shares the poll-jitter + proxy, but X's DM API is far less
  policed; no watermarking there (single-thread history call already).

## Second checkpoint (2026-08-05 01:05 UTC) — what the deeper research found

Seven hours of normal relayed traffic, then a **manual-verification
checkpoint** on a single `direct_v2/inbox` poll (`[400]` + `challenge_required`).
The poller did everything right (jitter, watermarks, no retry storm, froze
~3.6 h). So the trigger wasn't cadence — it was **identity correlation**.
Research from `reference/` (okgram, insta-wizard, instaharvest_v2) + instagrapi
issue/PRs:

1. **TLS/HTTP fingerprint (top suspect).** instagrapi 2.18.12 speaks the
   private API over plain `requests.Session` = **Python OpenSSL JA3/JA4 + HTTP/1.1**.
   Instagram's WAF fingerprints the TLS handshake: a "Pixel 8 Pro Android"
   UA riding Python's TLS stack is a detectable mismatch. 2026 fixes swap the
   transport for a browser/OkHttp impersonation layer (curl_cffi / httpcloak /
   tls_client). Current session confirms it: `device=Pixel 8 Pro`, UA app
   `428.0.0.47.67`, but the connection layer is Python.
2. **Routing headers not echoed.** The saved session has `ig_u_rur: False`
   (and no `shbid/shbts/direct-region-hint`). okgram treats **echoing
   `IG-U-RUR`/`X-MID`/`X-IG-WWW-Claim` from every response** as a first-class
   fix for sessionid logouts — a missing `IG-U-RUR` is a top bounce cause.
   instagrapi 2.18.12 does NOT capture/replay these.
3. **Geo coherence is currently OK** (VPS IP = US/PA, session = US/en_US/
   GMT-04:00) — so the checkpoint was NOT impossible-travel. But the IP is a
   **datacenter ASN** (Redoubt Networks); DC IPs are inherently higher-risk.
4. **Challenge type matters.** This was a *native* manual-verification
   checkpoint (`flow_render_type` native), not the email/SMS code flow that
   `challenge_code_handler` can pass — so only a human in the official app
   resolves it; freezing was correct. insta-wizard's `challenge.py` taxonomy
   (VettedDelta / UFAC / scraping-warning) is the reference for classifying.

### Concrete mitigations (all applied 2026-08-05)

Implemented in `utils/ig_anti_detect.py` (wired into `_make_client` /
`_ig_login` in `modules/direct_forward.py`). Every piece is independently
failing-safe: a missing dependency or a library change only logs a warning and
the worker keeps running on the previous behaviour.

| # | Lever | Status |
|---|---|---|
| 1 | **Impersonated transport** for instagrapi — `CurlCffiAdapter` (from the `curl-adapter` PyPI package, the same adapter instagrapi's own public-transport `curl` extra uses) mounted on `cl.private`. `cl.private` stays a `requests.Session` so cookies/proxies/verify/headers all keep working; only the TLS layer impersonates `chrome136`. Requires `pip install "curl-adapter>=1.2.1"` (added to requirements.txt) + a compat shim (curl_cffi 0.15 renamed `normalize_browser_type` → `resolve_latest_browser_type`). `_configure_private_session_retry` is patched once so every `load_settings`/`login_by_sessionid` re-applies the transport instead of silently re-mounting the stock HTTPAdapter | **done** |
| 2 | **Echo `IG-U-RUR`/`X-MID`/`X-IG-WWW-Claim` + persist them** — `install_token_echo` wraps `cl.private_request` to capture `ig-set-ig-u-rur`, `ig-set-ig-u-shbid`, `ig-set-ig-u-shbts`, `x-ig-set-www-claim`, `ig-set-x-mid` from every response into `cl.settings` (durable via the per-poll `dump_settings`), and patches `base_headers` + `get_settings` (instagrapi natively serializes rur/www_claim/mid but DROPS shbid/shbts) to re-apply captured values | **done** |
| 3 | **Sticky residential proxy** (`DIRECT_FORWARD_PROXY`) — DC ASN is the residual risk | knob exists; test VPS refreshes cookies through the SOCKS5 proxy whose egress = the bot's own IP |
| 4 | **geo/locale/device explicit sync** via `set_country/set_country_code/set_locale/set_timezone_offset` (`pin_geo`), driven by `IG_DIRECT_COUNTRY`/`IG_DIRECT_COUNTRY_CODE`/`IG_DIRECT_LOCALE`/`IG_DIRECT_TZ_OFFSET`/`IG_DIRECT_TZ_NAME` (defaults US / 1 / en_US / -14400 / GMT-04:00) | **done** |
| 5 | Keep the 3–5 h freeze on native checkpoints, **and alert the relay chat directly** (not just the log channel) with instructions to pass the verification in the official app | **done** |
| 6 | **Cold-start warmup** — `warmup()` runs a few paced benign reads (`account_info`, `direct_threads(5)` ×3) right after login so the first real poll isn't the session's first activity on a fresh IP | **done** |
| 7 | **Burst pacing on backfill** — `burst_pace(n)` returns a per-item sleep that scales with the backfill size (`base = 6 + log2(n+1) * 2` seconds, capped at 30s). Applied in the gap-fetch loop. For a 30+ item backfill (the pattern that previously triggered "we suspect automated behavior") the cumulative activity now spaces over 5-7 minutes instead of 2.5 — the live 1-item case adds <2 s | **done** |
| 8 | **Cold-start jitter** — `cold_start_jitter(cl)` runs AFTER warmup, before the first real poll: `account_info` → 60-90 s → `direct_threads(20)` → 45-90 s → `direct_threads(20)`. The first paired-thread poll is no longer the very first observable activity on a new session | **done** |
| 9 | **Public-GraphQL soft-block counter** — `record_public_soft_block()` / `public_soft_block_active()`. Counts consecutive `JSONDecodeError`s on `cl.media_pk_from_url` (the public web endpoint). After 3 strikes, skip the public path for a 10 min cooldown — hammering a throttled endpoint only deepens the block | **done** |
| 10 | **Email-change alert handler** — `install_email_change_alert(cl, alert_sink=...)` registers a `change_password_handler` that, when invoked, alerts the operator (via the direct-forward chat) and re-raises. We do NOT attempt to bypass the password reset programmatically (that deepens the flag) — we freeze the worker per the existing challenge policy and tell the operator what to do in the official app | **done** |

### Deployment facts (2026-08-05, test VPS)

- `curl-adapter==1.2.1` installed in the VPS venv; `curl_cffi==0.15.1b2` (yt-dlp
  extra, left untouched).
- Verified end-to-end in the running bot: transport swaps to `CurlCffiAdapter`
  and survives `load_settings` re-mounts; `login_by_sessionid` and the
  persisted-session resume both work through the impersonated transport;
  warmup runs after resume; worker entered the idle `~300 s ±40%` poll loop with
  `NRestarts=0`, no checkpoint, geo persisted (`US / -14400 / en_US`), `mid`
  captured. `ig_u_rur`/`shbid` were not set in that run (Instagram only sends
  them on certain responses); instagrapi's synthetic values remain the fallback
  and the capture persists real values whenever the server does send them.
- Gotcha hit during wiring: wrapping `cl.private_request` must NOT re-pass
  `self` (the captured `orig_request` is already a bound method) — passing it
  shifted the endpoint arg and blew up with `'Client' object has no attribute
  'startswith'`.

### Reference clones (untracked, in `reference/`)

- `reference/instagrapi` — upstream 2.18.12 (same as installed); read
  `auth.py`/`private.py` for the session/header mechanics.
- `reference/okgram` — the **phone-grade** reference: device pool, stable
  UUIDs, `IG-U-RUR` echo + persistence, geo auto-sync, `doctor` diagnostics,
  rate governor. Best source for what the app's fingerprint actually looks like.
- `reference/insta-wizard` — async client with checkpoint taxonomy +
  challenge section (`mobile/sections/challenge.py`), proxy rotation.
- `reference/instaharvest_v2` — curl_cffi transport + challenge resolver +
  anti-detect system (14-layer fallback GraphQL/mobile/web).

All four are gitignored via `.gitignore` → `reference/`; they're local research
material, not vendored dependencies.

### Third recurrence (2026-08-28) — burst-pacing + cold-start + soft-block + email-handler

Operator reported the **second** recurrence of "we suspect automated behavior
on your account" + forced email change. All 6 levers above were live
(verified via log lines: `private transport now impersonates chrome136`,
`geo pinned to US / en_US / GMT-04:00`, `warmup: account_info ok` × 3, etc.),
and the fresh cookie upload + cursor-gap recovery worked correctly. The
auto-check log showed: `gap fetch: thread 34028... had 51 items, 33 new
after cursor 32977...` followed by 33 items delivered at a near-uniform
4-6 s cadence over ~150 s.

**Root cause:** even with `cl.delay_range = [2, 4]`, the bot's own
`await loop.run_in_executor(None, cl.private_request, ...)` calls in
the gap-fetch loop are NOT subject to `delay_range` (delay_range only
applies to instagrapi's *internal* calls, not to the bot's direct
private_request calls). So all 33 calls fired in a tight burst, looking
exactly like scripted scraping to Instagram's behavior model.

**The four new levers (7-10) close the burst + first-activity + email
bypass paths.** Each is independently failing-safe (degrades to a
no-op on a missing dep / library change) and wired into
`_instagram_worker` in `modules/direct_forward/instagram.py`.

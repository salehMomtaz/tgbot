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

### Concrete mitigations (in priority order, not all applied yet)

| # | Lever | Status |
|---|---|---|
| 1 | **Impersonated transport** for instagrapi (curl_cffi/httpcloak/TLS-client) so the TLS fingerprint matches a phone — the single biggest 2026 lever | pending (needs an instagrapi fork/patch; upstream PR #2364 was closed unmerged) |
| 2 | **Echo `IG-U-RUR`/`X-MID`/`X-IG-WWW-Claim` + persist them** in the session bundle | pending (upstream instagrapi gap) |
| 3 | **Sticky residential proxy** (`DIRECT_FORWARD_PROXY`) — DC ASN is the residual risk | config knob exists, not set on test VPS |
| 4 | **geo/locale/device explicit sync** via `set_country/set_locale/set_timezone_offset` so the session never drifts from the account's home region | can add (instagrapi API exists) |
| 5 | Keep the 3–5 h freeze on native checkpoints; optionally alert to the log channel when one hits (currently only `logs/bot.log`) | could add |

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

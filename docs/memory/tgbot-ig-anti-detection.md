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

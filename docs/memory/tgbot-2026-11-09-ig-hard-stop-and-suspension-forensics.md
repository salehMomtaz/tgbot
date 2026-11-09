# 2026-11-09 — Instagram suspension forensics + hard "zero traffic after rejection" latch

Operator brief: the previous session's Instagram account was permanently
suspended, then a **fresh jar was uploaded on 2026-11-07** and the operator's
**main 2015 account (the self-DM account) was suspended + video identity
verification** within ~17 hours. The operator did NOT upload fresh cookies
again and asked for one of two outcomes: a real fix, or an exact explanation of
how/why it happens. They also gave a hard rule:

> *"do not allow any kind of attempt when bot faces any problem related to
> instagram cookies — it won't do relogin attempt, it fucking notifies me once
> and fucking waits until I upload fresh cookies."*

Both were delivered: the suspension mechanism is reconstructed below from
`cookies/history.jsonl` + `logs/bot.log*`, and the bot now makes **zero**
authenticated Instagram calls once a session is rejected.

## Snapshot / rollback

- Baseline commit: `1b3371125ec7210cd3b9d2076ddd3d42f99b1434` (`main`, clean tree).
- Services at start: `tgbot` (PID 654), `tgbot-monitor`, `tgbot-xchat-bridge`,
  `cookie-watch` — all `active (running)`.
- Rollback: `git checkout 1b33711` + `kill -TERM $(systemctl show tgbot --property=MainPID --value)`.

## The exact "why did it try twice?" answer (2026-11-09 10:53)

`modules/direct_forward/instagram.py::_ig_login` had a two-step fallback:

1. load `direct_ig_session.json` → `cl.account_info()` → Instagram answered
   **403 login_required** (attempt #1, a read-only probe with the dead session);
2. the `except` block then called `cl.login_by_sessionid(jar_sessionid)` **with
   the same dead sessionid**. instagrapi's implementation of that call is a
   ladder: `user_info_v1()` (403) → `_user_short_from_private_stream()` (403) →
   `user_short_gql()` (follows the login redirect until) **`Exceeded 30
   redirects`** (attempt #2 and its internal requests).

Friend Media independently probed the same persisted session at 10:53:17, and
the headless cookie refresher visited instagram.com with the same dead
sessionid at 11:00:50. That is the "it keeps trying" behavior the operator saw.

## Suspension forensics (2026-11-07 17:42 → 2026-11-08 10:59)

The fresh jar was uploaded at **2026-11-07 17:42:01Z** (sessionid
`2210…Dtkg`, `ds_user_id 2210…3164`). The bot logged in at 18:12:31 via
`login_by_sessionid`. The session died at **2026-11-08 10:59:29Z** (`403
login_required` on `direct_v2/inbox`). The account was suspended and later
recovered via video identity verification.

### Evidence from `logs/bot.log*` (17h window)

| Metric | Value |
|---|---|
| authenticated `i.instagram.com` private requests | **1,237** in ~17 h |
| first hour after login (18:00) | **259** requests |
| `GET /api/v1/direct_v2/inbox/` | 258 (5-min poll + restarts) |
| `GET /api/v1/accounts/current_user/` | **105** (restart/warmup/FriendMedia probes) |
| `GET /api/v1/feed/user/<id>/` + `/story/` | ~800 across **10 friends** (Friend Media hourly cycle) |
| restarts in the window | ~15 (each = ~4 `account_info` + warmup + jitter) |

### Device / identity evidence

One sessionid was used by **3–4 distinct device identities**:

1. `direct_ig_session.json` — Android device A (`phone_id 4d57…`, `uuid 51b8…`,
   `android_device_id android-b332…`).
2. `friend_media_ig_session.json` — Android device B (`phone_id 7f9b…`,
   `uuid 7492…`, `android_device_id android-d8ea…`).
3. Headless Chromium (cookie refresher) — a **brand-new profile every run**;
   the log itself says *"headless profile seen as a NEW device (datr, ig_did,
   mid differ)"*.
4. yt-dlp — a web transport sharing the same jar.

### Network / geo evidence

- VPS egress: **38.45.x.x, Prague, CZ, Coolhousing ASN** (datacenter).
- The bot pins `X-IG-App-Startup-Country: US`, `X-IG-Timezone-Offset: -14400`
  (US Eastern), locale `en_US` — i.e. the IP says CZ, the headers say US.
- The uploaded jar's `rur` was `LDC,…`; the bot's first login wrote back
  `RVA,<uid>,…` — a region/route change visible to Instagram.
- `DIRECT_FORWARD_PROXY` was **not set**: every request came from the
  datacenter IP.

### Root cause (ranked, high confidence)

1. **Datacenter IP + a fresh session for an old account.** A 2015 account that
   suddenly logs in from a hosting ASN is the strongest account-takeover signal.
   Instagram's answer to that is a checkpoint; repeated automated activity
   escalates it to suspension.
2. **Request volume/cadence.** 259 authenticated calls in the first hour, then
   ~45–190/hour — dominated by Friend Media scraping 10 friends' feeds/stories.
   No human account behaves like that.
3. **Multi-device identity on one sessionid.** The previous fix (2026-11-06)
   stopped *login* churn (578 `instagrapi_writeback` events) but left two
   persisted Android identities, an hourly new-device headless browser, and
   yt-dlp all sharing one session.
4. **Transport mismatch.** A session exported from a browser was driven through
   the Android private API with a fresh instagrapi device identity
   (`X-IG-Device-ID`/`phone_id` do not match the session's device binding).
5. **Restart storms.** ~15 restarts in 17 h multiplied profile probes and
   warmups.

This is the "how it happens": Instagram correlates IP reputation, device
identity, request volume and cadence; the combination trips an automation
checkpoint, and any retry against the flagged session deepens enforcement until
suspension. It is not one single request that bans an account.

## Fixes shipped (this session)

### Hard stop (operator's explicit rule)

New `utils/ig_session_guard.py`:

- A persistent latch at `cache/ig_session_dead.json`, keyed on the **sessionid
  VALUE signature** (never mtime), protected from the cache cleaner.
- While latched: `modules/direct_forward/instagram.py`,
  `modules/friend_media/instagram.py`, `utils/cookie_refresher.py` and
  `utils/downloader/cookies.py` make **zero** Instagram calls.
- `mark_dead()` returns True only for the first detector → **one** operator DM,
  even across restarts. A new sessionid in the jar auto-clears the latch.
- `_ig_login` no longer falls back to `login_by_sessionid` when the persisted
  session and the jar carry the **same** rejected sessionid; an *older*
  persisted session is not probed at all — the fresh jar gets exactly **one**
  bootstrap login.
- Challenge/PleaseWait now latches instead of a timed retry storm.
- The relay watchdog treats a latched IG as intentional silence (no false
  "stalled" DM).

### One identity + auto-geo + volume (2026-11-09 follow-up)

- **ONE device identity only.** Both IG workers use `direct_ig_session.json`;
  `friend_media_ig_session.json` was deleted and is never created again. A
  fresh jar now KEEPS the persisted uuids/device identity and only replaces the
  session cookies — `modules/admin/cookies.py` no longer deletes the session
  file on upload, and `_ig_login` loads the device identity without probing the
  stale session.
- **yt-dlp never gets the IG jar** unless `IG_YTDLP_AUTH_ENABLED=true` (default
  **false**). A web transport on the same sessionid is a second identity;
  public posts/reels still download anonymously.
- **Geo auto-detection** (`utils/ig_geo.py`): the egress IP is resolved via
  ipinfo.io (ip-api.com fallback) and cached for 7 days in `cache/ig_geo.json`.
  On this box it resolves to **CZ / 420 / cs_CZ / Europe/Prague (+3600s)**.
  Any non-empty `IG_DIRECT_*` value overrides one field. No more US headers on
  a CZ connection.
- **Volume dial**: `IG_DIRECT_POLL_SECONDS` default **900** (jittered ±40%) →
  ~4 idle `direct_v2/inbox` requests/hour instead of 12. An idle poll is ONE
  request; the thread-activity watermark already makes an unchanged thread cost
  zero extra calls. `warmup` reduced to one step (2 calls); the cold-start
  jitter adds 3. `COOKIE_REFRESH_IG=false` (headless browser never touches IG).
- `.env` on this box: `FRIEND_MEDIA_IG_ENABLED=false` (the archiver was the
  dominant API consumer: ~200 calls/hour for 10 friends). Telegram archiving is
  untouched.
- Current jar is pre-latched dead; the bot will stay completely silent on IG
  until a fresh sessionid is uploaded.

### Polling: what else can be done

- **Current (cheap) path**: 1 authenticated inbox request per poll, 900 s ±40%
  jitter. That is ~96 requests/day while idle, versus 288/day at 300 s.
- **MQTT push — IMPLEMENTED 2026-11-09** (`IG_DIRECT_MQTT_ENABLED`, default
  false; enabled in this box's `.env` so a fresh jar activates it). The real
  app's channel is a persistent MQTToT/IRIS connection; while it is healthy the
  worker polls only when a push arrives, plus a reconciliation poll every
  `IG_DIRECT_MQTT_FALLBACK_SECONDS` (default 1800 s). Safety model:
  - wraps the **same authenticated `cl`** (same `sessionid`, `phone_id`,
    `user_agent`, locale, proxy) — no `IGClient()`, no login call anywhere;
  - `direct_subscribe()` is the ONLY HTTP request (one inbox read for
    `seq_id`/`snapshot_at_ms`); the reader thread only reads the socket, sends
    PINGREQ every 15 s and signals the asyncio loop;
  - typing/presence/seen events are ignored;
  - the worker owns a **bounded** reconnect (default 3, jittered backoff). A
    plain network failure falls back to the polling loop; an auth-classified
    failure (including MQTT CONNACK 4/5) arms the session-dead latch and stops
    everything. There is no recursive login.
  - Tests: `tests/test_ig_realtime.py` (same-client use, no login call in the
    AST, noise filter, auth classification).
- **Zero-IG option**: the Telegram bot already accepts links directly. If the
  goal is just relaying media, sending the link to the bot in Telegram needs no
  Instagram session and carries no suspension risk at all.

### Tests

- `tests/test_ig_session_guard.py` — latch keyed on sessionid, one-shot notify,
  auto-clear, auth classification, persisted/ jar comparison.
- `tests/test_ig_login_no_retry.py` — the exact 2026-11-09 bug: a dead persisted
  session with the same sessionid must produce **no** `login_by_sessionid`;
  an armed latch makes **zero** calls; an older persisted session is not
  probed; a transient timeout never arms the latch.
- `tests/test_ig_session_gate.py` — yt-dlp jar handoff is gated on the latch.

## Before reconnecting a new account (operator checklist)

1. **Residential proxy is ideal but optional.** The operator cannot afford the
   traffic, so the fallback is: the geo, identity and volume signals are now
   clean, and the residual risk is the datacenter ASN itself. The only truly
   zero-IG-risk alternative is to send links to the Telegram bot directly (it
   already accepts links) — no IG session, no polling, no suspension surface.
2. **Geo is auto-detected** (CZ/Prague on this box). No manual values needed.
   Only set `IG_DIRECT_COUNTRY`/`IG_DIRECT_COUNTRY_CODE`/`IG_DIRECT_LOCALE`/
   `IG_DIRECT_TZ_OFFSET`/`IG_DIRECT_TZ_NAME` if the account's real home region
   differs from the VPS.
3. Create the session on the **same egress** (or at least the same country) the
   bot will use; do not move a session between countries.
4. **Never reuse a session that hit a checkpoint/suspension.** Export a NEW
   session after passing verification. Let the account rest/behave normally for
   a day before connecting the bot.
5. Keep `FRIEND_MEDIA_IG_ENABLED=false` for at least the first days; enable it
   only after the DM relay has been stable. The archiver is the volume bomb.
6. Keep `IG_DIRECT_POLL_SECONDS` at 900–1800 for the first week.
7. Do not run the account's app and the bot's API client as competing
   identities during the first day if possible.

## Left open / not verified

- No live Instagram login was attempted (operator said they are not uploading
  cookies yet; `IG_AUTH_ENABLED` stays true but the latch keeps traffic at
  zero).
- The exact Instagram classifier thresholds are not public; the ranking above
  is an evidence-based reconstruction, not a court-grade proof.
- `certbot` certs for the operator's x-ui domains remain outside tgbot scope
  (pre-existing manual-DNS renewal issue from 2026-11-06).

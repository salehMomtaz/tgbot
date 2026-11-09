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

### Removing the device/volume triggers

- `COOKIE_REFRESH_IG=false` (new config, default **false**): the headless
  browser no longer touches instagram.com. instagrapi's write-back already
  rotates `sessionid`/`csrftoken`/`ds_user_id`/`mid`/`rur`.
- Friend Media now persists the **shared** `direct_ig_session.json` instead of
  minting a second Android identity; the bootstrap is serialized by a lock so a
  fresh jar produces one device, not two.
- `.env` on this box: `FRIEND_MEDIA_IG_ENABLED=false` (the archiver was the
  dominant API consumer: ~200 calls/hour for 10 friends). Telegram archiving is
  untouched.
- Current jar is pre-latched dead; the bot will stay completely silent on IG
  until a fresh sessionid is uploaded.

### Tests

- `tests/test_ig_session_guard.py` — latch keyed on sessionid, one-shot notify,
  auto-clear, auth classification, persisted/ jar comparison.
- `tests/test_ig_login_no_retry.py` — the exact 2026-11-09 bug: a dead persisted
  session with the same sessionid must produce **no** `login_by_sessionid`;
  an armed latch makes **zero** calls; an older persisted session is not
  probed; a transient timeout never arms the latch.
- `tests/test_ig_session_gate.py` — yt-dlp jar handoff is gated on the latch.

## Before reconnecting a new account (operator checklist)

1. **Use a residential proxy in the account's country** and set
   `DIRECT_FORWARD_PROXY` to it BEFORE the first login. This is the single
   biggest mitigation; a datacenter IP will reproduce the same outcome.
2. **Pin geo to the account's real region**, not US: `IG_DIRECT_COUNTRY`,
   `IG_DIRECT_COUNTRY_CODE`, `IG_DIRECT_LOCALE`, `IG_DIRECT_TZ_OFFSET`,
   `IG_DIRECT_TZ_NAME`. Mismatch (CZ IP + US headers) is a red flag.
3. Create the session on the **same egress** (or at least the same country) the
   bot will use; do not move a session between countries.
4. **Never reuse a session that hit a checkpoint/suspension.** Export a NEW
   session after passing verification. Let the account rest/behave normally for
   a day before connecting the bot.
5. Keep `FRIEND_MEDIA_IG_ENABLED=false` for at least the first days; enable it
   only after the DM relay has been stable. The archiver is the volume bomb.
6. Consider raising `IG_DIRECT_POLL_SECONDS` from 300 to 900–1800 (fewer,
   less-regular inbox polls).
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

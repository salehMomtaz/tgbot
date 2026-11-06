# 2026-11-06 — Autonomous health audit + Instagram suspension root cause (auto-login annihilation)

Operator brief: full production health check, ~20k-line code audit, and a
forensic investigation of the repeated Instagram account suspensions that ended
in a **permanent** ban — plus an explicit order: *any auto-login system found
must be annihilated.* This box's IG auth is paused (`IG_AUTH_ENABLED=false`);
nothing in this session re-enabled it.

## Snapshot / rollback

- Baseline commit: `4ae8a6916b8d603dc3184f40160efac206cd4ca8` (branch `main`, clean tree).
- Services at start: `tgbot` (PID 2728871, up since Nov 05 10:30), `tgbot-monitor`,
  `tgbot-xchat-bridge`, `cookie-watch` — all `active (running)`, no crash loops.
- Rollback path: `git checkout <baseline>` + `sudo systemctl restart tgbot`.

## Health verdicts (evidence-based, read-only first pass)

| Subsystem | Verdict |
|---|---|
| tgbot core | healthy — 17 h uptime, no FloodWait/retry storms, yt-dlp nightly current |
| tgbot-monitor | healthy — reports on schedule; warnings coincide with dev CPU bursts |
| XChat bridge | functional, noisy — CycleTLS dead-connection sub-restart every ~80–100 min, self-healing, cursor intact |
| cookie-watch | healthy — 0 tamper alerts |
| PO provider | healthy — deno on `127.0.0.1:4417` only (invariant) |
| stream server | up — public by design; unauthenticated (hardened this session: full-length token + lazy expiry sweep) |
| X / TikTok relays | healthy — self-healed 23:30 transient |
| Friend Media | healthy — IG paused by design |
| Cookie jars | YT/TT/X warm via write-back; IG dead since Oct 8 (intentional pause) |
| Disk/RAM/CPU | healthy — 26% disk, load < 1 |
| **TLS renewal (certbot)** | **BROKEN — external to tgbot** (see “Left open”) |

## Instagram suspension — evidence, root cause, fix

### Evidence (redacted fingerprints only; `cookies/history.jsonl`)

- **578** `overlay / instagrapi_writeback` events on `igcookies.txt` — the #1
  writer over the account's life (vs 53 `cookie_refresher`, 23 `yt_dlp` merges).
- Their cadence is **~65–72 min** — exactly the Friend Media watcher period, not
  the DM worker (which logs one `ig_login_ok` per boot / per jar upload).
- Before each death streak, write-backs continue right up to the
  `ig_session_dead` minute (e.g. 2026-10-01 10:28/11:39 write-backs → 11:44 dead;
  repeated Oct 3–8).
- A 2026-10-08 audit already proved the *refresher* rotated device cookies
  (`mid`, `ig_did`, `datr`) on every headless run, invalidating the session.

### Root cause (high confidence, not a court-grade proof)

`modules/friend_media/instagram.py::_ig_client()` had a **30-minute client TTL**
while the watcher runs hourly. Every cycle therefore **rebuilt a brand-new
`IGClient()`** — fresh randomized Android device identity (`phone_id`, `uuid`,
`android_device_id`, advertising id) — then called **`login_by_sessionid()`**
and wrote the newly issued cookies back into `igcookies.txt`. That is ~24
“new phones”/day logging into one sessionid, while the DM worker concurrently
polled from a *different* persisted device (`direct_ig_session.json`). Device
churn + parallel identities + identical-session multi-IP usage is precisely
the automation pattern Instagram suspends on. Repeated checkpoints escalated to
the permanent ban.

Aggravating factors found and fixed:

1. **mtime-based re-arm.** `_ig_wait_for_fresh_jar` (DM worker) and
   `_ig_breaker_open` (Friend Media) treated *any* jar mtime bump as “operator
   uploaded a fresh jar”. The cookie refresher / yt-dlp write-back rotate
   `csrftoken`/`mid` and bump mtime while the **same dead sessionid** stays —
   so the bot re-attempted login with the dead session. Additionally the Friend
   Media breaker had a **1-hour cooldown auto-probe** — an automated retry loop.
2. **`utils/downloader/cookies.py` ignored `IG_AUTH_ENABLED`** — every
   login-walled/story IG link still handed yt-dlp a snapshot of the dead/suspended
   `igcookies.txt` (25 `commit_failure` events Oct 13 → Nov 6), i.e. traffic with
   a suspended session after the pause.
3. **No proxy parity** — `DIRECT_FORWARD_PROXY` was applied by the DM worker but
   not by Friend Media, so one session appeared from the VPS IP and the
   residential proxy.
4. **MQTToT hybrid listener** — a SECOND `IGClient` with its own randomized
   device identity that performed its own login. Off by default, untested, but
   a latent auto-login/second-device path.
5. **Backfill burst** — a long-gap backlog could relay in one scrape-shaped run
   (the 2026-09-05 archive-burst class).

### Fixes shipped

- `modules/friend_media/instagram.py`
  - Rebuilds now **resume a persisted session** (`direct_ig_session.json`
    preferred, then `friend_media_ig_session.json`) validated by a read-only
    `account_info()`. **No `login_by_sessionid` on rebuild.**
  - Bootstrap login happens **at most once per NEW sessionid** (`_IG_BOOT_SIG`
    gate) and never retries the same rejected sessionid.
  - Breaker (`_ig_breaker_open`) re-arms **only when the sessionid VALUE changes**
    (`utils.ig_anti_detect.sessionid_sig`); the 1-hour cooldown auto-probe is gone.
  - `DIRECT_FORWARD_PROXY` applied for IP parity.
  - Client persists its own identity once (`friend_media_ig_session.json`,
    chmod 600, git-ignored).
- `modules/direct_forward/instagram.py`
  - `_ig_wait_for_fresh_jar` waits for a **sessionid change**, not mtime; one
    login attempt then.
  - **MQTToT hybrid listener removed entirely** (second client + own login).
  - Backfill safety cap wired: `delivered_in_run` counter → `burst_pace()` returns
    `-1` at `IG_DIRECT_BACKFILL_MAX_ITEMS`; the worker stops cleanly, cursor stays
    behind the remainder, later jittered polls resume.
- `utils/ig_anti_detect.py`
  - New `sessionid_sig(jar)` primitive (+ module had a latent **missing
    `import config`** that would have crashed `burst_pace`; fixed).
  - `burst_pace(n_items, delivered_in_run=0)` cap sentinel.
- `utils/downloader/cookies.py` — Instagram jar is **not** handed to yt-dlp while
  `IG_AUTH_ENABLED=false` (no-auth ladder still serves public media).
- `config.py` / `.env.example` — `IG_DIRECT_MQTT_ENABLED` removed; new
  `IG_DIRECT_BACKFILL_MAX_ITEMS` (default 150). `tools/test_ig_mqtt.py` deleted.
- **Rule going forward: exactly one IG session path per worker, zero automated
  re-login. The ONLY recovery is an operator jar upload (sessionid change).**

## Code-audit fixes (independent of IG)

| # | File | Defect | Fix |
|---|---|---|---|
| H1 | `utils/queue_manager.py` | cancelled worker left `_active=True` → queue wedged forever; unmanaged `create_task` ref | unconditional reset + exactly-one respawn when pending; task ref + crash callback |
| M1 | `utils/queue_manager.py` | `MAX_QUEUE_DEPTH` never enforced (`_pending` unbounded) | reject at cap with a user notice |
| — | `utils/queue_manager.py` | `int(priority or 1)` coerced FREE tier priority 0 → 1 (free was never last) | keep 0 (`priority if priority is not None else 1`) |
| H2 | `utils/gate.py` | non-atomic `database.json` write → crash truncated it and `load_database` silently returned EMPTY defaults (auth/blacklist/premium wipe) | temp + fsync + `os.replace`, unique temp name |
| H3 | `modules/friend_media/admin.py` | on-demand IG zip ran outside `_ARCHIVE_LOCK`, no dedupe → two full archives concurrently on one session | global lock + per-friend in-flight set |
| H4 | `utils/uploader_handler.py` | `except Exception` missed `CancelledError` → split parts leaked; unguarded `os.remove` failed an already-sent upload | `except BaseException` cleanup + guarded removes |
| M5 | `modules/stream_*.py` | 48-bit token, expired entries never swept unless re-hit, `print` | `uuid4().hex`, lazy eviction, logger |
| M6 | `modules/friend_media/admin.py` | 8 unmanaged `create_task` sites (GC-able, silent exceptions) | `_spawn_bg()` refs + error callback |

Deliberately **not** changed (documented, not bugs worth the risk):

- `socket_timeout`/`retries` (M2): yt-dlp already defaults to bounded values
  (20 s socket timeout, 10 retries); adding explicit values would only change
  tuning, not remove an unbounded hang.
- Quota TOCTOU (M3): a whitelisted user can queue N jobs within one day and each
  increments on success; bounded in practice by queue depth (now 20) and the
  admin-only user base. Admission-time increment would change user-visible
  semantics (failed downloads consuming quota) for marginal gain.
- Position-edit storm (M4) and unbounded `DOWNLOAD_CACHE`/`FLOOD` dicts (L1):
  memory-only, bounded by admin usage; no correctness impact.

## Tests added (all plain scripts, pytest-compatible)

- `tests/test_queue_manager.py` — cancel-mid-drain respawn, depth cap, priority
  ordering (caught the priority-0 coercion bug).
- `tests/test_gate_db.py` — atomic write survives a simulated mid-write crash;
  no scratch files; blacklist/authorize roundtrip.
- `tests/test_ig_session_gate.py` — `sessionid_sig` rotates only with the
  sessionid value; `burst_pace` cap sentinel; yt-dlp IG jar gated on
  `IG_AUTH_ENABLED`.

Run: `venv/bin/python -m py_compile $(git ls-files '*.py')` then
`for f in tests/test_*.py; do venv/bin/python "$f"; done` (42 tests).

## Left open (operator action / out of tgbot scope)

1. **certbot renewals broken** (pre-existing): `elahe-rad.ir`, `rprx.ir`,
   `zyklon.ir` already expired (Oct 4/8); `anjomansut.ir`, `knxv.ir` expire
   Nov 23. Their renewal configs use `authenticator = manual` with a
   TTY-driven DNS hook (xui-lite), which cannot run unattended. Not touched:
   it is the operator's separate x-ui/DNS infrastructure and needs DNS API
   credentials or a manual run.
2. **Instagram account**: permanently suspended. Recovery requires a NEW account
   and a fresh jar; do not re-enable `IG_AUTH_ENABLED` with the banned session.
3. XChat bridge binds `0.0.0.0:19220`; stream `0.0.0.0:8080`; x-ui panel
   `0.0.0.0:29118`; no INPUT firewall rules. Recommended: bind cycletls to
   loopback and firewall the panel.

# tgbot — agent working notes (memory)

These are stripped, repo-local copies of the project's long-term memory
(originally kept in the AI coding agent's private memory store across sessions).
They capture non-obvious invariants and history that are not obvious from the
code or the git log alone. Last updated: 2026-08-13.

> **Sensitive values have been redacted.** Where you see `<vps-ip>`,
> `<ssh-port>`, `<vps-user>`, or `<redacted>`, substitute your own. No real
> credentials, IP addresses, or usernames are stored here.

- [tgbot ↔ balebot integration](tgbot-balebot-integration.md) — tgbot is the
  reference; balebot was a Bale.ai experiment. Full platform/API diff + a plan
  for an optional Bale frontend inside tgbot (one process, one PO provider, one
  yt-dlp), including balebot-only modules (GitHub explorer, translate, /yt,
  /transcript, /web) that could come to Telegram.
- [Cookie protection & monitor](tgbot-cookie-protection-and-monitor.md) — cookie
  corruption is fixed (`b44db54`); yt-dlp only ever touches disposable snapshots;
  `cookie-watch.service` (inotifywait) is the tamper monitor; `auditd` is inert on
  the test host.
- [VPS two-bots runtime state](vps-two-bots-runtime-state.md) — on the test VPS
  `balebot.service` (PO port 4416) and `tgbot.service` (PO port 4417) both run;
  both are now enabled and survive reboot; a bare `python main.py` is balebot.
- [YouTube size fix & IP flag](tgbot-youtube-size-and-ip-flag.md) — uploaded<
  shown bug was a silent `/best` muxed fallback (fixed `5003d78`: use a
  height-capped **merged** fallback); the test VPS IP gets storyboard-flagged
  intermittently (not a code bug).
- [Fetch concurrent / download queued](tgbot-fetch-concurrent-download-queued.md)
  — metadata fetches bypass the queue and run concurrently; only the real
  download+upload jobs serialize. Never call `extract_formats` inline.
- [How download-button sizes are computed](tgbot-ytdlnis-size-approach.md) —
  ytdlnis/yt-dlp reference: sizes are per-format, yt-dlp never sums a `v+a` merge,
  so the app must add `video+audio` itself. tgbot's `estimate_format_size` already
  does this; the real mismatch cause is the selector fallback, not the size math.
- [Exact CDN size probe](tgbot-exact-cdn-size-probe.md) — Instagram DASH reels
  carry no `filesize`/`tbr`/`duration`, so the 60 s heuristic overshot 2–3×;
  button-visible blind-guess formats now get an exact `Content-Length` HEAD
  (Range-GET fallback) and drop the `~`.
- [TikTok shortlink fix](tgbot-tiktok-shortlink-fix.md) — `vt./vm./vn.` links
  died on TikTok's stochastic anti-bot interstitial (yt-dlp's bare
  facebookexternalhit HEAD); we pre-resolve with a browser UA, pin
  `yt-dlp[default,curl-cffi]` for the PoW webpage solver, and retry once.
- [Instagram anti-detection posture](tgbot-ig-anti-detection.md) — the DM
  poller got the account flagged; jittered ≥several-minute cadence,
  activity-watermark zero-idle-cost polling, stable session/device, optional
  single residential proxy, and 3–5 h checkpoint freezes. **Update 2026-08-05:**
  a second native checkpoint hit after clean relay traffic; deeper research
  implicates the Python `requests` TLS fingerprint + un-echoed `IG-U-RUR`
  routing headers (see `reference/` clones: okgram, insta-wizard, instaharvest_v2).
- [System monitor](tgbot-system-monitor.md) — the health monitor is now a
  static **Go binary** (`cmd/tgbot-monitor/` → `build/tgbot-monitor`, the
  project's one Go component); a /proc-only, zero-dep process that keeps
  sending `#system` reports + 80% warnings even when the bot is down; runs as
  `tgbot-monitor.service` (systemd) or a detached bot spawn; dedup via
  pidfile + /proc scan shared between Go and Python.
- [Go feasibility](go-feasibility.md) — analysis of Go as a complementary
  language: full rewrite rejected (yt-dlp is irreplaceable); the standalone
  system monitor is the one recommended Go sidecar — **implemented 2026-08-04**.
- [Silent 203/EXEC outage](tgbot-silent-203-exec.md) — `run.sh` lost its exec
  bit (tracked `100644` in git, reset by a pull) so systemd crash-looped with
  `status=203/EXEC` while `bot.log` stayed clean; fixed the git mode,
  hardened `install.sh`, documented the fingerprint.
- [2026-08-07 full health pass](tgbot-2026-08-07-health-pass.md) — runtime +
  feature + security pass on the production box (old US VPS expired). Every
  site tested live via the Telethon driver; fixes: non-core yt-dlp sites now
  routed to the format flow, HLS fragment-size artifact guard, silent
  `MESSAGE_NOT_MODIFIED`, SSRF guard on the direct-file path, secret file
  perms `600`.
- [2026-08-11 health pass — package-split regression sweep](tgbot-2026-08-11-health-pass.md)
  — full admin-console button sweep via Telethon after the `modules/admin.py` /
  `modules/direct_forward.py` → sub-packages split (commit `81a5139`); found + fixed
  one silent dispatch regression (`admin_pot_menu` no-op), confirmed X photo-paste
  fix live in production, tightened `database.json` perms, documented the iptables
  `INPUT ACCEPT` exposure of the 8080 streamer for the operator.
- [2026-08-12 health pass — full-feature + security sweep](tgbot-2026-08-12-health-pass.md)
  — comprehensive feature matrix (YouTube video/audio/playlist, SoundCloud,
  direct file, cancel, admin console, SSRF guard) all green; direct-forward
  workers (IG/X/TikTok) running healthy; XChat bridge active; no code changes
  made.
- [X photo-paste fix](tgbot-2026-08-11-x-photo-paste-fix.md) — photo-only
  pasted tweet URLs silently failed ("No downloadable media") because twikit
  2.3.3's `User.__init__` raises `KeyError('urls')` on some authors, aborting
  `get_tweet_by_id`. Fixed by switching `_x_fallback_photos` to a raw
  `client.gql.tweet_detail` GraphQL walk scoped to the focal `tweet-<id>`
  entry (no model building, so the bug cannot fire).
- [Dispatch-propagation bugs](tgbot-2026-08-13-propagation-dispatche-bugs.md) —
  two dispatcher bugs: bare `except Exception` swallowed `StopPropagation`
  (group-0 GitHub links ALSO grabbed by the group-1 downloader → duplicate
  replies), and a `RawUpdateHandler` mid-group starved later handlers (fixed by
  `raise ContinuePropagation` on non-owned updates). New shared helpers in
  `utils/propagation.py` (`stop()`/`continue_()`).
- [Balebot hardening](tgbot-balebot-hardening-2026-08-13.md) — the Bale.ai
  frontend: aiogram 3.30 poller on `tapi.bale.ai`, LIMITED admin (no
  cookies/premium/POT/direct-forward), real 20 MB limit (docs lie 50),
  `getUpdates` drain (Bale `deleteWebhook` is NOOP). `bale_log` is a **Telegram**
  channel (never `tapi.bale.ai` — government-owned messenger).
- [Balebot merge](tgbot-balebot-merge-2026-08-13.md) — GitHub explorer,
  YouTube search, Translate, Web→Markdown ported from balebot into shared
  transport-free modules usable by both Telegram and Bale.
- [Instagram risky + push](tgbot-instagram-risky-and-push-2026-08-13.md) —
  IG gap recovery (pagination with cursor, at-least-once) + optional MQTToT
  push via `instagrapi 2.18.14`.
- [Subscription system](tgbot-subscription-system.md) — toggleable 3-tier
  Stars/TON subscriptions + free tier with multi-channel force-join + WebApp.

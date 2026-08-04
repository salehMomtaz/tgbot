# tgbot — agent working notes (memory)

These are stripped, repo-local copies of the project's long-term memory
(originally kept in the AI coding agent's private memory store across sessions).
They capture non-obvious invariants and history that are not obvious from the
code or the git log alone. Last updated: 2026-08-04.

> **Sensitive values have been redacted.** Where you see `<vps-ip>`,
> `<ssh-port>`, `<vps-user>`, or `<redacted>`, substitute your own. No real
> credentials, IP addresses, or usernames are stored here.

- [tgbot ↔ balebot port relationship](tgbot-balebot-port-relationship.md) — tgbot
  ports balebot's PO-token provider, cookie protection, and `install.sh`
  deployment; GitHub explorer and translate are intentionally NOT ported.
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
  single residential proxy, and 3–5 h checkpoint freezes.
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

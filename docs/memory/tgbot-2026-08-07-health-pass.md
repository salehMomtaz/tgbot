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

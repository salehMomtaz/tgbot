# 2026-09-04 — IG "certain audiences" reels → preview images + cookie-jar deaths

Operator report: Instagram DM shares of reels repeatedly arrived as a **preview
image** with `⚠️ (full media failed to download)` instead of the reel video;
log showed `This content isn't available to everyone: It can't be seen by
certain audiences.` (e.g. `reel/Dc1o4qms3qa` from @luxeauura, 2026-09-04
12:58). Separately, the IG cookie jar "corrupted" again — the operator
injected the jar into an incognito browser via Cookie-Editor and Instagram
behaved as logged-out. The operator asked for a **cookie-jar history tracker**
so future IG errors can be correlated with the nearest jar change.

## Post-mortem (from logs + meta.json timestamps)

1. **Sep 2 15:58** — headless cookie refresher "refreshed" igcookies.txt
   (4 cookies rotated, 14 total). Direct worker still worked (16:01 polls 200).
2. **Sep 3 15:25** — the IG account's session died (LoginRequired on
   `user_stream_by_id_v1`; persisted `direct_ig_session.json` unusable; jar
   `sessionid` → 403 `login_required`). Cause on Instagram's side (risk
   system / rate limit); the friend-media archive runs were rate-limited
   ("Please wait a few minutes") hours earlier. No refresher ran in that
   window — the death was NOT caused by a jar write.
3. **Sep 3 16:49** — the 24h headless refresher visited instagram.com with the
   DEAD sessionid. Instagram served the **anonymous** logged-out home (fresh
   anonymous `sessionid` + login form). The refresher then **replaced the
   ENTIRE jar** with that anonymous cookie set ("6 cookies rotated, 16
   total") and stamped `touch_cookie_success` — permanently destroying the
   jar's contents and faking "warm" freshness in meta.json.
   → *This* is "cookies corrupted / site behaves like no cookies".
4. **Sep 4 09:55** — operator re-uploaded a fresh jar via Admin → Cookies;
   worker logged in via sessionid at 09:56; reels relayed again from 10:03.
5. **The 12:58 reel→image failure happened with a HEALTHY session** —
   `media_info` returned 200 OK seconds after the yt-dlp failure. The chain:
   yt-dlp web fetch → audience gate ("certain audiences") → `_ig_native_deliver_once`
   **returned False for clips by design** ("yt-dlp handles quality") → the
   final `_download_and_deliver(preview_url)` retry failed the same way →
   preview image delivered. The audience gate is what Instagram serves
   instead of HTTP 400 for follower/age-restricted media, and yt-dlp's ladder
   only escalated to cookies on `http error 400` — so the logged-in jar was
   never even tried, even though the account could watch the reel in-app.

## Fixes shipped

| Fix | Where |
|---|---|
| Cookie history tracker (append-only `cookies/history.jsonl` + 40-per-jar full snapshots in `cookies/history_snapshots/`; events: startup, admin_replace, refresher_write/visit/refused, overlay, merge, commit_failure, restore, ig_login_ok, ig_session_dead, ig_relogin_failed; values only as `first4…last4(len)` fingerprints) | `utils/cookie_history.py` + hooks in `cookie_manager` (merge/overlay/commit), `admin/cookies._write_cookie_jar(actor=…)`, `cookie_refresher`, `main.initialize_cookie_jars`, `direct_forward/instagram` |
| In-chat correlation view: Admin → 🍪 Cookie Jars → *jar* → 📜 History (jar events + IG health interleaved) | `modules/admin/keyboards.py`, `modules/admin/callback_dispatch.py` |
| Refresher never overwrites a jar it was logged out of: IG gate = `sessionid` present AND no login URL AND no `action="/accounts/login/ajax/"` login form in the DOM; other sites refuse on login-page final URL. Refused visit records `refresher_refused` + preserves a snapshot, writes NOTHING. Verified visits write as an **overlay** (never full replace — full replace was also shrinking jars 24→14 lines). `touch_cookie_success` only on verified visits. | `utils/cookie_refresher.py` |
| yt-dlp IG ladder escalates to cookies on the audience gate too ("available to everyone" / "certain audiences" ≙ HTTP-400 login-wall) | `utils/downloader/download.py` (Case B) + `classify_auth_error` markers |
| Reel native fallback actually delivers: `_ig_native_deliver_once(allow_clips=True)` sends the clip video from the app API (media_info 200 OK → `video_url`) instead of no-oping; preview image now truly last-resort | `modules/direct_forward/instagram.py` |
| IG session-death alert: after 2 consecutive mid-poll re-login failures the operator gets a Telegram alert pointing at the jar Replace + History buttons (previously 17 silent "re-login failed" hours) | `modules/direct_forward/instagram.py` |

## Verification

- Unit-style: overlay never deletes / preserves duplicate lines; merge records
  changed cookie names; snapshots rotate; timeline formatting. (temp-dir run)
- Live: re-drove the exact failing reel `Dc1o4qms3qa` via Telethon —
  interactive flow delivered 1080p mp4; the relay-path invocation
  (`download_media(url, None, "v", cache_id)`) reproduced the 12:58 failure
  mode then **recovered via the new cookie escalation** (full 1.39 MB video).
- 📜 History button live: shows startup / ig_login_ok / overlay / merge
  events with sha-chain, sessionid fingerprints, snapshot names.
- Bot restarted (SIGTERM → systemd), startup history events written, IG
  worker resumed session (`ig_login_ok`), zero tracebacks.

## What remains open (operator-relevant)

- The **first** session-death (Sep 3 15:25) is attributed to Instagram's risk
  system, not a jar write — the history tracker makes the next death
  diagnosable with certainty (compare first `💀` vs nearest jar event).
- Jar snapshots under `cookies/history_snapshots/` are FULL jars (real
  session cookies) — git-ignored (`.gitignore`), like the rest of `cookies/`.
- Headless refreshes still run every ~24h but are now fail-safe: worst case
  they refuse to write and leave the jar untouched.

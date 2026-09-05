# 2026-09-05 — IG session death: incident analysis + hardening

## What the operator saw

A wall of `🚨 [ERROR] Client error user_stream_by_id_v1, exception:
LoginRequired('login_required')` posts in the log channel (158 in 12 h),
each with a full double traceback (users/<id>/info/ → users/<id>/info_stream/).

## Root-cause timeline (from cookies/history.jsonl + logs/bot.log)

- **02:54** — operator runs `🗂 Archive (zip)` on IG friend `@_.nedabeig._`
  (Friend Media Archiver). Profile pic + posts fetch fine (`[200]`).
- **02:55:08** — mid-burst, `feed/reels_media/` (highlight fetch) returns
  **403 login_required**. The session is dead — killed by Instagram
  server-side.
- **02:55:11 → 02:55:35** — the archive loop kept hammering highlights 2-8
  back-to-back (each answered `{"message":"","status":"fail"}`) — ~8 more
  auth'd calls into a dead session.
- **02:56** — direct-forward IG worker's poll hits the same wall; re-login
  (sessionid from jar) fails `Exceeded 30 redirects`; sleeps 1 h; then
  exponential backoff.
- **03:04 → 06:34 (pre-fix)** — Friend Media's hourly cycle re-attempted
  **12 friends × 2 archives** against the dead session every cycle:
  `login_by_sessionid` → 403 → instagrapi's internal fallback probe
  (`user_info_v1` → `user_stream_by_id_v1`) logs ERROR + traceback twice per
  attempt ≈ **~100 authenticated 403s/hour of checkpoint fuel** plus the
  log-channel spam.

## Who corrupted the jar? Nobody — verdict on our own systems

The cookie history proves the jar was **NOT corrupted by us**:

- The jar's `sessionid` fingerprint (`1267…BSSQ(77)`) is **byte-identical**
  across every `overlay` (instagrapi write-back), `merge` (yt-dlp write-back)
  and `cookie_refresher` visit from 16:26 (operator upload) to the end.
- All write-backs touched only rotation cookies (`rur` by instagrapi;
  `csrftoken/datr/ig_did/mid` by the refresher) — exactly the overlay
  design: never delete, never swap identity.
- The refresher's logged-in gate (sessionid present + no /accounts/login
  URL + no anonymous login form) still passed at 03:09/03:25 because
  Instagram's **web surface kept accepting the sessionid** while the
  **private API rejected it** — so the headless visit genuinely saw a
  logged-in page and overlaid device-cookie rotations. Harmless (overlay),
  but it means the refresher cannot detect a private-API-only death.

**Conclusion: Instagram killed the session server-side** (private API
surface), most likely triggered by the burst pattern of the full archive
(8 highlight fetches + posts pagination + story fetch in ~90 s). Our
refresher/write-back systems did NOT damage the jar; the 2026-09-03
"refresher destroys jar" class of bug is not what happened here.

## Fixes shipped (commit this session)

1. **IG circuit breaker for Friend Media**
   (`modules/friend_media/instagram.py`): `_IG_BREAKER` trips on the first
   auth-classified login failure (`_ig_auth_failure`: LoginRequired /
   ChallengeRequired / redirect-loop / checkpoint) and grounds ALL
   friend-media IG calls for 1 h. Re-arms automatically when the jar's
   mtime changes (operator re-upload) — the moment a fresh igcookies.txt
   lands, the next cycle probes it. Worst case after the fix: ONE failed
   login per cycle instead of ~100 auth'd 403s/hour.
   Mid-archive bursts (`archive_instagram_full`) abort on the first auth
   failure or 3 consecutive non-auth highlight failures, instead of
   hammering the remaining posts/highlights.
2. **Dead-session probe noise filter**
   (`utils/ig_anti_detect.py::_install_login_noise_filter`, installed from
   `install_transport`): drops exactly the `Client error
   user_stream_by_id_v1` / `user_info_by_username_v1` ERROR records
   (instagrapi's `login_by_sessionid` fallback probe tracebacks). Our own
   single actionable line ("IG session unusable — pausing IG archives…")
   is what remains in the log channel.
3. **One operator DM per dead-session streak**
   (`modules/friend_media/admin.py::_IG_DEAD_ALERTED`): when the breaker is
   tripped, the operator gets ONE "💀 Instagram session is dead" message
   (streak-keyed by `tripped_at`, so a new death always alerts again),
   including the jar re-upload path. The per-cycle "IG archives paused"
   warning goes to the log channel once per cycle instead of 12 per-friend
   skip lines.
4. **Breaker events in cookie history**: trips are recorded as
   `ig_session_dead` (actor `FriendMedia/IG`) in `cookies/history.jsonl`,
   so Admin → 🍪 → Instagram → 📜 History shows why archives went quiet,
   correlated with jar writes.
5. **PhotoExtInvalid fix (unrelated 1-off error from 13:39 the day
   before)**: direct-forward photo routing is now magic-bytes-based
   (`sniff_image_extension` / `normalize_photo_file` in
   `modules/direct_forward/common.py`, used by `_download_and_deliver` and
   `_x_deliver_tweet`): a yt-dlp photo post with a missing/bogus extension
   is renamed to the sniffed extension before `send_photo`. Previously the
   320×320-probe-fallback + extension heuristic misrouted such files into
   `send_photo` → `[400 PHOTO_EXT_INVALID]` (TikTok photo share).

## What is still needed from the operator

The IG session itself is dead until a fresh `igcookies.txt` is uploaded
(**Admin Console → 🍪 Cookie Jars → Instagram → ✏️ Replace**). Both the
direct-forward IG worker and Friend Media IG archives will pick it up
automatically (no restart needed):

- Friend Media re-arms on the jar mtime change within one cycle.
- Direct-forward picks up the new sessionid on its next login backoff retry.

## Follow-up (same day, operator request): archiver pacing

The operator confirmed the archiver is explicitly **not** time-sensitive
("steps time need to be more random and increased"), and asked for
confirmation that no password login fallback exists (it doesn't — see
below). Shipped:

- **Password-fallback cleanup**: the fallback CODE was already gone
  (removed 2026-08-26), but its dead config definitions
  (`IG_DIRECT_USERNAME` / `IG_DIRECT_PASSWORD` / `IG_DIRECT_TOTP_SEED` in
  config.py), the .env.example template entries, the stale values in the
  live .env, and a stale line in docs/DIRECT_FORWARD_SETUP.md all
  survived and made it look like a fallback might exist. All removed;
  config.py + .env.example now carry an explicit "sessionid-only, no
  credentials fallback" note. The IG private API is either fed a valid
  sessionid or waits for the operator to upload a fresh jar.
- **Pace window for the full archive**: `archive_instagram_full` opens a
  refcounted window (`pace_window_enter/exit`); the shared client's
  `private_request` is wrapped once per build (flag-gated, installed after
  the token-echo wrapper) so every private-API call during an archive —
  including `user_medias_v1`'s internal pagination, which `delay_range`
  does not pace — sleeps a random pause in
  `FRIEND_MEDIA_ARCHIVE_PACE_MIN..MAX` (default 4-10 s) first.
- **Inter-step delays scale from the same range**: per-post/per-highlight-
  item 1.5-6 s, between highlight reels 2-10 s (previously a failing
  highlight went straight into the next one with NO gap), phase
  transitions 4-10 s.
- **Inter-friend gap**: `_run_archives` pauses
  `FRIEND_MEDIA_FRIEND_GAP_MIN..MAX` (default 20-60 s) between two
  consecutive friends that both have IG work.
- **Hourly story/post item jitters bumped**: stories 1.2-2.7 s+ per item
  (was 0.6-2 s), posts 1.5-3.6 s+ (was 0.8-2.4 s).

New .env knobs: `FRIEND_MEDIA_ARCHIVE_PACE_MIN/MAX` (4/10),
`FRIEND_MEDIA_FRIEND_GAP_MIN/MAX` (20/60). A full archive of a friend
with many posts/highlights now takes 30+ minutes by design.

## Verification

- All unit checks pass (breaker open/close/re-arm, half-open probe,
  auth-failure classification incl. `login_by_sessionid` underscore marker,
  noise filter idempotence, JPEG/PNG/WebP/GIF magic sniffing, RIFF≠WebP,
  pace-window refcounting, gated pacing 4-10 s vs 0 ms pass-through).
- `python -m py_compile` clean repo-wide; pyright 0 errors on touched files.
- Live: post-restart cycle shows friend 1 tripping the breaker with ONE
  warning, friends 2-12 skipped instantly, zero instagrapi traceback spam
  (1 actionable ERROR line total vs ~158 before).

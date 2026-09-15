# 2026-09-15 — Friend Media IG archive: highlight 403 killed the whole archive

## What the operator saw

`🗂 Archive (zip)` on `samin_mshk` (and `samin854917`) failed with
`[FriendMedia] IG archive for @samin_mshk failed: session died mid-archive at
highlight 1: login_required`, plus a 60-min "IG archives paused" warning. The
operator also reported the bot's session no longer appears under Instagram's
"where you're logged in", and that they want to archive a username **without
adding it as a friend**.

## Root cause of the archive failure (ours — fixed)

Timeline (16:49-16:55): the DM relay was working (`reacted 👍`, native relays),
then `highlights_tray` returned 200, then the first `highlight_info_v1`
(`POST feed/reels_media/`) returned **403 login_required**. The old archiver
treated ANY auth-classified exception during highlights as "session dead",
raised `IGUnavailable` (failing the entire zip) and tripped the global breaker.

But the **highlights phase runs LAST** — profile + all posts had already
succeeded, which proves the session works. Instagram simply does not serve
highlight *media* for accounts the bot doesn't follow (it happily serves the
tray), so a 403 there is expected for an unfollowed/private account. Failing the
whole archive — and grounding IG for an hour — was wrong.

**Fix (`modules/friend_media/instagram.py::archive_instagram_full`):** on an
auth failure at a highlight, log a clear "highlights not accessible — likely not
following this account; skipping highlights" warning, break the highlight loop,
and finish with profile + posts. The zip caption notes highlights were skipped.
The global breaker is NOT tripped (the session is demonstrably alive). The
3-consecutive-empty-failure guard for a genuinely dying session is unchanged.
Added `FRIEND_MEDIA_IG_HIGHLIGHTS` (default true) to skip the phase entirely.

## Ad-hoc archive without adding a friend (NEW)

Console → Friend Media now has **🗂 Archive IG username (no add)**: enter one or
more `@username`s (or profile links) and get a one-shot zip. It builds a
synthetic friend dict (`{"platform":"instagram","ig_username":…}`) and calls
`archive_instagram_full` — nothing is written to the friends list.

## On the "cookies destroyed / device not visible" report

The jar itself was intact (the fresh 15:40 upload, deduped to ~12 cookies; the
sessionid byte-identical across write-backs). What happened is the **session
died on Instagram's side** ~16:51: after the `feed/reels_media` 403, the very
next DM inbox poll (16:54:43) also returned 403 and `login_by_sessionid` failed
with "Exceeded 30 redirects", so the worker went to its 1-h backoff. A dead
session naturally disappears from the app's login-activity list. Recovery is
unchanged: upload a fresh `igcookies.txt` (Admin → 🍪 Cookie Jars → Instagram →
✏️ Replace); the archiver re-arms on the jar mtime change and the worker picks
it up without a restart.

**Not conclusively attributed:** whether the 16:51 death was *caused* by the
highlight fetch, by cumulative account activity (archives + DM polls + the
headless cookie refresher), or was already flagged. The highlight 403 was
merely where the dying session first surfaced. Mitigations available if deaths
recur: `FRIEND_MEDIA_IG_HIGHLIGHTS=false`, a lower auto-check frequency, or
disabling the IG headless refresh (the refresher and instagrapi present two
different device fingerprints for one sessionid — a plausible stressor).

## Verification
- `python -m py_compile` clean; `bash -n` clean; `go test ./...` ok.
- Bot restarted cleanly; probes unchanged (a live archive needs a valid session).

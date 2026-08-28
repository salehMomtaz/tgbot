# Instagram cookie jar — the "Last authenticated success" confusion

**Date:** 2026-08-28 · **Operator report:** uploaded a fresh `igcookies.txt`
(~2 KB, 30 lines including `ps_l`/`ps_n`/etc.), injected it into a Chrome
incognito window, Instagram web said "no account". Re-asked: "why 63
hours ago? cookies were fresh and I uploaded them last night." The
admin menu said "✅ Last authenticated success: 63h ago · rotation
merges: 1, 📤 Last uploaded: 0h ago".

## What was happening

Two distinct timestamps are stored in `cookies/meta.json` per jar:

| Field | Updated by | Meaning |
|---|---|---|
| `last_success` | `utils/cookie_refresher._refresh_one` — only after a successful headless refresh cycle | When the headless browser last authenticated against the jar (the "live IG login via the headless Chrome") |
| `last_upload` | `modules/admin.cookies._write_cookie_jar` — only after the operator (re)uploads the jar via a Telegram document | When the operator last replaced the file via the bot's cookie admin menu |
| `last_failure` | Same as `last_success`, but on a failure | Most recent headless refresh failure |
| `merge_count` | `utils.cookie_refresher._refresh_one` | How many times a headless refresh captured rotated cookies |

A fresh operator upload sets `last_upload` but does NOT set
`last_success`. The `last_success` is the timestamp of the most
recent 24h headless refresh cycle that successfully authenticated —
which can be 63h ago if the headless cycle skipped the site (the
old code skipped IG with `mtime 6h ago (<20h)` even when the
session was already stale).

The admin menu's old label "Last authenticated success" made the
operator think their fresh upload was 63h old, when in fact:

- The `last_upload` field showed `0h ago` (the operator's just-uploaded jar).
- The `last_success` was 63h ago (the headless cycle that ran at 17:32 and
  had the old `ps_l`/`ps_n`-less `igcookies.txt`).

The two are completely different things and the menu now labels
them clearly:

- "✅ Last headless refresh: 63h ago" — when the 24h cycle last
  successfully ran against this jar.
- "📤 Last uploaded: 0h ago" — when the operator last (re)uploaded.

## The "no account" in Chrome incognito

The bigger problem. The operator uploaded a 2 KB jar with 30 lines
(Chrome DevTools "Copy as Netscape" export typically has each
cookie twice, plus the IG personalization cookies `ps_l`/`ps_n`). The
bot's `_write_cookie_jar` saved the file as-is. But the next
`utils/cookie_refresher` cycle called
`utils/cookie_manager.overlay_cookies` to merge in the rotated
sessionid. `overlay_cookies` parsed the jar into a dict keyed by
`(domain, path, name)` and wrote back. **The dict silently dropped
duplicates** (the same key written twice → only one entry survives).
Result: 30 lines → 13 entries → 1 KB on disk. The
`ps_l`/`ps_n`/`__Secure-ENID`/etc. were lost in the merge.

When the operator then injected the resulting 11-line, 1 KB jar
into Chrome incognito, Instagram web said "no account" because
the missing `ps_l`/`ps_n` cookies are the ones that tell IG "this
browser has been used by this account before" — without them, IG
treats the session as a brand-new anonymous visit.

## The fix (commit history)

1. `utils/cookie_manager._parse_cookie_lines` now returns an
   ORDERED LIST of `(key, raw_line)` instead of a dict. The
   `merge_snapshot_into_real` and `overlay_cookies` paths walk
   this list in source order, build a key→line dict for
   in-place updates, and write the source-ordered output. Duplicates
   are preserved end-to-end. Verified: 30-line autobak now
   produces 26 entries (was 13); each duplicated key is still
   present twice in the output.

2. `utils/cookie_refresher._refresh_one` for IG no longer skips
   on `mtime < 20h` (the old skip missed the case where the operator
   uploads fresh cookies but the headless cycle is the only way to
   rotate IG's mid/rur/csrftoken). It also navigates to
   `/explore/` and `/accounts/edit/` after the home page to force
   the server to issue fresh tokens in response headers (the home
   page alone doesn't rotate them).

3. The admin menu label "Last authenticated success" was renamed
   to "Last headless refresh" to make it unambiguous that this
   metric is about the 24h headless cycle, not the jar's age.
   The companion "Last uploaded" line already shows the upload
   timestamp; the two together tell the operator exactly what
   they need to know.

## Operator recovery path (after this fix is live)

The current `igcookies.txt` on disk is the 1 KB version that
already lost its `ps_l`/`ps_n`. The fix above is permanent going
forward, but for the **next** headless cycle (within 24h), the
fresh cookies in the operator's mobile app will land in the jar
fully. If the operator wants immediate recovery:

1. Re-export cookies from the Instagram mobile app (not Chrome
   DevTools — mobile export has each cookie once and includes
   `ps_l`/`ps_n`).
2. Re-upload via Admin → 🍪 Cookie Jars → Replace.
3. The current bot's `login_by_sessionid` works with the new
   sessionid (verified at 23:03:07 today) — Chrome's web login
   still won't recognize the session until the next headless
   refresh completes (it'll add the missing `__Secure-*` and
   `ig_psid` tokens via the new `/explore/` and `/accounts/edit/`
   navigation paths).

## Reference clones (untracked, in `reference/`)

- `reference/instagrapi` — upstream 2.18.12; read `client.py`,
  `mixins/auth.py`, `mixins/private.py` for session/header mechanics.
- `reference/okgram` — the **phone-grade** reference: device pool,
  stable UUIDs, `IG-U-RUR` echo + persistence, geo auto-sync,
  `doctor` diagnostics, rate governor.
- `reference/insta-wizard` — async client with checkpoint
  taxonomy + challenge section.
- `reference/instaharvest_v2` — curl_cffi transport + challenge
  resolver + 14-layer fallback GraphQL/mobile/web.

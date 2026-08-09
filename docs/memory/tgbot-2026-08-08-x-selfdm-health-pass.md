# 2026-08-08 X self-DM direct-forward health pass

Live end-to-end pass of the rewritten X direct-forward worker
(`modules/direct_forward.py`, self-DM method) against the production bot
(chat `7429671248`, `tgbot.service`, box clock Sat 2026-08-08). The X session
was already live in `cookies/twitter/xcookies.txt` (`auth_token` + `twid`,
expiry 2027); this pass enabled the worker and drove every delivery path with
the operator's real self-DM data plus one fresh media upload.

## Setup done

- `.env`: `X_DIRECT_ENABLED=true` (was absent → worker disabled). The bot was
  restarted via its own SIGTERM path (running as `dev`, `Restart=always`), no
  sudo needed.
- First boot: `[DirectForward/X] first run — priming cursor, backlog is
  skipped.` → `x.last_id` set to the newest self-DM message id.

## What was driven (all via twikit, read-only + one upload)

| Input (self-DM) | Path exercised | Result |
|---|---|---|
| Tweet share card (photo-only, @cuteukeboy) — re-driven by resetting `x.last_id` | `_x_deep_find_tweet` → native `_x_deliver_share_photos` | ✅ 1 photo relayed, header `📥 X DM … 👤 Post by @cuteukeboy` |
| Tweet URL text (video, @oshtru, oshtru/status/1577855540407197696) | `_x_deliver_tweet` → `extract_formats` → `download_media` top 900p → split/upload | ✅ 1.71 MB mp4 relayed, full header + post link |
| Fresh DM photo (re-uploaded real 0.10 MB JPEG via `dm_new` + `upload_media`) | `_x_deep_find_media_url` → `_x_deliver_dm_attachment` (authenticated ton.twitter.com fetch) | ✅ 0.10 MB photo relayed |
| Plain-text self-test messages | route 3 URL scan | ✅ skipped (`no relayable media`) |

## Bugs found + fixed (each live-verified)

1. **`_x_deep_find_tweet` missed legacy share cards.** The real share card is
   `attachment.tweet.status.{id_str, text}` (key is `text`, not `full_text`)
   with the canonical link in `attachment.tweet.expanded_url`. The parser only
   matched GraphQL-style (`rest_id`+`legacy.full_text`) and `id_str`+`full_text`,
   so real tweet shares fell through to the bare-DM-photo path (no author/post
   header). Fix: also match `id_str`+`text`, and fall back to a card-level
   `expanded_url` when no status object is embedded. Verified against the
   operator's actual card before deploying.

2. **httpx `CookieConflict: Multiple cookies exist with name=__cf_bm` killed
   the worker.** `_x_fetch_auth_bytes` used the *shared* twikit `client.http`
   with `follow_redirects=True`. ton.twitter.com is Cloudflare-fronted; its
   `Set-Cookie: __cf_bm` piled a duplicate name into the session jar, and the
   next `dm_conversation` poll died (`dict(self.http.cookies)` itself raises).
   Symptom: everything works for one poll, then the worker goes permanently
   silent (`_x_fetch_self_messages` swallows the exception → returns `[]`).
   Fix: `_x_fetch_auth_bytes` now uses a **throwaway `httpx.AsyncClient`**
   (same base headers + a copy of the session jar, closed afterwards) so
   response cookies never touch the session. Verified by 3+ clean polls after
   ton fetches.

3. **`<500B` payload guard rejected legitimately tiny images.** The operator's
   earlier "photo test" self-DMs were genuinely tiny flat PNGs (133 B / 99 B).
   The guard `len(data) < 500 → invalid` dropped them. Fix: `_x_media_payload_ok`
   validates by **magic bytes** (PNG/JPEG/GIF/WebP/BMP, mp4 `ftyp`), rejecting
   only HTML interstitials — never by size.

## Residual notes

- Old DM-photo URLs in re-driven messages are still fetchable and return the
  real bytes (not expired); the earlier "invalid payload" was purely the
  size-guard false positive, now fixed.
- `_x_fallback_photos` (`get_tweet_by_id`) can raise `KeyError` on some old
  tweet JSON (twikit model quirk) but degrades to `[]` — delivery then fails
  loudly instead of dropping; acceptable.
- yt-dlp's X extractor works with the shared xcookies jar (900p video
  extracted cleanly); two candidate test URLs turned out to embed YouTube
  links (bot's YT sign-in guard fired) — not an X bug.

## Convergence

- `python3 -m py_compile` on all tracked `.py`, `bash -n` on the three scripts:
  pass. No secret tracked (`git ls-files | grep -E '\.(env|session)$|cookies/'`
  empty). Worker stable over multiple poll cycles, zero `ERROR`/`Traceback`
  from the X worker since the fixes.
- Left enabled: `X_DIRECT_ENABLED=true` (intended final state).

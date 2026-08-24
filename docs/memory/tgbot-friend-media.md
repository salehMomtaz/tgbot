# Friend Media Archiver — design, invariants & hardening (2026-08-24)

`modules/friend_media/` archives **friends'** media using the operator's
PREMIUM user-account session (`premium_app`) as the *reader* and the BOT as the
*delivery* channel. Bot accounts cannot read another user's profile-photo
history; only a real user account can. The premium session is already used for
4 GB uploads; here it doubles as the archiver's eyes.

## What it does

Per friend record (key `tg:<handle>` or `<id>`, or `ig:<username>`):

- **Telegram profile pictures**: full backfill (oldest→newest, one friend had
  1927) + incremental checks that deliver only *new* photos.
- **Telegram stories**: deduped via `seen_story_ids`.
- **Instagram stories + posts** (best-effort): posts are gated by a
  `last_ig_media_pk` **watermark — the first run primes the watermark and
  delivers NOTHING**. This enforces the operator's constraint: never fetch
  older IG content; only new posts after the friend was added.
- Delivery: default destination is the **log channel** (bot posts there, then
  forwards to the creator DM); alternatives: premium account's Saved Messages,
  or an explicit chat id.

## Non-negotiable rules baked into the code

1. **NEVER message anyone.** The only friend-touching operation is a silent
   `add_contact` (so the account can see restricted profiles). No DMs, no
   comments, no reactions — ever.
2. **Incremental, not re-spam.** Every photo/story/post id is recorded in
   `cache/friend_media_state.json` (`seen_photo_ids`, bounded at 5000,
   `seen_story_ids`, `seen_ig_story_pks`, `last_ig_media_pk`). Re-checking a
   backfilled friend delivers "0 new" — verified live.
3. **Backfill is crash-safe**: state checkpoints every 10 deliveries while
   walking oldest→newest, so an interrupted backfill resumes where it stopped.
4. **IG watermark**: `archive_instagram_posts()` refuses to deliver anything
   before `last_ig_media_pk` is primed. Deleting the state file would re-prime
   from newest — acceptable (still never fetches old content).
5. **instagrapi client is cached** module-level (30-min TTL, invalidated when
   the igcookies jar mtime changes). Never build+login per call.
6. **All archives serialize** behind `_ARCHIVE_LOCK` (asyncio.Lock); the
   auto-loop self-gates each cycle on live `config.FRIEND_MEDIA_ENABLED`
   / `FRIEND_MEDIA_SCHEDULE_MINUTES` with jittered sleep (never fixed cadence).

## Console (Admin → 📸 Friend Media)

Everything is in-chat, persisted via `_persist_env` = `dotenv.set_key('.env')`
+ `setattr(config)` (survives restart, applies live):
enable/disable toggle, schedule minutes, destination, IG-global toggle.
Contacts tools: 📇 browse (paginated), 🔎 search, 📞 add-by-phone
(`import_contacts`). Per-friend: ⬇️ full backfill button, IG stories/posts
toggles. Adding a friend auto-starts its backfill task.

Text states dispatched before the id gate in `register.py`:
`waiting_for_friend_add|_ig:<key>|_dest|_schedule|_search|_phone`.

## Hard-won gotchas

- **`from main import X` re-executes main.py** when running as a script:
  submodules got UNSTARTED Client copies ("Client has not been started yet").
  Fix: `sys.modules.setdefault('main', sys.modules[__name__])` right after
  imports in main.py, AND the `__main__` entry block must stay at the VERY
  BOTTOM of main.py (after `schedule_self_restart` def) — otherwise a
  partially-initialized duplicate module breaks self-restart (caused a ~31
  restart crash-loop during testing).
- **Digit strings are phone numbers to kurigram**: `get_users("7665239058")`
  → PHONE_NOT_OCCUPIED. Pass numeric ids as `int`; fall back to a
  `get_contacts()` scan by id.
- **aiogram overwrote SIGTERM** (`dp.start_polling` installs its own handler),
  so SIGTERM only stopped Bale polling and the process hung — Restart Bot
  button dead, systemd restarts hung. Fix: `handle_signals=False` in
  `modules/bale/runner.py`. Keep it.
- **Restart without sudo** (this box has no passwordless sudo):
  `kill -TERM $(systemctl show tgbot --property=MainPID --value)` →
  `Restart=always` relaunches (~20 s).
- Archive-time delivery uses a minimal `_KnownPeer(.id)` so raw numeric ids
  never need re-resolution against the premium account.

## Config knobs (.env)

`FRIEND_MEDIA_ENABLED` (0/1), `FRIEND_MEDIA_SCHEDULE_MINUTES` (0 = manual-only),
`FRIEND_MEDIA_DESTINATION` (logchannel|saved|<chat_id>),
`FRIEND_MEDIA_MAX_PHOTOS` (backfill cap), `FRIEND_MEDIA_MAX_POSTS_PER_RUN` (10),
plus IG-global toggles written by the console. State file
`cache/friend_media_state.json` is exempt from the hourly cache cleaner.

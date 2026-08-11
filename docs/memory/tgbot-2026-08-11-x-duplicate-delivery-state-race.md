# 2026-08-11 X self-DM duplicate deliveries — shared-state race

Live incident on the production bot (chat `7429671248`, `tgbot.service`, box
clock Sun 2026-08-11, all timestamps UTC unless noted). The operator self-DM'd
a few X posts and each relayed **twice, then four times**. The failure mode,
root cause and the invariant-preserving fix are documented here so nobody
re-introduces a full-dict state write.

## Symptom

One self-DM'd media post (caption `📥 X DM from x-user 1743868576920928256`,
link `https://x.com/i/status/2085032034943017419`) was delivered to the relay
chat four times:

| # | bot.log UTC | operator local (Iran, UTC+3:30) |
|---|---|---|
| 1 | 04:18:15 | 07:48 |
| 2 | 04:27:44 | 07:57 |
| 3 | 04:34:00 | 08:04 |
| 4 | 06:02:57 | 09:32 |

Crucially it was **not** one duplicated message — the **whole self-DM backlog
(≈14 tweets) re-relayed in waves**, and the waves were not aligned with bot
restarts (only 4 boots in the whole log window, 5 relay waves in one boot).

## What the logs showed

Relay waves in `logs/bot.log` on 2026-08-10 UTC:

- `04:17:54–04:18:23` wave 1 — 7 tweets incl. `2085032034943017419`
- `04:22:04–04:22:52` wave 2 — 4 *different* tweets
- `04:27:25–04:28:21` wave 1 + 2 again (with `04:27:44` = delivery #2)
- `04:33:39–04:34:36` wave 1 + 2 again (with `04:34:00` = delivery #3)
- `04:34 → 05:56` **silence** (no X relays at all, despite polls continuing)
- `05:56:20` one genuinely new tweet relayed once
- `06:02:36–06:03:32` *entire* backlog again (with `06:02:57` = delivery #4)
- then silence again until the next boot

So the X worker's cursor advanced to the newest id, held for a while, then got
**reset backwards** and the whole backlog re-relayed. The "held for a while"
gap is the key tell: a per-boot cursor bug would re-relay every poll, not in
clusters with quiet gaps between.

## Root cause: concurrent read-modify-write on a shared state file

`modules/direct_forward.py` keeps ALL three direct-forward platforms (IG, X,
TikTok) in ONE JSON file `direct_forward_state.json`. Each platform runs its
own worker coroutine:

- `_instagram_worker` — loaded the state dict **once** at startup and held
  that stale in-memory copy for the process lifetime. On every poll where it
  had new DMs or a changed `thread_activity` watermark it called
  `_save_state(state)`, which writes the **whole dict** via tmp+`os.replace`.
- `_tiktok_worker` / `_tt_run_ws` — reloaded and saved the whole dict on every
  WS push.
- `_twitter_worker` — loaded fresh per poll (correct) but its
  `_bump_cursor` + `_save_state(state)` write could be **clobbered** by any
  other worker's full-dict write.

The race: IG's stale in-memory copy still held the **boot-time** `x.last_id`.
Every time IG saved, it wrote that old `x.last_id` back over X's advanced
cursor. Next X poll: everything newer than the boot-time cursor looked "new"
again → whole backlog re-relayed. X's cursor was correct until the *next* IG
save, then reverted.

**Log correlation that pins it:** every X re-relay wave immediately followed
an IG save. `04:22` IG processed new DMs → `04:27` X re-relayed. `04:28` IG
relayed (saved state) → `04:33` X re-relayed again. `04:34→05:56` IG was idle
(no `thread_activity` change → no save) → **X held its cursor and stayed
silent**. IG activity ~`06:00` → `06:02` X re-relayed everything. Textbook
reader-writer stampede on the shared file.

There was no locking anywhere (`_ig_api_lock` is unrelated; it only guards the
instagrapi API client).

## Why the previous fix (5c2bab5 "per-message cursor persist") didn't help

That commit fixed the *restart-mid-batch* case: the X worker used to save the
cursor once after the batch, so a crash mid-batch lost progress and the whole
batch re-relayed after restart. Saving per-message fixed that. It did **not**
address concurrent clobbering by the other two workers — which is why the bug
"returned" after looking fixed. Two distinct failure classes, same file.

## The fix: merge-only, per-platform, under a lock

`modules/direct_forward.py` (all paths in the same event loop):

- Added module-level `_STATE_LOCK = asyncio.Lock()`.
- Added `_merge_state_save(state, owned: set[str])` — **synchronous** (cannot
  be interleaved by other coroutines): re-reads the freshest on-disk state,
  applies **only the caller's `owned` platform sections**, writes back
  atomically, then refreshes the caller's in-memory dict in place with the
  merged result.
- Added `async def _state_save_owned(state, owned)` wrapping that under the
  lock for the async workers.
- Rewired **every** worker save site to `_state_save_owned(state, {own_platform})`
  (IG/X/TikTok priming, per-message cursor bumps, batch saves) and the two
  sync admin pairing helpers to `_merge_state_save`.
- IG worker now also `_load_state()`s **fresh at the top of every poll** so
  admin pairing / other-platform cursor changes land within one interval.

After the change the only remaining `_save_state` caller is inside
`_merge_state_save` itself. No full-dict write can ever clobber a sibling
platform's cursor again.

## Invariant for the future (now in AGENTS.md #13)

> `direct_forward_state.json` is SHARED by the IG, X and TikTok workers.
> Never call `_save_state(state)` (full-dict write) from a worker — it will
> clobber another platform's cursor and re-trigger duplicate relay of the
> backlog. Always persist through `_state_save_owned(state, {own_platform})`
> (async workers) or `_merge_state_save(state, {own_platform})` (sync admin
> helpers). Keep the helpers synchronous and keep all cursor bumps inside the
> per-platform owned write.

## Verification

- `python3 -m py_compile modules/direct_forward.py` passes; grep confirms no
  stray full-dict saves.
- Bot restarted (`systemctl restart tgbot`); clean startup log:
  `[DirectForward] started -> chat 7429671248, 3 platform(s)`, X worker
  polling, TikTok WS connected, IG session resumed.
- Monitoring: watch `logs/bot.log` for an X relay wave following an IG
  poll/relay with no new self-DM — should now never happen.

## Files touched

- `modules/direct_forward.py` — the fix.
- `AGENTS.md` — invariant #13 expanded with the shared-state rule.
- `docs/memory/tgbot-2026-08-11-x-duplicate-delivery-state-race.md` — this doc.

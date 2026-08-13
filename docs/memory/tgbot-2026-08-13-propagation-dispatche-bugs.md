# Telegram extras were silently dead under the "merge" — two pyrogram dispatch bugs — 2026-08-13

## Symptom (reported)

Testing the subscription implementation with a second account: `/start` → greeting,
`/subscription` → plans, **then the greeting again**. Separately: `/tr`, `/yt`,
`/search`, `/web` and pasted `github.com/...` links all returned **nothing** on
Telegram (same commands worked on the Bale frontend).

## Two independent root causes (both live in pyrogram's dispatcher)

### 1. `stop_propagation()`/`continue_propagation()` swallow — `utils/propagation.py`

`pyrogram.StopPropagation` and `pyrogram.ContinuePropagation` are BOTH
`Exception` subclasses (MRO: `… -> StopAsyncIteration -> Exception`). The ported
handlers all used:

```python
try: message.stop_propagation()
except Exception: pass
```

which **swallows the signal entirely**. The handler returns normally → the
dispatcher `break`s its group → control flows to the NEXT group. Two visible
results:

- **group 0 github link** (new) also processed as a **group-1 direct-file
  upload** → duplicate replies ("📥 Received URL. Queueing job…" + a wasted
  HTTP GET of github.com HTML).
- **group-1 greeting** re-fired the welcome after `/subscription` (the old
  "double greeting"); only the group-1 `/`-swallow guard masked it.

**Fix (systemic):** new `utils/propagation.py` with `stop()` / `continue_()`
that call the method but **re-raise** the real propagation signals
(`StopPropagation`/`ContinuePropagation`) while still swallowing genuinely
unexpected errors. Refactored all **37** buggy `try: …stop_propagation() except
Exception: pass` sites in `modules/{admin,subscription,github,youtube,
translate,web}` to use the helper.

### 2. `RawUpdateHandler` stalls a handler group (`_raw_precheckout`)

pyrogram's dispatcher treats a `RawUpdateHandler` as matching **every** update
(feeds it `(update, users, chats)` unconditionally); if its callback returns
normally the dispatcher `break`s **the whole group**, so any handler registered
AFTER it in the same group never runs.

The Stars `@app.on_raw_update(group=0)` pre_checkout handler lived in the
**middle** of group 0 (right after the subscription commands). The ported extras
(translate/web/github/youtube) re-registered later in group 0 → **starved →
silently ignored all input**. `/subscription` still worked because it was
registered BEFORE the raw handler.

**Fix:** `_raw_precheckout` now `raise pyrogram.ContinuePropagation` both for
non-pre_checkout updates (let later group-0 handlers run) and after handling a
real pre_checkout (harmless continue). The raise is OUTSIDE the helper's try
block since `ContinuePropagation` is an `Exception`.

## Verification (driven live via Telethon)

| Action | Before | After |
|---|---|---|
| `/tr fa:en سلام` | no reply | `🈯 Translation (fa → en) hello` |
| `/subscription` | plans + greeting | plans only |
| `github.com/owner/repo` | browser panel + **Direct Upload** | browser panel only |
| `/search <q>` | (starved) no reply | reaches handler + results |
| `/web <url>` | (starved) | `🌐 Webpage: Example Domain …` Markdown |

`/search salehMomtaz` returning "No repositories found" is CORRECT (that's a
user, not a repo); `/search django` works.

## POT provider "goes down" — answered (no code change)

Saw `[POT] Provider process is gone (returncode=-15); will restart`. `-15` =
SIGTERM. Two distinct causes:

- **Ancient flap (already fixed in HEAD `36505e6`, 07:17):** `health_check_loop`
  slept 10s and restarted on "proc gone" without logging returncode → rapid
  restart loops under load (Bale + Telegram share one Deno POT on 4417). Now:
  sleep 30s + returncode logged + backoff.
- **Expected SIGTERM-on-restart:** every `systemctl restart tgbot` SIGTERMs the
  bot → graceful teardown calls `pot_manager.stop()` → `proc.terminate()` →
  deno exits `-15` → next boot's health loop restarts it ~5s later. The 09:50 /
  09:56 `-15`s during this session were the redeploys. Provider is healthy on
  `127.0.0.1:4417` (loopback-only, invariant #2 intact).

## Architecture note (shared vs divided)

Confirmed the intended split — this is a SHARED core, two THIN frontends, no
duplicated shared logic:

**Shared (one copy, both endpoints):** `config`, `utils/shared.queue`,
`utils/gate`, `utils/downloader/*` (extract/download/normalize/playlists/
cookies), `utils/cookie_manager`, `utils/pot_provider`, `utils/queue_manager`,
and the transport-free extras `modules/{github,youtube,translate,web}`.

**Divided (per-endpoint, never duplicated):** Telegram transport
`modules/{admin,downloader_handler,subscription,stream_handler}` (pyrogram, FULL
console); Bale transport `modules/bale/` (aiogram, LIMITED console, own 20 MB
uploader `clean_caption_text`/`sanitize_filename_for_bale`, own `BALE_*`
creator id). The Bale runner imports the shared downloader + shared extras.

## Files changed

- `utils/propagation.py` (new)
- `modules/{admin,subscription,github,youtube,translate,web}` (37 swallow-sites → helper; `_raw_precheckout` raise-on-mismatch)
- `AGENTS.md` (new invariants 19 + 20; reworked "When porting from balebot")
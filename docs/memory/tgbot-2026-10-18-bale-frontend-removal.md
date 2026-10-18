# 2026-10-18 — Bale.ai frontend removed

Operator instruction: *"I want you to get rid of bale and any trace of it except
in history obviously for recording and timeline reasons. I came to the
understanding that Bale infrastructure is so inferior to Telegram that it is
better to have another separate project for it, but I won't, because I don't want
a bale bot anymore — this Bale integration just caused unnecessary problems."*

**Result: the repository is Bale-free. One frontend, one poller, one log channel,
one uploader.** Only the historical record remains (this note, `BALEBOT.md`, the
`blueprint.md` phase entries and the dated audit notes).

## What was deleted

| Path / setting | What it was |
|---|---|
| `modules/bale/` (`__init__.py`, `runner.py`, `uploader.py`, `admin.py`) | aiogram v3 poller for `tapi.bale.ai` (manual `getUpdates` drain, no webhook), the 19/20 MB direct-multipart uploader (`sanitize_filename_for_bale`, `clean_caption_text`), and the LIMITED admin console |
| `utils/logger/bale.py` | `BaleChannelHandler` → the separate `bale_log` Telegram channel (a `bale.`-prefixed child logger) |
| `apiDocuments/baleAPI.md` | the Bale Bot API reference (`tapi.bale.ai` is a Telegram API `~v6` clone) |
| `aiogram==3.30.0` (requirements.txt) | the framework the Bale poller ran on; nothing else imported it |
| `config.py`: `BALE_TOKEN`, `BALE_SYSTEM_CREATOR_ID`, `BALE_LOG_CHANNEL_ID`, `BALE_HARD_LIMIT_MB`, `BALE_SPLIT_TARGET_MB`, `BALE_DIRECT_DOWNLOAD` | every Bale knob |
| `.env` / `.env.example` | the same keys (the live `.env` was backed up to `.env.autobak` before the edit) |
| `main.py` | the Bale log-channel wiring in `setup_system_logger()` (including the `_bale_filter` strict split) and the `_bale_wrapper()` frontend task in `main_engine()` |
| cross-frontend branches/comments | `_tg_filter`'s "drop Bale lines" rules, `utils/url_validator.py` ("both frontends"), `utils/filenames.py`, `utils/downloader/thumbnails.py`, `modules/downloader_handler.py`, the `# ported from balebot` banners on the shared extras, `requirements.txt` comment, `.env.example` header |

`RUNTIME_SETTINGS` never held a Bale knob (documented in AGENTS.md), the Go
monitor never referenced `BALE_LOG_CHANNEL_ID` (the `strings` hit on the prebuilt
binaries was `math/big.karatsubaLen`, a false positive — no rebuild needed), and
no other module imported `modules.bale`, `utils.logger.bale` or `aiogram`
(verified by `git grep` after the change).

## Docs

* **Live docs rewritten to the Telegram-only state:** `README.md` (feature
  bullets, Quick-start env steps, first-run prose, admin-console table row, Logs
  section), `docs/USER_GUIDE.md` (the "Where it works" column, the Bale-quirks
  blockquote, the Bale console section, the Bale lock/size troubleshooting
  entries), `docs/UBUNTU_VPS_SETUP.md` (the "Using Bale as well?" log-channel
  block), `docs/INFRA.md` (logger split → historical, the shared/divided
  architecture paragraph, the aiogram SIGTERM gotcha → historical),
  `docs/COOKIES.md`, `docs/DOWNLOADER.md`, `docs/reference/README.md`.
* **AGENTS.md:** the top-of-file description is now Telegram-only, with a
  **"Bale.ai frontend REMOVED (2026-10-18) — do not re-add without an explicit
  ask"** banner mirroring the admin-WebApp removal; the file-map row points at
  the history instead of `modules/bale/`; the logger section records the removal
  of the strict split; the sanitiser caller list and the RUNTIME_SETTINGS gotcha
  lost their Bale items; the "When porting from balebot" section became
  "The extras are shared modules (historical: ported from balebot)" with the
  rule that outlives the removal: never duplicate an extra's logic.
* **History kept, clearly labelled:** `docs/memory/BALEBOT.md` (REMOVED banner
  plus the full original design/threat-model text), its row in
  `docs/memory/README.md`, the `blueprint.md` Phase 22/23/26/27/28 entries, and
  this note. `blueprint.md` gained **Phase 29** recording the removal in the
  timeline.

## Verification

```bash
git grep -nE "modules\.bale|utils\.logger\.bale|BaleChannel|aiogram|BALE_"   # empty
python3 -m py_compile $(git ls-files '*.py')                                  # clean
for f in tests/test_*.py; do venv/bin/python "$f"; done                       # 7/7, 22/22, 44/44
grep -c -i bale .env                                                          # 0
grep -rl -i bale modules/ utils/ main.py config.py requirements.txt            # empty
```

Live after `systemctl restart tgbot`:

* startup shows **no** `[Bale]`/`aiogram` lines and **no** "Bale logging linked"
  line — the logger now attaches exactly one channel handler;
* `[Logger] Telegram logging linked to LOG_CHANNEL_ID` → the single log channel
  keeps receiving bot, direct-forward, PO-provider and friend-media lines;
* Direct-forward (X self-DM + TikTok WS), the friend-media watcher, the cookie
  refresher and the PO provider all came up normally in the same restart;
* **`grep -cE "bale|aiogram"` over the log since the restart: 0**;
* re-driven with `tools/telethon_drive.py` (real inline-keyboard presses):
  shared extra `/tr fa:en سلام` → "🈯 Translation (fa → en) hello" ✔,
  YouTube `jNQXAC9IVRw` `--pick v` → video delivered ✔,
  admin console chain `admin_cookies_menu → admin_main → admin_premium_menu →
  admin_main → admin_close` → **5 presses done** ✔.

## Notes / leftovers

* The operator's private Telegram channel `bale_log`
  (id later removed from `.env`) still exists as a chat — it is simply never
  written to any more; delete it whenever convenient.
* `apiDocuments/telegramAPI.md` remains (Telegram is the only API this bot uses).
* Nothing about the *Iran-shutdown rationale* was deleted from the record: the
  reasoning lives on verbatim in `docs/memory/BALEBOT.md` §"WHY this frontend
  exists", so the decision can be revisited with full context if a standalone
  Bale project ever happens.

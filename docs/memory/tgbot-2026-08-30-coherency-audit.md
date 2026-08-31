# 2026-08-30 coherency / consistency audit (static + production verified)

A repo-wide audit for **conflicts, contradictions and incompleteness** across
`modules/`, `utils/`, `config.py`, `.env.example`, `docs/` and `AGENTS.md`.
Method: AST-based static analysis over all tracked modules (not blind file
dumping), then targeted reads, then production verification of every fix.

## Why AST tooling instead of "load every file into context"

95 tracked modules / ~20k LOC. Tooling found things reading could not:

- **Callback-graph diff** — extract every emitted `callback_data` (including
  f-string prefixes) and every handler route (`data == x`, `data in (...)`,
  `data.startswith(x)`, `filters.regex(r"^prefix")`, aiogram
  `F.data.startswith`), then diff both ways. Result: **209 emitted callbacks,
  0 dead buttons**, 3 orphan routes (2 false positives from helper-variable
  emission, 1 intentional legacy alias).
- **Cross-module duplicate-definition scan** — surfaced helpers duplicated
  verbatim across modules.
- **pyflakes + autoflake** — 136 findings → 11 (all benign unused locals).

### Tooling gotchas worth remembering

- Scope the walk: `group=-2` handlers live in **`main.py`**, not `modules/`.
  Scanning only `modules/` produced a wrong "documented group doesn't exist"
  conclusion that a follow-up grep caught. **Verify before "fixing".**
- `grep ... | head -N` silently truncates file order and will make a live
  symbol look dead. `IG_DIRECT_MQTT_ENABLED` was first mis-filed as a dead
  knob this way; it is used at `modules/direct_forward/instagram.py`.
- Unused-import removal must skip `__init__.py` re-export modules, where
  "unused" names are the public API.

## Bugs found and fixed (all verified in production)

1. **`friend_media`: Media object passed where instagrapi wants a pk.**
   `cl.photo_download(media_obj, ...)` — instagrapi 2.18.14 wants an `int` pk.
   `str(media_obj)` is a pydantic repr full of underscores, which hits
   `media_pk()`'s `media_pk, _ = media_id.split("_")` → *"too many values to
   unpack (expected 2)"*. The CDN-fallback download path failed **100%** of
   the time (16x in `bot.log`, once per hourly cycle). Fixed by passing `.pk`.
2. **Same path, next layer: carousels.** Once the pk arrived, `assert
   media.media_type == 1, "Must been photo"` fired — `media_type` 8
   (carousel) was routed to `photo_download`. Now dispatched to
   `album_download`.
3. **`e` referenced after its `except` block.** In
   `modules/friend_media/admin.py`, the "IG session expired, re-upload
   cookies" alert read `e` outside `except IGUnavailable as e:` (Python
   unbinds `e` on block exit) → `NameError`, swallowed by the enclosing
   `except Exception: pass`. The operator silently never got the alert the
   code exists to send. Message is now captured inside the handler.
4. **`IG_DIRECT_MQTT_ENABLED` was overloaded (safety bug).** Besides enabling
   the experimental MQTToT push listener, it shortened the IG checkpoint
   freeze from 3–5h to 60–120s — silently disabling the retry-storm
   protection of invariant #13 for anyone enabling "instant push". Also
   inconsistent: the polling-site checkpoint always froze 3–5h. Split into a
   separate `IG_DIRECT_CHALLENGE_FREEZE_TEST` knob honoured at **both** sites.
5. **Leaked MQTT task.** `mqtt_task` was created and assigned but never
   cancelled, so on any exit it kept a second IG client + MQTToT socket
   alive. Poll loop now wrapped in `try/finally`.

All four IG failure modes were invisible because the failure log was a bare
one-liner; it now passes `exc_info=True`.

### Fix verification timeline (from `logs/bot.log`)

The two friend-media fixes are individually confirmed — each error stops
exactly at its own deploy, and neither has recurred since:

| error | events | last seen | fix deployed |
|---|---|---|---|
| `too many values to unpack (expected 2)` | 16 | 20:21:30 | pk fix, ~20:23 |
| `AssertionError: Must been photo` | 2 | 20:26:57 | album fix, ~20:29 |

Note the second error only *starts* appearing after the first fix: until the pk
reached instagrapi, execution never got past `media_pk()`, so the carousel
assertion was unreachable. Fixing one silent failure exposed the next — which
is the normal shape of these, so re-check the logs after each deploy rather
than assuming a fix is final.

**Log-filtering gotcha:** `awk -F'|' '$1 > "2026-08-31 03:16:00"'` on `bot.log`
produces false positives. Traceback continuation lines have no `|` delimiter,
so `$1` is the whole line and `"AssertionError…" > "2026-08-31…"` compares
true. Always anchor on `^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}` to select real
log events.

## Doc/code contradictions resolved

- **`generate_session.py`** still existed at the repo root, and
  `docs/UBUNTU_VPS_SETUP.md` still told operators to run
  `python generate_session.py` — while `AGENTS.md` forbids a terminal
  generator and `docs/INFRA.md` already said the flow "is gone". Script
  deleted; that section rewritten for the in-chat flow (Admin → 👑 Premium →
  🔑 Generate Session, dial-pad code, 💾 Save to .env, self-restart); stale
  pointers in `config.py`, `blueprint.md`, `utils/premium_session.py` updated.
- **AGENTS.md handler-group table** was labelled misleadingly: most callback
  dispatchers (`^admin_`, `^fm_`, `^dl:`, `^pl:`, `^pln:`, `^plx:`, `^sub:`)
  are in **group 0**; only `^chkjoin:` and `^gh:` are in group 2. And the
  downloader link handler is **group 1**, which is precisely why a group-0
  extra calling `stop(message)` preempts the direct-file fallback. Replaced
  with the verified table. (The `-2`/`-1` layering itself is correct, and
  `dispatcher.py` sorts groups numerically — insertion order is not an issue.)
- **`GITHUB_ZIP_MAX_FILES` / `GITHUB_ZIP_MAX_BYTES`** were read via
  `os.getenv` inside `modules/github/handlers.py`, bypassing the config layer
  and absent from both `config.py` and `.env.example`. Moved into `config.py`
  (defaults unchanged: 750 / 512 MB) and documented.
- **`SUB_WEBAPP_SECRET`** — leftover of the subscription webapp removed
  2026-08-26, read by nothing. Removed, plus the mislabelled
  "WebApp / domain" section (it configures the **streaming** server, the only
  thing FastAPI still serves).
- **`IG_DIRECT_MQTT_ENABLED` comment** still claimed instagrapi 2.1.2 "has NO
  Realtime" and told the reader to upgrade; the venv ships 2.18.14, which
  provides `realtime_*`.
- **`tools/test_ig_mqtt.py`** printed its whole report **on import** and its
  conclusion was hardcoded ("instagrapi 2.1.2 has NO RealtimeMixin") while the
  same run printed `realtime_connect`/`realtime_on` directly above it. The
  verdict is now *computed* from the probe (checks the four `realtime_*`
  methods the hybrid listener actually calls), reads the version from installed
  metadata (instagrapi has no `__version__`), and is `__main__`-guarded.
  Root cause worth generalising: **a conclusion stated in prose instead of
  derived from the check beside it will always eventually contradict it.**

## Incompleteness removed

- `fm_settings` — a settings screen fully implemented but unreachable (no
  button emitted it; its own docstring admitted as much). Its only control,
  `⏱ Set check interval`, is already on the main menu, so wiring it up would
  only add a redundant nested path. Removed along with `_settings_keyboard()`.

## Deliberately kept

- `admin_sub_set_channel` — an orphan route, but **inline keyboards persist in
  chat history**, so old messages can still emit it. Kept and documented as a
  legacy alias rather than removed.
- 11 `local variable assigned but never used` — all benign (return values
  intentionally discarded, e.g. `zip_path`, `ok`). Not worth the churn.
- `XCHAT_POLL_MIN/MAX_SECONDS` — absent from `config.py` on purpose: consumed
  only by the Deno sidecar, which receives them via the wrapper's blanket
  `export` in `tools/start_xchat_bridge.sh`.

## Verification performed

`py_compile` on all tracked modules · `bash -n install.sh run.sh uninstall.sh`
· pyflakes 136 → 11 · **73/73 modules import cleanly** · Go monitor
`go test ./...` green · 0 dead inline-keyboard buttons · bot restarted (final
pass via `sudo systemctl restart tgbot`, restart counter reset to 0 = no
crash-loop) and logs checked: no `too many values to unpack` or
`Must been photo` after the respective fixes.

Elevated-access checks (need `sudo`):

- `tgbot.service` unit at `/etc/systemd/system/tgbot.service` is **rendered**
  (no `__USER__`/`__PROJECT_DIR__`/`__MEMORY_MAX__` left over — invariant #7).
- Invariant #1: **no `ulimit -v`** in the unit or `run.sh`; memory protection
  is `MemoryMax=2621440000` (2.4 G), and `run.sh` only sets `-n/-u/-f`.
- Invariant #2: PO provider listens on **127.0.0.1:4417 only** (upstream logs
  `[::]:4417`, so the localhost patch is doing its job). Port is 4417 because
  `.env` sets `YTDLP_POT_PORT` — not drift from the 4416 default.
  `0.0.0.0:8080` is the FastAPI **streaming** server, intentionally public.
- Invariant #15: `build/tgbot-monitor` is byte-identical to
  `prebuilt/tgbot-monitor-linux-amd64` and newer than the Go sources, so no
  prebuilt rebuild is owed.
- Entrypoint scripts are `100755` in the git index *and* on disk.
- Services: `tgbot` enabled+active, `tgbot-xchat-bridge` enabled+active,
  `cookie-watch` enabled+active, `tgbot-monitor` disabled (by design — the bot
  spawns it).

## Secrets re-check

`git ls-files cookies/` → 90 tracked files, **0 containing real cookie rows**
(they are ~200-byte empty Netscape templates, intentionally committed). All
four live jars (`cookies/{youtube,instagram,tiktok,twitter}/`) confirmed
`git check-ignore`d. No `.env` / `*.session` tracked.

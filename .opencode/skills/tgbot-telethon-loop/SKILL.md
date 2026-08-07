---
name: tgbot-telethon-loop
description: >
  Use when driving/testing tgbot (a Telegram downloader bot) from this box via
  Telethon as the operator's user account: pressing inline keyboard buttons,
  sending links, auto-picking format buttons, or running a "test all features"
  health pass (YouTube/TikTok/Instagram/X/SoundCloud/Dailymotion, playlists,
  direct files, cancel, admin console). Also use for the SELF-CORRECTING
  FEEDBACK LOOP: an agent that edits the bot's source, deploys it, re-drives
  the bot through Telethon, reads errors from logs and button responses, fixes
  the code, and converges to a stable point like a phase-locked loop. Keywords:
  telethon, inline keyboard, press button, drive bot, test features, feedback
  loop, self-correcting, self-healing, health pass, fix the bot, harden the bot.
---

# Telethon ↔ Bot self-correcting feedback loop

## Mental model: a phase-locked loop

Treat the bot + your agent as one closed-loop control system. Every block maps
to a concrete action, and the loop converges when no new errors appear on a
re-test (the "locked", stable point):

| PLL block | Agent equivalent |
|---|---|
| **Phase detector** (VCO output vs reference) | Log scan + Telethon observation: collect errors, tracebacks, warnings, and what the bot actually sent back |
| **Loop filter** (error → correction) | Your reasoning: classify the error, find the faulty code path |
| **VCO / plant** (the thing being steered) | The bot source + running process |
| **VCO control input** | Code edit → `python3 -m py_compile` → `systemctl restart tgbot` |
| **Feedback path** (output → input) | Re-drive the same feature via Telethon, press the same buttons, compare outcomes |
| **Lock / stability** | Zero new errors, every feature green, security checks passing — loop terminates |

The golden rule of the loop: **never trust the code you read as "working";
always close the loop by driving the running bot and reading what it emits.**
And: never "fix" from inspection alone — a fix is only real after the feedback
path (re-drive) shows it converged.

## When to use

- User says "test all the features", "health pass", "check the logs for
  abnormalities", "press the inline buttons", "drive the bot", "interact with
  the bot", "fix the bot", "make it self-correcting / self-healing".
- You need to verify a change you made by exercising the real bot UI
  (inline keyboards), not just unit reasoning.
- The operator asks you to maintain this repo: this loop is the maintenance
  ritual — sense → drive → verify → fix → harden → converge.

## Prerequisites / environment

- Repo lives **in-place on the box**: `/home/dev/opencode/tgbot`. The bot runs
  as `tgbot.service` (systemd, user `dev`). The old US test VPS is retired —
  do not reach for `66.23.198.52`.
- The agent has the operator's **user account** (Telegram creator `id`
  `7429671248`) authenticated as `telethon_session.txt` (git-ignored) and the
  bot username `AngelaBalzac_bot`. Creator id is also `config.SYSTEM_CREATOR_ID`.
- Sudo for service restarts: use `sudo -S` with the operator-supplied password
  (from session context), **never hardcode it in any file** — the repo copy of
  this skill is committed to git.
- Everything is driven from `venv/bin/python` (repo venv). The repo has a
  Python venv at `venv/`.
- **Time**: the box clock is authoritative for log correlation. Note the
  operator may think the date is different from the box clock; trust the box.

## Toolset (all in `tools/`)

### `telethon_login.py` — one-time
Generates `telethon_session.txt`. Run only if the session is missing/expired
(interactive; needs `API_ID`/`API_HASH` from `config`/.env).

### `telethon_bot.py` — quick probe, at most one press
```bash
venv/bin/python tools/telethon_bot.py "https://youtu.be/..." --press '🎥 240p' --watch 20
```
Sends one message, prints bot replies for `--watch` seconds, optionally presses
the first button whose label/data contains `--press`. Good for a smoke test;
use the driver for anything scripted.

### `telethon_drive.py` — the workhorse
Full event loop: sends a URL/message, listens for new messages **and** message
edits, executes a chain of `--press` patterns, and auto-picks format buttons.

```bash
# single-video format pick (video or audio), size-limited
venv/bin/python tools/telethon_drive.py \
  --url "https://www.youtube.com/watch?v=jNQXAC9IVRw" \
  --pick v --size-max-mb 50 --timeout 150

# sequential inline-button chain (playlist tier keyboard)
venv/bin/python tools/telethon_drive.py \
  --url "https://www.youtube.com/playlist?list=<ID>" \
  --press 'pl:*:whole' --press 'pl:*:vl' --timeout 230

# admin console navigation
venv/bin/python tools/telethon_drive.py \
  --message "console" --press 'admin_cookies_menu' --press 'admin_main' \
  --press 'admin_premium_menu' --press 'admin_main' --press 'admin_close' --timeout 90

# cancel a format keyboard
venv/bin/python tools/telethon_drive.py --url <url> --press 'dl:*:cancel' --timeout 60
```

Flags:
- `--url URL` / `--message TEXT` — what to send.
- `--press PATTERN` (repeatable) — a **sequential** chain. Each pattern matches
  the NEXT button whose label contains it OR whose callback data matches
  (supports `*` wildcards for volatile cache ids). A `--press` chain runs in
  order; only one button per pattern is clicked.
- `--pick v|a` — auto-click the first video/audio `dl:` button whose size is in
  `--size-min-mb`/`--size-max-mb`. Size parsing handles `K/M/G` and the `~`
  estimate prefix.
- `--timeout SECS` — total listen window. **This must be smaller than the
  bash tool timeout you pass** or the shell kills the driver mid-test.

Output is JSON lines on stdout; the last line is the `RESULT` (media delivered,
presses done, last text). Always confirm a job via the bot log too (below).

## Inline keyboard / callback-data cheat sheet

These data patterns are what the bot's buttons carry (they change per link via
the cache id — use `*` in `--press`):

- **Single video** format keyboard: `dl:<cacheid>:v:<format_id>`,
  `dl:<cacheid>:a:<format_id>`, `dl:<cacheid>:cancel`. The picker auto-detects
  `:v:` vs `:a:`.
- **Playlist** decision: `pl:<cacheid>:whole|explore|single|menu|cancel`.
- **Playlist tiers** (after `whole`): `pl:<cacheid>:vh|vm|vl` (video high/med/
  low) and `ah|am|al` (audio). Tiers apply to every video.
- **Admin console** (creator only): `admin_*` (e.g. `admin_cookies_menu`,
  `admin_premium_menu`, `admin_main`, `admin_close`, `admin_toggle_doc`).
  Re-tapping an already-open menu is harmless (the bot swallows
  `MESSAGE_NOT_MODIFIED`).
- Size labels look like `🎥 240p (617K)`, `🎵 128k (~469K)`, `🎥 1080p (~225M)`.
  The `~` means "estimate, may run a little high". A bare number without `~`
  is an exact content-length (or a successful CDN probe).

## The loop playbook — run this every maintenance pass

### Phase 0 — baseline
```bash
date && TZ=Asia/Tehran date          # box clock (authoritative)
sudo -S systemctl is-active tgbot <<< '<password>'
systemctl show tgbot -p MainPID -p NRestarts
df -h / | tail -1; uptime
```

### Phase 1 — sense (the phase error)
Scan `logs/bot.log` for abnormalities since the last restart:
```bash
grep -nE "^2026.*\| (ERROR|WARNING)" logs/bot.log | tail -40
grep -n "Traceback" logs/bot.log | tail -5
```
Classify what you find:
- `socket.send()` / reconnect storms at one timestamp → transient network blip,
  self-heals; note it, don't chase it.
- `upload.SaveBigFilePart` wait warnings → normal Telegram upload throttle.
- A real `Traceback` → **the phase error**; open the file:line and read the
  code path.
- `MESSAGE_NOT_MODIFIED` → benign re-tap, already handled silently.

### Phase 2 — drive (close the loop per feature)
Pick small test URLs (≈≤50 MB each) and pre-validate liveness offline first:
```bash
venv/bin/python -c "import yt_dlp; ..."   # yt_dlp --simulate: title/duration/formats
```
Known-good small sources: YouTube `jNQXAC9IVRw` (19 s), TikTok
`@scout2015/video/6718335390845095173` (10 s), SoundCloud
`soundcloud.com/markronson/uptown-funk` (30 s preview), small YouTube
playlists (e.g. NASA's `list=PLb911ot23pTQ` = 4 short clips). Direct files:
`proof.ovh.net/files/1Mb.dat` or learningcontainer sample mp4. Vimeo now
requires login — skip it.

Drive every feature (table of `--url` + press chain):
1. YouTube video: `--pick v --size-max-mb 50`
2. YouTube audio: `--pick a`
3. YouTube playlist: `--press 'pl:*:whole' --press 'pl:*:vl'`
4. TikTok / Instagram / X: `--pick v --size-max-mb 50`
5. SoundCloud: `--pick a`
6. Dailymotion (or other yt-dlp site): `--pick v --size-max-mb 50`
7. Direct file: `--url <direct.mp4> --timeout 120`
8. Cancel: `--url <yt> --press 'dl:*:cancel'`
9. Admin console chain (see toolset example).

### Phase 3 — verify (read the feedback)
The driver `RESULT` alone is not proof (it may exit a second before delivery).
Confirm in the log:
```bash
grep -nE "Job Successful|Job Failed|Direct Upload|Playlist skip" logs/bot.log | tail
```
- `✅ Job Successful:` `file.mp4` → media actually delivered.
- `✅ Direct Upload:` → direct path finished.
- `✅ Playlist item X/N:` / `⚠️ Playlist skip Y/N: <reason>` → playlist flow;
  skips are by design (bad/private entries are skipped, never fatal).

### Phase 4 — correct (steer the VCO)
On a failure: read the code path, edit, then **validate + deploy + re-drive**:
```bash
python3 -m py_compile <edited.py>        # syntax gate
sudo -S systemctl restart tgbot <<< '<password>'
sleep 12 && systemctl is-active tgbot    # confirm up
grep -E "POT. Provider is healthy|DirectForward. started" logs/bot.log | tail -2
```
Re-run exactly the failing Phase 2 command. Repeat until it converges.

### Phase 5 — harden (stability margin)
After features pass, run the security checklist (below). Tighten perms, patch
guards, then re-drive anything affected.

### Phase 6 — converge, document, ship
- Confirm **zero new** tracebacks/errors since the fixes.
- Write/update a `docs/memory/tgbot-<date>-<topic>.md` capture (findings,
  what was tested, what was fixed, verification).
- `git add` only source + docs (never secrets), commit with the repo's
  `fix:`/`docs:`/`feat:` style, push. Verify no secrets staged:
  `git ls-files | grep -E '\.(env|session)$|cookies/'` must be empty.

## Security & hardening checklist (Phase 5)

- **Auth gate**: group −1 interceptor stops non-creator users, auto-blacklists
  intruders, logs to the channel. `database.json` should only ever hold the
  creator as implicitly authorized.
- **Secrets in git**: run `git ls-files | grep -E '\.(env|session)$|cookies/'`
  → must be empty. `git check-ignore -v <path>` confirms a rule. If a secret is
  tracked, stop and fix `.gitignore` before committing anything.
- **PO provider** must bind `127.0.0.1` only: `ss -tlnp | grep 4417`.
- **SSRF**: the direct-file path refuses loopback/private/link-local targets
  (`_is_ssrf_target`). Probe it:
  `--url "http://127.0.0.1:4417/"` must come back refused.
- **Scheme gate**: only `http://`/`https://` are links; `file://`/`ftp://` are
  plain text and never downloaded.
- **Secret file perms**: `.env`, `telethon_session.txt`, `direct_ig_session.json`
  should be `600`; cookie jars locked `444` (bot writes via snapshots).
- **No leakage**: grep replies/logs for `BOT_TOKEN`/`API_HASH` values — absent.

## Gotchas

- **Bash tool timeout vs driver `--timeout`**: if the driver needs 150 s, pass
  the bash tool a timeout strictly larger (e.g. 200 000 ms) or the shell kills
  the driver mid-run and you get an empty RESULT.
- **Downloads serialize on one queue worker**; metadata fetches run
  concurrently. A URL whose format keyboard you didn't click does NOT download.
- **Verify in logs, not just the driver window.** The upload often lands right
  after `--timeout` expires.
- **Never restart the bot mid-download** of something the operator cares about;
  restarts drop in-flight jobs. Do it when the queue is idle.
- **Clean up after yourself**: a pass leaves stale admin/format keyboards and
  test messages in the operator's chat. Delete your own sent messages and
  button-carrying messages (keep delivered media) via a small Telethon script.
- **Secrets hygiene**: the repo copy of this skill is committed to git. Never
  put the session string, sudo password, `API_ID`/`API_HASH`, or `BOT_TOKEN`
  values in this skill, in AGENTS.md, or in any committed file.
- **The loop is the product.** A "fix" you never re-drove is not a fix.

## Worked example

`docs/memory/tgbot-2026-08-07-health-pass.md` is a full pass done with this
loop: 9 platform tests green, 4 bugs found and fixed (site routing, HLS size
guard, silent `MESSAGE_NOT_MODIFIED`, SSRF guard), perms tightened, documented
and pushed. Read it to calibrate expectations of a converged pass.

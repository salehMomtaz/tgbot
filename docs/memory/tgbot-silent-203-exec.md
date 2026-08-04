# Silent bot outage: systemd 203/EXEC (lost exec bit on run.sh)

**Date:** 2026-08-04 · **Area:** `deploy/tgbot.service`, `run.sh`, `install.sh`,
`uninstall.sh`, git file modes

## Symptom

The bot went **completely silent** — no replies, no log-channel activity.
`logs/bot.log` ended cleanly (last line was a normal relay). The service was
not crashed in the "python died" sense; it was crash-looping invisibly:

```
$ systemctl status tgbot
Active: activating (auto-restart) (Result: exit-code) since …
Process: 2396 ExecStart=/home/dev/tgbot/run.sh (code=exited, status=203/EXEC)
```

`NRestarts` climbed ~5/s (817 in under 2 h). The Go monitor noticed (the VPS
was hot from the restart storm) and fired 80% warnings — the first *visible*
signal of the problem.

## Root cause

`deploy/tgbot.service` has `ExecStart=__PROJECT_DIR__/run.sh` — systemd exec's
`run.sh` **directly**, so it needs the executable bit. `run.sh` (and
`install.sh`/`uninstall.sh`) had been committed to git as **mode `100644`
(non-executable)**. Any `git pull` resets the working-copy mode to the tracked
mode; when the repo was pulled on the VPS that morning, `run.sh` came back
`0644` and every subsequent service start failed with `203/EXEC`.

`203/EXEC` = "executable not found or not executable" — **not** a Python
traceback, so `bot.log` stays clean. The unit was fine; the file it launches
had lost its exec bit.

## Fix

1. **VPS (immediate):** `chmod +x ~/tgbot/run.sh`; `systemctl restart tgbot`.
2. **Repo (permanent):** commit the exec bit so pulls can't strip it again —
   `chmod +x run.sh install.sh uninstall.sh` then
   `git update-index --chmod=+x <file>` for each, and commit the mode change
   (`100644 → 100755`).
3. **Hardening:** `install.sh` now runs `chmod +x run.sh install.sh uninstall.sh`
   every invocation (right after `PROJECT_DIR` is set), so a bad pull self-heals
   on the next install/upgrade run.

## Verification

- `git ls-files -s run.sh` → `100755 …` (was `100644`).
- On the VPS: `systemctl is-active tgbot` → `active`; `NRestarts` frozen at 817
  (0 failures since the fix); pyrogram sessions start; a live Instagram DM was
  relayed seconds after restart.
- Both `python main.py` processes are correct (PID 2999 cwd=~/tgbot,
  PID 560 cwd=~/balebot in its own cgroup) — no getUpdates conflict.

## Fingerprint / diagnosis checklist

- Bot silent but `systemctl status tgbot` shows `activating (auto-restart)` +
  `203/EXEC`, and `logs/bot.log` ends cleanly → **check file mode of run.sh
  first** (`ls -l`, compare `git ls-files -s`).
- Also check balebot: it uses its own unit; its `run.sh` was still `0755`, so
  only tgbot went down.
- Don't just chmod the VPS copy — the tracked mode was `100644`, so the next
  pull would break it again. Fix the git index and commit (AGENTS.md Gotchas).

## References

- AGENTS.md → Gotchas: **"Entrypoint scripts must stay executable — systemd
  calls `run.sh` directly"** (added from this incident).
- `install.sh` chmod-hardening block right after `PROJECT_DIR=`.

# tgbot system monitor — design & invariants

The standalone system monitor is a **static Go binary** (`cmd/tgbot-monitor/` →
`build/tgbot-monitor`, built by install.sh) that reports the VPS health to the
bot's log channel. It is a /proc-only health reporter that runs as **its own
process** — either the `tgbot-monitor.service` systemd unit (installed by
`install.sh`, survives reboots) or a detached fork spawned by `main.py` on bot
startup (`utils/system_monitor.py::spawn_detached_monitor`, deduped so it never
stacks a duplicate).

It is the project's **one Go component**. The port rationale is in
`docs/go-feasibility.md`; this file captures the operational invariants.

## Why it exists / what it replaces

The old approach was an in-process uvicorn log line (`[System] Disk usage ...`)
— only visible when the bot was up. If the bot hung or crashed, nobody learned
the box was hot. The monitor keeps reporting even when the bot is dead, which
is exactly the failure mode it observes. It was originally written in Python
(`utils/system_monitor.py` had the full engine) and ported to Go because it is
the one component whose profile — long-lived, resident, /proc-only, no shared
library, must outlive the bot — matches Go's strengths: ~5 MB static binary vs
~27 MB CPython RSS on a 961 MB VPS, no venv/interpreter to break, and a real
test suite (`go test`).

## Non-obvious invariants (do not break these)

1. **The engine lives in Go; the Python file is only a spawner.**
   `utils/system_monitor.py` is deliberately thin: `spawn_detached_monitor()`
   and `is_running()`. All sampling/formatting/sending is in
   `cmd/tgbot-monitor/`. Do NOT re-add a Python monitoring engine there.

2. **Zero dependencies (Go side).** Stdlib + `/proc` only. No psutil/gopsutil,
   no database, no dashboard. Measured on a 1-core/1 GB VPS: a fraction of a
   percent of a core and a few MB RSS.

3. **Independent of the bot.** The binary talks to Telegram via a plain
   `net/http` POST to the Bot API (like `utils.logger`), NOT pyrogram, NOT the
   bot's event loop. This is why it must stay a separate process, not a task
   inside `main.py`.

4. **Output format is a contract.** The `#system` report and the warning carry
   the **VPS local date-time** (`VPS time:`) line and are byte-identical to
   what the channel already showed (verified by diffing the Go formatter
   against the old Python one). Do not change spacing/emoji/format specifiers
   without a reason. Tests pin the exact strings.

5. **Reports every `SYSMON_REPORT_INTERVAL` samples** (default 60 × 15 s =
   15 min) and **warns at `SYSMON_WARN_PCT`** (default 80) on CPU/RAM/disk,
   repeating every `SYSMON_WARN_SECONDS` (default 60) **until every metric drops
   back below** — nag-until-fixed, never floods, stops on recovery.

6. **Config is self-contained via a minimal dotenv reader.** The systemd unit
   deliberately has no `EnvironmentFile=` (run.sh owns .env parsing for the
   bot); the Go binary parses `.env` itself (real env wins, python-dotenv
   semantics), so it stays standalone. If `BOT_TOKEN` or `LOG_CHANNEL_ID` are
   missing, `run()` exits 2 with a stderr message rather than silently sampling
   forever.

7. **Per-process CPU% needs deltas.** `/proc/<pid>/stat` utime+stime are
   cumulative since process start, so the scanner keeps a pid→(ticks, wall)
   snapshot from the previous poll and diffs (top's method). First sample of a
   new pid is 0%. `procPrev` is a package global — reset it if you add a way to
   restart the scan.

8. **Never blocks the sampler.** Each send is fire-and-forget in a goroutine
   with an 8 s timeout (`sendTelegram`), and a bad sample is skipped — the loop
   sleeps `POLL_SECONDS` and continues.

9. **Dedup: pidfile + /proc scan, understood by BOTH languages.** The Go binary
   writes project-root `system_monitor.pid` on start and removes it on exit.
   The Python `is_running()` checks that pidfile (stale/recycled-pid safe) and
   also scans `/proc/*/cmdline` for an argv0 containing `tgbot-monitor`, so the
   systemd unit and the bot's detached spawn can never stack. If you change the
   binary's argv0 or pidfile path, update BOTH sides.

10. **The systemd unit is a template** (`deploy/tgbot-monitor.service`) with the
    same `__USER__` / `__GROUP__` / `__PROJECT_DIR__` placeholders as
    `tgbot.service`, rendered by install.sh, `ExecStart` = the Go binary path.
    It has **no** `__MEMORY_MAX__` placeholder — the monitor is tiny
    (MemoryMax=256M is hardcoded headroom, NOT a tight cap; see AGENTS.md #1
    re `ulimit -v`). Installed but **not auto-enabled**; enable with
    `systemctl enable --now tgbot-monitor`.

## Operational notes

- Live log: `journalctl -u tgbot-monitor -f`; the detached path (non-systemd)
  writes `logs/system_monitor.log`.
- If the bot and the systemd unit both run, only the systemd instance samples —
  the bot's `spawn_detached_monitor` sees `is_running() == True` and returns.
- Env knobs: `SYSMON_POLL_SECONDS`, `SYSMON_REPORT_INTERVAL`,
  `SYSMON_WARN_PCT`, `SYSMON_WARN_SECONDS`, `SYSMON_TOP_N`,
  `SYSMON_HISTORY_SAMPLES`, `SYSMON_DISK_PATHS` (all in `config.py` +
  `.env.example`; the Go binary reads the same names).
- Build manually: `cd cmd/tgbot-monitor && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o ../../build/tgbot-monitor .`
- Tests: `cd cmd/tgbot-monitor && go test ./...` (this is the project's one
  test suite — the Python side still has none).

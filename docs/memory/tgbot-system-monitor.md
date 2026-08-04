# tgbot system monitor — design & invariants

The standalone system monitor (`utils/system_monitor.py`) is a /proc-only
health reporter for the VPS. It runs as **its own process** — either the
`tgbot-monitor.service` systemd unit (installed by `install.sh`, survives
reboots) or a detached fork spawned by `main.py` on bot startup
(`spawn_detached_monitor`, guarded so it never stacks a duplicate).

## Why it exists / what it replaces

The old approach was an in-process uvicorn log line (`[System] Disk usage ...`)
— only visible when the bot was up. If the bot hung or crashed, nobody learned
the box was hot. The monitor keeps reporting even when the bot is dead, which
is exactly the failure mode it observes.

## Non-obvious invariants (do not break these)

1. **Zero heavy dependencies.** It reads Linux counters straight from `/proc`
   (`/proc/stat`, `/proc/meminfo`, `/proc/loadavg`, `/proc/uptime`,
   `/proc/<pid>/stat` + `/proc/<pid>/status`) and `/proc/<pid>/cmdline` for
   liveness scans. No psutil, no netdata/glances/beszel, no DB, no dashboard.
   Measured on a 1-core/1 GB VPS: a fraction of a percent of a core and
   ~15–30 MB RSS — orders of magnitude under netdata (340 MB) / glances (50 MB).
   Do **not** add psutil just because it's convenient; the /proc readers are
   already written and tested.

2. **Independent of the bot.** It talks to Telegram via plain `requests.post`
   to the Bot HTTP API (like `utils.logger`), NOT pyrogram, NOT the bot's event
   loop. A bot crash, hung loop, or pyrogram disconnect never stops reports.
   This is why it must stay a separate process (`python -m utils.system_monitor`
   or the unit), not a task inside `main.py`.

3. **Reports every `SYSMON_REPORT_INTERVAL` samples** (default 60 × 15 s =
   15 min) and **warns at `SYSMON_WARN_PCT`** (default 80) on CPU/RAM/disk,
   repeating every `SYSMON_WARN_SECONDS` (default 60) **until every metric drops
   back below** — nag-until-fixed, never floods, stops on recovery. Both the
   report and the warning carry the **VPS local date-time** (`VPS time:`) so you
   can correlate a spike in the channel with VPS wall-clock — keep that line.

4. **Config is self-contained via dotenv.** `load_dotenv()` is called at module
   top so the standalone process reads `.env` even when nothing else imported
   it first (`40529a9`). If `BOT_TOKEN` or `LOG_CHANNEL_ID` are missing, `run()`
   exits 2 with a stderr message rather than silently sampling forever.

5. **Per-process CPU% needs deltas.** `/proc/<pid>/stat` utime+stime are
   cumulative since process start, so the scanner keeps a pid→(ticks, wall)
   snapshot from the previous poll and diffs (top's method). First sample of a
   new pid is 0%. `_process_prev` is a module-global — reset it if you ever add
   a way to restart the scan.

6. **Never blocks the sampler.** Each Telegram send is fire-and-forget in its
   own daemon thread with a short timeout (`_send_telegram`), and a bad sample
   is caught and skipped (`run()`'s try/except around `_collect()`) — the loop
   sleeps `POLL_SECONDS` and continues.

7. **`is_running()` = pidfile AND /proc scan.** The pidfile alone is
   unreliable: the systemd unit writes no pidfile, and a stale pidfile can
   point at a recycled pid. The scan looks for any *python* process whose
   cmdline names `utils.system_monitor`, requiring argv[0] to start with
   `python` so shells/debuggers whose command string merely mentions the module
   don't false-positive. This is what prevents a duplicate monitor when the bot
   restarts under systemd (`8a2d001`).

8. **The systemd unit is a template** (`deploy/tgbot-monitor.service`) with the
   same `__USER__` / `__GROUP__` / `__PROJECT_DIR__` placeholders as
   `tgbot.service`, rendered by `install.sh`. It has **no** `__MEMORY_MAX__`
   placeholder — the monitor is tiny (MemoryMax=256M is hardcoded headroom, NOT
   a tight cap; see invariant #1 in AGENTS.md re `ulimit -v`). It is installed
   but **not auto-enabled**; enable with `systemctl enable --now tgbot-monitor`.

## Operational notes

- Live log: `journalctl -u tgbot-monitor -f`; the detach path (non-systemd)
  writes `logs/system_monitor.log`.
- If the bot and the systemd unit both run, only the systemd instance samples —
  the bot's `spawn_detached_monitor` sees `is_running() == True` and returns.
- Env knobs: `SYSMON_POLL_SECONDS`, `SYSMON_REPORT_INTERVAL`,
  `SYSMON_WARN_PCT`, `SYSMON_WARN_SECONDS`, `SYSMON_TOP_N`,
  `SYSMON_HISTORY_SAMPLES`, `SYSMON_DISK_PATHS` (all in `config.py` +
  `.env.example`).

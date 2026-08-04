# utils/system_monitor.py
"""
Lightweight system monitor for tgbot.

Design goals (see docs/memory/tgbot-system-monitor.md for the full rationale):

* ZERO heavy dependencies. Reads Linux counters straight from /proc (the same
  technique htop/top/ps use) and reports via the plain Telegram Bot HTTP API —
  no psutil, no netdata/glances/beszel agents, no dashboard, no DB. Measured
  cost on a 1-core/1 GB VPS: a fraction of a percent of a core and a few MB of
  RSS, orders of magnitude under netdata (340 MB) / glances (50 MB).

* INDEPENDENT of the bot. It is meant to run as its OWN process
  (``python3 -m utils.system_monitor``, or the ``tgbot-monitor.service`` unit
  installed by install.sh). It talks to Telegram with a plain
  ``requests.post`` to the Bot API — exactly like utils.logger does — so a
  bot crash, a hung event loop, or a full pyrogram disconnect never stops
  system reports. If the bot dies, the monitor keeps watching.

* Periodic ``#system`` report to the log channel: current CPU/RAM/swap/disk,
  time-frame averages (the last N samples), load averages, and the top-N
  processes by CPU and by RAM over the window.

* Threshold warnings: when CPU or RAM or disk crosses
  ``SYSMON_WARN_PCT`` (default 80), a warning with the live percentages is
  sent immediately, then repeated every ``SYSMON_WARN_SECONDS`` (default 60)
  until every metric drops back below the threshold. This is the "keep nagging
  until it's fixed" behaviour — it never floods (once per interval while hot),
  and it stops the moment the box recovers.

Everything is bounded: the sample ring is capped, the process scan is limited
to top-N per sort key, and each Telegram send is fire-and-forget with a short
timeout in its own daemon thread so the sampler never blocks.
"""

from __future__ import annotations

import os
import sys
import time
import threading
import html
import shutil
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()  # self-contained: reads .env even when run standalone
except Exception:
    pass

# ---------------------------------------------------------------------------
# Configuration (all overridable via .env)
# ---------------------------------------------------------------------------

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, "").strip())
    except ValueError:
        return default


POLL_SECONDS = _env_int("SYSMON_POLL_SECONDS", 15)          # sample cadence
REPORT_EVERY_SAMPLES = _env_int("SYSMON_REPORT_INTERVAL", 60)  # report each N samples (15s*60=15min)
WARN_PCT = _env_int("SYSMON_WARN_PCT", 80)                   # threshold for warnings
WARN_SECONDS = _env_int("SYSMON_WARN_SECONDS", 60)           # warning repeat interval while hot
TOP_N = _env_int("SYSMON_TOP_N", 20)                         # top processes per sort key
HISTORY_SAMPLES = _env_int("SYSMON_HISTORY_SAMPLES", 240)    # ring buffer for averages (15s*240=1h)
DISK_PATHS = [p.strip() for p in os.getenv("SYSMON_DISK_PATHS", ".").split(",") if p.strip()] or ["."]

LOG_CHANNEL_ID = _env_int("LOG_CHANNEL_ID", 0)
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

REQUESTS_PROXY = os.getenv("REQUESTS_PROXY", "").strip() or None

# /proc uptime/stat helpers
_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100

_MEMINFO_KEYS = {
    "MemTotal": 0,
    "MemAvailable": 0,
    "SwapTotal": 0,
    "SwapFree": 0,
}


def _read_meminfo() -> dict:
    out = dict(_MEMINFO_KEYS)
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                key, _, rest = line.partition(":")
                if key in out:
                    out[key] = int(rest.split()[0]) * 1024  # kB -> bytes
    except OSError:
        pass
    return out


def _read_cpu_total() -> tuple[int, int]:
    """Return (idle_ticks, total_ticks) since boot from /proc/stat."""
    try:
        with open("/proc/stat", "r", encoding="utf-8") as f:
            line = f.readline()
        parts = line.split()
        # cpu  user nice system idle iowait irq softirq steal guest guest_nice
        nums = [int(x) for x in parts[1:8]]  # user,nice,system,idle,iowait,irq,softirq
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)  # idle + iowait
        total = sum(nums)
        return idle, total
    except (OSError, IndexError, ValueError):
        return 0, 0


def _read_loadavg() -> tuple[float, float, float]:
    try:
        with open("/proc/loadavg", "r", encoding="utf-8") as f:
            parts = f.read().split()
        return float(parts[0]), float(parts[1]), float(parts[2])
    except (OSError, IndexError, ValueError):
        return 0.0, 0.0, 0.0


def _read_uptime() -> float:
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as f:
            return float(f.read().split()[0])
    except (OSError, IndexError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Process scanning (top-N by CPU and RAM)
# ---------------------------------------------------------------------------
# Per-process CPU% needs two /proc/stat-style deltas, so we keep a snapshot of
# each pid's utime+stime from the PREVIOUS poll and diff against the current
# one, scaled by wall-clock time between polls (the same method top uses).

_process_prev: dict[int, tuple[int, int]] = {}  # pid -> (prev_ticks, prev_wall)
_last_poll_wall = time.monotonic()


def _scan_processes(n_cpu: int = TOP_N, n_ram: int = TOP_N) -> tuple[list[tuple], list[tuple]]:
    """Return (top_by_cpu, top_by_ram) as (pid, name, cpu_pct, rss_bytes)."""
    global _last_poll_wall
    now = time.monotonic()
    delta_wall = max(now - _last_poll_wall, 0.1)
    _last_poll_wall = now

    cur: dict[int, tuple[str, int, int]] = {}  # pid -> (name, ticks, rss)
    try:
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                stat_path = os.path.join("/proc", entry.name, "stat")
                with open(stat_path, "r", encoding="utf-8") as f:
                    stat = f.read()
                # comm can contain spaces/parens; split on last ')'
                rparen = stat.rfind(")")
                if rparen < 0:
                    continue
                comm_raw = stat[stat.find("(") + 1:rparen] or entry.name
                name = comm_raw[:40]
                fields = stat[rparen + 2:].split()
                # fields: state(0) ppid(1) ... utime(11) stime(12) ...
                if len(fields) < 14:
                    continue
                utime = int(fields[11])
                stime = int(fields[12])
                ticks = utime + stime

                rss = 0
                try:
                    status = os.path.join("/proc", entry.name, "status")
                    with open(status, "r", encoding="utf-8") as sf:
                        for line in sf:
                            if line.startswith("VmRSS:"):
                                rss = int(line.split()[1]) * 1024  # kB -> bytes
                                break
                except OSError:
                    pass

                cur[pid] = (name, ticks, rss)
            except (OSError, ValueError, IndexError):
                continue
    except OSError:
        pass

    # Compute per-process CPU% from the delta since the previous poll.
    cpu_rows: list[tuple[int, str, float, int]] = []
    for pid, (name, ticks, rss) in cur.items():
        prev = _process_prev.get(pid)
        if prev is not None:
            prev_ticks, prev_wall = prev
            dticks = max(ticks - prev_ticks, 0)
            dtime = delta_wall
            cpu_pct = (dticks / _CLK_TCK) / dtime * 100.0 if dtime > 0 else 0.0
        else:
            cpu_pct = 0.0
        cpu_rows.append((pid, name, cpu_pct, rss))

    _process_prev.update({pid: (cur[pid][1], now) for pid in cur})

    cpu_top = sorted(cpu_rows, key=lambda r: -r[2])[:n_cpu]
    ram_top = sorted(cpu_rows, key=lambda r: -r[3])[:n_ram]
    return cpu_top, ram_top


def _fmt_rss(bytes_: int) -> str:
    if bytes_ >= 1024 ** 3:
        return f"{bytes_ / 1024 ** 3:.1f}G"
    if bytes_ >= 1024 ** 2:
        return f"{bytes_ / 1024 ** 2:.0f}M"
    return f"{bytes_ / 1024:.0f}K"


# ---------------------------------------------------------------------------
# Sample model
# ---------------------------------------------------------------------------

class Sample:
    __slots__ = ("ts", "cpu_pct", "load1", "load5", "load15", "mem_total",
                 "mem_avail", "swap_total", "swap_used", "disks", "uptime",
                 "cpu_top", "ram_top")

    def __init__(self, cpu_pct: float, load: tuple, mem: dict, disks: dict,
                 uptime: float, cpu_top: list, ram_top: list):
        self.ts = time.time()
        self.cpu_pct = cpu_pct
        self.load1, self.load5, self.load15 = load
        self.mem_total = mem.get("MemTotal", 0)
        self.mem_avail = mem.get("MemAvailable", 0)
        self.swap_total = mem.get("SwapTotal", 0)
        self.swap_used = max(mem.get("SwapTotal", 0) - mem.get("SwapFree", 0), 0)
        self.disks = disks  # {path: (used_pct, total_bytes, free_bytes)}
        self.uptime = uptime
        self.cpu_top = cpu_top
        self.ram_top = ram_top

    @property
    def mem_pct(self) -> float:
        if self.mem_total <= 0:
            return 0.0
        return max(100.0 * (self.mem_total - self.mem_avail) / self.mem_total, 0.0)

    @property
    def swap_pct(self) -> float:
        if self.swap_total <= 0:
            return 0.0
        return max(100.0 * self.swap_used / self.swap_total, 0.0)


def _collect() -> Sample:
    idle1, total1 = _read_cpu_total()
    time.sleep(1.0)
    idle2, total2 = _read_cpu_total()
    d_total = total2 - total1
    cpu_pct = 100.0 * (d_total - (idle2 - idle1)) / d_total if d_total > 0 else 0.0

    mem = _read_meminfo()
    load = _read_loadavg()
    uptime = _read_uptime()

    disks = {}
    for path in DISK_PATHS:
        try:
            usage = shutil.disk_usage(path)
            disks[path] = (
                100.0 * usage.used / usage.total if usage.total else 0.0,
                usage.total,
                usage.free,
            )
        except OSError:
            pass

    cpu_top, ram_top = _scan_processes()
    return Sample(cpu_pct, load, mem, disks, uptime, cpu_top, ram_top)


# ---------------------------------------------------------------------------
# Telegram sending (independent of pyrogram — raw Bot API, like utils.logger)
# ---------------------------------------------------------------------------

_send_lock = threading.Lock()


def _send_telegram(text: str) -> None:
    """Fire-and-forget a message to the log channel via the Bot HTTP API."""
    if not BOT_TOKEN or not LOG_CHANNEL_ID:
        return
    payload = {
        "chat_id": LOG_CHANNEL_ID,
        "text": text[:4000],
        "parse_mode": "HTML",
    }
    proxies = {"http": REQUESTS_PROXY, "https": REQUESTS_PROXY} if REQUESTS_PROXY else None
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    def _do():
        try:
            with _send_lock:
                requests.post(url, json=payload, timeout=8, proxies=proxies)
        except Exception:
            pass

    threading.Thread(target=_do, daemon=True).start()


def _esc(s) -> str:
    return html.escape(str(s))


# ---------------------------------------------------------------------------
# Report + warning formatting
# ---------------------------------------------------------------------------

def _avg(samples: list[Sample], getter) -> float:
    if not samples:
        return 0.0
    return sum(getter(s) for s in samples) / len(samples)


def _uptime_str(seconds: float) -> str:
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days}d {hours}h {mins}m"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def _top_rows(rows: list, kind: str) -> list[str]:
    lines = []
    for i, (pid, name, cpu, rss) in enumerate(rows[:TOP_N], 1):
        if kind == "cpu":
            lines.append(f"{i}. <code>{_esc(name)}</code> (pid {pid}) — CPU <b>{cpu:.1f}%</b> · RSS {_fmt_rss(rss)}")
        else:
            lines.append(f"{i}. <code>{_esc(name)}</code> (pid {pid}) — RSS <b>{_fmt_rss(rss)}</b> · CPU {cpu:.1f}%")
    return lines


def format_report(samples: list[Sample], current: Sample) -> str:
    window_min = len(samples) * POLL_SECONDS / 60.0
    avg_cpu = _avg(samples, lambda s: s.cpu_pct)
    avg_mem = _avg(samples, lambda s: s.mem_pct)
    avg_swap = _avg(samples, lambda s: s.swap_pct)

    lines = [
        "📊 <b>SYSTEM REPORT</b>",
        f"Window: last <b>{window_min:.0f} min</b> ({len(samples)} samples @ {POLL_SECONDS}s) · Uptime {_uptime_str(current.uptime)}",
        "",
        f"<b>CPU</b> now {current.cpu_pct:.1f}% · avg {avg_cpu:.1f}%",
        f"<b>Load</b> 1m {current.load1:.2f} / 5m {current.load5:.2f} / 15m {current.load15:.2f}",
        f"<b>RAM</b> now {current.mem_pct:.1f}% · avg {avg_mem:.1f}%  ({_fmt_rss(current.mem_total - current.mem_avail)} / {_fmt_rss(current.mem_total)})",
        f"<b>Swap</b> now {current.swap_pct:.1f}% · avg {avg_swap:.1f}%  ({_fmt_rss(current.swap_used)} / {_fmt_rss(current.swap_total)})",
    ]
    for path, (pct, total, free) in current.disks.items():
        label = path if path != "." else "disk"
        lines.append(f"<b>{_esc(label)}</b> {pct:.1f}% used ({_fmt_rss(free)} free)")

    if current.cpu_top:
        lines += ["", f"🏆 <b>Top {min(TOP_N, len(current.cpu_top))} by CPU</b> (this window)"]
        lines += _top_rows(current.cpu_top, "cpu")
    if current.ram_top:
        lines += ["", f"🧠 <b>Top {min(TOP_N, len(current.ram_top))} by RAM</b> (now)"]
        lines += _top_rows(current.ram_top, "ram")

    lines += ["", "#system"]
    return "\n".join(lines)


def format_warning(samples: list[Sample], current: Sample) -> str:
    hot = []
    if current.cpu_pct >= WARN_PCT:
        hot.append(f"CPU {current.cpu_pct:.1f}%")
    if current.mem_pct >= WARN_PCT:
        hot.append(f"RAM {current.mem_pct:.1f}%")
    for path, (pct, *_rest) in current.disks.items():
        if pct >= WARN_PCT:
            hot.append(f"disk {pct:.1f}% ({path})")
    if not hot:
        return ""

    lines = [
        "🚨 <b>HIGH SYSTEM USAGE</b>",
        f"Threshold: {WARN_PCT}%. Currently: <b>{', '.join(hot)}</b>",
        f"Load: {current.load1:.2f} / {current.load5:.2f} / {current.load15:.2f}",
        "",
        "⚠️ I will keep reporting every minute until everything drops below "
        f"{WARN_PCT}%. Check for runaway downloads, a full disk, or a memory leak.",
        "#system",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _pidfile() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "system_monitor.pid")


def is_running() -> bool:
    """True if a system monitor instance is alive (checked via its pidfile)."""
    try:
        with open(_pidfile(), "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)  # signal 0 = existence probe, no signal sent
        return True
    except (OSError, ValueError, IOError):
        return False


def spawn_detached_monitor() -> bool:
    """Fork a fully-detached system-monitor process (daemonises via double-fork).

    Returns True when this call launched the monitor, False when one is already
    running (so callers never stack duplicate watchers). Used by main.py on bot
    startup; harmless to call repeatedly.
    """
    if is_running():
        return False

    import subprocess
    log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    os.makedirs(log_dir, exist_ok=True)
    out_path = os.path.join(log_dir, "system_monitor.log")
    with open(out_path, "a", encoding="utf-8") as logf:
        # start_new_session detaches from the bot's process group/session so
        # killing or restarting the bot never takes the monitor down with it.
        proc = subprocess.Popen(
            [sys.executable, "-m", "utils.system_monitor"],
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=logf,
            start_new_session=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
    try:
        with open(_pidfile(), "w", encoding="utf-8") as f:
            f.write(str(proc.pid))
    except OSError:
        pass
    return True


def run() -> int:
    if not BOT_TOKEN or not LOG_CHANNEL_ID:
        print("[system_monitor] ERROR: BOT_TOKEN and LOG_CHANNEL_ID must be set in .env.", file=sys.stderr)
        return 2

    print(f"[system_monitor] started. poll={POLL_SECONDS}s, report every "
          f"{REPORT_EVERY_SAMPLES * POLL_SECONDS}s, warn >={WARN_PCT}% every {WARN_SECONDS}s.")
    print(f"[system_monitor] channel={LOG_CHANNEL_ID} paths={DISK_PATHS}")

    samples: list[Sample] = []
    last_report_idx = -1
    last_warn_ts = 0.0
    warn_active = False

    while True:
        try:
            s = _collect()
        except Exception as exc:  # never die on a bad sample
            print(f"[system_monitor] sample error: {exc!r}", file=sys.stderr)
            time.sleep(POLL_SECONDS)
            continue

        samples.append(s)
        if len(samples) > HISTORY_SAMPLES:
            samples = samples[-HISTORY_SAMPLES:]

        # --- Threshold warning: fire when hot, repeat every WARN_SECONDS. ---
        is_hot = (s.cpu_pct >= WARN_PCT or s.mem_pct >= WARN_PCT or
                  any(pct >= WARN_PCT for pct, *_r in s.disks.values()))
        if is_hot:
            if not warn_active or (time.time() - last_warn_ts) >= WARN_SECONDS:
                text = format_warning(samples, s)
                if text:
                    _send_telegram(text)
                    print(f"[system_monitor] warning sent ({text.splitlines()[1]})")
                last_warn_ts = time.time()
            warn_active = True
        else:
            warn_active = False

        # --- Periodic report every REPORT_EVERY_SAMPLES samples. ---
        if len(samples) % REPORT_EVERY_SAMPLES == 0 and (len(samples) - 1) != last_report_idx:
            _send_telegram(format_report(samples, s))
            last_report_idx = len(samples) - 1
            print(f"[system_monitor] report sent ({len(samples)} samples)")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        sys.exit(run())
    except KeyboardInterrupt:
        print("[system_monitor] stopped.")
        sys.exit(0)

# utils/system_monitor.py
"""
Spawner for the standalone Go system monitor (cmd/tgbot-monitor).

The actual monitor is a static Go binary (built by install.sh into
``build/tgbot-monitor``) — see docs/INFRA.md and
docs/INFRA.md for why. This module exists ONLY to give
main.py a hook to launch it: it finds the binary, checks whether an instance is
already alive (pidfile written by the Go process + a /proc cmdline scan), and
forks it detached so it survives the bot's crash/restart.

Kept deliberately thin: the report/warning/sampling logic lives in Go. Do not
re-add the Python monitoring engine here.
"""

from __future__ import annotations

import os
import subprocess

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PIDFILE = os.path.join(_PROJECT_ROOT, "system_monitor.pid")
_GO_BIN = os.path.join(_PROJECT_ROOT, "build", "tgbot-monitor")


def _pidfile() -> str:
    return _PIDFILE


def _go_binary() -> str:
    return _GO_BIN


def is_running() -> bool:
    """True if a system monitor instance is alive.

    Checks the pidfile written by the Go binary, then a /proc scan for a live
    ``tgbot-monitor`` process (argv0 basename must contain ``tgbot-monitor`` so
    shells whose command strings merely mention it don't false-positive). A
    stale pidfile pointing at a recycled pid is detected and ignored.
    """
    pidfile = _pidfile()
    if os.path.exists(pidfile):
        try:
            with open(pidfile, "r", encoding="utf-8") as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)  # signal 0 = existence probe, no signal sent
            return True
        except (OSError, ValueError, IOError):
            pass

    try:
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            try:
                with open(os.path.join(entry.path, "cmdline"), "rb") as f:
                    raw = f.read()
                cmd = raw.replace(b"\x00", b" ")
                if b"tgbot-monitor" not in cmd:
                    continue
                argv0 = raw.split(b"\x00")[0]
                if b"tgbot-monitor" in argv0:
                    return True
            except OSError:
                continue
    except OSError:
        pass
    return False


def spawn_detached_monitor() -> bool:
    """Fork the Go monitor fully detached (own session, survives the bot).

    Returns True when this call launched the monitor, False when one is already
    running (so callers never stack duplicate watchers). The Go binary writes
    system_monitor.pid itself on startup and removes it on exit.
    """
    if is_running():
        return False
    if not os.path.exists(_go_binary()):
        return False

    log_dir = os.path.join(_PROJECT_ROOT, "logs")
    os.makedirs(log_dir, exist_ok=True)
    out_path = os.path.join(log_dir, "system_monitor.log")
    with open(out_path, "a", encoding="utf-8") as logf:
        # start_new_session detaches from the bot's process group/session so
        # killing or restarting the bot never takes the monitor down with it.
        subprocess.Popen(
            [_go_binary()],
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=logf,
            start_new_session=True,
            cwd=_PROJECT_ROOT,
        )
    return True

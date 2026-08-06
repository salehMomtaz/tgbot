# VPS two-bots runtime state

The test VPS (SSH to `<vps-ip>` on port `<ssh-port>` as `<vps-user>`) hosts BOTH
bots side by side: **balebot** in `/home/<vps-user>/balebot` and **tgbot** in
`/home/<vps-user>/tgbot`. This is the VPS used for live testing.

Systemd state (as of 2026-07-18):

- `balebot.service` — **enabled**, auto-starts on boot. Its PO-token provider
  (Deno) listens on **127.0.0.1:4416**.
- `tgbot.service` — **enabled** (as of 2026-07-18), auto-starts on boot and
  auto-restarts on crash (`Restart=always`). Its PO provider listens on
  **127.0.0.1:4417**. `install.sh` installs the unit but does **not** enable it —
  enabling is a deliberate manual step (`sudo systemctl enable --now tgbot`) so a
  first-run can be watched interactively first.
- `cookie-watch.service` — **enabled**, auto-starts on boot. Runs
  `/home/<vps-user>/tgbot/tools/cookie_watch.sh` (inotifywait; `auditd` is inert
  on this host). Harmless monitor.

**Diagnostic gotcha:** `ps` shows a bare `python main.py` — on this host that is
**balebot**, not tgbot (both projects have a `main.py`). Distinguish them via
cgroup (`/proc/<pid>/cgroup` → `balebot.service` vs `tgbot.service`) or cwd
(`readlink /proc/<pid>/cwd` → `/home/<vps-user>/balebot` vs
`/home/<vps-user>/tgbot`). A healthy tgbot run shows: recent `bot.log` writes,
port 4417 listening, and "Started 5 HandlerTasks" + "Provider is healthy on
127.0.0.1:4417" in `/home/<vps-user>/tgbot/logs/bot.log`.

**"No response from tgbot" after a reboot** used to mean "tgbot wasn't running"
(the unit was disabled, so the bot was down — not crashed). Now that the unit is
enabled, check the service first:
`sudo systemctl status tgbot` / `sudo journalctl -u tgbot -f`. If for any reason
it must be started ad-hoc without systemd, stop the service first and run it
detached from the repo dir with `./run.sh`. See
[Cookie protection & monitor](tgbot-cookie-protection-and-monitor.md) and
[tgbot ↔ balebot integration](tgbot-balebot-integration.md).

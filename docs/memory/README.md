# tgbot — agent working notes (memory)

These are stripped, repo-local copies of the project's long-term memory
(originally kept in the AI coding agent's private memory store across sessions).
They capture non-obvious invariants and history that are not obvious from the
code or the git log alone. Last updated: 2026-07-18.

> **Sensitive values have been redacted.** Where you see `<vps-ip>`,
> `<ssh-port>`, `<vps-user>`, or `<redacted>`, substitute your own. No real
> credentials, IP addresses, or usernames are stored here.

- [tgbot ↔ balebot port relationship](tgbot-balebot-port-relationship.md) — tgbot
  ports balebot's PO-token provider, cookie protection, and `install.sh`
  deployment; GitHub explorer and translate are intentionally NOT ported.
- [Cookie protection & monitor](tgbot-cookie-protection-and-monitor.md) — cookie
  corruption is fixed (`b44db54`); yt-dlp only ever touches disposable snapshots;
  `cookie-watch.service` (inotifywait) is the tamper monitor; `auditd` is inert on
  the test host.
- [VPS two-bots runtime state](vps-two-bots-runtime-state.md) — on the test VPS
  `balebot.service` (PO port 4416) and `tgbot.service` (PO port 4417) both run;
  both are now enabled and survive reboot; a bare `python main.py` is balebot.

# 2026-08-25 — Routine health pass (no code changes)

## Scope

No task was assigned this session; this was a scheduled health/maintenance pass.
The working tree was clean and in sync with `origin/main` at `4851074`, so the
pass was verification-only. **No source, config, or deployment changes were made
on purpose** — nothing needed fixing.

## Verification results

- `systemctl`: `tgbot` active (0 restarts), `tgbot-xchat-bridge` active,
  `cookie-watch` active, `tgbot-monitor` inactive **by design** (the bot spawns
  a detached monitor at startup; the systemd unit is opt-in).
- `python3 -m py_compile $(git ls-files '*.py')` — clean.
- `bash -n install.sh run.sh uninstall.sh tools/start_xchat_bridge.sh` — clean.
- `cd cmd/tgbot-monitor && go test ./...` — ok.
- `logs/bot.log`: X self-DM polls returning 200 on cadence, cookie refresher
  skipping all four jars (<20h freshness), yt-dlp updater up to date
  (2026.08.20.234504). No tracebacks.
- `cache/xchat_inbox.jsonl`: 451 lines, zero duplicate lines.

## Operational finding (needs a HUMAN, not code)

The Instagram direct-forward worker has been cycling through checkpoint
challenges since 2026-08-24 ~19:10 (`Manual verification required via Instagram
native challenge flow`, then once `Please wait a few minutes before you try
again`). Each wake-up retries login, hits the wall again, and freezes another
~4 h. This matches the documented design (freeze 3–5 h, no retry storms); the
only fix is a human passing the checkpoint in the official Instagram app on the
bot account, then restarting the bot for a clean resume. The agent deliberately
did NOT touch the IG session/jar/proxy — retry gymnastics against an active
checkpoint make the account's standing worse.

## Gotcha learned: never execute `tools/start_xchat_bridge.sh` by hand

While syntax-checking scripts, the supervisor wrapper was accidentally
*executed* instead of only `bash -n`-checked. Because it is a resident loop that
(re)spawns the Deno sidecar whenever its gates hold, this briefly created a
SECOND `deno run -A xchat_bridge.mjs` alongside the one owned by
`tgbot-xchat-bridge.service`. Cleanup: killed the orphaned PID; verified the
legit bridge (child of the systemd wrapper) stayed up and the inbox file gained
no duplicates.

Rule for future agents: `tools/start_xchat_bridge.sh` is owned by systemd.
Interact with the bridge only via `systemctl status/restart tgbot-xchat-bridge`;
at most use `bash -n` for syntax checks — never run the wrapper directly while
the unit exists, or you fork a duplicate sidecar.

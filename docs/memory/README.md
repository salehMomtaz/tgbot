# tgbot — agent working notes (memory)

These are consolidated, repo-local copies of the project's long-term memory (originally scattered across `docs/` and `docs/memory/`). Contents are verbatim merges of the originals — no information was dropped. Last consolidated: **2026-08-30** (39 → 8 files).

> **Sensitive values have been redacted.** Where you see `<vps-ip>`, `<ssh-port>`, `<vps-user>`, or `<redacted>`, substitute your own.

## Consolidated docs (start here)

| Doc | What it covers | Sources merged |
|---|---|---|
| [Health passes — timeline](HEALTH_PASSES.md) | 5 point-in-time Telethon-driven health passes + 2026-09-01 log audit on the prod box | `tgbot-2026-08-07-health-pass`, `2026-08-08-x-selfdm-health-pass`, `2026-08-11-health-pass`, `2026-08-12-health-pass`, `2026-08-25-health-pass`, `2026-09-01-log-audit` |
| [Direct-forward history](DIRECT_FORWARD_HISTORY.md) | IG / X self-DM / TikTok IM relay incidents, XChat bridge, state-race, photo fallback | `2026-08-08-x-pairing`, `2026-08-09-xchat-bridge`, `2026-08-11-selfdm-audit`, `2026-08-11-x-duplicate…`, `2026-08-11-x-photo-paste…`, `instagram-risky-and-push`, `vps-two-bots…` — user-facing setup stays in `../DIRECT_FORWARD_SETUP.md` |
| [Balebot integration](BALEBOT.md) | Bale.ai experiment → optional frontend inside tgbot, ported extras, hardening | `tgbot-balebot-integration`, `tgbot-balebot-merge-2026-08-13`, `tgbot-balebot-hardening-2026-08-13` |

Other consolidated docs live one level up:

- [`../TIKTOK.md`](../TIKTOK.md) — TikTok DM research + shortlink + download + embed + `curl_cffi` pin (6 sources).
- [`../COOKIES.md`](../COOKIES.md) — cookie jars, protection/write-back, IG cookie confusion & anti-detection (5 sources).
- [`../DOWNLOADER.md`](../DOWNLOADER.md) — yt-dlp site support, size estimation, CDN probes, thumbnails (8 sources).
- [`../INFRA.md`](../INFRA.md) — architecture decisions, Go monitor, propagation bugs, subscription/premium/friend-media/copy-message, 203/EXEC, kurigram (15 sources).
- [`../REFERENCE.md`](../REFERENCE.md) — the 4 `reference/` clones (gitignored, 36 MB) summarized as a manifest; clones remain deletable via `rm -rf reference/`.

## Kept separate (not consolidated)

- `../DIRECT_FORWARD_SETUP.md` — user-facing DM relay setup (IG + X + TikTok).
- `../UBUNTU_VPS_SETUP.md` — bare-metal provisioning from fresh Ubuntu 24.04.
- `../USER_GUIDE.md` — end-user bot usage.
- `../apiDocuments/` — raw Telegram/Bale API dumps (680 K, reference).
- `../../learn/` — 20-lesson Python course (intentionally 20 small files).
- `../../AGENTS.md` / `../../blueprint.md` — agent invariants / architecture (root).

## Migration note (for old links)

Old per-topic files (e.g. `tgbot-cookie-protection-and-monitor.md`, `tgbot-ig-anti-detection.md`, `tgbot-2026-08-07-health-pass.md`, …) are **removed** — their content now lives verbatim inside the 8 consolidated files above under `## N. Source: \`path\`` headers. `git log --follow` retains history. Update any `docs/memory/<old-name>.md` links to the new consolidated files (search the new file for `Source: \`<old-name>\``).

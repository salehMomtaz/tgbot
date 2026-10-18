# tgbot — agent working notes (memory)

These are consolidated, repo-local copies of the project's long-term memory (originally scattered across `docs/` and `docs/memory/`). Contents are verbatim merges of the originals — no information was dropped. Last consolidated: **2026-08-30** (39 → 8 files).

> **Sensitive values have been redacted.** Where you see `<vps-ip>`, `<ssh-port>`, `<vps-user>`, or `<redacted>`, substitute your own.

## Consolidated docs (start here)

| Doc | What it covers | Sources merged |
|---|---|---|
| [Health passes — timeline](HEALTH_PASSES.md) | 5 point-in-time Telethon-driven health passes + 2026-09-01 log audit on the prod box | `tgbot-2026-08-07-health-pass`, `2026-08-08-x-selfdm-health-pass`, `2026-08-11-health-pass`, `2026-08-12-health-pass`, `2026-08-25-health-pass`, `2026-09-01-log-audit`, `2026-09-01-bale-github-evaluation` |
| [Direct-forward history](DIRECT_FORWARD_HISTORY.md) | IG / X self-DM / TikTok IM relay incidents, XChat bridge, state-race, photo fallback | `2026-08-08-x-pairing`, `2026-08-09-xchat-bridge`, `2026-08-11-selfdm-audit`, `2026-08-11-x-duplicate…`, `2026-08-11-x-photo-paste…`, `instagram-risky-and-push`, `vps-two-bots…` — user-facing setup stays in `../DIRECT_FORWARD_SETUP.md` |
| [Balebot integration — **REMOVED 2026-10-18**](BALEBOT.md) | Bale.ai experiment → optional frontend inside tgbot, ported extras, hardening. **Historical record only**: the whole frontend (`modules/bale/`, `BaleChannelHandler`/`bale_log`, `aiogram`, all `BALE_*`) was deleted 2026-10-18 — see [the removal report](tgbot-2026-10-18-bale-frontend-removal.md) | `tgbot-balebot-integration`, `tgbot-balebot-merge-2026-08-13`, `tgbot-balebot-hardening-2026-08-13` |
| [IG session death 2026-09-05](tgbot-2026-09-05-ig-session-death-incident.md) | private-API session killed mid-archive (jar NOT corrupted — verdict via history fingerprints), friend-media IG circuit breaker + probe-noise log filter + PhotoExtInvalid magic-bytes fix | session analysis of `cookies/history.jsonl` + 12h log audit |
| [Log + cookie audit 2026-09-15](tgbot-2026-09-15-log-and-cookie-audit.md) | 10-day log scan; thread-exhaustion cascade (`can't start new thread`) from per-record logger threads → bounded log queue; IG jar corruption (overlay updated only the last duplicate + refresher injected foreign-domain cookies + broken logged-in gate) → dedupe-by-triple, allowlists, repaired jar | log channel + `cookies/history.jsonl` |
| [DM linking + downloader fixes 2026-09-15](tgbot-2026-09-15-dm-forwarder-linking-and-downloader.md) | multi-video-tweet playlist misrouted to the YouTube storyboard error (fixed); IG DM emoji reactions via instagrapi; X linking handshake (IG pairing verified working); TikTok history verdict | operator report |
| [Friend Media archive highlights 2026-09-15](tgbot-2026-09-15-friend-media-archive-highlights.md) | highlight `feed/reels_media` 403 aborted the whole archive + tripped the breaker (fixed: skip highlights, deliver zip); ad-hoc archive-by-username; session-death note | operator report + `cookies/history.jsonl` |
| [IG cookie corruption + X bridge 2026-10-08](tgbot-2026-10-08-ig-cookie-corruption-and-x-bridge.md) | 15-day log audit; IG session deaths proven to be OUR headless refresher rotating `mid`/`ig_did` while keeping the sessionid (fixed: `_DEVICE_IDENTITY` denylist); IG worker no longer auto-relogins (waits for fresh jar); XChat bridge crash (`x-client-transaction-id` 0.3.1→0.3.2 + `emusks` 2.3.6→2.3.15); X relay crash on Persian `.NA` filenames (fixed); **X backlog gap — cursor advanced on enqueue, not on relay → in-memory queue lost jobs on restart (fixed: `_enqueue_relay` returns a Future, workers await it; `tools/recover_x_gap.py` replayed 79 dropped messages)** | operator report + `cookies/history_snapshots/` + 15-day logs |
| [Filename length overflow 2026-10-10](tgbot-2026-10-10-filename-length-audit.md) | 15-day log audit; **X relay `OSError [Errno 36] File name too long` on emoji-bloated tweet titles** — yt-dlp sanitizes chars but not length, so a title overflowing Linux's 255-byte `NAME_MAX` killed the thumbnail write (fixed: byte-bounded outtmpl `%(title).150B`); same latent class in the direct-file path (fixed: `safe_task_filename` length cap); monitoring/subsystem status confirmed healthy | operator report + 15-day logs |
| [Filename sanitizer + media classification 2026-10-16](tgbot-2026-10-16-filename-sanitizer-and-media-classification.md) | **New shared `utils/filenames.py`** (NFKC, byte budget + stable hash, Windows reserved names, traversal, ZWJ/RTL-preserving; `ascii_only` legacy mode) adopted by every untrusted-name path (direct file, github cache names, Bale, web→Markdown, YouTube transcript, Bale custom filename, friend-media zip); re-verified the reported Errno 36 with the real yt-dlp template; **new defect: `[400 PHOTO_EXT_INVALID]`** — a TikTok photo-mode post exposes its *soundtrack mp3* as yt-dlp "best" and the `320×320` probe sentinel was read as "photo" (fixed: content-based `classify_media_file`/`ensure_photo_path`, image→photo / audio→audio / video→splitter / other→document); subsystem audit + 5 Telethon live tests green | operator report + 24-day logs + live bot |
| [Instagram photo posts 2026-10-18](tgbot-2026-10-18-instagram-photo-posts.md) | **Instagram photo posts / carousels were unreachable through the bot.** yt-dlp's IG extractor reads `video_versions` only, so a photo post has no formats and a carousel with any photo fails the whole playlist ("The site changed its layout or the URL is malformed"). Fixed with a **name-shadowing `InstagramIE` subclass** (replaces yt-dlp's extractor; adds `ig_photos`, defuses `raise_no_formats`) + native delivery (photos → quote-replied media group, carousel videos → splitter/uploader; video-only posts keep the format keyboard); IG-specific error text; README WebApp claims corrected (removed 2026-08-26); 15-day log audit found every other error cluster already fixed or transient; full monitoring/subsystem audit healthy; 3 live Telethon drives green | operator report + 15-day logs |

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

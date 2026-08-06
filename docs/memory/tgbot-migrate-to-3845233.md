# Migration: remote VPS → new machine (38.45.80.233)

The bot's production home moved from the small remote VPS
(`dev@66.23.198.52:1605`) to this machine (`/home/dev/opencode/tgbot`,
host `ubuntu`, IP `38.45.80.233`). Motivation: the old VPS's 8.7 GB disk
(3.6 GB free) could not survive a ~3.1 GB YouTube download — the merge step
peaks at ~2× the final file, ffmpeg died with
`ERROR: Postprocessing: WARNING: unable to obtain file audio codec with ffprobe`,
and the user's media never uploaded. The new box has 96 GB disk (~83 GB free),
3.8 GiB RAM + 4.0 GiB swap, `MemoryMax=2500M` in the systemd unit.

## What changed

1. **Size-aware disk pre-check** (commit `d9042af`) — `utils/downloader.py`:
   `required_merge_headroom(final_bytes) = final * 2 + 500 MB` models the merge
   peak (video part + audio + merged mp4 + metadata temp copy). `download_media`
   gained an `expected_size_bytes` param; the pre-download check and the metadata
   embed check now use it. The `dl:` dispatch in
   `modules/downloader_handler.py` checks disk **before** enqueueing and answers
   the callback with a user-facing alert if insufficient. On this machine the
   same 3.1 GB case now passes: needs ~6.7 GB peak, we have 82 GB free.

2. **Provisioning** — `./install.sh` installed ffmpeg + deps, Deno 2.9.4
   (`~/.deno/bin/deno`, added to PATH by `run.sh` — the bot process finds it,
   but a bare interactive shell does NOT, so standalone yt-dlp tests need
   `export PATH="$HOME/.deno/bin:$PATH"`), python venv with yt-dlp
   2026.07.04 + bgutil provider ref 1.3.1, and the prebuilt Go monitor.

3. **Secrets copy** — `.env` (DOMAIN updated to `http://38.45.80.233:8080`),
   `cookies/` jars, `database.json`, `direct_forward_state.json`,
   `direct_ig_session.json` copied from the remote via `scp`. All jars re-locked
   `0o444` by the bot at startup. The remote has no `direct_x_cookies.json`
   (X relay was off).

4. **Remote retired** — `tgbot`, `tgbot-monitor`, `cookie-watch` on
   `66.23.198.52` are stopped + disabled. It still holds a full secret copy and
   is treated as a trusted backup until decommissioned. Do NOT paste its
   credentials into tracked files.

5. **cookie-watch fix** — `tools/cookie_watch.sh` was adapted to local paths
   and needs the `inotify-tools` package (`inotifywait`); without it the unit
   crash-loops in `activating`. After `apt-get install inotify-tools` +
   `systemctl restart cookie-watch` it is `active` ("Setting up watches.").

6. **Temp sudoers drop-in removed** — `/etc/sudoers.d/99-tgbot-dev`
   (`dev ALL=(ALL) NOPASSWD:ALL`) was created for install.sh and deleted
   afterwards; passwordless sudo is gone.

## Verified end-to-end

- `ss -tlnp`: python on `0.0.0.0:8080`, deno PO provider on `127.0.0.1:4417`
  only (localhost-bound per invariant #2).
- Bot log: 3× `Session initialized: Layer 158`, PO provider v1.3.1 healthy,
  Uvicorn "Application startup complete", DirectForward started (chat
  7429671248), IG anti-detect warmup ok.
- `curl http://127.0.0.1:4417/ping` → `{"server_uptime":...,"version":"1.3.1"}`.
- Full pipeline test (`PATH` incl. `~/.deno/bin`): extraction of
  `C6Q2ZjyKxa0` returned 25 video-only + 12 audio-only formats (2160p60 top);
  a real `format='137+140/best'` download + mp4 merge completed in ~25 s.

## Gotchas found

- The PO provider and the `n` challenge solver BOTH need deno on PATH. The bot
  inherits it from `run.sh`, but standalone yt-dlp calls from a bare shell fail
  extraction with `Requested format is not available` (n challenge unsolved →
  formats missing). Symptom is misleading — it's the missing JS runtime, not the
  URL.
- The read-only jar (`0o444`) makes yt-dlp throw `PermissionError` if you pass
  the real path; always copy to `/tmp` for manual tests, as the bot does via
  `cookie_manager.acquire`.

## Rollback

If this machine must give up production: copy the same secret files back to
`66.23.198.52`, re-enable + start its units, and stop `tgbot` here. The
remote `.env` DOMAIN would need its original `http://66.23.198.52:8080`.

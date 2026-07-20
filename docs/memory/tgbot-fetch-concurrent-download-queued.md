# Fetch concurrent / download queued

In tgbot (`modules/downloader_handler.py`) the single-worker `DownloadQueue`
(`utils/queue_manager.py`) serializes the **real download+upload jobs only**:
`queued_transfer_job` (single video), `playlist_job` (whole playlist), and
`direct_upload_job` (direct file). (This is a stripped, repo-local copy of a
project memory.)

Metadata **fetches** (`show_format_selection` → `extract_formats`, the
`begin_playlist_flow` `meta_job` → `extract_playlist_meta`, and the playlist
`single`/`plx` picks) are spawned concurrently via `_spawn_fetch`
(`asyncio.create_task`, refs held in `_bg_fetch_tasks`) and must NOT go through
`queue.add_task`.

> **Why:** a user sending link B while link A is downloading must see B's format
> buttons immediately — not be queued behind A's download. The old behavior
> blocked the fetch, forcing the user to wait for each download before picking the
> next format. Downloads still run one-at-a-time so many can be queued and
> collected later.

**How to apply:** never route a fetch through `queue.add_task`. Never call
`extract_formats` / `extract_playlist_meta` inline — they are blocking yt-dlp
calls; always wrap in `loop.run_in_executor(None, ...)`, or concurrent fetches +
any running download will freeze the event loop. Documented as invariant #10 in
`AGENTS.md` (commit `1d2a199` / `5a74b3b`, 2026-07-19). Related:
[YouTube size fix & IP flag](tgbot-youtube-size-and-ip-flag.md).

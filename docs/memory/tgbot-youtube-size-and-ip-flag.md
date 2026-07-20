# YouTube size fix & IP flag

Two durable facts about YouTube handling in tgbot (commit `5003d78`, 2026-07-19).
(This is a stripped, repo-local copy of a project memory; the real test-VPS IP is
redacted as `<vps-ip>`.)

**Size mismatch root cause.** The single-video download selector was
`{format_id}+bestaudio/best`. When that exact `format_id` was stale or not
mergeable under the `mweb` player client, yt-dlp **silently** fell back to the
final `/best` — a single low-res **muxed** stream. The button had shown the
high-res video + best-audio size, so the uploaded file came out far **smaller**
(and lower quality) than promised. Fix: fall back to a height-capped **merged**
`bestvideo[height<=H]+bestaudio` instead of muxed `/best`, and mark
`filesize_approx` / tbr / heuristic estimates with `~` (only a real
`filesize`/`clen` is exact). See `utils/downloader.py` `download_media` and
`extract_formats`.

> **Why:** the previous model assumed audio was added on top → bigger; reality
> was the opposite (silent muxed collapse → smaller). Always reason from the
> selector's fallback chain, not from "audio is added." See
> [How download-button sizes are computed](tgbot-ytdlnis-size-approach.md) for
> the deeper yt-dlp mechanism.

**Intermittent YouTube storyboard / IP flag (NOT a code bug).** The test VPS IP
(`<vps-ip>`) gets bot-flagged by YouTube periodically: `extract_info` returns
**only storyboard/preview formats**, every player_client fails, even with valid
cookies + a healthy PO provider on `127.0.0.1:4417`. It clears on its own (hours).
When this happens, **flat playlist extraction still works** (it lists the page
without PO tokens), but per-video download fails. Don't chase this as a code
defect — it's YouTube anti-bot on the IP. See
[Cookie protection & monitor](tgbot-cookie-protection-and-monitor.md).

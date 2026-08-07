# TikTok >10MB videos arrived thumbless — thumbnail discovery + fully-provided uploads

**Date:** 2026-08-07
**Status:** Fixed, verified live, shipped (`main`)

## Symptom

The operator observed that TikTok videos **larger than ~10 MB** arrived in
Telegram **without a thumbnail**. YouTube videos never showed the problem.

## Root cause (two layers)

1. **Telegram's server only auto-generates a video thumbnail for files
   < 10 MB.** For larger files the sender MUST provide one or the message
   renders with no preview. (python-telegram-bot #1155 quotes the Bot API
   changelog: "for … videos … less than 10 MB in size, thumbnails are
   generated automatically"; confirmed live on 10–20 MB Telegram.Bot / go-
   telegram-bot-api threads.) This is why *only large* videos misbehaved —
   small TikTok videos hid the bot's missing thumb behind server generation.

2. **The bot was never finding TikTok's cover.** yt-dlp's `writethumbnail`
   writes TikTok's cover as `<title>.image` (extension `.image`, still a
   valid JPEG — verified `FF D8 FF` magic / mjpeg 540×960), but
   `download_media` only looked for `.jpg/.jpeg/.png/.webp`. So
   `thumb_path` was always `None` for TikTok. YouTube is unaffected because
   its cover is a real `.jpg`. **Every** TikTok upload was actually
   thumbless; small ones just had the hole covered by server generation.

## Fixes

- `utils/downloader.py`:
  - `_find_thumbnail_file(base_path, task_dir)` — discovery is now **magic
    bytes, not extensions**: known extensions first, then a scan of the task
    dir for `<stem>.*`/`<stem>_*` siblings whose bytes look like
    JPEG/PNG/WebP/GIF/BMP (`_looks_like_image`). `download_media` uses it.
  - `extract_video_frame_thumb(video_path)` — 320×320 frame fallback
    (ffmpeg `-ss 1`, `-ss 0` retry, best-effort `None`). Named
    `<video>_thumb.jpg` so concurrent relays never clobber a shared file.
- `utils/uploader_handler.py` — every video send is now fully specified:
  - `send_single_media` / `_stage_and_relay` (premium): if no usable cover,
    fall back to `extract_video_frame_thumb` before `send_video`.
  - `process_split_and_upload`: thumb resolved **once** before the split
    loop and attached to part 1 (previously NO part of a split file got a
    thumb).
- `modules/direct_forward.py` — DM-relay native sends (IG single/carousel,
  IG DM attachment, X DM attachment) previously called `send_video` with
  only `caption` + `supports_streaming`. New `_video_upload_kwargs(path)`
  probes `width/height/duration` and generates a frame thumb; used by all
  four native `send_video` calls and the `InputMediaVideo` carousel group.
  The relays' `finally` blocks also remove the generated `<video>_thumb.jpg`.
- `tools/telethon_drive.py` — `media_summary` crashed on Telethon `Document`
  (`no attribute 'w'`); now reads the `DocumentAttributeVideo` and reports
  `thumb_count` too.

## Verification (feedback loop)

- Offline probe: TikTok `.image` cover now discovered + converted
  (`cache/<id>/thumb.jpg`, valid 320×320 JPEG). Frame fallback produced a
  valid JPEG; `_video_upload_kwargs` on a synthetic mp4 returned
  `width/height/duration/thumb` all populated.
- Live drive of the same 12.1 MB TikTok through the bot, message inspected
  via Telethon:
  - msg 88384 (pre-fix run at 10:39): `thumbs []` ← the bug
  - msg 88390 (post-fix run at 11:09): `thumbs [('m', 320, 320)]` ✓
- YouTube regression (`jNQXAC9IVRw`): `thumb_count 1`, `w 320 h 240
  duration 19.064`, `supports_streaming` ✓.
- `<10MB` TikTok videos keep their server-generated thumbs (unchanged).
- `logs/bot.log`: zero tracebacks/errors since restart.

## Lesson

Thumbnail discovery for downloaded media must never rely on the cover's file
extension — extractors name covers unpredictably (TikTok `.image`). Guard with
image magic bytes, and treat Telegram's <10 MB server-thumbnail rule as the
amplifier that turns a "thumb not found" bug into a user-visible one.

See `AGENTS.md` invariant #17.

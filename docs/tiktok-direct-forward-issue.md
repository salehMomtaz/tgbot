# TikTok Direct Forward - Known Issue (Aug 2026)

## Problem

TikTok direct forward (self-DM relay via WebSocket) receives video shares correctly, but fails to download the videos with the error:

```
yt_dlp.utils.ExtractorError: [TikTok] <video_id>: Unexpected response from webpage request
```

## Root Cause

This is a **known upstream issue in yt-dlp** ([issue #17403](https://github.com/yt-dlp/yt-dlp/issues/17403), reported Aug 10, 2026).

TikTok changed their anti-bot challenge format. yt-dlp's `_solve_challenge_and_set_cookies` function cannot parse the new challenge page, causing all TikTok downloads to fail with "Unexpected response from webpage request".

The issue affects:
- TikTok direct forward (self-DM relay)
- Interactive TikTok downloads (sending links to the bot)
- Any TikTok download via yt-dlp

## Current Status

- **WebSocket connection**: Works correctly - receives self-DM pushes via `im-ws-sg.tiktok.com`
- **Video metadata extraction**: Works via oEmbed API (gets author, itemId)
- **Video download**: **FAILS** due to yt-dlp's broken challenge solver

## Workarounds

### 1. Wait for yt-dlp fix (Recommended)
The yt-dlp team typically fixes site-breaking changes within days. Monitor [issue #17403](https://github.com/yt-dlp/yt-dlp/issues/17403) for updates.

### 2. Use interactive download instead
Send TikTok links directly to the bot. While this uses the same yt-dlp pipeline, some users report it works intermittently.

### 3. Manual download
Use the TikTok app/website to save the video, then send the file to the bot.

## Bot Behavior

- TikTok direct forward worker logs clear error messages referencing the yt-dlp issue
- Admin console test (`test_tiktok_connection`) shows a warning about the known issue
- Failed downloads are logged but don't crash the worker - it continues listening for new shares
- When yt-dlp releases a fix, the bot will automatically work after updating (`./run.sh` pulls latest yt-dlp)

## Monitoring

Check logs for:
```
[DirectForward/TT] relay of msg <id> failed: TikTok's anti-bot challenge has changed...
```

Or in the log channel:
```
🚨 [ERROR] ... TikTok's anti-bot challenge has changed and yt-dlp cannot solve it (known issue: https://github.com/yt-dlp/yt-dlp/issues/17403)
```

## Fixing After yt-dlp Update

When yt-dlp releases a fix:
1. Update the bot: `./run.sh` (pulls latest yt-dlp nightly with `[default,curl-cffi]`)
2. Restart the bot: `sudo systemctl restart tgbot` or use Admin → 🔄 Restart Bot
3. Test with Admin → 📨 Direct-Forward → 🧪 Test TikTok

## Technical Details

The challenge page HTML contains the video data (`playAddr`, `desc`, `author`) but yt-dlp's parser looks for a specific script tag (`__UNIVERSAL_DATA_FOR_REHYDRATION__`) that doesn't exist on the new challenge page format.

A temporary workaround was attempted (direct CDN download via extracted `playAddr`), but TikTok serves CAPTCHA pages for direct HTTP requests, making it unreliable.

## Related Files

- `utils/downloader/download.py` - TikTok retry logic with improved error messages
- `utils/downloader/errors.py` - Error classification for TikTok challenge failures
- `modules/direct_forward/tiktok.py` - Direct forward worker with connection test warning
- `docs/memory/tgbot-tiktok-direct-dm.md` - Original implementation design
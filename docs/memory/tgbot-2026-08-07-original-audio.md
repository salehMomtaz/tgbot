# Multi-audio YouTube videos merged a DUBBED track instead of the original audio

**Date:** 2026-08-07
**Status:** Fixed, verified live, shipped (`main`)

## Symptom

Operator downloaded `https://youtu.be/-ZXWaps2Z2g` (a debate video), picked a
video quality, and received the video with **Hindi AI-dubbed audio** instead of
the original English. Nothing in the request asked for dubbing.

## Root cause

The video carries **7 parallel audio tracks** (de, es, fr, hi, id, it, pt, en)
for each audio itag. The YouTube extractor marks each one:

- `language_preference = 10` → **original** track (format_note `(original)`)
- `language_preference = 5` → **default** track
- `language_preference = -1` (unset) → dubbed track

yt-dlp's own `bestaudio` selector sorts by `lang` first, so it correctly picks
the original. **The bot does not use `bestaudio` for single videos** — it picks
`best_audio_format_id` in `extract_formats` by sorting audio options **purely on
bitrate** (`utils/downloader.py`). On this video the original English is the
*lowest*-bitrate track (104k opus / 129k m4a) while the dubs run 130-134k, so
the merge pulled a dubbed track:

- old: `best_audio_format_id` = `251-6` (`id`, Indonesian, 134k) — a dub
- new: `best_audio_format_id` = `140-7` (`en-US` original, pref 10, 129k m4a)

Small/normal videos are unaffected because they have a single audio track
(`language_preference` unset everywhere → tie, bitrate decides, as before).

## Fix

`extract_formats` now records `language_preference` per audio format and sorts
audio options by `(language_preference, bitrate)`, both descending — original
tracks first, then default, then dubs, and within each class highest bitrate
first (`utils/downloader.py`). Consequences:

- video merge uses `best_audio_format_id` = the best **original**-language track;
- the audio buttons show original tracks first (dubs still listed below if
  someone explicitly wants one);
- the `+bestaudio` fallbacks and playlist `PLAYLIST_TIERS` selectors were already
  language-aware (`bestaudio` prefers the original), so no change needed there.

## Verification (feedback loop)

- `extract_formats` for `-ZXWaps2Z2g`: `best_audio_format_id` 140-7 (pref 10)
  instead of the previous top-bitrate dub.
- Live drive through the bot (picked 360p): delivered `Dr Frances Welsing...mp4`
  downloaded from Telegram and probed — **audio = aac 128k stereo** (format 140,
  the original) and h264 640x360. A dub would have been Opus.
- Format keyboard now lists `129k` (140-7) and `104k` (251-7), the two originals,
  above the dubbed `134k`/`131k`/`130k`.
- Zero errors in `logs/bot.log` since restart.

## Lesson

Never re-rank audio (or video) formats with a homegrown quality metric that
ignores the extractor's own language/source preference fields. yt-dlp already
encodes "original > default > dub" as `language_preference` and its format sort
honours it; any custom selection logic must keep that key ahead of bitrate.

See `AGENTS.md` invariant #18.

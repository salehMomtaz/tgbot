# Exact CDN size probe (the "button ~5M, upload 2 MB" fix)

**Date:** 2026-08-03 · **Area:** `utils/downloader.py` (`extract_formats`)

## Symptom

Format buttons on **Instagram reels** consistently showed sizes far above the
uploaded file — measured case: button `~5M`, delivered file `2,100,332` bytes
(−168%). YouTube and TikTok buttons were accurate (TikTok matched to −0.0%).

## Root cause (proven with a live probe on the test VPS)

Instagram reels now extract as **DASH formats** (`dash-…v` video-only +
`dash-…a` audio-only) with:

- **no `filesize`** (only TikTok reports it),
- **no usable `tbr/vbr/abr`** on the video entries,
- frequently **no `duration`** on the post (`info['duration'] = 0`).

`estimate_format_size` then fell through to the `DEFAULT_POST_DURATION = 60`
heuristic: a ~21 s reel got priced as 60 s of 1080p content (~3× overshoot),
and the "~" prefix told the user an estimate that was actually a blind guess.

## Fix

`_apply_cdn_size_probes()`: after deduping + clipping to button-visible
formats (≤5 video + ≤5 audio), any entry collected with

- `exact == False` (no real `filesize`), **and**
- `duration_seconds <= 0` **or** no stream metadata at all

gets its stream URL **HEAD**ed (valid `Content-Length` wins); CDNs that reject
HEAD get a 1-byte **Range GET** and the total is parsed from
`Content-Range: bytes 0-0/<total>`. Winners become `exact: True`.

Verified: button `2M` (`2,094,484` = video 1,866,812 + audio 227,672) vs
actual uploaded `2,100,332` — within 0.3% (mp4 merge + ffmpeg metadata embed).

## Do / don't

- **DO** keep the probe scoped to blind-guess formats only. YouTube has
  `filesize_approx`/tbr per format; probing it would add ~10 CDN requests per
  link for zero accuracy gain.
- **DO** probe before the `video + best_audio` merge-sum, so merged buttons sum
  exact parts and drop the `~`.
- **DON'T** remove the `~` prefix from ordinary tbr estimates — they still
  overshoot a little on purpose-bearing bitrate spikes.
- Sub-MB sizes render as `K` (e.g. `222K`), MB is rounded to nearest.

## Related display rules (fixed in the same pass)

- Muxed single-stream sites (TikTok) have **no separate best-audio stream**:
  exactness of a video button is the video's own; the joint
  `video.exact AND best_audio.exact` rule only applies when an audio stream
  actually merges in.
- `format_size_short` no longer floors MB (`int()`), it rounds.

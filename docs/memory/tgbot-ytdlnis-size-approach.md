# How download-button sizes are computed (ytdlnis / yt-dlp reference)

Reference note (2026-07-20) on how **ytdlnis** (https://github.com/deniscerri/ytdlnis)
and **yt-dlp** get the file size that a download button shows, and how that maps
onto tgbot. (This is a stripped, repo-local copy of a project memory.)

**Core fact — sizes are PER-FORMAT, yt-dlp never sums a merge.** yt-dlp exposes
three size-related fields on each format dict:

- `filesize` — exact, from a real HTTP content-length / `clen` (YouTube DASH).
  `None` when yt-dlp never made the HEAD/range request.
- `filesize_approx` — derived from bitrate × duration. `--print "%(filesize,filesize_approx)s"`
  returns `NA` for some videos.
- `tbr` / `vbr` / `abr` — total / video / audio bitrate in kbit/s.

For a `bestvideo+bestaudio` (merged) download, **yt-dlp does NOT report the
combined size**. `--print filesize_approx` on a `v+a` selector returns **only one
stream** (usually the audio), not the sum (yt-dlp issue #947). The caller must
add `video_size + audio_size` itself. yt-dlp #2518 confirms filesize filters
operate per-stream on combined selections too.

**What ytdlnis therefore does** (Kotlin app driving yt-dlp): for each candidate
format it takes `filesize` if present, else `filesize_approx`, else computes
`bitrate × duration / 8` from `tbr`/`vbr`/`abr`; for a merged format it sums the
chosen video track + chosen audio track; and it visually distinguishes an exact
`clen` from an estimate.

**tgbot already matches this** — `utils/downloader.py` `estimate_format_size`
does `filesize -> filesize_approx -> tbr/vbr/abr × duration`; the format-builder
adds `best_audio_bytes` to each video tier (`v['bytes'] += best_audio_bytes`); and
only a real `filesize`/`clen` is shown as exact — everything else gets a `~`
prefix. So the sizing model is correct; do **not** rewrite it on size grounds.

**The residual, real cause of "uploaded ≠ shown" is NOT the size math — it's the
selector falling back to a *different* stream than the one that was sized.** That
was the silent `{format_id}+bestaudio/best` collapse to a muxed `/best` (fixed in
`5003d78`, see [YouTube size fix & IP flag](tgbot-youtube-size-and-ip-flag.md)).
The lesson: any future size complaint → first check the fallback chain in
`download_media`, not the estimator. ytdlnis avoids this by sizing the *exact*
format id it will request and refusing silent quality collapse — a behavior worth
imitating if more mismatch reports appear.

Sources: yt-dlp issues #947 and #2518; ytdlnis repo format/size handling.

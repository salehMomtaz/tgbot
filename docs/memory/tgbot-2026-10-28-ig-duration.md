# 2026-10-28 — Instagram video duration always 0 + health pass

Operator report: *"when i send instagram links to download videos the duration
of videos in response message is always 0 and this is incorrect find out why
this happens and fix it"*, bundled with the standing maintenance ask (health
check every subsystem, fix active problems highest severity first).

**Result: fixed and verified live — the format keyboard now shows the real
length (`0m 16s` for a 16.03 s reel) and the delivered video bubble carries
`duration: 16.048`. The health pass found no other active failure.**

Baseline before any change: `26ee244`, `tgbot` active, `NRestarts=0`,
MainPID 659, disk 39 %, load 0.25, PO provider healthy on
`127.0.0.1:4417`, Go monitor pid 987, `cookie-watch` + `xchat-bridge` active.

---

## Defect — Instagram duration was always 0

### Symptom

Sending any Instagram reel produced:

```
📥 Format Selection
📝 Title: Video by co1inmyers
⏱ Duration: 0m 0s
```

### Root cause (two independent halves)

1. **yt-dlp reads a field Instagram no longer sends.** `InstagramIE.
   _extract_product_media` maps `'duration': product_media['video_duration']`.
   Verified live on `instagram.com/reel/DYW7WMRM4kd`: the logged-out GraphQL
   `product_media` has **no `video_duration` key at all** (the key set was
   dumped), so `info['duration']` is `None` → `extract_formats` normalises to
   0 → `0m 0s`, and `estimate_format_size` fell back to its blind 60 s
   heuristic.
2. **The existing name-shadowing extractor was not registered on the format
   path.** `utils/downloader/instagram_post.InstagramIE` (added 2026-10-18 for
   photo posts) was registered only inside `fetch_instagram_post` (/p|tv/).
   `extract_formats` and `download_media` built plain `yt_dlp.YoutubeDL`
   objects, so reels and video-only posts ran *upstream's* extractor.

### Fix

* `utils/downloader/instagram_post.py`
  * `_dash_duration_seconds(manifest)` — parses the ISO-8601
    `mediaPresentationDuration` out of `video_dash_manifest` (still present in
    the same payload: `PT16.033333S`). Never raises; returns `None` for
    missing/zero/unparseable values.
  * `InstagramIE._extract_product_media` now fills a **missing** duration from
    that manifest. A duration the extractor *did* report stays authoritative,
    so a future upstream fix keeps working.
  * `register_instagram_extractor(ydl)` — the supported way to swap the
    built-in extractor (`ie_key() == "Instagram"` makes
    `add_info_extractor` replace, not append).
* `utils/downloader/formats.py` registers it on the extraction `YoutubeDL`.
* `utils/downloader/download.py` registers it via a new `_ydl(opts)` factory
  used by every download/retry attempt.
* Docs: AGENTS.md invariants #11(b) and #21 updated; regression tests added to
  `tests/test_instagram_post.py` (ISO-8601 parsing, fill-vs-keep, and a
  source-level guard that both flows keep registering the extractor).

No extra network request, no second GraphQL client, no `doc_id` to rot.

### Verification (live, after deploy + restart)

* Offline: `extract_formats(reel)` → `duration 16.033333` (was `None`/0);
  sizes `~484K/~375K/~308K` now come from `tbr × duration`
  (estimated 495 K vs 500 K real, ~1 % error).
* `download_media(...)` → `duration 16.033333`; ffprobe of the merged file
  `16.047`.
* Driven through the real bot via Telethon as the operator:
  * keyboard: `⏱ Duration: 0m 16s`
  * pressed `🎥 1280p (~484K)` → uploaded `Video by co1inmyers.mp4`,
    `media.duration = 16.048`, 720×1280, thumbnail present, 500 KB.
  * log: `✅ Job Successful: Video by co1inmyers.mp4`.
* Tests: all three suites pass (`test_downloader_media` 7/7,
  `test_filenames` 22/22, `test_instagram_post` all checks).

---

## Health pass (no other active problem)

| Subsystem | State | Evidence |
|---|---|---|
| `tgbot` service | healthy | active/running, new MainPID 114938, `NRestarts=1` (our deploy), no ERROR/WARNING since restart |
| PO-token provider | healthy, loopback-only | `[POT] Provider is healthy on 127.0.0.1:4417`, `ss` shows `127.0.0.1:4417` (not `[::]`), `/ping` 200 |
| YouTube path (cookies + PO, no fallback) | working | live `extract_formats(jNQXAC9IVRw)` → 19 s, formats listed |
| Stream server | healthy | native TLS on `:8080`; `/stream/<fake>` → 404 (route alive); public `https://tgbot.southpark.ir:8080/stream/...` → 404 |
| Go system monitor | healthy | detached pid alive 9 h 51 m, monotonic `report sent (2220 samples)` |
| `cookie-watch` / `xchat-bridge` units | active | `systemctl is-active` = active, bridge relay connected |
| X self-DM relay | healthy | conversation poll `200 OK` every ~5-6 min, bridge cursor state present |
| TikTok WS relay | healthy | `WS connected + cmd-1001 sent` after restart |
| Friend Media | healthy | per-friend scans logged, nothing new |
| Cookie jars | fresh | YT/TT/X jars written today 08:45-08:46, jars `444` |
| TLS cert | valid | `certs/fullchain.pem` notAfter **2026-12-04** |
| Disk / RAM / load | healthy | 39 % used (59 G free), 2.2/3.9 GB used + 7.7 G swap free, load 0.25 |
| Log noise | clean | 3 ERROR lines in the 5 MB rotated log, all already-known upstream blips (one TikTok WS 3003 ping timeout, one X tweet with no video) |

**Deliberately left alone**

* `IG_AUTH_ENABLED=false` (operator paused Instagram after the suspension);
  every authenticated IG feature is skipped by design. Anonymous photo/reel
  delivery is unaffected — verified above.
* `tgbot-monitor.service` inactive — the bot spawns the detached monitor at
  startup, which is running (enabling the unit as well would double-report).
* IG size buttons lost their exact CDN probes: with a real duration the
  `tbr × duration` estimate is ~1 % off and carries the honest `~` prefix, so
  the probes are no longer needed; tbr-less formats still get probed.

## Deploy record

* Fix commit `aa3963f` (`fix(downloader): take Instagram video duration from
  the DASH manifest`), pushed to `main`.
* Restarted via SIGTERM on the old MainPID (no sudo needed; `Restart=always`).
* Rollback path if ever needed: `git revert aa3963f` + restart — the fix is
  self-contained in `utils/downloader/`.

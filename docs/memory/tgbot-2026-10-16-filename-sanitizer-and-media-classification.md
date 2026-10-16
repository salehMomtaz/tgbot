# 2026-10-16 — shared filename sanitizer + direct-forward media classification

Session scope: take a production snapshot, audit every monitoring/subsystem,
fix the reported X/Twitter direct-forward "file name too long or invalid"
error at the root, and implement the requested **single shared
filename-sanitizing module**. Two live defects were found and fixed (one of
them a *new* failure that appeared after the 2026-10-10 patch), a shared
sanitizer was introduced and adopted by every untrusted-name path, and the
live bot was driven end-to-end to prove the fixes.

Snapshot at session start (rollback point):

- HEAD `56846c3` (`docs: record 2026-10-10 filename-length audit + subsystem
  status`), working tree clean, branch `main`.
- `tgbot.service` active 7h (PID 654), `NRestarts=0`; xchat-bridge,
  cookie-watch, nginx, fail2ban all active with 0 restarts.
- Rollback path used: `git reset --hard 56846c3` (+ `kill -TERM <MainPID>` to
  re-read the tree; systemd `Restart=always` relaunches `run.sh`).

## Defect 1 (reported) — `OSError [Errno 36] File name too long` (X relay)

The operator's report ("file name too long or invalid") is the 2026-10-09 X
relay failure already root-caused on 2026-10-10:

```
[DirectForward/X] bridge message 2108536104957923328 failed (attempt 1..3):
  [Errno 36] File name too long:
  'cache/x_0bd05999/GAYROCK2026 🌋🌈🏳️‍🌈(6️⃣0️⃣K) 👬😀🎵🎶 - 🌈🌈🌈… 🌋🌋....jpg'
```

Cause: yt-dlp sanitizes illegal *characters* but does not bound *length*; a
tweet title that is a wall of emoji (4 UTF-8 bytes each) overflows Linux's
**255-byte** per-component `NAME_MAX` when the extractor writes
`<title>.jpg`/`<title>.mp4`, the download fails, and the relay's 3-strike cap
skips the message (a **drop**, not a retry).

Re-verified this session with the real yt-dlp template (not just reasoning):

```
title bytes           : 329
OLD component bytes   : 333 -> write: OSError 36 File name too long
NEW component bytes   : 152 -> write: OK
```

The 2026-10-10 fix (`%(title).150B`, byte-based truncation) is live and
correct. Its budget now comes from the shared module
(`utils/filenames.MAX_TITLE_BYTES`) so there is one definition of "safe".
**`yt-dlp`'s own `trim_file_name` option must NOT be used** — it truncates the
whole path string (`cache/<id>/...`) to N characters and would corrupt the
directory.

### What was actually added this session — `utils/filenames.py`

A single shared sanitizer, `sanitize_filename(value, *, fallback, ext,
max_bytes, ascii_only, strip_dirs, keep_extension, hash_len)`, replacing five
ad-hoc sanitizers that each capped the wrong unit (characters, not bytes) or
capped nothing:

1. **Byte budget** (`MAX_COMPONENT_BYTES = 200`) enforced on the *whole*
   returned component, measured in UTF-8 bytes, never splitting a multi-byte
   code point (`truncate_bytes` slices the encoded value and drops a partial
   trailing sequence).
2. **Uniqueness under truncation**: a stable `blake2b` digest (8 hex chars) of
   the *original* input is appended only when truncating, so two distinct long
   names sharing a prefix cannot collide, and the same input always yields the
   same name across processes/restarts (unlike Python's salted `hash()`).
3. **NFKC** normalisation (full-width digits/letters → ASCII, NBSP → space,
   ligatures folded — and full-width `ＣＯＮ` is therefore caught as a reserved
   name).
4. **Character policy**: Cc (controls), Cs (surrogates), Co (private use) and
   Cf (**bidi/format overrides** — a U+202E in a filename can disguise the real
   extension) are removed, **except ZWNJ/ZWJ**, which Persian/Arabic text and
   emoji ZWJ sequences (🏳️‍🌈 = 🏳 + ZWJ + 🌈) legitimately need. Windows-illegal
   `<>:"/\|?*` plus control/DEL are replaced; whitespace runs collapse to one
   space; separator runs collapse to one `_`.
5. **Portability**: trailing dots/spaces stripped, Windows device names
   (`CON`/`NUL`/`COM1`…`LPT9`, with or without extension) prefixed with `_`,
   result is never `.`/`..`.
6. **Traversal**: `strip_dirs=True` (default) keeps only the final component
   (`../../etc/passwd` → `passwd`, `C:\Windows\evil.exe` → `evil.exe`).
   `strip_dirs=False` flattens separators instead — required for composed stems
   like `f"{repo}_{branch}"`, whose branch may contain `/`.
7. **Extension** preserved (`keep_extension`) or forced (`ext=`), never
   duplicated (`clip.webm` + `ext=".mp4"` → `clip.mp4`), always inside budget.
8. **Fallback**: an id-based caller-supplied name (`download_<cache_id>`), and
   `"file"` if even that is unusable.
9. `ascii_only=True` = the legacy strict allowlist (`[A-Za-z0-9._-]`, everything
   else `_`) — what `safe_task_filename` and Bale used, kept byte-for-byte
   compatible for those callers.

### Adoption (every untrusted-name path)

| Path | Before | Now |
|---|---|---|
| `utils/security.safe_task_filename` (direct-file + interactive display names) | ASCII regex, **char** cap 150, no reserved-name handling | delegates, `ascii_only=True`, 150 **bytes** |
| `modules/github/handlers.safe_cache_filename` | ASCII regex, **no length cap** | delegates, 120 B, `strip_dirs=False` |
| `modules/bale/uploader.sanitize_filename_for_bale` | regex, 40 **chars** on the stem | delegates, 60 B, `ascii_only=True` |
| `modules/web/handlers` web→Markdown `<title>` | inline join, **no cap** | delegates, 100 B |
| `modules/bale/runner` web→Markdown `<title>` | inline join, **no cap** | delegates, 100 B |
| `modules/bale/runner` **custom filename** | **trusted verbatim** (`cn if cn.endswith(ext) else ...`) | `safe_task_filename` |
| `modules/youtube/handlers` transcript name | inline join, **no cap** | delegates, 100 B |
| `modules/friend_media` archive zip name | raw user-supplied ig username | delegates, 60 B |
| `utils/downloader/download` yt-dlp title | literal 150 | `MAX_TITLE_BYTES` from the shared module |

Audited and left alone (names are generated locally from fixed prefixes +
ids/timestamps, never from remote text): the direct-forward photo/DM paths
(`cache/df_x_photo_<ts>_<i>.jpg`, `cache/df_pk_<pk>.jpg`,
`cache/df_native_<pk>_<i>.<ext>`), `cache/<uuid8>/…` task dirs,
`convert_thumbnail_to_jpeg` (`cache/<id>/thumb.jpg`),
`embed_metadata_ffmpeg` (uses `tempfile.mkstemp`), the splitter's
`<stem>.partNNN`, and the ffmpeg frame thumb `<video>_thumb.jpg` (covered by the
200-byte budget's margin). `tests/test_filenames.py`
(`test_shared_helpers_still_route_through_sanitizer`) asserts the two public
helpers still honour their contracts.

## Defect 2 (found this session) — `[400 PHOTO_EXT_INVALID]` on a TikTok photo post

Scanning the retained logs (Sep 23 – Oct 16) surfaced one failure *after* the
2026-10-10 patch:

```
2026-10-11 13:31:56 [DirectForward/TT] share -> https://www.tiktok.com/@karl_being_relatable/video/7689851507444845832
2026-10-11 13:31:57 ERROR utils.queue_manager  Task c4e9... failed
  ... common.py:225 in _download_and_deliver -> bot_client.send_photo(...)
  pyrogram ... PhotoExtInvalid: [400 PHOTO_EXT_INVALID] ... (caused by "messages.SendMedia")
2026-10-11 13:31:57 ERROR modules.direct_forward.tiktok  relay of msg 7695402480045508148 failed
```

### Root cause (reproduced, not inferred)

The URL is a TikTok **photo-mode** post. Reproducing it through the bot's own
pipeline:

```
extract_formats -> videos: []            # no video formats at all
download_media(...) -> file_path = '... #karl_being_relatable #christian #....mp3'
                        magic: b'ID3\x04'    size 325217
sniff_image_extension -> None            # it is NOT an image
probe_video_dimensions -> (320, 320, 20)  # the sentinel!
```

yt-dlp exposes the post's **soundtrack (.mp3)** as its "best" format, and
`probe_video_dimensions` returns the historical `(320, 320, …)` sentinel for
*any* file with no video stream — so `_download_and_deliver`'s
`is_photo = (width == 320 and height == 320) or …` concluded "photo" and called
`send_photo` on the mp3. Telegram rejects it by extension. The same
`320×320`-as-photo heuristic existed in `twitter._x_deliver_tweet`, so the
class was cross-platform.

The 2026-09-04 TikTok photo-share incident has the same fingerprint, which is
why the earlier magic-byte fix did not fully close the class: it fixed the
*extension* of things that are images, but not the *misclassification* of files
that are not.

### Fix — classify by content

`utils/downloader/thumbnails.py`:

- `probe_media_info(path)` → `{has_video, has_audio, width, height, duration}`
  or `None`; never fabricates values. **Excludes `attached_pic` streams**, so an
  M4A carrying embedded cover art (our own `embed_metadata_ffmpeg` writes one)
  is not "video".
- `probe_video_info(path)` → the real video stream info, or `None`.
- `probe_video_dimensions(path)` → the legacy tuple API, unchanged for the
  uploader/Bale callers, still with the `(320,320,…)` sentinel (documented as
  *not* for classification).
- `classify_media_file(path)` → `"image" | "video" | "audio" | "other"` from
  image magic + ffprobe stream kinds, with audio-magic as an ffprobe-failure
  fallback.
- `ensure_photo_path(path)` → a path `send_photo` accepts: rename to the real
  magic for JPEG/PNG, **transpose WebP/GIF/BMP to JPEG with ffmpeg** (first
  frame), `None` when impossible so the caller sends a document instead of
  guessing.

`common._download_and_deliver` and `twitter._x_deliver_tweet` now route on
`classify_media_file`: image → `send_photo` (converted), audio → `send_audio`,
video → the splitter, other → `send_document`. No path can send a non-image as
a photo any more.

### Verification (same URL, through the real function)

```
OLD routing (320 sentinel) -> send_photo      # the bug
NEW routing (classifier)   -> audio
OPENS AS PHOTO?             None
=> action: send_audio

# stubbed-Telegram integration run of _download_and_deliver:
send_audio | {'audio': 'Bro really left a mark on history. …....mp3',
              'caption': '[DirectForward/TT] test\n\nsmoke', 'duration': 20}
OK: relay delivered via send_audio (no send_photo, no PHOTO_EXT_INVALID)
```

Tests: `tests/test_downloader_media.py` (7 tests, real media generated with
ffmpeg, per-case skip when ffmpeg/encoder is unavailable) — mp3-is-audio,
m4a-with-cover-art-is-audio, mp4-is-video, jpeg/png-is-image,
webp-is-converted-to-jpeg, missing/empty-is-other, audio-magic-never-image.

## Tests

Two standalone suites (no pytest installed; plain scripts that are also
pytest-compatible), 29 tests total, all green:

- `tests/test_filenames.py` — 22 tests: the reported emoji bug title at a real
  byte budget, stability/determinism, collisions on long shared prefixes, emoji
  preservation, ZWJ preservation, RTL (Persian) preservation + bidi-override
  removal, empty/junk → id fallback, Windows reserved names (incl. `COM1.mp4`),
  NFKC (incl. full-width reserved), traversal stripping, control/illegal chars,
  trailing dots/spaces, whitespace/separator collapsing, forced extension,
  character-boundary truncation (never a partial code point / `\ufffd`),
  byte budget for every input class, ascii-strict mode, suffix-only-on-truncate,
  single-safe-component invariant, helper delegation, constants sanity.
- `tests/test_downloader_media.py` — 7 tests as above.

## Live bot verification (Telethon-driven, `tools/telethon_drive.py`)

Deployed via `kill -TERM $(systemctl show tgbot --property=MainPID --value)`
(new PID 91208, `NRestarts=1`, clean start), then driven as the operator:

| Test | Result |
|---|---|
| YouTube video keyboard + `240p` pick | ✅ `✅ Job Successful: Me at the zoo.mp4`, 633 830 B, 320×240, duration 19.06, `thumb_count=1` |
| TikTok video pick (720×1280) | ✅ delivered 2 004 744 B, `thumb_count=1`; emoji/hashtag title came through with the byte-bounded `....mp4` name |
| Direct file `proof.ovh.net/files/1Mb.dat` | ✅ `📁 Part: 1Mb.dat`, 1 048 576 B document |
| SSRF probe `http://127.0.0.1:4417/` | ✅ refused: "Refusing to fetch a local/private network address." |
| Admin console chain (cookies → main → premium → main → close) | ✅ 5 presses, no errors |

Post-restart log: **0 ERROR / 0 CRITICAL**. Test messages removed afterwards
(delivered media kept) via a one-off Telethon cleanup.

## Subsystem audit — all healthy

- `tgbot.service` active (PID 91208 since 14:55:37 UTC); clean startup log:
  2 pyrogram sessions, `[POT] Provider is healthy on 127.0.0.1:4417`,
  FriendMedia watcher, Bale polling, cookie-refresh scheduled, SysMon spawned,
  DirectForward supervisor (X + TikTok started; IG correctly skipped because
  `IG_AUTH_ENABLED=false`), updater "yt-dlp already up to date".
- `tgbot-xchat-bridge` / `cookie-watch` / `nginx` / `fail2ban` active,
  `NRestarts=0` each. `tgbot-monitor.service` installed-but-disabled by design
  (the bot spawns the Go monitor: PID 91347, `poll=15s report every 900s`,
  channel set; reports continue past the old 240-sample cap and ≥80 % warnings
  fire).
- Bridge cursor consistency: `cache/xchat_bridge_state.json.last_seq` ==
  `state.x.last_id` (2102979638850920448); the newest
  `cache/xchat_inbox.jsonl` line (687 lines) matches
  `x.cursors["1743868576920928256:2095053127040876548"]` (2110557479323029504).
- PO provider binds **127.0.0.1 only** (`ss`: `LISTEN 127.0.0.1:4417`).
- Bot HTTP (streaming endpoints) listening on `0.0.0.0:8080` (bot PID).
- Bot API `getMe` → `ok=True`, `AngelaBalzac_bot` (external connectivity fine).
- `kurigram 2.2.26`, `yt-dlp 2026.10.10.233141` (nightly updater reports
  current).
- Disk 38 % (`/`), RAM 2.6/3.8 GiB with 8 GiB swap mostly unused, load ~0.3.
- Cookie jars all `0444` (bot writes via snapshots): yt 14:57, tt 15:05
  (write-back working), x 04:46, IG Oct 8 and intentionally idle (paused).
  600 perms on `.env` and `telethon_session.txt`.
- certbot.timer active (last run today); the `tgbot-copy.sh` deploy hook copies
  the renewed lineage into `certs/` (current `certs/fullchain.pem` expires
  **2026-12-04**, i.e. renewed before expiry). `/etc/letsencrypt` is root-only,
  so nginx's lineage expiry could not be read as `dev` — managed by the active
  timer.
- `database.json`: authorized 0 / blacklisted 0 / document_mode 0 /
  premium_users 0 / sub_settings 5 / usage 3 (creator is implicitly
  authorized). `git ls-files | grep -E '\.(env|session)$|cookies/'` → empty
  (no secret is tracked).
- Log inventory Sep 23 – Oct 16: 149 ERROR lines total; sources are
  `aiogram.dispatcher` (79, Bale transient network), `direct_forward.instagram`
  (55, the paused dead session), `queue_manager` (8) +
  `direct_forward.twitter` (3) — the 2026-10-09 filename bug —
  `direct_forward.tiktok` (4, WS reconnects + this PHOTO_EXT_INVALID).
  Nothing else unexplained.

## Operator follow-ups

- Nothing required: both fixes apply to all future relays/downloads.
- The TikTok photo-mode post now delivers its **soundtrack** (the image is
  exposed by yt-dlp only as the thumbnail). A *native TikTok photo-post path*
  (deliver the carousel images) is a feature, not this bug fix — worth an
  explicit ask if the operator wants photo-mode posts archived as images.
- IG remains paused pending a fresh `igcookies.txt` + Admin → 🔐 IG Auth.

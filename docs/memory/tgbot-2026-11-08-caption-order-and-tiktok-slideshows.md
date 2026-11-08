# 2026-11-08 — Caption order (A→B), TikTok slideshows + soundtrack, direct-link captions

Operator brief: (1) check the logs after a little use; (2) the relay put the
bot's info first and the post caption second — they want **post caption A,
relay info B**, on every platform; (3) test it on TikTok, Instagram and X;
(4) why no subagents; (5) TikTok slideshow/photo posts must deliver the pictures
**and** the background audio as an extra reply; (6) keep the rich-text long
caption behaviour, for self-DMs **and** direct links.

## Log check (2026-11-07 19:00 → 2026-11-08 02:50)

- No new `ERROR`/`CRITICAL` lines and no new tracebacks. The only tracebacks in
  the log are the 2026-11-07 18:14 FriendMedia `ReadTimeout` block from before
  the `request_timeout = 30` fix.
- IG self-DM relayed reels/posts steadily (00:56–01:56); X relayed tweets and
  photo-only tweets; three X `extract failed` warnings correctly fell back to
  the share's photos. Upload pacing warnings are Telegram's normal throttle.

## Caption order — A then B everywhere

- `direct_forward/common._compose_caption`: `body + "\n\n" + header`; on
  overflow the media keeps the caption head and the tail + header follow as one
  rich/plain message.
- `utils/rich_media.build_html`: body paragraphs first, header paragraph last.
- Direct links: new `_link_caption_parts` (downloader_handler) = description A,
  `🎥 title / 👤 @uploader / 🔗 url` B; `_instagram_post_caption` now returns
  `(header_lines, body)` instead of a pre-ordered/truncated string.
- Tests assert A before B in the plain caption and in the rich HTML.

## TikTok photo mode (slides + background audio)

- yt-dlp sees only the soundtrack of a photo post (`audio`/m4a), and
  `/@user/photo/<id>` is not in any `_VALID_URL`.
- New `utils/downloader/tiktok_photo.py` parses the embed page's
  `__FRONTITY_CONNECT_STATE__` JSON: `itemInfos.text` (caption),
  `imagePostInfo.displayImages[].urlList` (original 1080×1920 slides),
  `musicInfos.playUrl/musicName/authorName/coversLarger`. One page fetch,
  300 s TTL cache, one retry on 429.
- `direct_forward.common.deliver_tiktok_photo`: all slides + caption (rich
  collage when >1024), THEN the audio as its own `send_audio` message with
  title/performer/cover.
- Self-DM `_tt_relay_share` uses it automatically; direct links are detected in
  `show_format_selection` (shortlink resolved *without* the embed rewrite) and
  skip the format keyboard. `normalize_url` rewrites `/photo/` → `/embed/`.

## Direct-link captions (all platforms)

`queued_transfer_job` and `playlist_job` now pass an explicit `caption=` built
from `result['description']` (A) + title/uploader/URL (B), and call
`_try_rich_link` when the text exceeds 1024 chars; the tail goes through
`_send_followups`. The filename (`clean_name`) is no longer used as the title.
IG photo posts use the same ordering and rich album path.

## Live verification (Telethon as the operator; test messages deleted)

| Test | Result |
|---|---|
| TikTok photo `@mens.guidance/photo/7467226363544554770` | 6 slides, caption `#founderstory #datingapps` then `🔗 …`, then a 972 KB audio message (`you not the same — tilekid`) |
| TikTok video `@pwr_films/video/7703597136830680342` | 1080p video; caption block = full post text, then `🎥 … / 👤 @pwr_films / 🔗 …` |
| IG reel `DVIp5xvkc1u` | 16.3 MB 1280p video; caption = post text then title/uploader/URL |
| X tweet `2119102475731075192` | 7.3 MB video; caption = tweet text then title/uploader/URL |
| IG reel `DfKhaK_v6M1` (1264-char caption) | **rich message**: PageBlockVideo first, caption paragraphs, relay header as the last paragraph |

Rich API itself was verified live on 2026-11-07 (single video, collage,
text-only → `ok:true`).

## Subagents

One `explore` subagent mapped the interactive download/caption flow while the
TikTok photo-post JSON was being reverse-engineered — independent work, run in
parallel. Most tasks here are sequential (code → deploy → drive), so a subagent
is used only where the work is genuinely separable.

## Tests / files

- `tests/test_relay_captions.py` 11/11 (A→B ordering, rich HTML order, photo
  delivery sends slides then audio, photo URL detection, IG quality picks).
- Full suite green; `py_compile` on every tracked Python file.
- Deployed commit: see `git log` (this note ships with it), `NRestarts`
  incremented by the deploy restart only.

## Correction (same day, after operator feedback)

The first implementation put the long caption **inside** a rich message
(`sendRichMessage` with inline media). Two problems surfaced:

1. **Media-tab loss** — Telegram does not list a rich message's inline media in
   the chat's Media tab, so the operator's media history lost those posts.
2. **Upload cap** — `sendRichMessage` media uploads are capped far below normal
   media. A 73 MB TikTok video (`7705002907258719509`, 2338-char caption) was
   rejected with `Request Entity Too Large`, and the code silently fell back to
   the old split caption. Log proof:
   `[RichMedia] sendRichMessage rejected: Request Entity Too Large` (04:31:41).

Final design (all relays + direct links):
- ≤1024 chars → one normal media message, caption `A (post text) + B (header)`.
- >1024 chars → the media is a **normal media message** (Media tab) whose
  caption is only **B**; the full `A + B` follows as **one rich TEXT reply**
  (`sendRichMessage`, no media upload) attached to that media message.
- Text-only tweets use the same rich text path.

Plumbing: `process_split_and_upload` now returns the first sent `Message`;
every caller passes its id to `_send_followups(..., reply_to_message_id=...)`.
`utils/rich_media.py` is text-only (inline-media helpers removed on purpose —
do not reintroduce them; see AGENTS #22).

Verified live:
- slide link `vt.tiktok.com/ZSpBhe7ff/` (2 slides, 3226-char caption): media
  group with B caption, then a rich reply (reply_to = first slide) whose first
  paragraph is the caption and last paragraph is the header;
- the 73 MB / 2338-char video: normal 75 MB video with B caption + rich reply
  carrying the full caption;
- short/no-caption slide (`ZSpBrvAvG`): media with B caption only, no reply.

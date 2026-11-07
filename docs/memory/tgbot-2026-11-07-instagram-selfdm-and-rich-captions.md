# 2026-11-07 — Instagram self-DM relay + post captions on every DM relay

Operator brief: (1) they uploaded fresh Instagram cookies and want **self-DM
relay on Instagram like TikTok/X** — they sent the text `my self dm in
instgram` to their own Instagram thread as a marker; (2) every DM relay on
Instagram/TikTok/X must now carry the **post's own text** with the media, and
if the text is too long for a Telegram caption, use Telegram rich text so the
media stays on top with the long text below; (3) media must always be the
highest quality available.

## Snapshot / rollback

- Baseline commit before this work: `eb2246e` (TikTok backlog session).
  `IG_AUTH_ENABLED` was `false` (account paused).
- Deployed after: commits `…` (see `git log`), `IG_AUTH_ENABLED=true` written to
  `.env` with `dotenv.set_key`, bot restarted.
- Rollback: `IG_AUTH_ENABLED=false` + restart pauses every authenticated IG
  feature without touching code.

## Instagram self-DM — what was learned

- The jar (`cookies/instagram/igcookies.txt`, uploaded 17:42 UTC) logged in
  with `login_by_sessionid` (no password path, by design).
- **The self thread id is NOT `{uid}_{uid}`.** It is an opaque 39-digit number
  (`340282366841710301244260149421211047426` on this account). The self thread
  is the only 1:1 thread whose `users` list is **empty** (Instagram models
  "Message yourself" with no other participant). The worker therefore adopts
  the id from the inbox on first sighting and persists it.
- The thread cursor is separate (`state["ig"]["self_last_id"]`) from the
  paired-contact global cursor — mixing them would skip messages on either
  side. First sighting primes to the newest item so the whole history is never
  dumped; the marker item was logged at prime time
  (`latest: 'text' 'my self dm in instgram'`).
- Self-DM is the one place `is_sent_by_viewer` items are processed (they are
  the payload); reactions are skipped there.
- When the configured pair resolves to the account's own uid, it is ignored:
  pairing with yourself would make every 1:1 thread look "paired" and burn
  private-API calls without relaying anything. `IG_DIRECT_FROM_USERNAME`
  (=`saleh.momtaz`) thus selects self-DM mode.
- Share shapes seen in the real thread: `xma_clip` (resolves to
  `instagram.com/reel/<code>`), `generic_xma` (no resolvable link in that
  sample; unknown/profile share), and plain text. `_ig_resolve_raw` now also
  resolves `instagram://...?id=<pk>` deep links to the native pk path.

## Captions on every relay

- `download_media` now returns `description` (yt-dlp's full caption).
  `_download_and_deliver` merges it into the body (deduped when the share body
  already equals it), so TikTok/IG/X links carry the post text.
- IG native delivery already used `media.caption_text`.
- Telegram media captions cap at 1024 chars. Long text is sent as **one Bot API
  10.2 rich message** (`sendRichMessage`): media block first
  (`<figure><video src="tg://video?id=m0">` / `<img src="tg://photo?id=m0">`),
  then the text (up to ~32k). New `utils/rich_media.py`
  (`send_rich_media`, `send_rich_album` collage, `send_rich_text`), wrapped by
  `direct_forward.common._try_rich_media` and used by:
  `_download_and_deliver`, `_ig_native_deliver_once` (single + album),
  `_ig_process_message` attachments, `_x_deliver_tweet`,
  `_x_deliver_share_photos`, `_x_deliver_dm_attachment`.
- Best-effort: a rich rejection falls back to the old 1024-char caption +
  follow-up text. Photo cap 10 MB, video cap 2 GB (Bot API); the premium 4 GB
  MTProto path keeps the classic send + rich-text follow-up.

## Highest quality

- yt-dlp: `bestvideo+bestaudio/best` (verified IG reel: 15.29 MiB, 34 s).
- instagrapi: `extract_media_v1`/`extract_resource_v1` already sort
  `video_versions` and `image_versions2` candidates by area and take the
  LARGEST — `thumbnail_url` is a misnomer, not a low-res thumb. Do not
  "fix" this.
- IG DM video attachments: `_ig_resolve_raw` also picks the largest
  `video_versions` entry explicitly.
- X: `videos[0]` from `extract_formats`; DM media fetched from the original
  CDN URL.

## Live verification

- Session: `[DirectForward/IG] Logged in via sessionid from igcookies.txt.`
- Self-DM: thread id adopted + primed, marker text confirmed in the log.
- **End-to-end (2026-11-07 18:43):** a reel link was sent into the self-DM from
  the persisted session; the worker logged
  `gap fetch: … 1 new after cursor …`, resolved
  `item …: xma_clip -> https://www.instagram.com/reel/DVIp5xvkc1u/ (author
  @_m.mouse_28)`, delivered it (`✅ relayed … (video)`), and advanced
  `self_last_id`. The delivered Telegram caption carried the full post text
  (`Part 3 der jungle tour🦜… #sydney`), proving the caption merge.
- Rich API: live `sendRichMessage` calls (single video, 2-photo collage,
  text-only) all returned `ok:true`; test messages were deleted afterwards.
- IG reel download: 15.29 MiB, caption + thumbnail present.
- Found and fixed while testing: instagrapi's default `request_timeout` is
  **1 second**, which aborted CDN media reads (`ReadTimeout` tracebacks from
  FriendMedia archives). Both IG clients now set `request_timeout = 30`.
- Tests: `tests/test_relay_captions.py` (8/8), full suite green, `py_compile`.
- **Final acceptance:** the operator sends a reel/post/link to the Instagram
  self-DM; the worker should log `[DirectForward/IG] item … -> …` and deliver
  it with the caption. Self-DM polling is jittered ~300 s ±40 %.

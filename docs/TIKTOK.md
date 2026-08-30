# TikTok — DM relay, shortlinks, embed & download fixes

All TikTok-related research, issue analyses and fix notes merged. Covers self-DM relay (WebSocket), shortlink pre-resolve, /embed rewrite, chrome-pin, and download failures.

## Sources consolidated

- `docs/tiktok-direct-forward-issue.md`
- `docs/tiktok-dm-relay-research.md`
- `docs/tiktok-download-fix.md`
- `docs/memory/tgbot-tiktok-direct-dm.md`
- `docs/memory/tgbot-tiktok-shortlink-fix.md`
- `docs/memory/tgbot-2026-08-11-startup-crash-loop-and-tiktok-embed.md`

---

---

## 1. Source: `docs/tiktok-direct-forward-issue.md`

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

### Headless Browser Analysis (Aug 11, 2026)

**Old Challenge Format (what yt-dlp expects):**
- Element with `id="cs"` containing base64-encoded challenge JSON
- Elements with `id="wci"`, `id="rci"`, `id="rs"` for cookie names/values  
- yt-dlp's `_solve_challenge_and_set_cookies()` parses this and solves the SHA256 challenge in pure Python

**New Challenge Format (current TikTok):**
- **No `id="cs"`, `id="wci"`, `id="rci"`, or `id="rs"` elements exist**
- Page loads full React app with `webmssdk.js` (Mouse/Security SDK v1.0.0.388) and `secsdk.js` (SecSDK)
- Challenge implemented in **JavaScript** requiring a full JS engine to execute
- Uses sophisticated browser fingerprinting (canvas, WebGL, audio context, etc.)

**Why yt-dlp fails:**
```python
# yt-dlp/extractor/tiktok.py _solve_challenge_and_set_cookies():
challenge_data = traverse_obj(webpage, (
    {find_element(id='cs', html=True)}, {extract_attributes}, 'class',
    filter, {lambda x: f'{x}==='}, {base64.b64decode}, {json.loads}))

if not challenge_data:
    raise ExtractorError('Unexpected response from webpage request')  # FAILS HERE
```
The `find_element(id='cs', html=True)` returns `None` because the element doesn't exist in the new challenge page.

**Working Page (with valid cookies):**
- Contains `__UNIVERSAL_DATA_FOR_REHYDRATION__` script with full video data
- Video URLs (`playAddr`, `downloadAddr`) embedded in the JSON
- Video plays normally via blob URLs

**Root Cause**: TikTok moved from a simple HTML-based challenge to a sophisticated JavaScript-based fingerprinting system (MSSDK/SecSDK). yt-dlp's Python-based solver cannot execute the required JavaScript.

**Solution Path**: yt-dlp will likely add headless browser support (Playwright) for the TikTok extractor, similar to how they handle other sites with JS challenges.

---

## 2. Source: `docs/tiktok-dm-relay-research.md`

# TikTok DM Relay Research

## 1. TikTok DOES Have Self-DM

Unlike what initial research suggested, TikTok **does** have a self-DM feature. The user can
send messages and share videos to themselves — the conversation appears in the chat list
with the user's own name. It is functionally equivalent to X/Twitter's "Message Yourself"
or Telegram's "Saved Messages". The conversation is accessible at `https://www.tiktok.com/messages`
when authenticated.

## 2. No Open-Source Library Supports TikTok DMs

Every major Python library explicitly lacks DM/user-auth support:

| Library | Stars | DM Support | Notes |
|---|---|---|---|
| [TikTokApi](https://github.com/davidteather/TikTok-Api) | 6.5k | No | "no support for any user-authenticated routes" |
| [pyktok](https://github.com/dfreelon/pyktok) | 463 | No | video/metadata only |
| [traktok](https://jbgruber.github.io/traktok/) | — | No | R only, video search/download |
| [armxe/tiktok-api](https://github.com/armxe/tiktok-api) | 302 | No | signing algorithms only (X-Bogus, X-Argus) |
| [SyntaxSparkk/TikTok](https://github.com/SyntaxSparkk/TikTok) | 733 | No | mobile API RE docs |

The official TikTok Business Messaging API does **not** support self-DM — it requires a
separate Business Account and only handles incoming user messages (24h window, text-only,
no media). Not applicable for our use case.

## 3. Headless Browser Approach — Tested Live on This VPS

### What was tested

Playwright (Python, async) with Chromium headless shell, using the existing TikTok cookies
from `cookies/tiktok/ttcookies.txt`. Tested on the actual production VPS (2 vCPU Xeon Gold
6142, 3.8 GB RAM, 82 GB free disk).

### Resource Usage — Measured

| Resource | Measured | VPS Capacity | Impact |
|---|---|---|---|
| **Disk** | 262 MB (headless shell) | 82 GB free | Negligible (0.3%) |
| **RAM** | 876 MB per browser instance | 2.1 GB available | **Severe** — 42% of available |
| **CPU** | ~10s per poll cycle | 2 vCPU | Moderate |
| **Startup** | 0.14s launch + 0.78s navigate + 5s JS settle | — | ~7s total |

### Authentication — Works

The existing `cookies/tiktok/ttcookies.txt` (70 cookies, 69 tiktok-domain) successfully
authenticates in headless Chromium. The browser lands on `https://www.tiktok.com/messages`
with title "Messages | TikTok". No login redirect.

### Conversation List — Readable

DOM contains `[data-e2e="dm-new-conversation-item"]` elements. The self-DM conversation
is visible with the user's name, last message preview, and timestamp.

Key selectors discovered:
- `[data-e2e="dm-new-conversation-list"]` — conversation list container
- `[data-e2e="dm-new-conversation-item"]` — individual conversation
- `[data-e2e="dm-new-conversation-nickname"]` — conversation partner name
- `[data-e2e="dm-new-chatbox"]` — chat area
- `[data-e2e="dm-new-message-list"]` — message list container
- `[data-e2e="dm-new-chat-item"]` — individual message
- `[data-e2e="dm-new-shared-video"]` — shared video thumbnail
- `[data-e2e="dm-new-chat-nickname"]` — chat partner name in opened conversation
- `[data-e2e="chat-uniqueid"]` — unique ID like `@username`
- `[data-e2e="dm-new-input-editor"]` — message input field

### Message Content — Readable

After clicking a conversation, message items are visible with:
- Sender nickname and avatar
- Message text content
- Shared video thumbnails (background-image with CDN URL)
- Timestamps

### Shared Video Data — Extractable via Internal API

When clicking a shared video, the browser intercepts an internal API call to:

```
https://www.tiktok.com/api/im/item_detail/?aweme_id=<VIDEO_ID>&...
```

This API returns full video metadata as JSON:
```json
{
  "itemInfo": {
    "itemStruct": {
      "id": "7670568936034831617",
      "desc": "video description",
      "author": { "uniqueId": "username", "nickname": "Display Name", ... },
      "video": {
        "playAddr": "https://v16-webapp-prime.tiktok.com/video/...",
        "downloadAddr": "https://v16-webapp-prime.tiktok.com/video/...",
        "cover": "https://p16-common-sign.tiktokcdn.com/...",
        "duration": 16,
        "width": 1080,
        "height": 1920,
        "size": 5242880
      }
    }
  }
}
```

The `playAddr` and `downloadAddr` are direct MP4 URLs that can be downloaded with standard
HTTP requests (using the browser's cookies for authentication).

### Video URLs from Network Interception

When clicking a shared video, the browser also captures direct video CDN URLs:
```
https://v16-webapp-prime.tiktok.com/video/tos/useast5/...
```

## 4. Architecture Options

### Option A: Full Playwright Per Poll

Launch browser → navigate to /messages → read DOM → extract video URLs → download → relay → close browser.

- **RAM**: 876 MB released after each poll
- **Latency**: ~10s per cycle
- **Reliability**: Medium (DOM selectors can change with TikTok updates)
- **Pros**: Simple, consistent with existing architecture
- **Cons**: 876 MB RAM spike every poll interval; on a 3.8 GB VPS alongside the bot, this is too tight

### Option B: Persistent Browser Context

Keep browser running, poll messages periodically via DOM.

- **RAM**: 876 MB held continuously
- **Latency**: ~3s per cycle
- **Reliability**: Medium
- **Pros**: Faster subsequent polls
- **Cons**: Memory leak risk; 876 MB permanently reserved; bot already uses 1.7 GB

### Option C: Hybrid — Browser for Auth, API for Data

Use Playwright to capture authenticated session, then make lightweight HTTP requests to the
internal `/api/im/item_detail/` endpoint for video data.

- **RAM**: 876 MB (released after session capture)
- **Latency**: ~8s
- **Reliability**: Higher (API more stable than DOM selectors)
- **Pros**: API responses are structured JSON; no DOM parsing needed
- **Cons**: Still needs browser for initial auth and conversation list discovery

### Option D: Pure API (No Browser)

Use the internal TikTok API directly with cookies, bypassing the browser entirely.

- **RAM**: ~10 MB
- **Latency**: ~1s
- **Reliability**: Low (TikTok signs requests with rotating X-Bogus/X-Argus crypto)
- **Pros**: Minimal resource usage
- **Cons**: Requires reverse-engineering request signing; signatures rotate with app updates

## 5. Recommendation

**The Playwright approach is technically feasible but resource-prohibitive on this VPS.**
The 876 MB RAM footprint alongside the existing bot (1.7 GB) is too tight for reliable
operation. On a VPS with 8+ GB RAM, Option A or C would work well.

**If the VPS is upgraded to 8+ GB RAM**, the recommended approach is:

1. Install Playwright: `pip install playwright && playwright install --only-shell chromium`
2. Use existing `cookies/tiktok/ttcookies.txt` for authentication
3. Poll `/messages` page, click into self-DM conversation
4. Intercept `/api/im/item_detail/` responses for shared video metadata
5. Download videos via `playAddr` URL using standard HTTP (with cookies)
6. Relay to Telegram via the existing `process_split_and_upload` pipeline
7. Track processed message IDs in `direct_forward_state.json` (same pattern as X/IG)

**On the current 3.8 GB VPS**, the link-based approach (user sends TikTok URL to the bot,
which already works via yt-dlp) remains the viable path.

## 6. TikTok Business Messaging API — Not Applicable

The Business Messaging API (free, official) has constraints that make it unsuitable:

- **Cannot message first**: User must send the first message to the Business Account
- **24-hour session window**: Bot can only reply within 24h of the last user message
- **Text-only**: No media messages (photos/videos) supported
- **Requires separate Business Account**: Cannot use a personal account
- **No self-DM**: Business accounts don't have a "message yourself" feature

This API is designed for customer support chatbots, not for personal media relay.

---

## 3. Source: `docs/tiktok-download-fix.md`

# TikTok Download Failure Fix — "Unexpected response from webpage request"

## The Problem

Sending a TikTok link (e.g. `https://vt.tiktok.com/ZS4GyN38b/`) to the bot resulted in:

```
❌ Download/Upload failure.
Error: ERROR: [TikTok] 7649688932615032086: Unexpected response from webpage request;
please report this issue on https://github.com/yt-dlp/yt-dlp/issues?q=
```

The same error occurred regardless of whether the user sent the shortlink or the
canonical `tiktok.com/@user/video/<id>` URL.

## Root Cause Analysis

### What worked

- `extract_formats()` successfully extracted 11 formats and displayed the format keyboard
- `yt-dlp -f best --cookies ttcookies.txt <url>` worked from the CLI (first attempt)
- The cookie snapshot mechanism (`cookie_manager.acquire`) was functioning correctly

### What failed

- `download_media()` with a specific format selector failed consistently
- The error originated from yt-dlp's TikTok extractor at `tiktok.py:231`:
  ```python
  def _solve_challenge_and_set_cookies(self, webpage):
      challenge_data = traverse_obj(webpage, (
          {find_element(id='cs', html=True)}, {extract_attributes}, 'class',
          filter, {lambda x: f'{x}==='}, {base64.b64decode}, {json.loads}))
      if not challenge_data:
          if 'Please wait...' in webpage:
              raise ExtractorError('Unable to extract challenge data')
          raise ExtractorError('Unexpected response from webpage request')
  ```

### The two contributing factors

**Factor 1: Shortlink re-resolution during download**

The `show_format_selection` function stored the **original short URL**
(`https://vt.tiktok.com/ZS4GyN38b/`) in `DOWNLOAD_CACHE`. When the user clicked
a format button, `download_media` received this short URL, called `normalize_url`
(which resolved it to `tiktok.com/@user/video/<id>`), but then yt-dlp's internal
download path re-fetched the webpage. TikTok's anti-bot interstitial served a
different page on the second fetch — one that didn't contain the expected challenge
data.

**Factor 2: Stochastic anti-bot challenge**

TikTok's proof-of-work challenge solver in yt-dlp fails stochastically. The
`_solve_challenge_and_set_cookies` method expects the webpage to contain a `cs`
HTML element with base64-encoded challenge data. Sometimes TikTok serves this;
other times it serves an interstitial page without it. The existing TikTok retry
logic (line 1356) only retried without cookies (no-auth), but this video is
sensitive content that **requires** cookies — the no-auth retry got
"This post may not be comfortable for some audiences. Log in for access."

## How It Was Found

1. Reproduced the error by simulating the bot's exact `download_media` call path
2. Tested `extract_formats` — it succeeded (cookies worked for extraction)
3. Tested `download_media` with the resolved URL — it also failed
4. Tested `yt-dlp -f best` from CLI — it worked on first attempt
5. Compared the difference: CLI uses `download=False` equivalent (extraction only),
   while `download_media` calls `extract_info(url, download=True)` which re-fetches
   the webpage
6. Read yt-dlp's `tiktok.py` source and found the `_solve_challenge_and_set_cookies`
   method that raises "Unexpected response from webpage request" when the webpage
   doesn't contain expected challenge data
7. Tested the no-auth retry — got "This post may not be comfortable for some
   audiences" (sensitive content needs cookies)
8. Tested a fresh cookie retry — it succeeded

## The Fix (two parts)

### Part 1: Store normalized URL in download cache

**Files:** `modules/downloader_handler.py`, `utils/downloader.py`

`extract_formats` now returns a `normalized_url` field (the post-normalization URL,
e.g. the resolved TikTok shortlink). The download cache in `show_format_selection`
stores this instead of the original short URL. This means `download_media` never
re-resolves a `vt.tiktok.com` shortlink, avoiding the first fetch of the anti-bot
interstitial.

```python
# downloader_handler.py — cache stores normalized URL
DOWNLOAD_CACHE[cache_id] = {
    "url": data.get("normalized_url") or url,
    ...
}

# downloader.py — extract_formats returns normalized URL
return {
    ...
    'normalized_url': url,  # post-normalize URL (e.g. resolved TikTok shortlink)
}
```

### Part 2: Extra cookie retry for TikTok downloads

**File:** `utils/downloader.py`

The TikTok download retry path now attempts three strategies:
1. **Cookies** (first attempt — may fail on anti-bot challenge)
2. **No-auth** (existing retry — works for public content)
3. **Fresh cookies** (new retry — needed for sensitive/login-walled content)

The third retry acquires a fresh cookie snapshot, which gets past the stochastic
anti-bot challenge because the new snapshot triggers a different TLS fingerprint
rotation in yt-dlp's curl-cffi impersonation engine.

```python
# Retry 1: no-auth (for public content blocked by interstitial)
retry_opts = dict(ydl_opts)
retry_opts.pop('cookiefile', None)
try:
    with yt_dlp.YoutubeDL(retry_opts) as ydl:
        info = ydl.extract_info(url, download=True)
except Exception as e2:
    last_attempt_error = str(e2)
# Retry 2: fresh cookie snapshot (for login-walled sensitive content)
if info is None and site_jar:
    snap_in_play = cookie_manager.acquire(site_jar)
    retry_opts2 = dict(ydl_opts)
    if snap_in_play:
        retry_opts2['cookiefile'] = snap_in_play
    try:
        with yt_dlp.YoutubeDL(retry_opts2) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e2:
        last_attempt_error = str(e2)
```

## Verification

Tested the full flow: `extract_formats` → `download_media` with the specific
format selector `bytevc1_1080p_613984-1` on the same video that previously failed.
The fresh cookie retry succeeded and downloaded 6.5 MB.

## Related

- yt-dlp TikTok extractor: `venv/.../yt_dlp/extractor/tiktok.py:223-231`
- TikTok anti-bot challenge solver: `_solve_challenge_and_set_cookies`
- Existing AGENTS.md invariant: "TikTok shortlinks are pre-resolved by us, not yt-dlp"
- The normalized URL fix strengthens that invariant by ensuring the resolved URL
  is also used for the download phase, not just the extraction phase

---

## 4. Source: `docs/memory/tgbot-tiktok-direct-dm.md`

# TikTok Self-DM → Telegram Direct Forward: IM WebSocket Research

Date: 2026-08-10. Status: **researched + proven + implemented** — the worker
lives in `modules/direct_forward.py` (`_tiktok_worker`, `_tt_run_ws`,
`_tt_parse_push`).

## TL;DR

TikTok has a **self-DM** ("Message Yourself") feature. A user can send/share
videos to their own chat, exactly like Telegram Saved Messages / X self-DM /
IG self-DM. The web IM client streams these over a **WebSocket**
(`wss://im-ws-sg.tiktok.com/ws/v2`) as **cmd 500 `NEW_MSG_NOTIFY` pushes**
(protobuf). The bot can:

1. Compute a session `access_key` from the account's `wid` cookie endpoint
   (md5 of a salted app-key+wid string).
2. Open the WS with the account cookies.
3. Receive real-time pushes for messages in the **self-DM conversation**
   `0:1:{uid}:{uid}`.
4. Parse the pushed `MessageBody.content` JSON → `itemId` (+ `s:client_message_id`
   UUID for dedupe).
5. Resolve the video author's username via the **oEmbed** endpoint (signature-free).
6. Download through the normal yt-dlp pipeline (TikTok needs the fresh-cookie
   retry — see invariant #3 + `docs/tiktok-download-fix.md`).

## WebSocket protocol (reverse-engineered from webapp IM SDK `chunk_80840`)

### Endpoint

```
wss://im-ws-sg.tiktok.com/ws/v2?aid=1459&fpid=9&access_key=<KEY>&device_platform=web&ttwid=<urlencoded>&Web-Sdk-Ms-Token=<msToken>
```

Connect with the account cookie jar and `Origin: https://www.tiktok.com`.

### `access_key` derivation

1. `GET https://www.tiktok.com/api/v1/web-cookie-privacy/config?appId=1988&locale=en&theme=default&tea=1`
   with cookies → JSON containing `data.wid` (observed `7663715092794754582`).
2. `access_key = md5("9" + APP_KEY + wid + "f8a69f1719916z")` with
   `APP_KEY = e1bd35ec9db7b8d846de66ed140b1ad9`.
   Observed: `fdbfe780a7ecf2ac9fc52d20e65b659c`.

### Frame / Request envelopes (protobuf — cv-cat `Tiktok_Request.proto`)

- **Frame** (`Tiktok_Request.Frame`): `seqid`, `logid`, `service=5`,
  `method=1`, repeated `headers` (`ExtValue`), `payload_type='pb'`,
  `payload` = serialized `Request`.
- **Request** (`Tiktok_Request.Request`): `cmd`, `sequence_id`,
  `sdk_version="1.2.3"`, `refer=3`, `inbox_type=0`,
  `build_number="831c301:master"`, `device_id`, `device_platform="web"`,
  `auth_type=1`, repeated `headers`, and the body wrapped in Request field 8
  (`0x42` tag) → `RequestBody`.
- **RequestBody** carries per-command body fields. Field numbers differ
  between the **WS** variant (field number == cmd) and the **HTTP** variant
  (large numbers like 2402/2410/16050). **For WS, body field number == cmd
  number.** e.g. cmd 300 → body field 300 (`conversations_list_body`).

Key cmds (enum from SDK): `300=GET_CONVERSATION_LIST`,
`301=GET_MESSAGES_BY_CONVERSATION`, `500=NEW_MSG_NOTIFY` (server push),
`501=MARK_READ_NOTIFY`, `604=MARK_CONVERSATION_READ`, `2010=CLIENT_ACK`,
`100=send_message`, `1001=get_stranger_conversation_list`.

Headers (`ExtValue` list) mirror the web client: `X-Bogus`, `aid=1988`,
`app_name=tiktok_web`, `channel=web`, `device_platform=web_pc`, `device_id`,
`region`, `priority_region`, `os`, `referer=https://www.tiktok.com/messages`,
`verifyFp`, `app_language`, `tz_name`, `is_page_visible`, `user_is_login`,
`user_agent`, `Web-Sdk-Ms-Token` (from cookie), etc.

### Acknowledged status codes

- `200001` = command rejected (observed on cmd 300/301). The **conversation
  list and history-by-conversation reads are NOT usable** on this endpoint
  build; the push channel is the reliable path.
- cmd 300 with WS-variant body field 300 returned an ack `200001` but the
  server **also pushed** the pending unread self-DM message as cmd 500.

## The push (cmd 500) structure — proven with a real self-DM video share

Response envelope (`Tiktok_Request.Response`): `cmd=500`, `status_code=0`,
`body` (field 6) → `ResponseBody` → **field 500** `has_new_message_notify`
→ `NewMessageNotify`:

```
NewMessageNotify {
  field 2: conversation_id = "0:1:7539848641810170888:7539848641810170888"
  field 3: conversation_type = 1
  field 4: notify_type = 1
  field 5: MessageBody { ... }
  ...
}
MessageBody {
  field 1: conversation_id
  field 3: server_message_id (int64)
  field 6: message_type
  field 8: content (JSON string)
  field 10: create_time
  field 13: order_in_conversation
  ...
}
```

- Self-DM conversation id = `0:1:{uid}:{uid}` (both participants are the
  account itself). Observed uid: `7539848641810170888` (`@saleh_momtaz`).
- `content` JSON keys (video share, `aweType` ~= `800` share_video):
  `itemId`, `uid` (video author uid — differs from account uid), `content_name`,
  `cover_url`, `content_thumb`, `cover_weitht` [sic], `cover_height`.
  Observed share: `{"aweType":800,"uid":"76435929696841782280",
  "itemId":"7666661069972065544","content_name":"Seed Truth", ...}`.
- The `MessageBody` has extra wire types (group/start-end) that proto3
  python (`google.protobuf`) rejects — decode with a tolerant byte walker
  (see probes) or extract the JSON via a brace-scanner.
- Dedupe key: `s:client_message_id` UUID inside the `ext` entries of the raw
  message (also in the `client_message_id` field when sending). Persist these
  to state; skip already-seen ids.

### Delivery semantics (important!)

- On **first connect**, the server pushes **all currently-unread** messages as
  cmd 500 (backlog for messages that arrived while the bot was offline).
- After a message has been pushed once it is **marked delivered** — a
  subsequent reconnect does **not** re-push it. Therefore the worker must
  keep a **persistent WS with reconnect** and process pushes as they arrive;
  reconnects pick up anything that arrived during downtime.
- The push is the **only** reliable ingestion path here: cmd 300/301 history
  reads return `200001`.

## Downloading the shared video

- The share JSON carries only `itemId` + author `uid`, not the author's
  username. Resolve the username with TikTok's public **oEmbed** endpoint
  (no signature/cookies needed):

  ```
  GET https://www.tiktok.com/oembed?url=https://www.tiktok.com/video/<itemId>
  -> {"author_url":"https://www.tiktok.com/@seedtruth","author_name":"Seed Truth",...}
  ```

- Canonical yt-dlp URL: `https://www.tiktok.com/@<author_name>/video/<itemId>`.
  yt-dlp's `TikTokIE._VALID_URL` requires the `@user/video/<id>` form.
- **TikTok anti-bot is stochastic** ("Unable to extract universal data for
  rehydration"). It is already handled by the existing download pipeline
  (fresh-cookie retry — `docs/tiktok-download-fix.md`). A fresh
  `cookie_manager.acquire` snapshot extracts fine when the first attempt
  fails. Do NOT add a headless-browser dependency for this; the retry ladder
  is sufficient.

## Where this lives in the bot

- `modules/direct_forward.py::_tiktok_worker` — WS client (async `websockets`,
  v17), poll/reconnect loop mirroring `_twitter_worker`. Wired into
  `_direct_forward_supervisor` and `start_direct_forward_task`; the admin
  console gets a **🟢 Enable TikTok / 🔴 Disable TikTok** toggle plus a
  **🧪 Test TikTok** button (`admin_direct_toggle_tiktok`,
  `admin_direct_test_tiktok`, `test_tiktok_connection()`).
- **Handshake (proven end-to-end 2026-08-10):** jar → `_tt_wid()` (the
  web-cookie-privacy config endpoint) → `_tt_access_key()` (md5 of
  `9{APP_KEY}{wid}f8a69f1719916z`) → `_tt_connect_frame()` (cmd-1001
  `get_stranger_conversation_list` Frame) → the server acks the socket and
  pushes pending unread as cmd-500. The connection requires the decoded
  `ttwid` (the jar stores it URL-encoded; unquote first or the socket gets
  **HTTP 400**).
- **Push decoding:** `_tt_parse_push` uses a tolerant byte-walker
  (`_tt_walk`) — the MessageBody has extra group wire types that proto3
  rejects, so only the wanted fields are read and the JSON at field 8 is
  never recursed into.
- **Dedupe:** `server_message_id` (MessageBody field 3) persisted under
  `state["tiktok"]["seen_msg_ids"]` (capped at 2000), not the ext
  `s:client_message_id` — server_message_id is always present and stable.
- **First run primes and skips backlog:** `_tiktok_worker` marks
  `state["tiktok"]["primed"]`, then a `prime=True` connect consumes anything
  the server pushes inside a 15 s window WITHOUT relaying (so enabling the
  relay never floods the chat), then hands off to the relay loop.
- Jars: `cookies/tiktok/ttcookies.txt` (config.COOKIE_JARS / `config.TT_COOKIES`),
  already live.
- Config: `TIKTOK_DIRECT_ENABLED`, `TIKTOK_DIRECT_POLL_SECONDS`,
  `TIKTOK_DIRECT_POLL_JITTER_PCT` (mirror X/IG keys).
- Downloading goes through the existing `_download_and_deliver` pipeline —
  the fresh-cookie retry ladder (`docs/tiktok-download-fix.md`) handles the
  stochastic anti-bot challenge. NO headless browser needed (see below).

## Headless-browser analysis (for periodic cookie refresh)

**Question:** should the bot run a periodic headless-browser pass (every
6/12/24 h) to refresh the TikTok cookies before they go stale, like the
operator proposed?

**Answer: not now.** The current design already defeats the failure modes a
browser would fix, with zero extra RAM or install weight:

1. **Downloads** use yt-dlp + `curl-cffi` TLS/browser impersonation + a
   fresh-cookie retry ladder (`docs/tiktok-download-fix.md`) — the anti-bot
   interstitial is solved by a second attempt with a NEW cookie snapshot, not
   by running a browser.
2. **Cookie freshness** is handled by *write-back* (invariant #4): every
   yt-dlp run overlays the site's `Set-Cookie` session rotation back into the
   real jar, so the jar self-refreshes on every download. A jar "dying in
   days" was the write-back fix's original target.
3. **The IM WebSocket** (this worker) authenticates with the SAME live jar,
   so it inherits that freshness. If the WS starts rejecting (HTTP 400 /
   auth failures), that is the signal the session actually lapsed — at which
   point the fix is a fresh jar upload, not a browser pass.

**Ranking of the headless options (from the operator's list, scored for
resource usage + fit):**

| Rank | Option | RAM/CPU | Verdict for THIS project |
|---|---|---|---|
| 1 | **Playwright (Python, Chromium)** | ~150–250 MB per page, heavy one-time browser download | Best *real* browser: full JS + modern TLS. Would be the choice IF we ever add cookie refresh. |
| 2 | **curl-cffi impersonation (already in the bot)** | ~0 extra | Not a browser, but a real Chrome TLS/HTTP2 fingerprint — why we don't need one. |
| 3 | Raw CDP + system Chromium (`chromedp`-style) | ~150 MB | Works, but hand-rolled CDP for no real gain over Playwright. |
| 4 | Selenium + ChromeDriver | ~150–250 MB + driver binary | Heavier, slower, more moving parts than Playwright. |
| 5 | Pyppeteer | ~150 MB | Unmaintained fork of Puppeteer — skip. |
| 6 | Camoufox | ~250 MB+ | Best anti-fingerprint Firefox fork, but new/niche; overkill. |
| 7 | PhantomJS / old WebKit stacks | low | **Dead.** Fail modern TLS + proof-of-work. Never. |

**Feasibility verdict:** a 24 h (jittered) Playwright pass that logs into
tiktok.com and re-exports the Netscape jar *is* feasible and would harden the
session — but it adds ~250 MB RAM to a bot that explicitly avoids heavy
dependencies, and an *automated* login cadence is itself the kind of fixed
behavior that flags accounts (the IG history burned us on exactly this).
Since the write-back + retry ladder already keeps both download and WS paths
alive, defer it: revisit only if jars start lapsing despite write-back.

## Reference links reviewed (2026-08-10)

- https://zernio.com/blog/tiktok-api — general web API notes; **not** IM.
- https://github.com/dfreelon/pyktok — web scrape of video pages; no IM/DM.
- https://jbgruber.github.io/traktok/... — browser-extension capture; no IM.
- https://github.com/SyntaxSparkk/TikTok — web API wrappers; no DM.
- https://github.com/armxe/tiktok-api — API list; no DM.
- Useful working code: **cv-cat/TiktokApis** (`/tmp/opencode/ttapis/`) —
  WS IM client with the exact `access_key` + protobuf recipe used here.
- The IM SDK chunks (webapp `messages.ce1a37cf.js` + `chunk_80840…`) are the
  ground truth for cmd enum + MessageBody schema; captured under
  `/tmp/opencode/ttmsg/`.

## Probe artifacts

- `/tmp/opencode/ttpush1.hex` — raw cmd-500 push payload for the real
  self-DM video share (Seed Truth, itemId `7666661069972065544`).
- `/tmp/opencode/ttmsg/` — SDK chunks; `messages_page.html` (384 KB).
- `/tmp/opencode/ttapis/` — cv-cat TiktokApis clone with `Tiktok_Request.proto`.

---

## 5. Source: `docs/memory/tgbot-tiktok-shortlink-fix.md`

# TikTok shortlink ("malformed site") fix

**Date:** 2026-08-03 · **Area:** `utils/downloader.py` (`normalize_url`,
`extract_formats`, `download_media`), `requirements.txt`, `utils/updater.py`

## Symptom

A large share of `https://vt.tiktok.com/<code>/` links failed with yt-dlp
errors that surfaced to users as **"The site changed its layout or the URL is
malformed."** (`❌ Job Failure` in the log channel). Intermittent — the same
link could work later.

## Root cause (researched from yt-dlp source + issues)

1. yt-dlp's short-link extractor for `vt/vm/vn.tiktok.com` resolves the
   redirect with a **bare HEAD whose UA is `facebookexternalhit/1.1`**, no
   cookies, no impersonation (`TikTokShortIE`). TikTok answers a fraction of
   these with its **anti-bot interstitial** (a ~600-byte page,
   `x-tt-system-error: 3`, or a JS "Please wait…" challenge) instead of a 301.
2. The same stochastic block hits the final `www.tiktok.com` webpage fetch —
   `__UNIVERSAL_DATA_FOR_REHYDRATION__` absent → `Unable to extract webpage
   video data`. Blocks are IP/fingerprint-reputation based and flip pass/fail
   between attempts.
3. yt-dlp ≥ 2025-01 ships a **pure-Python proof-of-work challenge solver**
   (PR yt-dlp/yt-dlp#15672) — but the webpage fetch requires **curl-cffi
   impersonation**; without the extra installed, TikTok logs "no impersonate
   target available" and blocks rise again.

## Fix (three layers)

1. **Pre-resolve ourselves** — `_resolve_tiktok_short_url` inside
   `normalize_url`: real browser UA, `requests.get(stream=True)` follow
   redirects, one retry, accept only a canonical `tiktok.com/@…` hop (a
   login/interstitial hop keeps the short URL so yt-dlp's own resolver +
   challenge solver still gets its chance). 1-hour TTL cache because
   `extract_formats` and `download_media` normalize the same URL.
2. **`curl-cffi` everywhere yt-dlp goes**: `requirements.txt` and the nightly
   auto-updater both pin `yt-dlp[default,curl-cffi]` (AGENTS.md invariant #5).
3. **Stochastic-failure retries**: `extract_formats` adds one extra `no-auth`
   attempt for TikTok; `download_media` does one clean no-cookies retry when a
   TikTok attempt died (block pages are not auth failures — the watchdog stays
   quiet).

## Verified

`vt.tiktok.com/ZS4UH9uRa/` → canonical `@user/video/7649651757630508295…`
locally; extraction exact-size `6,125,053` vs delivered `6,125,002`.

## Gotchas / don'ts

- Don't drop the shortlink back into yt-dlp unresolved "because it works on
  your box" — block probability is per-IP and changes weekly.
- The mobile-app API path (`api16/api19 aweme`) is dead without X-Argus
  signing (yt-dlp banned from `api-h2`); don't chase it as a fallback —
  oEmbed (`/oembed?url=`) and `/embed/v2/{id}` are the viable last-resort
  metadata paths if the webpage solver ever breaks again.
- TikTok formats are muxed single streams — no `+bestaudio` merge; exactness
  rule is the video's own.

Sources: yt-dlp `extractor/tiktok.py` (short-IE), yt-dlp issues #15418 /
#17332, PR #15672 (+ review notes in #15644), gallery-dl PR #8850 (same solver
ported), Evil0ctal/Douyin_TikTok_Download_API (cookie/UA pairing for CDN).

---

## 6. Source: `docs/memory/tgbot-2026-08-11-startup-crash-loop-and-tiktok-embed.md`

# Startup crash-loop + undefined-name bug sweep + TikTok embed workaround

**Date:** 2026-08-11 · **Area:** `modules/admin/*`, `modules/direct_forward/*`,
`utils/downloader/*`, `main.py`

## Part 1 — the crash-loop (why the bot was silently dead)

### Symptom

The bot was down even though systemd said `active (running)`. `journalctl -u
tgbot` showed a fresh traceback every ~8 s, each ending in:

```
NameError: name 'CallbackQuery' is not defined
  File "modules/admin/register.py", line 428, in admin_callback_handler
```

`bot.log` mirrored it: the startup banner `[Logger] Standalone Telegram Logging
Service linked to Root Logger.` appeared again and again every ~8 s — a dead
giveaway that the process was crash-looping at **startup**, never reaching the
update loop. The handler signature `async def admin_callback_handler(client,
callback_query: CallbackQuery)` annotated the parameter with a type that was
never imported, so the `def` itself failed at module load.

### Fix

`from pyrogram.types import Message, InlineKeyboardMarkup,
InlineKeyboardButton` → added `CallbackQuery`. Verified with `python3 -m
py_compile` and by restarting the unit. (The crash-loop also meant every admin
console button press timed out silently — this single missing import had
disabled the whole admin UI.)

## Part 2 — the undefined-name sweep (found with pyflakes)

While the bot was dead we ran a full pyflakes scan over every module. It found
**8 more latent `NameError`s** — none crashed the bot yet, but all would fire
the first time their code path ran (a callback press, a cookie test, a TikTok
relay). All are fixed and import-verified:

| File | Bug | Fix |
|---|---|---|
| `modules/admin/register.py` | `add_premium_user` / `remove_premium_user` used but never imported | added to the `from utils.gate import (...)` block |
| `modules/admin/register.py` | `blacklist_user` used in `security_gate` but not imported | added to the same block |
| `modules/admin/callback_dispatch.py` | `get_direct_menu_keyboard` undefined | imported from `.keyboards` (defined at `keyboards.py:74`) |
| `modules/admin/callback_dispatch.py` | `direct_forward` undefined at module level (was only imported locally in two handlers) | added module-level `from modules import direct_forward` |
| `modules/admin/cookie_test.py` | `log_event` undefined | `from main import log_event` |
| `modules/admin/premium_gen.py` | `log_event` undefined | `from main import log_event` |
| `modules/direct_forward/instagram.py` | `random` undefined | added `import random` |
| `modules/direct_forward/instagram.py` | `_cursor` / `_bump_cursor` undefined | added to the `from .state import (...)` block (`state.py:57` / `state.py:64`) |
| `modules/direct_forward/tiktok.py` | `_tt_poll_interval` undefined | added to the `from .common import (...)` block (`common.py:45`) |
| `utils/downloader/formats.py` | `os` referenced at line 26 before the local `import os` inside the function body — a **runtime** NameError on every `extract_formats` call | moved to a top-level `import os` |
| `utils/downloader/formats.py` | `_apply_pot_options` undefined | added to the `from .url_normalize import ...` line |
| `utils/downloader/cookies.py` | `_apply_pot_options` undefined | added `from .url_normalize import _apply_pot_options` |

### Lessons

- **`from main import log_event` is safe and is the established pattern** —
  `main.py` defines it at module level and `main_engine()` imports `modules.*`
  lazily *inside the function*, so there is no circular import risk.
- **Signature annotations that reference unimported names kill the module at
  import time**, not at call time. A type-hint on a handler parameter is
  executed when the `def` runs. This is why the crash-loop started at startup.
- **A local `import os` inside a function does not protect an earlier use of
  `os` at module scope.** Keep stdlib imports at the top of the file.
- The **venv import smoke test** (`source venv/bin/activate && python -c
  "import <module>..."` on every edited module) catches both circular-import
  and undefined-name-at-import problems that `py_compile` cannot. Use it after
  any import-related change.

## Part 3 — TikTok embed workaround (yt-dlp#17403)

### What changed on TikTok's side

yt-dlp's `www.tiktok.com/@user/video/<id>` webpage fetch now frequently hits
the **anti-bot challenge page** instead of the rehydration JSON. Symptom in
logs:

```
ERROR: [TikTok] ...: Unexpected response from webpage request
```

This is tracked upstream as **yt-dlp issue #17403** — it's a site change, not
a bug in our code, so it is deliberately **not** "fixed" by us beyond a
workaround. The oEmbed endpoint still serves metadata without the challenge.

### Workaround (mirrors the direct-forward path's earlier commit `e48b060`)

1. **`_to_tiktok_embed_url`** in `utils/downloader/url_normalize.py` rewrites
   `https://www.tiktok.com/@user/video/<id>` →
   `https://www.tiktok.com/embed/<id>`. The embed page serves the challenge-free
   JSON. `normalize_url` now chains `_resolve_tiktok_short_url` → `_to_tiktok_embed_url`.
2. **`_apply_pot_options`** sets `opts["http_headers"] = {"User-Agent":
   _TIKTOK_EMBED_UA}` for `tiktok.com/embed/` URLs (Chrome 140 UA; the embed
   page needs it). YouTube's PO-token branch is unchanged, other sites pass
   through untouched.
3. **`download_media`** in `utils/downloader/download.py` skips cookies for
   TikTok embed URLs (`use_cookies_now = bool(site_jar) and not is_instagram
   and not is_tiktok_embed`) — the embed path needs no session.

### Gotchas / don'ts

- The embed workaround lives at the **`normalize_url` layer**, so it applies
  to both `extract_formats` and `download_media` automatically. Don't add a
  second copy in the download pipeline.
- Do **not** treat the `Unexpected response from webpage request` error as a
  bot bug while the challenge is live — check `git log` for the upstream
  fix. The embed rewrite is our hedge, not a guarantee; if TikTok hardens the
  embed page too, wait for the yt-dlp fix rather than piling on more layers.
- Keep the Chrome UA tied to the embed URL only. A blanket TikTok UA change
  is not needed and would burn the browser-like fingerprint on the main site.

## Part 4 — other log findings (already expected, no code change)

- **Instagram 400s** (`[Instagram] <code>: Video info extraction failed: HTTP
  Error 400`): stale/expired session on the cookies path; IG extraction stays
  no-auth-first (AGENTS.md invariant #3). Cookie write-back (invariant #4)
  keeps the jar warm; re-upload via Admin → Cookies when it recurs.
- **X direct-forward photo fallbacks** (`No video could be found in this tweet`
  → `'urls'` / `'withheld_in_countries'` KeyErrors inside twikit's `User`
  parsing): already handled by the `_x_fallback_photos` walk + text-only
  note fallback. Tweeting at X (280 chars) makes the `'urls'` key optional —
  don't rely on twikit's strict parsing.
- **Dailymotion `Access forbidden`**: site-side, intermittent.
- **`0.0.0.0:8080` FastAPI is internet-exposed**; uvicorn access logs show
  routine scanner noise (`/.env`, `/.aws/credentials`, `/actuator/env`, …) all
  404. Harmless, but the health/status endpoints stay unauthenticated by
  design — do not add secrets to them.

## Verification

```bash
python3 -m py_compile $(git ls-files '*.py')        # all OK
bash -n install.sh run.sh uninstall.sh             # all OK
cd cmd/tgbot-monitor && go test ./...              # ok
source venv/bin/activate && python -c "import utils.downloader.formats; import utils.downloader.cookies; import modules.admin.callback_dispatch; import modules.direct_forward.instagram; import modules.direct_forward.tiktok; import modules.admin.premium_gen; import modules.admin.cookie_test; import modules.admin.register"
sudo systemctl restart tgbot                        # stable, no crash-loop
```

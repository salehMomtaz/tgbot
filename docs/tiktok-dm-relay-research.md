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

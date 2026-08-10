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

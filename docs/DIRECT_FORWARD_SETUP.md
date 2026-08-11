# Direct-forward setup (Instagram / X DM → Telegram)

The bot relays media into your Telegram chat from two very different setups:

- **Instagram** uses a dedicated bot account: you DM posts/reels/stories/links
  from your personal account to the bot's IG account.
- **X/Twitter** uses the **self-DM method**: no separate bot account, no
  pairing. You send tweet links / photos / videos to your OWN X self-DM
  ("Message Yourself"); the bot — authenticated with the same `xcookies.txt`
  jar yt-dlp already uses — polls that one conversation and relays it.

No third-party APIs — `instagrapi` for Instagram, `twikit` for X, plus the
in-repo `xchat_bridge.mjs` Deno sidecar for X Chat (E2EE) decryption.

```
IG:  You ──DM──▶ Bot's IG account          X:  You ──DM to YOURSELF──▶
     (paired)                                  "Message Yourself"
                       │ poll every N sec                       │
                       ▼                                        ▼
                       tgbot downloads + sends to DIRECT_FORWARD_CHAT_ID
```

Instagram only processes DMs from **your paired account** — strangers who
message the bot account are ignored. X needs no pairing: the self-DM
conversation `<self_id>-<self_id>` is only reachable by the account itself.
The first run primes the cursor and skips the existing backlog.

Instagram pairs in one of two ways:

- **Interactive handshake (recommended):** Telegram → Admin Console → 📨
  Direct-Forward → 🔗 Pair Instagram. The bot issues a one-time 6-digit code
  (valid 10 min). Send it via DM to the bot account; the bot confirms the pair
  in your Telegram chat and locks relays to your platform user id.
- **Static pre-pair:** `IG_DIRECT_FROM_USERNAME` in `.env` (resolved to a
  numeric id at startup and persisted).

Unpair Instagram any time via the Direct-Forward menu. Deleting
`direct_forward_state.json` clears the cursor and the IG pairing.

## What you need first

1. A **dedicated Instagram account** for the bot (not your personal one).
2. For X: **no separate account** — just a working `xcookies.txt` (the same
   jar the downloader uses) and, if your self-DM is passcode-protected, the
   `XCHAT_PIN` for the bridge (see the X section below).
3. From your *personal* account, open a DM thread with the IG bot account so
   the thread exists; for X, make sure your self-DM ("Message Yourself") thread
   exists.

## Instagram

Preferred auth is **zero-interaction**: the DM client bootstraps from the
`sessionid` inside your `igcookies.txt` jar — the same jar the downloader
uses, kept warm by cookie write-back. So:

1. Upload a fresh Instagram jar: **Telegram → Admin Console → 🍪 Cookie Jars →
   Instagram → ✏️ Replace** (export with a "Get cookies.txt LOCALLY" browser
   extension while logged into the bot's IG account).
2. Set in `.env`:

```
DIRECT_FORWARD_CHAT_ID=123456789        # your numeric Telegram user id
DIRECT_FORWARD_POLL_SECONDS=300
IG_DIRECT_ENABLED=true
IG_DIRECT_FROM_USERNAME=your_personal_ig_handle
```

3. `IG_DIRECT_USERNAME` / `IG_DIRECT_PASSWORD` are only a **fallback** when the
   sessionid login fails (dead session). `IG_DIRECT_TOTP_SEED` is optional for
   accounts with TOTP-based 2FA.

Session persistence: after the first successful login the client dumps its
session to `direct_ig_session.json` (git-ignored, `chmod 600`) and resumes it
on every restart (validated via `account_info()`, so no password is required),
so Instagram challenges you at most once.

The IG worker **never exits on a login failure**. If the session/jar is dead it
retries on the poll cadence with a fresh client per attempt — so uploading a
fresh `igcookies.txt` mid-run (Admin → Cookies → Replace) is picked up
automatically, **no bot restart needed**. The earlier "login failed" errors in
the logs that required a restart are fixed by this.

If Instagram challenges the bot (CheckpointRequired), the worker freezes for
~3–5 hours and logs a loud, actionable line (retry storms only deepen the flag).
The **durable fix** is to open the official Instagram app on the bot account
and pass the checkpoint there, then restart the bot.

## Avoiding checkpoints ("We suspect automated behavior")

Instagram flags accounts on behavior, not one bad request. The poller is the
loudest signal, so it is built to look human (instagrapi + instagram-private-api
community best practices):

| Lever                            | What tgbot does                                             | Knob |
|----------------------------------|-------------------------------------------------------------|------|
| Poll cadence                     | Several minutes, never seconds                              | `DIRECT_FORWARD_POLL_SECONDS` (≥ 300 recommended) |
| Machine-perfect cadence          | Each interval is randomized                                 | `DIRECT_FORWARD_POLL_JITTER_PCT` (default 40 ⇒ ±40%) |
| Request bursts                   | `delay_range = [2, 4]` paces every private-API call         | — (fixed) |
| Idle request volume              | inbox `last_activity_at` → unchanged threads cost **0** calls | — (automatic) |
| Session identity                 | persisted `direct_ig_session.json`, same device/UA forever  | — (automatic) |
| IP reputation                    | optional **one stable** residential proxy for the account   | `DIRECT_FORWARD_PROXY` |
| Checkpoint response              | freeze 3–5 h; never a retry storm                           | — (fixed) |
| TLS/HTTP fingerprint             | private API rides a **curl_cffi-backed impersonating transport** (`CurlCffiAdapter`, `curl-adapter>=1.2.1`) speaking a real Chrome JA3/JA4 instead of Python `requests` | `IG_DIRECT_TRANSPORT_IMPERSONATE` (default `chrome136`) |
| Echo headers                     | `IG-U-RUR` / `IG-U-SHBID` / `IG-U-SHBTS` / `X-IG-WWW-Claim` / `X-MID` are captured from every response, persisted, and re-applied | — (automatic) |
| Geo / locale / timezone          | pinned to the account's home region, never drifts           | `IG_DIRECT_COUNTRY` / `IG_DIRECT_COUNTRY_CODE` / `IG_DIRECT_LOCALE` / `IG_DIRECT_TZ_OFFSET` / `IG_DIRECT_TZ_NAME` |
| Cold start                       | paced benign warmup (`account_info` + `direct_threads`) right after login | — (automatic) |
| Checkpoint alert                 | freeze 3–5 h **and** a direct Telegram alert to the relay chat with instructions to pass verification in the official app | — (automatic) |

> **2026-08-05:** a second checkpoint (native manual-verification) hit after
> clean relay traffic — cadence was fine; the trigger was identity correlation
> (Python `requests` TLS riding an Android UA, plus missing echo-header
> persistence). All of `utils/ig_anti_detect.py`'s levers above are now
> implemented, deployed, and verified (see
> `docs/memory/tgbot-ig-anti-detection.md`). Reference code: `reference/`
> (okgram, insta-wizard, instaharvest_v2).

**Practical rules that matter most:**

1. **Keep the session, don't re-login.** `direct_ig_session.json` IS the
   account's trusted device. Deleting it (or re-logging with password every
   boot) is the #1 checkpoint trigger. Let it persist; only replace
   `igcookies.txt` when the downloader jar actually dies.
2. **One account, one IP.** If the VPS datacenter IP keeps getting flagged,
   put the DM session behind ONE residential proxy close to where the account
   normally lives (`DIRECT_FORWARD_PROXY=socks5h://user:pass@host:port`) and
   never change it. Per-request rotation is worse than no proxy.
3. **Don't run a second poller.** Two devices polling the same inbox 24/7
   (another bot, a desktop client) doubles the automation signal.
4. **Challenged? Pass it in the app**, then restart. Fresh jars + restart in a
   loop looks like account sharing.

## X / Twitter (self-DM)

No separate bot account, no username/password, no pairing. The worker reads the
**shared `xcookies.txt` jar** (`cookies/twitter/xcookies.txt`) that yt-dlp
already downloads with, extracts the account's numeric id from the `twid`
cookie, and polls the self-DM conversation `<self_id>-<self_id>`. Cookie
write-back keeps the jar warm, so there is **no twikit-specific session file**
(`direct_x_cookies.json` no longer exists).

```
X_DIRECT_ENABLED=true
XCHAT_PIN=1234        # only if your self-DM uses X Chat (E2EE) — see below
```

**To use the X relay:**

1. Upload a working Twitter jar: **Telegram → Admin Console → 🍪 Cookie Jars →
   Twitter → ✏️ Replace** (export with a "Get cookies.txt LOCALLY" browser
   extension while logged in to the account you will DM from).
2. In X, open **Message Yourself** and send tweet links, photos, or videos.

**X Chat / E2EE — now fully supported via the bridge.** The 2025 X Chat rollout
(4-digit passcode, enabled when both parties opt in) encrypts the self-DM
conversation, which twikit's legacy DM API cannot read. tgbot ships a Deno
sidecar, `xchat_bridge.mjs`, that decrypts the XChat-encrypted self-DM with
your PIN and appends canonical lines to `cache/xchat_inbox.jsonl` — the X
worker reads that file first, and only falls back to the twikit poll when no
bridge output exists. So you **may** enable the passcode on your self-DM; the
bridge handles it. To use it:

1. **No SSH needed.** In the bot console: **Admin → 📨 Direct-Forward →
   🔑 Set X Chat PIN**, then send your 4-digit passcode as a message. The bot
   writes it to `.env` (`XCHAT_PIN`) automatically.
2. The `tgbot-xchat-bridge` systemd unit (enabled by `install.sh`, wrapper:
   `tools/start_xchat_bridge.sh`, logs to
   `sudo journalctl -u tgbot-xchat-bridge -f`) is a **resident supervisor**: it
   re-reads `.env` every ~5 s and (re)spawns the Deno sidecar as soon as
   `X_DIRECT_ENABLED` + `XCHAT_PIN` + the xcookies jar all hold — so the relay
   comes up on its own a few seconds after you enter the PIN, with no
   `systemctl` and no shell access. It is a harmless sleeping no-op until
   configured.
3. The bridge needs the project's npm deps (`emusks` → `cycletls`); install.sh
   runs `npm install` for you. Runtime is Deno (already installed).

Without `XCHAT_PIN` the bridge stays down and the worker's twikit fallback
still relays **unencrypted** self-DMs — only passcode-protected messages are
invisible.

**How each DM type is handled:**

- **Tweet link / tweet share** → the yt-dlp pipeline (with your X cookies),
  auto-picking the **highest available quality**. When the top format exceeds
  the upload ceiling (2 GB bot / 4 GB Premium) the format-selection keyboard is
  posted instead so you can pick a smaller quality.
- **Photo-only tweet** (no video stream for yt-dlp) → delivered natively from
  the tweet's CDN URLs, grouped when there is more than one. Pasting a photo-only
  tweet URL works too: the bot reads the tweet via twikit's raw GraphQL
  (`gql.tweet_detail`, scoped to the focal tweet) and delivers the photos.
- **DM photo / video attachment** → fetched through the authenticated twikit
  session (ton.twitter.com URLs 401 without cookies).
- **Plain link** → generic download pipeline.


**Warning:** a fresh `xcookies.txt` export from a datacenter IP can be
challenged by X on first use. Warm the account first: log in once in a browser
(a mobile/residential IP helps), complete any verification, then export the
jar. The worker **live-reloads the jar every poll** (hash-compare in
`_twitter_worker`), so a mid-run re-upload (Admin → Cookies → Replace) is
picked up on the next poll without a bot restart; if the `twid` changes it
rebuilds the client and re-primes the cursor.

## How each DM type is handled

| You send…                      | Bot does… |
|---|---|
| Photo/video attachment         | downloads the attachment, sends it as Telegram photo/video |
| Share a photo post / album / story | **Instagram-native delivery** through the DM session's CDN (yt-dlp's extractor reliably breaks on these). A carousel with an empty/invalid CDN resource is degraded to its healthy items instead of failing the whole send |
| Share a reel / clip            | resolves the shortcode → yt-dlp pipeline (with your IG cookies) |
| Paste any link                 | routes through the standard download pipeline (all sites) |
| Share a tweet / tweet link (X) | **highest-quality auto-download** via the yt-dlp pipeline (with your X cookies); the format keyboard is posted when it exceeds the upload ceiling |
| Photo-only tweet (X)           | delivered natively from the tweet's CDN URLs (grouped when > 1); pasted links resolved via twikit's raw GraphQL walk |

Downloads enqueue on the **same single-worker queue** as your interactive
downloads, so a DM relay never starves you out of the bot.

## State / reset

- `direct_forward_state.json` — per-platform cursors. Delete to re-prime
  (backlog will be skipped again on the next boot). The file is SHARED by the
  IG, X and TikTok workers; the code always writes it merge-only per platform
  (see `_state_save_owned`), so deleting/resetting it re-primes every
  platform, not just the one you were editing.
- `direct_ig_session.json` — Instagram's live session. Delete to force
  re-login. (X has no session file — it rides the shared `xcookies.txt` jar.)
- `cache/xchat_inbox.jsonl` — the bridge's canonical message lines (the worker
  filters by cursor, so it is safe to truncate).
- `cache/xchat_bridge_state.json` — the bridge's own `last_seq`. Delete to
  re-prime (the bridge then skips backlog, same as the worker).

**Both `cache/xchat_bridge_state.json` and `cache/xchat_inbox.jsonl` are
exempt from the hourly cache cleaner** (`main.py::auto_clean_cache_directory`
skip-list). Deleting the bridge state while the bridge is running makes it
re-prime `last_seq` to newest and silently skip older messages — a data-loss
window, hence the exemption. If you reset the X relay by hand, delete the
state file intentionally, not via the cleaner.

## TikTok (self-DM via WebSocket IM)

**⚠️ CURRENTLY BROKEN — Known upstream issue (Aug 2026)**

TikTok direct forward uses the same self-DM concept as X: you send video shares to your own TikTok self-DM, and the bot relays them via a persistent WebSocket connection to TikTok's IM service (`im-ws-sg.tiktok.com/ws/v2`).

**Status**: The WebSocket connection and message reception work correctly. However, **video downloads fail** due to a known yt-dlp issue ([#17403](https://github.com/yt-dlp/yt-dlp/issues/17403)) where TikTok changed their anti-bot challenge format and yt-dlp's solver cannot parse it.

```
TIKTOK_DIRECT_ENABLED=true
TIKTOK_DIRECT_POLL_SECONDS=300
TIKTOK_DIRECT_POLL_JITTER_PCT=40
```

**To use (when fixed):**
1. Upload a working TikTok jar: **Telegram → Admin Console → 🍪 Cookie Jars → TikTok → ✏️ Replace** (export cookies while logged into your TikTok account)
2. In TikTok, open **Messages** (your self-DM) and share videos to yourself
3. The bot receives pushes via WebSocket and downloads via yt-dlp

**Workaround until fixed:**
- Send TikTok links directly to the bot (interactive download)
- Use the TikTok app to save videos, then send files to the bot
- Monitor yt-dlp issue #17403 for the fix

The admin console test (`Admin → 📨 Direct-Forward → 🧪 Test TikTok`) shows the WebSocket authentication status and warns about the download issue.

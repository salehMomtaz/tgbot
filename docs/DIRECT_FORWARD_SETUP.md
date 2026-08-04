# Direct-forward setup (Instagram / X DM → Telegram)

The bot relays whatever you **DM to its own Instagram or X account** into your
Telegram chat. Photos, videos, reels, story shares, tweet shares, and plain
links are all supported. No third-party APIs — the bot logs in as the bot
account directly with local libraries (`instagrapi` for Instagram, `twikit`
for X).

```
You (personal account)  ──DM: "look at this reel"──▶  Bot's IG/X account
                                                         │ poll every N sec
                                                         ▼
                                            tgbot downloads + sends to
                                              DIRECT_FORWARD_CHAT_ID
```

Only DMs from **your paired account** are processed — strangers who message the
bot account are ignored. The first run primes the cursor and skips the existing
backlog. Pair in one of two ways:

- **Interactive handshake (recommended):** Telegram → Admin Console → 📨
  Direct-Forward → 🔗 Pair Instagram. The bot issues a one-time 6-digit code
  (valid 10 min). Send it via Instagram DM to the bot account; the bot confirms
  the pair in your Telegram chat and locks relays to your Instagram user id.
- **Static pre-pair:** `IG_DIRECT_FROM_USERNAME` in `.env` (resolved to your
  numeric user id at startup and persisted).

Unpair any time via the Direct-Forward menu. Deleting `direct_forward_state.json`
clears both the cursor and the pairing.

## What you need first

1. A **dedicated Instagram account** for the bot (not your personal one).
2. Optionally a **dedicated X account** for the bot.
3. From your *personal* account, open a DM thread with the bot account on each
   platform so the thread exists.

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

## X / Twitter

`twikit` logs in with the bot account's username/password (+email) once and
persists cookies to `direct_x_cookies.json`:

```
X_DIRECT_ENABLED=true
X_DIRECT_USERNAME=bot_x_handle
X_DIRECT_PASSWORD=bot_x_password
X_DIRECT_EMAIL=bot_x_email
X_DIRECT_FROM_USER_ID=0123456789        # YOUR numeric X user id
```

Finding your numeric X user id: any "what's my user id" service/bot, or check
the `profile_id` for your handle. DMs from any other sender are ignored.

**Warning:** X aggressively locks accounts on fresh automation logins from
datacenter IPs. If login fails repeatedly, warm the account first: log in once
in a browser (a mobile/residential IP helps), complete any verification, then
retry. `xcookies.txt` (Admin → Cookies) also warms yt-dlp downloads but twikit
keeps its own cookie jar.

## How each DM type is handled

| You send…                      | Bot does… |
|---|---|
| Photo/video attachment         | downloads the attachment, sends it as Telegram photo/video |
| Share a photo post / album / story | **Instagram-native delivery** through the DM session's CDN (yt-dlp's extractor reliably breaks on these). A carousel with an empty/invalid CDN resource is degraded to its healthy items instead of failing the whole send |
| Share a reel / clip            | resolves the shortcode → yt-dlp pipeline (with your IG cookies) |
| Paste any link                 | routes through the standard download pipeline (all sites) |
| Share a tweet (X)              | builds the status URL → yt-dlp pipeline (with your X cookies) |

Downloads enqueue on the **same single-worker queue** as your interactive
downloads, so a DM relay never starves you out of the bot.

## State / reset

- `direct_forward_state.json` — per-platform cursors. Delete to re-prime
  (backlog will be skipped again on the next boot).
- `direct_ig_session.json`, `direct_x_cookies.json` — live sessions. Delete to
  force re-login.

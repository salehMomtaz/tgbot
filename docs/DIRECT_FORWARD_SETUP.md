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

Only DMs from **your whitelisted account** are processed; strangers' messages
are ignored. The first run primes the cursor and skips the existing backlog.

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
DIRECT_FORWARD_POLL_SECONDS=120
IG_DIRECT_ENABLED=true
IG_DIRECT_FROM_USERNAME=your_personal_ig_handle
```

3. `IG_DIRECT_USERNAME` / `IG_DIRECT_PASSWORD` are only a **fallback** when the
   sessionid login fails (dead session). `IG_DIRECT_TOTP_SEED` is optional for
   accounts with TOTP-based 2FA.

Session persistence: after the first successful login the client dumps its
session to `direct_ig_session.json` (git-ignored, `chmod 600`) and resumes it
on every restart, so Instagram challenges you at most once.

If Instagram challenges the bot (CheckpointRequired), the worker pauses for an
hour and logs a loud, actionable line — upload a fresh jar and restart.

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
| Share a post / reel / clip     | resolves the shortcode → yt-dlp pipeline (with your IG cookies) |
| Share a story                  | builds the story URL → yt-dlp pipeline (needs IG cookies) |
| Paste any link                 | routes through the standard download pipeline (all sites) |
| Share a tweet (X)              | builds the status URL → yt-dlp pipeline (with your X cookies) |

Downloads enqueue on the **same single-worker queue** as your interactive
downloads, so a DM relay never starves you out of the bot.

## State / reset

- `direct_forward_state.json` — per-platform cursors. Delete to re-prime
  (backlog will be skipped again on the next boot).
- `direct_ig_session.json`, `direct_x_cookies.json` — live sessions. Delete to
  force re-login.

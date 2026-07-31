# Auto-forward Setup Guide

This is the practical companion to the blueprint's auto-forward section.

## The concept (one sentence)

You share posts to a **dedicated bot account** on Instagram / TikTok / X;
the bot polls that account's saved/liked feed and forwards anything new to
your Telegram chat.

## The accounts you need

| Platform | Account purpose | What you do in-app | Cookie jar |
|---|---|---|---|
| Instagram | A separate `@your_bot_ig` | **Save** posts from your real account | `cookies/instagram/igcookies.txt` |
| TikTok | A separate `@your_bot_tt` | **Like** videos from your real account | `cookies/tiktok/ttcookies.txt` |
| X / Twitter | A separate `@your_bot_x` | **Like** tweets from your real account | `cookies/twitter/xcookies.txt` |

## Step-by-step

### 1. Create the bot accounts

Make dedicated accounts (not your personal ones) for each platform you want.
These accounts exist only to receive the posts you share.

### 2. Export cookies from the bot account

Log in to the bot account in a browser, open a feed/post, and export cookies
with the same `.txt` extension. Upload to the bot via the admin console:
```
🍪 Cookie Jars → Instagram → ✏️ Replace
```
Then tap `🧪 Test`.

### 3. Configure `.env`

```bash
# The Telegram chat ID that receives forwarded media (your numeric user ID)
AUTO_FORWARD_CHAT_ID=123456789

# Poll interval (default 300 = 5 minutes)
AUTO_FORWARD_POLL_SECONDS=300

# Max items per poll (prevents runaway pulls)
AUTO_FORWARD_MAX_ITEMS=10

# Per-platform toggles and usernames
IG_AUTO_FORWARD_ENABLED=true
IG_AUTO_FORWARD_USERNAME=your_bot_ig_username_without_@

TT_AUTO_FORWARD_ENABLED=true
TT_AUTO_FORWARD_USERNAME=your_bot_tt_username

X_AUTO_FORWARD_ENABLED=true
X_AUTO_FORWARD_USERNAME=your_bot_x_username
```

### 4. Restart the bot

```bash
sudo systemctl restart tgbot
```

Then check it started cleanly:
```bash
sudo journalctl -u tgbot -n 30
```
You should see:
```
[AutoForward] AUTO_FORWARD_CHAT_ID not set; ...
```
if `CHAT_ID` is 0, or the enabled confirmation if configured.

### 5. Share a post

- **Instagram:** open any Reel / post → tap the bookmark (Save). The post lands
  in your bot account's `saved/` feed. The bot detects it on the next poll.
- **TikTok:** open any video → tap the heart (Like). The bot polls the liked feed.
- **X:** open any tweet → tap the heart (Like). The bot polls the liked feed.

### 6. Wait and collect

Within `POLL_SECONDS` (default 5 minutes) you receive a Telegram message with:
- The photo or video file.
- Caption: `"🔄 Auto-forward from instagram/tiktok/x"`.
- Title from the original post.

### 7. What to do if nothing arrives

1. Check the bot log: `tail -f logs/bot.log`
2. Check the admin console: does the cookie jar pass `🧪 Test`?
3. Check the bot account: did you actually Save / Like the post?
4. Check `.env`: is `CHAT_ID` set to your numeric Telegram ID (not username,
   not bot token)?
5. Check the bot account cookies: is the session still valid? (Test button
   tells you.)

### 8. How to turn it off

```bash
# In .env, either:
AUTO_FORWARD_CHAT_ID=0
# or disable each platform:
IG_AUTO_FORWARD_ENABLED=false
```

Then `sudo systemctl restart tgbot`. The relay exits cleanly and never starts.

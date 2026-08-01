# Lesson 18 — Build a feature end-to-end

Your final boss. Here is a real feature added to this bot in ~30 lines:

**Feature**: `/ping` command — replies with the bot's uptime.

## Step 1: Where does it live?

It is a text command, so `modules/downloader_handler.py` (the link handler) is
the natural place. Look at the existing `is_link` handler and add a branch.

## Step 2: Write the handler

```python
import time

_START_TIME = time.time()

@app.on_message(filters.command("ping") & filters.private, group=1)
async def ping_handler(client, message):
    uptime = int(time.time() - _START_TIME)
    hours, rem = divmod(uptime, 3600)
    minutes, seconds = divmod(rem, 60)
    await message.reply_text(
        f"🏓 Pong!\nUp for {hours}h {minutes}m {seconds}s."
    )
```

`divmod(a, b)` returns `(a//b, a%b)` — a stdlib function you will see in the
real codebase (`format_size_short`).

## Step 3: Register the module

`main.py::main_engine` already imports `register_downloader_handlers(app)`, so
the new handler is picked up automatically. No wiring needed.

## Step 4: Verify

```bash
python3 -m py_compile modules/downloader_handler.py
```

If it compiles, the syntax is valid. Then (if you can) deploy to the VPS and
send `/ping` in your Telegram chat.

## Step 5: Commit

```bash
git add modules/downloader_handler.py
git commit -m "feat: /ping command shows bot uptime"
git push origin main
```

The commit message follows the repo convention: `feat:` prefix, short
imperative summary.

## What did we just use?

In order: `import time` (module), `async def` (async), `message.reply_text`
(class method), `divmod` (stdlib function), `f"{hours}h" {minutes}m"`
(f-string). Every concept from lessons 1 through 15 showed up.

## Where this shows up in tgbot

Every feature in the changelog was written this way:

- `direct_forward.py` → a background worker + a state JSON file.
- `cookies/ytdlp/<site>.txt` → a per-site lookup inside `get_cookies_for_url`.
- Per-site cookie uploads → a new callback `admin_cookie_add_site` + a state
  machine transition.

The pattern is always: **find where related features live → write a small
handler → add the registration if needed → test, commit, push.**

## Exercise

Pick your own feature. Some ideas ranked easy→hard:

1. `/stats` command — show how many downloads, errors, users (read from a dict
   you maintain in `utils/shared.py`).
2. `/uptime` that combines ping + current download queue length.
3. A button inside the admin console that dumps `RUNTIME_SETTINGS` as text.

Write it, compile it, (optionally) deploy it, commit it. You are now a bot
author.

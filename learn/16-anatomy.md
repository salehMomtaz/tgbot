# Lesson 15 — Telegram bots with pyrogram

pyrogram is the framework this bot uses. You do not need to master it — you need
to read it. Here are the core pieces.

## Install and connect

```python
from pyrogram import Client
from pyrogram import filters

app = Client(
    "my_session",               # session name (creates my_session.session)
    api_id=API_ID,              # from my.telegram.org
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,        # from @BotFather
)
```

## Message handlers

```python
@app.on_message(filters.private & filters.text)
async def echo(client, message):
    await message.reply_text(f"You said: {message.text}")
```

Handler order is controlled by `group`:

```python
@app.on_message(filters.private, group=-2)   # runs first (log)
@app.on_message(filters.private, group=-1)   # security gate
@app.on_message(filters.private, group=0)     # state machine
@app.on_message(filters.private, group=1)     # text router
@app.on_message(filters.private, group=2)     # callback dispatcher
```

Lower group numbers run first. Each handler can `continue_propagation()` (let
others run) or `stop_propagation()` (stop the chain).

## Buttons and callbacks

```python
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton("🍪 Cookie Jars", callback_data="admin_cookies_menu")]
])

@app.on_callback_query(filters.regex(r"^admin_"))
async def handle_callback(client, callback_query):
    await callback_query.answer()             # dismiss the "loading" spinner
    await callback_query.message.edit_text("Menu", reply_markup=keyboard)
```

`callback_data` is the string that flows back when a user taps the button. It is
how the bot's entire menu system works (split by `:`).

## Where this shows up in tgbot

`modules/admin.py` is a giant pile of callback handlers:

```python
@app.on_callback_query(filters.regex(r"^admin_cookie_action:"))
async def handle_cookie_action(client, callback_query):
    data = callback_query.data                 # "admin_cookie_action:ytcookies:replace"
    _parts = data.split(":")
    cookie_key, action = parts[1], parts[2]
    # ... do the action ...
```

And `main.py::patch_pyrogram_send_methods` monkey-patches pyrogram to log every
sent message — a neat example of how Python lets you replace a method at runtime:

```python
orig_send_message = Client.send_message

async def wrapped_send_message(self, *args, **kwargs):
    sent = await orig_send_message(self, *args, **kwargs)
    logging.info(f"📤 **[SENT MESSAGE]**\n{str(sent)}")
    return sent

Client.send_message = wrapped_send_message
```

## Exercise

Build a tiny bot on your own machine (dummy `api_id`/`api_hash`/`bot_token` will
fail to connect, but you can write the code): a `/start` handler that replies
with two inline buttons "Cat" and "Dog". A callback handler that edits the
message to "You picked Cat" or "You picked Dog" depending on `callback_data`.
</think:6124c78e>
<tool_call:6124c78e>write<tool_sep:6124c78e>
<arg_key:6124c78e>content</arg_key:6124c78e>
<arg_value:6124c78e># Lesson 16 — Anatomy of tgbot

Now put the pieces together. Here is the file map from `AGENTS.md`:

| File | Responsibility |
|------|----------------|
| `config.py` | all `.env` values, cookie path constants |
| `main.py` | entry point, system wiring, progress bars, cookie init |
| `modules/admin.py` | admin console, cookie upload/replace, PO-token control |
| `modules/downloader_handler.py` | link → format keyboard → download job |
| `modules/direct_forward.py` | IG/X DM relay |
| `modules/stream_handler.py` | direct-link streaming server (FastAPI) |
| `modules/stream_interceptor.py` | forwarded-file → stream link |
| `utils/downloader.py` | yt-dlp wrapper: extract_formats, download_media |
| `utils/uploader_handler.py` | split + upload to Telegram |
| `utils/pot_provider.py` | YouTube PO-token server lifecycle |
| `utils/queue_manager.py` | single-worker download queue |
| `utils/shared.py` | runtime registry, RUNTIME_SETTINGS |
| `utils/updater.py` | background yt-dlp nightly updater |
| `utils/logger.py` | Telegram channel logging |
| `utils/id_validator.py` | Telegram ID syntax check |

## Lifecycle (what happens at boot)

`main.py::main_engine`:

```
1. setup_system_logger()        # Telegram + file log sinks
2. initialize_cookie_jars()     # create cookies/* dirs, lock YT jar to 0o444
3. disk guard                   # refuse if disk >95% full
4. register handlers            # admin, downloader, stream
5. app.start()                  # connect to Telegram
6. premium_app.start()          # optional 4 GB uploader
7. start PO provider            # deno server on 127.0.0.1
8. start FastAPI (uvicorn)      # port 8080
9. run background tasks:        # updater, cache cleaner, auto-forward
   asyncio.gather(server.serve(), auto_update_ytdlp(),
                 auto_clean_cache_directory(), af_task)
```

## The two paths that matter most

**Download path** (a user pastes a link):

```
message → downloader_handler.is_link()
        → show_format_selection() (spawns fetch)
        → extract_formats() (yt-dlp, no queue)
        → user taps a button → queued_transfer_job
        → download_media() (yt-dlp, in queue)
        → process_split_and_upload() (Telegram)
```

**Direct-forward path** (bot account receives a DM):

```
_direct_forward_supervisor loop (every N seconds)
  → _extract_saved_or_liked(platform, username)
  → compare against seen IDs
  → _download_and_send() → process_split_and_upload()
```

Both paths converge on `utils/downloader.py` (the yt-dlp layer) and
`utils/uploader_handler.py` (the Telegram layer). That is the "kernel" of the
bot.

## Exercise

Open `main.py` and read `main_engine` start to finish. Write a one-line comment
above each block explaining what it does (in your own words, in a scratch file
— do not edit the repo). Cross-check your understanding against this map.
```

## Where this shows up in tgbot

The `download_media` function is the bridge: it accepts `format_id` (single
video), `format_selector` (playlist tier), or nothing (best), and always ends
with `process_split_and_upload`.

## Exercise

Add a new format-tier "audio best quality" to `PLAYLIST_TIERS` for the audio
`high` tier that uses `bestaudio/best` (already there) — instead, try adding a
*new* `("a", "ultra")` tier using `bestaudio[abr>=320]/bestaudio/best`. Then
add a menu button in `build_playlist_tier_keyboard` that calls it.

This is a real, safe, additive change. Test it does not break the existing
`("a", "high")`, `("a", "medium")`, `("a", "low")` paths.
```

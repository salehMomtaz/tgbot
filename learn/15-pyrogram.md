# Lesson 15 — Telegram bots with pyrogram

pyrogram is the framework this bot uses. You do not need to master it — you
need to read it. Here are the core pieces.

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

`modules/admin.py` is a big pile of callback handlers:

```python
@app.on_callback_query(filters.regex(r"^admin_cookie_action:"))
async def handle_cookie_action(client, callback_query):
    data = callback_query.data                 # "admin_cookie_action:ytcookies:replace"
    parts = data.split(":")
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

#!/usr/bin/env python3
"""Agent-side helper: interact with the bot as the operator's user account.

Loads the git-ignored Telethon session string (produced by telethon_login.py)
and sends a private message to the bot, printing replies for a few seconds.
Usage:
    python3 tools/telethon_bot.py "your message or link"
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from telethon import TelegramClient  # noqa: E402
from telethon.events import NewMessage  # noqa: E402
from telethon.sessions import StringSession  # noqa: E402

SESSION_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "telethon_session.txt",
)
BOT_USERNAME = "AngelaBalzac_bot"
WATCH_SECONDS = 20


async def main(text: str, press: str | None = None, watch: int = WATCH_SECONDS) -> None:
    if not os.path.exists(SESSION_FILE):
        print(f"ERROR: {SESSION_FILE} missing — run tools/telethon_login.py first.")
        sys.exit(1)
    with open(SESSION_FILE) as f:
        session_string = f.read().strip()

    client = TelegramClient(StringSession(session_string), config.API_ID, config.API_HASH)
    await client.connect()
    me = await client.get_me()
    print(f"Connected as {me.first_name} (id={me.id})")

    async with client:
        bot = await client.get_entity(BOT_USERNAME)
        print(f"Bot resolved: {getattr(bot, 'username', bot.id)}")

        @client.on(NewMessage(chats=bot, incoming=True))
        async def on_reply(event):
            print(f"\n[bot reply] {event.raw_text!r}")
            if press and event.buttons:
                for row in event.buttons:
                    for b in row:
                        label = getattr(b, "text", "")
                        data = (b.data or b"").decode("utf-8", "replace")
                        if press in label or press in data:
                            print(f"\n>>> pressing button {label!r} (data={data!r})")
                            await b.click()
                            return

        await client.send_message(bot, text)
        print(f"Sent: {text!r}\n--- waiting up to {watch}s for replies ---")

        end = asyncio.get_event_loop().time() + watch
        while asyncio.get_event_loop().time() < end:
            await asyncio.sleep(1)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Interact with the bot via Telethon")
    parser.add_argument("message", help="message or link to send to the bot")
    parser.add_argument("--press", help="substring of an inline button to press", default=None)
    parser.add_argument("--watch", type=int, default=WATCH_SECONDS, help="seconds to watch replies")
    args = parser.parse_args()
    asyncio.run(main(args.message, args.press, args.watch))

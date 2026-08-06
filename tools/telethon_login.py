#!/usr/bin/env python3
"""One-time interactive Telethon login for the operator's account.

Run it, follow the prompts (phone, code, optional 2FA password), and it stores a
Telethon StringSession in a git-ignored file. That file is what the agent-side
scripts load to interact with the bot as the operator.

No secrets are hardcoded here: api_id / api_hash come from config.py (.env).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from telethon.sessions import StringSession  # noqa: E402
from telethon import TelegramClient  # noqa: E402

SESSION_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "telethon_session.txt",
)


def main() -> int:
    if not config.API_ID or not config.API_HASH:
        print("ERROR: API_ID / API_HASH missing from .env (see .env.example).")
        return 1

    client = TelegramClient(StringSession(), config.API_ID, config.API_HASH)

    async def run():
        await client.start()
        me = await client.get_me()
        print(f"\nLogged in as: {me.first_name} (id={me.id})")
        if me.bot:
            print("WARNING: you logged in with a BOT token. The agent needs a USER account.")
            return False
        session_string = client.session.save()
        with open(SESSION_FILE, "w") as f:
            f.write(session_string)
        print(f"Session string saved to: {SESSION_FILE}")
        print("This file is git-ignored. Keep it secret.")
        return True

    with client:
        ok = client.loop.run_until_complete(run())

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

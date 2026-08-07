#!/usr/bin/env python3
"""Agent-side test driver: drives the bot through feature scenarios via Telethon.

Acts as the operator's user account, sending links to the bot, pressing inline
buttons (matched by label substring OR callback data substring, with `*`
wildcards supported in the data pattern), and reporting which messages/media
came back. Handles BOTH new messages and edits (playlist tier keyboards etc).

Usage:
    python3 tools/telethon_drive.py --url <URL> --press 'pl:*:whole' --press 'pl:*:vl' --timeout 240
    python3 tools/telethon_drive.py --url <URL> --pick v --size-min-mb 10 --size-max-mb 100 --timeout 130
    python3 tools/telethon_drive.py --message "hello"
"""
import asyncio
import argparse
import fnmatch
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from telethon import TelegramClient  # noqa: E402
from telethon.events import NewMessage, MessageEdited  # noqa: E402
from telethon.sessions import StringSession  # noqa: E402

SESSION_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "telethon_session.txt",
)
BOT_USERNAME = "AngelaBalzac_bot"
DEFAULT_TIMEOUT = 180


def _parse_size_mb(label: str) -> float | None:
    import re
    m = re.search(r"\(\s*~?([\d.]+)\s*([KMG])\)", label)
    if not m:
        return None
    val = float(m.group(1))
    mult = {"K": 1 / 1024, "M": 1, "G": 1024}
    return val * mult[m.group(2)]


def media_summary(m):
    if m.video:
        return {"type": "video", "size": m.video.size, "w": m.video.w, "h": m.video.h, "duration": m.video.duration}
    if m.audio:
        return {"type": "audio", "size": m.audio.size, "duration": m.audio.duration}
    if m.document:
        return {"type": "document", "size": m.document.size}
    if m.photo:
        return {"type": "photo"}
    return None


def _matches(pat, label, data):
    if not pat:
        return False
    if pat in label:
        return True
    # data patterns may use '*' wildcards for the cache id (changes per link)
    if "*" in pat:
        return fnmatch.fnmatch(data, pat) or fnmatch.fnmatch(data, f"*{pat}")
    return pat in data


async def main(args):
    if not os.path.exists(SESSION_FILE):
        print(json.dumps({"ok": False, "error": "session missing"}))
        sys.exit(1)
    with open(SESSION_FILE) as f:
        session_string = f.read().strip()

    client = TelegramClient(StringSession(session_string), config.API_ID, config.API_HASH)
    await client.connect()
    me = await client.get_me()
    print(json.dumps({"connected_as": me.first_name, "id": me.id}))

    replies = []

    async with client:
        bot = await client.get_entity(BOT_USERNAME)
        print(json.dumps({"bot": getattr(bot, "username", bot.id)}))

        press_index = [0]

        def make_handler(event_cls):
            async def handler(event):
                entry = {
                    "id": event.id,
                    "text": event.raw_text,
                    "media": media_summary(event),
                    "reply_to": event.message.reply_to_msg_id,
                    "buttons": [],
                    "is_edit": event_cls is MessageEdited,
                }
                if event.buttons:
                    for row in event.buttons:
                        for b in row:
                            entry["buttons"].append({
                                "text": getattr(b, "text", ""),
                                "data": (b.data or b"").decode("utf-8", "replace"),
                            })
                replies.append(entry)
                print(json.dumps({"msg": entry}))

                # Sequential presses: each --press matches the NEXT button that
                # satisfies it (whether the keyboard is a new message or an edit).
                if args.press and press_index[0] < len(args.press):
                    pat = args.press[press_index[0]]
                    for row in event.buttons or []:
                        for b in row:
                            label = getattr(b, "text", "")
                            data = (b.data or b"").decode("utf-8", "replace")
                            if _matches(pat, label, data):
                                print(json.dumps({"press": label, "data": data, "pattern": pat}))
                                # Increment BEFORE the click: the bot usually edits
                                # the menu in response, and that edit event can be
                                # dispatched while `await b.click()` is pending. With
                                # the index already advanced, the edit is handled with
                                # the NEXT pattern instead of being consumed early.
                                press_index[0] += 1
                                await b.click()
                                return

                # Auto-pick of a media format (single-video keyboard).
                if args.pick and event.buttons and press_index[0] == 0:
                    for row in event.buttons:
                        for b in row:
                            label = getattr(b, "text", "")
                            data = (b.data or b"").decode("utf-8", "replace")
                            kind = "v" if (data.startswith("dl:") and ":v:" in data) else "a" if (data.startswith("dl:") and ":a:" in data) else ""
                            if kind != args.pick:
                                continue
                            size_mb = _parse_size_mb(label)
                            if args.size_min_mb and size_mb < args.size_min_mb:
                                continue
                            if args.size_max_mb and size_mb > args.size_max_mb:
                                continue
                            print(json.dumps({"pick": label, "data": data, "size_mb": size_mb}))
                            await b.click()
                            return
            return handler

        client.add_event_handler(make_handler(NewMessage), NewMessage(chats=bot, incoming=True))
        client.add_event_handler(make_handler(MessageEdited), MessageEdited(chats=bot, incoming=True))

        if args.message:
            await client.send_message(bot, args.message)
            print(json.dumps({"sent": args.message}))
        elif args.url:
            await client.send_message(bot, args.url)
            print(json.dumps({"sent_url": args.url}))

        deadline = asyncio.get_event_loop().time() + args.timeout
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(1)

    media_delivered = [r for r in replies if r["media"]]
    result = {
        "ok": bool(media_delivered),
        "presses_done": press_index[0],
        "replies": len(replies),
        "media_delivered": media_delivered[-1] if media_delivered else None,
        "last_text": replies[-1]["text"][:120] if replies else None,
        "all_replies": replies,
    }
    print(json.dumps({"RESULT": result}))
    await client.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Drive the bot via Telethon")
    parser.add_argument("--message", help="plain text message to send", default=None)
    parser.add_argument("--url", help="link to send", default=None)
    parser.add_argument("--press", help="substring of inline button label/data to press (repeat for a chain; '*' = wildcard)", action="append", default=None)
    parser.add_argument("--pick", help="auto-pick first media button of kind 'v' or 'a' matching size range", default=None)
    parser.add_argument("--size-min-mb", type=float, help="min size in MB for --pick", default=None)
    parser.add_argument("--size-max-mb", type=float, help="max size in MB for --pick", default=None)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()
    asyncio.run(main(args))

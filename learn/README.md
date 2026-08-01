# Learn Python by Building This Bot

This folder is a **from-scratch Python course** built entirely around the `tgbot`
project in this repository. Instead of abstract toy examples, every lesson shows
the real code that powers the Telegram downloader/streamer bot and explains the
Python feature behind it.

By the end you will be able to read `main.py`, `utils/downloader.py`,
`modules/admin.py`, `modules/direct_forward.py` and friends, add your own
features, and build similar Telegram bots independently — **without an LLM
doing it for you**.

## Who this is for

You already know a little: `for` loops, `input()`, `print()`. You are now
exploring `import sys`, `import os`, dictionaries and the `datetime` library.
This course starts a notch below that (so nothing is assumed) and climbs all the
way to understanding the whole bot.

## How to study

1. Read a lesson top to bottom.
2. Run the tiny example snippets on your own machine (`python3 lesson_snippet.py`).
3. Open the **"Where this shows up in tgbot"** section and jump to that file in
   the repo to see the real, production use.
4. Do the exercise at the bottom (solutions in `exercises/`).
5. Move to the next lesson. Lessons are numbered; do them in order.

## The path

| # | Lesson | Python concept | Bot feature it explains |
|---|--------|----------------|--------------------------|
| 01 | [Variables, print, input](01-variables-print-input.md) | names, output, input | how the bot reads its first message |
| 02 | [Data types](02-data-types.md) | int/str/bool/None | why `BOT_TOKEN` is a string |
| 03 | [Numbers & strings](03-numbers-strings.md) | math, f-strings | progress bars, file sizes |
| 04 | [Lists & tuples](04-lists-tuples.md) | sequences | cookie jar lists, format buttons |
| 05 | [Dictionaries](05-dictionaries.md) | key→value maps | `COOKIE_MAP`, config, state |
| 06 | [if / elif / else](06-conditionals.md) | branching | the strategy ladder |
| 07 | [Loops: for & while](07-loops.md) | iteration | iterating formats, cache sweep |
| 08 | [Functions](08-functions.md) | reuse, params | `extract_formats()`, `download_media()` |
| 09 | [Modules & import](09-modules-import.md) | files as libraries | how `config.py` is shared |
| 10 | [stdlib: os, sys, datetime](10-stdlib.md) | the standard library | cookie folders, disk space, timestamps |
| 11 | [Files & JSON](11-files-json.md) | reading/writing | `.env`, `direct_forward_state.json` |
| 12 | [Exceptions (try/except)](12-exceptions.md) | error handling | defensive cookie writes |
| 13 | [Classes & OOP](13-classes.md) | objects | `Client`, `CallbackQuery` |
| 14 | [Async / await](14-async.md) | concurrency | `async def`, `asyncio` |
| 15 | [Telegram bots with pyrogram](15-pyrogram.md) | the framework | handlers & filters |
| 16 | [Anatomy of tgbot](16-anatomy.md) | whole-system view | the file map |
| 17 | [Reading real code](17-reading-code.md) | navigation | tracing one download |
| 18 | [Build a feature](18-build-feature.md) | applied | adding a `/ping` command |
| 19 | [Deploy on a VPS](19-deploy.md) | systemd, env | `run.sh`, `tgbot.service` |

## Ground rules

- Every snippet is real, runnable Python 3.
- File references use `path:line` so you can `grep -n` straight to the source.
- Nothing here is copied from the internet — it is written against *this* repo.

Start at [01-variables-print-input.md](01-variables-print-input.md).

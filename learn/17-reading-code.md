# Lesson 17 — Reading real code

The single most valuable skill you can learn. Here is a repeatable method for
diving into a codebase you did not write.

## Step 1: Find the entry point

Every repo has one. Ours is `main.py`, which calls `main_engine()`. Read it
once with fresh eyes.

## Step 2: Follow the data

Pick one user action (e.g. "send a YouTube link") and trace the data through
the code:

1. **Message arrives** → `modules/downloader_handler.py` `is_link()` returns
   `True` → the link handler picks it up.
2. **Format fetch** → `show_format_selection()` → `extract_formats(url)` →
   builds keyboard → waits for a tap.
3. **Download** → `download_media(url, format_id, ...)` → yt-dlp downloads →
   `process_split_and_upload` sends the file.
4. **Cleanup** → `shutil.rmtree(task_dir)` removes the cache folder.

Use `grep -n "def extract_formats" utils/downloader.py` to jump to the
function, then read it. When it calls another function you do not know
(`_apply_pot_options`), grep for that name too.

## Step 3: Understand the *why*, not just the *what*

Every line is there for a reason. Ask why:

- Why does `get_cookies_for_url` copy the file to a snapshot before use?
  → Because yt-dlp rewrites jars on exit and we do not want the admin's
    original corrupted.
- Why is `initialize_cookie_jars` called before `app.start()`?
  → Because we want to fail loudly if cookies are unreadable *before* the bot
    goes online and someone sends a link.
- Why is the Instagram strategy `no-auth` first?
  → Because cookies trigger HTTP 400 on Instagram's authenticated API when the
    session is stale — we learned this from the bug in the log.

## Step 4: Draw the call graph

A piece of paper with arrows like
`extract_formats → get_cookies_for_url → _cookie_snapshot` helps more than
re-reading the code.

## Step 5: Read the tests (if any) or make one

This repo has no test suite, but you can run the bot locally (with a valid
`.env`) and watch `logs/bot.log` while clicking buttons — that is your test.

## Step 6: When stuck, read one level deeper

If `download_media` is confusing, read `_cookie_snapshot` and
`_apply_pot_options` until the fog lifts.

## Where this shows up in tgbot

The `docs/memory/` folder is literally this process: it captures decisions so
you do not have to reverse-engineer them:

- `tgbot-cookie-protection-and-monitor.md` explains why cookies are locked.
- `tgbot-ytdlnis-size-approach.md` explains the size-mismatch fix.
- `AGENTS.md` lists the invariants (the "do not break these" list).

Read those before touching the related code.

## Exercise

Pick a feature you do not understand (e.g. "how does the PO-token provider
work?"). Open `utils/pot_provider.py` and write a 5-line summary of what it
does, using only `grep`, `read`, and the method above. Compare your summary
with `docs/memory/` (or `AGENTS.md`) for the same feature.

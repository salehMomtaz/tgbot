# Cookie protection & tamper monitor

tgbot cookie corruption is solved. Two things were wrong and both are fixed
(commit `b44db54` + earlier `e326794`, then the in-memory read fix `af7fa77`):

1. **`.download()` AttributeError** (`e326794`, then `af7fa77`) — pyrogram v2
   wants `client.download_media(message=..., in_memory=True)`, not
   `client.download()`. The returned `BytesIO` has its cursor at EOF, so the
   upload handler must read it with `.getvalue()` (not `.read()`, which returns
   empty bytes). This was the past "jar replace did not work / overwrote to
   empty" symptom: the failed document handler never wrote the jar at all.

2. **Text-paste truncation** (the real remaining bug vs balebot) — Telegram
   silently truncates text messages at **4096 chars**, but a YouTube Netscape jar
   is ~17 KB. tgbot *allowed* pasting the jar as text; balebot blocks this and
   requires a `.txt` document. Fixed: tgbot now rejects text-paste in the Replace
   state and requires a document, validates ≥1 real cookie line (≥7 tab-separated
   fields), writes atomically (temp + fsync + `os.replace`), auto-backs up to
   `<file>.autobak`, re-`chmod 444`s ytcookies, and purges snapshots.

**Why yt-dlp can't corrupt the original jar:** `utils/downloader.py`
(`get_cookies_for_url` → `_cookie_snapshot`) always points yt-dlp at a disposable
copy in `cache/cookies/<basename>.snapshot`. yt-dlp rewrites *that* snapshot on
exit, never the original. The original `cookies/youtube/ytcookies.txt` is locked `0o444` at
startup (`main.py`) and after every Replace. Verified by running a full extraction
through the bot's exact code path: original md5 unchanged.

**Tamper monitor on the test VPS:** `cookie-watch.service` (systemd, enabled for
reboot, runs as the VPS user) runs `<repo>/tools/cookie_watch.sh`. It watches the
parent *directories* (`<repo>` and `<repo>/cache/cookies`) with `inotifywait` and
logs timestamp/event/file/size/md5 + the running bot processes to
`<repo>/logs/cookie_watch.log`. Watch dirs (not files) because `os.replace`
unlinks the inode, which would blind a file watch.

**Why not auditd:** `auditd` is **inert on the test VPS host** (a
container/LXC-style kernel restricts the audit netlink — `auditctl -w` looks
accepted but `ausearch` returns 0 events on real changes). Don't rely on auditd
there; use inotifywait. There may be a leftover auditd watch rule; it's harmless.

Both bots coexist on the same test VPS: balebot's POT provider on
`127.0.0.1:4416`, tgbot's on `127.0.0.1:4417`. See
[VPS two-bots runtime state](vps-two-bots-runtime-state.md) and
[tgbot ↔ balebot integration](tgbot-balebot-integration.md).

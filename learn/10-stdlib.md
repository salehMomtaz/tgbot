# Lesson 10 — Standard library: os, sys, datetime

The "standard library" ships with Python. You already met `import os` and
`import sys`. Here is what the bot actually uses them for.

## os — talk to the operating system

Path and file operations. Note: the bot prefers `os.path` (or `pathlib`) over
hardcoded separators like `/`.

```python
import os

os.path.exists("cookies/instagram/igcookies.txt")  # file there?
os.path.getsize("file.txt")                          # bytes
os.path.join("cookies", "ytdlp", "reddit.txt")      # safe path join
os.makedirs("cookies/tiktok", exist_ok=True)        # create dir
os.listdir("cache")                                 # list files
os.remove("tmp.txt")                                # delete
os.chmod("ytcookies.txt", 0o444)                   # read-only
os.rename(old, new)                                # move
shutil.copy(src, dst)                              # copy (needs import shutil)
```

The `0o444` is *octal* (starts with `0o`) — the Unix permission `r--r--r--`.
The bot uses it to lock cookie files so yt-dlp cannot corrupt them:

```python
os.chmod(config.YT_COOKIES, 0o444)
```

## sys — the running interpreter

```python
import sys
sys.argv        # command-line args: ["main.py", "--debug"]
sys.exit(1)     # terminate the program
sys.path        # the import search path (see lesson 09)
```

The bot rarely uses `sys.argv` directly (it reads config from `.env`), but you
will see `sys` in debugging and packaging.

## datetime — dates and times

```python
from datetime import datetime, timedelta

now = datetime.now()
print(now.strftime("%Y-%m-%d %H:%M:%S"))   # 2026-07-30 18:33:23
later = now + timedelta(hours=1)
```

The log timestamps in `logs/bot.log` are produced with exactly this format.

## Where this shows up in tgbot

`utils/downloader.py` uses `os` heavily for cookie snapshots:

```python
import os

snap_path = os.path.join(_COOKIE_SNAPSHOT_DIR, f"{os.path.basename(original_path)}.snapshot")
os.makedirs(_COOKIE_SNAPSHOT_DIR, exist_ok=True)
shutil.copy(original_path, snap_path)
```

`utils/updater.py` checks the file mtime with `os.path.getmtime` to decide if a
cache file is fresh:

```python
now = time.time()
threshold = now - (max_age_hours * 3600)
if entry.stat().st_mtime < threshold:
    os.remove(entry.path)
```

And disk space via `shutil.disk_usage` (a friendly wrapper over `os.statvfs`):

```python
usage = shutil.disk_usage(os.getcwd())
free_gb = usage.free / (1024 ** 3)
```

## Exercise

1. Write a snippet that creates a folder `tmp_demo`, writes `hello` into
   `tmp_demo/a.txt`, then prints its size.
2. Compute what time it will be 90 minutes from now and print it as
   `HH:MM` using `strftime`.
3. Use `os.path.join` to build the path `cookies/ytdlp/reddit.txt` and
   `print` it (no need to actually create it).

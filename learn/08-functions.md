# Lesson 08 — Functions

A function is a named block of code you can run with different inputs.

```python
def greet(name: str, loud: bool = False) -> str:
    msg = f"Hello, {name}!"
    return msg.upper() if loud else msg

print(greet("dev"))             # Hello, dev!
print(greet("dev", loud=True))  # HELLO, DEV!
```

## Parameters vs arguments

- **parameters** = `name`, `loud` — what the function declares.
- **arguments** = `"dev"`, `True` — what you pass when calling.

## Defaults make options optional

Anything after `=` in the parameter list is optional. Callers can skip them or
override with keyword args:

```python
greet("dev")                 # uses default loud=False
greet("dev", loud=True)      # keyword overrides
greet(loud=True, name="dev") # order does not matter with keyword args
```

## What does a function return?

Whatever is after `return`. If there is no `return` (or a bare `return`),
the function returns `None`.

```python
def add(a, b):
    return a + b    # returns the number

def log(msg):
    print(msg)       # returns None
```

## Where this shows up in tgbot

Every feature in the bot is a function. Here is
`utils/downloader.py::get_cookies_for_url` — it takes a URL, picks the right
cookie file, and returns a path (or `None`):

```python
def get_cookies_for_url(url: str) -> str | None:
    url_lower = url.lower()
    if "youtube.com" in url_lower or "youtu.be" in url_lower:
        cookie_path = config.YT_COOKIES
    elif "instagram.com" in url_lower:
        cookie_path = config.IG_COOKIES
    # ...
    return _cookie_snapshot(cookie_path)
```

Notice the `str | None` return type. `|` means "either of". The caller must
handle the `None` case, typically with `if cookie_path:`.

And `download_media()` takes *a lot* of optional parameters — every one that
is not in a typical YouTube download has a sensible default:

```python
def download_media(
    url: str, format_id: str | None = None, format_type: str = 'v',
    cache_id: str | None = None, progress_fn=None, format_selector: str | None = None,
    max_height: int | None = None, best_audio_format_id: str | None = None,
) -> dict:
```

## Exercise

Write a function `format_progress(current: int, total: int) -> str` that
returns a string like `"[##########          ] 45%"`.

1. Compute `pct = current / total` (guard against `total == 0`).
2. Fill a 20-char bar with `#` proportional to `pct`.
3. Return something like `[####                ] 20%`.

Compare your result to `main.py::progress_bar_handler`.

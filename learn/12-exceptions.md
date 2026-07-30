# Lesson 12 — Exceptions (try/except)

When something goes wrong, Python raises an **exception**. If nobody catches
it, the program crashes. `try/except` lets you handle errors gracefully.

```python
try:
    value = int("not_a_number")
except ValueError:
    value = 0
    print("That was not a number, using 0 instead")
```

## The full structure

```python
try:
    risky_thing()
except FileNotFoundError:
    print("file missing")
except (ValueError, TypeError):
    print("bad value/type")
except Exception as e:
    print(f"unexpected: {e}")
else:
    print("ran fine")
finally:
    print("always runs, error or not")
```

- `except Exception` catches almost anything — but be specific when you can.
- `finally` is for cleanup (closing files, releasing locks).
- The `as e` part gives you the error message via `str(e)`.

## Raising your own

```python
def divide(a, b):
    if b == 0:
        raise ValueError("Cannot divide by zero")
    return a / b
```

## Where this shows up in tgbot

Every yt-dlp call is wrapped because downloads fail for many reasons:

```python
try:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
except Exception as e:
    error_msg = str(e).lower()
    if "requested format" in error_msg and "not available" in error_msg:
        # fallback to generic best-effort selectors
        ydl_opts['format'] = fallback_format
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    else:
        raise RuntimeError(_classify_ytdl_error(e, url))
```

This is a **retry with fallback** pattern: try the specific format; if it fails
with "not available", try a generic best-effort format; if *that* fails, wrap
the error in a human-readable message and re-raise.

`_classify_ytdl_error` turns cryptic yt-dlp errors into actionable messages:

```python
def _classify_ytdl_error(exc: Exception, url: str) -> str:
    text = str(exc).lower()
    if _is_sign_in_error(text):
        site, jar = _site_cookie_context(url)
        return f"{site} is requiring sign-in..."
    if "no video formats found" in text:
        return "The video has no playable formats..."
    return str(exc)
```

## Exercise

Write a function `safe_read_int(prompt)` that calls `input(prompt)`, tries to
convert the answer to `int`, and returns it. If the user types something that
is not a number, print a friendly error and ask again. Loop until you get a
valid int.

Test it: type "abc", then "12.5", then "42".

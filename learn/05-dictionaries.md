# Lesson 05 — Dictionaries

A **dictionary** maps *keys* to *values*, like a real dictionary maps words to
definitions. You are exploring this now — good, it is one of the most important
types in this bot.

```python
user = {
    "name": "Saleh",
    "id": 7665239058,
    "premium": False,
}
```

## Reading and writing

```python
user["name"]            # "Saleh"
user["id"]              # 7665239058
user["age"] = 21        # add a new key
user.get("email", "n/a")  # safe read with default
"name" in user          # True
del user["premium"]     # remove a key
```

The `.get(key, default)` form is *critical*: plain `user["missing"]` raises
`KeyError`. `.get` returns the default instead.

## Looping over a dict

```python
for key in user:           # keys
    print(key)

for value in user.values(): # values
    print(value)

for key, value in user.items():  # pairs
    print(f"{key} -> {value}")
```

## Nesting

Dictionaries hold lists, lists hold dicts, dicts hold dicts — this nesting is
how JSON-style data works. `auto_forward_state.json` is literally
`{"instagram": ["id1", "id2"], "tiktok": [...], "x": [...]}`.

## Where this shows up in tgbot

The cookie jar menu in `modules/admin.py` is a dict mapping a short key to a
file path:

```python
COOKIE_MAP = {
    "ytcookies": config.YT_COOKIES,
    "igcookies": config.IG_COOKIES,
    "ttcookies": config.TT_COOKIES,
    "xcookies": config.X_COOKIES,
    "cookies": config.COOKIES_FILE,
}
```

When the bot receives `admin_cookie_select:igcookies`, it splits the string,
takes `igcookies`, and looks up `COOKIE_MAP["igcookies"]` to find the file.

The format keyboard options are dicts too (`modules/downloader_handler.py`):

```python
video_options.append({
    'format_id': fmt['format_id'],
    'quality': f"{resolution}p",
    'bytes': size,
    'height': resolution,
    'exact': exact,
})
```

## Exercise

Create a dict `bot_stats` with keys `"downloads"` (int), `"errors"` (int),
and `"users"` (list of strings). Write a function `record_error()` that
increments `"errors"` by 1. Call it three times and print the dict.

Then add a user `"dev"` to the `"users"` list. Print the final dict.

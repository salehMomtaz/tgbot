# Lesson 11 — Files & JSON

## Reading a file

```python
with open("config.py", "r") as f:
    content = f.read()
```

`with` is a *context manager*: it opens the file, runs the block, and closes
the file even if an error happens. Always use `with` for files.

## Writing a file

```python
with open("state.json", "w") as f:
    f.write("hello")
```

`"w"` overwrites. `"a"` appends. `"r"` is read (default).

## JSON — a language for data

JSON is just text, but it is the universal format for APIs and config.

```python
import json

data = {"downloads": 42, "users": ["dev"]}
text = json.dumps(data)         # dict -> string
back = json.loads(text)          # string -> dict
```

`json.dumps` converts a Python dict/list to a string. `json.loads` goes the
other way. `json.dump(obj, file)` and `json.load(file)` work with files
directly.

## Where this shows up in tgbot

`modules/direct_forward.py` remembers which Instagram/TikTok/X posts it has
already sent by writing a JSON file:

```python
def _save_state(state: dict[str, set[str]]) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({k: sorted(v) for k, v in state.items()}, f)
    os.replace(tmp, STATE_FILE)
```

Why the `.tmp` + `os.replace`? It is the "atomic write" pattern: if the bot is
killed mid-write, the original file is never half-written. This is the same
defense that `_write_cookie_jar` in `modules/admin.py` uses:

```python
tmp_path = f"{file_path}.tmp.{os.getpid()}"
with open(tmp_path, "w", encoding="utf-8") as f:
    f.write(normalized)
    f.flush()
    os.fsync(f.fileno())
os.replace(tmp_path, file_path)
```

`os.fsync` forces the OS to flush the write to disk before the `os.replace`.
That is how the bot survives a sudden reboot without corrupting cookies.

## Exercise

1. Create a dict `state = {"instagram": ["id1"], "tiktok": [], "x": []}`.
2. Convert it to JSON and write it to `state.json`.
3. Read it back with `json.load`.
4. Add `"id2"` to the `instagram` list and save again.
5. Print the final state.

This is *exactly* what `direct_forward_state.json` does every time the bot sends
a forwarded video.

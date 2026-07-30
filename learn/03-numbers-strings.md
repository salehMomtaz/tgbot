# Lesson 03 — Numbers & Strings (and f-strings)

## Numbers: do math

```python
size_mb = 1500 / 1024
print(size_mb)        # 1.46484375
print(int(size_mb))    # 1
print(round(size_mb, 1))  # 1.5
```

Integer division uses `//` and remainder uses `%`:

```python
bytes_per_minute = total_bytes // 60
is_even = (n % 2) == 0
```

## Strings: do text

Strings have many built-in methods. You only use these five most of the time:

```python
url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
url.lower()          # lowercase
url.startswith("h")  # True
"youtube" in url     # True
url.split("/")       # ['https:', '', 'www.youtube.com', ...]
url.strip()          # remove leading/trailing whitespace
```

## f-strings: embed values in text

The `f"..."` syntax lets you put any Python expression *inside* a string.
This is the single most important string feature to master.

```python
name = "Saleh"
size = 150
print(f"Hello {name}, the file is {size} MB")
# Hello Saleh, the file is 150 MB
```

You can put any expression in the braces:

```python
print(f"{5 + 3}")             # 8
print(f"{'hi'.upper()}")      # HI
print(f"{3.14159:.2f}")       # 3.14   (format specifier)
```

## Where this shows up in tgbot

`utils/downloader.py::format_size_short` is a compact example of both math
and f-strings doing a real job:

```python
def format_size_short(size_bytes: int) -> str:
    if size_bytes <= 0:
        return "??"
    size_mb = size_bytes / (1024 * 1024)
    if size_mb >= 1024:
        return f"{round(size_mb / 1024, 1)}G"
    return f"{int(size_mb)}M"
```

Notice: `:` inside the braces is a format specifier (`.1f` = "one decimal
place"), `round()` returns a float, and `int()` drops decimals.

And the progress bar in `main.py` is pure f-string work:

```python
text = (
    f"`[{bar_str}]` {percentage:.1f}%\n"
    f"📦 `{current_mb} MB / {total_mb} MB`"
)
```

## Exercise

Rewrite `format_size_short` to also handle kilobytes: files under 1 MB should
show as `327K`. Test it with `100_000` (≈98K) and `1_500_000` (≈1M).

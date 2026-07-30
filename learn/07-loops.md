# Lesson 07 — Loops: for & while

## for loops: iterate over a sequence

```python
for i in range(5):        # 0, 1, 2, 3, 4
    print(i)

for fruit in ["apple", "banana"]:
    print(fruit)

for index, value in enumerate(["a", "b", "c"]):
    print(index, value)
```

`for` always goes over *something*: a `range`, a list, a string's characters,
a dict's keys, a file's lines. You already know this — it is where your course
started.

## while loops: repeat until a condition breaks

```python
retries = 0
while retries < 3:
    try:
        do_something()
        break
    except Exception:
        retries += 1
```

## break and continue

- `break` exits the loop immediately.
- `continue` skips to the next iteration.

```python
for fmt in formats:
    if fmt.get('format_note') == 'storyboard':
        continue  # skip storyboards
    process(fmt)
```

## List comprehensions: a one-line for

The most Pythonic way to build a list:

```python
heights = [v['height'] for v in video_options]
# equivalent to:
heights = []
for v in video_options:
    heights.append(v['height'])
```

With a filter:

```python
non_storyboard = [f for f in formats if f.get('format_note') != 'storyboard']
```

## Where this shows up in tgbot

`utils/downloader.py::extract_formats` deduplicates video resolutions:

```python
unique_videos = []
seen_heights = set()
for v in video_options:
    if v['height'] not in seen_heights:
        unique_videos.append(v)
        seen_heights.add(v['height'])
```

And `main.py::auto_clean_cache_directory` is a `while True` loop:

```python
while True:
    # sweep the cache directory
    await asyncio.sleep(3600)  # wait 1 hour
```

A comprehension deduplicates formats:

```python
non_storyboard = [
    f for f in formats
    if f.get("format_note") != "storyboard" and f.get("ext") != "mhtml"
]
```

## Exercise

Given `formats = [{"h": 1080}, {"h": 720}, {"h": 1080}, {"h": 480}, {"h": 720}]`:

1. Write a for-loop that builds a list of unique heights in order.
2. Rewrite it as a list comprehension (hint: you may need a helper `set`).
3. Write a `while` loop that pops items from the list until it is empty,
   printing each one.

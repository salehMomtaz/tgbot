# Lesson 06 — if / elif / else

Branching: run this block, otherwise that block.

```python
if size_mb > 2000:
    print("too big for Bot API")
elif size_mb > 500:
    print("large but OK")
else:
    print("small file")
```

Rules:
- `if` is always first, `else` is always last, `elif` is in between.
- Indentation matters (4 spaces). A block ends when indentation drops.
- Conditions are expressions that evaluate to `True` or `False`.

## Truthiness (this trips up every beginner)

Some values are "falsy" even though they are not `False`:

```python
bool(0)      # False
bool("")     # False
bool([])     # False
bool(None)   # False
bool(0.0)    # False
```

Everything else is truthy. So `if cookie_path:` means "if cookie_path is not
None and not empty".

## Where this shows up in tgbot

The strategy ladder in `utils/downloader.py::extract_formats` is a big
if/elif/else deciding which auth strategy to try:

```python
if _is_youtube(url):
    strategies = [("cookies+pot", True)]
elif "instagram.com" in url.lower():
    strategies = [("no-auth", None)]
    if cookie_path:
        strategies.append(("cookies", False))
else:
    strategies = []
    if cookie_path:
        strategies.append(("cookies", False))
    strategies.append(("no-auth", None))
```

Notice the nested `if cookie_path:` inside the `elif` and `else` branches —
this is the exact pattern that prevents the Instagram 400 error you saw in the
logs: Instagram goes no-auth first because cookies trigger HTTP 400.

## Exercise

Write a function `classify_size(bytes_count)` that returns:
- `"huge"` if > 2 GB
- `"large"` if > 100 MB
- `"medium"` if > 1 MB
- `"small"` otherwise

Test it with 5 values and print the results.

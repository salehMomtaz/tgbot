# Lesson 04 — Lists & Tuples

A **list** is an ordered, changeable collection. A **tuple** is the same but
unchangeable.

```python
fruits = ["apple", "banana", "cherry"]   # list
colors = ("red", "green", "blue")        # tuple
```

`tuple` has a tiny extra bit of syntax: a 1-tuple is `("only",)` — the comma
matters.

## What you can do with a list

```python
nums = [3, 1, 4, 1, 5, 9, 2, 6]

nums.append(7)       # add to end -> [3, 1, 4, 1, 5, 9, 2, 6, 7]
nums.pop()           # remove last -> [3, 1, 4, 1, 5, 9, 2, 6]
nums.remove(1)       # remove first 1 -> [3, 4, 1, 5, 9, 2, 6]
nums.sort()          # in-place sort
len(nums)            # length
nums[0]              # first
nums[-1]             # last
nums[2:5]            # slice (2, 3, 4)
nums[::-1]           # reversed
"x" in nums          # membership
```

## Slicing is the power move

`list[start:stop:step]`. Defaults are `start=0`, `stop=len`, `step=1`.
So `nums[2:5]` is the same as `nums[2:5:1]` — items at index 2, 3, 4.

## Where this shows up in tgbot

`utils/downloader.py` returns a list of format options:

```python
return {
    'title': info.get('title', 'Unknown Title'),
    'videos': unique_videos[:5],   # first 5 only
    'audios': unique_audios[:5],
    'best_audio_format_id': best_audio_format_id,
}
```

`[:5]` is a slice that takes the first five items. If `unique_videos` is
shorter than 5, the slice is a no-op (no error).

And in `modules/direct_forward.py`, the per-platform inbox items are a list:

```python
items = _extract_saved_or_liked(platform, username, max_items)
new_items = [i for i in items if i["id"] not in state[platform]]
```

`[... for ... if ...]` is a *list comprehension* — a one-line loop that
builds a list. Lesson 7 covers it in detail.

## Exercise

You have a list of YouTube format strings like
`["1080p", "1080p", "720p", "480p", "720p", "1080p", "1080p"]`.

Build a new list that contains each resolution *once*, in the order it first
appeared. Print it.

(Hint: a `set` is fast for "seen?", but it loses order. Use a list + `not in`.)

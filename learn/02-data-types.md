# Lesson 02 — Data types

Python has a handful of built-in types. You will meet the same ones every day
for the rest of your Python career, so learn them now.

| Type | Example | What it is |
|------|---------|------------|
| `int` | `5`, `-100`, `0` | whole numbers |
| `float` | `3.14`, `0.0` | decimal numbers |
| `str` | `"hello"`, `'a'` | text |
| `bool` | `True`, `False` | truth / falsity |
| `NoneType` | `None` | "nothing here" |

## Checking a type

```python
x = 5
print(type(x))   # <class 'int'>

name = "tgbot"
print(type(name)) # <class 'str'>
```

## Type matters because operations differ

```python
5 + 5       # 10  (int addition)
"5" + "5"   # "55" (string concatenation!)
```

This is the #1 beginner trap: a value that *looks* like a number but is a
string does string things.

## Where this shows up in tgbot

`config.py` has a helper specifically because `.env` files give you strings,
but some config values must be integers:

```python
def get_env_int(key: str, default: int) -> int:
    val = os.getenv(key, "")
    if val.isdigit():
        return int(val)
    return default
```

Why this matters: `LOG_CHANNEL_ID` from `.env` is a string like `"-1001234567890"`.
If you tried to use it as an int without converting, comparisons and arithmetic
would silently do the wrong thing.

Meanwhile `BOT_TOKEN` is a string like `"7665239058:AAG..."` — it is never
converted because the Telegram API expects text.

## Exercise

1. Create `x = "10"` and `y = 20`. Print `x + y` — what happens? Fix it.
2. Look at `config.py` and find three variables. For each, guess the type.
   Use `type()` to check your guess (you can add `print(type(API_ID))`
   temporarily at the bottom of the file).

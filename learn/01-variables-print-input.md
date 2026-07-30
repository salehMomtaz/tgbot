# Lesson 01 — Variables, print, input

Everything in Python starts here. A **variable** is just a name that holds a
value. `print()` shows it. `input()` asks the user for it.

```python
name = input("What is your name? ")
print("Hello,", name)
```

## The three things you must internalise

1. **Assignment** — `x = 5` means "the name `x` now points to the value 5".
   It is *not* a math equation. `x = x + 1` is perfectly fine.
2. **Everything is a value** — numbers, strings, lists, even functions.
3. **Order matters** — Python runs top to bottom, one line at a time.

## Where this shows up in tgbot

The very first thing a user does is send the bot a link. `main.py` never calls
`input()` (Telegram delivers the message), but the idea is identical: the bot
*receives a value* (the message text) and *stores it* in a variable.

Look at `modules/downloader_handler.py`:

```python
def is_link(text: str) -> bool:
    return text.startswith("http://") or text.startswith("https://")
```

The parameter `text` is the variable that holds what the user typed — same
role as `name` in the toy example.

And `print` is everywhere in `main.py`:

```python
print("Telegram Bot Online.")
```

So even in a large async application the *mental model* from lesson 1 holds:
**get a value, put it in a name, use the name.**

## Exercise

Write a program that:
1. Asks the user for a YouTube URL with `input()`.
2. Prints `You sent: <url>`.
3. Prints `That looks like a link: True` if it starts with `http`.

Try it with a real link and with a random word. That is literally the job of
`is_link()` in the bot.

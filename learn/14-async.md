# Lesson 14 — async / await (concurrency)

When the bot has to wait for a network reply (download, upload, Telegram API
call), it can do something else while waiting. That is the entire point of
`async`. It is not parallel processing — it is **non-blocking I/O**.

## The shape

```python
import asyncio

async def fetch(url):
    # ... do network I/O ...
    return data

async def main():
    result = await fetch("https://example.com")
    print(result)

asyncio.run(main())
```

`async def` declares a coroutine. `await` suspends it until the inner call
finishes, letting the event loop do other work meanwhile.

## Why the bot uses it

While yt-dlp downloads a 500 MB video, pyrogram must still be able to receive
new messages from Telegram. If you used blocking code, the bot would be
*unresponsive* until the download finished.

```python
# Blocking — bad
result = ydl.extract_info(url)   # blocks event loop, no new messages

# Non-blocking — good
loop = asyncio.get_event_loop()
result = await loop.run_in_executor(None, ydl.extract_info, url)
```

`run_in_executor` pushes the blocking work onto a thread pool. The event loop
goes back to handling messages. When the download finishes, the result is
handed back to your coroutine.

## Tasks and gather

```python
# Run many coroutines concurrently
results = await asyncio.gather(task1(), task2(), task3())
```

## Where this shows up in tgbot

Every pyrogram handler is `async def`. The download worker uses
`run_in_executor` for yt-dlp work:

```python
loop = asyncio.get_event_loop()
data = await loop.run_in_executor(None, extract_formats, url)
```

And the FastAPI streaming server is run as a task:

```python
tasks = [
    server.serve(),
    auto_update_ytdlp(),
    auto_clean_cache_directory(),
]
await asyncio.gather(*tasks)
```

The bot lifecycle is one giant gather: server, updater, cache cleaner, and
auto-forward run *concurrently* without one blocking another.

## Exercise

Write an async function `slow_greet(name, delay)` that awaits
`asyncio.sleep(delay)` and returns `f"Hello, {name} (waited {delay}s)"`.

Then write an async `main()` that calls `slow_greet` three times in parallel
with different delays, using `asyncio.gather`. Print the three results.

Time the whole program with `time.perf_counter()` — it should take as long as
the *longest* delay, not the sum.

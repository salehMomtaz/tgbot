# tgbot
Beginning of my journy to build my dream python telegram bot, with help from the council of AIs, called MAGI System. 


Honorable memebers, Balthasar(OpenAI GPT5.5), Casper(DeepSeek V4 Pro), Melchior(Gemini 3.1 Pro), Sibyl System(Claude 4.8 Opus), Bifrost System(Grok 4.1 Fast), Angela Balsac(Minimax M2), Hohenzollern(Kimi 2.5).

Each member of the council will bare responsibility for a certain section of the bot. 

First we shall agree upon a defined roadmap, designating features, understanding limitations and implementing final code.

## Possible architecture of the bot:
We should be open to change when it's needed.
```
Telegram User
   │
   ▼
[ aiogram Bot Core ]  ──►  [ Plugin Router ]
   │                           ├─ downloader
   │                           ├─ uploader
   │                           ├─ file2link
   │                           ├─ shazam
   │                           └─ ...
   │
   ├─► [ Redis Queue ] ─► [ Worker: download/upload/transcode ]
   │
   ├─► [ PostgreSQL ] (jobs, users, links, audit)
   │
   └─► [ FastAPI Stream API ]  /d/{token}
              │
              └─► [ Telethon/Pyrogram Stream Worker ] -> Telegram file chunks -> HTTP client
```

## Structure

```
telegram-bot-platform/
├─ app/
│  ├─ main.py
│  ├─ config.py
│  ├─ bootstrap.py
│  │
│  ├─ core/
│  │  ├─ bot.py
│  │  ├─ dispatcher.py
│  │  ├─ middlewares/
│  │  └─ security/
│  │
│  ├─ features/
│  │  ├─ downloader/
│  │  │  ├─ plugin.py
│  │  │  ├─ handlers.py
│  │  │  ├─ service.py
│  │  │  └─ adapters/
│  │  │     ├─ youtube.py
│  │  │     ├─ instagram.py
│  │  │     ├─ tiktok.py
│  │  │     ├─ direct.py
│  │  │     └─ telegram.py
│  │  ├─ uploader/
│  │  │  ├─ plugin.py
│  │  │  ├─ handlers.py
│  │  │  └─ service.py
│  │  ├─ file2link/
│  │  │  ├─ plugin.py
│  │  │  ├─ handlers.py
│  │  │  ├─ service.py
│  │  │  └─ token.py
│  │  └─ shazam/
│  │     ├─ plugin.py
│  │     ├─ handlers.py
│  │     └─ service.py
│  │
│  ├─ api/
│  │  ├─ fastapi_app.py
│  │  └─ routes_download.py
│  │
│  ├─ workers/
│  │  ├─ queue.py
│  │  ├─ download_worker.py
│  │  ├─ upload_worker.py
│  │  └─ stream_worker.py
│  │
│  ├─ infra/
│  │  ├─ db/
│  │  │  ├─ models.py
│  │  │  └─ repos.py
│  │  ├─ redis/
│  │  └─ storage/
│  │
│  └─ shared/
│     ├─ schemas.py
│     ├─ enums.py
│     └─ utils.py
│
├─ migrations/
├─ tests/
├─ Dockerfile
├─ docker-compose.yml
├─ requirements.txt
├─ .env.example
└─ .github/workflows/docker-image.yml
```
## Plugin agreement

Bot should be designed and implemented in a way that it support expansion of features with least change or effect on other prvious working parts. 

So we agree upon a certain interface.
```
# app/features/<name>/plugin.py
from aiogram import Dispatcher

def register(dp: Dispatcher):
    from .handlers import router
    dp.include_router(router
)
```
Core then load the list of the plugins.
```
ENABLED_PLUGINS = [
  "app.features.downloader.plugin",
  "app.features.uploader.plugin",
  "app.features.file2link.plugin",
]
```
## Phase 1

aiogram core + plugin loader

> for heavy tasks, it is okay if we use a pyrogram or telethon worker

downloader (youtube/direct)

uploader to channel

Redis queue + worker

Docker compose

## Phase 2

instagram/tiktok adapters

complete metadata + thumbnail pipeline

admin panel commands (/jobs /stats)

## Phase 3

file2link streaming با token + Range

telethon stream worker

rate limit + anti abuse

## Phase 4

shazam/github/pro features

metrics + tracing

autoscaling

## Current framework

Core: aiogram v3

Downloader: yt-dlp + ffmpeg

stream bridge: FastAPI + Telethon worker

queuing: Redis + arq (light and async), open to use celery if we find out it's a better option

DB: PostgreSQL

deploy: Docker Compose

Bot will be desinged to work in docker container.

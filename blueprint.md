# Blueprint: Private Media Downloader & Streamer Telegram Bot

## 📂 System Layout
- Docker Container running on Ubuntu 24.04 (Python 3.11 + FFmpeg + Deno)
- Core Framework: Pyrogram (MTProto Engine) & FastAPI (Direct Streaming Engine)

## 📁 Directory Structure
telegram-media-bot/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── config.py
├── database.json          # Whitelisted & Blacklisted user registries
├── ytcookies.txt          # YouTube cookies
├── igcookies.txt          # Instagram cookies
├── ttcookies.txt          # TikTok cookies
├── main.py                # App Entry point & Console Engine
└── utils/
    ├── __init__.py
    ├── gate.py            # Security Gate Access controls (with auto-blacklist protection)
    ├── downloader.py      # yt-dlp format separator, downloader, ffmpeg metadata extractor, progress wrapper
    └── updater.py         # 6-hour automatic pre-release yt-dlp patcher
└── modules/
    ├── __init__.py
    └── stream_handler.py  # FastAPI Server Stream Bridge (with 24-hour token expiration)

## 🛠 Progress Log
- [x] Phase 1: Base Docker & Environment setup
- [x] Phase 2: Security Gate (Ignore strangers)
- [x] Phase 2.5: Bottom keyboard removed, direct console access with security blacklist tracking.
- [x] Phase 3: yt-dlp dynamic cookie engine & format categorizer (with Deno JS integration)
- [x] Phase 3.5: Automated 6-hour pre-release yt-dlp self-updater routine
- [x] Phase 4: Video Formats Interactive Selection Handler, Thumbnail Processing Engine & Metadata Embedder (compact button layouts)
- [x] Phase 5: FastAPI Stream Bridge (Direct File-to-Link streaming with 24-hour expiration)
- [x] Phase 6: Private Channel Logging integration, 2GB Bot limit warnings, and 5-second graphical Progress Bars

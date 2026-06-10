# Blueprint: Private Media Downloader & Streamer Telegram Bot

## 📂 System Layout
- Docker Container running on Ubuntu 24.04 (Python 3.11 + FFmpeg)
- Core Framework: Pyrogram (MTProto Engine) & FastAPI (Direct Streaming Engine)

## 📁 Directory Structure
telegram-media-bot/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── config.py
├── database.json          # Authorized user registry
├── ytcookies.txt          # YouTube cookies
├── igcookies.txt          # Instagram cookies
├── ttcookies.txt          # TikTok cookies
├── main.py                # App Entry point & Console Engine
└── utils/
    ├── __init__.py
    ├── gate.py            # Security Gate Access controls
    ├── downloader.py      # yt-dlp format separator, downloader, ffmpeg metadata extractor
    └── updater.py         # 6-hour automatic pre-release yt-dlp patcher
└── modules/
    ├── __init__.py
    └── stream_handler.py  # FastAPI Server Stream Bridge

## 🛠 Progress Log
- [x] Phase 1: Base Docker & Environment setup
- [x] Phase 2: Security Gate (Ignore strangers)
- [x] Phase 2.5: Bottom keyboard "🛠 Console" drawer which stays in client-control, never automatically forced open/closed.
- [x] Phase 3: yt-dlp dynamic cookie engine & format categorizer (Videos & Audios separated)
- [x] Phase 3.5: Automated 6-hour pre-release yt-dlp self-updater routine
- [x] Phase 4: Video Formats Interactive Selection Handler, Thumbnail Processing Engine & Metadata Embedder 
- [x] Phase 5: FastAPI Stream Bridge (Direct File-to-Link streaming with zero disk-write)

## 📝 Configuration (config.py)
Imports secrets, defines database locations, and paths for three isolated cookie files: ytcookies.txt, igcookies.txt, and ttcookies.txt.

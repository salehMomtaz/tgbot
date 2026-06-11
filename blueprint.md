# Blueprint: Private Media Downloader & Streamer Telegram Bot

## 📂 System Layout
- **Docker Container:** Runs on Ubuntu 24.04 (Python 3.11 + FFmpeg + Deno) [1.1.6, 1.4.1].
- **Double-Client MTProto Engine:** Uses a standard Telegram Bot API client for control panels, links, and streaming, alongside an idle/on-demand Userbot Client if a Premium Account String Session is provided [2.1].
- **Web Stream Server:** Powered by a lightweight FastAPI server, structured to pipe files directly from Telegram to the client on-the-fly with a 24-hour token expiration.
- **Reverse Proxy Ready:** Supports Nginx routing over HTTPS via the `/tgbot/` path with zero local file buffering.

## 📁 Directory Structure
```text
tgbot/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── config.py              # [UPDATED] Holds log channel and Premium keys
├── generate_session.py    # [FIXED] Local utility script to generate Pyrogram Userbot keys
├── database.json          # Whitelisted, blacklisted, and setting registries
├── ytcookies.txt          # YouTube cookies
├── igcookies.txt          # Instagram cookies
├── ttcookies.txt          # TikTok cookies
├── xcookies.txt           # X/Twitter cookies for restricted content
├── main.py                # [UPDATED] App entry point, dual-client bootstrap, queue dispatcher, and core handlers
└── utils/
    ├── __init__.py
    ├── gate.py            # [UPDATED] Security Access Control (with auto-blacklist & settings registry)
    ├── downloader.py      # [UPDATED] Format extractor (with compact buttons and smart size fallbacks), ffmpeg prober & On-Demand sequential file splitter
    ├── queue_manager.py   # [ADDED] Synthesized non-blocking, serializing task queue manager
    └── updater.py         # 6-hour automatic pre-release yt-dlp patcher
└── modules/
    ├── __init__.py
    └── stream_handler.py  # [UPDATED] FastAPI Server Stream Bridge (with 24-hour token check)
```

## 🛠 Progress Log
- [x] **Phase 1: Base Docker & Environment Setup**
- [x] **Phase 2: Security Gate & Admin Console** (Banning logic and settings toggle written in `utils/gate.py`)
- [x] **Phase 3: yt-dlp Extractor & Dynamic Sizing** (3-tiered size fallback formula written in `utils/downloader.py`)
- [x] **Phase 4: Format Grid Selector & Metadata Embedder** (Metadata packing, compact layouts, and ffmpeg probing integrated in `utils/downloader.py`)
- [x] **Phase 5: FastAPI Stream Bridge** (Streaming file link generator with 24-hour validity checks written in `modules/stream_handler.py`)
- [x] **Phase 6: Active Job Queue & Event Logs** (Synthesized queue implementation written in `utils/queue_manager.py`)
- [x] **Phase 7: Premium Integration & On-Demand Chunk Splitting** (On-demand sequential chunking generator implemented in `utils/downloader.py` and dual-client uploader loop resolved in `main.py`)

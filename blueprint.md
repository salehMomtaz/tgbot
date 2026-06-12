### Project Blueprint & Status Update

Overwrite your local `blueprint.md` file:

```markdown
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
├── config.py              # Holds log channel, Premium keys, and SSL configurations
├── generate_session.py    # Local utility script to generate Pyrogram Userbot keys
├── database.json          # Whitelisted, blacklisted, and setting registries
├── ytcookies.txt          # YouTube cookies
├── igcookies.txt          # Instagram cookies
├── ttcookies.txt          # TikTok cookies
├── xcookies.txt           # X/Twitter cookies for restricted content
├── cookies.txt            # Fallback global cookies for all other sites
├── main.py                # [UPDATED] Cleanest app bootloader (Double-instantiation bug fixed)
└── utils/
    ├── __init__.py
    ├── gate.py            # Security Access Control (with settings registry)
    ├── downloader.py      # Format extractor (with smart size fallbacks), ffmpeg prober & On-Demand splitter
    ├── queue_manager.py   # Synthesized non-blocking, serializing task queue manager
    ├── shared.py          # [ADDED] Isolated shared in-memory dictionary registries
    ├── id_validator.py    # Handles digit checks and Telegram ID boundary verification
    ├── uploader_handler.py# Isolated "Toyota Just-In-Time" sequential split uploader
    ├── updater.py         # 6-hour automatic pre-release yt-dlp patcher
    └── logger.py          # [UPDATED] Non-blocking, secure HTML-formatted system logging handler
└── modules/
    ├── __init__.py
    ├── admin.py           # [UPDATED] Single-Message Morphing Admin Console (With sequential prompt deletion flow)
    ├── downloader_handler.py # [UPDATED] Link and direct file URL queue worker (Double-import loops resolved)
    ├── stream_interceptor.py # Telegram file-to-stream link generator (with 24h validity checks)
    └── stream_handler.py  # FastAPI Server Stream Bridge (with 24-hour token check)
```

## 🛠 Progress Log
- [x] **Phase 1: Base Docker & Environment Setup**
- [x] **Phase 2: Security Gate & Admin Console** (Polished admin console with database boundaries written in `modules/admin.py`)
- [x] **Phase 3: yt-dlp Extractor & Dynamic Sizing**
- [x] **Phase 4: Format Grid Selector & Metadata Embedder** (Renaming, custom names, and metadata routines implemented in `modules/downloader_handler.py`)
- [x] **Phase 5: FastAPI Stream Bridge** (Streaming file link generator with 24-hour validity checks written in `modules/stream_handler.py`)
- [x] **Phase 6: Active Job Queue & Event Logs** (Queue is live and handles sequential flow natively)
- [x] **Phase 7: Premium Integration & On-Demand Chunk Splitting** (Uploader isolated in `utils/uploader_handler.py` and stream interceptor isolated in `modules/stream_interceptor.py`)
- [x] **Phase 8: State-Machine Finalization & Storage Cleanups** (Completed and fully verified)
- [x] **Phase 9: Standalone System-Wide Logger & Morphing Cleanup** (Dynamic sequential prompt deletions fully working, and root-logging engine linked to private Telegram channel in `utils/logger.py`)
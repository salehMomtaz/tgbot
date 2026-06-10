import uuid
import mimetypes
import time
import urllib.parse
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pyrogram import Client
from pyrogram.types import Message
import config

# In-memory session mapper
# Structure: { "token": { "chat_id": 123, "message_id": 456, "file_name": "x.mp4", "file_size": 1000000, "mime_type": "video/mp4", "created_at": 1609459200 } }
STREAM_CACHE = {}

fastapi_app = FastAPI(title="Telegram Direct Stream Bridge")
tg_client: Client = None

@fastapi_app.get("/stream/{token}/{file_name}")
async def stream_telegram_file(token: str, file_name: str):
    """Retrieve media dynamically and enforce 24-hour token lifespans."""
    if token not in STREAM_CACHE:
        raise HTTPException(status_code=404, detail="Streaming token expired or not found.")
        
    meta = STREAM_CACHE[token]
    
    # Enforce 24-hour expiration limit (24 hours = 86400 seconds)
    if time.time() - meta["created_at"] > 86400:
        STREAM_CACHE.pop(token, None)
        raise HTTPException(status_code=410, detail="This streaming link has expired (24-hour limit reached).")
    
    unquoted_name = urllib.parse.unquote(file_name)
    if meta["file_name"] != unquoted_name:
        raise HTTPException(status_code=400, detail="Filename mismatch for security token.")

    if not tg_client:
        raise HTTPException(status_code=500, detail="MTProto Telegram engine is offline.")

    try:
        msg: Message = await tg_client.get_messages(chat_id=meta["chat_id"], message_ids=meta["message_id"])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch file parameters from Telegram: {str(e)}")

    media_obj = None
    for attr in ["document", "video", "audio", "voice", "video_note", "photo"]:
        if hasattr(msg, attr) and getattr(msg, attr) is not None:
            media_obj = getattr(msg, attr)
            break

    if not media_obj:
        raise HTTPException(status_code=400, detail="The specified Telegram message has no streamable media.")

    async def chunk_generator():
        try:
            async for chunk in tg_client.stream_media(msg):
                yield chunk
        except Exception as e:
            print(f"[Streamer] Stream connection aborted: {str(e)}")

    headers = {
        "Content-Disposition": f'attachment; filename="{urllib.parse.quote(file_name)}"',
        "Content-Length": str(meta["file_size"]),
        "Accept-Ranges": "none",
    }

    return StreamingResponse(
        chunk_generator(),
        media_type=meta["mime_type"],
        headers=headers
    )

"""
TikTok photo-mode (slideshow/carousel) posts: images + background audio.

yt-dlp models a TikTok photo post as audio only — its embed extractor returns a
single ``audio`` format (the soundtrack) and no images. The images live in the
embed page's hydration JSON:

    source.data["/embed/<id>"].videoData {
        itemInfos.text                 -> caption
        authorInfos.uniqueId           -> author
        imagePostInfo.displayImages[]  -> [{urlList: [...], width, height}]
        musicInfos { playUrl, musicName, authorName, coversLarger }
    }

This module fetches that JSON and downloads the highest-resolution images
(``displayImages[].urlList`` is original size, ~1080x1920) plus the soundtrack,
so a relay can deliver [images + caption] and then the audio as its own reply.

Network/IO functions are synchronous; callers run them in an executor.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

import requests

logger = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36")

_EMBED_JSON_RE = re.compile(
    r'<script id="__FRONTITY_CONNECT_STATE__" type="application/json">(.*?)</script>',
    re.S)
_PHOTO_URL_RE = re.compile(
    r"https?://(?:www\.)?tiktok\.com/@[^/]+/photo/(\d+)", re.IGNORECASE)
# Embed info is requested by the relay header + the delivery path; cache it so
# one share costs one page fetch (TikTok 429s on bursts).
_INFO_CACHE: dict[str, tuple[float, dict]] = {}
_INFO_TTL = 300


def tiktok_photo_item_id(url: str | None) -> str | None:
    """Item id when *url* is a canonical TikTok photo-post URL, else None."""
    if not url:
        return None
    m = _PHOTO_URL_RE.search(url)
    return m.group(1) if m else None


def is_tiktok_photo_url(url: str | None) -> bool:
    return tiktok_photo_item_id(url) is not None


def _looks_like_image(data: bytes) -> bool:
    h = data[:12]
    return (h.startswith(b"\xff\xd8\xff") or h.startswith(b"\x89PNG\r\n\x1a\n")
            or h.startswith(b"GIF87a") or h.startswith(b"GIF89a")
            or (h.startswith(b"RIFF") and data[8:12] == b"WEBP"))


def _audio_ext(data: bytes) -> str:
    if data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3"):
        return ".mp3"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return ".m4a"
    return ".mp3"


def fetch_embed_info(item_id: str, timeout: int = 25) -> dict | None:
    """Fetch the embed page and return caption/author/images/audio for one item.

    Returns None only when the page or its JSON cannot be read. Photos advertise
    a non-empty ``images`` list; videos have ``images == []`` (the same struct
    still supplies the author and caption, so relays need one request, not two).
    """
    if not item_id:
        return None
    now = time.time()
    cached = _INFO_CACHE.get(str(item_id))
    if cached and cached[0] > now:
        return cached[1]
    try:
        r = None
        for attempt in range(2):
            r = requests.get(f"https://www.tiktok.com/embed/{item_id}",
                             headers={"user-agent": _UA}, timeout=timeout)
            if r.status_code == 429 and attempt == 0:
                time.sleep(3)
                continue
            break
        if r is None or r.status_code != 200:
            logger.warning("[TikTokPhoto] embed %s -> HTTP %s", item_id,
                           r.status_code if r else "?")
            return None
        m = _EMBED_JSON_RE.search(r.text)
        if not m:
            logger.warning("[TikTokPhoto] embed %s: hydration JSON not found", item_id)
            return None
        state = json.loads(m.group(1))
    except Exception as e:  # noqa: BLE001
        logger.warning("[TikTokPhoto] embed %s fetch failed: %s", item_id, e)
        return None

    video_data = None
    for payload in ((state.get("source") or {}).get("data") or {}).values():
        if isinstance(payload, dict) and isinstance(payload.get("videoData"), dict):
            video_data = payload["videoData"]
            break
    if not video_data:
        return None

    item = video_data.get("itemInfos") or {}
    author = (video_data.get("authorInfos") or {}).get("uniqueId") or ""
    music = video_data.get("musicInfos") or {}
    images = []
    for img in (video_data.get("imagePostInfo") or {}).get("displayImages") or []:
        urls = [u for u in (img.get("urlList") or []) if isinstance(u, str) and u]
        if urls:
            images.append({
                "urls": urls,
                "url": urls[0],
                "width": int(img.get("width") or 0),
                "height": int(img.get("height") or 0),
            })
    play = music.get("playUrl") or []
    covers = music.get("coversLarger") or music.get("coversMedium") or music.get("covers") or []
    info = {
        "item_id": str(item_id),
        "author": author,
        "caption": str(item.get("text") or ""),
        "is_photo": bool(images),
        "images": images,
        "audio_url": play[0] if play else None,
        "audio_title": str(music.get("musicName") or ""),
        "audio_artist": str(music.get("authorName") or ""),
        "audio_cover": covers[0] if covers else None,
    }
    if len(_INFO_CACHE) > 200:
        _INFO_CACHE.clear()
    _INFO_CACHE[str(item_id)] = (now + _INFO_TTL, info)
    return info


def _get(url: str, timeout: int = 30) -> bytes | None:
    try:
        r = requests.get(url, headers={"user-agent": _UA}, timeout=timeout)
        if r.status_code == 200 and r.content:
            return r.content
    except Exception as e:  # noqa: BLE001
        logger.warning("[TikTokPhoto] GET %.90s failed: %s", url, e)
    return None


def download_photo_assets(info: dict, dest_dir: str) -> tuple[list[str], str | None, str | None]:
    """Download the images + soundtrack + cover into *dest_dir*.

    Returns ``(image_paths, audio_path|None, cover_path|None)``. Images are
    tried mirror-by-mirror (TikTok's ``urlList`` holds CDN alternates of the
    same original) and validated by magic bytes.
    """
    os.makedirs(dest_dir, exist_ok=True)
    images: list[str] = []
    for idx, img in enumerate(info.get("images") or []):
        data = None
        for url in img.get("urls") or [img.get("url")]:
            if not url:
                continue
            data = _get(url)
            if data and _looks_like_image(data):
                break
            data = None
        if not data:
            logger.warning("[TikTokPhoto] image %d/%d unavailable", idx + 1,
                           len(info.get("images") or []))
            continue
        path = os.path.join(dest_dir, f"photo_{idx:02d}.jpg")
        with open(path, "wb") as f:
            f.write(data)
        images.append(path)

    audio_path = cover_path = None
    if info.get("audio_url"):
        data = _get(info["audio_url"])
        if data:
            audio_path = os.path.join(dest_dir, "audio" + _audio_ext(data))
            with open(audio_path, "wb") as f:
                f.write(data)
    if info.get("audio_cover"):
        data = _get(info["audio_cover"], timeout=20)
        if data and _looks_like_image(data):
            cover_path = os.path.join(dest_dir, "audio_cover.jpg")
            with open(cover_path, "wb") as f:
                f.write(data)
    return images, audio_path, cover_path

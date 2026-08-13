# modules/youtube/scraper.py — ported from balebot
import os
import re
import asyncio
import yt_dlp


def clean_vtt_subtitles(vtt_path: str) -> str:
    if not os.path.exists(vtt_path):
        return "No subtitles found."
    with open(vtt_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    clean, seen = [], set()
    for line in lines:
        ls = line.strip()
        if not ls or ls.startswith("WEBVTT") or ls.startswith("Kind:") or ls.startswith("Language:") or "-->" in ls or ls.isdigit():
            continue
        c = re.sub(r"<[^>]+>", "", ls)
        if c not in seen:
            clean.append(c); seen.add(c)
    return "\n".join(clean)


async def search_ytdlp_flat(query: str, max_results: int) -> list:
    import config
    loop = asyncio.get_event_loop()
    def extract():
        ydl_opts = {'quiet': True, 'extract_flat': True, 'skip_download': True, 'proxy': getattr(config, 'YTDLP_PROXY', None)}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
    info = await loop.run_in_executor(None, extract)
    return info.get('entries', [])

# modules/web/api.py — ported from balebot
import urllib.parse
import aiohttp
import config

async def fetch_markdown_text(url: str) -> tuple[str, str]:
    enc = urllib.parse.quote(url, safe="")
    api_url = f"https://urltomarkdown.herokuapp.com/?url={enc}&title=true&links=false"
    timeout = aiohttp.ClientTimeout(total=20, connect=10)
    proxy = getattr(config, "AIOHTTP_PROXY", None) or getattr(config, "PROXY_URL", None)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(api_url, proxy=proxy) as resp:
            if resp.status != 200:
                raise RuntimeError(f"urltomarkdown HTTP {resp.status}")
            raw_title = resp.headers.get("X-Title", "Webpage")
            title = urllib.parse.unquote(raw_title).strip()
            text = await resp.text()
            return title, text

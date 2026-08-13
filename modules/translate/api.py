# modules/translate/api.py — ported from balebot
import aiohttp
import config

async def google_translate_async(text: str, src_lang: str, dst_lang: str) -> str:
    url = "https://translate.googleapis.com/translate_a/single"
    params = {"client": "gtx", "sl": src_lang, "tl": dst_lang, "dt": "t", "q": text}
    timeout = aiohttp.ClientTimeout(total=15, connect=10)
    proxy = getattr(config, "AIOHTTP_PROXY", None) or getattr(config, "PROXY_URL", None)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, params=params, proxy=proxy) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Translate API HTTP {resp.status}")
            result = await resp.json()
            try:
                return "".join(item[0] for item in result[0] if item[0])
            except (IndexError, TypeError):
                raise ValueError("Failed to parse translation payload.")

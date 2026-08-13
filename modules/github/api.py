# modules/github/api.py — GitHub HTTP helpers (ported from balebot)
import aiohttp
import config


def get_github_headers() -> dict:
    """Headers with optional PAT to raise rate limit to 5000/hr."""
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "tgbot-github-assistant",
    }
    tok = (getattr(config, "GITHUB_TOKEN", "") or "").strip()
    if tok and tok != "YOUR_GITHUB_PAT_HERE":
        headers["Authorization"] = f"token {tok}"
    return headers


async def fetch_github_api(url: str) -> dict | list:
    timeout = aiohttp.ClientTimeout(total=45, connect=15)
    proxy = getattr(config, "AIOHTTP_PROXY", None) or getattr(config, "PROXY_URL", None)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=get_github_headers(), proxy=proxy) as resp:
            if resp.status == 403:
                raise RuntimeError("GitHub API rate limit exceeded. Configure GITHUB_TOKEN.")
            if resp.status == 404:
                raise FileNotFoundError("GitHub resource not found.")
            if resp.status != 200:
                raise RuntimeError(f"GitHub returned HTTP {resp.status}")
            return await resp.json()

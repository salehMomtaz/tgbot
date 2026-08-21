# utils/updater.py
import asyncio
import logging
import subprocess
import sys

logger = logging.getLogger(__name__)


def _installed_version() -> str | None:
    """Return the installed yt-dlp version string, or None if not callable."""
    try:
        out = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--version"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, check=False,
        )
        ver = out.stdout.strip().splitlines()[0] if out.stdout else ""
        return ver or None
    except Exception:
        return None


def _is_nightly(version: str) -> bool:
    """yt-dlp stable = 'YYYY.MM.DD' (2 dots); nightly/dev = 'YYYY.MM.DD.HHMMSS' (3 dots)."""
    return version.count(".") >= 3


async def auto_update_ytdlp():
    """Keep yt-dlp on the latest nightly build.

    `pip install -U --pre "yt-dlp[default,curl-cffi]" "curl_cffi<0.14"`:
      * `--pre` allows pre-releases — yt-dlp publishes nightly timestamp builds
        to PyPI as pre-releases, so this stays on the nightly channel.
      * `[default]` preserves the default extras (certifi, etc.) across upgrades
        — plain `yt-dlp` would silently strip them.
      * `[curl-cffi]` preserves the impersonation extra yt-dlp needs for
        TikTok's proof-of-work webpage challenge solver.
      * `"curl_cffi<0.14"` is a HARD PIN — do NOT drop it. yt-dlp's TikTok
        extractor hardcodes `impersonate=True`, which resolves to curl_cffi's
        newest chrome target; curl_cffi >= 0.14 ships chrome142+ fingerprints
        that TikTok now blocks with a "Site Maintenance" page ("Unexpected
        response from webpage request", yt-dlp#17403). 0.13.x's newest chrome is
        131, which TikTok accepts. Without the pin, a fresh `[curl-cffi]`
        resolution on the next nightly check would silently break TikTok again.
    Runs every 6 hours.
    """
    # One initial delay (30 s) so we don't fight with boot-time provider startup.
    await asyncio.sleep(30)
    while True:
        before = _installed_version()
        logger.info(f"[Updater] Checking for yt-dlp nightly updates (current: {before or 'unknown'})...")
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pip", "install", "-U", "--pre",
                "yt-dlp[default,curl-cffi]", "curl_cffi<0.14",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            after = _installed_version()
            if process.returncode == 0:
                if after and after != before:
                    logger.info(f"[Updater] yt-dlp updated: {before or 'n/a'} → {after}.")
                else:
                    logger.info(f"[Updater] yt-dlp already up to date ({after or 'unknown'}).")
                if after and not _is_nightly(after):
                    logger.warning(
                        f"[Updater] yt-dlp {after} is NOT a nightly build "
                        f"(expected a YYYY.MM.DD.HHMMSS version). "
                        "YouTube may break between releases; a nightly is recommended."
                    )
            else:
                logger.error(f"[Updater] yt-dlp update failed: {stderr.decode().strip()}")
        except Exception as e:
            logger.exception(f"[Updater] Exception occurred during update: {e}")

        # Wait 6 hours before checking again (6 hours = 21600 seconds)
        await asyncio.sleep(21600)

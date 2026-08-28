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


def _installed_kurigram_version() -> str | None:
    """Return the runtime kurigram version (via the 'pyrogram' namespace) or
    None. The wheel ships its code under ``pyrogram/`` (drop-in) and only
    bumps ``pyrogram.__version__``; ``pip show kurigram`` is also valid but
    the runtime identity is what matters for the bot."""
    try:
        import pyrogram  # noqa: import-outside-toplevel  (sync helper)
        return getattr(pyrogram, "__version__", None)
    except Exception:
        return None


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


async def auto_update_kurigram():
    """Keep kurigram on the newest stable release.

    kurigram is the actively-maintained drop-in for archived upstream
    pyrogram — it ships its code under the ``pyrogram/`` namespace, so
    ``import pyrogram; pyrogram.__version__`` reports the kurigram version
    and the bot keeps working without any code change (AGENTS.md invariant
    #5a, commit ``31f4dfd``).

    POLICY: always upgrade to the newest stable kurigram release. This loop
    enforces the policy in-process: every 24 hours, ``pip install -U
    kurigram`` pulls whatever is newest on PyPI. ``requirements.txt`` pins
    ``kurigram>=X.Y.Z`` (the floor) so a fresh ``pip install -r`` on a
    new box also gets the policy's intent — but the in-process check is
    what catches a release published AFTER the last manual refresh.

    IMPORTANT: a kurigram upgrade changes the running ``pyrogram`` module
    in place on the next ``pip install`` call, but the running pyrogram
    Clients (premium_app, app) keep their already-imported module
    reference. The upgrade is therefore LOGGED but only APPLIED on the
    NEXT bot restart — which is exactly what the operator wants
    (auto-upgrade detected and reported, restart on operator's terms).
    If a future maintainer wants a self-restart after upgrade, wire it
    into the same path as the premium-session ``schedule_self_restart``
    in main.py.
    """
    # One initial delay (60 s) — schedule this AFTER the yt-dlp updater's
    # boot so we don't fight pip locks if both kick off at once.
    await asyncio.sleep(60)
    while True:
        before = _installed_kurigram_version()
        logger.info(f"[Updater] Checking for kurigram updates (current: {before or 'unknown'})...")
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pip", "install", "-U", "kurigram",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            after = _installed_kurigram_version()
            if process.returncode == 0:
                if after and after != before:
                    logger.info(
                        f"[Updater] kurigram updated: {before or 'n/a'} → {after}. "
                        "Restart the bot to load the new code (in-process "
                        "imports were bound at startup)."
                    )
                else:
                    logger.info(f"[Updater] kurigram already up to date ({after or 'unknown'}).")
            else:
                logger.error(f"[Updater] kurigram update failed: {stderr.decode().strip()}")
        except Exception as e:
            logger.exception(f"[Updater] Exception occurred during kurigram update: {e}")

        # Wait 24 hours between checks — kurigram releases are slower than
        # yt-dlp nightly (typically weekly to monthly). 24 h keeps the
        # operator-aware delay low while not hammering PyPI.
        await asyncio.sleep(86400)

# modules/direct_forward.py
"""
Direct-forward: relay media you DM to the bot's own Instagram / X accounts
straight into your Telegram chat.

How it works
------------
You run DEDICATED bot accounts on Instagram and/or X. From your personal
account you open the chat with the bot account and share a post / reel / story
/ photo / video — or just paste a link. The bot polls its own DM inbox and:

  1. resolves what each new DM contains (media attachment, reel/story share,
     plain link, tweet share, X DM photo/video),
  2. downloads it (link items go through the normal yt-dlp pipeline WITH your
     cookie jars, so login-walled content works — cookie write-back keeps
     those jars fresh),
  3. sends the media to DIRECT_FORWARD_CHAT_ID,
  4. advances a per-platform cursor in direct_forward_state.json so nothing is
     sent twice.

No third-party relay APIs: Instagram uses the local ``instagrapi`` library
(bootstrapped from the existing igcookies.txt ``sessionid`` when possible,
username/password login as fallback), X/Twitter uses the local ``twikit``
library. Both clients persist their session to disk, so login challenges only
happen once.

First run only PRIMES the cursor (skips whatever is already in the inbox) so
you are never blasted by backlog. Delete direct_forward_state.json to re-prime.

Configuration (.env) — all values read through config.py
--------------------------------------------------------
DIRECT_FORWARD_CHAT_ID=123456789      # Telegram chat that receives the media
DIRECT_FORWARD_POLL_SECONDS=120

IG_DIRECT_ENABLED=true
IG_DIRECT_USERNAME=bot_ig_login       # fallback auth if sessionid login fails
IG_DIRECT_PASSWORD=bot_ig_password
IG_DIRECT_TOTP_SEED=                  # optional
IG_DIRECT_FROM_USERNAME=your_personal_ig_handle

X_DIRECT_ENABLED=true
X_DIRECT_USERNAME=bot_x_login
X_DIRECT_PASSWORD=bot_x_password
X_DIRECT_EMAIL=bot_x_email
X_DIRECT_FROM_USER_ID=your_numeric_x_user_id

Only DMs from the whitelisted sender are processed; everything else is ignored.
"""

import asyncio
import json
import logging
import os
import re
from typing import Any

import config
from utils import cookie_manager

logger = logging.getLogger(__name__)

STATE_FILE = "direct_forward_state.json"
IG_SESSION_FILE = "direct_ig_session.json"
X_COOKIES_FILE = "direct_x_cookies.json"

URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"']+")


# =========================================================================
# State (dedup cursors)
# =========================================================================

def _load_state() -> dict[str, Any]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    tmp = f"{STATE_FILE}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        logger.warning(f"[DirectForward] Could not save state: {e}")


def _cursor(state: dict, platform: str) -> int:
    try:
        return int(state.get(platform, {}).get("last_id", 0))
    except Exception:
        return 0


def _bump_cursor(state: dict, platform: str, new_id: int) -> None:
    state.setdefault(platform, {})["last_id"] = str(new_id)


# =========================================================================
# Delivery helpers
# =========================================================================

class _NullStatusMessage:
    """Stand-in for a Pyrogram status message: the download queue wants
    something it can edit, background relays have nothing to edit."""
    async def edit_text(self, _text: str) -> None:
        return


async def _download_and_deliver(bot_client, premium_client, chat_id: int, url: str,
                                platform: str, sender_label: str) -> None:
    """Download *url* via the normal yt-dlp pipeline (cookie jars included)
    and deliver it. Runs on the shared single-worker queue behind interactive
    downloads — a DM relay never overtakes a user."""
    from utils.downloader import download_media, probe_video_dimensions
    from utils.uploader_handler import process_split_and_upload
    import shutil
    import hashlib

    cache_id = f"df_{platform}_{hashlib.md5(url.encode()).hexdigest()[:10]}"
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, download_media, url, None, "v", cache_id, None, None, None, None,
        )

        file_path = result["file_path"]
        caption = f"📥 **{platform} DM** from {sender_label}\n`{result.get('title', '')}`"
        width, height, _dur = probe_video_dimensions(file_path)
        is_photo = (width == 320 and height == 320) or file_path.lower().endswith(
            (".jpg", ".jpeg", ".png", ".webp"))

        if is_photo:
            await bot_client.send_photo(chat_id=chat_id, photo=file_path, caption=caption)
        else:
            await process_split_and_upload(
                bot_client=bot_client,
                premium_client=premium_client,
                chat_id=chat_id,
                file_path=file_path,
                action="v",
                title=result.get("title", "Media"),
                uploader=result.get("uploader", "Unknown"),
                duration=result.get("duration", 0),
                thumb_path=result.get("thumb_path"),
                progress_msg=None,
                delete_progress_after=True,
            )
        logger.info(f"[DirectForward] ✅ relayed {url} -> {chat_id}")
    finally:
        task_dir = f"cache/{cache_id}"
        if os.path.exists(task_dir):
            try:
                shutil.rmtree(task_dir)
            except Exception:
                pass


async def _deliver_raw_file(bot_client, chat_id: int, file_path: str, is_photo: bool,
                            caption: str) -> None:
    """Send an already-downloaded attachment (DM photo/video bytes)."""
    if is_photo:
        await bot_client.send_photo(chat_id=chat_id, photo=file_path, caption=caption)
    else:
        await bot_client.send_video(chat_id=chat_id, video=file_path, caption=caption,
                                    supports_streaming=True)


def _enqueue_relay(queue, user_chat_id: int, corofn) -> None:
    """Fire-and-forget enqueue of one relay job on the shared download queue."""
    async def _submit():
        await queue.add_task(user_id=user_chat_id, message=_NullStatusMessage(), coroutine=corofn)
    asyncio.create_task(_submit())


def _fetch_bytes(url: str, referer: str | None = None) -> bytes:
    """Small helper for downloading DM attachment bytes (runs in executor)."""
    import requests
    headers = {"User-Agent": getattr(config, "YTDLP_USER_AGENT", "") or
               "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
    if referer:
        headers["Referer"] = referer
    proxies = {"http": config.REQUESTS_PROXY, "https": config.REQUESTS_PROXY} if config.REQUESTS_PROXY else None
    resp = requests.get(url, headers=headers, proxies=proxies, timeout=60)
    resp.raise_for_status()
    return resp.content


# =========================================================================
# Instagram DM worker (instagrapi)
# =========================================================================

def _ig_sessionid_from_jar() -> str | None:
    """Pull the ``sessionid`` cookie value out of the Instagram jar, so the DM
    client can bootstrap from the exact session yt-dlp already uses (and keeps
    fresh via write-back)."""
    jar = config.IG_COOKIES
    if not os.path.exists(jar):
        return None
    try:
        with open(jar, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                if raw.startswith("#") or not raw.strip():
                    continue
                parts = raw.rstrip("\n").split("\t")
                if len(parts) >= 7 and parts[5] == "sessionid" and parts[6]:
                    return parts[6]
    except Exception:
        pass
    return None


def _ig_login(cl, log_prefix: str = "[DirectForward/IG]") -> None:
    """Authenticate the instagrapi client. Order:
    1. resume persisted session settings (cheapest, zero challenges),
    2. login by the sessionid from the shared IG cookie jar,
    3. username/password (+ TOTP) as last resort."""
    from instagrapi.exceptions import LoginRequired

    if os.path.exists(IG_SESSION_FILE) and os.path.getsize(IG_SESSION_FILE) > 0:
        try:
            cl.load_settings(IG_SESSION_FILE)
            cl.login(config.IG_DIRECT_USERNAME or None, config.IG_DIRECT_PASSWORD or None)
            cl.account_info()  # forces a session check
            logger.info(f"{log_prefix} Resumed persisted direct session.")
            return
        except Exception as e:
            logger.info(f"{log_prefix} Persisted session unusable ({e}); trying sessionid.")

    sessionid = _ig_sessionid_from_jar()
    if sessionid:
        try:
            if cl.login_by_sessionid(sessionid):
                logger.info(f"{log_prefix} Logged in via sessionid from igcookies.txt.")
                return
        except Exception as e:
            logger.warning(f"{log_prefix} sessionid login failed ({e}); trying password.")

    if not (config.IG_DIRECT_USERNAME and config.IG_DIRECT_PASSWORD):
        raise RuntimeError(
            "No usable IG session. Upload a fresh igcookies.txt (Admin → Cookies) "
            "or set IG_DIRECT_USERNAME/IG_DIRECT_PASSWORD in .env.")

    kwargs = {}
    if config.IG_DIRECT_TOTP_SEED:
        kwargs["verification_code"] = cl.totp_generate_code(config.IG_DIRECT_TOTP_SEED)
    try:
        if cl.login(config.IG_DIRECT_USERNAME, config.IG_DIRECT_PASSWORD, **kwargs):
            logger.info(f"{log_prefix} Logged in with username/password.")
            return
    except LoginRequired:
        raise
    raise RuntimeError("Instagram login failed (all methods exhausted).")


def _ig_extract_share_url(cl, msg) -> str | None:
    """Turn a shared-content DM item into an Instagram URL we can hand to the
    yt-dlp pipeline."""
    item_type = (msg.item_type or "").lower()
    try:
        if item_type == "clip" and msg.clip is not None:
            code = getattr(msg.clip, "code", None)
            return f"https://www.instagram.com/reel/{code}/" if code else None
        if item_type in ("reel_share", "media_share", "felix_share", "xma_share"):
            payload = (msg.reel_share or msg.media_share or msg.felix_share or msg.xma_share)
            if isinstance(payload, dict):
                media = payload.get("media") or payload.get("clip") or payload
                code = media.get("code") if isinstance(media, dict) else getattr(media, "code", None)
                pk = (media.get("pk") or media.get("id")) if isinstance(media, dict) else getattr(media, "pk", None)
                if code:
                    return f"https://www.instagram.com/reel/{code}/"
                if pk:
                    try:
                        return f"https://www.instagram.com/reel/{cl.media_info(pk).code}/"
                    except Exception:
                        return None
        if item_type == "story_share" and msg.story_share is not None:
            payload = msg.story_share if isinstance(msg.story_share, dict) else {}
            media = payload.get("media") or {}
            username = ((media.get("user") or {}).get("username")
                        or payload.get("message", "").split()[-1:][0]
                        if payload.get("message") else None)
            media_id = media.get("id") or payload.get("story_id")
            if username and media_id:
                return f"https://www.instagram.com/stories/{username}/{media_id}/"
    except Exception as e:
        logger.warning(f"[DirectForward/IG] share-url extraction failed ({msg.item_type}): {e}")
    return None


async def _instagram_worker(bot_client, premium_client, chat_id: int, queue) -> None:
    try:
        from instagrapi import Client as IGClient
        from instagrapi.exceptions import (
            ChallengeRequired, LoginRequired, PleaseWaitFewMinutes,
        )
    except ImportError:
        logger.error("[DirectForward/IG] instagrapi is not installed "
                     "(pip install instagrapi) — Instagram direct-forward disabled.")
        return

    loop = asyncio.get_event_loop()
    cl = IGClient()
    cl.delay_range = [1, 3]

    try:
        await loop.run_in_executor(None, lambda: _ig_login(cl))
        cl.dump_settings(IG_SESSION_FILE)
        os.chmod(IG_SESSION_FILE, 0o600)
    except Exception as e:
        logger.error(f"[DirectForward/IG] login failed: {e}")
        return

    if not config.IG_DIRECT_FROM_USERNAME:
        logger.error("[DirectForward/IG] IG_DIRECT_FROM_USERNAME is not set — "
                     "need YOUR handle so the bot knows whose DMs to accept.")
        return

    try:
        target_uid = await loop.run_in_executor(
            None, cl.user_id_from_username, config.IG_DIRECT_FROM_USERNAME)
    except Exception as e:
        logger.error(f"[DirectForward/IG] cannot resolve "
                     f"@{config.IG_DIRECT_FROM_USERNAME}: {e}")
        return

    state = _load_state()
    if "ig" not in state:
        # First run: prime cursor, skip backlog.
        state["ig"] = {"last_id": "0"}
        _save_state(state)
        logger.info("[DirectForward/IG] first run — priming cursor, backlog is skipped.")
        try:
            threads = await loop.run_in_executor(None, lambda: cl.direct_threads(amount=20))
            last = 0
            for th in threads:
                if any(u.pk == target_uid for u in (th.users or [])):
                    msgs = await loop.run_in_executor(None, lambda tid=th.id: cl.direct_messages(tid, amount=1))
                    if msgs:
                        last = max(last, int(msgs[0].id))
            if last:
                _bump_cursor(state, "ig", last)
                _save_state(state)
        except Exception as e:
            logger.warning(f"[DirectForward/IG] priming peek failed: {e}")

    sender_label = f"@{config.IG_DIRECT_FROM_USERNAME}"
    poll = max(30, config.DIRECT_FORWARD_POLL_SECONDS)
    logger.info(f"[DirectForward/IG] polling DMs from {sender_label} every {poll}s")

    while True:
        try:
            threads = await loop.run_in_executor(None, lambda: cl.direct_threads(amount=20))
            for th in threads:
                users = {u.pk for u in (th.users or [])}
                if target_uid not in users or th.is_group:
                    continue
                msgs = await loop.run_in_executor(
                    None, lambda tid=th.id: cl.direct_messages(tid, amount=25))
                last_seen = _cursor(state, "ig")
                new_msgs = sorted(
                    (m for m in msgs if not m.is_sent_by_viewer and int(m.id) > last_seen),
                    key=lambda m: int(m.id),
                )
                for m in new_msgs:
                    try:
                        await _process_ig_message(m, cl, loop, queue, chat_id,
                                                  bot_client, premium_client, sender_label)
                    except Exception as e:
                        logger.error(f"[DirectForward/IG] item {m.id} failed: {e}")
                    # Always advance past attempted items: one bad DM must not
                    # block the relay forever.
                    _bump_cursor(state, "ig", int(m.id))
                if new_msgs:
                    _save_state(state)
            cl.dump_settings(IG_SESSION_FILE)
        except (ChallengeRequired, PleaseWaitFewMinutes) as e:
            logger.error(f"[DirectForward/IG] Instagram challenged the session: {e}. "
                         f"Upload a fresh igcookies.txt via Admin → Cookies, then restart. "
                         f"Pausing this worker until next hour.")
            await asyncio.sleep(3600)
        except LoginRequired:
            logger.warning(f"[DirectForward/IG] session expired — attempting re-login.")
            try:
                await loop.run_in_executor(None, lambda: _ig_login(cl))
                cl.dump_settings(IG_SESSION_FILE)
            except Exception as e:
                logger.error(f"[DirectForward/IG] re-login failed: {e}. Sleeping 1h.")
                await asyncio.sleep(3600)
        except Exception as e:
            logger.error(f"[DirectForward/IG] poll error: {e}")
            await asyncio.sleep(min(600, poll))

        await asyncio.sleep(poll)


async def _process_ig_message(m, cl, loop, queue, chat_id, bot_client, premium_client, sender_label) -> None:
    item_type = (getattr(m, "item_type", "") or "").lower()

    # 1) Direct media attachment (photo / video recorded or uploaded in chat).
    media = getattr(m, "media", None) or getattr(m, "visual_media", None)
    if item_type in ("media", "visual_media") and media is not None:
        video_url = getattr(media, "video_url", None)
        photo_url = getattr(media, "thumbnail_url", None)
        is_photo = not video_url
        src = str(video_url or photo_url)
        if src:
            data = await loop.run_in_executor(None, _fetch_bytes, src, "https://www.instagram.com/")
            ext = ".jpg" if is_photo else ".mp4"
            path = f"cache/df_ig_dm_{m.id}{ext}"
            os.makedirs("cache", exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
            await _deliver_raw_file(bot_client, chat_id, path, is_photo,
                                    f"📥 **Instagram DM** from {sender_label}")
            try:
                os.remove(path)
            except Exception:
                pass
            return

    # 2) Shared post / reel / clip / story.
    share_url = _ig_extract_share_url(cl, m)
    if share_url:
        _enqueue_relay(queue, chat_id,
                       lambda u=share_url: _download_and_deliver(
                           bot_client, premium_client, chat_id, u,
                           "Instagram", sender_label))
        return

    # 3) Plain text with one or more links (the frequent case: pasting a URL).
    text = getattr(m, "text", None) or ""
    urls = URL_RE.findall(text)
    for u in urls:
        _enqueue_relay(queue, chat_id,
                       lambda u=u: _download_and_deliver(
                           bot_client, premium_client, chat_id, u,
                           "Instagram", sender_label))


# =========================================================================
# X / Twitter DM worker (twikit)
# =========================================================================

def _x_deep_find_media_url(node: Any) -> tuple[str | None, bool]:
    """Duck-type an X DM attachment tree into (media_url, is_photo)."""
    if isinstance(node, dict):
        if "media_url_https" in node and isinstance(node["media_url_https"], str):
            return node["media_url_https"], True
        variants = node.get("variants")
        if isinstance(variants, list):
            mp4s = [v for v in variants if isinstance(v, dict)
                    and str(v.get("content_type", "")).startswith("video")
                    or (isinstance(v, dict) and ".mp4" in str(v.get("url", "")))]
            if mp4s:
                mp4s.sort(key=lambda v: int(v.get("bitrate", 0) or 0), reverse=True)
                return mp4s[0].get("url"), False
        for v in node.values():
            url, is_photo = _x_deep_find_media_url(v)
            if url:
                return url, is_photo
    elif isinstance(node, list):
        for v in node:
            url, is_photo = _x_deep_find_media_url(v)
            if url:
                return url, is_photo
    return None, False


def _x_deep_find_tweet(node: Any) -> str | None:
    """Find a shared tweet inside a DM attachment tree → canonical status URL."""
    if isinstance(node, dict):
        if "rest_id" in node and ("legacy" in node or node.get("__typename") == "Tweet"):
            return f"https://x.com/i/status/{node['rest_id']}"
        if "id_str" in node and "full_text" in node:
            return f"https://x.com/i/status/{node['id_str']}"
        for v in node.values():
            found = _x_deep_find_tweet(v)
            if found:
                return found
    elif isinstance(node, list):
        for v in node:
            found = _x_deep_find_tweet(v)
            if found:
                return found
    return None


async def _twitter_worker(bot_client, premium_client, chat_id: int, queue) -> None:
    try:
        from twikit import Client as XClient
    except ImportError:
        logger.error("[DirectForward/X] twikit is not installed "
                     "(pip install twikit) — X direct-forward disabled.")
        return

    if not (config.X_DIRECT_USERNAME and config.X_DIRECT_PASSWORD and config.X_DIRECT_FROM_USER_ID):
        logger.error("[DirectForward/X] need X_DIRECT_USERNAME, X_DIRECT_PASSWORD and "
                     "X_DIRECT_FROM_USER_ID — X direct-forward disabled.")
        return

    client = XClient(language="en-US")
    try:
        if os.path.exists(X_COOKIES_FILE) and os.path.getsize(X_COOKIES_FILE) > 0:
            client.load_cookies(X_COOKIES_FILE)
        else:
            await client.login(
                auth_info_1=config.X_DIRECT_USERNAME,
                auth_info_2=config.X_DIRECT_EMAIL or config.X_DIRECT_USERNAME,
                password=config.X_DIRECT_PASSWORD,
                cookies_file=X_COOKIES_FILE,
            )
    except Exception as e:
        logger.error(f"[DirectForward/X] login failed: {e}. "
                     f"X aggressively locks fresh automation logins — if this repeats, "
                     f"log in once in a browser on the VPS IP or warm the xcookies.txt jar.")
        return

    state = _load_state()
    if "x" not in state:
        state["x"] = {"last_id": "0"}
        _save_state(state)
        logger.info("[DirectForward/X] first run — priming cursor, backlog is skipped.")
        try:
            history = await client.get_dm_history(user_id=config.X_DIRECT_FROM_USER_ID)
            if history:
                _bump_cursor(state, "x", int(history[0].id))
                _save_state(state)
        except Exception as e:
            logger.warning(f"[DirectForward/X] priming peek failed: {e}")

    sender_label = f"x-user {config.X_DIRECT_FROM_USER_ID}"
    poll = max(30, config.DIRECT_FORWARD_POLL_SECONDS)
    logger.info(f"[DirectForward/X] polling DM history from {sender_label} every {poll}s")

    while True:
        try:
            history = await client.get_dm_history(user_id=config.X_DIRECT_FROM_USER_ID)
            last_seen = _cursor(state, "x")
            new_msgs = sorted(
                (m for m in history
                 if int(m.id) > last_seen and str(m.sender_id) == str(config.X_DIRECT_FROM_USER_ID)),
                key=lambda m: int(m.id),
            )
            for m in new_msgs:
                try:
                    await _process_x_message(m, queue, chat_id, bot_client, premium_client, sender_label)
                except Exception as e:
                    logger.error(f"[DirectForward/X] message {m.id} failed: {e}")
                _bump_cursor(state, "x", int(m.id))
            if new_msgs:
                _save_state(state)
        except Exception as e:
            logger.error(f"[DirectForward/X] poll error: {e}")
            await asyncio.sleep(min(600, poll))

        await asyncio.sleep(poll)


async def _process_x_message(m, queue, chat_id, bot_client, premium_client, sender_label) -> None:
    data = getattr(m, "data", None) or {}
    message_data = data.get("message_data", data) if isinstance(data, dict) else {}

    # 1) Tweet shared via DM → route through the yt-dlp pipeline (xcookies jar).
    tweet_url = _x_deep_find_tweet(message_data.get("attachment"))
    if tweet_url:
        _enqueue_relay(queue, chat_id,
                       lambda u=tweet_url: _download_and_deliver(
                           bot_client, premium_client, chat_id, u,
                           "X", sender_label))
        return

    # 2) Photo / video DM attachment.
    media_url, is_photo = _x_deep_find_media_url(message_data.get("attachment"))
    if media_url:
        loop = asyncio.get_event_loop()
        data_bytes = await loop.run_in_executor(None, _fetch_bytes, media_url, "https://x.com/")
        ext = ".jpg" if is_photo else ".mp4"
        path = f"cache/df_x_dm_{m.id}{ext}"
        os.makedirs("cache", exist_ok=True)
        with open(path, "wb") as f:
            f.write(data_bytes)
        await _deliver_raw_file(bot_client, chat_id, path, is_photo,
                                f"📥 **X DM** from {sender_label}")
        try:
            os.remove(path)
        except Exception:
            pass
        return

    # 3) Plain text with links.
    text = message_data.get("text", "") if isinstance(message_data, dict) else ""
    for u in URL_RE.findall(text):
        _enqueue_relay(queue, chat_id,
                       lambda u=u: _download_and_deliver(
                           bot_client, premium_client, chat_id, u,
                           "X", sender_label))


# =========================================================================
# Supervisor
# =========================================================================

async def _direct_forward_supervisor(bot_client, premium_client, chat_id: int) -> None:
    from utils.shared import queue

    workers = []
    if config.IG_DIRECT_ENABLED:
        workers.append(_instagram_worker(bot_client, premium_client, chat_id, queue))
    if config.X_DIRECT_ENABLED:
        workers.append(_twitter_worker(bot_client, premium_client, chat_id, queue))

    if not workers:
        logger.info("[DirectForward] No platform enabled (IG_DIRECT_ENABLED / "
                    "X_DIRECT_ENABLED) — direct-forward is off.")
        return

    logger.info(f"[DirectForward] started -> chat {chat_id}, {len(workers)} platform(s)")
    await asyncio.gather(*workers)


def start_direct_forward_task(bot_client, premium_client):
    """Create the background task. Called from main.py after clients are up.
    Returns the task, or None when the feature is unconfigured (no-op)."""
    chat_id = getattr(config, "DIRECT_FORWARD_CHAT_ID", 0)
    if not chat_id:
        logger.info("[DirectForward] DIRECT_FORWARD_CHAT_ID not set; direct-forward disabled.")
        return None
    if not (config.IG_DIRECT_ENABLED or config.X_DIRECT_ENABLED):
        logger.info("[DirectForward] No platform enabled; direct-forward disabled.")
        return None
    return asyncio.create_task(_direct_forward_supervisor(bot_client, premium_client, chat_id))

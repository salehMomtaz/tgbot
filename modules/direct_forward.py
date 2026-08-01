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
     xma shares, plain link, tweet share, X DM photo/video),
  2. downloads it (link items go through the normal yt-dlp pipeline WITH your
     cookie jars — cookie write-back keeps those jars fresh),
  3. sends the media to DIRECT_FORWARD_CHAT_ID with an info header showing the
     ORIGINAL POST AUTHOR (username + numeric id) and the post link; long
     captions are split: media caption at most 1024 chars, the remainder
     follows as separate text messages,
  4. advances a per-platform cursor in direct_forward_state.json so nothing is
     sent twice.

Pairing / protection
--------------------
Other people can DM the bot account too, so relays only happen from the PAIRED
partner. Pairing is a handshake:

  1. In Telegram: Admin Console → 📨 Direct-Forward → 🔗 Pair Instagram. The
     bot issues a one-time code (TTL 10 min).
  2. You send that code as an Instagram DM to the bot account.
  3. The worker sees the code, locks the pair to YOUR Instagram user id, and
     confirms in Telegram.

``IG_DIRECT_FROM_USERNAME`` in .env also acts as a static pre-pair (resolved to
a numeric user id once and persisted). X cannot offer an inbox-wide pairing
handshake (twikit can only read DM history of a known user id), so the X
protection is the numeric ``X_DIRECT_FROM_USER_ID`` itself.

Sessions persist to disk (direct_ig_session.json / direct_x_cookies.json).
First run primes the cursor and skips backlog. Delete direct_forward_state.json
to re-prime (this also clears the pairing). No third-party APIs.
"""

import asyncio
import json
import logging
import os
import random
import re
import time
from typing import Any

import config
from utils import cookie_manager

logger = logging.getLogger(__name__)

STATE_FILE = "direct_forward_state.json"
IG_SESSION_FILE = "direct_ig_session.json"
X_COOKIES_FILE = "direct_x_cookies.json"

URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"']+")
# xma share targets can point at reels/posts (/reel/, /p/, /tv/) OR stories
# (/stories/<user>/<media_id>/). yt-dlp handles all of them (stories via the
# cookie jar); the important part is we never fail to SEE the link.
IG_POST_RE = re.compile(r"(instagram\.com/(?:reel|reels|p|tv|stories)/[^\s?]+)")

TELEGRAM_CAPTION_LIMIT = 1024
TELEGRAM_TEXT_LIMIT = 4096


# =========================================================================
# State (dedup cursors + pairing)
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


def _get_pair(state: dict, platform: str) -> dict | None:
    pair = state.get(platform, {}).get("paired")
    return pair if isinstance(pair, dict) and pair.get("user_id") else None


def _set_pair(state: dict, platform: str, user_id: str | int, username: str = "") -> None:
    state.setdefault(platform, {})["paired"] = {
        "user_id": str(user_id),
        "username": username.lstrip("@"),
        "paired_at": time.time(),
    }


# =========================================================================
# Pairing handshake (one-time code issued from the Telegram admin console)
# =========================================================================

_PAIR_TTL_SECONDS = 600
_pending_pairs: dict[str, dict] = {}   # platform -> {code, expires_at, requested_by}


def request_pair_code(platform: str, requested_by: int) -> str:
    """Issue a one-time pairing code for *platform*. Called from the admin
    console; the corresponding DM worker picks it up on its next poll."""
    code = f"{random.SystemRandom().randint(0, 999999):06d}"
    _pending_pairs[platform] = {
        "code": code,
        "expires_at": time.time() + _PAIR_TTL_SECONDS,
        "requested_by": requested_by,
    }
    return code


def cancel_pairing(platform: str) -> None:
    _pending_pairs.pop(platform, None)


def unpair_platform(platform: str) -> bool:
    """Forget the paired DM contact for *platform*. Returns True when a pair
    existed. The worker re-reads the state on its next poll, so unlinking is
    effective within one poll interval without a restart."""
    state = _load_state()
    if _get_pair(state, platform):
        state.get(platform, {}).pop("paired", None)
        _save_state(state)
        return True
    return False


def pairing_status(platform: str, state: dict) -> str:
    pair = _get_pair(state, platform)
    pending = _pending_pairs.get(platform)
    pending_txt = ""
    if pending:
        if pending["expires_at"] > time.time():
            pending_txt = f" (code {pending['code']} pending, {int(pending['expires_at'] - time.time())}s left)"
        else:
            _pending_pairs.pop(platform, None)
    if pair:
        return f"paired with @{pair.get('username', '?')} (id {pair['user_id']}){pending_txt}"
    return f"not paired{pending_txt}"


# =========================================================================
# Delivery helpers
# =========================================================================

class _NullStatusMessage:
    """Stand-in for a Pyrogram status message: the download queue wants
    something it can edit, background relays have nothing to edit."""
    async def edit_text(self, _text: str) -> None:
        return


def _chunk_text(text: str, limit: int) -> list[str]:
    """Split *text* into <= limit-char chunks on paragraph/word boundaries."""
    if not text:
        return []
    chunks = []
    rest = text
    while rest:
        if len(rest) <= limit:
            chunks.append(rest)
            break
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip()
    return chunks


def _compose_caption(header_lines: list[str], body: str) -> tuple[str, list[str]]:
    """Return (media_caption <= 1024, followup_texts). Telegram media captions
    cap at 1024 chars; the overflow is sent as normal text messages (<= 4096)."""
    header = "\n".join(line for line in header_lines if line)
    if not body:
        return header, []
    full = f"{header}\n\n{body}" if header else body
    if len(full) <= TELEGRAM_CAPTION_LIMIT:
        return full, []
    budget = TELEGRAM_CAPTION_LIMIT - len(header) - 4  # "\n\n" + "…"
    first_body = body[:max(0, budget)] + "…"
    remainder = body[max(0, budget):]
    followups = _chunk_text(remainder, TELEGRAM_TEXT_LIMIT)
    return f"{header}\n\n{first_body}", followups


async def _send_followups(bot_client, chat_id: int, followups: list[str]) -> None:
    for chunk in followups:
        try:
            await bot_client.send_message(chat_id=chat_id, text=chunk)
        except Exception as e:
            logger.warning(f"[DirectForward] follow-up message failed: {e}")


async def _download_and_deliver(bot_client, premium_client, chat_id: int, url: str,
                                header_lines: list[str], body: str,
                                preview_url: str | None = None) -> None:
    """Download *url* via the normal yt-dlp pipeline (cookie jars included)
    and deliver it with the info header + body (split over follow-ups).
    Runs on the shared single-worker queue behind interactive downloads.

    When yt-dlp hard-fails and a *preview_url* (from the DM's share payload)
    exists, the preview image is delivered as a fallback so the user still
    sees what the share was, with a note that the full media failed."""
    from utils.downloader import download_media, probe_video_dimensions
    from utils.uploader_handler import process_split_and_upload
    import shutil
    import hashlib

    cache_id = f"df_{hashlib.md5(url.encode()).hexdigest()[:10]}"
    caption, followups = _compose_caption(header_lines, body)
    try:
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None, download_media, url, None, "v", cache_id, None, None, None, None,
            )
        except Exception as dl_error:
            if not preview_url:
                raise
            logger.warning(f"[DirectForward] yt-dlp failed for {url}: {dl_error} — delivering preview image")
            data = await loop.run_in_executor(None, _fetch_bytes, preview_url, "https://www.instagram.com/")
            path = f"cache/{cache_id}/preview.jpg"
            os.makedirs(f"cache/{cache_id}", exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
            await bot_client.send_photo(chat_id=chat_id, photo=path,
                                        caption=f"⚠️ (full media failed to download)\n{caption}")
            await _send_followups(bot_client, chat_id, followups)
            return

        file_path = result["file_path"]
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
                caption=caption,
            )
        await _send_followups(bot_client, chat_id, followups)
        logger.info(f"[DirectForward] ✅ relayed {url} -> {chat_id}")
    finally:
        task_dir = f"cache/{cache_id}"
        if os.path.exists(task_dir):
            try:
                shutil.rmtree(task_dir)
            except Exception:
                pass


def _enqueue_relay(queue, user_chat_id: int, corofn_factory) -> None:
    """Fire-and-forget enqueue of one relay job on the shared download queue."""
    async def _submit():
        await queue.add_task(user_id=user_chat_id, message=_NullStatusMessage(),
                             coroutine=corofn_factory)
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


def _header_lines(platform: str, sender_label: str, author_label: str | None,
                  author_id: str | None, post_url: str | None) -> list[str]:
    lines = [f"📥 **{platform} DM** from {sender_label}"]
    if author_label:
        if author_id:
            lines.append(f"👤 **Post by @{author_label}** (id: `{author_id}`)")
        else:
            lines.append(f"👤 **Post by @{author_label}**")
    if post_url:
        lines.append(f"🔗 {post_url}")
    return lines


# =========================================================================
# Instagram DM worker (instagrapi)
# =========================================================================

_uid_cache: dict[str, str] = {}
_ig_api_lock = asyncio.Lock()   # serialize instagrapi calls across executor runs


# -------------------------------------------------------------------------
# IG DM item resolution — over the RAW direct_v2/threads payload.
#
# We deliberately do NOT use instagrapi's typed `direct_messages()` here: its
# DirectMessage model *drops* fields that matter for shares — notably
# ``original_media_igid`` (the story/post's media pk on photo shares) and
# ``auxiliary_text`` ("Sent @user's story highlight"). The raw dicts keep
# everything, so every item type can be resolved.
# -------------------------------------------------------------------------

def _ig_raw_xma_entries(item: dict) -> list[dict]:
    """Collect XMA share-entry dicts from a raw DM item, whatever key they sit
    under (``xma_share``, ``xma_story_share``, ``xma_media_share``, ...) —
    Instagram renames these between app versions and instagrapi versions."""
    entries: list[dict] = []
    for v in (item or {}).values():
        if isinstance(v, list):
            for e in v:
                if isinstance(e, dict) and any(
                        k in e for k in ("preview_url", "video_url", "target_url",
                                         "header_title_text", "xma_layout_type")):
                    entries.append(e)
        elif isinstance(v, dict) and any(
                k in v for k in ("preview_url", "video_url", "target_url")):
            entries.append(v)
    return entries


def _ig_resolve_raw(item: dict) -> dict | None:
    """Normalize one raw DM item into a relayable form:
      {"kind": "url",       url, author, preview, body}   — share with a link (XMA/legacy)
      {"kind": "ig_pk",     pk, author, preview}          — share identified only by media pk (photo stories/highlights)
      {"kind": "attachment", url, is_photo}               — photo/video uploaded straight into the chat
      {"kind": "text_urls", urls}                         — plain text containing links
    Returns None when the item carries nothing relayable."""
    item_type = (item.get("item_type") or "").lower()

    # 1) XMA containers (modern clips & post/story shares)
    xma_entries = _ig_raw_xma_entries(item)
    for e in xma_entries:
        link = e.get("video_url") or e.get("target_url")
        if link and "instagram.com" in str(link):
            m = IG_POST_RE.search(str(link))
            if m:
                return {"kind": "url",
                        "url": f"https://www.{m.group(1).rstrip('/')}/",
                        "author": e.get("header_title_text"),
                        "preview": e.get("preview_url"),
                        "body": e.get("title") or e.get("title_text") or ""}

    # 2) Story/highlight shares and pk-only items: original_media_igid points
    #    at the underlying media (works for expired stories inside highlights).
    pk = item.get("original_media_igid")
    if pk:
        author = None
        aux = item.get("auxiliary_text") or ""
        am = re.search(r"@([A-Za-z0-9_.]+)", aux)
        if am:
            author = am.group(1)
        if not author and xma_entries:
            author = xma_entries[0].get("header_title_text")
        return {"kind": "ig_pk", "pk": int(pk), "author": author,
                "preview": xma_entries[0].get("preview_url") if xma_entries else None}

    # 3) Direct media attachment (photo / video uploaded into the chat)
    media = item.get("media") or item.get("visual_media")
    if isinstance(media, dict):
        video_url = media.get("video_url")
        img = None
        iv = media.get("image_versions2")
        if isinstance(iv, dict):
            cands = iv.get("candidates") or []
            if cands:
                img = cands[0].get("url")
        src = video_url or media.get("thumbnail_url") or img
        if src:
            return {"kind": "attachment", "url": src, "is_photo": not bool(video_url)}

    # 4) Classic share containers (older app versions)
    for key in ("reel_share", "media_share", "felix_share", "clip"):
        payload = item.get(key)
        if not isinstance(payload, dict):
            continue
        md = payload.get("media") or payload.get("clip") or payload
        if not isinstance(md, dict):
            continue
        code = md.get("code")
        pk2 = md.get("pk") or md.get("id")
        user = md.get("user") or {}
        author = user.get("username") if isinstance(user, dict) else None
        if code:
            return {"kind": "url", "url": f"https://www.instagram.com/reel/{code}/",
                    "author": author, "preview": None,
                    "body": (md.get("caption") or {}).get("text", "") if isinstance(md.get("caption"), dict) else ""}
        if pk2:
            return {"kind": "ig_pk", "pk": int(pk2), "author": author, "preview": None}

    # 5) Plain text with links
    text = item.get("text") or ""
    urls = URL_RE.findall(text)
    if urls:
        return {"kind": "text_urls", "urls": urls}
    return None


def _ig_pk_from_url(cl, url: str) -> int:
    """Extract the media pk from any Instagram URL form."""
    story = re.search(r"instagram\.com/stories/[^/]+/(\d+)", url)
    if story:
        return int(story.group(1))
    return int(cl.media_pk_from_url(url))


async def _ig_native_deliver_once(bot_client, chat_id, cl, pk: int,
                                  header_lines: list[str], body: str,
                                  url: str | None = None) -> bool:
    """Deliver one Instagram post/story natively through the logged-in
    instagrapi session — photo posts and carousels reliably break yt-dlp's
    extractor ('No video formats found'), so photos/albums download straight
    from the CDN here. Returns False (so the caller can fall back to yt-dlp)
    when the media is actually a reel (clips product type)."""
    loop = asyncio.get_event_loop()
    async with _ig_api_lock:
        media = await loop.run_in_executor(None, cl.media_info, pk)

    mt = getattr(media, "media_type", None)        # 1=photo, 2=video, 8=album
    product_type = getattr(media, "product_type", None)

    user = getattr(media, "user", None)
    if user is not None and getattr(user, "username", None):
        sender_label = header_lines[0].split(" from ", 1)[-1] if header_lines and " from " in header_lines[0] else "paired contact"
        if not url:
            code = getattr(media, "code", None)
            if code:
                seg = "reel" if product_type == "clips" else "p"
                url = f"https://www.instagram.com/{seg}/{code}/"
            else:
                url = f"https://www.instagram.com/stories/{user.username}/{pk}/"
        header_lines = _header_lines("Instagram", sender_label, user.username,
                                     str(getattr(user, "pk", "")), url)
        body = getattr(media, "caption_text", None) or body
    if mt == 2 and product_type == "clips":
        return False  # reel: yt-dlp handles quality/merge properly

    targets: list[tuple[str, bool]] = []           # (cdn_url, is_video)
    if mt == 8 and getattr(media, "resources", None):
        for r in media.resources[:10]:             # Telegram media groups cap at 10
            v = getattr(r, "video_url", None) or getattr(r, "thumbnail_url", None)
            if v:
                targets.append((str(v), bool(getattr(r, "video_url", None))))
    elif mt in (1, 2):
        v = getattr(media, "video_url", None) or getattr(media, "thumbnail_url", None)
        if v:
            targets.append((str(v), bool(getattr(media, "video_url", None))))
    if not targets:
        raise RuntimeError(f"IG media {pk}: no downloadable media version")

    caption, followups = _compose_caption(header_lines, body)
    os.makedirs("cache", exist_ok=True)
    files: list[tuple[str, bool]] = []
    try:
        for i, (src, is_video) in enumerate(targets):
            data = await loop.run_in_executor(None, _fetch_bytes, src, "https://www.instagram.com/")
            path = f"cache/df_native_{pk}_{i}{'.mp4' if is_video else '.jpg'}"
            with open(path, "wb") as f:
                f.write(data)
            files.append((path, is_video))
        if len(files) == 1:
            path, is_video = files[0]
            if is_video:
                await bot_client.send_video(chat_id=chat_id, video=path,
                                            caption=caption, supports_streaming=True)
            else:
                await bot_client.send_photo(chat_id=chat_id, photo=path, caption=caption)
        else:
            from pyrogram.types import InputMediaPhoto, InputMediaVideo
            group = [
                (InputMediaVideo(p, caption=caption if j == 0 else "") if is_video
                 else InputMediaPhoto(p, caption=caption if j == 0 else ""))
                for j, (p, is_video) in enumerate(files)
            ]
            await bot_client.send_media_group(chat_id=chat_id, media=group)
        await _send_followups(bot_client, chat_id, followups)
        logger.info(f"[DirectForward/IG] ✅ native relayed {url} (media_type={mt}, {len(files)} file(s))")
        return True
    finally:
        for p, _f in files:
            try:
                os.remove(p)
            except Exception:
                pass


def _enqueue_ig_relay(queue, chat_id, bot_client, premium_client, cl,
                      url: str, header_lines: list[str], body: str,
                      preview_url: str | None) -> None:
    """Route one resolved Instagram link: probe the media pk, deliver photos/
    albums/stories natively, reels via yt-dlp (quality merge), preview image
    or yt-dlp as fallbacks."""
    async def job():
        is_ig = "instagram.com" in url
        pk = None
        if is_ig:
            loop = asyncio.get_event_loop()
            async with _ig_api_lock:
                try:
                    pk = await loop.run_in_executor(None, _ig_pk_from_url, cl, url)
                except Exception as e:
                    logger.warning(f"[DirectForward/IG] could not resolve media pk for {url}: {e}")
        if pk and "/reel/" not in url:
            # posts / carousels / stories: native-first (yt-dlp breaks on them)
            try:
                if await _ig_native_deliver_once(bot_client, chat_id, cl, pk, header_lines, body, url):
                    return
            except Exception as e:
                logger.warning(f"[DirectForward/IG] native path failed for {url}: {e} — falling back to yt-dlp")
        if is_ig and "/reel/" in url:
            # reels: yt-dlp-first (quality merge), native as fallback
            try:
                await _download_and_deliver(bot_client, premium_client, chat_id, url,
                                            header_lines, body, None)
                return
            except Exception as e:
                logger.warning(f"[DirectForward/IG] yt-dlp reel failed for {url}: {e} — trying native")
                if pk:
                    try:
                        if await _ig_native_deliver_once(bot_client, chat_id, cl, pk, header_lines, body, url):
                            return
                    except Exception as e2:
                        logger.warning(f"[DirectForward/IG] native reel fallback failed: {e2}")
        await _download_and_deliver(bot_client, premium_client, chat_id, url,
                                    header_lines, body, preview_url)
    _enqueue_relay(queue, chat_id, job)


def _ig_resolve_user_id(cl, username: str) -> str | None:
    username = (username or "").lstrip("@")
    if not username:
        return None
    if username in _uid_cache:
        return _uid_cache[username]
    try:
        uid = str(cl.user_id_from_username(username))
        _uid_cache[username] = uid
        return uid
    except Exception:
        return None


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


async def _ig_process_message(item: dict, cl, loop, queue, chat_id,
                              bot_client, premium_client, paired_username: str) -> None:
    """Process one RAW direct_v2 DM item (dict) from the paired contact."""
    sender_label = f"@{paired_username}" if paired_username else "paired contact"
    item_id = item.get("item_id", "?")
    item_type = (item.get("item_type") or "").lower()
    resolved = _ig_resolve_raw(item)
    kind = (resolved or {}).get("kind")

    if kind == "url":
        header = _header_lines("Instagram", sender_label,
                               resolved.get("author"),
                               _ig_resolve_user_id(cl, resolved.get("author")) if resolved.get("author") else None,
                               resolved["url"])
        logger.info(f"[DirectForward/IG] item {item_id}: {item_type} -> {resolved['url']} (author @{resolved.get('author')})")
        _enqueue_ig_relay(queue, chat_id, bot_client, premium_client, cl,
                          resolved["url"], header, resolved.get("body") or "",
                          resolved.get("preview"))
        return

    if kind == "ig_pk":
        pk = resolved["pk"]
        author = resolved.get("author")
        header = _header_lines("Instagram", sender_label, author,
                               _ig_resolve_user_id(cl, author) if author else None, None)
        logger.info(f"[DirectForward/IG] item {item_id}: {item_type} -> native pk {pk} (author @{author})")

        async def job(pk=pk, header=header, preview=resolved.get("preview")):
            try:
                ok = await _ig_native_deliver_once(bot_client, chat_id, cl, pk, header, "", None)
                if ok:
                    return
            except Exception as e:
                logger.warning(f"[DirectForward/IG] native pk {pk} failed: {e}")
            if preview:
                data = await asyncio.get_event_loop().run_in_executor(
                    None, _fetch_bytes, preview, "https://www.instagram.com/")
                path = f"cache/df_pk_{pk}.jpg"
                with open(path, "wb") as f:
                    f.write(data)
                cap = "⚠️ (full media unavailable)\n" + "\n".join(header) if header else "⚠️ media"
                await bot_client.send_photo(chat_id=chat_id, photo=path, caption=cap)
                try:
                    os.remove(path)
                except Exception:
                    pass
        _enqueue_relay(queue, chat_id, job)
        return

    if kind == "attachment":
        src, is_photo = resolved["url"], resolved["is_photo"]
        data = await loop.run_in_executor(None, _fetch_bytes, src, "https://www.instagram.com/")
        ext = ".jpg" if is_photo else ".mp4"
        path = f"cache/df_ig_dm_{item_id}{ext}"
        os.makedirs("cache", exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        caption, followups = _compose_caption(
            _header_lines("Instagram", sender_label, None, None, None), "")
        if is_photo:
            await bot_client.send_photo(chat_id=chat_id, photo=path, caption=caption)
        else:
            await bot_client.send_video(chat_id=chat_id, video=path,
                                        caption=caption, supports_streaming=True)
        await _send_followups(bot_client, chat_id, followups)
        try:
            os.remove(path)
        except Exception:
            pass
        logger.info(f"[DirectForward/IG] item {item_id}: {item_type} -> direct attachment delivered")
        return

    if kind == "text_urls":
        for u in resolved["urls"]:
            header = _header_lines("Instagram", sender_label, None, None, u)
            logger.info(f"[DirectForward/IG] item {item_id}: text url -> {u}")
            if "instagram.com" in u:
                _enqueue_ig_relay(queue, chat_id, bot_client, premium_client, cl,
                                  u, header, "", None)
            else:
                _enqueue_relay(queue, chat_id,
                               lambda u=u, h=header: _download_and_deliver(
                                   bot_client, premium_client, chat_id, u, h, ""))
        return

    logger.info(f"[DirectForward/IG] item {item_id}: {item_type!r} has no relayable media — skipped")


async def _ig_pairing_scan(item: dict, thread_users: dict, state: dict,
                           bot_client, chat_id: int) -> bool:
    """Check one raw DM item for an active pairing code; on match, lock the
    pair and confirm in Telegram. Returns True when the message was consumed."""
    pending = _pending_pairs.get("ig")
    if not pending:
        return False
    if pending["expires_at"] <= time.time():
        _pending_pairs.pop("ig", None)
        return False
    text = (item.get("text") or "").strip()
    if pending["code"] not in text:
        return False
    sender_uid = str(item.get("user_id") or "")
    username = thread_users.get(sender_uid, "")
    if not sender_uid:
        return False
    _set_pair(state, "ig", sender_uid, username)
    _save_state(state)
    _pending_pairs.pop("ig", None)
    try:
        await bot_client.send_message(
            chat_id=chat_id,
            text=(f"✅ **Instagram paired!** Found our chat: this bot's account ↔ "
                  f"@{username or sender_uid} (id `{sender_uid}`).\n"
                  f"From now on, media you DM to the bot's Instagram account will "
                  f"be relayed here automatically. Other people's DMs are ignored."),
        )
    except Exception as e:
        logger.warning(f"[DirectForward/IG] pairing confirmation failed: {e}")
    logger.info(f"[DirectForward/IG] paired with @{username} (id {sender_uid}) via handshake code")
    return True


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

    state = _load_state()

    # Static pre-pair from .env (bootstrap for existing setups). Resolves the
    # handle to a numeric user id once and persists it as the pair.
    if not _get_pair(state, "ig") and config.IG_DIRECT_FROM_USERNAME:
        uid = await loop.run_in_executor(None, _ig_resolve_user_id, cl, config.IG_DIRECT_FROM_USERNAME)
        if uid:
            _set_pair(state, "ig", uid, config.IG_DIRECT_FROM_USERNAME)
            _save_state(state)
            logger.info(f"[DirectForward/IG] pre-paired with @{config.IG_DIRECT_FROM_USERNAME} (id {uid}) from .env")

    if "ig" not in state or "last_id" not in state.get("ig", {}):
        state.setdefault("ig", {"last_id": "0"})
        _save_state(state)
        logger.info("[DirectForward/IG] first run — priming cursor, backlog is skipped.")
        try:
            threads = await loop.run_in_executor(None, lambda: cl.direct_threads(amount=20))
            last = 0
            for th in threads:
                raw = await loop.run_in_executor(
                    None, lambda tid=th.id: cl.private_request(f"direct_v2/threads/{tid}/", params={"limit": 1}))
                items = (raw or {}).get("thread", {}).get("items", [])
                if items:
                    try:
                        last = max(last, int(items[0]["item_id"]))
                    except Exception:
                        pass
            if last:
                _bump_cursor(state, "ig", last)
                _save_state(state)
        except Exception as e:
            logger.warning(f"[DirectForward/IG] priming peek failed: {e}")

    poll = max(30, config.DIRECT_FORWARD_POLL_SECONDS)
    pair = _get_pair(state, "ig")
    if pair:
        logger.info(f"[DirectForward/IG] polling DMs from @{pair['username']} (id {pair['user_id']}) every {poll}s")
    else:
        logger.info("[DirectForward/IG] no pair yet — waiting for pairing handshake "
                    "(Admin Console → Direct-Forward → Pair Instagram) or a .env pre-pair.")

    while True:
        try:
            threads = await loop.run_in_executor(None, lambda: cl.direct_threads(amount=20))
            pair = _get_pair(state, "ig")
            pairing_active = "ig" in _pending_pairs

            for th in threads:
                if th.is_group:
                    continue
                thread_users = {str(u.pk): u.username for u in (th.users or [])}
                pair_uid = pair["user_id"] if pair else None
                # We only ever read threads that contain the paired partner,
                # EXCEPT while a pairing handshake is pending (scan everything).
                if not pairing_active and (not pair_uid or pair_uid not in thread_users):
                    continue
                raw = await loop.run_in_executor(
                    None, lambda tid=th.id: cl.private_request(f"direct_v2/threads/{tid}/", params={"limit": 25}))
                items = ((raw or {}).get("thread", {}) or {}).get("items", []) or []
                last_seen = _cursor(state, "ig")
                new_msgs = []
                for m in items:
                    if m.get("is_sent_by_viewer"):
                        continue
                    try:
                        if int(m["item_id"]) > last_seen:
                            new_msgs.append(m)
                    except Exception:
                        logger.warning(f"[DirectForward/IG] weird item id {m.get('item_id', '!')!r} skip")
                new_msgs.sort(key=lambda m: int(m["item_id"]))

                pair_username = ""
                if pair:
                    pair_username = pair.get("username", "")

                for m in new_msgs:
                    consumed = False
                    try:
                        if pairing_active:
                            consumed = await _ig_pairing_scan(m, thread_users, state,
                                                              bot_client, chat_id)
                            pair = _get_pair(state, "ig")
                            pairing_active = "ig" in _pending_pairs
                        if not consumed and pair and str(m.get("user_id", "")) == pair["user_id"]:
                            await _ig_process_message(m, cl, loop, queue, chat_id,
                                                      bot_client, premium_client, pair.get("username", ""))
                    except Exception as e:
                        logger.error(f"[DirectForward/IG] item {m.get('item_id', '?')} failed: {e}")
                    # Always advance past attempted items: one bad DM must not
                    # block the relay forever.
                    try:
                        _bump_cursor(state, "ig", int(m["item_id"]))
                    except Exception:
                        pass
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


def _x_deep_find_tweet(node: Any) -> tuple[str | None, str]:
    """Find a shared tweet inside a DM attachment tree → (status URL, text)."""
    if isinstance(node, dict):
        legacy = node.get("legacy") if isinstance(node.get("legacy"), dict) else None
        if "rest_id" in node and legacy is not None:
            return f"https://x.com/i/status/{node['rest_id']}", legacy.get("full_text", "")
        if "id_str" in node and "full_text" in node:
            return f"https://x.com/i/status/{node['id_str']}", node.get("full_text", "")
        for v in node.values():
            found, txt = _x_deep_find_tweet(v)
            if found:
                return found, txt
    elif isinstance(node, list):
        for v in node:
            found, txt = _x_deep_find_tweet(v)
            if found:
                return found, txt
    return None, ""


async def _x_process_message(m, queue, chat_id, bot_client, premium_client, sender_label) -> None:
    data = getattr(m, "data", None) or {}
    message_data = data.get("message_data", data) if isinstance(data, dict) else {}

    # 1) Tweet shared via DM → route through the yt-dlp pipeline (xcookies jar).
    tweet_url, tweet_text = _x_deep_find_tweet(message_data.get("attachment"))
    if tweet_url:
        header = _header_lines("X", sender_label, None, None, tweet_url)
        _enqueue_relay(queue, chat_id,
                       lambda u=tweet_url, h=header, b=tweet_text: _download_and_deliver(
                           bot_client, premium_client, chat_id, u, h, b))
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
        caption, followups = _compose_caption(
            _header_lines("X", sender_label, None, None, None), "")
        if is_photo:
            await bot_client.send_photo(chat_id=chat_id, photo=path, caption=caption)
        else:
            await bot_client.send_video(chat_id=chat_id, video=path,
                                        caption=caption, supports_streaming=True)
        await _send_followups(bot_client, chat_id, followups)
        try:
            os.remove(path)
        except Exception:
            pass
        return

    # 3) Plain text with links.
    text = message_data.get("text", "") if isinstance(message_data, dict) else ""
    urls = URL_RE.findall(text)
    for u in urls:
        header = _header_lines("X", sender_label, None, None, u)
        _enqueue_relay(queue, chat_id,
                       lambda u=u, h=header: _download_and_deliver(
                           bot_client, premium_client, chat_id, u, h, ""))
    if not urls:
        logger.info(f"[DirectForward/X] message {m.id}: no relayable media — skipped")


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

    sender_label = f"x-user `{config.X_DIRECT_FROM_USER_ID}`"
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
                    await _x_process_message(m, queue, chat_id, bot_client, premium_client, sender_label)
                except Exception as e:
                    logger.error(f"[DirectForward/X] message {m.id} failed: {e}")
                _bump_cursor(state, "x", int(m.id))
            if new_msgs:
                _save_state(state)
        except Exception as e:
            logger.error(f"[DirectForward/X] poll error: {e}")
            await asyncio.sleep(min(600, poll))

        await asyncio.sleep(poll)


# =========================================================================
# Supervisor + admin-console surface
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

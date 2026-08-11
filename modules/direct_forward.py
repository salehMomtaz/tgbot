# modules/direct_forward.py
"""
Direct-forward: relay media you send to the bot's own Instagram account, to
YOUR OWN X self-DM, or to YOUR OWN TikTok self-DM straight into your Telegram
chat.

How it works
------------
Instagram uses a DEDICATED bot account: from your personal account you open
the chat with the bot account and share a post / reel / story / photo / video
— or just paste a link. The bot polls its own DM inbox.

X/Twitter uses the SELF-DM method: no separate bot account, no pairing. You
send tweet links / photos / videos to your OWN X self-DM (Message Yourself),
and the bot — authenticated with the same cookies yt-dlp already uses
(``cookies/twitter/xcookies.txt``) — polls that one conversation and relays it:

  1. resolves what each new message contains (tweet share, X DM photo/video,
     plain link),
  2. downloads it (link items go through the normal yt-dlp pipeline WITH your
     cookie jars — cookie write-back keeps those jars fresh). Tweet links pick
     the HIGHEST available quality automatically; when that exceeds the upload
     ceiling (2 GB bot / 4 GB Premium) the usual format-selection keyboard is
     posted instead so you can pick a smaller quality. Photo-only tweets (no
     video stream for yt-dlp) are delivered natively from the share's CDN URLs.
  3. sends the media to DIRECT_FORWARD_CHAT_ID with an info header showing the
     ORIGINAL POST AUTHOR (username + numeric id) and the post link; long
     captions are split: media caption at most 1024 chars, the remainder
     follows as separate text messages,
  4. advances a per-platform cursor in direct_forward_state.json so nothing is
     sent twice. The state file is SHARED by the IG, X and TikTok workers, so
     every save must be MERGE-ONLY for the caller's own platform section
     (_state_save_owned / _merge_state_save) — never a full-dict _save_state,
     or a stale snapshot reverts another platform's cursor and its whole
     backlog re-relays (see docs/memory/tgbot-2026-08-11-x-duplicate-delivery-state-race.md).

Instagram pairing / protection
------------------------------
Other people can DM the bot's IG account too, so IG relays only happen from
the PAIRED partner. Pairing is a handshake:

  1. In Telegram: Admin Console → 📨 Direct-Forward → 🔗 Pair Instagram. The
     bot issues a one-time code (TTL 10 min).
  2. You send that code as an Instagram DM to the bot account.
  3. The worker sees the code, locks the pair to YOUR Instagram user id, and
     confirms in Telegram.

``IG_DIRECT_FROM_USERNAME`` in .env also acts as a static pre-pair (resolved to
a numeric user id once and persisted). X needs NO pairing — the self-DM
conversation ``<self_id>-<self_id>`` is only reachable by the account itself,
so there is nothing to lock down. ``X_DIRECT_USERNAME`` / ``X_DIRECT_PASSWORD``
are no longer used; the X worker boots from the shared xcookies jar.

Anti-detection posture (Instagram)
----------------------------------
The poller is the account's loudest signal, so it behaves like a human, not a
cron job: jittered poll intervals (base ±40%), 2–4 s random pacing between
private-API requests, per-thread activity watermarks (inbox listing marks
unchanged threads ⇒ zero item fetches while idle), one stable session/device
persisted to disk, optional single fixed proxy for the account's lifetime, and
multi-hour freezes on checkpoint challenges instead of retry storms.
``DIRECT_FORWARD_POLL_SECONDS`` / ``_JITTER_PCT`` / ``DIRECT_FORWARD_PROXY``
tune it in .env. See docs/DIRECT_FORWARD_SETUP.md → "Avoiding checkpoints".

On top of the pacing, the private API no longer speaks Python's stock TLS
fingerprint (an instant "this is a script" tell): ``utils/ig_anti_detect.py``
mounts a curl_cffi-backed TLS-impersonating transport on the instagrapi
session, captures and persists Instagram's echo headers (IG-U-RUR / IG-U-SHBID
/ IG-U-SHBTS / X-IG-WWW-Claim / X-MID) and re-applies them on every request,
pins country/locale/timezone to the account's home region, and runs a short
paced warmup after login. Every piece degrades to a no-op on failure so the
worker always survives a library hiccup. Checkpoint hits now also alert the
relay chat directly (not just the log channel) with instructions to pass the
verification in the official app.

Sessions: Instagram persists to direct_ig_session.json; the X worker has NO
session file — it rides the shared xcookies jar (cookies/twitter/xcookies.txt)
that yt-dlp keeps warm via write-back. First run primes the cursor and skips
backlog. Delete direct_forward_state.json to re-prime (this also clears the IG
pairing). No third-party APIs.
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
from utils import ig_anti_detect

logger = logging.getLogger(__name__)

STATE_FILE = "direct_forward_state.json"
IG_SESSION_FILE = "direct_ig_session.json"

URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"']+")
# xma share targets can point at reels/posts (/reel/, /p/, /tv/) OR stories
# (/stories/<user>/<media_id>/). yt-dlp handles all of them (stories via the
# cookie jar); the important part is we never fail to SEE the link.
IG_POST_RE = re.compile(r"(instagram\.com/(?:reel|reels|p|tv|stories)/[^\s?]+)")

TELEGRAM_CAPTION_LIMIT = 1024
TELEGRAM_TEXT_LIMIT = 4096


def _poll_interval() -> float:
    """Effective delay between inbox sweeps: base ± jitter.

    Instagram flags machine-perfect fixed-cadence polling far more easily than
    a humanized, jittered several-minute rhythm (instagrapi best-practices:
    read-only monitoring should poll "several minutes rather than seconds").
    """
    base = max(60, config.DIRECT_FORWARD_POLL_SECONDS)
    jitter_pct = max(0, min(90, getattr(config, "DIRECT_FORWARD_POLL_JITTER_PCT", 40)))
    if jitter_pct:
        base *= random.uniform(1 - jitter_pct / 100, 1 + jitter_pct / 100)
    return base


def _activity_stamp(thread) -> str:
    """Serialize a thread's last_activity_at for the state watermark map."""
    last_act = getattr(thread, "last_activity_at", None)
    try:
        return last_act.isoformat() if last_act else ""
    except Exception:
        return ""


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


# The three workers (IG/X/TikTok) share ONE state file. Writing the whole
# in-memory dict on every save lets a stale snapshot clobber another
# platform's cursor — the race that made X self-DM posts relay repeatedly
# (the IG worker loaded state once and each of its saves reverted the X
# cursor, so the whole X backlog re-relayed on every IG poll). Every worker
# must persist via _merge_state_save / _state_save_owned: merge ONLY its own
# platform section over the freshest on-disk state, never a full-dict write.
_STATE_LOCK = asyncio.Lock()


def _merge_state_save(state: dict[str, Any], owned: set[str]) -> dict[str, Any]:
    """Merge only the caller's *owned* platform sections over the freshest
    on-disk state and write it back atomically (tmp+rename). Refreshes
    *state* in place with the merged result so later reads see other
    workers' cursor advances. Fully synchronous, so it cannot be
    interleaved by other coroutines on the event-loop thread."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            disk = json.load(f)
    except Exception:
        disk = {}
    for plat in owned:
        if plat in state:
            disk[plat] = state[plat]
    _save_state(disk)
    state.clear()
    state.update(disk)
    return state


async def _state_save_owned(state: dict[str, Any], owned: set[str]) -> dict[str, Any]:
    """Async variant of _merge_state_save, serialized by _STATE_LOCK."""
    async with _STATE_LOCK:
        return _merge_state_save(state, owned)


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
    existed. Also cancels any pending pairing handshake for the platform. The
    worker re-reads the state on its next poll, so unlinking is effective
    within one poll interval without a restart."""
    state = _load_state()
    _pending_pairs.pop(platform, None)
    if _get_pair(state, platform):
        state.get(platform, {}).pop("paired", None)
        _merge_state_save(state, {platform})
        return True
    return False


def set_platform_pair(platform: str, user_id: str | int, username: str = "") -> None:
    """Persist a paired DM contact for *platform* directly (no handshake).
    Used for the admin console's manual numeric-id entry; the worker picks it
    up on its next poll."""
    state = _load_state()
    _set_pair(state, platform, user_id, username)
    _merge_state_save(state, {platform})


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
                premium_allowed=True,
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


def _video_upload_kwargs(file_path: str) -> dict:
    """Full send_video metadata for a raw CDN video (DM attachments, carousels).

    Raw CDN bytes have no sidecar cover file, so probe width/height/duration
    and generate a frame thumbnail — otherwise every such upload would arrive
    thumbless (Telegram only auto-generates thumbs for <10MB videos).
    Best-effort: never raises, so a corrupt file still uploads as before.
    """
    from utils.downloader import probe_video_dimensions, extract_video_frame_thumb
    width, height, duration = probe_video_dimensions(file_path)
    return {
        "width": width,
        "height": height,
        "duration": duration,
        "thumb": extract_video_frame_thumb(file_path),
        "supports_streaming": True,
    }


def _x_media_payload_ok(data: bytes, is_photo: bool) -> bool:
    """Validate a fetched CDN payload by MAGIC BYTES, never by size — X serves
    legitimately tiny images (a 133-byte solid PNG is a real photo). Reject only
    when the bytes are neither a real image nor a real mp4, or are an HTML
    interstitial (login wall / challenge page)."""
    head = data[:16]
    img_ok = (head.startswith(b"\x89PNG\r\n\x1a\n") or head.startswith(b"\xff\xd8\xff")
              or head.startswith(b"GIF87a") or head.startswith(b"GIF89a")
              or head.startswith(b"RIFF") or head.startswith(b"BM"))
    mp4_ok = len(data) >= 8 and data[4:8] == b"ftyp"
    lower = data[:256].lstrip().lower()
    if lower.startswith(b"<html") or lower.startswith(b"<!doctype"):
        return False
    return img_ok if is_photo else (mp4_ok or img_ok)


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
            try:
                data = await loop.run_in_executor(None, _fetch_bytes, src, "https://www.instagram.com/")
            except Exception as e:
                logger.warning(f"[DirectForward/IG] resource {i} download failed: {e} — skipping")
                continue
            # Guard against empty / interstitial-HTML payloads. Instagram's CDN
            # occasionally 200s a dead resource with an empty body or an HTML
            # error page; Telegram rejects those with [400 MEDIA_EMPTY], which
            # used to sink the ENTIRE carousel group.
            head = data[:256].lstrip().lower()
            if len(data) < 500 or head.startswith(b"<html") or head.startswith(b"<!doctype"):
                logger.warning(f"[DirectForward/IG] resource {i} invalid payload ({len(data)}B) — skipping")
                continue
            path = f"cache/df_native_{pk}_{i}{'.mp4' if is_video else '.jpg'}"
            with open(path, "wb") as f:
                f.write(data)
            files.append((path, is_video))
        if not files:
            raise RuntimeError(f"IG media {pk}: all {len(targets)} resource(s) were empty/invalid")
        if len(files) == 1:
            path, is_video = files[0]
            if is_video:
                await bot_client.send_video(chat_id=chat_id, video=path,
                                            caption=caption, **_video_upload_kwargs(path))
            else:
                await bot_client.send_photo(chat_id=chat_id, photo=path, caption=caption)
        else:
            from pyrogram.types import InputMediaPhoto, InputMediaVideo
            group = [
                (InputMediaVideo(p, caption=caption if j == 0 else "",
                                 **_video_upload_kwargs(p)) if is_video
                 else InputMediaPhoto(p, caption=caption if j == 0 else ""))
                for j, (p, is_video) in enumerate(files)
            ]
            try:
                await bot_client.send_media_group(chat_id=chat_id, media=group)
            except Exception:
                # One corrupt item still inside the group can fail the whole
                # call (MEDIA_EMPTY). Send the survivors individually instead
                # of dropping the post entirely.
                sent_any = False
                for j, (p, is_video) in enumerate(files):
                    try:
                        if is_video:
                            await bot_client.send_video(chat_id=chat_id, video=p,
                                                        caption=caption if not sent_any else "",
                                                        **_video_upload_kwargs(p))
                        else:
                            await bot_client.send_photo(chat_id=chat_id, photo=p,
                                                        caption=caption if not sent_any else "")
                        sent_any = True
                    except Exception as e:
                        logger.warning(f"[DirectForward/IG] item {j} individual send failed: {e}")
                if not sent_any:
                    raise
        await _send_followups(bot_client, chat_id, followups)
        logger.info(f"[DirectForward/IG] ✅ native relayed {url} (media_type={mt}, {len(files)} file(s))")
        return True
    finally:
        for p, _f in files:
            for candidate in (p, f"{os.path.splitext(p)[0]}_thumb.jpg"):
                try:
                    os.remove(candidate)
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
            # Validate the persisted session WITHOUT calling login(): instagrapi's
            # login() demands both username+password, but a good persisted session
            # needs neither — account_info() alone proves it's alive. Requiring a
            # password here made the resume path dead-on-arrival for setups that
            # rely on cookie-jar sessionids (the common direct-forward config).
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
                                        caption=caption, **_video_upload_kwargs(path))
        await _send_followups(bot_client, chat_id, followups)
        for candidate in (path, f"{os.path.splitext(path)[0]}_thumb.jpg"):
            try:
                os.remove(candidate)
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
    await _state_save_owned(state, {"ig"})
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
    # Human pacing between every private-API request (instagrapi best practice:
    # random, short delays — never machine-paced bursts).
    ig_proxy = getattr(config, "DIRECT_FORWARD_PROXY", None)

    def _make_client():
        c = IGClient()
        c.delay_range = [2, 4]
        # One STABLE proxy for the whole account lifetime (residential preferred);
        # churning IPs is a top checkpoint trigger.
        if ig_proxy:
            c.set_proxy(ig_proxy)
            logger.info("[DirectForward/IG] using configured DIRECT_FORWARD_PROXY for the DM session")
        # Anti-detection hardening (utils/ig_anti_detect.py): TLS-impersonating
        # transport, geo pinning, and echo-token capture. Each piece degrades
        # to a no-op on failure, so a library hiccup never kills the worker.
        try:
            ig_anti_detect.install_transport(
                c, getattr(config, "IG_DIRECT_TRANSPORT_IMPERSONATE", "chrome136") or "chrome136")
        except Exception as e:
            logger.warning(f"[DirectForward/IG] transport install degraded: {e}")
        try:
            ig_anti_detect.pin_geo(
                c,
                country=getattr(config, "IG_DIRECT_COUNTRY", "US") or "US",
                country_code=getattr(config, "IG_DIRECT_COUNTRY_CODE", 1) or 1,
                locale=getattr(config, "IG_DIRECT_LOCALE", "en_US") or "en_US",
                timezone_offset=getattr(config, "IG_DIRECT_TZ_OFFSET", -14400),
                timezone_name=getattr(config, "IG_DIRECT_TZ_NAME", "GMT-04:00") or "GMT-04:00",
            )
        except Exception as e:
            logger.warning(f"[DirectForward/IG] geo pin degraded: {e}")
        try:
            ig_anti_detect.install_token_echo(c)
        except Exception as e:
            logger.warning(f"[DirectForward/IG] token-echo install degraded: {e}")
        return c

    # The cookie jar (and hence the sessionid) can change after startup: the
    # admin uploads a fresh igcookies.txt mid-run, or a previous worker died on
    # a stale jar. NEVER exit on a login failure — retry on the poll cadence so
    # a freshly-uploaded jar is picked up without a bot restart. Each attempt
    # gets a fresh client: a half-failed instagrapi login can poison cl state.
    #
    # Exception: a real CHALLENGE (checkpoint / please-wait) must NOT be retried
    # on a machine cadence — that deepens the flag. Freeze for hours instead
    # (the durable fix is a human passing the checkpoint in the official app).
    # LoginRequired mid-poll is handled separately inside the loop.
    cl = _make_client()
    login_attempt = 0
    while True:
        try:
            await loop.run_in_executor(None, lambda: _ig_login(cl))
            cl.dump_settings(IG_SESSION_FILE)
            os.chmod(IG_SESSION_FILE, 0o600)
            # Cold-start warmup: a few paced, benign reads so the first real
            # poll isn't the session's first activity on a fresh IP.
            try:
                await loop.run_in_executor(None, lambda: ig_anti_detect.warmup(cl))
            except Exception as e:
                logger.warning(f"[DirectForward/IG] warmup skipped: {e}")
            break
        except (ChallengeRequired, PleaseWaitFewMinutes) as e:
            freeze = random.uniform(3 * 3600, 5 * 3600)
            logger.error(f"[DirectForward/IG] Instagram challenged the login: {e}. "
                         f"Pausing this worker for ~{freeze / 3600:.1f}h. "
                         f"Open the official Instagram app on the bot account and pass the "
                         f"checkpoint there, then restart the bot for a clean resume.")
            try:
                await bot_client.send_message(
                    chat_id=chat_id,
                    text=(f"⚠️ **Instagram checkpoint on the bot account!**\n\n"
                          f"The IG direct-forward worker hit a manual-verification "
                          f"checkpoint during login and is pausing ~{freeze / 3600:.1f}h "
                          f"to avoid making it worse.\n\n"
                          f"Open the official Instagram app on the bot account and pass the "
                          f"verification there, then restart the bot. Instagram: `{e}`"),
                )
            except Exception as alert_err:
                logger.warning(f"[DirectForward/IG] checkpoint alert to chat failed: {alert_err}")
            await asyncio.sleep(freeze)
            cl = _make_client()
        except Exception as e:
            login_attempt += 1
            if login_attempt == 1:
                logger.error(f"[DirectForward/IG] login failed: {e}. "
                             f"Retrying every ~{_poll_interval()}s — a fresh "
                             f"igcookies.txt upload will be picked up automatically.")
            else:
                logger.warning(f"[DirectForward/IG] login retry {login_attempt} failed: {e}")
            cl = _make_client()
            await asyncio.sleep(_poll_interval())

    state = _load_state()

    # Static pre-pair from .env (bootstrap for existing setups). Resolves the
    # handle to a numeric user id once and persists it as the pair.
    if not _get_pair(state, "ig") and config.IG_DIRECT_FROM_USERNAME:
        uid = await loop.run_in_executor(None, _ig_resolve_user_id, cl, config.IG_DIRECT_FROM_USERNAME)
        if uid:
            _set_pair(state, "ig", uid, config.IG_DIRECT_FROM_USERNAME)
            await _state_save_owned(state, {"ig"})
            logger.info(f"[DirectForward/IG] pre-paired with @{config.IG_DIRECT_FROM_USERNAME} (id {uid}) from .env")

    if "ig" not in state or "last_id" not in state.get("ig", {}):
        state.setdefault("ig", {"last_id": "0"})
        await _state_save_owned(state, {"ig"})
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
                await _state_save_owned(state, {"ig"})
        except Exception as e:
            logger.warning(f"[DirectForward/IG] priming peek failed: {e}")

    poll = max(60, config.DIRECT_FORWARD_POLL_SECONDS)
    pair = _get_pair(state, "ig")
    if pair:
        logger.info(f"[DirectForward/IG] polling DMs from @{pair['username']} (id {pair['user_id']}) "
                    f"every ~{poll}s (jittered ±{config.DIRECT_FORWARD_POLL_JITTER_PCT}%)")
    else:
        logger.info("[DirectForward/IG] no pair yet — waiting for pairing handshake "
                    "(Admin Console → Direct-Forward → Pair Instagram) or a .env pre-pair.")

    while True:
        state = _load_state()  # fresh each poll: admin pairing/cursor changes land within one interval
        state_dirty = False
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

                # Activity watermark: the inbox listing already carries each
                # thread's last_activity_at, so an unchanged thread costs ZERO
                # private-API calls. This is the biggest request-volume cut for
                # an idle poller (20 thread fetches/cycle → ~0), and API volume
                # is what Instagram's automation model watches.
                activity = state.setdefault("ig", {}).setdefault("thread_activity", {})
                act_key = str(th.id)
                act_now = _activity_stamp(th)
                if act_now and activity.get(act_key) == act_now and not pairing_active:
                    continue
                activity[act_key] = act_now
                state_dirty = True

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
                    await _state_save_owned(state, {"ig"})
            if state_dirty:
                await _state_save_owned(state, {"ig"})
            cl.dump_settings(IG_SESSION_FILE)
        except (ChallengeRequired, PleaseWaitFewMinutes) as e:
            # Do NOT hammer a challenged session: re-trying hourly only deepens
            # the flag. Freeze for hours; the durable fix is a human passing
            # the checkpoint in the official app, then restarting the bot.
            freeze = random.uniform(3 * 3600, 5 * 3600)
            logger.error(f"[DirectForward/IG] Instagram challenged the session: {e}. "
                         f"Pausing this worker for ~{freeze / 3600:.1f}h. "
                         f"Open the official Instagram app on the bot account and pass the "
                         f"checkpoint there, then restart the bot for a clean resume.")
            try:
                await bot_client.send_message(
                    chat_id=chat_id,
                    text=(f"⚠️ **Instagram checkpoint on the bot account!**\n\n"
                          f"The IG direct-forward worker hit a manual-verification "
                          f"checkpoint while polling and is pausing ~{freeze / 3600:.1f}h "
                          f"to avoid making it worse.\n\n"
                          f"Open the official Instagram app on the bot account and pass the "
                          f"verification there, then restart the bot. Instagram: `{e}`"),
                )
            except Exception as alert_err:
                logger.warning(f"[DirectForward/IG] checkpoint alert to chat failed: {alert_err}")
            await asyncio.sleep(freeze)
        except LoginRequired:
            logger.warning(f"[DirectForward/IG] session expired — attempting re-login.")
            try:
                await loop.run_in_executor(None, lambda: _ig_login(cl))
                cl.dump_settings(IG_SESSION_FILE)
                try:
                    await loop.run_in_executor(None, lambda: ig_anti_detect.warmup(cl))
                except Exception as e:
                    logger.warning(f"[DirectForward/IG] warmup skipped: {e}")
            except Exception as e:
                logger.error(f"[DirectForward/IG] re-login failed: {e}. Sleeping 1h.")
                await asyncio.sleep(3600)
        except Exception as e:
            logger.error(f"[DirectForward/IG] poll error: {e}")
            await asyncio.sleep(min(600, _poll_interval()))

        await asyncio.sleep(_poll_interval())


# =========================================================================
# X / Twitter DM worker (twikit) — SELF-DM method
# =========================================================================
#
# No separate bot account, no pairing handshake. The user sends tweet links /
# photos / videos to their OWN X self-DM ("Message Yourself"), and the worker
# polls that one conversation (<self_id>-<self_id>). It authenticates with the
# SAME cookies yt-dlp already uses (cookies/twitter/xcookies.txt), which
# cookie write-back keeps fresh — there is no twikit-specific session file.

def _x_jar_cookies() -> dict:
    """Read the shared X cookie jar into a twikit-style {name: value} dict.

    This is the SAME jar yt-dlp downloads with, so the twikit session rides the
    exact session yt-dlp keeps warm via write-back. Reading the locked 0o444
    jar directly is safe — nothing here merges cookies back (yt-dlp owns that),
    and the jar is swapped atomically so a read always sees a complete file."""
    jar = config.X_COOKIES
    if not jar or not os.path.exists(jar):
        return {}
    out = {}
    try:
        with open(jar, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.rstrip("\n")
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                domain, _i, _p, _s, _e, name, value = parts[:7]
                if "x.com" in domain or "twitter.com" in domain:
                    out[name] = value
    except Exception:
        return {}
    return out


def _x_twid_user_id(cookies: dict) -> str | None:
    """Extract the account's numeric user id from the `twid` cookie (value is
    `u%3D<uid>` or `u=<uid>`)."""
    raw = (cookies.get("twid") or "").strip()
    if not raw:
        return None
    if "%3D" in raw:
        uid = raw.split("%3D", 1)[1]
    else:
        uid = raw.replace("u=", "")
    uid = uid.strip()
    return uid if uid.isdigit() else None


def test_x_connection() -> str:
    """Validate the xcookies jar for X direct-forward. Returns a human-readable
    status string suitable for the admin console."""
    cookies = _x_jar_cookies()
    if not cookies:
        return ("❌ **X Connection Test**\n\n"
                "No xcookies jar found or it is empty.\n"
                "Upload one via Admin → Cookie Jars → X/Twitter → ✏️ Replace.")
    missing = [k for k in ("auth_token", "twid") if k not in cookies]
    if missing:
        return (f"❌ **X Connection Test**\n\n"
                f"xcookies jar is missing required cookie(s): "
                f"`{'`, `'.join(missing)}`.\n"
                f"Re-export a fresh jar from your logged-in X session.")
    uid = _x_twid_user_id(cookies)
    if not uid:
        return ("❌ **X Connection Test**\n\n"
                "Could not extract user ID from the `twid` cookie. "
                "The jar may be corrupted.")
    return (f"✅ **X Connection Test Passed**\n\n"
            f"• User ID: `{uid}`\n"
            f"• Self-DM conversation: `{uid}-{uid}`\n"
            f"• Required cookies present: `auth_token`, `twid`")


def _x_is_tweet_url(url: str) -> bool:
    return bool(re.search(r"(?:x\.com|twitter\.com)/(?:[^/?#]+/)?status(?:es)?/\d+", url, re.I))


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
    """Find a shared tweet inside a DM attachment tree → (status URL, text).

    Handles the two shapes X actually emits:
    - GraphQL-style: ``{"rest_id": ..., "legacy": {"full_text": ...}}``
    - legacy share cards: ``tweet.status.{id_str, text}``, or the card's own
      ``expanded_url`` when no status object is embedded.
    ``text`` is best-effort (the URL is what matters for delivery)."""
    if isinstance(node, dict):
        legacy = node.get("legacy") if isinstance(node.get("legacy"), dict) else None
        if "rest_id" in node and legacy is not None:
            return f"https://x.com/i/status/{node['rest_id']}", legacy.get("full_text", "")
        if "id_str" in node and isinstance(node.get("full_text"), str):
            return f"https://x.com/i/status/{node['id_str']}", node.get("full_text", "")
        if "id_str" in node and isinstance(node.get("text"), str):
            return f"https://x.com/i/status/{node['id_str']}", node.get("text", "")
        for v in node.values():
            found, txt = _x_deep_find_tweet(v)
            if found:
                return found, txt
        expanded = node.get("expanded_url")
        if isinstance(expanded, str) and _x_is_tweet_url(expanded):
            return expanded, node.get("text") or ""
    elif isinstance(node, list):
        for v in node:
            found, txt = _x_deep_find_tweet(v)
            if found:
                return found, txt
    return None, ""


def _x_tweet_share_author(attachment) -> tuple[str | None, str | None]:
    """Extract the ORIGINAL post author (username, numeric id) from a tweet
    share's payload wherever the user object sits."""
    if not isinstance(attachment, dict):
        return None, None
    t = attachment.get("tweet")
    if isinstance(t, dict):
        status = t.get("status")
        if isinstance(status, dict):
            user = status.get("user")
            if isinstance(user, dict):
                screen = user.get("screen_name") or user.get("username")
                uid = user.get("id_str") or user.get("rest_id")
                return screen, (str(uid) if uid else None)
    return None, None


def _x_share_media(attachment) -> list[dict]:
    """Extract media items from a tweet share payload as
    [{"type": "photo"|"video", "url": ...}]. Photos use the CDN media_url_https;
    videos use the highest-bitrate mp4 variant."""
    if not isinstance(attachment, dict):
        return []
    t = attachment.get("tweet")
    if not isinstance(t, dict):
        return []
    status = t.get("status")
    if not isinstance(status, dict):
        return []
    out = []
    media = status.get("media") or (status.get("extended_entities") or {}).get("media") or []
    for m in media:
        if not isinstance(m, dict):
            continue
        mtype = m.get("type")
        if mtype == "photo":
            u = m.get("media_url_https") or m.get("media_url")
            if u:
                out.append({"type": "photo", "url": u})
        elif mtype in ("video", "animated_gif"):
            variants = (m.get("video_info") or {}).get("variants") or []
            mp4s = [v for v in variants if isinstance(v, dict)
                    and str(v.get("content_type", "")).startswith("video") and v.get("url")]
            mp4s.sort(key=lambda v: int(v.get("bitrate", 0) or 0), reverse=True)
            if mp4s:
                out.append({"type": "video", "url": mp4s[0]["url"]})
    return out


async def _x_fetch_auth_bytes(client, url: str) -> bytes:
    """Fetch *url* through the authenticated twikit session. Uses the session's
    base headers (Bearer auth + browser UA) and its cookies — exactly how the
    web client reads protected media (ton.twitter.com DM photos 401 without
    them). httpx is async, so this runs on the event loop.

    The fetch runs on a THROWAWAY httpx client (same headers + a copy of the
    session cookie jar) that is closed afterwards. Never reuse ``client.http``
    for these: ton.twitter.com is Cloudflare-fronted and its ``Set-Cookie``
    (__cf_bm & co.) would pile duplicate names into the shared session jar,
    and the next ``dm_conversation`` poll dies with
    ``httpx.CookieConflict: Multiple cookies exist with name=__cf_bm``."""
    import httpx
    headers = dict(client.v11.base._base_headers)
    cookies = {c.name: c.value for c in client.http.cookies.jar}
    async with httpx.AsyncClient(headers=headers, cookies=cookies,
                                 follow_redirects=True, timeout=120) as ac:
        resp = await ac.get(url)
        resp.raise_for_status()
        return resp.content


async def _x_fallback_photos(client, url: str) -> list[str]:
    """For a PASTED (text-only) tweet URL, yt-dlp exposes no media when the
    tweet is photo-only. Fetch the tweet through twikit and return its photo
    CDN URLs so it can be delivered natively."""
    m = re.search(r"status(?:es)?/(\d+)", url)
    if not m:
        return []
    try:
        t = await client.get_tweet_by_id(m.group(1))
    except Exception as e:
        logger.warning(f"[DirectForward/X] tweet {url} photo fallback fetch failed: {e}")
        return []
    out = []
    for med in (getattr(t, "media", None) or []):
        if str(getattr(med, "type", "")).lower() != "photo":
            continue
        u = getattr(med, "media_url", None) or getattr(med, "url", None)
        if u and str(u).startswith("http"):
            out.append(str(u))
    return out


async def _x_process_message(client, m: dict, queue, chat_id, bot_client, premium_client, self_uid: str) -> None:
    """Process one raw self-DM message (dict from _x_fetch_self_messages)."""
    self_label = f"x-user `{self_uid}`"
    msg_id = m.get("id", "?")

    # 1) Tweet shared via DM → route through the yt-dlp pipeline, auto-picking
    #    the highest quality; the format keyboard is posted when the top format
    #    exceeds the upload ceiling; photo-only tweets deliver natively.
    tweet_url, tweet_text = _x_deep_find_tweet(m.get("attachment"))
    if tweet_url:
        author, author_id = _x_tweet_share_author(m.get("attachment"))
        media = _x_share_media(m.get("attachment"))
        header = _header_lines("X", self_label, author, author_id, tweet_url)
        photos = [mm["url"] for mm in media if mm["type"] == "photo"]
        has_video = any(mm["type"] == "video" for mm in media)
        if photos and not has_video:
            logger.info(f"[DirectForward/X] msg {msg_id}: photo-only tweet share -> {tweet_url}")
            _enqueue_relay(queue, chat_id,
                           lambda ph=photos, h=header, b=tweet_text: _x_deliver_share_photos(
                               client, bot_client, chat_id, ph, h, b))
        else:
            logger.info(f"[DirectForward/X] msg {msg_id}: tweet share -> {tweet_url} (by @{author})")
            share_video = next((mm["url"] for mm in media if mm["type"] == "video"), None)
            _enqueue_relay(queue, chat_id,
                           lambda u=tweet_url, h=header, b=tweet_text, sv=share_video:
                               _x_deliver_tweet(client, bot_client, premium_client, chat_id, u, h, b, sv))
        return

    # 2) Photo / video DM attachment → authenticated fetch via the twikit session.
    media_url, is_photo = _x_deep_find_media_url(m.get("attachment"))
    if media_url:
        header = _header_lines("X", self_label, None, None, None)
        _enqueue_relay(queue, chat_id,
                       lambda u=media_url, p=is_photo, h=header: _x_deliver_dm_attachment(
                           client, bot_client, chat_id, u, p, h))
        return

    # 3) Plain text with links. Tweet URLs use the highest-quality pipeline;
    #    other links go through the generic yt-dlp relay.
    text = m.get("text", "") or ""
    urls = URL_RE.findall(text)
    for u in urls:
        header = _header_lines("X", self_label, None, None, u)
        if _x_is_tweet_url(u):
            _enqueue_relay(queue, chat_id,
                           lambda u=u, h=header: _x_deliver_tweet(
                               client, bot_client, premium_client, chat_id, u, h, "", None))
        else:
            _enqueue_relay(queue, chat_id,
                           lambda u=u, h=header: _download_and_deliver(
                               bot_client, premium_client, chat_id, u, h, ""))
    if not urls:
        logger.info(f"[DirectForward/X] msg {msg_id}: no relayable media — skipped")


async def _x_process_bridge_line(line: dict, client, queue, chat_id, bot_client, premium_client, self_uid: str) -> None:
    """Process one canonical line from the XChat bridge's inbox file.

    Schema (see xchat_bridge.mjs):
      {"id": seq, "at": ms, "kind": "tweet", "url": "...", "text": ""}
      {"id": seq, "at": ms, "kind": "media", "media_url": "...", "is_photo": true, "text": ""}
      {"id": seq, "at": ms, "kind": "text", "text": "..."}
    The XChat sequence id IS the legacy DM id (same id space), so the cursor in
    direct_forward_state.json applies unchanged — nothing double-relays."""
    self_label = f"x-user `{self_uid}`"
    msg_id = line.get("id", "?")
    kind = line.get("kind")

    if kind == "tweet":
        url = line.get("url", "")
        if not url:
            return
        header = _header_lines("X", self_label, None, None, url)
        _enqueue_relay(queue, chat_id,
                       lambda u=url, h=header, b=line.get("text", ""): _x_deliver_tweet(
                           client, bot_client, premium_client, chat_id, u, h, b, None))
        return

    if kind == "media":
        media_url = line.get("media_url", "")
        if media_url:
            header = _header_lines("X", self_label, None, None, None)
            _enqueue_relay(queue, chat_id,
                           lambda u=media_url, p=bool(line.get("is_photo")), h=header:
                               _x_deliver_dm_attachment(client, bot_client, chat_id, u, p, h))
        else:
            # Encrypted DM media — the URL requires a media key the bridge does
            # not extract (yet). Log once and skip; never drop silently forever.
            logger.info(f"[DirectForward/X] msg {msg_id}: encrypted DM media without a URL — skipped")
        return

    if kind == "text":
        text = line.get("text", "") or ""
        urls = URL_RE.findall(text)
        for u in urls:
            header = _header_lines("X", self_label, None, None, u)
            if _x_is_tweet_url(u):
                _enqueue_relay(queue, chat_id,
                               lambda u=u, h=header: _x_deliver_tweet(
                                   client, bot_client, premium_client, chat_id, u, h, "", None))
            else:
                _enqueue_relay(queue, chat_id,
                               lambda u=u, h=header: _download_and_deliver(
                                   bot_client, premium_client, chat_id, u, h, ""))
        if not urls:
            logger.info(f"[DirectForward/X] msg {msg_id}: bridge text with no links — skipped")
        return

    logger.info(f"[DirectForward/X] msg {msg_id}: bridge line kind {kind!r} not relayable — skipped")


def _x_read_inbox(cursor: int) -> list[dict]:
    """Parse cache/xchat_inbox.jsonl (created by the Deno XChat bridge). Returns
    lines whose id is strictly above *cursor*, ascending by id. Missing file →
    []. Corrupt lines are skipped — a partially written line must not poison the
    whole batch."""
    inbox = getattr(config, "XCHAT_INBOX", "cache/xchat_inbox.jsonl")
    try:
        with open(inbox, "r", encoding="utf-8") as f:
            raw = f.readlines()
    except OSError:
        return []
    out = []
    for rl in raw:
        rl = rl.strip()
        if not rl:
            continue
        try:
            line = json.loads(rl)
        except (ValueError, TypeError):
            continue
        try:
            lid = int(line.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if lid > cursor:
            line["_id"] = lid
            out.append(line)
    out.sort(key=lambda x: x["_id"])
    return out


async def _x_fetch_self_messages(client, conversation_id: str) -> list[dict]:
    """Fresh messages from the self-DM conversation, newest-first. Each item is
    the raw ``message_data`` dict plus an ``id`` key (the DM message id, which
    is the cursor)."""
    try:
        response, _ = await client.v11.dm_conversation(conversation_id, None)
    except Exception as e:
        logger.warning(f"[DirectForward/X] self-DM conversation {conversation_id} fetch failed: {e}")
        return []
    timeline = (response.get("conversation_timeline") or {}).get("entries") or []
    msgs = []
    for entry in timeline:
        msg = entry.get("message") or {}
        m = msg.get("message_data") or {}
        if m:
            msgs.append({"id": msg.get("id", ""), **m})
    msgs.sort(key=lambda x: str(x.get("id", "")), reverse=True)
    return msgs


async def _x_deliver_share_photos(client, bot_client, chat_id, photo_urls, header_lines, body):
    """Deliver a photo-only tweet's images natively from the share CDN (yt-dlp
    exposes no video stream for these). Fetched through the authenticated
    twikit session; group-sent when there is more than one."""
    caption, followups = _compose_caption(header_lines, body)
    files = []
    os.makedirs("cache", exist_ok=True)
    try:
        for i, url in enumerate(photo_urls):
            try:
                data = await _x_fetch_auth_bytes(client, url)
                if not _x_media_payload_ok(data, is_photo=True):
                    raise RuntimeError("empty / interstitial payload")
            except Exception as e:
                logger.warning(f"[DirectForward/X] share photo {i} failed: {e} — skipping")
                continue
            path = f"cache/df_x_photo_{int(time.time() * 1000)}_{i}.jpg"
            with open(path, "wb") as f:
                f.write(data)
            files.append(path)
        if not files:
            raise RuntimeError("no share photos could be fetched")
        if len(files) == 1:
            await bot_client.send_photo(chat_id=chat_id, photo=files[0], caption=caption)
        else:
            from pyrogram.types import InputMediaPhoto
            group = [InputMediaPhoto(p, caption=caption if j == 0 else "") for j, p in enumerate(files)]
            try:
                await bot_client.send_media_group(chat_id=chat_id, media=group)
            except Exception:
                # One corrupt item inside the group can fail the whole call —
                # send the survivors individually instead of dropping them all.
                sent_any = False
                for j, p in enumerate(files):
                    try:
                        await bot_client.send_photo(chat_id=chat_id, photo=p,
                                                    caption=caption if not sent_any else "")
                        sent_any = True
                    except Exception as e:
                        logger.warning(f"[DirectForward/X] photo {j} individual send failed: {e}")
                if not sent_any:
                    raise
        await _send_followups(bot_client, chat_id, followups)
        logger.info(f"[DirectForward/X] ✅ relayed {len(files)} share photo(s) -> {chat_id}")
    finally:
        for p in files:
            try:
                os.remove(p)
            except Exception:
                pass


async def _x_deliver_dm_attachment(client, bot_client, chat_id, media_url, is_photo, header_lines):
    """Fetch a photo/video DM attachment through the authenticated twikit
    session (ton.twitter.com URLs 401 without cookies) and send it."""
    caption, followups = _compose_caption(header_lines, "")
    data = await _x_fetch_auth_bytes(client, media_url)
    if not _x_media_payload_ok(data, is_photo):
        raise RuntimeError(f"DM attachment invalid payload ({len(data)}B)")
    ext = ".jpg" if is_photo else ".mp4"
    path = f"cache/df_x_dm_{int(time.time() * 1000)}{ext}"
    os.makedirs("cache", exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    try:
        if is_photo:
            await bot_client.send_photo(chat_id=chat_id, photo=path, caption=caption)
        else:
            await bot_client.send_video(chat_id=chat_id, video=path,
                                        caption=caption, **_video_upload_kwargs(path))
        await _send_followups(bot_client, chat_id, followups)
    finally:
        for candidate in (path, f"{os.path.splitext(path)[0]}_thumb.jpg"):
            try:
                os.remove(candidate)
            except Exception:
                pass


async def _x_deliver_tweet(client, bot_client, premium_client, chat_id, url, header_lines, body, share_video_url=None):
    """Deliver a tweet link via the yt-dlp pipeline, auto-picking the HIGHEST
    available quality. When the top format exceeds the upload ceiling (2 GB bot
    / 4 GB Premium) the format-selection keyboard is posted instead. When
    yt-dlp exposes no video stream (photo-only tweets) callers deliver the
    share's images natively before ever reaching here; if yt-dlp itself fails
    while the share carried a video variant, that mp4 is delivered natively so
    the message is never dropped."""
    from utils.downloader import extract_formats, download_media, probe_video_dimensions
    from utils.uploader_handler import process_split_and_upload, _BOT_HARD, _PREMIUM_HARD
    from modules.downloader_handler import build_format_keyboard
    from utils.shared import DOWNLOAD_CACHE
    import uuid

    loop = asyncio.get_event_loop()
    cache_id = f"x_{uuid.uuid4().hex[:8]}"
    caption, followups = _compose_caption(header_lines, body)
    task_dirs = set()

    try:
        try:
            data = await loop.run_in_executor(None, extract_formats, url)
        except Exception as e:
            logger.warning(f"[DirectForward/X] tweet {url} extract failed: {e}")
            data = None

        videos = (data or {}).get("videos") or []
        if not videos:
            if share_video_url:
                logger.info(f"[DirectForward/X] tweet {url}: no yt-dlp video formats "
                            f"— falling back to the share's own mp4")
                return await _x_deliver_dm_attachment(client, bot_client, chat_id,
                                                      share_video_url, False, header_lines)
            photo_urls = await _x_fallback_photos(client, url)
            if photo_urls:
                logger.info(f"[DirectForward/X] tweet {url}: no yt-dlp video formats "
                            f"— delivering {len(photo_urls)} photo(s) natively")
                return await _x_deliver_share_photos(client, bot_client, chat_id,
                                                     photo_urls, header_lines, body)
            raise RuntimeError(f"tweet {url}: no video formats (photo-only tweet handled natively)")

        audios = (data or {}).get("audios") or []
        best_audio_id = (data or {}).get("best_audio_format_id")
        top = videos[0]

        # Upload ceiling: 4 GB via the Premium userbot when available, else the
        # bot's hard 2 GB cap. The relay always passes premium_allowed=True.
        ceiling = _PREMIUM_HARD if premium_client else _BOT_HARD

        if top.get("bytes") and top["bytes"] <= ceiling:
            task_dirs.add(f"cache/{cache_id}")
            result = await loop.run_in_executor(
                None, download_media, url, top["format_id"], "v", cache_id, None, None,
                top.get("height"), best_audio_id, bool(top.get("muxed")), top.get("bytes"),
            )
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
                    premium_allowed=True,
                )
            await _send_followups(bot_client, chat_id, followups)
            logger.info(f"[DirectForward/X] ✅ relayed tweet {url} (top {top['quality']}) -> {chat_id}")
            return

        # Over the ceiling: post the format-selection keyboard so the operator
        # can pick a smaller quality. The relay chat is the operator's chat, so
        # a posted keyboard is tappable; the dl: callback handles the download.
        cache_key = f"x_{uuid.uuid4().hex[:8]}"
        DOWNLOAD_CACHE[cache_key] = {
            "url": url,
            "title": data.get("title", "Media"),
            "videos": videos,
            "audios": audios,
            "thumbnail_url": data.get("thumbnail"),
            "custom_filename": None,
            "best_audio_format_id": best_audio_id,
            "origin_message_id": None,
        }
        keyboard = build_format_keyboard(cache_key, videos, audios, premium_allowed=True)
        header_txt = "\n".join(line for line in header_lines if line)
        await bot_client.send_message(
            chat_id=chat_id,
            text=(f"⚠️ **{data.get('title', 'This tweet')}** is over the upload ceiling "
                  f"({top['size_str']}) at its best quality. Pick a smaller format:\n\n{header_txt}"),
            reply_markup=keyboard,
        )
        await _send_followups(bot_client, chat_id, followups)
        logger.info(f"[DirectForward/X] ⚠️ tweet {url} over ceiling — posted format keyboard")
    finally:
        for task_dir in task_dirs:
            if os.path.exists(task_dir):
                try:
                    import shutil
                    shutil.rmtree(task_dir)
                except Exception:
                    pass


async def _twitter_worker(bot_client, premium_client, chat_id: int, queue) -> None:
    try:
        from twikit import Client as XClient
    except ImportError:
        logger.error("[DirectForward/X] twikit is not installed "
                     "(pip install twikit) — X direct-forward disabled.")
        return

    # Boot from the shared xcookies jar — the same session yt-dlp uses and
    # keeps warm via write-back. No username/password, no separate bot account.
    cookies = _x_jar_cookies()
    uid = _x_twid_user_id(cookies)
    if not uid or not cookies.get("auth_token"):
        logger.error("[DirectForward/X] no usable session in the xcookies jar "
                     "(need auth_token + twid cookies). Upload a fresh jar via "
                     "Admin → Cookies — X direct-forward disabled.")
        return
    conv_id = f"{uid}-{uid}"

    x_proxy = getattr(config, "DIRECT_FORWARD_PROXY", None)
    try:
        client = XClient(language="en-US", proxy=x_proxy) if x_proxy else XClient(language="en-US")
    except TypeError:  # older twikit without a proxy kwarg
        logger.warning("[DirectForward/X] twikit has no proxy support — DIRECT_FORWARD_PROXY ignored for X")
        client = XClient(language="en-US")
    try:
        client.set_cookies(cookies)
    except Exception as e:
        logger.error(f"[DirectForward/X] could not apply xcookies: {e} — X direct-forward disabled.")
        return

    state = _load_state()
    if "x" not in state or "last_id" not in state.get("x", {}):
        state.setdefault("x", {"last_id": "0"})
        await _state_save_owned(state, {"x"})
        logger.info("[DirectForward/X] first run — priming cursor, backlog is skipped.")
        try:
            msgs = await _x_fetch_self_messages(client, conv_id)
            if msgs:
                _bump_cursor(state, "x", int(msgs[0]["id"]))
                await _state_save_owned(state, {"x"})
        except Exception as e:
            logger.warning(f"[DirectForward/X] priming peek failed: {e}")

    poll = max(60, config.DIRECT_FORWARD_POLL_SECONDS)
    logger.info(f"[DirectForward/X] polling your X self-DM "
                f"(conversation `{conv_id}`, your account id `{uid}`) "
                f"every ~{poll}s (jittered ±{config.DIRECT_FORWARD_POLL_JITTER_PCT}%)")

    while True:
        try:
            state = _load_state()
            last_seen = _cursor(state, "x")

            # Primary source: the XChat bridge's inbox file. The Deno sidecar
            # reads the XChat-encrypted self-DM that twikit's legacy DM API
            # cannot; its sequence ids live in the same id space as the legacy
            # DM ids, so the shared cursor dedupes. Fall back to the twikit
            # poll only when the bridge has never produced a file.
            bridge_lines = _x_read_inbox(last_seen)
            if bridge_lines:
                for line in bridge_lines:
                    try:
                        await _x_process_bridge_line(line, client, queue, chat_id,
                                                     bot_client, premium_client, uid)
                    except Exception as e:
                        logger.error(f"[DirectForward/X] bridge message {line.get('id')} failed: {e}")
                    _bump_cursor(state, "x", line["_id"])
                await _state_save_owned(state, {"x"})
                await asyncio.sleep(_poll_interval())
                continue

            msgs = await _x_fetch_self_messages(client, conv_id)
            new_msgs = sorted(
                (m for m in msgs if int(m.get("id") or 0) > last_seen),
                key=lambda m: int(m["id"]),
            )
            for m in new_msgs:
                try:
                    await _x_process_message(client, m, queue, chat_id, bot_client, premium_client, uid)
                except Exception as e:
                    logger.error(f"[DirectForward/X] message {m['id']} failed: {e}")
                _bump_cursor(state, "x", int(m["id"]))
                await _state_save_owned(state, {"x"})  # merge-only: never clobber IG/TikTok cursors
        except Exception as e:
            logger.error(f"[DirectForward/X] poll error: {e}")
            await asyncio.sleep(min(600, _poll_interval()))

        await asyncio.sleep(_poll_interval())


# =========================================================================
# TikTok self-DM worker (web IM WebSocket push channel)
# =========================================================================
# TikTok's "Message Yourself" chat streams over a persistent WebSocket
# (wss://im-ws-sg.tiktok.com/ws/v2) as cmd-500 NEW_MSG_NOTIFY protobuf pushes.
# The web IM SDK signs the connection with an `access_key` derived from the
# account's `wid` (md5 of a salted app-key+wid string); the session is the same
# web login as the ttcookies jar yt-dlp downloads with. Every pushed video share
# is resolved to @author/video/<itemId> via oEmbed and relayed through the
# normal yt-dlp pipeline (fresh-cookie retry — see docs/tiktok-download-fix.md).
# There is NO pairing and NO separate bot account, exactly like the X self-DM.
#
# Reconnect semantics: the server marks a message delivered once pushed, so a
# reconnect does NOT re-push old items — it only picks up anything that arrived
# while the socket was down. The worker therefore holds the socket open and
# reconnects (jittered) on drop. On the very first run the initial unread
# backlog is primed-and-skipped so enabling the relay never floods the chat.
# -------------------------------------------------------------------------

_TT_APP_KEY = "e1bd35ec9db7b8d846de66ed140b1ad9"
_TT_WS_HOST = "wss://im-ws-sg.tiktok.com/ws/v2"
_TT_WS_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/133.0.0.0 Safari/537.36")
_TT_PRIME_WINDOW = 15  # seconds of the first connect to treat as backlog


def _pb_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _pb_ld(field: int, data: bytes) -> bytes:
    return _pb_varint((field << 3) | 2) + _pb_varint(len(data)) + data


def _pb_uint(field: int, v: int) -> bytes:
    return _pb_varint((field << 3) | 0) + _pb_varint(v)


def _pb_str(field: int, s: str) -> bytes:
    return _pb_ld(field, s.encode())


def _pb_ext(key: str, value: str) -> bytes:
    return _pb_str(1, key) + _pb_str(2, value)


def _pb_read_varint(buf: bytes, i: int, end: int):
    v = 0
    shift = 0
    while i < end:
        b = buf[i]
        i += 1
        v |= (b & 0x7F) << shift
        if not b & 0x80:
            return v, i
        shift += 7
        if shift >= 64:
            return None, i
    return None, i


def _tt_walk(buf: bytes, want: set[int]) -> dict[int, list]:
    """Tolerant protobuf field walker returning {field: [values]} for the
    wanted fields. Skips unknown wire types and group (start/end) blocks so the
    MessageBody's extra wire types never trip us — we only ever read the fields
    we need and never descend into the JSON string at field 8."""
    out: dict[int, list] = {}
    end = len(buf)
    i = 0
    while i < end:
        key, i = _pb_read_varint(buf, i, end)
        if key is None:
            break
        field = key >> 3
        wire = key & 7
        if wire == 0:
            v, i = _pb_read_varint(buf, i, end)
            if field in want:
                out.setdefault(field, []).append(v)
        elif wire == 1:
            i += 8
        elif wire == 2:
            ln, i = _pb_read_varint(buf, i, end)
            if ln is None or i + ln > end:
                break
            if field in want:
                out.setdefault(field, []).append(buf[i:i + ln])
            i += ln
        elif wire == 3:
            depth = 1
            while i < end and depth:
                k2, i = _pb_read_varint(buf, i, end)
                if k2 is None:
                    break
                w2 = k2 & 7
                if w2 == 3:
                    depth += 1
                elif w2 == 4:
                    depth -= 1
                elif w2 == 0:
                    _, i = _pb_read_varint(buf, i, end)
                elif w2 == 1:
                    i += 8
                elif w2 == 2:
                    ln2, i = _pb_read_varint(buf, i, end)
                    if ln2 is None:
                        break
                    i += ln2
                elif w2 == 5:
                    i += 4
        elif wire == 4:
            break
        elif wire == 5:
            i += 4
        else:
            break
    return out


def _tt_jar_cookies() -> dict[str, str]:
    """Parse the TikTok Netscape jar into {name: value} (config.TT_COOKIES)."""
    jar = getattr(config, "TT_COOKIES", "cookies/tiktok/ttcookies.txt")
    cookies: dict[str, str] = {}
    if not os.path.exists(jar):
        return cookies
    try:
        with open(jar, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                if raw.startswith("#") or not raw.strip():
                    continue
                parts = raw.rstrip("\n").split("\t")
                if len(parts) >= 7 and parts[5] and parts[6]:
                    cookies[parts[5]] = parts[6]
    except Exception:
        pass
    return cookies


def _tt_jar_cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def _tt_wid(cookies: dict[str, str]) -> str | None:
    """GET the web-cookie-privacy config endpoint; its `wid` seeds access_key."""
    import requests
    headers = {
        "accept": "application/json, text/plain, */*",
        "referer": "https://www.tiktok.com/messages",
        "user-agent": _TT_WS_BROWSER_UA,
        "cookie": _tt_jar_cookie_header(cookies),
    }
    params = {"locale": "en", "appId": "1988", "theme": "default", "tea": "1"}
    proxies = ({"http": config.REQUESTS_PROXY, "https": config.REQUESTS_PROXY}
               if config.REQUESTS_PROXY else None)
    try:
        r = requests.get("https://www.tiktok.com/api/v1/web-cookie-privacy/config",
                         params=params, headers=headers, proxies=proxies, timeout=30)
        return r.json()["body"]["consent"]["wid"]
    except Exception as e:
        logger.warning(f"[DirectForward/TT] wid lookup failed: {e}")
        return None


def _tt_access_key(wid: str) -> str:
    import hashlib
    secret = f"9{_TT_APP_KEY}{wid}f8a69f1719916z"
    return hashlib.md5(secret.encode()).hexdigest()


def _tt_ws_url(cookies: dict[str, str], access_key: str) -> str | None:
    from urllib.parse import unquote, urlencode
    # The jar stores ttwid URL-encoded (e.g. `1%7C...`); the IM SDK passes the
    # DECODED value and lets urlencode re-encode it. Unquote here or the param
    # gets double-encoded and the server rejects the socket with HTTP 400.
    ttwid = unquote(cookies.get("ttwid", ""))
    ms_token = cookies.get("msToken", "")
    if not (ttwid and ms_token):
        logger.warning("[DirectForward/TT] jar lacks ttwid / msToken — WS can't authenticate.")
        return None
    qs = urlencode({
        "aid": 1459,
        "fpid": 9,
        "access_key": access_key,
        "device_platform": "web",
        "ttwid": ttwid,
        "Web-Sdk-Ms-Token": ms_token,
    })
    return f"{_TT_WS_HOST}?{qs}"


def _tt_connect_frame(cookies: dict[str, str], ms_token: str) -> bytes:
    """Build the cmd-1001 (get_stranger_conversation_list) Frame sent once on
    connect — it acks the socket and triggers the server to push any pending
    unread messages as cmd 500. Mirrors the web IM SDK / cv-cat's client."""
    seq_id = random.randint(10100, 10300)
    common_headers = [
        ("aid", "1988"),
        ("app_name", "tiktok_web"),
        ("channel", "web"),
        ("device_platform", "web_pc"),
        ("region", "US"),
        ("priority_region", "US"),
        ("os", "windows"),
        ("referer", "https://www.tiktok.com/messages"),
        ("browser_language", "en"),
        ("browser_online", "true"),
        ("user_is_login", "true"),
        ("from_appID", "1988"),
        ("Web-Sdk-Ms-Token", ms_token),
        ("user_agent", _TT_WS_BROWSER_UA),
    ]
    # GetStrangerConversationListRequestBody: cursor=0, count=1, show_total_unread
    gs = _pb_uint(1, 0) + _pb_uint(2, 1) + _pb_uint(3, 1)
    req_body = _pb_ld(1000, gs)
    req_headers = b"".join(_pb_ld(15, _pb_ext(k, v)) for k, v in common_headers)
    req = (
        _pb_uint(1, 1001) + _pb_uint(2, seq_id) + _pb_str(3, "1.2.3")
        + _pb_str(4, "") + _pb_uint(5, 3) + _pb_uint(6, 0)
        + _pb_str(7, "831c301:master") + _pb_ld(8, req_body)
        + _pb_str(9, "7460856262408259088") + _pb_str(11, "web")
        + req_headers + _pb_uint(18, 1)
    )
    frame_headers = b"".join(_pb_ld(5, _pb_ext(k, v)) for k, v in common_headers)
    return (
        _pb_uint(1, seq_id) + _pb_uint(2, int(time.time() * 1000))
        + _pb_uint(3, 5) + _pb_uint(4, 1) + frame_headers
        + _pb_str(6, "pb") + _pb_str(7, "pb") + _pb_ld(8, req)
    )


def _tt_parse_push(buf: bytes) -> dict | None:
    """Decode one WS binary Frame → Response → cmd-500 NewMessageNotify →
    MessageBody. Returns the fields we care about, or None when the frame is
    not a message push (ack / heartbeat / other cmd)."""
    fields = _tt_walk(buf, {8})
    payload = (fields.get(8) or [None])[0]
    if not payload:
        return None
    rsp = _tt_walk(payload, {1, 6})
    if rsp.get(1, [None])[0] != 500:
        return None
    body = (rsp.get(6) or [None])[0]
    if not body:
        return None
    notify = (_tt_walk(body, {500}).get(500) or [None])[0]
    if not notify:
        return None
    nf = _tt_walk(notify, {2, 5})
    conv = (nf.get(2) or [None])[0]
    mb = (nf.get(5) or [None])[0]
    if not conv or not mb:
        return None
    mf = _tt_walk(mb, {1, 3, 6, 8})
    return {
        "conversation_id": conv.decode("utf-8", "replace"),
        "server_message_id": (mf.get(3) or [0])[0],
        "message_type": (mf.get(6) or [0])[0],
        "content": ((mf.get(8) or [b""])[0]).decode("utf-8", "replace"),
    }


def _tt_oembed_author(item_id: str) -> str | None:
    """Resolve the video author's username via TikTok's public oEmbed endpoint
    (signature-free, no cookies needed)."""
    import requests
    try:
        r = requests.get("https://www.tiktok.com/oembed",
                         params={"url": f"https://www.tiktok.com/video/{item_id}"},
                         headers={"user-agent": _TT_WS_BROWSER_UA}, timeout=30)
        m = re.search(r"@([A-Za-z0-9_.-]+)", r.json().get("author_url") or "")
        return m.group(1) if m else None
    except Exception as e:
        logger.warning(f"[DirectForward/TT] oEmbed author lookup failed for {item_id}: {e}")
        return None


async def _tt_process_message(m: dict, queue, chat_id, bot_client, premium_client) -> None:
    """Relay one pushed TikTok self-DM share: canonical @author/video/<itemId>
    through the normal yt-dlp pipeline, enqueued behind interactive downloads."""
    conv = m.get("conversation_id") or ""
    parts = conv.split(":")
    if len(parts) < 4 or parts[2] != parts[3]:
        logger.info(f"[DirectForward/TT] skip non-self-DM conversation {conv}")
        return
    try:
        content = json.loads(m.get("content") or "{}")
    except Exception:
        content = {}
    item_id = str(content.get("itemId") or "")
    if not item_id:
        logger.info(f"[DirectForward/TT] msg {m.get('server_message_id')}: no itemId in share — skipped")
        return
    author = _tt_oembed_author(item_id)
    if not author:
        logger.warning(f"[DirectForward/TT] could not resolve author for itemId {item_id} — skipped")
        return
    url = f"https://www.tiktok.com/@{author}/video/{item_id}"
    header = _header_lines("TikTok", "your TikTok self-DM", author, item_id, url)
    body = str(content.get("content_name") or "")
    logger.info(f"[DirectForward/TT] share -> {url} (by @{author})")
    _enqueue_relay(queue, chat_id,
                   lambda u=url, h=header, b=body: _download_and_deliver(
                       bot_client, premium_client, chat_id, u, h, b))


async def _tt_run_ws(bot_client, premium_client, chat_id, queue, seen: set,
                     prime: bool = False) -> None:
    """One WS lifetime: connect, send the cmd-1001 frame, then relay pushed
    shares until the socket drops (the caller reconnects on a jittered delay).

    With ``prime=True`` (first ever run) the initial unread backlog is consumed
    and recorded in ``seen`` but NOT relayed, so enabling the relay never
    floods the chat; the worker then returns and the normal loop takes over."""
    cookies = _tt_jar_cookies()
    if not cookies.get("sessionid"):
        logger.error("[DirectForward/TT] no usable session in the ttcookies jar "
                     "(need a sessionid cookie). Upload a fresh jar via "
                     "Admin → Cookies — TikTok direct-forward disabled.")
        return
    wid = _tt_wid(cookies)
    if not wid:
        logger.error("[DirectForward/TT] could not fetch wid — TikTok direct-forward disabled.")
        return
    access_key = _tt_access_key(wid)
    url = _tt_ws_url(cookies, access_key)
    if not url:
        return

    import websockets
    logger.info("[DirectForward/TT] connecting IM WebSocket…")
    async with websockets.connect(
            url,
            origin="https://www.tiktok.com",
            additional_headers={"Cookie": _tt_jar_cookie_header(cookies),
                                "User-Agent": _TT_WS_BROWSER_UA},
            ping_interval=None, max_size=2 ** 24, open_timeout=30) as ws:
        await ws.send(_tt_connect_frame(cookies, cookies.get("msToken", "")))
        logger.info("[DirectForward/TT] WS connected + cmd-1001 sent — listening for pushes.")
        prime_deadline = time.time() + _TT_PRIME_WINDOW if prime else None
        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=35)
            except asyncio.TimeoutError:
                await ws.send("hi")
                if prime_deadline and time.time() > prime_deadline:
                    logger.info("[DirectForward/TT] prime window elapsed — handing off to the relay loop.")
                    return
                continue
            if isinstance(msg, str):
                continue  # server heartbeat echo
            m = _tt_parse_push(msg)
            if not m:
                continue
            msg_id = m.get("server_message_id") or 0
            if msg_id in seen:
                continue
            seen.add(msg_id)
            state = _load_state()
            state.setdefault("tiktok", {"seen_msg_ids": []})
            state["tiktok"]["seen_msg_ids"] = sorted(seen)[-2000:]
            await _state_save_owned(state, {"tiktok"})
            if prime:
                logger.info(f"[DirectForward/TT] prime: swallowed backlog msg {msg_id}")
                continue
            try:
                await _tt_process_message(m, queue, chat_id, bot_client, premium_client)
            except Exception as e:
                logger.error(f"[DirectForward/TT] relay of msg {msg_id} failed: {e}")


async def _tiktok_worker(bot_client, premium_client, chat_id: int, queue) -> None:
    state = _load_state()
    seen = set(state.get("tiktok", {}).get("seen_msg_ids", []))
    primed = bool(state.get("tiktok", {}).get("primed", False))

    if not primed:
        logger.info("[DirectForward/TT] first run — priming backlog, nothing relayed "
                    "for the first connect.")
        state.setdefault("tiktok", {})["primed"] = True
        await _state_save_owned(state, {"tiktok"})
        try:
            await _tt_run_ws(bot_client, premium_client, chat_id, queue, seen,
                             prime=True)
        except Exception as e:
            logger.warning(f"[DirectForward/TT] priming connect failed: {e}")
        return

    logger.info(f"[DirectForward/TT] listening for TikTok self-DM pushes "
                f"(reconnect jittered ±{config.TIKTOK_DIRECT_POLL_JITTER_PCT}%).")
    while True:
        try:
            await _tt_run_ws(bot_client, premium_client, chat_id, queue, seen)
        except Exception as e:
            logger.error(f"[DirectForward/TT] WS error: {e}")
        await asyncio.sleep(_poll_interval())


def test_tiktok_connection() -> str:
    """Admin-console self-test: jar presence, sessionid, wid + access_key."""
    cookies = _tt_jar_cookies()
    if not cookies:
        return ("❌ **TikTok**: no `cookies/tiktok/ttcookies.txt` jar. Upload one "
                "via Admin → Cookies → ➕ Per-Site Jar first.")
    missing = [k for k in ("sessionid", "ttwid", "msToken") if not cookies.get(k)]
    if missing:
        return f"❌ **TikTok**: jar present but missing cookies: `{', '.join(missing)}`. Re-upload."
    wid = _tt_wid(cookies)
    if not wid:
        return ("❌ **TikTok**: could not fetch `wid` from the web-cookie-privacy "
                "endpoint — the session may be stale. Re-upload the jar.")
    ak = _tt_access_key(wid)
    return (f"✅ **TikTok**: jar OK, `sessionid` present, wid `{wid}`, "
            f"access_key `{ak}`. The IM WebSocket can authenticate.")


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
    if getattr(config, "TIKTOK_DIRECT_ENABLED", False):
        workers.append(_tiktok_worker(bot_client, premium_client, chat_id, queue))

    if not workers:
        logger.info("[DirectForward] No platform enabled (IG_DIRECT_ENABLED / "
                    "X_DIRECT_ENABLED / TIKTOK_DIRECT_ENABLED) — direct-forward is off.")
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
    if not (config.IG_DIRECT_ENABLED or config.X_DIRECT_ENABLED
            or getattr(config, "TIKTOK_DIRECT_ENABLED", False)):
        logger.info("[DirectForward] No platform enabled; direct-forward disabled.")
        return None
    return asyncio.create_task(_direct_forward_supervisor(bot_client, premium_client, chat_id))

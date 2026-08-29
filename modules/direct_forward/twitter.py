"""
X / Twitter DM worker (twikit) — SELF-DM method for direct-forward.

Mirrors the original modules/direct_forward.py X worker exactly.
"""

import asyncio
import json
import logging
import os
import random
import re
import time
import uuid
from typing import Any

import config
from utils.shared import DOWNLOAD_CACHE, _should_stop, queue as shared_queue

from .state import (
    _load_state, _merge_state_save, _state_save_owned,
    _get_pair, _cursor, _bump_cursor,
)
from .common import (
    URL_RE, _poll_interval, _chunk_text, _compose_caption, _send_followups,
    _download_and_deliver, _enqueue_relay, _fetch_bytes, _video_upload_kwargs,
    _x_media_payload_ok, _header_lines,
)

logger = logging.getLogger(__name__)


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


def _x_cookies_signature(cookies: dict) -> tuple:
    """Lightweight content signature of a cookie dict, used to detect jar
    changes between polls without carrying the full jar around."""
    return tuple(sorted(cookies.items()))


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
    """For a PASTED (text-only) tweet URL, yt-dlp exposes no video when the
    tweet is photo-only. Fetch the tweet through twikit and return its photo
    CDN URLs so it can be delivered natively.

    PRIMARY PATH IS THE RAW GRAPHQL WALK, not ``get_tweet_by_id``: twikit
    2.3.3's ``User.__init__`` (user.py) reads ``legacy['entities']
    ['description']['urls']`` and ``legacy['pinned_tweet_ids_str']`` without a
    ``.get`` — any author missing those keys makes the WHOLE model parse raise
    ``KeyError``, so ``get_tweet_by_id`` can never return a usable Tweet for
    them (this is the recurring 'photo fallback fetch failed: urls' /
    'pinned_tweet_ids_str' log line). ``client.gql.tweet_detail`` returns the
    raw GraphQL JSON with NO model building, so the bug cannot fire; we walk
    the media entities ourselves. The model path is kept only as a secondary
    fallback for tweets the raw walk misses.
    """
    m = re.search(r"status(?:es)?/(\d+)", url)
    if not m:
        return []

    def _photo_from_media_dict(d):
        u = d.get("media_url_https") or d.get("media_url")
        if u and str(u).startswith("http"):
            return str(u)
        return None

    out: list[str] = []
    target_id = m.group(1)

    def _walk(node):
        if isinstance(node, dict):
            if node.get("type") == "photo" and (
                    "media_url_https" in node or "media_url" in node):
                u = _photo_from_media_dict(node)
                if u and u not in out:
                    out.append(u)
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    def _focal_subtree(node):
        """Return the subtree of the entry whose entryId is the focal tweet
        (``tweet-<id>``), mirroring twikit's ``get_tweet_by_id`` matching. The
        tweet_detail response also contains the thread's other tweets (replies,
        related/quote tweets) — a global media walk would over-collect photos
        that do NOT belong to the shared tweet."""
        if isinstance(node, dict):
            if node.get("entryId") == f"tweet-{target_id}":
                return node
            for v in node.values():
                found = _focal_subtree(v)
                if found:
                    return found
        elif isinstance(node, list):
            for v in node:
                found = _focal_subtree(v)
                if found:
                    return found
        return None

    # 1) Raw GraphQL walk — no Tweet/User models are built, so the twikit
    #    User.__init__ KeyError bug cannot abort the fetch.
    response = None
    try:
        response, _ = await client.gql.tweet_detail(target_id, None)
    except Exception as e:
        logger.warning(f"[DirectForward/X] tweet {url} gql detail failed: {e}")
    if response:
        if isinstance(response, dict) and response.get("errors"):
            logger.warning(f"[DirectForward/X] tweet {url} gql detail errors: {response['errors']}")
        subtree = _focal_subtree(response)
        _walk(subtree or response)
        if out:
            return out

    # 2) Secondary fallback: the normal twikit model path (works for most
    #    authors; only authors with missing entity keys crash it).
    t = None
    try:
        t = await client.get_tweet_by_id(m.group(1))
    except Exception as e:
        logger.warning(f"[DirectForward/X] tweet {url} model fallback fetch failed: {e}")
    if t is not None:
        for med in (getattr(t, "media", None) or []):
            if str(getattr(med, "type", "")).lower() != "photo":
                continue
            u = getattr(med, "media_url", None) or getattr(med, "url", None)
            if u and str(u).startswith("http"):
                out.append(str(u))
    # Raw _data walk (works even when the .media property is quirky or
    # get_tweet_by_id failed mid-parse but returned something).
    data = getattr(t, "_data", None)
    if not out and isinstance(data, dict):
        legacy = data.get("legacy") or {}
        for section in ("extended_entities", "entities"):
            for med in (legacy.get(section, {}) or {}).get("media", []) or []:
                u = _photo_from_media_dict(med)
                if u and u not in out:
                    out.append(u)
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
            # Neither yt-dlp nor the twikit fallback produced media. Never drop
            # the share silently and never let the queue task crash — relay a
            # text-only notice so the operator still gets the tweet.
            logger.warning(f"[DirectForward/X] tweet {url}: no video and no photo fallback "
                           f"— relaying text-only")
            note = (f"{caption}\n\n"
                    f"⚠️ *No downloadable media* — this tweet exposes no video "
                    f"stream to yt-dlp and the photo fallback failed.")
            await bot_client.send_message(chat_id=chat_id, text=note)
            for chunk in followups:
                await bot_client.send_message(chat_id=chat_id, text=chunk)
            return

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

    # Track the applied cookie set so a mid-run xcookies re-upload (Admin →
    # Cookies → X/Twitter → Replace, or write-back rotation) is picked up on
    # the NEXT poll with no bot restart. The jar stays locked 0o444 — we only
    # read it; yt-dlp owns the write-back merge.
    applied_sig = _x_cookies_signature(cookies)

    poll = max(60, config.DIRECT_FORWARD_POLL_SECONDS)
    logger.info(f"[DirectForward/X] polling your X self-DM "
                f"(conversation `{conv_id}`, your account id `{uid}`) "
                f"every ~{poll}s (jittered ±{config.DIRECT_FORWARD_POLL_JITTER_PCT}%)")

    while True:
        try:
            # Live jar reload: pick up a mid-run cookie re-upload (admin
            # replace or yt-dlp write-back rotation) on the next poll. If the
            # twid changes, the account itself changed → rebuild the client and
            # re-prime so the watched conversation tracks the new account.
            fresh = _x_jar_cookies()
            if fresh and _x_cookies_signature(fresh) != applied_sig:
                new_uid = _x_twid_user_id(fresh)
                if new_uid and new_uid != uid:
                    logger.info(f"[DirectForward/X] xcookies switched account "
                                f"({uid} -> {new_uid}) — rebuilding client")
                    uid, conv_id = new_uid, f"{new_uid}-{new_uid}"
                    client = XClient(language="en-US", proxy=x_proxy) if x_proxy else XClient(language="en-US")
                    client.set_cookies(fresh)
                    state = _load_state()
                    state.setdefault("x", {"last_id": "0"})
                    await _state_save_owned(state, {"x"})
                    try:
                        msgs = await _x_fetch_self_messages(client, conv_id)
                        if msgs:
                            _bump_cursor(state, "x", int(msgs[0]["id"]))
                            await _state_save_owned(state, {"x"})
                    except Exception as e:
                        logger.warning(f"[DirectForward/X] re-prime peek failed: {e}")
                else:
                    try:
                        client.set_cookies(fresh)
                        logger.info("[DirectForward/X] xcookies jar changed — re-applied session")
                    except Exception as e:
                        logger.warning(f"[DirectForward/X] could not re-apply new xcookies: {e}")
                cookies, applied_sig = fresh, _x_cookies_signature(fresh)

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

        if _should_stop():
            logger.info("[DirectForward/X] stop flag set — exiting worker loop")
            return
        await asyncio.sleep(_poll_interval())
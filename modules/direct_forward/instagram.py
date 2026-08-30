"""
Instagram DM worker (instagrapi) for direct-forward.

Mirrors the original modules/direct_forward.py Instagram worker exactly.
"""

import asyncio
import logging
import os
import random
import re
import time

import config
from utils import ig_anti_detect
from utils.shared import _should_stop

from .state import (
    _load_state, _state_save_owned, _get_pair,
    _set_pair, _pending_pairs, _activity_stamp, _cursor,
    _bump_cursor,
)
from .common import (
    URL_RE, IG_POST_RE, _poll_interval, _compose_caption,
    _send_followups, _download_and_deliver, _enqueue_relay, _fetch_bytes,
    _video_upload_kwargs, _header_lines,
)

logger = logging.getLogger(__name__)

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
    """Extract the media pk from any Instagram URL form.

    Uses the public web client (``cl.media_pk_from_url``) which goes through
    ``cl.public`` and hits the public web endpoint. When Instagram soft-blocks
    that endpoint (returns HTML instead of JSON, surfaced as a
    ``JSONDecodeError``), we count the failure and skip subsequent public
    calls for a cooldown window — hammering a throttled endpoint only
    deepens the block.
    """
    story = re.search(r"instagram\.com/stories/[^/]+/(\d+)", url)
    if story:
        return int(story.group(1))
    if ig_anti_detect.public_soft_block_active():
        raise RuntimeError(
            "IG public-web soft-block cooldown active; skipping "
            "media_pk_from_url for this cycle"
        )
    try:
        pk = int(cl.media_pk_from_url(url))
    except Exception as e:
        if "JSONDecode" in type(e).__name__ or "Expecting value" in str(e):
            ig_anti_detect.record_public_soft_block()
        raise
    ig_anti_detect.record_public_success()
    return pk


async def _ig_native_deliver_once(bot_client, chat_id, cl, pk: int,
                                  header_lines: list[str], body: str,
                                  url: str | None = None) -> bool:
    """Deliver one Instagram post/story natively through the logged-in
    instagrapi session — photo posts and carousels reliably break yt-dlp's
    extractor ('No video formats found'), so photos/albums download straight
    from the CDN here. Returns False (so the caller can fall back to yt-dlp)
    when the media is actually a reel (clips product type)."""
    loop = asyncio.get_event_loop()
    try:
        async with _ig_api_lock:
            media = await loop.run_in_executor(None, cl.media_info, pk)
    except Exception as e:
        # Story media objects route through instagrapi's ``user_stream_by_id_v1``,
        # which is session-gated: if Instagram rotated the sessionid mid-run (the
        # account is still flagged after the recent checkpoint), ``media_info``
        # raises LoginRequired and the story is silently lost. Re-login once from
        # the freshest jar sessionid and retry before giving up.
        if type(e).__name__ == "LoginRequired":
            logger.info(f"[DirectForward/IG] media_info for {pk} hit LoginRequired; "
                        f"re-logging once and retrying.")
            try:
                await loop.run_in_executor(None, lambda: _ig_login(cl))
                async with _ig_api_lock:
                    media = await loop.run_in_executor(None, cl.media_info, pk)
            except Exception as e2:
                logger.warning(f"[DirectForward/IG] story/pk {pk} re-login retry failed "
                               f"({type(e2).__name__}: {e2}); giving up.")
                raise
        else:
            raise

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
    if not jar or not os.path.exists(jar):
        logger.warning(f"[DirectForward/IG] cookie jar missing (config.IG_COOKIES={jar!r}); "
                       f"no sessionid available.")
        return None
    try:
        with open(jar, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                if raw.startswith("#") or not raw.strip():
                    continue
                parts = raw.rstrip("\n").split("\t")
                if len(parts) >= 7 and parts[5] == "sessionid" and parts[6]:
                    return parts[6]
        logger.warning(f"[DirectForward/IG] cookie jar {jar!r} has NO sessionid line.")
    except Exception as e:
        logger.warning(f"[DirectForward/IG] failed to read sessionid from {jar!r}: {e}")
    return None


def _ig_login(cl, log_prefix: str = "[DirectForward/IG]") -> None:
    """Authenticate the instagrapi client. Order:
    1. resume persisted session settings (cheapest, zero challenges),
    2. login by the sessionid from the shared IG cookie jar.

    There is deliberately NO username/password fallback: password login hammers
    ``accounts/login/`` and deepens Instagram's 429 rate-limit (the exact
    failure mode observed 2026-08-24..26). The operator disabled the password
    method entirely — a stale sessionid means "upload a fresh igcookies.txt and
    pass the checkpoint in the official app", never re-entering credentials.
    """
    if os.path.exists("direct_ig_session.json") and os.path.getsize("direct_ig_session.json") > 0:
        try:
            cl.load_settings("direct_ig_session.json")
            # Validate the persisted session WITHOUT calling login(): instagrapi's
            # login() demands both username+password, but a good persisted session
            # needs neither — account_info() alone proves it's alive.
            cl.account_info()  # forces a session check
            logger.info(f"{log_prefix} Resumed persisted direct session.")
            return
        except Exception as e:
            logger.info(f"{log_prefix} Persisted session unusable ({e}); trying sessionid.")

    sessionid = _ig_sessionid_from_jar()
    if not sessionid:
        raise RuntimeError(
            "No sessionid in igcookies.txt. Upload a fresh igcookies.txt "
            "(Admin → Cookies) — there is no password login fallback by design.")
    if cl.login_by_sessionid(sessionid):
        logger.info(f"{log_prefix} Logged in via sessionid from igcookies.txt.")
        # Persist the live session tokens Instagram just re-issued back into the
        # shared jar so it stays warm (instagrapi discards them).
        try:
            ig_anti_detect.write_back_session(cl, config.IG_COOKIES)
        except Exception as wb:
            logger.warning(f"{log_prefix} session write-back failed: {wb}")
        return
    raise RuntimeError(
        "login_by_sessionid failed (session expired/checkpointed). Upload a "
        "fresh igcookies.txt (Admin → Cookies) or pass the checkpoint in the "
        "official app — no password login fallback by design.")


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


async def _post_ig_alert(bot_client, chat_id: int, text: str) -> None:
    """Best-effort async alert to the operator's Telegram chat.

    Used by the IG anti-detect module (email-change handler) to surface
    in-app security nudges (Instagram forced email change) that the
    operator must resolve in the official app. Wrapped in try/except
    because the worker is a daemon — never let an alert sink crash the
    poll loop.
    """
    try:
        await bot_client.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        logger.warning(f"[IG direct-forward] failed to post IG alert to chat: {e}")


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
            logger.warning(f"[IG direct-forward] token-echo install degraded: {e}")
        # Email-change alert: when Instagram forces the operator to change
        # the account email, instagrapi's change_password_handler is
        # invoked. We do NOT attempt to bypass (a programmatic password
        # change deepens the flag); the handler freezes the worker per the
        # existing challenge policy and tells the operator what to do in
        # the official app.
        try:
            ig_anti_detect.install_email_change_alert(
                c, alert_sink=lambda msg: _post_ig_alert(bot_client, chat_id, msg))
        except Exception as e:
            logger.warning(f"[IG direct-forward] email-change handler install degraded: {e}")
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
            # Sessionid-only: there is no password login fallback (it deepened the
            # 429). A failing sessionid raises (caught below) → backoff/alert.
            await loop.run_in_executor(None, lambda: _ig_login(cl))
            cl.dump_settings("direct_ig_session.json")
            os.chmod("direct_ig_session.json", 0o600)
            # Cold-start warmup: a few paced, benign reads so the first real
            # poll isn't the session's first activity on a fresh IP.
            try:
                await loop.run_in_executor(None, lambda: ig_anti_detect.warmup(cl))
            except Exception as e:
                logger.warning(f"[DirectForward/IG] warmup skipped: {e}")
            # Cold-start jitter: extend the post-login window with a
            # longer paced "user opens app, reads inbox" sequence so
            # the first paired-thread poll isn't the very first observable
            # activity on the new session. The bot can run a few minutes
            # later than usual on cold start — the user pays that once
            # per boot in exchange for not flagging the account.
            try:
                await ig_anti_detect.cold_start_jitter(cl)
            except Exception as e:
                logger.warning(f"[DirectForward/IG] cold-start jitter skipped: {e}")
            break
        except (ChallengeRequired, PleaseWaitFewMinutes) as e:
            # For testing, shorten freeze to 60s instead of 4h; the gap recovery (200 items) will
            # still deliver the stalled batch once login succeeds. If you see repeated PleaseWait,
            # the durable fix is still to pass the challenge in the official app.
            # NOTE: gated on IG_DIRECT_CHALLENGE_FREEZE_TEST, NOT on
            # IG_DIRECT_MQTT_ENABLED — that flag only enables the experimental
            # MQTToT push listener and must not silently disable the freeze
            # that protects the account from a checkpoint retry storm.
            if getattr(config, "IG_DIRECT_CHALLENGE_FREEZE_TEST", False):
                freeze = random.uniform(60, 120)
            else:
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
            # Exponential backoff: a transient login failure (e.g. 429) must not
            # be retried on a fixed ~3h cadence forever — that cold-starts the
            # account's rate-limit every cycle and is what produced the sustained
            # 429 flood over 2026-08-24..26. Back off 2^n * base (capped at 24h).
            backoff = min(_poll_interval() * (2 ** (login_attempt - 1)), 24 * 3600)
            if login_attempt == 1:
                logger.error(f"[DirectForward/IG] login failed: {e}. "
                             f"Retrying in ~{backoff / 60:.0f}m (exponential backoff) — a fresh "
                             f"igcookies.txt upload will be picked up automatically.")
            else:
                logger.warning(f"[DirectForward/IG] login retry {login_attempt} failed: {e} "
                               f"(next retry in ~{backoff / 3600:.1f}h).")
            cl = _make_client()
            await asyncio.sleep(backoff)

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

    # --- Hybrid push (experimental, pure Python MQTToT, NO browser) ---
    # When IG_DIRECT_MQTT_ENABLED, we run a lightweight Realtime MQTT listener
    # alongside polling. Same TikTok-like instant push (~1-3s) with polling as
    # fallback + gap recovery for the stalled batch you saw.
    mqtt_task = None
    if getattr(config, "IG_DIRECT_MQTT_ENABLED", False):
        async def _ig_mqtt_listener():
            # Separate client for MQTT so polling's direct_threads doesn't race
            mqtt_cl = _make_client()
            try:
                await loop.run_in_executor(None, lambda: _ig_login(mqtt_cl, log_prefix="[DirectForward/IG-MQTT]"))
                mqtt_cl.dump_settings("direct_ig_session.json")
            except Exception as e:
                logger.warning(f"[DirectForward/IG-MQTT] MQTT login failed: {e} — will retry via polling only")
                return
            # Connect Realtime (MQTToT on edge-mqtt.facebook.com)
            try:
                # realtime_on must be set before connect to catch early events
                def _on_mqtt_message(payload):
                    # payload is already parsed by dispatch_message_sync → emitted as "message"
                    # We handle it via same path as polling but from MQTT thread.
                    # Use thread-safe scheduling: push to asyncio queue
                    try:
                        # payload wrapper: {"message": {"path": "/direct_v2/threads/...", "op": "replace", "thread_id": ..., "value": {...}}}
                        msg = payload.get("message") if isinstance(payload, dict) else None
                        if isinstance(msg, dict) and "value" in msg:
                            # MQTT value is the raw DM item dict (same shape as polling)
                            item = msg["value"] if isinstance(msg["value"], dict) else {}
                            if item.get("item_id"):
                                # Schedule processing off the MQTT thread
                                loop.call_soon_threadsafe(lambda: asyncio.create_task(_ig_mqtt_handle_item(item)))
                    except Exception as ex:
                        logger.warning(f"[DirectForward/IG-MQTT] handler error: {ex}")

                async def _ig_mqtt_handle_item(item: dict):
                    # Reuse same processing as polling, with shared state lock
                    st = _load_state()
                    pr = _get_pair(st, "ig")
                    if not pr:
                        return
                    uid = str(item.get("user_id", ""))
                    if uid != pr["user_id"]:
                        return
                    last = _cursor(st, "ig")
                    try:
                        iid = int(item.get("item_id", 0) or 0)
                        if iid <= last:
                            return  # already processed via polling or previous MQTT
                    except:
                        return
                    logger.info(f"[DirectForward/IG-MQTT] push item {item.get('item_id')} from @{pr['username']} — instant relay")
                    try:
                        await _ig_process_message(item, mqtt_cl, loop, queue, chat_id, bot_client, premium_client, pr["username"])
                        _bump_cursor(st, "ig", int(item["item_id"]))
                        await _state_save_owned(st, {"ig"})
                    except Exception as e:
                        logger.warning(f"[DirectForward/IG-MQTT] item {item.get('item_id')} failed: {e}")

                # Register handlers for both iris and direct realtime
                mqtt_cl.realtime_on("message", _on_mqtt_message)
                mqtt_cl.realtime_on("direct", _on_mqtt_message)
                # Connect and subscribe (iris_subscribe needs seq_id/snapshot_at_ms from inbox)
                rt = await loop.run_in_executor(None, mqtt_cl.realtime_connect)
                try:
                    await loop.run_in_executor(None, rt.direct_subscribe)
                    logger.info("[DirectForward/IG-MQTT] Realtime MQTT connected + direct_subscribe ok — push active")
                except Exception as e:
                    logger.warning(f"[DirectForward/IG-MQTT] direct_subscribe failed: {e} — will rely on polling")

                # Keepalive + read loop (blocking recv, so run in executor)
                while True:
                    try:
                        # ping every 60s to keep MQTToT alive
                        await asyncio.sleep(60)
                        try:
                            await loop.run_in_executor(None, mqtt_cl.realtime_ping)
                        except Exception as e:
                            logger.warning(f"[DirectForward/IG-MQTT] ping failed: {e} — reconnecting")
                            raise
                        # Also drain any pending packets (read_once is non-blocking after ping)
                        for _ in range(5):
                            try:
                                await loop.run_in_executor(None, mqtt_cl.realtime_read_once)
                            except Exception:
                                break
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.warning(f"[DirectForward/IG-MQTT] loop error: {e} — reconnect in 20s")
                        await asyncio.sleep(20)
                        # Reconnect logic: disconnect then reconnect
                        try:
                            await loop.run_in_executor(None, mqtt_cl.realtime_disconnect)
                        except:
                            pass
                        try:
                            rt2 = await loop.run_in_executor(None, mqtt_cl.realtime_connect)
                            await loop.run_in_executor(None, rt2.direct_subscribe)
                            logger.info("[DirectForward/IG-MQTT] reconnected")
                        except Exception as e2:
                            logger.warning(f"[DirectForward/IG-MQTT] reconnect failed: {e2}")
                            await asyncio.sleep(60)
            except asyncio.CancelledError:
                try:
                    await loop.run_in_executor(None, mqtt_cl.realtime_disconnect)
                except:
                    pass
                raise
            except Exception as e:
                logger.error(f"[DirectForward/IG-MQTT] fatal: {e} — falling back to polling only")
                await asyncio.sleep(60)

        mqtt_task = asyncio.create_task(_ig_mqtt_listener())
        logger.info("[DirectForward/IG-MQTT] hybrid enabled — polling + push (TikTok-like)")

    try:
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

                    # --- gap-aware fetch: paginate until we have all items after last_seen ---
                    # Previously we fetched only 25 items; a week-long outage could leave dozens
                    # of missed DMs beyond that window. Now we walk the thread backwards (older)
                    # until the gap is closed or we hit a safety cap, so after a stale-cookie
                    # recovery (like the recent risky_contactpoint) every missed item is relayed.
                    last_seen = _cursor(state, "ig")
                    all_items: list[dict] = []
                    cursor = None
                    # Safety: cap at 200 items per thread per poll (covers ~1-2 weeks of DM)
                    # and at most 8 pages (25*8=200). Next poll will catch any remainder.
                    for _page in range(8):
                        params = {"limit": 25, "direction": "older"}
                        if cursor:
                            params["cursor"] = cursor
                        raw_page = await loop.run_in_executor(
                            None, lambda tid=th.id, p=params: cl.private_request(f"direct_v2/threads/{tid}/", params=p))
                        thread_page = (raw_page or {}).get("thread", {}) or {}
                        page_items = thread_page.get("items") or []
                        if not page_items:
                            break
                        all_items.extend(page_items)
                        # If the oldest item in this page is already <= last_seen, gap is closed
                        try:
                            oldest_id = int(page_items[-1].get("item_id", 0) or 0)
                            if oldest_id and oldest_id <= last_seen:
                                break
                        except Exception:
                            pass
                        cursor = thread_page.get("oldest_cursor")
                        if not cursor:
                            break
                        if len(all_items) >= 200:
                            break

                    # Filter to only new, non-viewer items after cursor
                    new_msgs = []
                    for m in all_items:
                        if m.get("is_sent_by_viewer"):
                            continue
                        try:
                            if int(m["item_id"]) > last_seen:
                                new_msgs.append(m)
                        except Exception:
                            logger.warning(f"[DirectForward/IG] weird item id {m.get('item_id', '!')!r} skip")
                    # Process oldest first so Telegram order matches IG order
                    new_msgs.sort(key=lambda m: int(m["item_id"]))
                    if new_msgs:
                        logger.info(f"[DirectForward/IG] gap fetch: thread {th.id} had {len(all_items)} items, {len(new_msgs)} new after cursor {last_seen}")

                    pair_username = ""
                    if pair:
                        pair_username = pair.get("username", "")

                    for m in new_msgs:
                        consumed = False
                        success = False
                        try:
                            # Burst pacing: a multi-item backfill (the gap-fetch
                            # case) is exactly the pattern that triggers
                            # "we suspect automated behavior" — 33 items at a
                            # near-uniform 4-6 s cadence looks scripted. Apply
                            # a per-item sleep that scales with the burst size.
                            # For a 1-item cycle (the normal live case) this
                            # adds ~0-2 s and is barely noticeable; for a 30+
                            # item backfill it spaces the relay over several
                            # minutes so the cumulative activity looks human.
                            if len(new_msgs) > 1:
                                wait = ig_anti_detect.burst_pace(len(new_msgs))
                                await asyncio.sleep(wait)
                            if pairing_active:
                                consumed = await _ig_pairing_scan(m, thread_users, state,
                                                                  bot_client, chat_id)
                                pair = _get_pair(state, "ig")
                                pairing_active = "ig" in _pending_pairs
                            if not consumed and pair and str(m.get("user_id", "")) == pair["user_id"]:
                                await _ig_process_message(m, cl, loop, queue, chat_id,
                                                          bot_client, premium_client, pair.get("username", ""))
                                success = True
                            elif consumed:
                                success = True
                            elif not pair or str(m.get("user_id", "")) != pair["user_id"]:
                                # Non-paired user in same thread (group) — still advance cursor
                                success = True
                        except Exception as e:
                            logger.error(f"[DirectForward/IG] item {m.get('item_id', '?')} failed: {e}")
                            success = False
                        # Precise cursor: only advance on success. A failed item stays
                        # behind the cursor so the next poll retries it (at-least-once).
                        # The missed batch from the recent stale-cookie stall will thus be
                        # retried until delivered, instead of being skipped.
                        if success:
                            try:
                                _bump_cursor(state, "ig", int(m["item_id"]))
                                await _state_save_owned(state, {"ig"})
                            except Exception:
                                pass
                        else:
                            # Don't bump — leave cursor at previous value so this item is retried
                            logger.warning(f"[DirectForward/IG] cursor NOT advanced for failed item {m.get('item_id')} — will retry")

                if state_dirty:
                    await _state_save_owned(state, {"ig"})
                cl.dump_settings("direct_ig_session.json")
            except (ChallengeRequired, PleaseWaitFewMinutes) as e:
                # Do NOT hammer a challenged session: re-trying hourly only deepens
                # the flag. Freeze for hours; the durable fix is a human passing
                # the checkpoint in the official app, then restarting the bot.
                # Same test escape hatch as the login-time checkpoint branch, so the
                # two sites behave consistently.
                if getattr(config, "IG_DIRECT_CHALLENGE_FREEZE_TEST", False):
                    freeze = random.uniform(60, 120)
                else:
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
                logger.warning("[DirectForward/IG] session expired — attempting re-login.")
                try:
                    await loop.run_in_executor(None, lambda: _ig_login(cl))
                    cl.dump_settings("direct_ig_session.json")
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

            # Honor the global "Abort Operations" flag set by the admin console.
            # Cooperative cancel: we don't kill the in-flight `cl.*` HTTP call —
            # we let the current poll finish naturally, then break out of the
            # main loop. The systemd-supervised process keeps running; the
            # flag is reset by the next admin_restart or bot startup.
            if _should_stop():
                logger.info("[DirectForward/IG] stop flag set — exiting worker loop")
                return
            await asyncio.sleep(_poll_interval())
    finally:
        # Cancel the hybrid MQTT listener on ANY exit (stop flag, cancel,
        # or an escaping exception). Without this the task kept running a
        # second IG client + MQTToT socket after the worker returned.
        if mqtt_task is not None and not mqtt_task.done():
            mqtt_task.cancel()
            try:
                await mqtt_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

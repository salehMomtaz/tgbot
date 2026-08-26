"""
Instagram archiving for the Friend Media Archiver.

BEST-EFFORT ONLY. Per the operator's hard requirement ("for instagram we don't
need older contents"), this module delivers ONLY content that appears AFTER a
friend's IG was linked:
  * stories  — current live stories only (no highlights / older history),
               deduped via ``seen_ig_story_pks`` across runs;
  * posts    — new feed media gated by a ``last_ig_media_pk`` WATERMARK: the
               first run records the newest pk and delivers NOTHING; later runs
               deliver only pk > watermark. Older posts are never fetched.

Requires the shared IG cookie jar AND that the connected IG account is allowed
to view the target. Gated by FRIEND_MEDIA_IG_ENABLED.

Delivery goes through the BOT account to the safe destination (common.py) —
never the friend. The only Instagram operations are reads done with the
operator's own session; nothing is posted or messaged to anyone.

The instagrapi client is CACHED module-level with a TTL (building + logging in
a fresh client per call burned session lifetime and risked challenges); the jar
is re-read when the cache expires so admin jar replacements are picked up.
"""

import os
import io
import time
import random
import shutil
import zipfile
import tempfile
import asyncio
import logging
import config

from . import common

logger = logging.getLogger(__name__)

_IG_JAR = "cookies/instagram/igcookies.txt"
_IG_CLIENT_TTL = 1800  # seconds

_ig_client_cache = {"cl": None, "ts": 0.0}
_ig_lock = asyncio.Lock()


def _jar_mtime():
    try:
        return os.path.getmtime(_IG_JAR)
    except Exception:
        return 0.0


def _ig_sessionid_from_jar():
    try:
        with open(_IG_JAR, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                if raw.startswith("#") or not raw.strip():
                    continue
                parts = raw.rstrip("\n").split("\t")
                if len(parts) >= 7 and parts[5] == "sessionid" and parts[6]:
                    return parts[6]
    except Exception:
        pass
    return None


class IGUnavailable(Exception):
    """Raised when Instagram archiving cannot run (no jar sessionid, login
    failed, instagrapi missing). Carries an operator-actionable reason."""


async def _ig_client():
    """Return a cached authenticated instagrapi client, rebuilding it when the
    TTL lapses or the cookie jar changed on disk.

    Raises IGUnavailable with the REASON when unavailable — a silent None made
    the admin summary say "0 new IG stories" while the real problem was a dead
    cookie jar."""
    async with _ig_lock:
        now = time.time()
        cl = _ig_client_cache["cl"]
        fresh = (now - _ig_client_cache["ts"]) < _IG_CLIENT_TTL
        # A mid-run jar replacement must invalidate the cached session.
        if cl is not None and fresh and _jar_mtime() <= _ig_client_cache.get("mtime", 0):
            return cl
        try:
            from instagrapi import Client as IGClient
        except Exception:
            raise IGUnavailable("instagrapi not installed")

        def _build():
            c = IGClient()
            c.delay_range = [2, 4]
            # Anti-detection hardening (same as direct_forward): WITHOUT the
            # curl_cffi TLS-impersonating transport, the private API rides plain
            # Python requests TLS — a JA3 "this is a script" fingerprint that
            # Instagram answers with a login-wall redirect loop. That loop is the
            # "Exceeded 30 redirects" that sank every story/post/archive fetch.
            from utils import ig_anti_detect
            try:
                ig_anti_detect.install_transport(c, "chrome136")
            except Exception as e:
                logger.warning(f"[FriendMedia:ig] transport install degraded: {e}")
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
                logger.warning(f"[FriendMedia:ig] geo pin degraded: {e}")
            try:
                ig_anti_detect.install_token_echo(c)
            except Exception as e:
                logger.warning(f"[FriendMedia:ig] token-echo install degraded: {e}")

            sid = _ig_sessionid_from_jar()
            if not sid:
                raise RuntimeError(
                    "no sessionid in cookies/instagram/igcookies.txt — "
                    "re-upload a fresh jar via Admin → 🍪 Cookie Jars")
            if not c.login_by_sessionid(sid):
                raise RuntimeError("login_by_sessionid failed (session expired?) — "
                                   "re-upload igcookies.txt via Admin → 🍪 Cookie Jars")
            return c

        loop = asyncio.get_event_loop()
        try:
            cl = await loop.run_in_executor(None, _build)
        except Exception as e:
            raise IGUnavailable(str(e))
        _ig_client_cache["cl"] = cl
        _ig_client_cache["ts"] = now
        _ig_client_cache["mtime"] = _jar_mtime()
        return cl


async def _ig_client_retry():
    """Return a cached client, but on IGUnavailable invalidate the cache once and
    rebuild from the freshest jar sessionid — a mid-run session rotation (the
    "Exceeded 30 redirects" login-wall on ``user_stories``/``user_medias``) must
    not permanently sink the IG archive for a friend."""
    try:
        return await _ig_client()
    except IGUnavailable:
        # Force a one-time rebuild from the freshest jar (the cache may hold a
        # session Instagram just rotated).
        async with _ig_lock:
            _ig_client_cache["cl"] = None
            _ig_client_cache["ts"] = 0.0
            _ig_client_cache["mtime"] = 0.0
        return await _ig_client()


def _cleanup(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
        d = os.path.dirname(path) if path else None
        if d and os.path.exists(d) and not os.listdir(d):
            os.rmdir(d)
    except Exception:
        pass


async def _download_media(cl, media_obj, prefix):
    """Download one instagrapi Media into a temp dir; returns path or None."""
    d = tempfile.mkdtemp(prefix=prefix)
    try:
        loop = asyncio.get_event_loop()

        def _dl():
            if getattr(media_obj, "media_type", 0) == 2:
                return cl.video_download(media_obj, folder=d)
            return cl.photo_download(media_obj, folder=d)

        path = await loop.run_in_executor(None, _dl)
        if isinstance(path, (list, tuple)):
            path = path[0] if path else None
        if path and os.path.exists(path):
            return path
        return None
    except Exception as e:
        logger.warning(f"[FriendMedia:ig] download failed: {e}")
        _cleanup(os.path.join(d, "nonexistent"))
        return None


async def archive_instagram_stories(key, friend, bot=None):
    """Fetch + deliver CURRENT IG stories not already delivered. Returns count."""
    if not getattr(config, "FRIEND_MEDIA_IG_ENABLED", False):
        return 0
    ig_user = friend.get("ig_username")
    if not ig_user:
        return 0
    cl = await _ig_client_retry()  # raises IGUnavailable with an actionable reason
    max_stories = int(getattr(config, "FRIEND_MEDIA_MAX_STORIES", 100) or 100)
    seen = {str(x) for x in (friend.get("seen_ig_story_pks") or [])}
    delivered = 0
    try:
        loop = asyncio.get_event_loop()

        def _fetch():
            pk = cl.user_id_from_username(ig_user.lstrip("@"))
            stories = cl.user_stories(pk) or []
            return stories[:max_stories]

        try:
            stories = await loop.run_in_executor(None, _fetch)
        except Exception as first:
            # Session rotated mid-run (login-wall redirect loop) — rebuild from
            # the freshest jar sessionid ONCE and retry, instead of reporting
            # "IG stories skipped" and dropping live stories.
            logger.warning(f"[FriendMedia:ig] stories fetch failed ({first}); "
                           f"rebuilding client and retrying once for @{ig_user}.")
            cl = await _ig_client_retry()
            stories = await loop.run_in_executor(None, _fetch)

        bot_c = bot or common.bot_client()
        dest = common.resolve_destination()

        fresh = [s for s in stories if str(getattr(s, "pk", "")) not in seen]
        total = len(fresh)
        for idx, story in enumerate(fresh, start=1):
            path = None
            try:
                path = await _download_media(cl, story, "fmig_s_")
                if not path:
                    continue
                kind = "video" if str(path).lower().endswith((".mp4", ".mov", ".webm")) else "photo"
                ok = await common._safe_deliver_raw(
                    bot_c, dest, path, kind,
                    caption=f"📷 IG story {idx}/{total} · {key}"
                )
                if ok:
                    delivered += 1
                    spk = str(getattr(story, "pk", "") or "")
                    ring = list(friend.get("seen_ig_story_pks") or [])
                    ring.append(spk)
                    friend["seen_ig_story_pks"] = ring[-500:]
            except Exception as e:
                logger.warning(f"[FriendMedia:ig] story {idx} failed: {e}")
            finally:
                _cleanup(path)
    except Exception as e:
        logger.warning(f"[FriendMedia:ig] archive failed for {ig_user}: {e}")
    if delivered or friend.get("seen_ig_story_pks"):
        from . import state as fm_state
        await fm_state.update_friend(key, {"seen_ig_story_pks": friend.get("seen_ig_story_pks") or []})
    return delivered


async def archive_instagram_posts(key, friend, bot=None):
    """Deliver NEW IG feed posts since ``last_ig_media_pk``.

    FIRST RUN (no watermark): records the watermark and delivers nothing —
    per the operator's constraint there is no backfill of older IG content,
    ever. Later runs deliver posts newer than the watermark, oldest-first.
    """
    if not getattr(config, "FRIEND_MEDIA_IG_ENABLED", False):
        return 0
    ig_user = friend.get("ig_username")
    if not ig_user:
        return 0
    cl = await _ig_client_retry()  # raises IGUnavailable with an actionable reason
    max_posts = int(getattr(config, "FRIEND_MEDIA_MAX_POSTS_PER_RUN", 10) or 10)
    watermark = friend.get("last_ig_media_pk")
    delivered = 0
    try:
        loop = asyncio.get_event_loop()

        def _fetch():
            pk = cl.user_id_from_username(ig_user.lstrip("@"))
            # user_medias_v1 avoids the private api v1 endpoints where possible;
            # amount caps the fetch window.
            medias = cl.user_medias_v1(pk, amount=max(30, max_posts * 3)) or []
            return sorted(medias, key=lambda m: int(getattr(m, "pk", 0)))

        try:
            medias = await loop.run_in_executor(None, _fetch)
        except Exception as first:
            logger.warning(f"[FriendMedia:ig] posts fetch failed ({first}); "
                           f"rebuilding client and retrying once for @{ig_user}.")
            cl = await _ig_client_retry()
            medias = await loop.run_in_executor(None, _fetch)
        if not medias:
            return 0
        newest = str(getattr(medias[-1], "pk", ""))

        bot_c = bot or common.bot_client()
        dest = common.resolve_destination()

        if not watermark:
            # Watermark priming: acknowledge current content, deliver none.
            from . import state as fm_state
            await fm_state.update_friend(key, {"last_ig_media_pk": newest})
            logger.info(f"[FriendMedia:ig] watermark set to {newest} for @{ig_user} "
                        "(first run delivers nothing).")
            return 0

        new_items = [m for m in medias if int(getattr(m, "pk", 0)) > int(watermark)][-max_posts:]
        for idx, media in enumerate(new_items, start=1):
            path = None
            try:
                path = await _download_media(cl, media, "fmig_p_")
                if not path:
                    continue
                kind = "video" if str(path).lower().endswith((".mp4", ".mov", ".webm")) else "photo"
                cap = getattr(media, "caption_text", "") or ""
                cap = (cap[:180] + "…") if len(cap) > 180 else cap
                caption = f"🖼 IG post {idx}/{len(new_items)} · {key}"
                if cap:
                    caption += f"\n\n{cap}"
                ok = await common._safe_deliver_raw(bot_c, dest, path, kind, caption=caption)
                if ok:
                    delivered += 1
            except Exception as e:
                logger.warning(f"[FriendMedia:ig] post {idx} failed: {e}")
            finally:
                _cleanup(path)

        if new_items and delivered:
            from . import state as fm_state
            await fm_state.update_friend(key, {"last_ig_media_pk": newest})
    except Exception as e:
        logger.warning(f"[FriendMedia:ig] posts archive failed for {ig_user}: {e}")
    return delivered


def _fetch_bytes(url: str, referer: str = "https://www.instagram.com/"):
    """Download a URL into memory bytes (sync; run via executor)."""
    import requests as _requests
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/136.0.0.0 Safari/537.36"),
        "Referer": referer,
    }
    r = _requests.get(url, headers=headers, timeout=60)
    r.raise_for_status()
    return r.content


async def _jitter(lo=0.4, hi=1.6):
    """Random human-ish pause between Instagram steps (anti-robot pacing)."""
    await asyncio.sleep(random.uniform(lo, hi))


def _download_media_urls(cl, media, folder, prefix):
    """Download every CDN URL of a Media/Story into ``folder`` (sync). Returns paths.

    Handles carousel ``resources`` and single ``video_url``/``thumbnail_url``."""
    urls = []
    if getattr(media, "resources", None):
        for r in media.resources:
            v = getattr(r, "video_url", None) or getattr(r, "thumbnail_url", None)
            if v:
                urls.append(str(v))
    else:
        v = getattr(media, "video_url", None) or getattr(media, "thumbnail_url", None)
        if v:
            urls.append(str(v))
    out = []
    for i, u in enumerate(urls, start=1):
        try:
            data = _fetch_bytes(u)
            if len(data) < 500 or data[:6].lower() in (b"<html", b"<!doct"):
                continue
            ext = ".mp4" if getattr(media, "video_url", None) and u.endswith((".mp4", "")) else ".jpg"
            # Prefer extension from URL.
            if ".mp4" in u or "video" in str(u).lower():
                ext = ".mp4"
            else:
                ext = ".jpg"
            name = f"{prefix}{i}{ext}"
            with open(os.path.join(folder, name), "wb") as f:
                f.write(data)
            out.append(os.path.join(folder, name))
        except Exception as e:
            logger.warning(f"[FriendMedia:ig:archive] url {i} failed: {e}")
    return out


async def archive_instagram_full(key, friend, bot=None, status_cb=None):
    """Full capture of one IG friend into a .zip delivered to the destination:

    * profile picture (HD)
    * all highlight reels (each story media inside)
    * feed posts + carousels + reels

    Every fetch/download step is paced with a human-ish jitter (no zombie
    cadence) to avoid automation-flagging, per the operator's requirement.
    The result is ONE zip sent to the safe destination. Returns zip path or None.
    """
    ig_user = (friend.get("ig_username") or "").lstrip("@")
    if not ig_user:
        raise RuntimeError("no ig_username set for this friend")
    cl = await _ig_client_retry()
    loop = asyncio.get_event_loop()

    # Resolve the user + profile pic.
    def _user():
        pk = cl.user_id_from_username(ig_user)
        return cl.user_info_v1(pk)
    user = await loop.run_in_executor(None, _user)
    await _jitter()

    workdir = tempfile.mkdtemp(prefix="fmig_archive_")
    counted = 0
    try:
        zip_path = os.path.join(workdir, f"ig_{ig_user}_archive.zip")

        # 1. Profile picture.
        pp_url = getattr(user, "profile_pic_url_hd", None) or getattr(user, "profile_pic_url", None)
        if pp_url:
            try:
                data = await loop.run_in_executor(None, _fetch_bytes, pp_url)
                with open(os.path.join(workdir, "profile_pic.jpg"), "wb") as f:
                    f.write(data)
                counted += 1
            except Exception as e:
                logger.warning(f"[FriendMedia:ig:archive] profile pic failed: {e}")
        await _jitter()
        if status_cb:
            await status_cb(f"profile pic ✓")

        # 2. All posts/carousels/reels.
        def _posts():
            pk = cl.user_id_from_username(ig_user)
            return cl.user_medias_v1(pk, amount=0) or []
        medias = await loop.run_in_executor(None, _posts)
        await _jitter()
        posts = sorted(medias or [], key=lambda m: int(getattr(m, "pk", 0) or 0))
        for idx, m in enumerate(posts, start=1):
            paths = []
            try:
                paths = await loop.run_in_executor(
                    None, _download_media_urls, cl, m, workdir, f"post_{idx}_")
            except Exception as e:
                logger.warning(f"[FriendMedia:ig:archive] post {idx} failed: {e}")
            counted += len(paths)
            if status_cb and idx % 5 == 0:
                await status_cb(f"posts {idx}/{len(posts)}")
            await _jitter()

        # 3. Highlights (each story media inside).
        def _highlights():
            pk = cl.user_id_from_username(ig_user)
            return cl.user_highlights_v1(pk, amount=0) or []
        highlights = await loop.run_in_executor(None, _highlights)
        await _jitter()
        if status_cb:
            await status_cb(f"highlights: {len(highlights or [])}")
        for hi, hl in enumerate(highlights or [], start=1):
            try:
                full = await loop.run_in_executor(None, cl.highlight_info_v1, str(hl.pk))
                items = getattr(full, "items", None) or []
                for si, story in enumerate(items or [], start=1):
                    paths = await loop.run_in_executor(
                        None, _download_media_urls, cl, story, workdir, f"hl{hi}_{si}_")
                    counted += len(paths)
                    await _jitter()
            except Exception as e:
                logger.warning(f"[FriendMedia:ig:archive] highlight {hi} failed: {e}")
            if status_cb:
                await status_cb(f"highlight {hi}/{len(highlights or [])}")

        if status_cb:
            await status_cb("zipping…")

        # Zip everything (top-level files only).
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for name in sorted(os.listdir(workdir)):
                p = os.path.join(workdir, name)
                if os.path.isfile(p) and not name.endswith(".zip"):
                    z.write(p, arcname=name)

        if not os.path.exists(zip_path) or counted == 0:
            raise RuntimeError("nothing downloaded (private account or no media)")

        # Deliver the zip.
        bot_c = bot or common.bot_client()
        dest = common.resolve_destination()
        caption = f"🗂 IG archive · @{ig_user} ({counted} items)"
        ok = await common._safe_deliver_raw(bot_c, dest, zip_path, "document",
                                            caption=caption)
        if not ok:
            raise RuntimeError("delivery failed")
        return zip_path
    except Exception:
        # Best-effort cleanup of the temp dir on failure (success leaves the zip
        # for the cache cleaner to sweep).
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except Exception:
            pass
        raise

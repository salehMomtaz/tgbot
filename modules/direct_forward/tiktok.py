"""
TikTok self-DM worker (web IM WebSocket push channel) for direct-forward.

Mirrors the original modules/direct_forward.py TikTok worker exactly.
"""

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import time
from typing import Any

import config
from utils.shared import _should_stop

from .state import (
    _load_state, _merge_state_save, _state_save_owned,
    _get_pair, _cursor, _bump_cursor,
)
from .common import (
    URL_RE, _poll_interval, _tt_poll_interval, _chunk_text, _compose_caption,
    _send_followups, _download_and_deliver, _enqueue_relay, _fetch_bytes,
    _video_upload_kwargs, _x_media_payload_ok, _header_lines,
)

logger = logging.getLogger(__name__)

# TikTok IM WebSocket constants
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
    author = await asyncio.get_event_loop().run_in_executor(
        None, _tt_oembed_author, item_id)
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
    wid = await asyncio.get_event_loop().run_in_executor(None, _tt_wid, cookies)
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
        if _should_stop():
            logger.info("[DirectForward/TT] stop flag set — exiting worker loop")
            return
        try:
            await _tt_run_ws(bot_client, premium_client, chat_id, queue, seen)
        except Exception as e:
            logger.error(f"[DirectForward/TT] WS error: {e}")
        await asyncio.sleep(_tt_poll_interval())


async def test_tiktok_connection() -> str:
    """Admin-console self-test: jar presence, sessionid, wid + access_key."""
    cookies = _tt_jar_cookies()
    if not cookies:
        return ("❌ **TikTok**: no `cookies/tiktok/ttcookies.txt` jar. Upload one "
                "via Admin → Cookies → ➕ Per-Site Jar first.")
    missing = [k for k in ("sessionid", "ttwid", "msToken") if not cookies.get(k)]
    if missing:
        return f"❌ **TikTok**: jar present but missing cookies: `{', '.join(missing)}`. Re-upload."
    wid = await asyncio.get_event_loop().run_in_executor(None, _tt_wid, cookies)
    if not wid:
        return ("❌ **TikTok**: could not fetch `wid` from the web-cookie-privacy "
                "endpoint — the session may be stale. Re-upload the jar.")
    ak = _tt_access_key(wid)
    # Note: TikTok video download via yt-dlp currently has a known upstream issue
    # (yt-dlp#17403) where the challenge solver fails. The WebSocket connection works,
    # but video downloads may fail until yt-dlp releases a fix.
    return (f"✅ **TikTok**: jar OK, `sessionid` present, wid `{wid}`, "
            f"access_key `{ak}`. The IM WebSocket can authenticate.\n"
            f"⚠️ **Note**: Video downloads may fail due to a known yt-dlp issue "
            f"(github.com/yt-dlp/yt-dlp/issues/17403). This is a temporary upstream problem.")
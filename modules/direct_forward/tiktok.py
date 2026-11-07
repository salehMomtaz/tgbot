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
import string
import time

import config
from utils.shared import wait_if_stopped, mark_worker_alive

from .state import (
    _load_state, _state_save_owned,
)
from .common import (
    _tt_poll_interval, _download_and_deliver, _enqueue_relay, _await_relays, _header_lines,
)

logger = logging.getLogger(__name__)

# TikTok IM WebSocket constants
_TT_APP_KEY = "e1bd35ec9db7b8d846de66ed140b1ad9"
_TT_WS_HOST = "wss://im-ws-sg.tiktok.com/ws/v2"
_TT_WS_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/133.0.0.0 Safari/537.36")
_TT_PRIME_WINDOW = 15  # seconds of the first connect to treat as backlog

# HTTP IM API (history reads). The WS push channel only delivers a message
# ONCE — a share that arrives while the socket is down (or whose relay fails)
# is never re-pushed, so the dedicated read endpoint is the only way back.
# Reads need no signature: the web client sends a random X-Bogus and the
# server ignores it (proven 2026-11-07; see docs/TIKTOK.md).
_TT_HTTP_BASE = "https://im-api-sg.tiktok.com"
_TT_HISTORY_PAGE = 20          # rows per get_by_conversation page
_TT_HISTORY_PAGES = 100        # hard page cap per pass (2,000 rows); the walk
                               # stops as soon as it crosses the watermark
_TT_HISTORY_MAX_FAILS = 3      # give up on a share after N relay failures
_TT_RELAYED_CAP = 2000         # persisted relayed-item-id cap


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


def _tt_http_bogus() -> str:
    """Random X-Bogus placeholder — the history endpoints do not validate it."""
    alphabet = string.ascii_letters + string.digits + "-_"
    return "".join(random.choice(alphabet) for _ in range(22 + random.randint(0, 2)))


def _tt_http_meta(device_id: str, cookies: dict[str, str]) -> bytes:
    """The web client's metadata KV block (IM Request field 15)."""
    ms_token = cookies.get("msToken", "")
    verify_fp = cookies.get("s_v_web_id", "")
    ua = _TT_WS_BROWSER_UA
    pairs = [
        ("aid", "1988"), ("app_name", "tiktok_web"), ("channel", "web"),
        ("device_platform", "web_pc"), ("device_id", device_id),
        ("region", "US"), ("priority_region", "US"), ("os", "windows"),
        ("referer", "https://www.tiktok.com/messages"), ("root_referer", ""),
        ("cookie_enabled", "true"), ("screen_width", "1920"),
        ("screen_height", "1080"), ("browser_language", "en-US"),
        ("browser_platform", "Win32"), ("browser_name", "Mozilla"),
        ("browser_version", ua), ("browser_online", "true"),
    ]
    if verify_fp:
        pairs.append(("verifyFp", verify_fp))
    pairs += [
        ("app_language", "en"), ("webcast_language", "en"),
        ("tz_name", "UTC"), ("is_page_visible", "true"),
        ("focus_state", "true"), ("is_fullscreen", "false"),
        ("history_len", "2"), ("user_is_login", "true"),
        ("data_collection_enabled", "true"), ("from_appID", "1988"),
        ("locale", "en"), ("user_agent", ua),
    ]
    if ms_token:
        pairs.append(("Web-Sdk-Ms-Token", ms_token))
    return b"".join(_pb_ld(15, _pb_ext(k, v)) for k, v in pairs)


def _tt_http_post(path: str, body: bytes, cookies: dict[str, str]) -> bytes:
    """POST one protobuf IM request; returns the raw response body."""
    import requests
    params = {
        "aid": "1988", "version_code": "1.0.0", "app_name": "tiktok_web",
        "device_platform": "web_pc", "msToken": cookies.get("msToken", ""),
        "X-Bogus": _tt_http_bogus(),
    }
    proxies = ({"http": config.REQUESTS_PROXY, "https": config.REQUESTS_PROXY}
               if config.REQUESTS_PROXY else None)
    r = requests.post(
        _TT_HTTP_BASE + path, data=body, params=params, timeout=30,
        proxies=proxies,
        headers={
            "Cookie": _tt_jar_cookie_header(cookies),
            "User-Agent": _TT_WS_BROWSER_UA,
            "Accept": "application/x-protobuf",
            "Content-Type": "application/x-protobuf",
            "Cache-Control": "no-cache",
            "Origin": "https://www.tiktok.com",
            "Referer": "https://www.tiktok.com/messages",
        })
    r.raise_for_status()
    return r.content


def _tt_envelope(cmd: int, sub: int, payload: bytes, wid: str,
                 cookies: dict[str, str], with_device_id: bool = False) -> bytes:
    """Build the IM Request envelope shared by inbox (203) and history (301)."""
    body = (
        _pb_uint(1, cmd) + _pb_uint(2, sub) + _pb_str(3, "1.6.0") + _pb_ld(4, b"")
        + _pb_uint(5, 3) + _pb_uint(6, 0) + _pb_str(7, "")
        + _pb_ld(8, payload)
    )
    if with_device_id:  # inbox carries it at field 9; get_by_conversation does not
        body += _pb_str(9, wid)
    body += _pb_str(11, "web") + _tt_http_meta(wid, cookies) + _pb_uint(18, 1)
    return body


def _tt_parse_inbox_self_dm(raw: bytes) -> dict | None:
    """Pick the ``0:1:{uid}:{uid}`` self-DM row out of an inbox response."""
    top = _tt_walk(raw, {6})
    pl = (top.get(6) or [b""])[0]
    if not pl:
        return None
    ul = (_tt_walk(pl, {203}).get(203) or [b""])[0]
    if not ul:
        return None
    entries = _tt_walk(ul, {1}).get(1) or []
    for entry in entries:
        f = _tt_walk(entry, {1, 5})
        conv = (f.get(1) or [b""])[0]
        if not isinstance(conv, bytes):
            continue
        conv_id = conv.decode("utf-8", "replace")
        parts = conv_id.split(":")
        if len(parts) == 4 and parts[2] == parts[3]:  # 0:1:{uid}:{uid}
            source_id = (f.get(5) or [0])[0]
            if source_id:
                return {"conv_id": conv_id, "source_id": int(source_id)}
    return None


def _tt_self_dm_conversation(cookies: dict[str, str], wid: str) -> dict | None:
    """Resolve the self-DM conversation id + source_id via the HTTP inbox."""
    for sub in (10006, 10002):
        payload = _pb_ld(203, _pb_uint(1, 0))  # sort_type=0, server default page
        try:
            raw = _tt_http_post(
                "/v2/message/get_by_user_init",
                _tt_envelope(203, sub, payload, wid, cookies, with_device_id=True),
                cookies)
        except Exception as e:
            logger.warning(f"[DirectForward/TT] inbox lookup (sub {sub}) failed: {e}")
            continue
        found = _tt_parse_inbox_self_dm(raw)
        if found:
            return found
    return None


def _tt_parse_history_page(raw: bytes) -> list[dict]:
    """Decode one get_by_conversation response, oldest-first."""
    top = _tt_walk(raw, {6})
    pl = (top.get(6) or [b""])[0]
    if not pl:
        return []
    conv = (_tt_walk(pl, {301}).get(301) or [b""])[0]
    if not conv:
        return []
    msgs = []
    for entry in (_tt_walk(conv, {1}).get(1) or []):
        f = _tt_walk(entry, {3, 4, 7, 8, 25})
        content = (f.get(8) or [b""])[0]
        msgs.append({
            "srv_id": int((f.get(3) or [0])[0]),
            "ts_us": int((f.get(4) or [0])[0]),
            "sender": int((f.get(7) or [0])[0]),
            "content": content.decode("utf-8", "replace") if isinstance(content, bytes) else "",
            "cursor_ts_us": int((f.get(25) or [0])[0]),
        })
    msgs.sort(key=lambda m: m["ts_us"])
    return msgs


def _tt_fetch_history(cookies: dict[str, str], wid: str, conv_id: str,
                      source_id: int, cursor_ts_us: int = 0,
                      count: int = _TT_HISTORY_PAGE) -> list[dict]:
    """One page of get_by_conversation, oldest-first (mirrors libtiktok)."""
    query = (
        _pb_str(1, conv_id) + _pb_uint(2, 1) + _pb_uint(3, source_id)
        + _pb_uint(4, 1) + _pb_uint(5, cursor_ts_us) + _pb_uint(6, count)
    )
    raw = _tt_http_post(
        "/v1/message/get_by_conversation",
        _tt_envelope(301, 1, _pb_ld(301, query), wid, cookies),
        cookies)
    return _tt_parse_history_page(raw)


def _tt_share_item_id(content: str | None) -> str | None:
    """Extract the shared video/photo itemId from a self-DM share body."""
    if not content:
        return None
    try:
        data = json.loads(content)
    except Exception:
        return None
    item_id = data.get("itemId")
    return str(item_id) if item_id else None


def _tt_reconcile_interval() -> float:
    """Jittered cadence for the HTTP backlog reconciliation pass."""
    base = max(60, getattr(config, "TIKTOK_DIRECT_RECONCILE_SECONDS", 900))
    jitter_pct = max(0, min(90, getattr(config, "TIKTOK_DIRECT_POLL_JITTER_PCT", 40)))
    if jitter_pct:
        base *= random.uniform(1 - jitter_pct / 100, 1 + jitter_pct / 100)
    return base


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


def _tt_resolve_author(item_id: str) -> str | None:
    """Best-effort author username for the chat header.

    TikTok's public oEmbed endpoint stopped returning ``author_url`` in
    Nov 2026, so the embed page's hydration JSON is the primary source; both
    are optional — the relay NEVER depends on the author (the embed URL is a
    valid yt-dlp input on its own)."""
    import requests
    try:
        r = requests.get("https://www.tiktok.com/oembed",
                         params={"url": f"https://www.tiktok.com/video/{item_id}"},
                         headers={"user-agent": _TT_WS_BROWSER_UA}, timeout=15)
        m = re.search(r"@([A-Za-z0-9_.-]+)", r.json().get("author_url") or "")
        if m:
            return m.group(1)
    except Exception:
        pass
    try:
        r = requests.get(f"https://www.tiktok.com/embed/{item_id}",
                         headers={"user-agent": _TT_WS_BROWSER_UA}, timeout=15)
        m = re.search(r'"uniqueId"\s*:\s*"([^"]+)"', r.text)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def _tt_record_relayed(state: dict, item_id: str) -> None:
    """Record an itemId as delivered (bounded, ordered) in the shared state."""
    tt = state.setdefault("tiktok", {})
    relayed = list(tt.get("relayed_item_ids") or [])
    if item_id not in relayed:
        relayed.append(item_id)
    tt["relayed_item_ids"] = relayed[-_TT_RELAYED_CAP:]


async def _tt_relay_share(item_id: str, content: dict, queue, chat_id,
                          bot_client, premium_client, state: dict | None = None) -> bool:
    """Relay one share by itemId. Returns True only when the relay actually ran.

    The URL is the challenge-free ``/embed/<id>`` form (see url_normalize), so
    no author resolution is needed — yt-dlp's TikTok extractor accepts it and
    ``download_media`` already skips cookies for embed URLs."""
    loop = asyncio.get_event_loop()
    url = f"https://www.tiktok.com/embed/{item_id}"
    try:
        author = await loop.run_in_executor(None, _tt_resolve_author, item_id)
    except Exception:
        author = None
    header = _header_lines("TikTok", "your TikTok self-DM", author, item_id, url)
    body = str(content.get("content_name") or "")
    logger.info(f"[DirectForward/TT] share -> {url}"
                + (f" (by @{author})" if author else ""))
    try:
        # Await the relay so the caller only records the message once it has
        # actually run — otherwise a restart mid-queue drops it forever (see
        # common._enqueue_relay).
        await _await_relays([_enqueue_relay(queue, chat_id,
                       lambda u=url, h=header, b=body: _download_and_deliver(
                           bot_client, premium_client, chat_id, u, h, b))])
    except Exception as e:
        logger.error(f"[DirectForward/TT] relay failed for itemId {item_id}: {e}")
        return False
    if state is not None:
        _tt_record_relayed(state, item_id)
    return True


async def _tt_process_message(m: dict, queue, chat_id, bot_client,
                              premium_client, state: dict) -> bool:
    """Relay one pushed TikTok self-DM share. Returns True when handled (or
    intentionally ignored) and False when the relay failed and should retry."""
    conv = m.get("conversation_id") or ""
    parts = conv.split(":")
    if len(parts) < 4 or parts[2] != parts[3]:
        logger.info(f"[DirectForward/TT] skip non-self-DM conversation {conv}")
        return True
    try:
        content = json.loads(m.get("content") or "{}")
    except Exception:
        content = {}
    item_id = str(content.get("itemId") or "")
    if not item_id:
        logger.info(f"[DirectForward/TT] msg {m.get('server_message_id')}: no itemId in share — skipped")
        return True
    tt = state.setdefault("tiktok", {})
    if item_id in set(tt.get("relayed_item_ids") or []):
        logger.info(f"[DirectForward/TT] itemId {item_id} already relayed — skipping duplicate push")
        return True
    return await _tt_relay_share(item_id, content, queue, chat_id,
                                 bot_client, premium_client, state)


async def _tt_reconcile_backlog(cookies: dict[str, str], wid: str, queue,
                                chat_id, bot_client, premium_client,
                                state: dict) -> None:
    """Replay shares the push channel cannot deliver (sent while the socket was
    down, or whose relay failed). Reads the HTTP history endpoint, relays every
    unseen itemId newer than the persisted watermark, oldest first, and caps
    retries per item so one poison share cannot wedge the cursor."""
    loop = asyncio.get_event_loop()
    tt = state.setdefault("tiktok", {})

    conv_id = tt.get("conv_id")
    source_id = int(tt.get("source_id") or 0)
    if not conv_id or not source_id:
        conv = await loop.run_in_executor(None, _tt_self_dm_conversation, cookies, wid)
        if not conv:
            logger.warning("[DirectForward/TT] backlog: could not resolve the self-DM conversation")
            return
        conv_id, source_id = conv["conv_id"], conv["source_id"]
        tt["conv_id"], tt["source_id"] = conv_id, source_id

    backfill_min = max(1, int(getattr(config, "TIKTOK_DIRECT_BACKFILL_MINUTES", 1440)))
    watermark = int(tt.get("history_watermark_us") or 0)
    if watermark <= 0:
        watermark = int(time.time() * 1_000_000) - backfill_min * 60 * 1_000_000
        tt["history_watermark_us"] = watermark

    relayed = list(tt.get("relayed_item_ids") or [])
    relayed_set = set(relayed)
    fails = dict(tt.get("relay_fail_counts") or {})

    msgs: list[dict] = []
    cursor = 0
    truncated = True
    for _ in range(_TT_HISTORY_PAGES):
        try:
            page = await loop.run_in_executor(
                None, _tt_fetch_history, cookies, wid, conv_id, source_id,
                cursor, _TT_HISTORY_PAGE)
        except Exception as e:
            logger.warning(f"[DirectForward/TT] backlog history fetch failed: {e}")
            truncated = False
            break
        if not page:
            truncated = False
            break
        msgs.extend(page)
        if min(m["ts_us"] for m in page) <= watermark:
            truncated = False
            break
        cursor = min((m["cursor_ts_us"] for m in page if m["cursor_ts_us"]), default=0)
        if not cursor:
            truncated = False
            break
    if truncated and msgs:
        logger.warning("[DirectForward/TT] backlog: page cap ("
                       f"{_TT_HISTORY_PAGES * _TT_HISTORY_PAGE} rows) reached before "
                       "the watermark — the backlog exceeds one pass; raise "
                       "_TT_HISTORY_PAGES or reset history_watermark_us")
    msgs.sort(key=lambda m: m["ts_us"])

    new_watermark = watermark
    for m in msgs:
        if m["ts_us"] <= watermark:
            continue
        item_id = _tt_share_item_id(m.get("content"))
        if not item_id:
            new_watermark = max(new_watermark, m["ts_us"])
            continue
        if item_id in relayed_set:
            new_watermark = max(new_watermark, m["ts_us"])
            continue
        if fails.get(item_id, 0) >= _TT_HISTORY_MAX_FAILS:
            logger.warning(f"[DirectForward/TT] backlog: giving up on itemId "
                           f"{item_id} after {_TT_HISTORY_MAX_FAILS} failed relays")
            relayed_set.add(item_id)
            relayed.append(item_id)
            fails.pop(item_id, None)
            new_watermark = max(new_watermark, m["ts_us"])
            continue
        try:
            content = json.loads(m.get("content") or "{}")
        except Exception:
            content = {}
        ok = await _tt_relay_share(item_id, content, queue, chat_id,
                                   bot_client, premium_client, state)
        if ok:
            relayed_set.add(item_id)
            relayed.append(item_id)
            fails.pop(item_id, None)
            new_watermark = max(new_watermark, m["ts_us"])
        else:
            fails[item_id] = fails.get(item_id, 0) + 1
            if fails[item_id] >= _TT_HISTORY_MAX_FAILS:
                logger.warning(f"[DirectForward/TT] backlog: giving up on itemId "
                               f"{item_id} after {_TT_HISTORY_MAX_FAILS} failed relays")
                relayed_set.add(item_id)
                relayed.append(item_id)
                fails.pop(item_id, None)
                new_watermark = max(new_watermark, m["ts_us"])
                continue
            # Keep the watermark before the failed share so the next pass retries it.
            break

    tt["history_watermark_us"] = int(new_watermark)
    tt["relayed_item_ids"] = relayed[-_TT_RELAYED_CAP:]
    if len(fails) > 200:
        fails = dict(list(fails.items())[-200:])
    tt["relay_fail_counts"] = fails
    await _state_save_owned(state, {"tiktok"})


async def _tt_persist_seen(state: dict, seen: set) -> None:
    """Persist the seen-push-id set (trimmed to 2000) to the shared state file.

    Mutates the caller's state dict (never a throwaway copy) so a later
    ``_state_save_owned`` cannot clobber the list with a stale in-memory copy.
    """
    if len(seen) > 2000:
        for old in sorted(seen)[:-2000]:
            seen.discard(old)
    tt = state.setdefault("tiktok", {})
    tt["seen_msg_ids"] = sorted(seen)[-2000:]
    await _state_save_owned(state, {"tiktok"})


async def _tt_run_ws(bot_client, premium_client, chat_id, queue, state: dict,
                     seen: set, prime: bool = False) -> None:
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
        next_reconcile = 0.0 if prime else time.time()
        while True:
            # Refresh the liveness heartbeat each iteration so a healthy-but-
            # idle socket doesn't look "silent" to the watchdog (a stuck/dead
            # one stops refreshing and does alert).
            mark_worker_alive("tiktok")
            # Pushed messages are delivered exactly once, so reconcile against
            # the HTTP history on every connect and periodically while idle.
            if not prime and time.time() >= next_reconcile:
                await _tt_reconcile_backlog(cookies, wid, queue, chat_id,
                                            bot_client, premium_client, state)
                next_reconcile = time.time() + _tt_reconcile_interval()
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
            if prime:
                seen.add(msg_id)
                await _tt_persist_seen(state, seen)
                logger.info(f"[DirectForward/TT] prime: swallowed backlog msg {msg_id}")
                continue
            # Only mark the push as seen AFTER its relay has actually run, so a
            # crash/restart mid-queue cannot silently drop it (at-least-once).
            try:
                handled = await _tt_process_message(
                    m, queue, chat_id, bot_client, premium_client, state)
            except Exception as e:
                logger.error(f"[DirectForward/TT] relay of msg {msg_id} failed: {e}")
                continue
            if not handled:
                # Leave it unseen: the history reconciler retries it.
                logger.warning(f"[DirectForward/TT] msg {msg_id} not relayed — "
                               f"left for backlog reconciliation")
                continue
            seen.add(msg_id)
            await _tt_persist_seen(state, seen)


async def _tt_reconcile_once(state: dict, queue, chat_id, bot_client,
                             premium_client) -> None:
    """Best-effort reconcile that resolves its own cookies/wid.

    Used when the WebSocket itself is down: the push channel is then certainly
    dead, so the HTTP history read is the only way shares can still arrive."""
    cookies = _tt_jar_cookies()
    if not cookies.get("sessionid"):
        return
    loop = asyncio.get_event_loop()
    wid = await loop.run_in_executor(None, _tt_wid, cookies)
    if not wid:
        return
    await _tt_reconcile_backlog(cookies, wid, queue, chat_id, bot_client,
                                premium_client, state)


async def _tiktok_worker(bot_client, premium_client, chat_id: int, queue) -> None:
    state = _load_state()
    seen = set(state.get("tiktok", {}).get("seen_msg_ids", []))
    primed = bool(state.get("tiktok", {}).get("primed", False))

    if not primed:
        logger.info("[DirectForward/TT] first run — priming backlog, nothing relayed "
                    "for the first connect.")
        state.setdefault("tiktok", {})["primed"] = True
        # Establish the history watermark now so enabling the relay does not
        # flood the chat with the whole historical conversation.
        state["tiktok"].setdefault(
            "history_watermark_us", int(time.time() * 1_000_000))
        await _state_save_owned(state, {"tiktok"})
        try:
            await _tt_run_ws(bot_client, premium_client, chat_id, queue, state,
                             seen, prime=True)
        except Exception as e:
            logger.warning(f"[DirectForward/TT] priming connect failed: {e}")
        return

    logger.info(f"[DirectForward/TT] listening for TikTok self-DM pushes "
                f"(reconnect jittered ±{config.TIKTOK_DIRECT_POLL_JITTER_PCT}%).")
    while True:
        mark_worker_alive("tiktok")
        await wait_if_stopped()
        try:
            await _tt_run_ws(bot_client, premium_client, chat_id, queue, state, seen)
        except Exception as e:
            logger.error(f"[DirectForward/TT] WS error: {e}")
            try:
                await _tt_reconcile_once(state, queue, chat_id, bot_client, premium_client)
            except Exception as re_err:
                logger.warning(f"[DirectForward/TT] history reconcile while WS down failed: {re_err}")
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
    loop = asyncio.get_event_loop()
    conv = await loop.run_in_executor(None, _tt_self_dm_conversation, cookies, wid)
    if not conv:
        return (f"❌ **TikTok**: WebSocket auth OK (wid `{wid}`) but the HTTP IM "
                f"inbox did not return the self-DM conversation — the session "
                f"may be stale. Re-upload the jar.")
    try:
        msgs = await loop.run_in_executor(
            None, _tt_fetch_history, cookies, wid, conv["conv_id"],
            conv["source_id"], 0, 5)
        hist = (f"history OK ({len(msgs)} recent message(s) readable)"
                if msgs else "history reachable but empty")
    except Exception as e:
        hist = f"history read FAILED ({type(e).__name__})"
    return (f"✅ **TikTok**: jar OK, `sessionid` present, wid `{wid}`, "
            f"access_key `{ak}`.\n"
            f"• IM WebSocket: can authenticate\n"
            f"• Self-DM backlog: {hist}\n"
            f"• Relays retry missed shares every "
            f"`TIKTOK_DIRECT_RECONCILE_SECONDS` (default 900 s).")
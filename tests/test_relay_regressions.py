"""Regression tests for relay-delivery correctness bugs found in the
2026-11-08 audit.

Each test pins a defect that silently LOST user data in production:

1. TikTok photo posts: ``deliver_tiktok_photo`` reports "nothing delivered" by
   returning False, so ``_tt_relay_share`` recorded the itemId as relayed even
   though nothing was sent → the post was deduped forever (2026-11-08).
2. Targeted replay: recovering a known-missing itemId must not clear the
   dedupe set or rewind the watermark (the old ``--since`` mode re-relayed an
   entire window).
3. X text-only tweets: ``_full_text`` was referenced but never imported →
   ``NameError`` on every tweet with no video and no photo.
4. Instagram pk-only story/highlight shares: ``xma_entries`` was undefined →
   ``NameError`` whenever the author could not be read from the auxiliary text.
5. Queue: a queued relay dropped by "Abort Operations" (or rejected because
   the queue is full) must resolve its future, not leave the worker blocked
   forever.
6. ``database.json`` corruption must preserve a copy instead of being silently
   overwritten with defaults.

Standalone: ``venv/bin/python tests/test_relay_regressions.py`` (pytest-compatible).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.direct_forward import common  # noqa: E402
from modules.direct_forward import tiktok as tt  # noqa: E402


# ---------------------------------------------------------------------------
# 1) TikTok photo relay must report a False result
# ---------------------------------------------------------------------------

def _fake_enqueue_running_job():
    """An _enqueue_relay stand-in that actually runs the job, like the queue."""
    def enqueue(queue, chat_id, job):
        loop = asyncio.get_event_loop()
        fut = loop.create_future()

        async def runner():
            try:
                await job()
            except Exception as e:  # noqa: BLE001
                if not fut.done():
                    fut.set_exception(e)
            else:
                if not fut.done():
                    fut.set_result(None)

        asyncio.ensure_future(runner())
        return fut
    return enqueue


def test_tiktok_photo_relay_false_is_not_recorded_as_delivered():
    import utils.downloader.tiktok_photo as photo

    orig = (photo.fetch_embed_info, tt.deliver_tiktok_photo,
            tt._enqueue_relay, tt._header_lines)
    try:
        photo.fetch_embed_info = lambda item_id: {
            "is_photo": True, "author": "a", "images": ["x"], "caption": ""}
        tt.deliver_tiktok_photo = _make_async(False)
        tt._enqueue_relay = _fake_enqueue_running_job()
        tt._header_lines = lambda *a, **k: ["h"]
        state = {"tiktok": {}}
        ok = asyncio.run(tt._tt_relay_share("111", {}, None, 1, None, None, state))
        assert ok is False, "a photo post that delivered nothing must not report success"
        assert state["tiktok"].get("relayed_item_ids", []) == [], \
            "failed photo post must stay retryable"
    finally:
        (photo.fetch_embed_info, tt.deliver_tiktok_photo,
         tt._enqueue_relay, tt._header_lines) = orig


def test_tiktok_photo_relay_true_is_recorded():
    import utils.downloader.tiktok_photo as photo

    orig = (photo.fetch_embed_info, tt.deliver_tiktok_photo,
            tt._enqueue_relay, tt._header_lines)
    try:
        photo.fetch_embed_info = lambda item_id: {
            "is_photo": True, "author": "a", "images": ["x"], "caption": ""}
        tt.deliver_tiktok_photo = _make_async(True)
        tt._enqueue_relay = _fake_enqueue_running_job()
        tt._header_lines = lambda *a, **k: ["h"]
        state = {"tiktok": {}}
        ok = asyncio.run(tt._tt_relay_share("222", {}, None, 1, None, None, state))
        assert ok is True
        assert state["tiktok"]["relayed_item_ids"] == ["222"]
    finally:
        (photo.fetch_embed_info, tt.deliver_tiktok_photo,
         tt._enqueue_relay, tt._header_lines) = orig


def _make_async(value):
    async def _fn(*a, **k):
        return value
    return _fn


# ---------------------------------------------------------------------------
# 2) Targeted (missing-only) replay
# ---------------------------------------------------------------------------

def test_targeted_replay_requests_item_ids_without_clearing_dedupe():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "req.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"item_ids": ["7694362499512159510"]}, f)
    orig = tt._TT_REPLAY_REQUEST
    try:
        tt._TT_REPLAY_REQUEST = path
        state = {"history_watermark_us": 999, "relayed_item_ids": ["keep"],
                 "relay_fail_counts": {}}
        tt._tt_apply_replay_request(state)
        assert state["replay_item_ids"] == ["7694362499512159510"]
        assert state["history_watermark_us"] == 999, "targeted replay must not rewind"
        assert state["relayed_item_ids"] == ["keep"], "targeted replay must not clear dedupe"
        assert not os.path.exists(path)
    finally:
        tt._TT_REPLAY_REQUEST = orig


def test_reconcile_relays_targeted_item_below_watermark_only_once():
    """A requested itemId older than the watermark is relayed; nothing else is."""
    relayed_calls = []

    async def fake_save(state, owned):
        return state

    async def fake_relay(item_id, content, queue, chat_id, bot, premium, state):
        relayed_calls.append(item_id)
        if item_id == "wanted":
            tt._tt_record_relayed(state, item_id)
            return True
        return True

    page = [
        {"srv_id": 1, "ts_us": 100, "content": '{"itemId":"wanted"}', "cursor_ts_us": 100},
        {"srv_id": 2, "ts_us": 200, "content": '{"itemId":"neighbour"}', "cursor_ts_us": 200},
    ]

    def fake_fetch(cookies, wid, conv_id, source_id, cursor, count):
        return page if not cursor else []

    orig = (tt._state_save_owned, tt._tt_relay_share, tt._tt_fetch_history,
            tt._tt_apply_replay_request)
    try:
        tt._state_save_owned = fake_save
        tt._tt_relay_share = fake_relay
        tt._tt_fetch_history = fake_fetch
        tt._tt_apply_replay_request = lambda t: None
        state = {"tiktok": {"conv_id": "0:1:1:1", "source_id": 9,
                            "history_watermark_us": 5000,
                            "replay_item_ids": ["wanted"]}}
        asyncio.run(tt._tt_reconcile_backlog(
            {"sessionid": "x"}, "wid", None, 1, None, None, state))
        assert relayed_calls == ["wanted"], f"only the requested item: {relayed_calls}"
        assert state["tiktok"]["history_watermark_us"] == 5000
        assert "replay_item_ids" not in state["tiktok"]
        assert "wanted" in state["tiktok"]["relayed_item_ids"]
    finally:
        (tt._state_save_owned, tt._tt_relay_share, tt._tt_fetch_history,
         tt._tt_apply_replay_request) = orig


# ---------------------------------------------------------------------------
# 3) X text-only relay
# ---------------------------------------------------------------------------

def test_twitter_module_exposes_full_text():
    from modules.direct_forward import twitter
    assert hasattr(twitter, "_full_text"), \
        "twitter.py must import _full_text (NameError killed text-only tweets)"
    assert twitter._full_text(["a", "b"], "body") == "body\n\na\nb"


# ---------------------------------------------------------------------------
# 4) Instagram pk-only share resolution
# ---------------------------------------------------------------------------

def test_ig_pk_only_share_uses_xma_author_without_nameerror():
    from modules.direct_forward.instagram import _ig_resolve_raw
    item = {
        "original_media_igid": "123456",
        "auxiliary_text": "Sent a story",           # no @handle here
        "xma_share": [{"preview_url": "p", "header_title_text": "@someone"}],
    }
    got = _ig_resolve_raw(item)
    assert got and got["kind"] == "ig_pk" and got["pk"] == 123456
    assert got["author"] == "@someone"
    assert got["preview"] == "p"


# ---------------------------------------------------------------------------
# 5) Queue drop / rejection must resolve the relay future
# ---------------------------------------------------------------------------

def test_dropped_queued_relay_resolves_future():
    from utils.queue_manager import DownloadQueue

    async def scenario():
        q = DownloadQueue()
        fut = common._enqueue_relay(q, 7, lambda: asyncio.sleep(0))
        await asyncio.sleep(0)
        # Simulate "Abort Operations": drop the queued task.
        for t in list(q._pending):
            if t.on_cancel:
                t.on_cancel()
        q._pending.clear()
        try:
            await asyncio.wait_for(fut, timeout=2)
        except common._RelayAborted:
            return True
        return False

    assert asyncio.run(scenario()) is True


def test_queue_full_rejection_resolves_future():
    class _FullQueue:
        async def add_task(self, **kw):
            return ""

    async def scenario():
        fut = common._enqueue_relay(_FullQueue(), 7, lambda: asyncio.sleep(0))
        try:
            await asyncio.wait_for(fut, timeout=2)
        except common._RelayAborted:
            return True
        return False

    assert asyncio.run(scenario()) is True


# ---------------------------------------------------------------------------
# 6) Corrupt database is preserved
# ---------------------------------------------------------------------------

def test_corrupt_database_is_quarantined():
    import utils.gate as gate

    d = tempfile.mkdtemp()
    db = os.path.join(d, "database.json")
    with open(db, "w", encoding="utf-8") as f:
        f.write('{"authorized": [123], ')  # truncated JSON
    orig = gate.DB_FILE
    try:
        gate.DB_FILE = db
        data = gate.load_database()
        assert data["authorized"] == []
        copies = [p for p in os.listdir(d) if ".corrupt." in p]
        assert copies, "the corrupt database must be preserved, not silently replaced"
        payload = open(os.path.join(d, copies[0]), encoding="utf-8").read()
        assert "123" in payload
    finally:
        gate.DB_FILE = orig


def _run():
    tests = sorted(
        (n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)
    )
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"FAIL  {name}: {exc!r}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())

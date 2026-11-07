"""Regression tests for the TikTok self-DM relay backlog recovery.

Guards the 2026-11-07 outage: TikTok's oEmbed endpoint stopped returning
``author_url``, the worker treated "no author" as fatal, skipped every share
AND marked the push as seen — so the shares were lost forever (the push
channel delivers each message exactly once).

Invariants under test:

* The relay URL is the challenge-free ``/embed/<id>`` form; author resolution
  is cosmetic and must never block or fail a relay.
* A pushed share is only marked ``seen`` when its relay actually ran.
* The HTTP history endpoint (``get_by_conversation``) is decoded correctly and
  used to replay shares the push channel cannot deliver, oldest first, behind
  a persistent watermark with itemId-level dedupe.
* A failing share is retried at most 3 times and then given up on, so one
  poison message can never pin the watermark forever.

Standalone: ``venv/bin/python tests/test_tiktok_backlog.py`` (pytest-compatible).
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.direct_forward import tiktok as tt  # noqa: E402


# ---------------------------------------------------------------------------
# synthetic protobuf builders (mirror the wire layout used by the worker)
# ---------------------------------------------------------------------------

def _inbox_response(rows):
    list_body = b"".join(
        tt._pb_ld(1, tt._pb_str(1, conv) + tt._pb_uint(5, source))
        for conv, source in rows
    )
    payload = tt._pb_ld(203, list_body)
    return tt._pb_ld(6, payload)


def _history_response(rows):
    entries = b"".join(
        tt._pb_ld(1, tt._pb_uint(3, srv) + tt._pb_uint(4, ts)
                  + tt._pb_uint(7, 42) + tt._pb_str(8, content) + tt._pb_uint(25, ts))
        for srv, ts, content in rows
    )
    payload = tt._pb_ld(301, entries)
    return tt._pb_ld(6, payload)


def _share(item_id: str) -> str:
    return '{"aweType":800,"itemId":"%s","content_name":"x"}' % item_id


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_share_item_id_extraction():
    assert tt._tt_share_item_id(_share("7704453223486196998")) == "7704453223486196998"
    assert tt._tt_share_item_id('{"aweType":800}') is None
    assert tt._tt_share_item_id("not json") is None
    assert tt._tt_share_item_id("") is None


def test_parse_inbox_picks_self_dm_row():
    rows = [
        ("0:1:7431195759292826631:7539848641810170888", 7699960386226159880),
        ("0:1:7539848641810170888:7539848641810170888", 7671968801294024968),
    ]
    got = tt._tt_parse_inbox_self_dm(_inbox_response(rows))
    assert got == {"conv_id": "0:1:7539848641810170888:7539848641810170888",
                   "source_id": 7671968801294024968}
    assert tt._tt_parse_inbox_self_dm(b"") is None


def test_parse_history_is_oldest_first_with_cursor():
    rows = [
        (300, 2_000, _share("b")),
        (100, 1_000, _share("a")),
    ]
    msgs = tt._tt_parse_history_page(_history_response(rows))
    assert [m["srv_id"] for m in msgs] == [100, 300]
    assert [m["ts_us"] for m in msgs] == [1_000, 2_000]
    assert [tt._tt_share_item_id(m["content"]) for m in msgs] == ["a", "b"]
    assert msgs[0]["cursor_ts_us"] == 1_000


def test_relay_uses_embed_url_and_needs_no_author():
    captured = {}

    def fake_header(platform, sender, author, author_id, url):
        captured["url"] = url
        return ["x"]

    def enqueue(queue, chat_id, job):
        fut = asyncio.get_event_loop().create_future()
        fut.set_result(None)
        return fut

    orig = (tt._tt_resolve_author, tt._enqueue_relay, tt._header_lines)
    try:
        tt._tt_resolve_author = lambda item_id: None  # oEmbed + embed page down
        tt._enqueue_relay = enqueue
        tt._header_lines = fake_header
        state = {"tiktok": {}}
        ok = asyncio.run(tt._tt_relay_share(
            "7704453223486196998", {"content_name": "x"}, None, 1, None, None,
            state))
        assert ok is True
        assert captured["url"] == "https://www.tiktok.com/embed/7704453223486196998"
        assert state["tiktok"]["relayed_item_ids"] == ["7704453223486196998"]
    finally:
        tt._tt_resolve_author, tt._enqueue_relay, tt._header_lines = orig


def _run_reconcile(state, pages, relay_results):
    """Drive one reconcile pass with stubbed history + relay."""
    async def fake_save(state, owned):
        return state

    async def fake_relay(item_id, content, queue, chat_id, bot, premium, state):
        ok = relay_results.get(item_id, False)
        if ok:
            tt._tt_record_relayed(state, item_id)
        return ok

    def fake_fetch(cookies, wid, conv_id, source_id, cursor, count):
        return pages[0] if not cursor else []

    orig = (tt._state_save_owned, tt._tt_relay_share, tt._tt_fetch_history)
    try:
        tt._state_save_owned = fake_save
        tt._tt_relay_share = fake_relay
        tt._tt_fetch_history = fake_fetch
        asyncio.run(tt._tt_reconcile_backlog(
            {"sessionid": "x"}, "wid", None, 1, None, None, state))
    finally:
        tt._state_save_owned, tt._tt_relay_share, tt._tt_fetch_history = orig


def test_reconcile_replays_only_above_watermark_and_dedupes():
    state = {"tiktok": {"conv_id": "0:1:1:1", "source_id": 9,
                        "history_watermark_us": 500,
                        "relayed_item_ids": ["already"]}}
    pages = [[
        {"srv_id": 1, "ts_us": 100, "content": _share("old"), "cursor_ts_us": 100},
        {"srv_id": 2, "ts_us": 1000, "content": _share("new1"), "cursor_ts_us": 1000},
        {"srv_id": 3, "ts_us": 1100, "content": _share("already"), "cursor_ts_us": 1100},
        {"srv_id": 4, "ts_us": 2000, "content": _share("new2"), "cursor_ts_us": 2000},
    ]]
    _run_reconcile(state, pages, {"new1": True, "new2": True})
    t = state["tiktok"]
    assert t["relayed_item_ids"] == ["already", "new1", "new2"]
    assert t["history_watermark_us"] == 2000
    assert not t["relay_fail_counts"]


def test_reconcile_pins_watermark_until_three_strikes():
    pages = [[
        {"srv_id": 1, "ts_us": 1000, "content": _share("poison"), "cursor_ts_us": 1000},
    ]]
    state = {"tiktok": {"conv_id": "0:1:1:1", "source_id": 9,
                        "history_watermark_us": 500}}
    for i in range(1, 4):
        _run_reconcile(state, pages, {})  # every relay fails
        t = state["tiktok"]
        if i < 3:
            assert t["history_watermark_us"] == 500, f"pinned on attempt {i}"
            assert t["relay_fail_counts"]["poison"] == i
            assert "poison" not in t["relayed_item_ids"]
        else:
            # Third strike: give up on poison and advance past it.
            assert t["relay_fail_counts"] == {}
            assert "poison" in t["relayed_item_ids"]
            assert t["history_watermark_us"] == 1000


def test_reconcile_is_noop_without_new_messages():
    state = {"tiktok": {"conv_id": "0:1:1:1", "source_id": 9,
                        "history_watermark_us": 5000}}
    _run_reconcile(state, [[
        {"srv_id": 1, "ts_us": 100, "content": _share("old"), "cursor_ts_us": 100},
    ]], {})
    assert state["tiktok"]["history_watermark_us"] == 5000
    assert "relayed_item_ids" not in state["tiktok"] or \
        state["tiktok"]["relayed_item_ids"] == []


def test_process_message_does_not_confirm_a_failed_relay():
    """A failed relay must return False so the caller leaves the push unseen."""
    async def fake_relay(item_id, content, queue, chat_id, bot, premium, state):
        return False

    orig = tt._tt_relay_share
    try:
        tt._tt_relay_share = fake_relay
        m = {"conversation_id": "0:1:7:7", "server_message_id": 1,
             "content": _share("newshare")}
        state = {"tiktok": {}}
        ok = asyncio.run(tt._tt_process_message(m, None, 1, None, None, state))
        assert ok is False
        assert "relayed_item_ids" not in state["tiktok"]
    finally:
        tt._tt_relay_share = orig


def test_process_message_skips_already_relayed_item():
    """History may have relayed a share before its push arrives — no duplicate."""
    calls = []

    async def fake_relay(item_id, content, queue, chat_id, bot, premium, state):
        calls.append(item_id)
        return True

    orig = tt._tt_relay_share
    try:
        tt._tt_relay_share = fake_relay
        m = {"conversation_id": "0:1:7:7", "server_message_id": 2,
             "content": _share("done")}
        state = {"tiktok": {"relayed_item_ids": ["done"]}}
        ok = asyncio.run(tt._tt_process_message(m, None, 1, None, None, state))
        assert ok is True and calls == []
    finally:
        tt._tt_relay_share = orig


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

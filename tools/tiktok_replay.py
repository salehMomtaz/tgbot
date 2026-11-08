#!/usr/bin/env python3
"""List / replay TikTok self-DM shares from the HTTP IM history.

The WebSocket push channel delivers each self-DM message exactly once; the
worker's history reconciler automatically recovers anything missed since its
persisted watermark. This tool looks further back and lets the operator opt in
to replaying an older window.

Usage (read-only by default):

    venv/bin/python tools/tiktok_replay.py                 # list recent shares
    venv/bin/python tools/tiktok_replay.py --pages 10      # deeper listing
    venv/bin/python tools/tiktok_replay.py --ids 7691...,7703... --dry-run
    venv/bin/python tools/tiktok_replay.py --ids 7691...,7703...
    venv/bin/python tools/tiktok_replay.py --since 2026-10-23 --dry-run

TWO replay modes:

* ``--ids <comma-separated itemIds>`` — TARGETED recovery. The worker relays
  only those itemIds, wherever they sit in history, and skips any that are
  already in ``relayed_item_ids``. The watermark is untouched, so this cannot
  re-send an already-delivered post from the surrounding window. This is the
  safe way to recover a known-missing batch.

* ``--since <UTC date>`` — WINDOW replay. It rewinds the watermark and CLEARS
  the relayed-item dedupe, so every share after that date is re-relayed,
  including ones already delivered. Always run it with ``--dry-run`` first and
  read the listed duplicate count.

``--ids``/``--since`` write ``cache/tiktok_replay_request.json``. The RUNNING
worker owns ``direct_forward_state.json`` (an external edit would be
overwritten), so the worker applies the request itself on its next history pass
— within ``TIKTOK_DIRECT_RECONCILE_SECONDS`` (default 900 s, jittered) — or
immediately after a restart.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from modules.direct_forward import tiktok as tt  # noqa: E402
from modules.direct_forward.state import _load_state  # noqa: E402

STATE_FILE = os.path.join(ROOT, "direct_forward_state.json")


def _date(ts_us: int) -> str:
    return datetime.fromtimestamp(ts_us / 1_000_000, timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC")


def fetch_shares(pages: int) -> list[dict]:
    cookies = tt._tt_jar_cookies()
    if not cookies.get("sessionid"):
        raise SystemExit("No TikTok sessionid in cookies/tiktok/ttcookies.txt")
    wid = tt._tt_wid(cookies)
    if not wid:
        raise SystemExit("Could not fetch wid — the TikTok session may be stale")
    conv = tt._tt_self_dm_conversation(cookies, wid)
    if not conv:
        raise SystemExit("Could not resolve the self-DM conversation")
    out: list[dict] = []
    cursor = 0
    for _ in range(pages):
        page = tt._tt_fetch_history(cookies, wid, conv["conv_id"],
                                    conv["source_id"], cursor,
                                    tt._TT_HISTORY_PAGE)
        if not page:
            break
        for m in page:
            item_id = tt._tt_share_item_id(m["content"])
            if item_id:
                out.append({"ts_us": m["ts_us"], "item_id": item_id,
                            "srv_id": m["srv_id"]})
        cursor = min((m["cursor_ts_us"] for m in page if m["cursor_ts_us"]),
                     default=0)
        if not cursor:
            break
    out.sort(key=lambda m: m["ts_us"])
    return out


def parse_since(value: str) -> int:
    try:
        if len(value) == 10:          # YYYY-MM-DD
            dt = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        else:                         # YYYY-MM-DDTHH:MM
            dt = datetime.strptime(value, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        raise SystemExit("--since must be YYYY-MM-DD or YYYY-MM-DDTHH:MM (UTC)")
    return int(dt.timestamp() * 1_000_000)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pages", type=int, default=3,
                    help="history pages to read (20 rows each; default 3)")
    ap.add_argument("--ids", help="comma-separated itemIds to replay (targeted, duplicate-safe)")
    ap.add_argument("--since", help="replay shares after this UTC date/time (re-relays delivered posts)")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --ids/--since: only show what would be replayed")
    args = ap.parse_args()

    state = _load_state()
    st = state.get("tiktok", {}) or {}
    relayed = set(st.get("relayed_item_ids") or [])
    watermark = int(st.get("history_watermark_us") or 0)

    try:
        shares = fetch_shares(max(1, args.pages))
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"History read failed: {type(e).__name__}: {e}")

    if not shares:
        print("No share messages found in the requested history window.")
        return 0

    print(f"{'date (UTC)':<20} {'delivered':<9} {'itemId':<21} author-uid")
    for m in shares:
        mark = "yes" if m["item_id"] in relayed else "-"
        print(f"{_date(m['ts_us']):<20} {mark:<9} {m['item_id']:<21} {m['srv_id']}")
    print(f"\nwatermark: {_date(watermark) if watermark else 'unset'}")
    print(f"relayed item ids on record: {len(relayed)}")

    if args.ids:
        ids = [x.strip() for x in args.ids.split(",") if x.strip()]
        known = {m["item_id"]: m for m in shares}
        present = [known[i] for i in ids if i in known]
        absent = [i for i in ids if i not in known]
        todo = [m for m in present if m["item_id"] not in relayed]
        print(f"\n--ids: {len(ids)} requested, {len(present)} found in history, "
              f"{len(absent)} absent, {len(todo)} not yet delivered.")
        for i in absent:
            print(f"   absent (deleted/private): {i}")
        if args.dry_run or not todo:
            return 0
        req = {"item_ids": [m["item_id"] for m in todo],
               "requested_at": datetime.now(timezone.utc).isoformat()}
        os.makedirs(os.path.dirname(tt._TT_REPLAY_REQUEST) or ".", exist_ok=True)
        with open(tt._TT_REPLAY_REQUEST, "w", encoding="utf-8") as f:
            json.dump(req, f, indent=2)
        print(f"Wrote {tt._TT_REPLAY_REQUEST} ({len(todo)} itemId(s)). The running "
              "worker applies it on its next history pass (or on restart).")
        return 0

    if not args.since:
        print("\n(Replay is opt-in: pass --ids ID[,ID...] or --since YYYY-MM-DD "
              "[--dry-run].)")
        return 0

    since_us = parse_since(args.since)
    target = [m for m in shares if m["ts_us"] > since_us]
    print(f"\n--since {args.since}: {len(target)} share(s) would be replayed.")
    if args.dry_run or not target:
        return 0

    req = {"watermark_us": since_us, "clear_relayed": True,
           "requested_at": datetime.now(timezone.utc).isoformat()}
    os.makedirs(os.path.dirname(tt._TT_REPLAY_REQUEST) or ".", exist_ok=True)
    with open(tt._TT_REPLAY_REQUEST, "w", encoding="utf-8") as f:
        json.dump(req, f, indent=2)
    print(f"Wrote {tt._TT_REPLAY_REQUEST}. The running worker applies it on its "
          "next history pass (or on restart).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

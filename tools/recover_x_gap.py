#!/usr/bin/env python3
"""One-off recovery for a dropped X / XChat direct-forward message range.

Background: the X worker used to advance its per-conversation cursor as soon as
a relay job was *enqueued* (fire-and-forget). Any restart while the shared
download queue still held unrun jobs dropped those messages forever, because
the cursor was already past them (see modules/direct_forward/common.py:
``_enqueue_relay`` for the fix). ``cache/xchat_inbox.jsonl`` is append-only, so
the dropped lines are still on disk — this tool rewinds the conversation cursor
so the worker re-relays them.

Two knobs, both explicit:
  --resume SEQ     set the worker's per-conversation cursor to SEQ (the last
                   message that WAS delivered). Everything after it replays.
  --drop-from SEQ  (optional) delete inbox lines for this conversation whose
                   sequence id is >= SEQ. Use it to avoid re-delivering a later
                   batch that already arrived; the dropped lines are already
                   delivered so nothing is lost.

The bridge keeps its OWN cursor and never re-emits old lines, so this only
affects what the Python worker reads.

A timestamped backup of both files is written next to them as ``*.pre-recover``.
Stop the bot (and ideally the xchat bridge) before running, then start them
again; the worker replays the gap on its next poll.

Example — the 2026-10-08 gap on the linked peer conversation:
  python tools/recover_x_gap.py \\
    --conv 1743868576920928256:2095053127040876548 \\
    --resume 2105129464476684288 \\
    --drop-from 2108135431178989568
"""

import argparse
import json
import os
import shutil
import time


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--conv", required=True, help="conversation id key (e.g. uid:peer)")
    ap.add_argument("--resume", required=True, help="last delivered sequence id")
    ap.add_argument("--drop-from", default=None,
                    help="drop inbox lines for this conv with seq >= this value")
    ap.add_argument("--state", default="direct_forward_state.json")
    ap.add_argument("--inbox", default="cache/xchat_inbox.jsonl")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conv = str(args.conv)
    resume = str(args.resume)
    drop_from = str(args.drop_from) if args.drop_from else None

    if not os.path.exists(args.state):
        print(f"!! state file not found: {args.state}")
        return 2
    with open(args.state, "r", encoding="utf-8") as f:
        state = json.load(f)

    cursors = state.setdefault("x", {}).setdefault("cursors", {})
    old = cursors.get(conv)
    print(f"cursor[{conv}]: {old} -> {resume}")
    cursors[conv] = resume
    # A stale fail counter for a dropped id would immediately re-skip it.
    state.setdefault("x", {})["fail_counts"] = {}

    dropped = 0
    kept = []
    if os.path.exists(args.inbox):
        with open(args.inbox, "r", encoding="utf-8") as f:
            for raw in f:
                s = raw.strip()
                if not s:
                    continue
                try:
                    o = json.loads(s)
                except ValueError:
                    kept.append(s)
                    continue
                if drop_from and str(o.get("conv") or "") == conv:
                    try:
                        if int(o.get("id") or 0) >= int(drop_from):
                            dropped += 1
                            continue
                    except (TypeError, ValueError):
                        pass
                kept.append(s)
    print(f"inbox: kept {len(kept)} line(s), dropped {dropped} already-delivered line(s)")

    if args.dry_run:
        print("dry-run — nothing written")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    for path in (args.state, args.inbox):
        if os.path.exists(path):
            shutil.copy2(path, f"{path}.pre-recover.{stamp}")

    with open(args.state, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    if os.path.exists(args.inbox):
        with open(args.inbox, "w", encoding="utf-8") as f:
            f.write("\n".join(kept) + ("\n" if kept else ""))
    print(f"done — restart the bot; the worker will replay from {resume}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

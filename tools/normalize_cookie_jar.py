#!/usr/bin/env python3
# tools/normalize_cookie_jar.py
"""
One-time (but re-runnable) cookie-jar repair.

Deduplicates a Netscape jar by ``(domain, path, name)`` and, when a domain
allowlist is supplied, drops cookies from other domains. Both are exactly what
:mod:`utils.cookie_manager` now does on every read/write — this tool just
applies it to the file on disk so a jar that predates the fix (e.g. the
Instagram jar polluted with ``google.com`` / ISP-injected cookies and a
duplicated cookie block) is cleaned without waiting for the next rotation.

Originals are never lost: copy the jar (or its ``cookies/history_snapshots/``
copy) before running if you want a manual rollback.

Usage:
    python tools/normalize_cookie_jar.py igcookies          # by preset name
    python tools/normalize_cookie_jar.py --all              # every primary jar
    python tools/normalize_cookie_jar.py --path cookies/instagram/igcookies.txt \
        --domains instagram.com

Presets come for free from ``utils.cookie_refresher._SITES`` (the same
domain allowlists the headless refresher now enforces).
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import cookie_manager  # noqa: E402
from utils.cookie_refresher import _SITES  # noqa: E402


def _presets() -> dict:
    out = {}
    for path, _url, _hint, allowed in _SITES:
        name = os.path.basename(path).replace("cookies.txt", "")
        out[name] = (path, tuple(allowed))
    return out


def main() -> int:
    presets = _presets()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("preset", nargs="?", choices=sorted(presets),
                    help="preset jar to normalize")
    ap.add_argument("--all", action="store_true", help="normalize every preset jar")
    ap.add_argument("--path", help="explicit jar path (use with --domains)")
    ap.add_argument("--domains", help="comma-separated domain allowlist for --path")
    args = ap.parse_args()

    targets = []
    if args.path:
        domains = tuple(d.strip() for d in (args.domains or "").split(",") if d.strip())
        targets.append((args.path, domains or None))
    elif args.all:
        targets.extend(presets.values())
    elif args.preset:
        targets.append(presets[args.preset])
    else:
        ap.error("choose a preset, --all, or --path")

    rc = 0
    for path, domains in targets:
        before = 0
        if os.path.exists(path):
            before = sum(1 for ln in open(path, encoding="utf-8", errors="replace")
                         if ln.strip() and not ln.startswith("#"))
        dropped = cookie_manager.normalize_jar(path, domains or None, actor="tools/normalize")
        after = 0
        if os.path.exists(path):
            after = sum(1 for ln in open(path, encoding="utf-8", errors="replace")
                        if ln.strip() and not ln.startswith("#"))
        if dropped < 0:
            print(f"[skip] {path}: missing/unreadable")
            rc = 1
        elif dropped == 0:
            print(f"[ok]   {path}: already clean ({after} cookies)")
        else:
            print(f"[fix]  {path}: {before} -> {after} cookies (dropped {dropped})")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

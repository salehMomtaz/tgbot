#!/usr/bin/env python3
"""Validate a browser-exported Instagram cookie file BEFORE it goes near the bot.

The password-login path is currently throttled (429); the fallback is
bootstrapping the jar from a session exported from a logged-in desktop
browser (DevTools → Application → Cookies → instagram.com, exported in
Netscape format). Uploading a bad export would burn the one light
validation probe on garbage — this checker rejects garbage offline.

Usage (on the VPS, file copied over e.g. via scp):
    venv/bin/python tools/check_browser_cookies.py /tmp/ig_export.txt

Checks (zero network):
  - Netscape format parses; sessionid present and non-empty
  - ds_user_id present (tells you WHICH account the cookies belong to)
  - csrftoken + mid present (the write-back path needs them)
  - rur present (server-signed; without it the jar is a partial export)
  - sessionid identical to the latched dead one -> refuse (don't re-upload death)
  - ds_user_id differs from the persisted device's account -> warn that the
    device identity must NOT be reused across accounts (fresh device needed)

Exit 0 = READY to upload via Admin console (Cookies -> Replace IG jar).
Exit 1 = do NOT upload; fix the export first.
Exit 2 = usage error.
"""

from __future__ import annotations

import json
import os
import sys


def _fp(value: str) -> str:
    if not value:
        return "(missing)"
    return f"{value[:4]}…{value[-4:]}({len(value)})"


def _parse_netscape(path: str) -> dict:
    cookies: dict = {}
    lines = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            lines += 1
            name = parts[5].strip()
            # last field may itself contain '=' (e.g. base64 padding) — the
            # VALUE is everything after the 6th tab, rejoined.
            value = "\t".join(parts[6:]).strip()
            if name and value:
                cookies[name] = value
    return {"cookies": cookies, "lines": lines}


def _jar_sessionid() -> str | None:
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from utils import ig_session_guard
        return ig_session_guard.jar_sessionid()
    except Exception:
        return None


def _latch_info() -> dict:
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from utils import ig_session_guard
        return ig_session_guard.read_latch() or {}
    except Exception:
        return {}


def _persisted_account() -> str | None:
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from utils import ig_session_guard
        return ig_session_guard.persisted_account_id("direct_ig_session.json")
    except Exception:
        return None


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: venv/bin/python tools/check_browser_cookies.py /tmp/ig_export.txt")
        return 2
    path = sys.argv[1]
    if not os.path.exists(path):
        print(f"file not found: {path}")
        return 2

    parsed = _parse_netscape(path)
    cookies = parsed["cookies"]
    print(f"parsed {parsed['lines']} cookie lines from {path}")

    problems: list[str] = []
    for name in ("sessionid", "ds_user_id", "csrftoken", "mid", "rur"):
        present = bool(cookies.get(name))
        print(f"  {name:12s} {_fp(cookies.get(name, ''))}")
        if not present:
            problems.append(f"missing {name}")

    if problems:
        print("VERDICT: NOT READY — " + "; ".join(problems))
        print("(Re-export: DevTools on instagram.com AFTER logging in, full "
              "Netscape format, all cookies — not a filtered subset.)")
        return 1

    # Same dead sessionid as the latch? Re-uploading it buys one more 403.
    # (Fingerprint construction mirrors ig_anti_detect.sessionid_sig, which
    # hashes the raw cookie value — but that helper takes a jar PATH, so we
    # hash the exported value directly here.)
    import hashlib
    export_sig = hashlib.sha256(cookies["sessionid"].encode()).hexdigest()
    latch = _latch_info()
    if latch.get("sig") and export_sig and latch["sig"] == export_sig:
        print("VERDICT: NOT READY — this is the SAME sessionid the hard-stop "
              f"latch is armed for (reason: {latch.get('reason')}). "
              "Log in fresh in the browser first (log out + log back in), "
              "then export again.")
        return 1

    # Two accounts must never share one Android device identity.
    mine = _persisted_account()
    theirs = cookies["ds_user_id"]
    if mine and mine != theirs:
        print(f"WARNING: export belongs to account {theirs}, persisted device "
              f"belongs to {mine}. Do NOT reuse direct_ig_session.json — "
              "delete it before the first bootstrap so ONE fresh device is "
              "minted for the new account.")
    elif mine:
        print(f"account: {theirs} (matches persisted device)")

    cur = _jar_sessionid()
    if cur and cur == cookies["sessionid"]:
        print("NOTE: identical to the current jar — nothing to gain by uploading.")

    print("VERDICT: READY — upload via Admin console (Cookies -> Replace IG jar).")
    print("The worker then makes ONE light validation probe (account_info).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

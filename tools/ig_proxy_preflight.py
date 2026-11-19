#!/usr/bin/env python3
"""Pre-flight check for the IG sticky residential proxy (bytes-conscious).

Verifies, with the smallest possible traffic, that the iProyal proxy the bot
will use for Instagram API calls actually exits in the right place BEFORE you
mint a session or enable the worker. Run on the VPS:

    IPROYAL_USER='<proxy-username>' IPROYAL_PASS='<proxy-password>' \\
        venv/bin/python tools/ig_proxy_preflight.py

    # with Royal API balance display (token from dashboard Settings > API):
    IPROYAL_USER='...' IPROYAL_PASS='...' IPROYAL_API_TOKEN='<royal-token>' \\
        venv/bin/python tools/ig_proxy_preflight.py --city prague

Network cost: one ~2 KB account-API call (skipped without a token) + ONE
~200-byte egress probe. Nothing else. Credentials come from env vars or
interactive prompts only — never from files, never logged, never stored.

What it prints:
  - remaining iProyal traffic (if a Royal API token was given),
  - the proxy exit IP + country/city (must be CZ / Prague),
  - the exact DIRECT_FORWARD_PROXY line for .env,
  - the exact mint command to run next.
"""

from __future__ import annotations

import argparse
import getpass
import os
import random
import string
import sys


def _mask(secret: str) -> str:
    if not secret:
        return "(missing)"
    return f"{secret[:2]}…{secret[-2:]}({len(secret)})"


def _check_balance(api_token: str | None) -> None:
    if not api_token:
        print("balance: skipped (no IPROYAL_API_TOKEN / --api-token)")
        return
    import requests
    try:
        r = requests.get(
            "https://resi-api.iproyal.com/v1/me",
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=20,
        )
        r.raise_for_status()
        info = r.json()
        print(f"balance: {info.get('available_traffic')} GB remaining "
              f"(subusers: {info.get('subusers_count')})")
    except Exception as e:
        print(f"balance: FAILED ({e}) — continuing with egress check")


def _egress_check(proxy_url: str) -> bool:
    """ONE tiny probe through the proxy. Returns True when exit is CZ."""
    import requests
    # ~200 bytes: IP + country/city only. This is the ONLY proxied byte
    # this script ever spends.
    probe = "http://ip-api.com/json/?fields=status,countryCode,city,query"
    try:
        r = requests.get(
            probe,
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=30,
        )
        r.raise_for_status()
        info = r.json()
    except Exception as e:
        print(f"egress probe: FAILED ({e})")
        return False
    if info.get("status") != "success":
        print(f"egress probe: unexpected reply {info}")
        return False
    cc = info.get("countryCode", "?")
    print(f"egress probe: exit {info.get('query')} — {cc} / {info.get('city')}")
    if cc != "CZ":
        print("egress probe: NOT Czech Republic — do NOT mint through this. "
              "Check _country-cz / city targeting.")
        return False
    print("egress probe: OK (Czech exit)")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Tiny iProyal sticky-proxy pre-flight for IG.")
    ap.add_argument("--country", default="cz", help="exit country (default: cz)")
    ap.add_argument("--city", default="prague", help="exit city (default: prague)")
    ap.add_argument("--session", default="tgig0001",
                    help="sticky session id, exactly 8 alphanumerics (default: tgig0001). "
                         "Keep ONE value for the account's lifetime.")
    ap.add_argument("--lifetime", default="7d",
                    help="sticky lifetime, max 7d (default: 7d)")
    ap.add_argument("--scheme", default="http", choices=["http", "socks5h"],
                    help="proxy scheme (default: http; socks5h needs --port 32325)")
    ap.add_argument("--port", default="12321",
                    help="entry port: 12321 for http, 32325 for socks5 (default: 12321)")
    ap.add_argument("--api-token", default="",
                    help="Royal API token for balance display (or IPROYAL_API_TOKEN)")
    ap.add_argument("--no-check", action="store_true",
                    help="print the .env line without spending the egress probe")
    args = ap.parse_args()

    sid = args.session
    if len(sid) != 8 or not sid.isalnum():
        print(f"bad --session {sid!r}: must be exactly 8 alphanumeric chars")
        return 2

    user = os.getenv("IPROYAL_USER", "").strip()
    password = os.getenv("IPROYAL_PASS", "").strip()
    if not user:
        user = input("iProyal proxy username: ").strip()
    if not password:
        password = getpass.getpass("iProyal proxy password (hidden): ").strip()
    if not user or not password:
        print("username/password required (env IPROYAL_USER / IPROYAL_PASS)")
        return 2
    api_token = (args.api_token or os.getenv("IPROYAL_API_TOKEN", "")).strip() or None

    suffix = (f"_country-{args.country}_city-{args.city}"
              f"_session-{sid}_lifetime-{args.lifetime}_killswitch-1")
    # NOTE: the killswitch flag makes a dead sticky IP fail fast (HTTP 410)
    # instead of silently rotating — a silent rotation would change the
    # account's identity IP mid-session.
    proxy_url = f"{args.scheme}://{user}:{password}{suffix}@geo.iproyal.com:{args.port}"

    print(f"proxy user: {_mask(user)} @ geo.iproyal.com:12321")
    print(f"targeting: country={args.country} city={args.city} "
          f"session={sid} lifetime={args.lifetime} killswitch=on")
    _check_balance(api_token)
    if not args.no_check:
        if not _egress_check(proxy_url):
            return 1

    print()
    print("Put this in the VPS .env (exact line):")
    print(f"DIRECT_FORWARD_PROXY={args.scheme}://{user}:{password}{suffix}@geo.iproyal.com:{args.port}")
    print()
    print("Then mint the app-native session THROUGH the proxy (ONE attempt):")
    print("  venv/bin/python tools/mint_ig_session.py --yes")
    print("(mint reads DIRECT_FORWARD_PROXY from .env; rest the account 24-72h")
    print(" after the last automation warning first.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

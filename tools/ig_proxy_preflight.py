#!/usr/bin/env python3
"""Pre-flight check for the IG sticky residential proxy (bytes-conscious).

Verifies, with the smallest possible traffic, that the iProyal proxy the bot
will use for Instagram API calls actually exits in the right place BEFORE you
mint a session or enable the worker. Run on the VPS with NO flags: by default
it tests the exact DIRECT_FORWARD_PROXY already in .env — the same string the
mint script and the bot will use:

    venv/bin/python tools/ig_proxy_preflight.py

    # with Royal API balance display (token from dashboard Settings > API):
    IPROYAL_API_TOKEN='<royal-token>' venv/bin/python tools/ig_proxy_preflight.py

To test a DIFFERENT configuration, pass flags (then it prints the exact
.env line to install):

    IPROYAL_USER='<u>' IPROYAL_PASS='<p>' \\
        venv/bin/python tools/ig_proxy_preflight.py --session tgig0003

Network cost: one ~2 KB account-API call (skipped without a token) + ONE
~200-byte egress probe. Nothing else. Credentials come from env vars or
interactive prompts only — never logged, never stored.

What it prints:
  - remaining iProyal traffic (if a Royal API token was given),
  - the proxy exit IP + country/city,
  - the exact DIRECT_FORWARD_PROXY line for .env (when rebuilding),
  - the exact mint command to run next.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
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
        print("egress probe: NOT Czech Republic — do NOT mint through this.")
        return False
    print("egress probe: OK (Czech exit)")
    return True


def _dotenv_proxy_url() -> str | None:
    """The full DIRECT_FORWARD_PROXY value already in .env, if any."""
    try:
        with open(".env", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    m = re.search(r"DIRECT_FORWARD_PROXY=(\S+)", text)
    return m.group(1) if m else None


def _split_proxy_url(url: str) -> dict:
    """Split a proxy URL into components; password may carry _key-value suffix."""
    m = re.match(r"^(\w+)://([^:]+):(.+)@([^:]+):(\d+)$", url)
    if not m:
        return {}
    scheme, user, creds, host, port = m.groups()
    keys: dict = {}
    cut = creds.find("_country-")
    password = creds if cut < 0 else creds[:cut]
    tail = "" if cut < 0 else creds[cut:]
    for k in ("country", "city", "session", "lifetime"):
        km = re.search(rf"_{k}-([^_]+)", tail)
        if km:
            keys[k] = km.group(1)
    return {"scheme": scheme, "user": user, "password": password,
            "host": host, "port": port, **keys}


def main() -> int:
    ap = argparse.ArgumentParser(description="Tiny iProyal sticky-proxy pre-flight for IG.")
    ap.add_argument("--country", default=None, help="exit country (default: from .env, else cz)")
    ap.add_argument("--city", default=None,
                    help="pin an exit city, e.g. prague (default: from .env, else none = country-wide)")
    ap.add_argument("--no-city", action="store_true",
                    help="drop any city pin (country-wide exit pool)")
    ap.add_argument("--session", default="",
                    help="sticky session id, exactly 8 alphanumerics. Default: the "
                         "_session- value already in .env, else tgig0001. "
                         "Keep ONE value per account lifetime after first success.")
    ap.add_argument("--lifetime", default=None, help="sticky lifetime, max 7d")
    ap.add_argument("--scheme", default=None, choices=["http", "socks5h"],
                    help="proxy scheme (http needs port 12321, socks5h needs 32325)")
    ap.add_argument("--port", default=None, help="entry port")
    ap.add_argument("--host", default=None, help="entry node")
    ap.add_argument("--api-token", default="",
                    help="Royal API token for balance display (or IPROYAL_API_TOKEN)")
    ap.add_argument("--no-check", action="store_true",
                    help="skip the egress probe (spends nothing)")
    args = ap.parse_args()

    env_url = _dotenv_proxy_url()
    env = _split_proxy_url(env_url) if env_url else {}
    rebuild = any([args.session, args.country, args.city is not None,
                   args.no_city, args.lifetime, args.scheme, args.port, args.host])

    if env_url and not rebuild:
        # Default path: test EXACTLY what mint and the bot will use.
        proxy_url = env_url
        desc = (f"session={env.get('session', '?')} country={env.get('country', '?')} "
                f"city={env.get('city', 'any')} (exact .env string)")
        print(f"mode: testing exact .env DIRECT_FORWARD_PROXY "
              f"(user {_mask(env.get('user', ''))} @ {env.get('host')}:{env.get('port')})")
    else:
        country = args.country or env.get("country") or "cz"
        city = None if args.no_city else (args.city or env.get("city"))
        sid = args.session or env.get("session") or "tgig0001"
        if len(sid) != 8 or not sid.isalnum():
            print(f"bad session {sid!r}: must be exactly 8 alphanumeric chars")
            return 2
        lifetime = args.lifetime or env.get("lifetime") or "7d"
        scheme = args.scheme or env.get("scheme") or "http"
        host = args.host or env.get("host") or "proxy.iproyal.com"
        port = args.port or env.get("port") or ("32325" if scheme == "socks5h" else "12321")

        user = os.getenv("IPROYAL_USER", "").strip() or env.get("user", "")
        password = os.getenv("IPROYAL_PASS", "").strip() or env.get("password", "")
        if not user:
            user = input("iProyal proxy username: ").strip()
        if not password:
            # NOTE: the killswitch flag makes a dead sticky IP fail fast
            # (HTTP 410) instead of silently rotating — a silent rotation
            # would change the account's identity IP mid-session.
            password = getpass.getpass("iProyal proxy password (hidden): ").strip()
        if not user or not password:
            print("username/password required (env IPROYAL_USER / IPROYAL_PASS, or .env)")
            return 2

        city_part = f"_city-{city}" if city else ""
        suffix = (f"_country-{country}{city_part}"
                  f"_session-{sid}_lifetime-{lifetime}_killswitch-1")
        proxy_url = f"{scheme}://{user}:{password}{suffix}@{host}:{port}"
        desc = (f"session={sid} country={country} city={city or 'any'} "
                f"lifetime={lifetime} killswitch=on")
        print(f"mode: rebuilt string (user {_mask(user)} @ {host}:{port} {scheme})")

    print(f"targeting: {desc}")
    _check_balance((args.api_token or os.getenv("IPROYAL_API_TOKEN", "")).strip() or None)
    if not args.no_check:
        if not _egress_check(proxy_url):
            return 1

    if rebuild or not env_url:
        print()
        print("Put this in the VPS .env (exact line):")
        # password embedded: this line is meant for the operator's own
        # terminal/.env only — never paste it into chat or docs.
        print(f"DIRECT_FORWARD_PROXY={proxy_url}")
    print()
    print("Then mint the app-native session THROUGH the proxy (ONE attempt):")
    print("  venv/bin/python tools/mint_ig_session.py --yes")
    print("(mint reads DIRECT_FORWARD_PROXY from .env; rest the account 24-72h")
    print(" after the last automation warning first.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

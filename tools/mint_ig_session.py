#!/usr/bin/env python3
"""Mint an app-native Instagram session with instagrapi — ONE attempt, no retries.

Why this exists (2026-11-10 research): a `sessionid` exported from Chrome is a
WEB session. Driving it through instagrapi's Android private API is the
"browser session used by a different device" pattern Instagram treats as a
stolen session. This script logs in through the SAME instagrapi Android device
that will later use the session, so the session and device match from birth.

Usage (on the VPS, with the bot's IG worker disabled):

    # 1. Make sure .env has IG_AUTH_ENABLED=false and restart the bot.
    # 2. Run:
    venv/bin/python tools/mint_ig_session.py
    # or, to mint through a residential proxy (recommended if you have one):
    venv/bin/python tools/mint_ig_session.py --proxy 'http://user:pass@host:port'

It prompts for the username, password (hidden) and the 2FA code if Instagram
asks for one. It makes exactly ONE login attempt; any challenge/429/error
aborts with a clear message. Credentials are never written to disk or logged.

After success:
    - direct_ig_session.json is updated (same Android device identity);
    - cookies/instagram/igcookies.txt gets the live session cookies;
    - set IG_AUTH_ENABLED=true and restart the bot when you are ready.

IMPORTANT: wait 24-72 h after an automation warning before running this, keep
the same egress IP, and do not use the account in a browser at the same time.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402


def _fp(value: str) -> str:
    if not value:
        return "(none)"
    return f"{value[:4]}…{value[-4:]}({len(value)})"


def main() -> int:
    ap = argparse.ArgumentParser(description="Mint an app-native Instagram session")
    ap.add_argument("--proxy", default="",
                    help="proxy for the login + API (default: DIRECT_FORWARD_PROXY)")
    ap.add_argument("--username", default="",
                    help="Instagram username (prompted if omitted)")
    ap.add_argument("--yes", action="store_true",
                    help="skip the confirmation prompt")
    args = ap.parse_args()

    from instagrapi import Client
    from instagrapi.exceptions import (
        ChallengeRequired, PleaseWaitFewMinutes, TwoFactorRequired,
    )
    from utils import ig_anti_detect

    try:
        from importlib.metadata import version as _pkg_version
        _iv = _pkg_version("instagrapi")
    except Exception:
        _iv = "unknown"
    if _iv != "unknown" and not _iv.startswith("3."):
        print(f"⚠ instagrapi {_iv} is old: it presents app version 428, while the "
              f"current Instagram app is 449. Minting with a stale app version can "
              f"fail or increase risk. Upgrading to 3.2.8 is recommended before a "
              f"live login (see docs/memory/tgbot-2026-11-10-instagram-automation-"
              f"warning-research.md).")
        if not args.yes and input("Continue anyway? type YES: ").strip() != "YES":
            print("aborted")
            return 1

    if getattr(config, "IG_AUTH_ENABLED", False):
        print("✋ IG_AUTH_ENABLED is true. Stop the bot's IG worker first:\n"
              "   set IG_AUTH_ENABLED=false in .env and restart the bot.\n"
              "   (The bot is currently IG-disabled in this deployment.)")
        return 2

    proxy = (args.proxy or getattr(config, "DIRECT_FORWARD_PROXY", "") or "").strip()
    if not args.yes:
        print("This performs ONE Instagram login with instagrapi's Android device.")
        print("Make sure the account has rested and the egress IP is the one the")
        print("bot will keep using. Credentials are never stored.")
        if proxy:
            print(f"Proxy: {proxy.split('@')[-1]}")
        else:
            print("Proxy: (none — direct VPS IP)")
        if input("Type YES to continue: ").strip() != "YES":
            print("aborted")
            return 1

    cl = Client()
    cl.delay_range = [2, 4]
    cl.request_timeout = 30

    # Reuse the ONE persisted Android device identity, if present.
    if os.path.exists("direct_ig_session.json") and os.path.getsize("direct_ig_session.json") > 0:
        try:
            cl.load_settings("direct_ig_session.json")
            print("reusing persisted Android device identity")
        except Exception as e:
            print(f"could not load the persisted device identity ({e}); using a fresh one")

    if proxy:
        cl.set_proxy(proxy)
        print("proxy set for login + API")

    ig_anti_detect.install_transport(cl, "chrome136")
    ig_anti_detect.apply_identity(cl)   # resolved geo + Chrome-136 public UA + app version

    username = (args.username or input("Instagram username: ")).strip().lstrip("@")
    password = getpass.getpass("Instagram password (hidden): ")
    if not username or not password:
        print("username/password required")
        return 1

    print("logging in (one attempt)…")
    try:
        cl.login(username, password)
    except TwoFactorRequired:
        code = input("2FA code: ").strip()
        try:
            cl.login(username, password, verification_code=code)
        except Exception as e:
            print(f"✗ login failed after 2FA: {type(e).__name__}: {e}")
            return 1
    except ChallengeRequired as e:
        print(f"✗ challenge required — no automatic resolution. Complete it in the "
              f"official app, then wait and retry later. ({e})")
        return 1
    except PleaseWaitFewMinutes as e:
        print(f"✗ Instagram says please wait — stop and let the account rest. ({e})")
        return 1
    except Exception as e:
        print(f"✗ login failed: {type(e).__name__}: {e}")
        print("No retry was attempted. Wait and try again later.")
        return 1

    # Re-apply identity AFTER login (login_by_sessionid/login call init()).
    ig_anti_detect.apply_identity(cl)

    try:
        cl.dump_settings("direct_ig_session.json")
        os.chmod("direct_ig_session.json", 0o600)
    except Exception as e:
        print(f"✗ could not persist direct_ig_session.json: {e}")
        return 1
    try:
        ig_anti_detect.write_back_session(cl, config.IG_COOKIES)
    except Exception as e:
        print(f"⚠ session write-back failed: {e}")

    sid = getattr(cl, "sessionid", "") or ""
    print("\n✓ app-native session minted")
    print(f"  account      : {getattr(cl, 'username', username)} (id {getattr(cl, 'user_id', '?')})")
    print(f"  sessionid    : {_fp(sid)}")
    print(f"  device mid   : {_fp(getattr(cl, 'mid', '') or '')}")
    print(f"  geo          : {getattr(cl, 'country', '?')} / {getattr(cl, 'locale', '?')} / "
          f"{getattr(cl, 'timezone_name', '?')}")
    print("\nNext: set IG_AUTH_ENABLED=true in .env and restart the bot.")
    print("Keep the same egress IP and do not log in from a browser at the same time.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

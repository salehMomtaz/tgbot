# utils/cookie_refresher.py
"""
Sequential headless cookie refresher — keeps jars warm even when DM is the only user.

Why: Instagram (and X/TikTok/YouTube) rotate session cookies server-side via Set-Cookie.
yt-dlp write-back already captures rotations, but DM-only accounts never trigger yt-dlp,
so their jars go stale and hit `update_risky_contactpoint` / `login_required` after ~7 days.
Headless browser refresh visits the site as a real user, lets Set-Cookie rotate, and
captures the result — same effect as a manual Chrome incognito refresh, but automated.

Resource: 4 GB RAM + 8 GB swap → one Chromium at a time, sequential, not 4 tabs.
Each site: launch -> add_cookies -> goto -> wait -> extract -> close. Peak ~300 MB.
Scheduled every 24h via main.py::auto_refresh_cookies_loop.

Proxy: uses config.PROXY_URL / DIRECT_FORWARD_PROXY if set, same IP as DM worker,
so no impossible-travel flag.
"""
import asyncio
import logging
import os
import time
from typing import List

import config
from utils import cookie_history
from utils import cookie_manager

logger = logging.getLogger(__name__)

# Sites to refresh, in order. One at a time.
# The 4th element is the DOMAIN ALLOWLIST for that jar: the headless browser's
# context also collects cookies for whatever else it encounters (the VPS's ISP
# injects internet.tci.ir cookies; instagram embeds google.com scripts), and
# the old code overlaid *every* context cookie into the jar. That is how the IG
# jar ended up carrying `.google.com __Secure-ENID` and `internet.tci.ir`
# entries it has no business holding. Only these domains are ever written back.
# YouTube is the one site that genuinely needs google.com cookies too.
_SITES = [
    # (cookie_path, homepage_url, wait_selector_hint, allowed_domains)
    (config.IG_COOKIES, "https://www.instagram.com/", "nav", ("instagram.com",)),
    (config.X_COOKIES, "https://x.com/home", "body", ("x.com", "twitter.com")),
    (config.TT_COOKIES, "https://www.tiktok.com/", "body", ("tiktok.com",)),
    (config.YT_COOKIES, "https://www.youtube.com/", "ytd-app", ("youtube.com", "google.com")),
]

def _domain_allowed(domain: str, allowed: tuple) -> bool:
    """Thin alias for :func:`utils.cookie_manager.domain_matches` (kept so the
    call sites read naturally)."""
    return cookie_manager.domain_matches(domain, allowed)

def _parse_netscape_to_playwright(path: str) -> List[dict]:
    """Read Netscape jar and return playwright cookie dicts."""
    cookies = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                # Netscape: domain, flag, path, secure, expiration, name, value
                # HttpOnly is encoded as "#HttpOnly_.domain" prefix on domain
                http_only = False
                if line.startswith("#HttpOnly_"):
                    http_only = True
                    line = line[len("#HttpOnly_"):]
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                domain, _, c_path, secure, exp, name, value = parts[:7]
                try:
                    exp_int = int(exp)
                except:
                    exp_int = -1
                # Playwright expects expires as float seconds, -1 = session
                cookies.append({
                    "name": name,
                    "value": value,
                    "domain": domain.lstrip("."),
                    "path": c_path,
                    "expires": float(exp_int) if exp_int > 0 else -1,
                    "httpOnly": http_only,
                    "secure": secure.upper() == "TRUE",
                    "sameSite": "Lax",
                })
    except Exception as e:
        logger.warning(f"[CookieRefresh] parse {path} failed: {e}")
    return cookies

async def _refresh_one(cookie_path: str, url: str, wait_hint: str = None,
                       allowed_domains: tuple = ()) -> bool:
    """Refresh a single jar via headless Chromium. Returns True if cookies changed and were written.

    *allowed_domains* is the site's domain allowlist (see ``_SITES``): only
    cookies on those domains are overlaid back, so foreign cookies the browser
    happened to collect never leak into the jar.
    """
    if not os.path.exists(cookie_path) or os.path.getsize(cookie_path) == 0:
        logger.info(f"[CookieRefresh] skip {cookie_path} — missing/empty")
        return False
    # Quick check: if the jar was freshly written via yt-dlp or admin
    # (<24h), skip the OTHER sites to save RAM — but NEVER skip IG here.
    # Instagram's auth is tightly bound to mid/rur/csrftoken rotation, and
    # the only way to capture a fresh rotation is to actually visit the
    # site. The 17:32 cycle (Aug 28) skipped IG with "mtime 6h ago (<20h)"
    # while the session was already stale — that skip is exactly what
    # caused the 30-redirects regression.
    is_ig = cookie_path.endswith("igcookies.txt")
    if not is_ig:
        try:
            mtime = os.path.getmtime(cookie_path)
            if time.time() - mtime < 20*3600:  # 20h, in case 24h loop drifts
                logger.info(f"[CookieRefresh] skip {os.path.basename(cookie_path)} — mtime {int((time.time()-mtime)/3600)}h ago (<20h)")
                return False
        except:
            pass

    playwright_cookies = _parse_netscape_to_playwright(cookie_path)
    if not playwright_cookies:
        logger.warning(f"[CookieRefresh] no parseable cookies in {cookie_path} — skip")
        return False

    # Use proxy if configured (same as DM worker, avoids impossible travel)
    proxy = getattr(config, "DIRECT_FORWARD_PROXY", None) or getattr(config, "PROXY_URL", None)
    proxy_server = None
    if proxy:
        # Playwright expects {"server": "http://host:port", "username":..., "password":...}
        # We pass raw string; aiohttp proxy style "http://user:pass@host:port" works for chromium via --proxy-server
        # Simplification: use http:// prefix
        proxy_server = proxy
        logger.info(f"[CookieRefresh] using proxy for {os.path.basename(cookie_path)}")

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("[CookieRefresh] playwright not installed (pip install playwright) — skip")
        return False

    browser = None
    try:
        async with async_playwright() as p:
            # One browser at a time, headless, no-sandbox for VPS
            launch_args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            if proxy_server:
                # Playwright proxy is per-browser: use proxy server
                browser = await p.chromium.launch(headless=True, args=launch_args, proxy={"server": proxy_server} if "://" in proxy_server else {"server": f"http://{proxy_server}"})
            else:
                browser = await p.chromium.launch(headless=True, args=launch_args)
            # Single context, single page
            context = await browser.new_context(
                user_agent=getattr(config, "YTDLP_USER_AGENT", "") or "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
                locale=getattr(config, "IG_DIRECT_LOCALE", "en_US") or "en_US",
            )
            await context.add_cookies(playwright_cookies)
            page = await context.new_page()
            logger.info(f"[CookieRefresh] visiting {url} for {os.path.basename(cookie_path)}...")
            # Goto with timeout 45s
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                # Wait for network idle a bit, but not too long (15s)
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except:
                    pass
                # Small extra wait for Set-Cookie to settle
                await asyncio.sleep(5)
                # Trigger some JS that may refresh token (scroll)
                try:
                    await page.evaluate("window.scrollBy(0, 200)")
                    await asyncio.sleep(2)
                except:
                    pass
                # Instagram-specific: visit a few authenticated paths so
                # the server issues fresh mid/rur/csrftoken. Just loading
                # the home page often isn't enough — IG only rotates on
                # authed XHRs. /api/v1 web endpoints are the same paths
                # instagrapi hits, so cookies set there are the same ones
                # the DM worker needs.
                #
                # We also dismiss the "Save Your Login Info?" / "Turn on
                # Notifications?" dialogs that Instagram shows to fresh
                # devices (a headless Chromium without a saved profile
                # presents as a new device to IG) — the dialog clicks
                # trigger the personalization XHRs that issue ps_l/ps_n
                # (the cookies Chrome's web login uses to recognize the
                # account). Without them, an operator who downloads
                # the jar and injects it into Chrome incognito sees
                # "no account" — the exact regression the operator
                # reported on 2026-08-28.
                if is_ig:
                    # Dismiss the "Save Your Login Info?" / "Not Now" button
                    # if it appears (typical on first IG visit from a new
                    # device). The button text varies by locale; we look for
                    # the most common English variant.
                    try:
                        await page.locator(
                            'button:has-text("Not Now")').first.click(timeout=2000)
                        await asyncio.sleep(1)
                    except Exception:
                        pass
                    try:
                        await page.locator(
                            'button:has-text("Save info")').first.click(timeout=2000)
                    except Exception:
                        pass
                    # Dismiss the "Turn on Notifications?" dialog
                    try:
                        await page.locator(
                            'button:has-text("Not Now")').first.click(timeout=2000)
                    except Exception:
                        pass
                    # Visit authed pages that exercise the personalization XHR.
                    for extra_path in ("/explore/", "/accounts/edit/", "/"):
                        try:
                            await page.goto(
                                f"https://www.instagram.com{extra_path}",
                                wait_until="domcontentloaded", timeout=20000)
                            await asyncio.sleep(2)
                        except Exception as e2:
                            logger.info(
                                f"[CookieRefresh] IG extra visit "
                                f"{extra_path} skipped: {e2}")
                    # Scroll the home page a bit so the feed XHR fires and
                    # IG issues ps_l/ps_n (the personalization cookies Chrome
                    # web needs to recognize the account).
                    try:
                        for _ in range(3):
                            await page.evaluate("window.scrollBy(0, 800)")
                            await asyncio.sleep(2)
                    except Exception:
                        pass
            except Exception as e:
                logger.warning(f"[CookieRefresh] goto {url} failed: {e} — still trying to extract cookies")

            # Capture the final URL and (for IG) whether the anonymous login
            # form is present — MUST happen while the page/context are still
            # open. The old code called page.content() AFTER context.close(),
            # which always raised and silently disabled the strongest part of
            # the logged-in gate (IG serves its anonymous home from the SAME
            # url with a fresh anonymous sessionid, so the form is the only
            # unambiguous tell). See the 2026-09-03 16:49 jar wipe this gate
            # was added to prevent.
            final_url = ""
            try:
                final_url = page.url
            except Exception:
                pass
            anonymous_login_form = False
            if is_ig:
                try:
                    html = await page.content()
                    anonymous_login_form = (
                        'action="/accounts/login/ajax/"' in html
                        or ('name="username"' in html and 'name="password"' in html)
                    )
                except Exception:
                    pass  # DOM check failed — fall back to the URL/sessionid gate

            new_cookies = await context.cookies()
            await context.close()
            await browser.close()
            browser = None

            if not new_cookies:
                logger.warning(f"[CookieRefresh] no cookies extracted from {url}")
                cookie_history.record(cookie_path, "refresher_visit", actor="cookie_refresher",
                                      note=f"no cookies extracted (url={final_url or '?'})")
                return False

            # Normalise domains (the browser returns a leading dot for domain
            # cookies; the parsed jar stripped it) and drop anything outside
            # the site's allowlist so foreign cookies never leak into the jar.
            def _norm_domain(d: str) -> str:
                return (d or "").lower().lstrip(".")

            new_map = {(_norm_domain(c["domain"]), c["name"]): c["value"]
                       for c in new_cookies}
            old_map = {(_norm_domain(c["domain"]), c["name"]): c["value"]
                       for c in playwright_cookies}
            if allowed_domains:
                allowed_map = {(d, n): v for (d, n), v in new_map.items()
                               if _domain_allowed(d, allowed_domains)}
            else:
                allowed_map = dict(new_map)
            dropped = len(new_map) - len(allowed_map)
            if dropped:
                logger.info(f"[CookieRefresh] ignoring {dropped} cookie(s) outside "
                            f"{allowed_domains} for {os.path.basename(cookie_path)}")

            # --- Logged-in gate: NEVER write a jar the site logged out of. ---
            def _has_cookie(name: str, domain_hint: str) -> bool:
                return any(n == name and domain_hint in d
                           for (d, n) in allowed_map)

            visited_login_page = any(
                marker in (final_url or "").lower()
                for marker in ("/accounts/login", "/login", "/signin", "/auth")
            )
            if is_ig:
                logged_in = (_has_cookie("sessionid", "instagram")
                             and not visited_login_page
                             and not anonymous_login_form)
            else:
                # Other sites: a login-page redirect while we carried cookies
                # means the session was rejected — refuse to write for them too.
                logged_in = not visited_login_page
            if not logged_in:
                reason = ("anonymous login form detected" if anonymous_login_form
                          else f"visit ended logged out (url={final_url or '?'})")
                logger.error(
                    f"[CookieRefresh] {os.path.basename(cookie_path)} — {reason}. "
                    f"NOT writing: the context holds an anonymous/rejected cookie "
                    f"set. Jar left untouched; snapshot preserved in "
                    f"cookies/history_snapshots/. If this repeats, upload a fresh "
                    f"jar (Admin → Cookies).")
                cookie_history.record(cookie_path, "refresher_refused", actor="cookie_refresher",
                                      snapshot=True,
                                      note=f"{reason} — jar NOT overwritten")
                return False

            # Compare only the allowlisted cookies: did anything rotate?
            changed = sum(1 for k, v in allowed_map.items() if old_map.get(k) != v)
            if changed == 0:
                logger.info(f"[CookieRefresh] {os.path.basename(cookie_path)} — no rotation detected (still fresh)")
                # Still touch mtime via meta to avoid re-checking too soon
                cookie_history.record(cookie_path, "refresher_visit", actor="cookie_refresher",
                                      note="no rotation detected (logged in)")
                cookie_manager.touch_cookie_success(cookie_path)
                return False

            # Write back as an OVERLAY (atomic, respects 0o444 lock, never
            # deletes): only cookies the context actually rotated/added are
            # applied. The old full-replace shrank the jar on every cycle
            # (24 lines → 14) and dropped cookies the browser context never
            # touched — an overlay keeps everything the operator uploaded.
            # ONLY the site's allowlisted domains are considered (see _SITES).
            updates: dict[tuple[str, str], str] = {}
            for (domain, name), value in allowed_map.items():
                if domain and name and value is not None:
                    updates[(f".{domain}", name)] = value
            if not updates:
                logger.warning(f"[CookieRefresh] no usable cookies extracted from {url}")
                return False
            changed_written = cookie_manager.overlay_cookies(
                cookie_path, updates, actor="cookie_refresher")
            if changed_written is None or changed_written < 0:
                logger.warning(f"[CookieRefresh] overlay write failed for {cookie_path}")
                return False

            # Mark the rotation in cookies/meta.json so the admin menu's
            # "Last headless refresh" line tracks reality.
            cookie_manager.mark_merge(cookie_path, changed_written)
            cookie_manager.touch_cookie_success(cookie_path)

            logger.info(f"[CookieRefresh] {os.path.basename(cookie_path)} refreshed via headless ({changed_written} cookies overlaid, {len(new_cookies)} total)")
            # Also clear direct_ig_session.json if IG was refreshed, so next DM login uses fresh sessionid
            if cookie_path == config.IG_COOKIES:
                for stale in ("direct_ig_session.json", "direct_ig_session.json.bak"):
                    try:
                        if os.path.exists(stale):
                            os.remove(stale)
                            logger.info(f"[CookieRefresh] cleared {stale} after IG refresh")
                    except:
                        pass
            return True

    except Exception as e:
        logger.error(f"[CookieRefresh] {os.path.basename(cookie_path)} refresh failed: {e}")
        if browser:
            try:
                await browser.close()
            except:
                pass
        return False
    finally:
        if browser:
            try:
                await browser.close()
            except:
                pass

async def refresh_all_cookies_sequential():
    """Refresh each primary jar one after another (sequential, 1 browser at a time)."""
    from utils.shared import wait_if_stopped
    logger.info("[CookieRefresh] starting sequential refresh (4 sites, 1 browser at a time, ~5 min total)")
    for cookie_path, url, hint, allowed in _SITES:
        # A set abort flag pauses (self-clears in ~30s) rather than aborting the
        # whole 24h refresh cycle.
        await wait_if_stopped()
        try:
            ok = await _refresh_one(cookie_path, url, hint, allowed)
            # Small pause between sites to avoid hammering and to let swap settle
            await asyncio.sleep(5)
            # Log result is already in _refresh_one
        except Exception as e:
            logger.error(f"[CookieRefresh] site {url} failed: {e}")
            await asyncio.sleep(5)
    logger.info("[CookieRefresh] sequential refresh cycle complete")

async def auto_refresh_cookies_loop():
    """Background loop: every 24h, refresh all jars sequentially. Enabled via config."""
    # Respect flag: only run if at least one primary jar exists
    if not any(os.path.exists(p) for p, _, _, _ in _SITES):
        logger.info("[CookieRefresh] no primary jars found — loop disabled")
        return
    # Stagger first run by 5-10 min after boot to avoid competing with DM warmup
    await asyncio.sleep(300 + (hash(os.urandom(4)) % 300))
    while True:
        try:
            await refresh_all_cookies_sequential()
        except Exception as e:
            logger.error(f"[CookieRefresh] cycle failed: {e}")
        # Sleep 24h (with jitter ±1h to avoid exact 24h cadence detection)
        import random
        await asyncio.sleep(24*3600 + random.randint(-3600, 3600))

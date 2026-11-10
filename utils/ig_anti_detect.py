# utils/ig_anti_detect.py
"""
Instagram anti-detection hardening for the direct-forward IG worker.

The native Instagram checkpoint (``challenge_required`` on ``direct_v2/inbox``)
is triggered primarily by *identity correlation*: the private API is driven by a
plain Python ``requests`` session (no TLS/browser impersonation, so the TLS
fingerprint is a dead giveaway that the caller is a script), plus synthetic
headers that don't match what the real app echoes back.

This module closes those gaps with four, independently-failing-safe pieces:

1. ``install_transport`` — swap the private session's HTTP adapter for a
   curl_cffi-backed ``CurlCffiAdapter`` (the same one instagrapi ships for its
   public transport). ``cl.private`` stays a regular ``requests.Session`` so
   every existing call site (``.headers``, ``.cookies``, ``.proxies``,
   ``.verify``, ``.mount``) keeps working unchanged; only the TLS layer
   impersonates a real Chrome. Falls back to the stock adapter on ANY failure —
   the bot must keep running even if curl-adapter/curl_cffi break.

2. ``install_token_echo`` — the okgram gap. The server sets
   ``ig-set-ig-u-rur`` / ``ig-set-ig-u-shbid`` / ``ig-set-ig-u-shbts`` /
   ``x-ig-set-www-claim`` / ``ig-set-x-mid`` on responses; instagrapi only
   captures ``ig-set-x-mid`` and persists ``ig_u_rur``/``ig_www_claim`` but NOT
   the shbid/shbts pair. We capture all of them, persist into ``cl.settings``
   (so ``dump_settings`` makes them durable), and feed them back into
   ``base_headers`` on every request. If Instagram never sends them we keep
   instagrapi's synthetic values — no hard failure.

3. ``pin_geo`` — force the account's home region / locale / timezone so a
   session created in one place never speaks with another region's accent.

4. ``warmup`` — a short, paced, benign sequence of read-only calls right after
   login (profile, inbox listing) so the first real poll isn't the session's
   very first activity on a fresh IP.

5. ``burst_pace`` — for backfills (gap recovery after a long downtime),
   the cumulative activity is the signal that triggers Instagram's "we
   suspect automated behavior" prompt: 33 items delivered in 2 minutes
   at a 4-6s cadence looks like scripted scraping. The fix is per-item
   pacing that scales with the burst size (a small backfill of 1-2 items
   is unchanged; a 30+ item backfill adds seconds per item so the
   cumulative activity looks like a human re-reading messages after a
   day off).

6. ``cold_start_jitter`` — Instagram correlates a "first request 200ms
   after login" pattern with bot activity. After login, before the first
   real poll, simulate a "user opens app, reads inbox" sequence with
   randomized timing that includes 1-2 unrelated-thread reads, so the
   paired-thread poll isn't the very first thing the session does.

7. ``record_public_soft_block`` / ``public_soft_block_active`` — public
   web endpoints (used internally by instagrapi for ``media_pk_from_url``)
   return HTML (not JSON) when Instagram applies a soft block. Counting
   these and backing off when the rate exceeds a threshold prevents
   the bot from hammering an endpoint that's actively throttling it.

8. ``install_email_change_alert`` — register a no-op
   ``change_password_handler`` that detects the "Instagram forced email
   change" flow (which is signalled by ``change_password_handler`` being
   invoked). We do NOT attempt to bypass (that would deepen the flag);
   we just alert the operator and let the exception propagate so the
   worker freezes per the existing challenge policy.

9. ``sessionid_sig`` — sha256 fingerprint of the jar's ``sessionid`` VALUE.
   The only legitimate trigger for a login attempt / breaker re-arm is this
   value changing (operator uploads a fresh jar). Cookie rotation by the
   refresher / yt-dlp write-back changes mtime, never the sessionid — and
   must never re-arm a login.

Every entry point is a no-op (or a logged degradation) when its dependency is
missing, so this file can never take the worker down.
"""

import hashlib
import logging
import math
import os
import random
import time

import config

logger = logging.getLogger(__name__)


def sessionid_sig(jar_path: str) -> str:
    """sha256 fingerprint of the jar's ``sessionid`` VALUE ("" if absent).

    This is the ONLY legitimate "fresh jar uploaded" signal. The cookie
    refresher and yt-dlp write-back rotate OTHER cookies and bump the file's
    mtime while the same (possibly dead) sessionid stays in place — gating a
    login attempt on mtime re-tried logins with a dead session and deepened
    Instagram's enforcement (proven 2026-10-03..08 in cookies/history.jsonl).
    """
    try:
        with open(jar_path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                if raw.startswith("#") or not raw.strip():
                    continue
                parts = raw.rstrip("\n").split("\t")
                if len(parts) >= 7 and parts[5] == "sessionid" and parts[6]:
                    return hashlib.sha256(parts[6].encode()).hexdigest()
    except Exception:
        pass
    return ""

# Response headers Instagram sets that the app echoes back on later requests.
# instagrapi already persists ig_u_rur / ig_www_claim / mid; shbid + shbts are
# the two it drops on the floor.
_ECHO_HEADER_MAP = {
    "ig-set-ig-u-rur": "ig_u_rur",
    "ig-set-ig-u-shbid": "ig_u_shbid",
    "ig-set-ig-u-shbts": "ig_u_shbts",
    "x-ig-set-www-claim": "ig_www_claim",
    "ig-set-x-mid": "mid",
}
# The base_headers keys that should be repopulated from the captured values.
_ECHO_APPLY = {
    "ig_u_rur": "IG-U-RUR",
    "ig_u_shbid": "IG-U-SHBID",
    "ig_u_shbts": "IG-U-SHBTS",
    "ig_www_claim": "X-IG-WWW-Claim",
}

_CURL_ADAPTER = None  # None=uninitialised, False=unavailable, class=loaded
_BASE_HEADERS_PATCHED = False  # class-level property swap: once per process
_GET_SETTINGS_PATCHED = False  # class-level method swap: once per process
_RETRY_PATCHED = False  # class-level method swap: once per process
_DEFAULT_IMPERSONATE = "chrome136"


_DECODE_PATCHED = False  # CurlStreamResponse._decode signature shim: once per process

_LOGIN_NOISE_FILTERED = False  # instagrapi login-fallback spam filter: once per process

_NOISE_MARKERS = (
    "Client error user_stream_by_id_v1",   # login_by_sessionid fallback probe
    "Client error user_info_by_id_v1",     # same family, user_info_v1 probe
)


def _install_login_noise_filter():
    """Drop instagrapi's DEAD-SESSION probe noise from the logs.

    ``login_by_sessionid`` validates a session by calling ``user_info_v1``;
    when the session is dead, instagrapi catches the 403 and probes
    ``user_stream_by_id_v1`` as a fallback, and that second probe logs an
    ERROR **with a full traceback** per attempt. With 12 IG friends × an
    hourly cycle this alone produced ~158 ERROR posts in the log channel
    over 12h (2026-09-05 incident) — the actionable signal ("IG session is
    dead, re-upload the jar") is already logged once by OUR code
    (friend_media's breaker / direct_forward's re-login handler); the
    per-probe tracebacks are pure noise. This filter drops only those
    records; every other instagrapi error still passes through.
    Idempotent per process.
    """
    global _LOGIN_NOISE_FILTERED
    if _LOGIN_NOISE_FILTERED:
        return
    try:
        ig_user_logger = logging.getLogger("instagrapi.mixins.user")

        def _drop_login_probe_noise(record):
            try:
                msg = record.getMessage()
            except Exception:
                return True
            return not any(marker in msg for marker in _NOISE_MARKERS)

        # A logger-level filter applies before the logger's own level check
        # propagates the record to root, so the tracebacks never reach any
        # handler (file mirror + Telegram channel).
        ig_user_logger.addFilter(_drop_login_probe_noise)
        _LOGIN_NOISE_FILTERED = True
        logger.info("[IG anti-detect] instagrapi dead-session probe noise filter installed")
    except Exception as e:
        logger.warning(f"[IG anti-detect] login-noise filter install failed: {e}")


def _patch_decode_signature():
    """Compat shim: curl-adapter 1.1.0's ``CurlStreamResponse._decode`` has the
    signature ``(data, decode_content, flush_decoder)`` but urllib3 >= 2.3 calls
    ``_decode(..., max_length=None)``. Without the extra kwarg every request
    through the curl adapter dies with ``TypeError: CurlStreamResponse._decode()
    got an unexpected keyword argument 'max_length'``. Wrap it to swallow the
    kwarg (curl already handles content decoding). No-op on curl-adapter 1.2.x
    (its own signature already matches urllib3)."""
    global _DECODE_PATCHED
    if _DECODE_PATCHED:
        return
    try:
        from curl_adapter.stream.response import CurlStreamResponse

        orig_decode = CurlStreamResponse._decode
        if "max_length" in orig_decode.__code__.co_varnames:
            _DECODE_PATCHED = True
            return

        def _decode_compat(self, data, decode_content, flush_decoder, max_length=None):
            return orig_decode(self, data, decode_content, flush_decoder)

        CurlStreamResponse._decode = _decode_compat
        _DECODE_PATCHED = True
        logger.info("[IG anti-detect] CurlStreamResponse._decode patched for "
                    "urllib3 max_length kwarg")
    except Exception as e:
        logger.warning(f"[IG anti-detect] _decode signature patch failed ({e}); "
                       f"curl transport may crash on urllib3 >= 2.3.")


def _load_curl_adapter():
    """Import the curl-backed requests adapter, applying the curl_cffi
    ``normalize_browser_type`` and curl-adapter ``_decode`` compat shims first.
    Returns the class or False."""
    global _CURL_ADAPTER
    if _CURL_ADAPTER is not None:
        return _CURL_ADAPTER
    try:
        import curl_cffi.requests.impersonate as _imp

        if not hasattr(_imp, "normalize_browser_type") and hasattr(
            _imp, "resolve_latest_browser_type"
        ):
            _imp.normalize_browser_type = _imp.resolve_latest_browser_type
        from curl_adapter import CurlCffiAdapter  # noqa: F401

        _patch_decode_signature()
        _CURL_ADAPTER = CurlCffiAdapter
    except Exception as e:
        logger.warning(f"[IG anti-detect] curl transport unavailable ({e}); "
                       f"staying on plain requests.")
        _CURL_ADAPTER = False
    return _CURL_ADAPTER


def install_transport(cl, impersonate: str = "chrome136") -> bool:
    """Mount a TLS-impersonating adapter on ``cl.private``.

    Keeps the session a ``requests.Session`` (so instagrapi's cookie-jar,
    proxy and verify plumbing is untouched) while every HTTPS request through
    it speaks the requested Chrome TLS fingerprint instead of Python's.

    ALSO patches ``_configure_private_session_retry`` once per process so that
    every later ``init()`` / ``load_settings`` / ``login_by_sessionid`` (which
    re-mount the stock HTTPAdapter over our adapter) re-applies the curl
    transport automatically instead of silently downgrading.

    Returns True when the swap happened, False when it degraded to the stock
    requests adapter.
    """
    _patch_private_retry(impersonate)
    # Every client build passes through here — the natural once-per-process
    # hook for the dead-session log-noise filter.
    _install_login_noise_filter()
    # A challenge must NEVER trigger instagrapi's automatic /challenge/ calls
    # (operator rule: stop, notify once, wait for a fresh jar).
    _install_challenge_guard()

    adapter_cls = _load_curl_adapter()
    if not adapter_cls:
        return False
    try:
        adapter = adapter_cls(impersonate_browser_type=impersonate)
        # CurlCffiAdapter is a plain BaseAdapter (not HTTPAdapter), so it has
        # no urllib3 Retry; instagrapi's own app-level retry loop
        # (ClientRequestTimeout / ClientIncompleteReadError) still covers the
        # common transient failures.
        cl.private.mount("https://", adapter)
        cl.private.mount("http://", adapter)
        logger.info(f"[IG anti-detect] private transport now impersonates {impersonate}")
        return True
    except Exception as e:
        logger.warning(f"[IG anti-detect] transport install failed ({e}); "
                       f"staying on plain requests.")
        return False


_CHALLENGE_GUARDED = False
_DEFAULT_PUBLIC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)


def _install_challenge_guard() -> None:
    """Make ``challenge_resolve`` raise instead of firing /challenge/ requests.

    instagrapi's ``private_request`` catches ``ChallengeRequired`` and calls
    ``challenge_resolve`` automatically. On a flagged account that is exactly
    the retry storm the operator forbade: every extra /challenge/ call deepens
    enforcement. We replace it once per process so the exception surfaces and
    the worker can arm the session-dead latch.
    """
    global _CHALLENGE_GUARDED
    if _CHALLENGE_GUARDED:
        return
    try:
        import importlib
        from instagrapi.exceptions import ChallengeRequired

        # 2.x: ChallengeMixin; 3.x: ChallengeResolveMixin. Support both so an
        # upgrade cannot silently re-enable automatic /challenge/ requests.
        mod = importlib.import_module("instagrapi.mixins.challenge")
        mixin = (getattr(mod, "ChallengeMixin", None)
                 or getattr(mod, "ChallengeResolveMixin", None))
        if mixin is None:
            raise RuntimeError("no challenge mixin found")

        def _no_auto_resolve(self, last_json):
            ctx = dict(last_json or {})
            ctx["message"] = (
                "Instagram challenge — automatic resolution is disabled by "
                "operator policy. Complete it manually in the official app; "
                "no /challenge/ requests are made.")
            raise ChallengeRequired(**ctx)

        mixin.challenge_resolve = _no_auto_resolve
        _CHALLENGE_GUARDED = True
        logger.info(f"[IG anti-detect] challenge auto-resolution disabled "
                    f"({mixin.__name__})")
    except Exception as e:
        logger.warning(f"[IG anti-detect] challenge guard install failed: {e}")


def apply_identity(cl) -> None:
    """Re-apply the resolved geo + a coherent public web identity.

    MUST run AFTER every ``load_settings()`` / login: instagrapi's ``init()``
    restores ``country``/``locale``/``timezone`` from the session file,
    silently overwriting a pre-login ``pin_geo``. The persisted file on this
    box still carries US/en_US/GMT-4 from before auto-geo, so sending that from
    a CZ IP is exactly the mismatch this fixes. It also aligns the public web
    UA/transport with the private session (instagrapi 2.x otherwise sends a
    2017 desktop Safari UA on public calls — a second identity).
    """
    try:
        from utils import ig_geo
        geo = ig_geo.resolve()
        # Stamp the SETTINGS dict too: login_by_sessionid()/load_settings()
        # call init(), which restores country/locale/timezone/public_transport
        # from settings. Updating only the attributes is silently undone.
        if isinstance(getattr(cl, "settings", None), dict):
            cl.settings.update({
                "country": geo["country"],
                "country_code": geo["country_code"],
                "locale": geo["locale"],
                "timezone_offset": geo["timezone_offset"],
                "timezone_name": geo["timezone_name"],
                "public_transport": "curl",
                "public_transport_impersonate": "chrome136",
            })
        pin_geo(
            cl,
            country=geo["country"],
            country_code=geo["country_code"],
            locale=geo["locale"],
            timezone_offset=geo["timezone_offset"],
            timezone_name=geo["timezone_name"],
        )
    except Exception as e:
        logger.warning(f"[IG anti-detect] geo re-apply failed: {e}")
    try:
        cl.set_retry_config(public_transport="curl",
                            public_transport_impersonate="chrome136")
        # Set the public UA AFTER set_retry_config (which can reset the public
        # session). 3.x has public_graphql_user_agent too; keep it coherent.
        ua = getattr(config, "IG_PUBLIC_USER_AGENT", "") or _DEFAULT_PUBLIC_UA
        cl.public_user_agent = ua
        if hasattr(cl, "public_graphql_user_agent"):
            cl.public_graphql_user_agent = ua
    except Exception as e:
        logger.warning(f"[IG anti-detect] public identity alignment failed: {e}")
    try:
        # Present the library's current app build, not the one frozen into an
        # old session file (2.18.14 only knows up to 428; 3.x defaults to 449).
        from instagrapi.config import DEFAULT_APP_VERSION
        cl.set_app(DEFAULT_APP_VERSION)
        # 3.x set_app() updates device_settings but NOT the cached user_agent;
        # rebuild it so the wire UA matches the app build.
        try:
            cl.set_user_agent()
        except Exception:
            pass
    except Exception as e:
        logger.info(f"[IG anti-detect] app-version alignment skipped: {e}")


def _patch_private_retry(impersonate: str) -> None:
    """Once per process, make every future ``_configure_private_session_retry``
    call mount the curl transport (instagrapi calls it from ``__init__`` AND
    from ``set_retry_config`` on every ``init()`` — i.e. every
    ``load_settings`` / ``login_by_sessionid`` — which would otherwise wipe our
    adapter and silently revert the session to plain-requests TLS)."""
    global _RETRY_PATCHED, _DEFAULT_IMPERSONATE
    if _RETRY_PATCHED:
        return
    try:
        from instagrapi.mixins.private import PrivateRequestMixin
    except Exception:
        return
    adapter_cls = _load_curl_adapter()
    if not adapter_cls:
        return
    _DEFAULT_IMPERSONATE = impersonate
    orig_configure = PrivateRequestMixin._configure_private_session_retry

    def _patched(self, private_transport=None, *args, **kwargs):
        # 2.x calls this with no args; 3.x passes private_transport=...
        # (Literal["requests","curl"]). We always mount the curl adapter for
        # the private session, so the argument is accepted and ignored.
        try:
            adapter = adapter_cls(impersonate_browser_type=_DEFAULT_IMPERSONATE)
            self.private.mount("https://", adapter)
            self.private.mount("http://", adapter)
        except Exception as e:
            logger.warning(f"[IG anti-detect] _configure_private_session_retry "
                           f"transport fallback: {e}")
            try:
                return orig_configure(self, private_transport, *args, **kwargs)
            except TypeError:
                return orig_configure(self)

    PrivateRequestMixin._configure_private_session_retry = _patched
    _RETRY_PATCHED = True
    logger.info("[IG anti-detect] _configure_private_session_retry now re-applies "
                "the curl transport on every init()/login")


def _patch_base_headers():
    """Once per process, make every IGClient's ``base_headers`` re-apply any
    echo-token values captured into settings/attrs. Idempotent via a class
    flag; safe against recursion because it wraps the original fget once."""
    global _BASE_HEADERS_PATCHED
    if _BASE_HEADERS_PATCHED:
        return
    try:
        from instagrapi.mixins.private import PrivateRequestMixin
    except Exception:
        return

    orig_fget = PrivateRequestMixin.base_headers.fget

    def _patched(self):
        headers = dict(orig_fget(self))
        settings = getattr(self, "settings", None) or {}
        for attr, hdr in _ECHO_APPLY.items():
            value = getattr(self, attr, None) or settings.get(attr)
            if value:
                headers[hdr] = value
        return headers

    PrivateRequestMixin.base_headers = property(_patched)
    _BASE_HEADERS_PATCHED = True
    logger.info("[IG anti-detect] base_headers now re-applies captured echo tokens")


def _patch_get_settings():
    """Once per process, make ``get_settings`` include the echo tokens
    instagrapi doesn't natively serialize (ig_u_shbid / ig_u_shbts), so
    ``dump_settings`` round-trips them instead of dropping them."""
    global _GET_SETTINGS_PATCHED
    if _GET_SETTINGS_PATCHED:
        return
    try:
        from instagrapi.mixins.auth import LoginMixin
    except Exception:
        return

    orig_get_settings = LoginMixin.get_settings

    def _patched(self):
        settings = orig_get_settings(self)
        for attr in ("ig_u_shbid", "ig_u_shbts"):
            value = getattr(self, attr, None)
            if value:
                settings[attr] = value
        return settings

    LoginMixin.get_settings = _patched
    _GET_SETTINGS_PATCHED = True
    logger.info("[IG anti-detect] get_settings now serializes shbid/shbts")


def install_token_echo(cl) -> None:
    """Capture IG's echo headers from every private response and persist them.

    Wraps ``cl.private_request`` so each call, success or failure, captures the
    server-set echo tokens into ``cl.settings`` (durable via the existing
    per-poll ``dump_settings``) and into ``cl`` attrs. Purely additive — if the
    server never sends a token, instagrapi's synthetic value stays.
    """
    _patch_base_headers()
    _patch_get_settings()

    if getattr(cl, "_tgbot_echo_hooked", False):
        return

    orig_request = cl.private_request

    def _capture(self):
        resp = getattr(self, "last_response", None)
        if resp is None:
            return
        headers = getattr(resp, "headers", {}) or {}
        for resp_key, attr in _ECHO_HEADER_MAP.items():
            value = headers.get(resp_key)
            if not value:
                continue
            try:
                setattr(self, attr, value)
                self.settings[attr] = value
            except Exception:
                pass

    def _patched_private_request(self, *args, **kwargs):
        # NOTE: orig_request is already bound to cl (we captured it via
        # cl.private_request), so do NOT pass `self` again here — that would
        # shift the endpoint arg and blow up in _send_private_request.
        try:
            result = orig_request(*args, **kwargs)
            _capture(self)
            return result
        except Exception:
            _capture(self)
            raise

    cl.private_request = _patched_private_request.__get__(cl, type(cl))
    cl._tgbot_echo_hooked = True
    logger.info("[IG anti-detect] echo-token capture + persistence installed")


def pin_geo(cl, country: str = "US", country_code: int = 1,
            locale: str = "en_US", timezone_offset: int = -14400,
            timezone_name: str = "GMT-04:00") -> None:
    """Pin the account's home region so the session never drifts across
    locales/timezones (a strong identity-correlation signal)."""
    try:
        cl.set_country(country)
    except Exception as e:
        logger.warning(f"[IG anti-detect] set_country failed: {e}")
    try:
        cl.set_country_code(country_code)
    except Exception as e:
        logger.warning(f"[IG anti-detect] set_country_code failed: {e}")
    try:
        cl.set_locale(locale)
    except Exception as e:
        logger.warning(f"[IG anti-detect] set_locale failed: {e}")
    try:
        cl.set_timezone_offset(timezone_offset, timezone_name)
    except Exception as e:
        logger.warning(f"[IG anti-detect] set_timezone_offset failed: {e}")
    logger.info(f"[IG anti-detect] geo pinned to {country} / {locale} / "
                f"{timezone_name} ({timezone_offset})")


def warmup(cl, steps: int = 1) -> None:
    """Paced, benign read-only calls right after login.

    The first real inbox poll is the account's first visible activity on a
    fresh IP (and, after the checkpoint, on a fresh session) — warming up with
    a couple of ordinary reads and human pacing makes the transition look like
    an app open, not a bot starting a job.

    Default is ONE step (2 calls): the cold-start jitter that follows already
    adds three more paced reads. Every restart used to burn 6 warmup calls on
    top of those; API volume is what Instagram's automation model watches.
    """
    def _do(label, fn):
        try:
            fn()
            logger.info(f"[IG anti-detect] warmup: {label} ok")
        except Exception as e:
            logger.warning(f"[IG anti-detect] warmup: {label} skipped ({e})")
        # Human pacing between warmup calls, same spirit as delay_range.
        time.sleep(random.uniform(2, 5))

    for _ in range(max(1, min(steps, 6))):
        _do("account_info", lambda: cl.account_info())
        _do("direct_threads(5)", lambda: cl.direct_threads(amount=5))


def write_back_session(cl, jar_path: str) -> None:
    """Persist the LIVE session tokens Instagram just re-issued back into the
    Netscape jar, so the shared ``igcookies.txt`` (used by yt-dlp AND the DM
    workers) stays warm between browser refreshes.

    instagrapi stores every ``Set-Cookie`` rotation in ``cl.private.cookies``
    but never writes them back to the jar — so a session Instagram rotates
    during a private-API login was silently discarded, and the jar's
    ``sessionid`` went stale within hours (surfacing as the "Exceeded 30
    redirects" login-wall). This overlays the fresh ``sessionid`` /
    ``csrftoken`` / ``ds_user_id`` / ``mid`` / ``rur`` values into the jar via
    cookie_manager's atomic overlay — pure additive, never deletes, preserves
    the 0o444 lock, refuses an empty jar. No-op on any failure.

    NOTE: do NOT overlay the shbid/shbts pair or other echo tokens — those are
    per-transport anti-bot markers, not session cookies; the jar only needs the
    session identity for yt-dlp to reuse.
    """
    if not jar_path or not os.path.exists(jar_path):
        return
    try:
        cookies = getattr(cl.private, "cookies", None)
        updates: dict[tuple[str, str], str] = {}
        for name in ("sessionid", "csrftoken", "ds_user_id", "mid", "rur"):
            val = None
            if cookies is not None:
                try:
                    val = cookies.get(name)
                except Exception:
                    # Instagram sets e.g. rur on more than one domain/path;
                    # RequestsCookieJar.get raises CookieConflict there. Pick
                    # the instagram.com cookie explicitly instead.
                    for c in cookies:
                        if c.name == name and "instagram.com" in (c.domain or ""):
                            val = c.value
                            break
            if val:
                updates[(".instagram.com", name)] = str(val)
        if not updates:
            return
        from utils import cookie_manager
        changed = cookie_manager.overlay_cookies(jar_path, updates,
                                                 actor="instagrapi_writeback")
        if changed:
            logger.info(f"[IG anti-detect] wrote back {changed} live session "
                        f"cookie(s) to {jar_path}")
    except Exception as e:
        logger.warning(f"[IG anti-detect] session write-back failed: {e}")


# ---------------------------------------------------------------------------
# Burst pacing — the cumulative activity of a backfill is the signal that
# triggers "we suspect automated behavior". A 30-item backfill at a fixed
# 4-6 s cadence looks like a scraper; the same 30 items spread over 5-7
# minutes looks like a human re-reading messages. The fix is per-item
# pacing that scales with the burst size.
# ---------------------------------------------------------------------------

def burst_pace(n_items: int, delivered_in_run: int = 0) -> float:
    """Per-item sleep to apply between relayed items in a backfill.

    Returns the number of seconds to wait before processing the NEXT item.
    The formula is:
      base   = 6 + log2(n_items + 1) * 2    # grows with burst size
      jitter = uniform(-1.5, +2.5)            # human variance
    Capped at 30 s/item so a 1000-item backfill doesn't take days.
    Below 3 items (the normal live-polling case) the per-item sleep
    is < 8 s, so the live path feels unchanged.

    ``delivered_in_run`` is the media items already relayed by THIS worker
    run (not this poll batch). If it reaches
    ``IG_DIRECT_BACKFILL_MAX_ITEMS``, return -1 so the caller can stop
    cleanly: a >10h backlog killed the account once, and a cap that
    resumes at the normal poll cadence turns a scrape-shaped burst into
    hours of small human-sized read sessions. The cursor stays behind the
    remaining backlog, so the next successful poll starts one batch later.

    Called from the IG worker between gap-fetch items, not from the
    live-poll case (1 item per cycle), so this is a backfill-specific
    lever and doesn't throttle the idle case.
    """
    if n_items <= 0:
        return 0.0
    max_items = int(getattr(config, "IG_DIRECT_BACKFILL_MAX_ITEMS", 150) or 150)
    if delivered_in_run >= max_items:
        logger.warning(
            "[IG anti-detect] backfill safety cap reached after %d delivered item(s); "
            "stopping this poll batch — the cursor resumes at the normal poll cadence.",
            delivered_in_run)
        return -1.0
    base = 6.0 + (math.log2(n_items + 1) * 2.0)
    jitter = random.uniform(-1.5, 2.5)
    return max(0.0, min(30.0, base + jitter))


# ---------------------------------------------------------------------------
# Cold-start jitter — Instagram's "first activity" pattern detector.
# When the session's very first request is the paired-thread inbox poll,
# that sequence is structurally bot-like. After login (or session resume),
# do a few random reads on non-paired threads first, with realistic timing.
# ---------------------------------------------------------------------------

async def cold_start_jitter(cl) -> None:
    """Simulate a human "opens app, glances around" sequence right after
    login. Each step is a benign read with a long, randomized pause.

    Unlike ``warmup`` (which is a single short check just to prove the
    session is alive), this runs for a couple of minutes with multiple
    cross-thread reads so the first paired-thread poll isn't the
    session's first observable activity.
    """
    import asyncio as _asyncio
    loop = _asyncio.get_event_loop()

    def _read(label, fn):
        try:
            fn()
            logger.info(f"[IG anti-detect] cold-start: {label} ok")
        except Exception as e:
            logger.info(f"[IG anti-detect] cold-start: {label} skipped ({e})")

    # Two short reads spaced far apart. 60-90s between them gives the
    # risk model enough time to bucket this as "human-ish" rather
    # than "right-after-login burst".
    _read("account_info", lambda: cl.account_info())
    await _asyncio.sleep(random.uniform(60, 90))
    _read("direct_threads(20)", lambda: cl.direct_threads(amount=20))
    await _asyncio.sleep(random.uniform(45, 90))
    # One more pass; if the paired thread is in the top 20, fine, we
    # already saw it. If not, we just keep the cadence random.
    _read("direct_threads(20)", lambda: cl.direct_threads(amount=20))


# ---------------------------------------------------------------------------
# Public-web soft-block counter. instagrapi's public client (used by
# media_pk_from_url, etc.) hits api/graphql which returns HTML when
# Instagram applies a soft block. Counting these prevents the bot from
# hammering a throttled endpoint.
# ---------------------------------------------------------------------------

_public_soft_block_streak: int = 0
_PUBLIC_SOFT_BLOCK_THRESHOLD = 3    # 3 consecutive JSONDecodeErrors → back off
_PUBLIC_SOFT_BLOCK_COOLDOWN = 600   # 10 minutes before retrying the public path


def record_public_soft_block() -> bool:
    """Record a public-web soft-block event. Returns True when the streak
    has crossed the threshold and the public path should be skipped for a
    cooldown window.
    """
    global _public_soft_block_streak
    _public_soft_block_streak += 1
    if _public_soft_block_streak == _PUBLIC_SOFT_BLOCK_THRESHOLD:
        logger.warning(
            f"[IG anti-detect] {PUBLIC_SOFT_BLOCK_STREAK_LABEL} public-web soft "
            f"blocks in a row — skipping public endpoints for "
            f"{_PUBLIC_SOFT_BLOCK_COOLDOWN}s. (Reels that need media_pk_from_url "
            f"will fall back to the URL; photo posts will use the next-best "
            f"CDN URL.)"
        )
    return _public_soft_block_streak >= _PUBLIC_SOFT_BLOCK_THRESHOLD


def record_public_success() -> None:
    """Reset the soft-block counter on a successful public-web call."""
    global _public_soft_block_streak
    if _public_soft_block_streak:
        logger.info(
            f"[IG anti-detect] public-web recovered after "
            f"{_public_soft_block_streak} soft-block event(s)."
        )
    _public_soft_block_streak = 0


def public_soft_block_active() -> bool:
    """True when the public-web path is in the cooldown window. Callers
    should skip the public call (use a fallback or skip the item)."""
    return _public_soft_block_streak >= _PUBLIC_SOFT_BLOCK_THRESHOLD


# Backwards-compat name (an early version used PUBLIC_SOFT_BLOCK_STREAK_LABEL
# in the warning template). Keep it as a constant so the message renders.
PUBLIC_SOFT_BLOCK_STREAK_LABEL = "3+"


# ---------------------------------------------------------------------------
# Email-change handler — when Instagram forces the user to change email
# (the "we suspect your account was compromised" path), instagrapi's
# change_password_handler is invoked. We do NOT attempt to bypass
# (changing the password programmatically would deepen the flag); we
# just alert the operator and let the exception propagate so the worker
# freezes per the existing challenge policy.
# ---------------------------------------------------------------------------

def install_email_change_alert(cl, alert_sink=None) -> None:
    """Register a no-op ``change_password_handler`` that detects the
    Instagram-forced-email-change flow (signalled by the handler being
    invoked), alerts the operator, and re-raises so the worker freezes.

    ``alert_sink`` is an optional async callable that takes a single
    ``str`` (the alert text) and sends it somewhere the operator will see
    it — typically ``bot.send_message(SYSTEM_CREATOR_ID, text)``. The
    worker passes its own bot client + chat id here at startup. When
    ``alert_sink`` is None, the handler only logs at ERROR.
    """
    if getattr(cl, "_tgbot_email_handler_hooked", False):
        return

    def _alert_handler(username):
        msg = (
            f"⚠️ **Instagram is forcing an email change on the bot account "
            f"({username})!**\n\n"
            f"Instagram's fraud team believes the account may be compromised. "
            f"This usually means:\n"
            f"  • A login from a new device, IP, or country\n"
            f"  • A sessionid that doesn't match the owner's known device\n"
            f"  • A high request volume from a single IP in a short window\n\n"
            f"The IG direct-forward worker is pausing. **Do NOT let the bot "
            f"try to bypass this** (a programmatic password change deepens "
            f"the flag). The owner should:\n"
            f"  1. Open Instagram in the official app on the trusted phone\n"
            f"  2. Confirm the security nudge, then change the email from a "
            f"trusted device\n"
            f"  3. Restart the bot after a 3-5 hour cooldown\n\n"
            f"While frozen, the IG worker is paused. X (twikit) and the "
            f"downloader/stream/yt-dlp paths are unaffected."
        )
        logger.error(f"[IG anti-detect] Instagram forced email change on "
                     f"{username}; worker freezing. {msg}")
        if alert_sink is not None:
            try:
                loop_holder = {}

                import asyncio as _asyncio
                try:
                    loop = _asyncio.get_event_loop()
                except RuntimeError:
                    loop = None
                if loop is not None and loop.is_running():
                    _asyncio.ensure_future(alert_sink(msg), loop=loop)
                else:
                    logger.warning("[IG anti-detect] alert_sink provided but no "
                                   "running event loop; skipping async alert.")
            except Exception as e:
                logger.warning(f"[IG anti-detect] email-change alert sink failed: {e}")
        # Raise so the worker enters its existing 3-5h freeze path. We
        # do NOT return a new password — the human must do it in the
        # official app.
        raise RuntimeError(
            f"Instagram forced email change on {username}. Operator must "
            f"resolve in the official app; worker pausing."
        )

    cl.change_password_handler = _alert_handler
    cl._tgbot_email_handler_hooked = True
    logger.info("[IG anti-detect] change_password_handler installed — "
                "email-change flow freezes the worker instead of bypassing")

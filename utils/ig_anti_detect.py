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

Every entry point is a no-op (or a logged degradation) when its dependency is
missing, so this file can never take the worker down.
"""

import logging
import random
import time

logger = logging.getLogger(__name__)

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

    def _patched(self):
        try:
            adapter = adapter_cls(impersonate_browser_type=_DEFAULT_IMPERSONATE)
            self.private.mount("https://", adapter)
            self.private.mount("http://", adapter)
        except Exception as e:
            logger.warning(f"[IG anti-detect] _configure_private_session_retry "
                           f"transport fallback: {e}")
            orig_configure(self)

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


def warmup(cl, steps: int = 3) -> None:
    """Paced, benign read-only calls right after login.

    The first real inbox poll is the account's first visible activity on a
    fresh IP (and, after the checkpoint, on a fresh session) — warming up with
    a couple of ordinary reads and human pacing makes the transition look like
    an app open, not a bot starting a job.
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

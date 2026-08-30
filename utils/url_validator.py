# utils/url_validator.py
"""Tiny, transport-neutral URL predicates shared by every frontend.

Kept deliberately free of pyrogram/aiogram imports so both the Telegram
(pyrogram) and Bale (aiogram) frontends can use it without a cross-frontend
dependency. Previously ``is_link`` was duplicated verbatim in
``modules/downloader_handler.py`` and ``modules/bale/runner.py``.
"""


def is_link(text: str) -> bool:
    """True when *text* looks like an http(s) URL.

    Only ``http://`` and ``https://`` count as links for routing purposes —
    ``file://``, ``ftp://`` and friends are treated as plain text and are never
    fetched (the direct-file path has an SSRF guard that also refuses
    loopback/private destinations).
    """
    return text.startswith("http://") or text.startswith("https://")

# utils/url_validator.py
"""Tiny, transport-neutral URL predicates used by the downloader flow.

Kept deliberately free of pyrogram imports so the predicate has no UI-layer
dependency. Previously ``is_link`` was duplicated verbatim in
``modules/downloader_handler.py``.
"""


def is_link(text: str) -> bool:
    """True when *text* looks like an http(s) URL.

    Only ``http://`` and ``https://`` count as links for routing purposes —
    ``file://``, ``ftp://`` and friends are treated as plain text and are never
    fetched (the direct-file path has an SSRF guard that also refuses
    loopback/private destinations).
    """
    return text.startswith("http://") or text.startswith("https://")

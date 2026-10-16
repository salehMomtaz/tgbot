"""Single source of truth for turning untrusted text into a safe filename.

Every place in the bot that derives an **on-disk** name (or a zip arcname) from
remote or user-supplied text must route through :func:`sanitize_filename`.
The reasons, in historical order of discovery:

* **Errno 36 (the reported bug).** ``open()`` does not bound a path component,
  the kernel does: Linux ``NAME_MAX`` is **255 bytes** (not characters). A
  title that is a wall of emoji (4 UTF-8 bytes each) sails through yt-dlp's
  character sanitizer and then dies writing ``<title>.jpg`` with ``OSError
  [Errno 36] File name too long`` — which the direct-forward relay classifies
  as a permanent failure (3-strike skip + cursor advance), so the message is
  silently dropped. The fix is a *byte* budget, applied to the whole component.
* **Path traversal.** A URL path basename or a user-supplied display name can
  contain ``/``, ``..``, ``C:\\`` or NUL. Joining it onto ``cache/<id>/`` must
  never escape the task dir (``strip_dirs=True`` keeps only the final
  component; separators that survive a flatten are replaced).
* **Portability.** Names cross filesystems: a jar/zip extracted on Windows
  cannot hold ``< > : " | ? *``, a trailing dot/space, or a device name
  (``CON``/``NUL``/``COM1``…). We sanitize with the union of the Linux and
  Windows rules so one set of bytes works everywhere.
* **Collisions.** Truncation alone makes two long names that share a prefix
  identical. A short **stable** digest of the original input is appended
  whenever we truncate, so distinct inputs keep distinct names while the same
  input keeps the same name across runs/processes.

Two modes:

* default (``ascii_only=False``) — Unicode-preserving: only characters that are
  actually unsafe are replaced (controls, Windows-invalid, format/bidi
  overrides), whitespace is collapsed to single spaces. Emoji, Persian/Arabic
  and CJK titles survive intact.
* ``ascii_only=True`` — strict/legacy: everything outside ``[A-Za-z0-9._-]``
  becomes ``_`` (spaces included). This is the historical
  ``utils/security.safe_task_filename`` and Bale behaviour, kept byte-for-byte
  compatible for the callers that want a bulletproof ASCII component.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

# A whole component (stem + extension) is capped well below Linux's 255-byte
# NAME_MAX / Windows' 255-char limit. The margin absorbs the suffixes the bot
# itself appends to an already-safe name:
#   * ``<stem>_thumb.jpg``            (frame-thumb sibling, +10 B)
#   * ``<stem>.part001.mp4``          (splitter, +12 B)
#   * ``<stem>.part`` / ``<stem>.f137.mp4`` (yt-dlp intermediates, +9 B)
# The ffmpeg metadata temp file is ``tempfile.mkstemp`` (random, unaffected).
MAX_COMPONENT_BYTES = 200

# Contribution of a yt-dlp title to an on-disk name. Kept separate (and smaller)
# because yt-dlp appends ``.fNNN``/``.part`` intermediates and we append a
# ``_thumb.jpg`` sibling; 150 bytes guarantees the final component stays well
# under 255 for every template type. See ``utils/downloader/download.py``.
MAX_TITLE_BYTES = 150

# Legacy cap used by ``utils/security.safe_task_filename`` (kept as a named
# constant so the documented invariant stays greppable).
MAX_LEGACY_TASK_BYTES = 150

_REPLACEMENT = "_"

# Illegal in a filename on Windows (a strict superset of Linux's ``/`` + NUL).
# Also flattens any separator that survived and every C0 control + DEL.
_ILLEGAL_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')

# Strict-mode allowlist: everything else becomes "_".
_NON_ASCII_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")

_WS_RE = re.compile(r"\s+")
_UNDERSCORE_RUN_RE = re.compile(r"_{2,}")
_EXT_RE = re.compile(r"[A-Za-z0-9]{1,12}")
_MAX_EXT_CHARS = 12

# Windows reserves these device names in every directory, with or without an
# extension (``CON``, ``con.txt``). A file so named cannot be opened.
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(10)}
    | {f"LPT{i}" for i in range(10)}
)

# Unicode general-category groups we drop outright: Cc (controls), Cs
# (surrogates), Co (private use). Cf (format) is dropped EXCEPT the two
# zero-width joiners, which Persian/Arabic text (ZWNJ) and emoji ZWJ sequences
# (e.g. the 🏳️‍🌈 flag: 🏳 + ZWJ + 🌈) legitimately need. Dropping them would
# corrupt the very titles this module exists to preserve.
_KEEP_FORMAT_CHARS = frozenset({"\u200c", "\u200d"})  # ZWNJ, ZWJ


def _is_forbidden_char(ch: str) -> bool:
    cat = unicodedata.category(ch)
    if cat in ("Cc", "Cs", "Co"):
        return True
    if cat == "Cf":
        return ch not in _KEEP_FORMAT_CHARS
    return False


def truncate_bytes(text: str, max_bytes: int) -> str:
    """Truncate *text* so its UTF-8 encoding is ≤ ``max_bytes``.

    Cuts on a **character** boundary: the encoded value is sliced by bytes and
    any partial trailing code point is dropped (so a 4-byte emoji is either
    fully kept or fully removed, never mangled into a lone surrogate or an
    ``\ufffd``). ``max_bytes <= 0`` yields the empty string.
    """
    if max_bytes <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    sliced = encoded[:max_bytes]
    while sliced:
        try:
            return sliced.decode("utf-8")
        except UnicodeDecodeError as exc:
            # ``exc.start`` is where the incomplete trailing sequence begins —
            # dropping it removes only the partial code point.
            sliced = sliced[: exc.start]
    return ""


def _short_hash(value: str, hash_len: int) -> str:
    """Stable, filesystem-safe digest of *value* (blake2b, not Python's salted
    ``hash()``, so the name is reproducible across processes and restarts)."""
    digest_size = max(2, min(32, (max(2, hash_len) + 1) // 2))
    return hashlib.blake2b(
        value.encode("utf-8", "surrogatepass"), digest_size=digest_size
    ).hexdigest()[:hash_len]


def _normalize_ext(ext: str | None) -> str:
    if not ext:
        return ""
    cleaned = str(ext).strip().strip(".")
    # Keep only the last component if callers pass a dotted compound.
    cleaned = cleaned.rsplit(".", 1)[-1]
    cleaned = re.sub(r"[^A-Za-z0-9]", "", cleaned)[:12]
    return f".{cleaned}" if cleaned else ""


def _clean_component(text: str, *, ascii_only: bool) -> str:
    """The shared character pipeline: NFKC → whitespace → forbidden → illegal →
    (ascii) → collapse separators → trim whitespace.

    Deliberately does NOT strip leading/trailing ``.``/``_``: the extension is
    split off the caller afterwards, and stripping here would eat the dot of a
    name like ``"😀.txt"`` in strict mode (leaving a useless stem of ``"txt"``).
    """
    text = unicodedata.normalize("NFKC", text)
    # Whitespace (incl. \n \t and Unicode spaces NFKC leaves behind) collapses
    # to a single space BEFORE the control strip, so a newline becomes " " and
    # not "_".
    text = _WS_RE.sub(" ", text)
    if any(_is_forbidden_char(ch) for ch in text):
        text = "".join(
            _REPLACEMENT if _is_forbidden_char(ch) else ch for ch in text
        )
    text = _ILLEGAL_CHARS_RE.sub(_REPLACEMENT, text)
    if ascii_only:
        text = _NON_ASCII_SAFE_RE.sub(_REPLACEMENT, text)
    text = _UNDERSCORE_RUN_RE.sub(_REPLACEMENT, text)
    return text.strip()


def _sanitize_fallback(fallback: str | None, *, ascii_only: bool) -> str:
    cleaned = _clean_component(str(fallback) if fallback else "", ascii_only=ascii_only)
    # An id-based fallback never legitimately carries an extension; drop any
    # dangling separators so it can be used as a bare stem.
    return cleaned.strip(" ._") or "file"


def sanitize_filename(
    value: str | None,
    *,
    fallback: str = "file",
    ext: str = "",
    max_bytes: int = MAX_COMPONENT_BYTES,
    ascii_only: bool = False,
    strip_dirs: bool = True,
    keep_extension: bool = True,
    hash_len: int = 8,
) -> str:
    """Return a filesystem-safe single path component derived from *value*.

    Args:
        value: the untrusted text (title, URL basename, user-supplied name…).
        fallback: an **id-based** name used when nothing usable survives
            (callers should pass something like ``f"download_{cache_id}"``).
        ext: force this extension onto the result (``".mp4"`` or ``"mp4"``).
            When omitted, a trailing ``.ext`` in *value* is preserved iff
            ``keep_extension`` and it looks like an extension.
        max_bytes: byte budget for the **whole** returned component.
        ascii_only: strict allowlist mode (see the module docstring).
        strip_dirs: keep only the final path component (default). Set ``False``
            to flatten separators into ``_`` instead — used when composing a
            stem from values that legitimately contain ``/`` (branch names).
        keep_extension: preserve a trailing extension found in *value*.
        hash_len: length of the hex digest appended when truncating.

    The result never contains a path separator, never ends in ``.``/space,
    is never a Windows device name, and its UTF-8 length is ≤ ``max_bytes``
    (or the truncated-stem + digest + extension fits the budget).
    """
    original = str(value) if value is not None else ""
    forced_ext = _normalize_ext(ext)

    text = unicodedata.normalize("NFKC", original)
    if strip_dirs:
        # Windows accepts both separators; normalize then keep the last piece
        # (also defeats "C:\\Windows\\x" and "/etc/passwd").
        text = text.replace("\\", "/").rsplit("/", 1)[-1]

    text = _clean_component(text, ascii_only=ascii_only)

    # Split off the extension. A forced extension wins; otherwise preserve a
    # trailing extension only when it looks like one (short + alphanumeric).
    stem, suffix = text, ""
    if forced_ext:
        if text.lower().endswith(forced_ext.lower()):
            stem, suffix = text[: -len(forced_ext)], forced_ext
        else:
            # A different trailing extension (or none) — drop it so we do not
            # produce "clip.webm.mp4".
            head, dot, tail = text.rpartition(".")
            stem = head if (dot and head and _EXT_RE.fullmatch(tail)) else text
            suffix = forced_ext
    elif keep_extension:
        head, dot, tail = text.rpartition(".")
        if dot and head and _EXT_RE.fullmatch(tail):
            stem, suffix = head, f".{tail}"

    stem = stem.strip().strip(" ._")

    # Windows device names are unopenable under every extension.
    if stem and stem.upper() in _WINDOWS_RESERVED:
        stem = f"{_REPLACEMENT}{stem}"

    if not stem:
        stem = _sanitize_fallback(fallback, ascii_only=ascii_only)

    if len(f"{stem}{suffix}".encode("utf-8")) <= max_bytes:
        return f"{stem}{suffix}"

    # Over budget → truncate the stem on a character boundary and append a
    # stable digest so distinct long inputs cannot collide. The extension is
    # always preserved: without it Telegram/ffmpeg misclassify the file.
    digest = _short_hash(original or text, hash_len)
    tag = f"-{digest}"
    budget = max_bytes - len(tag.encode("utf-8")) - len(suffix.encode("utf-8"))
    if budget < 1:
        # Pathological budget (tiny max_bytes vs. long ext): fall back to the
        # bare digest, which is already ASCII and safe.
        return truncate_bytes(digest, max(1, max_bytes)) or "file"

    stem = truncate_bytes(stem, budget).rstrip("\u200c\u200d").strip().strip(" ._")
    if not stem:
        stem = truncate_bytes(
            _sanitize_fallback(fallback, ascii_only=ascii_only), budget
        ).strip().strip(" ._") or truncate_bytes(digest, budget)
    return f"{stem}{tag}{suffix}"

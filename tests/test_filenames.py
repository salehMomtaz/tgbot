"""Regression tests for :mod:`utils.filenames`.

The project has no pytest dependency; this file is a plain script that can be
run directly (``venv/bin/python tests/test_filenames.py``) and is also
pytest-compatible if pytest is ever installed. It intentionally exercises the
*reported* bug (a wall-of-emoji tweet title overflowing Linux's 255-byte
NAME_MAX → ``OSError [Errno 36]``) plus the traversal / portability / collision
guarantees the sanitizer promises.
"""

from __future__ import annotations

import os
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.filenames import (  # noqa: E402
    MAX_COMPONENT_BYTES,
    MAX_LEGACY_TASK_BYTES,
    MAX_TITLE_BYTES,
    sanitize_filename,
    truncate_bytes,
)

# The exact title from the 2026-10-09 production failure (names/logs stripped
# of the tweet id): 328 UTF-8 bytes because every emoji is 4 bytes plus ZWJ
# (🏳️‍🌈) and keycap (6️⃣) sequences inflate it further.
BUG_TITLE = (
    "GAYROCK2026 🌋🌈🏳️\u200d🌈(6️⃣0️⃣K) 👬😀🎵🎶 - "
    + "🌈" * 13
    + " "
    + "🏳️\u200d🌈" * 14
    + " 🌋🌋....jpg"
)
ZERO_WIDTH_JOINER = "\u200d"
ZWJ_FLAG = "🏳️\u200d🌈"


def _bytes(name: str) -> int:
    return len(name.encode("utf-8"))


def test_reported_bug_title_is_byte_bounded():
    assert _bytes(BUG_TITLE) > 255  # the input really is the overflow case
    name = sanitize_filename(BUG_TITLE, fallback="x_photo_1", ext=".jpg")
    assert _bytes(name) <= MAX_COMPONENT_BYTES
    assert name.endswith(".jpg")
    assert "\x00" not in name and "/" not in name
    # Nothing is silently renamed to a generic string: the ASCII head survives.
    assert name.startswith("GAYROCK2026")
    # Truncation happened → a stable digest is appended.
    assert "-" in name.split(".")[0]


def test_bug_title_is_stable_and_collision_free():
    a = sanitize_filename(BUG_TITLE, fallback="x", ext=".jpg")
    b = sanitize_filename(BUG_TITLE, fallback="x", ext=".jpg")
    assert a == b  # deterministic across calls/processes (blake2b, not hash())

    # A different long title with the SAME prefix must not collide.
    other = BUG_TITLE.replace("GAYROCK2026", "GAYROCK2027")
    c = sanitize_filename(other, fallback="x", ext=".jpg")
    assert c != a


def test_collisions_on_long_shared_prefix():
    base = "y" * 600
    one = sanitize_filename(base + "alpha", fallback="f")
    two = sanitize_filename(base + "beta", fallback="f")
    assert one != two
    assert _bytes(one) <= MAX_COMPONENT_BYTES
    assert _bytes(two) <= MAX_COMPONENT_BYTES


def test_emoji_preserved_in_default_mode():
    name = sanitize_filename("🇮🇷 cat 🐈 video 📹", fallback="f", ext=".mp4")
    assert name.endswith(".mp4")
    assert "🐈" in name and "📹" in name
    assert _bytes(name) <= MAX_COMPONENT_BYTES


def test_zwj_sequence_survives():
    name = sanitize_filename(f"flag {ZWJ_FLAG} report", fallback="f", ext=".txt")
    assert ZWJ_FLAG in name  # the ZWJ must NOT be stripped


def test_rtl_text_preserved():
    name = sanitize_filename("سلام دنیا — ویدیو.mp4", fallback="f")
    assert name.endswith(".mp4")
    assert any("\u0600" <= ch <= "\u06ff" for ch in name)
    assert _bytes(name) <= MAX_COMPONENT_BYTES
    # A bidi *override* inside the stem (used to disguise extensions) is gone.
    evil = sanitize_filename("photo\u202egnp.exe", fallback="f")
    assert "\u202e" not in evil
    assert evil.endswith(".exe")


def test_empty_and_junk_fall_back_to_id():
    assert sanitize_filename("", fallback="download_abc123") == "download_abc123"
    assert sanitize_filename(None, fallback="download_abc123") == "download_abc123"
    assert sanitize_filename("   ", fallback="d1") == "d1"
    assert sanitize_filename("...", fallback="d1") == "d1"
    assert sanitize_filename("///", fallback="d1") == "d1"
    assert sanitize_filename("\x00\x01\x02", fallback="d1") == "d1"
    # No fallback supplied → still a usable name.
    assert sanitize_filename("") == "file"


def test_windows_reserved_names():
    for raw in ("CON", "con", "PRN", "AUX", "NUL", "COM1", "com9", "LPT9"):
        out = sanitize_filename(raw, fallback="f")
        # The canonical workaround is a leading separator; the name must differ
        # from the reserved device name (a bare "CON" is unopenable on Windows).
        assert out != raw
        assert out.lstrip("_").upper() == raw.upper()
    # Reserved with an extension keeps the extension.
    assert sanitize_filename("nul.txt", fallback="f") == "_nul.txt"
    assert sanitize_filename("COM1.mp4", fallback="f") == "_COM1.mp4"


def test_nfkc_normalization():
    # Full-width digits/letters fold to ASCII...
    assert sanitize_filename("１２３ ＡＢＣ.txt", fallback="f") == "123 ABC.txt"
    # ...which means a full-width reserved name is caught too.
    assert sanitize_filename("ＣＯＮ.txt", fallback="f") == "_CON.txt"
    # NFKC is what turns NBSP into a plain space before collapsing.
    assert sanitize_filename("a\u00a0\u00a0b.txt", fallback="f") == "a b.txt"
    assert unicodedata.normalize("NFKC", "ﬁle") == "file"
    assert sanitize_filename("ﬁle.txt", fallback="f") == "file.txt"


def test_path_traversal_is_stripped():
    assert sanitize_filename("../../etc/passwd", fallback="f") == "passwd"
    assert sanitize_filename("/etc/shadow", fallback="f") == "shadow"
    assert sanitize_filename("C:\\Windows\\system32\\evil.exe", fallback="f") == "evil.exe"
    assert sanitize_filename("..\\..\\boot.ini", fallback="f") == "boot.ini"
    out = sanitize_filename("../../etc/passwd", fallback="f")
    assert "/" not in out and "\\" not in out and ".." not in out


def test_control_and_illegal_chars_replaced():
    out = sanitize_filename('a/b\\c:d*e?f"g<h>i|j\x00k\x1bl.txt', fallback="f")
    for bad in '/\\:*?"<>|':
        assert bad not in out
    assert out.endswith(".txt")


def test_trailing_dots_and_spaces_removed():
    out = sanitize_filename("report . . . ", fallback="f")
    assert not out.endswith(".")
    assert not out.endswith(" ")
    out2 = sanitize_filename("name...   ", fallback="f", keep_extension=False)
    assert not out2.endswith(".") and not out2.endswith(" ")


def test_whitespace_and_separator_runs_collapse():
    assert sanitize_filename("a    b___c.txt", fallback="f") == "a b_c.txt"
    assert sanitize_filename("a\n\n\tb.txt", fallback="f") == "a b.txt"


def test_forced_extension():
    assert sanitize_filename("clip", fallback="f", ext=".mp4") == "clip.mp4"
    assert sanitize_filename("clip.mp4", fallback="f", ext="mp4") == "clip.mp4"
    assert sanitize_filename("clip.webm", fallback="f", ext=".mp4") == "clip.mp4"
    # An extension never exceeds the byte budget.
    out = sanitize_filename("x" * 500, fallback="f", ext=".mp4", max_bytes=30)
    assert out.endswith(".mp4") and _bytes(out) <= 30


def test_truncation_never_splits_a_codepoint():
    # 4-byte emoji + 3-byte CJK; every budget must decode cleanly.
    text = ("🌋" * 40) + ("漢" * 40)
    for budget in range(1, 60):
        out = truncate_bytes(text, budget)
        assert _bytes(out) <= budget
        out.encode("utf-8").decode("utf-8")  # no partial sequence / surrogate
        assert "\ufffd" not in out
        assert "�" not in out


def test_byte_budget_is_enforced_for_every_input_class():
    inputs = [
        "🌋" * 200,
        "🌈🏳️" * 200,
        "漢" * 500,
        "x" * 1000,
        ("a" * 200) + ".jpg",
        "😀" * 100 + ".png",
    ]
    for raw in inputs:
        out = sanitize_filename(raw, fallback="f")
        assert _bytes(out) <= MAX_COMPONENT_BYTES, (raw[:20], _bytes(out))
        assert "/" not in out


def test_ascii_strict_mode():
    assert sanitize_filename("my file.pdf", fallback="f", ascii_only=True) == "my_file.pdf"
    assert sanitize_filename("😀😀.txt", fallback="fb", ascii_only=True) == "fb.txt"
    out = sanitize_filename("café — résumé.pdf", fallback="f", ascii_only=True)
    assert out.isascii()
    assert out.endswith(".pdf")
    # strip_dirs=False flattens separators instead of taking the basename —
    # required when composing "repo_branch" style stems.
    out2 = sanitize_filename(
        "myrepo_feature/foo", fallback="f", ascii_only=True, strip_dirs=False
    )
    assert out2 == "myrepo_feature_foo"
    assert _bytes(sanitize_filename("x" * 500, fallback="f", ascii_only=True, max_bytes=40)) <= 40


def test_uniqueness_suffix_only_on_truncation():
    short = sanitize_filename("clip.mp4", fallback="f")
    assert short == "clip.mp4"  # untruncated names stay clean
    long = sanitize_filename("c" * 500 + ".mp4", fallback="f")
    assert long != "clip.mp4"

    # Two truncations whose stems differ but whose first N chars are equal
    # must still be distinct (hash of the ORIGINAL, not the truncated output).
    a = sanitize_filename("z" * 300 + "-one", fallback="f", max_bytes=50)
    b = sanitize_filename("z" * 300 + "-two", fallback="f", max_bytes=50)
    assert a != b


def test_id_based_fallback_when_nothing_usable():
    # A name made only of characters the sanitizer removes in strict mode.
    out = sanitize_filename("🎉🎉🎉", fallback="photo_1699999999", ascii_only=True)
    assert out == "photo_1699999999"


def test_legacy_constants_stay_under_name_max():
    assert MAX_COMPONENT_BYTES < 255
    assert MAX_TITLE_BYTES < MAX_COMPONENT_BYTES < 255
    assert MAX_LEGACY_TASK_BYTES <= MAX_COMPONENT_BYTES


def test_result_is_a_single_safe_component():
    for raw in ("../../x", "a/b", "C:\\y", "🌋/z", "..", "./"):
        out = sanitize_filename(raw, fallback="f")
        assert os.path.basename(out) == out
        assert out not in (".", "..")
        assert "/" not in out and "\\" not in out


def test_shared_helpers_still_route_through_sanitizer():
    from utils.security import safe_task_filename

    # legacy contract: ASCII-only, bounded, keeps the extension.
    out = safe_task_filename("café 🌋 report.mp4", "fallback", ext=".mp4")
    assert out.isascii() and out.endswith(".mp4")
    assert _bytes(out) <= MAX_LEGACY_TASK_BYTES
    assert safe_task_filename(None, "download_abc") == "download_abc"
    long = safe_task_filename("x" * 400 + ".mkv", "f", ext=".mkv")
    assert _bytes(long) <= MAX_LEGACY_TASK_BYTES and long.endswith(".mkv")

    from modules.github.handlers import safe_cache_filename

    assert safe_cache_filename("repo/branch", "fb") == "repo_branch"
    assert safe_cache_filename("", "fb") == "fb"
    assert _bytes(safe_cache_filename("p" * 400, "fb")) <= 200


def _run() -> int:
    tests = sorted(
        (n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)
    )
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback

            print(f"FAIL  {name}: {exc!r}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())

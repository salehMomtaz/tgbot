"""Regression tests for ``utils/gate.py`` database persistence.

Guards the 2026-11-06 audit finding: ``save_database`` used a plain
``open('w') + json.dump``, so a crash mid-write left a truncated
``database.json`` and ``load_database`` then silently fell back to an EMPTY
default database — wiping the authorized, blacklist and premium lists with no
warning. Writes are now temp-file + fsync + ``os.replace`` (atomic).

Standalone: ``venv/bin/python tests/test_gate_db.py`` (pytest-compatible).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils.gate as gate  # noqa: E402


def _with_tmp_db(fn):
    """Run *fn(db_path)* with utils.gate.DB_FILE pointed at a temp file."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "database.json")
        old = gate.DB_FILE
        gate.DB_FILE = path
        try:
            return fn(path)
        finally:
            gate.DB_FILE = old


def test_default_db_is_created_atomically():
    def body(path):
        db = gate.load_database()
        assert db["authorized"] == [] and db["blacklisted"] == []
        assert os.path.exists(path)
        with open(path) as f:
            assert json.load(f) == db
        # No scratch files left behind by the atomic writer.
        leftovers = [n for n in os.listdir(os.path.dirname(path)) if ".tmp." in n]
        assert leftovers == [], leftovers

    _with_tmp_db(body)


def test_mid_write_crash_keeps_previous_database():
    def body(path):
        good = {"authorized": [11], "blacklisted": [99],
                "document_mode": [], "premium_users": []}
        gate.save_database(good)
        assert gate.load_database()["authorized"] == [11]

        orig_dump = gate.json.dump

        def boom(*_a, **_k):
            raise RuntimeError("simulated disk full")

        gate.json.dump = boom
        try:
            try:
                gate.save_database({"authorized": [11, 22], "blacklisted": []})
            except RuntimeError:
                pass
        finally:
            gate.json.dump = orig_dump

        # The pre-crash database must still be intact and parseable.
        db = gate.load_database()
        assert db["authorized"] == [11], db
        assert db["blacklisted"] == [99], db
        leftovers = [n for n in os.listdir(os.path.dirname(path)) if ".tmp." in n]
        assert leftovers == [], leftovers

    _with_tmp_db(body)


def test_migration_and_roundtrip():
    def body(path):
        gate.save_database({"authorized": [5], "blacklisted": [],
                            "document_mode": [], "premium_users": []})
        assert gate.is_authorized(5) is True
        assert gate.is_blacklisted(5) is False
        gate.blacklist_user(5)
        assert gate.is_blacklisted(5) is True
        assert gate.is_authorized(5) is False

    _with_tmp_db(body)


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

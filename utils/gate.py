import json
import logging
import os
import threading
import time
from config import SYSTEM_CREATOR_ID, DB_FILE

logger = logging.getLogger(__name__)


def _quarantine_corrupt_db() -> str | None:
    """Copy an unparseable database aside before defaults are written over it.

    ``load_database`` used to swallow a JSON error and return defaults; the very
    next mutation then persisted that empty DB over the real file, silently
    wiping ``authorized`` / ``blacklisted`` / ``premium_users`` with no
    evidence. Keeping a timestamped copy makes the failure loud and recoverable
    (restore + fix the JSON), which is the whole point of the atomic writer.
    """
    try:
        dst = f"{DB_FILE}.corrupt.{int(time.time())}"
        with open(DB_FILE, "rb") as src, open(dst, "wb") as out:
            out.write(src.read())
        os.chmod(dst, 0o600)
        logger.error("database.json is corrupt — preserved a copy at %s before "
                     "continuing with defaults; restore it to recover the user lists", dst)
        return dst
    except Exception as e:  # noqa: BLE001
        logger.error("database.json is corrupt AND could not be backed up: %r", e)
        return None


def load_database() -> dict:
    """Load authorized, blacklisted users, and settings from database."""
    default_db = {
        "authorized": [],
        "blacklisted": [],
        "document_mode": [],
        "premium_users": []
    }
    if not os.path.exists(DB_FILE):
        save_database(default_db)
        return default_db
    try:
        with open(DB_FILE, 'r') as f:
            data = json.load(f)
            
            # Migrate legacy list-only databases to new dictionary structure
            if isinstance(data, list):
                migrated = {
                    "authorized": data,
                    "blacklisted": [],
                    "document_mode": [],
                    "premium_users": []
                }
                save_database(migrated)
                return migrated
            
            # Enforce key integrity across database upgrades
            if "authorized" not in data:
                data["authorized"] = []
            if "blacklisted" not in data:
                data["blacklisted"] = []
            if "document_mode" not in data:
                data["document_mode"] = []
            if "premium_users" not in data:
                data["premium_users"] = []
            return data
    except Exception as e:  # noqa: BLE001
        logger.error("Could not parse %s: %r", DB_FILE, e)
        _quarantine_corrupt_db()
        return default_db

def save_database(data: dict):
    """Atomically persist the database (temp file + fsync + os.replace).

    A plain ``open('w')`` truncated the file when the process died mid-write,
    and ``load_database`` then silently returned defaults — wiping the
    authorized / blacklist / premium lists with no warning. The unique temp
    name keeps concurrent writers (threads / executors) from clobbering the
    same scratch file.
    """
    tmp = f"{DB_FILE}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp, 'w') as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, DB_FILE)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass

def is_authorized(user_id: int) -> bool:
    if user_id == SYSTEM_CREATOR_ID:
        return True
    db = load_database()
    return user_id in db["authorized"]

def is_blacklisted(user_id: int) -> bool:
    db = load_database()
    return user_id in db["blacklisted"]

def blacklist_user(user_id: int):
    """Ban an unauthorized intruder and strip their whitelist access if present."""
    db = load_database()
    if user_id not in db["blacklisted"] and user_id != SYSTEM_CREATOR_ID:
        db["blacklisted"].append(user_id)
        if user_id in db["authorized"]:
            db["authorized"].remove(user_id)
        save_database(db)

def unblacklist_user(user_id: int) -> bool:
    db = load_database()
    if user_id in db["blacklisted"]:
        db["blacklisted"].remove(user_id)
        save_database(db)
        return True
    return False

def add_user(user_id: int) -> bool:
    db = load_database()
    if user_id not in db["authorized"]:
        db["authorized"].append(user_id)
        if user_id in db["blacklisted"]:
            db["blacklisted"].remove(user_id)
        save_database(db)
        return True
    return False

def remove_user(user_id: int) -> bool:
    db = load_database()
    if user_id in db["authorized"]:
        db["authorized"].remove(user_id)
        save_database(db)
        return True
    return False

def is_document_mode(user_id: int) -> bool:
    db = load_database()
    return user_id in db["document_mode"]

def toggle_document_mode(user_id: int) -> bool:
    """Toggle document mode for a user and return the new state (True=ON, False=OFF)."""
    db = load_database()
    if user_id in db["document_mode"]:
        db["document_mode"].remove(user_id)
        state = False
    else:
        db["document_mode"].append(user_id)
        state = True
    save_database(db)
    return state


def is_premium_user(user_id: int) -> bool:
    """True if *user_id* is whitelisted for 4 GB Premium uploads.

    The bot creator is implicitly premium: they configured the Premium
    userbot session and must always be able to use it.
    """
    if user_id == SYSTEM_CREATOR_ID:
        return True
    db = load_database()
    return user_id in db["premium_users"]


def add_premium_user(user_id: int) -> bool:
    """Whitelist *user_id* for the 4 GB Premium upload path."""
    db = load_database()
    if user_id not in db["premium_users"]:
        db["premium_users"].append(user_id)
        save_database(db)
        return True
    return False


def remove_premium_user(user_id: int) -> bool:
    """Remove *user_id* from the 4 GB Premium upload whitelist."""
    db = load_database()
    if user_id in db["premium_users"]:
        db["premium_users"].remove(user_id)
        save_database(db)
        return True
    return False

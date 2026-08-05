import json
import os
from config import SYSTEM_CREATOR_ID, DB_FILE

def load_database() -> dict:
    """Load authorized, blacklisted users, and settings from database."""
    default_db = {
        "authorized": [],
        "blacklisted": [],
        "document_mode": [],
        "premium_users": []
    }
    if not os.path.exists(DB_FILE):
        with open(DB_FILE, 'w') as f:
            json.dump(default_db, f)
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
    except Exception:
        return default_db

def save_database(data: dict):
    with open(DB_FILE, 'w') as f:
        json.dump(data, f)

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

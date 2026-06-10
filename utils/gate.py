import json
import os
from config import SYSTEM_CREATOR_ID, DB_FILE

def load_database() -> dict:
    """Load authorized and blacklisted users from database."""
    default_db = {"authorized": [], "blacklisted": []}
    if not os.path.exists(DB_FILE):
        with open(DB_FILE, 'w') as f:
            json.dump(default_db, f)
        return default_db
    try:
        with open(DB_FILE, 'r') as f:
            data = json.load(f)
            # Handle migration from legacy pure lists to key-value dicts
            if isinstance(data, list):
                migrated = {"authorized": data, "blacklisted": []}
                save_database(migrated)
                return migrated
            if "authorized" not in data:
                data["authorized"] = []
            if "blacklisted" not in data:
                data["blacklisted"] = []
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
